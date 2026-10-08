"""
DUPLICATE FINDER v 0.9.1
Интеллектуальный поиск дубликатов файлов с графическим интерфейсом
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import os
import sys
import threading
import queue
import json
# import sqlite3 # >>> cache.py >>>
import logging 
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import subprocess

# --- опциональные зависимости ------------------------------------------------

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --- Локальный пакет ------------------------------------------------
from andup.constants import (
    DF_VERSION, DF_VERSION_TEXT, BLOCK_SIZE, FUTURES_BATCH,
    LOG_FILE, REPORTS_DIR, IMG_EXTS,
)
from andup.themes import LIGHT, DARK
from andup.config import load_settings, save_settings, DEFAULT_SETTINGS
from andup.utils import (
    HAS_SEND2TRASH, check_protected, move_to_trash,
    create_hardlink, format_size, normalize_path,
)
from andup.hashing import calculate_hash as _hash_full, calculate_quick_hash as _hash_quick
from andup.cache import HashCache
from andup.reporter import (
    build_html_report, build_txt_report, autosave_report,
    write_results_json, write_results_csv,
    write_group_txt, write_group_json, write_group_csv,
    write_checked_list,
)
from andup.session import save_session as _save_session, load_session as _load_session


logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("df")

# ============================================================================
#                                SQLite-КЭШ
# ============================================================================

# # >>> cache.py >>>
# class HashCache:
#     SCHEMA = """
#         CREATE TABLE IF NOT EXISTS hash_cache (
#             path TEXT PRIMARY KEY,
#             hash TEXT NOT NULL,
#             algo TEXT NOT NULL,
#             size INTEGER NOT NULL,
#             mtime REAL NOT NULL,
#             timestamp REAL
#         );
#         CREATE INDEX IF NOT EXISTS idx_path ON hash_cache(path);
#     """

#     def __init__(self, db_path="hash_cache.db"):
#         self.db_path = db_path
#         self.lock = threading.Lock()
#         self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
#         try:
#             self.conn.execute("PRAGMA journal_mode=WAL;")
#             self.conn.execute("PRAGMA synchronous=NORMAL;")
#         except sqlite3.OperationalError:
#             pass
#         self.conn.executescript(self.SCHEMA)
#         self.conn.commit()

#     def get(self, filepath, algorithm, size, mtime):
#         with self.lock:
#             cur = self.conn.execute(
#                 "SELECT hash, size, mtime FROM hash_cache WHERE path = ? AND algo = ?",
#                 (filepath, algorithm))
#             row = cur.fetchone()
#         if row is None:
#             return None
#         h, cs, cm = row
#         if cs == size and abs(cm - mtime) < 1:
#             return h
#         return None

#     def set(self, filepath, algorithm, hash_val, size, mtime):
#         with self.lock:
#             self.conn.execute(
#                 "INSERT OR REPLACE INTO hash_cache "
#                 "(path, hash, algo, size, mtime, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
#                 (filepath, hash_val, algorithm, size, mtime, time.time()))

#     def delete(self, filepath):
#         with self.lock:
#             self.conn.execute("DELETE FROM hash_cache WHERE path = ?", (filepath,))

#     def flush(self):
#         with self.lock:
#             self.conn.commit()

#     def clear(self):
#         with self.lock:
#             self.conn.execute("DELETE FROM hash_cache")
#             self.conn.commit()
#             try:
#                 self.conn.execute("VACUUM;")
#             except sqlite3.OperationalError:
#                 pass

#     def count(self):
#         with self.lock:
#             cur = self.conn.execute("SELECT COUNT(*) FROM hash_cache")
#             return cur.fetchone()[0] or 0

#     def size_bytes(self):
#         try:
#             return os.path.getsize(self.db_path)
#         except OSError:
#             return 0

#     def close(self):
#         with self.lock:
#             if self.conn:
#                 try:
#                     self.conn.commit()
#                 except Exception:
#                     pass
#                 self.conn.close()
#                 self.conn = None

# ============================================================================
# ПРИЛОЖЕНИЕ
# ============================================================================
class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Duplicate Finder v {DF_VERSION}")
        self.settings = load_settings()
        self.root.geometry(self.settings.get("window_geometry", "1400x900"))
        self.root.minsize(1000, 640)

        try:
            self.root.iconbitmap("icon.ico")
        except Exception:
            pass

        self.theme = DARK if self.settings.get("dark_theme") else LIGHT

        self.queue = queue.Queue()
        self.folder_path = tk.StringVar()
        self.search_in_progress = False
        self.stop_search = False

        try:
            self.cache = HashCache("hash_cache.db")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть кэш:\n{e}")
            self.cache = None

        self.current_group = 0
        self.duplicates = {}
        self.duplicate_keys = []
        self._tree_map = {}

        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_lock = threading.Lock()

        self.stats = {
            "total_files": 0, "duplicate_groups": 0, "duplicate_files": 0,
            "start_time": None, "end_time": None,
            "total_size": 0, "wasted_size": 0,
        }

        self._progress_start_ts = None

        # превью (вкладка "По группам")
        self._preview_photo = None
        self._preview_path = None

        # состояние вкладки "Все дубликаты"
        self._all_checked = set()
        self._thumb_cache = {}

        self.setup_styles()
        self.create_widgets()
        self.process_queue()

        log.info("Запуск v%s", DF_VERSION)

    # ==================================================================
    #                             СТИЛИ
    # ==================================================================
    def setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        t = self.theme
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"],
                        font=("Arial", 10))
        style.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"],
                        font=("Arial", 9))
        style.configure("H1.TLabel", background=t["bg"], foreground=t["accent"],
                        font=("Arial", 14, "bold"))
        style.configure("TLabelframe", background=t["bg"], foreground=t["accent"])
        style.configure("TLabelframe.Label", background=t["bg"],
                        foreground=t["accent"], font=("Arial", 10, "bold"))
        style.configure("TButton", font=("Arial", 10))
        style.configure("Accent.TButton", font=("Arial", 10, "bold"))
        style.configure("TCheckbutton", font=("Arial", 10), background=t["bg"],
                        foreground=t["fg"])
        style.map("TCheckbutton",
                  background=[("active", t["bg"])],
                  foreground=[("active", t["fg"])])
        style.configure("TRadiobutton", background=t["bg"], foreground=t["fg"])
        style.configure("TEntry", fieldbackground=t["entry_bg"],
                        foreground=t["fg"])
        style.configure("TCombobox", fieldbackground=t["entry_bg"],
                        foreground=t["fg"])
        style.configure("Treeview",
                        background=t["tree_bg"], foreground=t["tree_fg"],
                        fieldbackground=t["tree_bg"],
                        font=("Arial", 9), rowheight=24)
        style.map("Treeview",
                  background=[("selected", t["tree_sel"])],
                  foreground=[("selected", "white")])
        style.configure("Treeview.Heading", font=("Arial", 9, "bold"))
        style.configure("TNotebook", background=t["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 6), font=("Arial", 10))
        style.configure("TProgressbar", background=t["info"])

        self.root.configure(bg=t["bg"])

    def toggle_theme(self):
        self.settings["dark_theme"] = not self.settings.get("dark_theme", False)
        self.theme = DARK if self.settings["dark_theme"] else LIGHT
        save_settings(self.settings)
        self.setup_styles()
        self._restyle_custom_widgets()
        self.update_group_display()
        self._refresh_all_tree()

    def _restyle_custom_widgets(self):
        t = self.theme
        for w in (getattr(self, "text_output", None),
                  getattr(self, "preview_text", None)):
            if w is not None:
                try:
                    w.configure(bg=t["text_bg"], fg=t["text_fg"],
                                insertbackground=t["fg"])
                except tk.TclError:
                    pass
        if hasattr(self, "tree"):
            self.tree.tag_configure("first", background=t["row_first"])
        if hasattr(self, "all_tree"):
            self.all_tree.tag_configure("group", background=t["panel"])
            self.all_tree.tag_configure("first", background=t["row_first"])
        if hasattr(self, "preview_label"):
            self.preview_label.configure(bg=t["panel"], fg=t["fg"])

    # ==================================================================
    # UI
    # ==================================================================
    def create_widgets(self):
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=12, pady=(10, 6))

        ttk.Label(header, text="🔍 DUPLICATE FINDER",
                  style="H1.TLabel").pack(side=tk.LEFT)
        ttk.Label(header,
                  text=f"   {DF_VERSION_TEXT} | SQLite-кэш | 2-этапное хеширование",
                  style="Muted.TLabel").pack(side=tk.LEFT)

        self.btn_theme = ttk.Button(header, text="🌙 Тема",
                                    command=self.toggle_theme, width=12)
        self.btn_theme.pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        self.tab_search = ttk.Frame(self.notebook)
        self.tab_all = ttk.Frame(self.notebook)
        self.tab_results = ttk.Frame(self.notebook)
        self.tab_logs = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_search, text="  🔍 Поиск  ")
        self.notebook.add(self.tab_all, text="  🌲 Все дубликаты  ")
        self.notebook.add(self.tab_results, text="  📋 По группам  ")
        self.notebook.add(self.tab_logs, text="  📝 Логи  ")

        self.create_search_tab()
        self.create_all_tab()
        self.create_results_tab()
        self.create_logs_tab()

        status = ttk.Frame(self.root)
        status.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.status_var = tk.StringVar(value="✅ Готов к работе")
        ttk.Label(status, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=(8, 4), font=("Arial", 9)
                  ).pack(fill=tk.X)

        self.setup_hotkeys()

    # ------------------------------------------------------------------
    def create_search_tab(self):
        outer = ttk.Frame(self.tab_search)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        folder = ttk.LabelFrame(outer, text="📁 Исходная папка", padding=12)
        folder.pack(fill=tk.X, pady=(0, 12))
        folder.columnconfigure(0, weight=1)

        self.folder_combo = ttk.Combobox(folder, textvariable=self.folder_path,
                                         font=("Consolas", 10),
                                         values=self.settings.get("recent_paths", []))
        self.folder_combo.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 10))

        btns = ttk.Frame(folder)
        btns.grid(row=0, column=1, sticky=tk.E)
        ttk.Button(btns, text="📂 Папка",
                   command=self.select_folder, width=12).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="💿 Диск",
                   command=self.select_drive, width=12).pack(side=tk.LEFT)

        cfg = ttk.LabelFrame(outer, text="⚙️ Настройки поиска", padding=12)
        cfg.pack(fill=tk.X, pady=(0, 12))
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="Алгоритм:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self.hash_algo = tk.StringVar(value=self.settings.get("last_algorithm", "sha256"))
        ttk.Combobox(cfg, textvariable=self.hash_algo,
                     values=["md5", "sha1", "sha256"],
                     width=10, state="readonly").grid(row=0, column=1, sticky=tk.W)

        ttk.Label(cfg, text="Потоки:").grid(row=0, column=2, sticky=tk.W, padx=(20, 6))
        self.thread_count = tk.IntVar(value=self.settings.get("last_threads", 2))
        ttk.Spinbox(cfg, from_=1, to=32, width=5,
                    textvariable=self.thread_count).grid(row=0, column=3, sticky=tk.W)
        ttk.Label(cfg, text="(HDD: 1, SATA SSD: 2–4, NVMe: 4–8)",
                  style="Muted.TLabel").grid(row=0, column=4, sticky=tk.W, padx=(10, 0))

        self.recursive_var = tk.BooleanVar(value=self.settings.get("last_recursive", True))
        ttk.Checkbutton(cfg, text="Рекурсивно",
                        variable=self.recursive_var).grid(row=0, column=5, sticky=tk.W,
                                                          padx=(20, 0))
        self.use_cache_var = tk.BooleanVar(value=self.settings.get("last_use_cache", True))
        ttk.Checkbutton(cfg, text="Кэш",
                        variable=self.use_cache_var).grid(row=0, column=6, sticky=tk.W,
                                                          padx=(20, 0))

        ttk.Label(cfg, text="Расширения:").grid(row=1, column=0, sticky=tk.W,
                                                pady=(10, 0), padx=(0, 6))
        self.extensions_var = tk.StringVar(
            value=self.settings.get("last_extensions", DEFAULT_SETTINGS["last_extensions"]))
        ttk.Entry(cfg, textvariable=self.extensions_var,
                  font=("Consolas", 9)
                  ).grid(row=1, column=1, columnspan=6, sticky=(tk.W, tk.E), pady=(10, 0))

        row3 = ttk.Frame(cfg)
        row3.grid(row=2, column=0, columnspan=7, sticky=(tk.W, tk.E), pady=(10, 0))
        row3.columnconfigure(1, weight=1)

        ttk.Label(row3, text="Игнорировать:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self.ignore_var = tk.StringVar(
            value=", ".join(self.settings.get("ignore_folders", [])))
        ttk.Entry(row3, textvariable=self.ignore_var,
                  font=("Consolas", 9)).grid(row=0, column=1, sticky=(tk.W, tk.E))

        self.trash_var = tk.BooleanVar(value=self.settings.get("delete_to_trash", True))
        trash_text = "🗑️ Удалять в корзину" if HAS_SEND2TRASH else "🗑️ В локальную корзину"
        ttk.Checkbutton(row3, text=trash_text,
                        variable=self.trash_var).grid(row=0, column=2, padx=(12, 0))

        cache_btns = ttk.Frame(cfg)
        cache_btns.grid(row=3, column=0, columnspan=7, sticky=tk.W, pady=(12, 0))
        ttk.Button(cache_btns, text="🗑️ Очистить кэш",
                   command=self.clear_cache, width=18).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(cache_btns, text="📊 Статистика кэша",
                   command=self.show_cache_stats, width=18).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(cache_btns, text="📂 Папка отчётов",
                   command=self.open_results_folder, width=20).pack(side=tk.LEFT)

        prog = ttk.LabelFrame(outer, text="📊 Прогресс", padding=12)
        prog.pack(fill=tk.X, pady=(0, 12))
        prog.columnconfigure(0, weight=1)

        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(prog, variable=self.progress_var,
                        maximum=100, mode="determinate"
                        ).grid(row=0, column=0, sticky=(tk.W, tk.E))
        self.progress_label = ttk.Label(prog, text="Готов к работе",
                                        font=("Arial", 10, "bold"),
                                        foreground=self.theme["accent"])
        self.progress_label.grid(row=0, column=1, sticky=tk.E, padx=(12, 0))

        self.progress_detail_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.progress_detail_var,
                  style="Muted.TLabel", font=("Consolas", 9)
                  ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        acts = ttk.Frame(outer)
        acts.pack(fill=tk.X)
        self.btn_start = ttk.Button(acts, text="▶️ НАЧАТЬ ПОИСК",
                                    command=self.start_search, width=20,
                                    style="Accent.TButton")
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_stop = ttk.Button(acts, text="⏹️ СТОП",
                                   command=self.stop_search_func,
                                   width=14, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_export = ttk.Button(acts, text="💾 ЭКСПОРТ",
                                     command=self.export_results,
                                     width=14, state=tk.DISABLED)
        self.btn_export.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(acts, text="📄 HTML-отчёт",
                   command=self.export_html_report,
                   width=16).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Separator(acts, orient="vertical").pack(side=tk.LEFT, fill=tk.Y,
                                                    padx=8, pady=2)
        ttk.Button(acts, text="💾 Сохранить сессию",
                   command=self.save_session, width=20).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(acts, text="📂 Загрузить сессию",
                   command=self.load_session, width=20).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    def create_all_tab(self):
        outer = ttk.Frame(self.tab_all)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        top = ttk.Frame(outer)
        top.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(top, text="🔎 Фильтр:").pack(side=tk.LEFT, padx=(0, 4))
        self.all_filter_var = tk.StringVar()
        f_entry = ttk.Entry(top, textvariable=self.all_filter_var, width=32)
        f_entry.pack(side=tk.LEFT, padx=(0, 12))
        f_entry.bind("<KeyRelease>", lambda e: self._refresh_all_tree())

        ttk.Button(top, text="☑ Все кроме 1-го",
                   command=self._check_all_but_first_global,
                   width=20).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="⬜ Снять всё",
                   command=self._uncheck_all_global,
                   width=14).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="↕ Развернуть",
                   command=lambda: self._expand_all(True),
                   width=14).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="↕ Свернуть",
                   command=lambda: self._expand_all(False),
                   width=14).pack(side=tk.LEFT)

        self.all_summary_var = tk.StringVar(value="Пока нет данных")
        ttk.Label(top, textvariable=self.all_summary_var,
                  font=("Arial", 10, "bold"),
                  foreground=self.theme["accent"]).pack(side=tk.RIGHT)

        tf = ttk.Frame(outer)
        tf.pack(fill=tk.BOTH, expand=True)
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)

        columns = ("check", "size", "mtime", "path")
        self.all_tree = ttk.Treeview(tf, columns=columns,
                                     show="tree headings",
                                     selectmode="extended")
        self.all_tree.heading("#0", text="📦 Группа / 📄 Файл")
        self.all_tree.heading("check", text="☑")
        self.all_tree.heading("size", text="Размер")
        self.all_tree.heading("mtime", text="Изменён")
        self.all_tree.heading("path", text="Путь")

        self.all_tree.column("#0", width=380, anchor="w", stretch=False)
        self.all_tree.column("check", width=42, anchor="center", stretch=False)
        self.all_tree.column("size", width=90, anchor="e", stretch=False)
        self.all_tree.column("mtime", width=145, anchor="center", stretch=False)
        self.all_tree.column("path", width=520, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.all_tree.yview)
        self.all_tree.configure(yscrollcommand=vsb.set)
        self.all_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        vsb.grid(row=0, column=1, sticky=(tk.N, tk.S))

        self.all_tree.tag_configure("group",
                                    background=self.theme["panel"],
                                    font=("Arial", 10, "bold"))
        self.all_tree.tag_configure("first",
                                    background=self.theme["row_first"])

        self.all_tree.bind("<Button-1>", self._on_all_tree_click)
        self.all_tree.bind("<space>", self._on_all_tree_space)
        self.all_tree.bind("<Double-1>", self._on_all_tree_dblclick)
        self.all_tree.bind("<Button-3>", self._on_all_tree_right_click)
        self.all_tree.bind("<Delete>", lambda e: self.delete_checked_global())

        bottom = ttk.Frame(outer)
        bottom.pack(fill=tk.X, pady=(10, 0))

        self.all_selected_var = tk.StringVar(value="Выбрано: 0 файлов")
        ttk.Label(bottom, textvariable=self.all_selected_var,
                  font=("Arial", 10, "bold"),
                  foreground=self.theme["info"]).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Button(bottom, text="🗑️ Удалить отмеченные",
                   command=self.delete_checked_global,
                   width=22).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bottom, text="🔗 Заменить хардлинками",
                   command=self.hardlink_checked_global,
                   width=24).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bottom, text="💾 Экспорт отмеченных",
                   command=self.export_checked_global,
                   width=20).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    def create_results_tab(self):
        outer = ttk.Frame(self.tab_results)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        top = ttk.Frame(outer)
        top.pack(fill=tk.X, pady=(0, 8))

        nav = ttk.Frame(top)
        nav.pack(side=tk.LEFT)
        ttk.Button(nav, text="◀◀", command=lambda: self.go_to_group(0),
                   width=4).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(nav, text="◀", command=self.prev_group,
                   width=4).pack(side=tk.LEFT, padx=(0, 4))
        self.group_label = ttk.Label(nav, text="Группа 0/0",
                                     font=("Arial", 11, "bold"),
                                     foreground=self.theme["accent"], width=18,
                                     anchor=tk.CENTER)
        self.group_label.pack(side=tk.LEFT, padx=6)
        ttk.Button(nav, text="▶", command=self.next_group,
                   width=4).pack(side=tk.LEFT, padx=(4, 4))
        ttk.Button(nav, text="▶▶", command=self.go_to_last_group,
                   width=4).pack(side=tk.LEFT)

        ttk.Label(nav, text="   К группе:").pack(side=tk.LEFT, padx=(16, 4))
        self.group_entry = ttk.Entry(nav, width=7)
        self.group_entry.pack(side=tk.LEFT, padx=(0, 4))
        self.group_entry.bind("<Return>", lambda e: self.go_to_group_entry())

        self.summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.summary_var,
                  font=("Arial", 10)).pack(side=tk.RIGHT)

        self.group_info_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.group_info_var,
                  font=("Consolas", 9), foreground=self.theme["muted"],
                  anchor=tk.W, justify=tk.LEFT).pack(fill=tk.X, pady=(0, 6))

        ctrl = ttk.Frame(outer)
        ctrl.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(ctrl, text="🔎 Фильтр:").pack(side=tk.LEFT, padx=(0, 4))
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(ctrl, textvariable=self.filter_var, width=40)
        filter_entry.pack(side=tk.LEFT, padx=(0, 16))
        filter_entry.bind("<KeyRelease>", lambda e: self._apply_filter())

        ttk.Label(ctrl, text="Оставить:").pack(side=tk.LEFT, padx=(0, 4))
        self.strategy_var = tk.StringVar(
            value=self.settings.get("auto_select_strategy", "oldest"))
        ttk.Combobox(ctrl, textvariable=self.strategy_var,
                     values=["oldest", "newest", "shortest_path", "longest_path"],
                     width=16, state="readonly").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(ctrl, text="⬇ Авто-выбор",
                   command=self._auto_select, width=14).pack(side=tk.LEFT)

        pane = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        right = ttk.LabelFrame(pane, text="👁 Предпросмотр", padding=8)
        pane.add(right, weight=1)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        columns = ("num", "name", "size", "mtime", "path")
        self.tree = ttk.Treeview(left, columns=columns,
                                 show="headings", selectmode="extended")
        self.tree.heading("num", text="#")
        self.tree.heading("name", text="Имя файла")
        self.tree.heading("size", text="Размер")
        self.tree.heading("mtime", text="Изменён")
        self.tree.heading("path", text="Путь")

        self.tree.column("num", width=40, anchor="center", stretch=False)
        self.tree.column("name", width=260, anchor="w", stretch=False)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("mtime", width=145, anchor="center", stretch=False)
        self.tree.column("path", width=500, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        vsb.grid(row=0, column=1, sticky=(tk.N, tk.S))

        self.tree.tag_configure("first", background=self.theme["row_first"])

        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self.preview_label = tk.Label(right, text="Выберите файл",
                                      bg=self.theme["panel"],
                                      fg=self.theme["fg"],
                                      font=("Arial", 10))
        self.preview_label.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 6))

        self.preview_text = tk.Text(right, height=15,
                                    bg=self.theme["text_bg"],
                                    fg=self.theme["text_fg"],
                                    font=("Consolas", 9), wrap=tk.WORD,
                                    relief=tk.FLAT)
        pvsb = ttk.Scrollbar(right, orient="vertical",
                             command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=pvsb.set)
        self.preview_text.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        pvsb.grid(row=1, column=1, sticky=(tk.N, tk.S))

        self.ctx_menu = tk.Menu(self.root, tearoff=0)
        self.ctx_menu.add_command(label="📄 Открыть файл", command=self._ctx_open)
        self.ctx_menu.add_command(label="📁 Открыть папку", command=self._ctx_open_folder)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="📋 Копировать путь", command=self._ctx_copy)
        self.ctx_menu.add_command(label="⭐ Сделать эталоном",
                                  command=self._ctx_set_reference)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="🗑️ Удалить выбранные",
                                  command=self.delete_selected_files)
        self.ctx_menu.add_command(label="🔗 Заменить хардлинком на эталон",
                                  command=self.replace_with_hardlinks)

        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Delete>", lambda e: self.delete_selected_files())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_preview())

        acts = ttk.Frame(outer)
        acts.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(acts, text="☑ Все",
                   command=self._select_all, width=8).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(acts, text="☑ Все кроме 1-го",
                   command=self._select_all_but_first, width=18).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(acts, text="⬜ Снять",
                   command=lambda: self.tree.selection_remove(self.tree.selection()),
                   width=10).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Button(acts, text="🗑️ Удалить выбранные",
                   command=self.delete_selected_files,
                   width=22).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(acts, text="🗑️ Удалить все дубликаты",
                   command=self.delete_all_duplicates,
                   width=24).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(acts, text="🔗 Заменить хардлинками",
                   command=self.replace_with_hardlinks,
                   width=24).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(acts, text="💾 Экспорт группы",
                   command=self.export_current_group,
                   width=18).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    def create_logs_tab(self):
        outer = ttk.Frame(self.tab_logs)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        bar = ttk.Frame(outer)
        bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(bar, text="🗑️ Очистить лог",
                   command=lambda: self.text_output.delete(1.0, tk.END),
                   width=16).pack(side=tk.LEFT)
        ttk.Label(bar, text=f"   Файл лога: {LOG_FILE}",
                  style="Muted.TLabel").pack(side=tk.LEFT)

        self.text_output = scrolledtext.ScrolledText(
            outer, font=("Consolas", 9), wrap=tk.WORD,
            bg=self.theme["text_bg"], fg=self.theme["text_fg"],
            insertbackground=self.theme["fg"])
        self.text_output.pack(fill=tk.BOTH, expand=True)

        self.text_output.tag_config("success", foreground=self.theme["success"])
        self.text_output.tag_config("error", foreground=self.theme["warning"])
        self.text_output.tag_config("warning", foreground="#e67e22")
        self.text_output.tag_config("info", foreground=self.theme["info"])

    # ==================================================================
    #                           ХОТКЕИ
    # ==================================================================
    def _focus_in_input(self):
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, ttk.Entry, tk.Text,
                              ttk.Combobox, ttk.Spinbox))

    def setup_hotkeys(self):
        def guard(fn):
            def _w(event):
                if self._focus_in_input():
                    return None
                return fn(event)
            return _w
        self.root.bind("<F5>", lambda e: self.start_search())
        self.root.bind("<Escape>", lambda e: self.stop_search_func())
        self.root.bind("<Control-Left>", guard(lambda e: self.prev_group()))
        self.root.bind("<Control-Right>", guard(lambda e: self.next_group()))
        self.root.bind("<Control-Home>", guard(lambda e: self.go_to_group(0)))
        self.root.bind("<Control-End>", guard(lambda e: self.go_to_last_group()))
        self.root.bind("<Control-s>", lambda e: self.save_session())
        self.root.bind("<Control-o>", lambda e: self.load_session())

    # ==================================================================
    #                     ВКЛАДКА "ПО ГРУППАМ"
    # ==================================================================
    def _clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._tree_map = {}

    def update_group_display(self):
        self._clear_tree()
        self.preview_text.delete(1.0, tk.END)
        self.preview_label.configure(text="Выберите файл", image="")
        self._preview_photo = None

        if not self.duplicate_keys or self.current_group >= len(self.duplicate_keys):
            self.group_label.config(text="Группа 0/0")
            self.group_info_var.set("")
            self.summary_var.set("")
            return

        key = self.duplicate_keys[self.current_group]
        files = self.duplicates[key]
        self.group_label.config(
            text=f"Группа {self.current_group + 1}/{len(self.duplicate_keys)}")

        total_groups = len(self.duplicate_keys)
        total_files = sum(len(v) for v in self.duplicates.values())
        wasted = self.stats.get("wasted_size", 0)
        self.summary_var.set(
            f"Групп: {total_groups:,}  •  Файлов: {total_files:,}  •  "
            f"Свободно: {self.format_size(wasted)}")

        try:
            size = os.path.getsize(files[0])
            group_wasted = size * (len(files) - 1)
            info = (f"Хеш: {key[:28]}…   |   Копий: {len(files)}   |   "
                    f"Размер копии: {self.format_size(size)}   |   "
                    f"Лишнее: {self.format_size(group_wasted)}")
        except OSError:
            info = f"Хеш: {key[:28]}…   |   Копий: {len(files)}"
        self.group_info_var.set(info)

        self._populate_tree(files)

    def _populate_tree(self, files):
        query = self.filter_var.get().strip().lower()
        for i, fp in enumerate(files):
            if query and query not in fp.lower():
                continue
            try:
                size = self.format_size(os.path.getsize(fp))
            except OSError:
                size = "N/A"
            try:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                mtime = "N/A"
            tag = ("first",) if i == 0 else ()
            iid = f"i{i}"
            self._tree_map[iid] = fp
            self.tree.insert("", "end", iid=iid,
                             values=(i + 1, os.path.basename(fp), size, mtime, fp),
                             tags=tag)

    def _apply_filter(self):
        if not self.duplicate_keys or self.current_group >= len(self.duplicate_keys):
            return
        self._clear_tree()
        key = self.duplicate_keys[self.current_group]
        self._populate_tree(self.duplicates[key])

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _select_all_but_first(self):
        ch = self.tree.get_children()
        self.tree.selection_set(ch[1:] if len(ch) > 1 else ())

    def _selected_paths(self):
        return [self._tree_map[i] for i in self.tree.selection()
                if i in self._tree_map]

    def _auto_select(self):
        if not self.duplicate_keys or self.current_group >= len(self.duplicate_keys):
            return
        key = self.duplicate_keys[self.current_group]
        files = self.duplicates[key]
        strat = self.strategy_var.get()
        self.settings["auto_select_strategy"] = strat
        save_settings(self.settings)

        def sort_key(fp):
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                mt = 0
            if strat == "oldest":
                return (mt, len(fp))
            if strat == "newest":
                return (-mt, len(fp))
            if strat == "shortest_path":
                return (len(fp), mt)
            if strat == "longest_path":
                return (-len(fp), mt)
            return (mt, len(fp))

        ordered = sorted(files, key=sort_key)
        reference = ordered[0]
        idx_ref = files.index(reference)
        new_files = [reference] + [f for i, f in enumerate(files) if i != idx_ref]
        self.duplicates[key] = new_files
        self._apply_filter()
        ch = self.tree.get_children()
        self.tree.selection_set(ch[1:] if len(ch) > 1 else ())
        self.log_message(f"Эталон: {os.path.basename(reference)}", "info")

    def _on_tree_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid in self._tree_map:
            self.open_file(self._tree_map[iid])

    def _ctx_open(self):
        s = self._selected_paths()
        if s:
            self.open_file(s[0])

    def _ctx_open_folder(self):
        s = self._selected_paths()
        if s:
            self.open_folder(s[0])

    def _ctx_copy(self):
        s = self._selected_paths()
        if s:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(s))
            self.log_message(f"Скопировано: {len(s)}", "info")

    def _ctx_set_reference(self):
        s = self._selected_paths()
        if not s:
            return
        ref = s[0]
        key = self.duplicate_keys[self.current_group]
        files = self.duplicates[key]
        if ref not in files:
            return
        files.remove(ref)
        files.insert(0, ref)
        self._apply_filter()
        self.log_message(f"Эталон: {os.path.basename(ref)}", "success")

    def _update_preview(self):
        sel = self.tree.selection()
        if not sel:
            return
        fp = self._tree_map.get(sel[0])
        if not fp or fp == self._preview_path:
            return
        self._preview_path = fp
        self._render_preview(fp)

    def _render_preview(self, fp):
        self.preview_text.delete(1.0, tk.END)
        self.preview_label.configure(image="", text="")
        self._preview_photo = None

        ext = os.path.splitext(fp)[1].lower()
        is_img = ext in IMG_EXTS

        if is_img and HAS_PIL:
            try:
                img = Image.open(fp)
                img.thumbnail((340, 340))
                self._preview_photo = ImageTk.PhotoImage(img)
                self.preview_label.configure(image=self._preview_photo)
                self.preview_label.image = self._preview_photo
            except Exception as e:
                self.preview_label.configure(text=f"⚠️ Не удалось: {e}")
        elif is_img:
            self.preview_label.configure(
                text="(установите Pillow: pip install Pillow)")

        if ext in (".txt", ".log", ".md", ".csv", ".json", ".py", ".ini",
                   ".cfg", ".html", ".xml"):
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(4096)
                self.preview_text.insert(tk.END, head)
                if len(head) == 4096:
                    self.preview_text.insert(tk.END, "\n\n[...обрезано...]")
            except Exception as e:
                self.preview_text.insert(tk.END, f"⚠️ {e}")
            return

        lines = [f"📄 {os.path.basename(fp)}", f"📍 {fp}", ""]
        try:
            st = os.stat(fp)
            lines.append(f"📏 Размер:  {self.format_size(st.st_size)} ({st.st_size:,} байт)")
            lines.append(f"📅 Изменён: {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}")
            lines.append(f"📅 Создан:  {datetime.fromtimestamp(st.st_ctime):%Y-%m-%d %H:%M:%S}")
            lines.append(f"🔒 Режим:   {oct(st.st_mode)[-4:]}")
        except OSError as e:
            lines.append(f"⚠️ {e}")

        self.preview_text.insert(tk.END, "\n".join(lines))

    # ==================================================================
    #                    ВКЛАДКА "ВСЕ ДУБЛИКАТЫ"
    # ==================================================================
    def _file_icon(self, fp):
        ext = os.path.splitext(fp)[1].lower()
        if ext in IMG_EXTS:
            return "🖼️"
        if ext in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma"}:
            return "🎵"
        if ext in {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".flv"}:
            return "🎬"
        if ext == ".pdf":
            return "📕"
        if ext in {".epub", ".fb2", ".mobi", ".djvu", ".chm", ".azw3"}:
            return "📚"
        if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}:
            return "🗜️"
        if ext in {".doc", ".docx", ".odt", ".rtf"}:
            return "📝"
        if ext in {".xls", ".xlsx", ".csv", ".ods"}:
            return "📊"
        if ext in {".ppt", ".pptx", ".odp"}:
            return "📽️"
        if ext in {".txt", ".log", ".md", ".ini", ".cfg", ".conf"}:
            return "📄"
        if ext in {".py", ".js", ".html", ".css", ".java", ".cpp", ".c",
                   ".cs", ".go", ".rs", ".rb", ".php", ".sh"}:
            return "💻"
        if ext in {".exe", ".msi", ".dll", ".so", ".dylib"}:
            return "⚙️"
        return "📄"

    def _refresh_all_tree(self):
        if not hasattr(self, "all_tree"):
            return

        for iid in self.all_tree.get_children(""):
            self.all_tree.delete(iid)
        self._thumb_cache.clear()

        if not self.duplicates:
            self.all_summary_var.set("Пока нет данных — выполните поиск")
            self._update_global_summary()
            return

        query = self.all_filter_var.get().strip().lower()
        total_groups_visible = 0

        for gi, (h, files) in enumerate(self.duplicates.items(), 1):
            visible = [f for f in files if query in f.lower()] if query else list(files)
            if not visible:
                continue

            total_groups_visible += 1

            try:
                one_size = os.path.getsize(visible[0])
                group_wasted = one_size * (len(visible) - 1)
                header = (f"📦 Группа #{gi}   •   {len(visible)} копий   •   "
                          f"копия {self.format_size(one_size)}   •   "
                          f"лишнее {self.format_size(group_wasted)}")
            except OSError:
                header = f"📦 Группа #{gi}   •   {len(visible)} копий"

            g_iid = f"g::{gi}"
            self.all_tree.insert(
                "", "end", iid=g_iid,
                text=header,
                values=("", "", "", ""),
                open=True,
                tags=("group",),
            )

            for fi, fp in enumerate(visible, 1):
                try:
                    size = self.format_size(os.path.getsize(fp))
                except OSError:
                    size = "N/A"
                try:
                    mt = datetime.fromtimestamp(
                        os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    mt = "N/A"

                checked = fp in self._all_checked
                check = "☑" if checked else "☐"
                icon = self._file_icon(fp)
                first_tag = ("first",) if fi == 1 else ()

                self.all_tree.insert(
                    g_iid, "end", iid=fp,
                    text=f"{icon}  {fi}. {os.path.basename(fp)}",
                    values=(check, size, mt, fp),
                    tags=first_tag,
                )

            self._update_group_check_mark(g_iid)

        self.all_summary_var.set(
            f"Групп: {total_groups_visible:,}  •  "
            f"Файлов: {sum(len(v) for v in self.duplicates.values()):,}"
        )
        self._update_global_summary()

        if HAS_PIL:
            self.root.after(80, self._kickoff_thumbnails)

    def _update_group_check_mark(self, g_iid):
        children = self.all_tree.get_children(g_iid)
        if not children:
            return
        n = sum(1 for c in children if c in self._all_checked)
        if n == 0:
            mark = "☐"
        elif n == len(children):
            mark = "☑"
        else:
            mark = "◪"
        self.all_tree.set(g_iid, "check", mark)

    def _toggle_file_check(self, fp):
        if fp in self._all_checked:
            self._all_checked.discard(fp)
            self.all_tree.set(fp, "check", "☐")
        else:
            self._all_checked.add(fp)
            self.all_tree.set(fp, "check", "☑")
        parent = self.all_tree.parent(fp)
        if parent:
            self._update_group_check_mark(parent)
        self._update_global_summary()

    def _toggle_group_check(self, g_iid):
        children = self.all_tree.get_children(g_iid)
        all_on = all(c in self._all_checked for c in children) if children else False
        if all_on:
            for c in children:
                self._all_checked.discard(c)
                self.all_tree.set(c, "check", "☐")
        else:
            for c in children:
                self._all_checked.add(c)
                self.all_tree.set(c, "check", "☑")
        self._update_group_check_mark(g_iid)
        self._update_global_summary()

    def _on_all_tree_click(self, event):
        region = self.all_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.all_tree.identify_column(event.x)
        if col != "#1":
            return
        iid = self.all_tree.identify_row(event.y)
        if not iid:
            return
        if iid.startswith("g::"):
            self._toggle_group_check(iid)
        else:
            self._toggle_file_check(iid)
        return "break"

    def _on_all_tree_space(self, event):
        for iid in self.all_tree.selection():
            if iid.startswith("g::"):
                self._toggle_group_check(iid)
            else:
                self._toggle_file_check(iid)
        return "break"

    def _on_all_tree_dblclick(self, event):
        iid = self.all_tree.identify_row(event.y)
        if iid and not iid.startswith("g::"):
            self.open_file(iid)

    def _on_all_tree_right_click(self, event):
        iid = self.all_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.all_tree.selection():
            self.all_tree.selection_set(iid)

        menu = tk.Menu(self.root, tearoff=0)
        if not iid.startswith("g::"):
            menu.add_command(label="📄 Открыть файл",
                             command=lambda: self.open_file(iid))
            menu.add_command(label="📁 Открыть папку",
                             command=lambda: self.open_folder(iid))
            menu.add_command(label="📋 Копировать путь",
                             command=lambda: (self.root.clipboard_clear(),
                                              self.root.clipboard_append(iid)))
            menu.add_separator()
            menu.add_command(label="⭐ Сделать эталоном в группе",
                             command=lambda: self._promote_to_reference(iid))
            menu.add_separator()
            menu.add_command(label="☑ Отметить",
                             command=lambda: self._check_paths(
                                 self._selected_file_paths()))
            menu.add_command(label="⬜ Снять отметку",
                             command=lambda: self._uncheck_paths(
                                 self._selected_file_paths()))
            menu.add_separator()
            menu.add_command(label="🗑️ Удалить отмеченные",
                             command=self.delete_checked_global)
        else:
            menu.add_command(label="☑ Отметить всю группу",
                             command=lambda: self._toggle_group_check(iid))
            menu.add_command(label="💾 Экспорт группы",
                             command=lambda: self._export_specific_group(iid))

        menu.tk_popup(event.x_root, event.y_root)

    def _selected_file_paths(self):
        return [iid for iid in self.all_tree.selection()
                if not iid.startswith("g::")]

    def _check_paths(self, paths):
        for fp in paths:
            if self.all_tree.exists(fp):
                self._all_checked.add(fp)
                self.all_tree.set(fp, "check", "☑")
                parent = self.all_tree.parent(fp)
                if parent:
                    self._update_group_check_mark(parent)
        self._update_global_summary()

    def _uncheck_paths(self, paths):
        for fp in paths:
            self._all_checked.discard(fp)
            if self.all_tree.exists(fp):
                self.all_tree.set(fp, "check", "☐")
                parent = self.all_tree.parent(fp)
                if parent:
                    self._update_group_check_mark(parent)
        self._update_global_summary()

    def _promote_to_reference(self, fp):
        for h, files in self.duplicates.items():
            if fp in files:
                files.remove(fp)
                files.insert(0, fp)
                self._refresh_all_tree()
                self.update_group_display()
                self.log_message(f"Эталон: {os.path.basename(fp)}", "success")
                return

    def _check_all_but_first_global(self):
        n = 0
        for files in self.duplicates.values():
            for fp in files[1:]:
                self._all_checked.add(fp)
                n += 1
        for g in self.all_tree.get_children(""):
            for c in self.all_tree.get_children(g):
                if c in self._all_checked:
                    self.all_tree.set(c, "check", "☑")
                else:
                    self.all_tree.set(c, "check", "☐")
            self._update_group_check_mark(g)
        self._update_global_summary()
        self.log_message(f"Отмечено {n} дубликатов", "info")

    def _uncheck_all_global(self):
        self._all_checked.clear()
        for g in self.all_tree.get_children(""):
            for c in self.all_tree.get_children(g):
                self.all_tree.set(c, "check", "☐")
            self.all_tree.set(g, "check", "☐")
        self._update_global_summary()

    def _expand_all(self, expand=True):
        for g in self.all_tree.get_children(""):
            self.all_tree.item(g, open=expand)

    def _update_global_summary(self):
        files = list(self._all_checked)
        total = 0
        for fp in files:
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
        self.all_selected_var.set(
            f"Выбрано: {len(files):,} файлов  ({self.format_size(total)})"
        )

    # -------- миниатюры --------
    def _kickoff_thumbnails(self):
        if not HAS_PIL:
            return
        images = []
        for g in self.all_tree.get_children(""):
            for fp in self.all_tree.get_children(g):
                if os.path.splitext(fp)[1].lower() in IMG_EXTS:
                    images.append(fp)
        if not images:
            return
        threading.Thread(target=self._thumbnail_worker,
                         args=(images,), daemon=True).start()

    def _thumbnail_worker(self, filepaths):
        batch = []
        for fp in filepaths:
            try:
                img = Image.open(fp)
                img.thumbnail((28, 28))
                img = img.convert("RGBA")
                batch.append((fp, img))
            except Exception:
                pass
        if batch:
            self.root.after(0, lambda: self._apply_thumbnails_batch(batch))

    def _apply_thumbnails_batch(self, batch, idx=0):
        chunk = 30
        for i in range(idx, min(idx + chunk, len(batch))):
            fp, pil_img = batch[i]
            if not self.all_tree.exists(fp):
                continue
            try:
                photo = ImageTk.PhotoImage(pil_img)
                self._thumb_cache[fp] = photo
                self.all_tree.item(fp, image=photo)
            except Exception:
                pass
        if idx + chunk < len(batch):
            self.root.after(1, lambda: self._apply_thumbnails_batch(batch, idx + chunk))

    # -------- массовые операции на вкладке "Все" --------
    def delete_checked_global(self):
        to_delete = list(self._all_checked)
        if not to_delete:
            messagebox.showwarning("Нет выбранных",
                                   "Отметьте файлы галочками (клик по ☑).")
            return

        protected = [f for f in to_delete if check_protected(f)]
        if protected:
            m = (f"⚠️ Среди выбранных {len(protected)} защищённых файлов.\n"
                 "Продолжить?")
            if not messagebox.askyesno("Защищённые", m):
                return

        use_trash = self.trash_var.get()
        action = "в корзину" if use_trash else "БЕЗВОЗВРАТНО"
        if not messagebox.askyesno("Подтверждение",
                                   f"Удалить {len(to_delete):,} файлов ({action})?"):
            return

        deleted, errors = 0, []
        for fp in to_delete:
            if use_trash:
                ok, err = move_to_trash(fp, use_send2trash=HAS_SEND2TRASH)
            else:
                try:
                    os.remove(fp)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
            if ok:
                deleted += 1
                if self.cache:
                    self.cache.delete(fp)
                for h in list(self.duplicates.keys()):
                    if fp in self.duplicates[h]:
                        self.duplicates[h].remove(fp)
                        if len(self.duplicates[h]) <= 1:
                            del self.duplicates[h]
                        break
            else:
                errors.append(f"{os.path.basename(fp)}: {err}")

        if self.cache:
            self.cache.flush()

        self.duplicate_keys = list(self.duplicates.keys())
        self.stats["duplicate_groups"] = len(self.duplicates)
        self.stats["duplicate_files"] = sum(len(v) for v in self.duplicates.values())
        self._all_checked.clear()

        self._refresh_all_tree()
        self.update_group_display()

        m = f"✅ Удалено {deleted:,} файлов."
        if errors:
            m += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
        messagebox.showinfo("Результат", m)
        self.log_message(f"Удалено {deleted} файлов (общий вид)", "success")

    def hardlink_checked_global(self):
        to_link = list(self._all_checked)
        if not to_link:
            messagebox.showwarning("Нет выбранных", "Отметьте файлы.")
            return

        if not messagebox.askyesno(
            "Хардлинки",
            f"🔗 Заменить {len(to_link):,} файлов хардлинками на эталон группы?\n"
            "Место освободится, файлы останутся доступны.\nПродолжить?"
        ):
            return

        replaced, errors = 0, []
        for fp in to_link:
            ref = None
            for h, files in self.duplicates.items():
                if fp in files and files[0] != fp:
                    ref = files[0]
                    break
            if ref is None:
                errors.append(f"{os.path.basename(fp)}: эталон не найден")
                continue
            if check_protected(fp):
                errors.append(f"{os.path.basename(fp)}: защищён")
                continue
            ok, err = create_hardlink(ref, fp)
            if ok:
                replaced += 1
                if self.cache:
                    self.cache.delete(fp)
            else:
                errors.append(f"{os.path.basename(fp)}: {err}")

        if self.cache:
            self.cache.flush()

        self._all_checked.clear()
        self._refresh_all_tree()
        self.update_group_display()

        m = f"✅ Заменено {replaced:,} файлов."
        if errors:
            m += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
        messagebox.showinfo("Результат", m)

    # <<< sessions.py <<<
    # def export_checked_global(self):
    #     to_export = list(self._all_checked)
    #     if not to_export:
    #         messagebox.showwarning("Нет выбранных", "Отметьте файлы.")
    #         return
    #     fn = filedialog.asksaveasfilename(
    #         defaultextension=".txt",
    #         filetypes=[("Текст", "*.txt"), ("JSON", "*.json"), ("CSV", "*.csv")],
    #         title="Экспорт отмеченных",
    #         initialfile=f"checked_{datetime.now():%Y%m%d_%H%M%S}")
    #     if not fn:
    #         return

    #     if fn.endswith(".json"):
    #         with open(fn, "w", encoding="utf-8") as f:
    #             json.dump({"files": to_export,
    #                        "count": len(to_export),
    #                        "exported_at": datetime.now().isoformat()},
    #                       f, ensure_ascii=False, indent=2)
    #     elif fn.endswith(".csv"):
    #         import csv
    #         with open(fn, "w", newline="", encoding="utf-8") as f:
    #             w = csv.writer(f, delimiter=";")
    #             w.writerow(["Путь", "Размер", "Изменён"])
    #             for fp in to_export:
    #                 try:
    #                     size = os.path.getsize(fp)
    #                     mt = datetime.fromtimestamp(
    #                         os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    #                 except OSError:
    #                     size, mt = "N/A", "N/A"
    #                 w.writerow([fp, size, mt])
    #     else:
    #         with open(fn, "w", encoding="utf-8") as f:
    #             for fp in to_export:
    #                 f.write(fp + "\n")
    #     self.log_message(f"Экспорт {len(to_export)} файлов → {fn}", "success")
    #     messagebox.showinfo("Готово", f"Сохранено:\n{fn}")


    # скорректированная функция 
    def export_checked_global(self):
        to_export = list(self._all_checked)
        if not to_export:
            messagebox.showwarning("Нет выбранных", "Отметьте файлы.")
            return
        fn = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Текст", "*.txt"), ("JSON", "*.json"), ("CSV", "*.csv")],
            title="Экспорт отмеченных",
            initialfile=f"checked_{datetime.now():%Y%m%d_%H%M%S}")
        if not fn:
            return
        try:
            write_checked_list(to_export, fn)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")
            return
        self.log_message(f"Экспорт {len(to_export)} файлов -> {fn}", "success")
        messagebox.showinfo("Готово", f"Сохранено:\n{fn}")


    def _export_specific_group(self, g_iid):
        try:
            gi = int(g_iid.split("::")[1]) - 1
        except (ValueError, IndexError):
            return
        if 0 <= gi < len(self.duplicate_keys):
            self.current_group = gi
            self.export_current_group()

    # ==================================================================
    # ОБЩИЕ ДЕЙСТВИЯ
    # ==================================================================
    def open_file(self, filepath):
        try:
            if os.name == "nt":
                os.startfile(filepath)
            elif os.name == "posix":
                subprocess.run(["xdg-open", filepath])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл:\n{e}")

    def open_folder(self, filepath):
        try:
            folder = os.path.dirname(filepath)
            if os.name == "nt":
                os.startfile(folder)
            elif os.name == "posix":
                subprocess.run(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}")

    def _finalize_removed(self):
        if self.duplicate_keys and self.current_group < len(self.duplicate_keys):
            cur_key = self.duplicate_keys[self.current_group]
            if cur_key in self.duplicates and len(self.duplicates[cur_key]) <= 1:
                del self.duplicates[cur_key]
                self.duplicate_keys = list(self.duplicates.keys())
                if self.current_group >= len(self.duplicate_keys):
                    self.current_group = max(0, len(self.duplicate_keys) - 1)

        self.stats["duplicate_groups"] = len(self.duplicates)
        self.stats["duplicate_files"] = sum(len(f) for f in self.duplicates.values())
        self.update_group_display()
        self._refresh_all_tree()

    def delete_selected_files(self):
        selected = self._selected_paths()
        if not selected:
            messagebox.showwarning("Нет выбранных", "Выделите файлы в таблице.")
            return

        protected = [f for f in selected if check_protected(f)]
        if protected:
            msg = ("⚠️ Среди выбранных есть защищённые пути/типы:\n\n"
                   + "\n".join(protected[:5]))
            if len(protected) > 5:
                msg += f"\n...и ещё {len(protected) - 5}"
            msg += "\n\nВсё равно продолжить?"
            if not messagebox.askyesno("Защищённые файлы", msg):
                return

        use_trash = self.trash_var.get()
        action = "в корзину" if use_trash else "БЕЗВОЗВРАТНО"
        msg = f"Удалить {len(selected):,} файлов ({action})?\n\n"
        msg += "\n".join(os.path.basename(f) for f in selected[:3])
        if len(selected) > 3:
            msg += f"\n... и ещё {len(selected) - 3}"
        if not messagebox.askyesno("Подтверждение", msg):
            return

        deleted = 0
        errors = []
        for fp in selected:
            if use_trash:
                ok, err = move_to_trash(fp, use_send2trash=HAS_SEND2TRASH)
            else:
                try:
                    os.remove(fp)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
            if ok:
                deleted += 1
                if self.cache:
                    self.cache.delete(fp)
                cur_key = self.duplicate_keys[self.current_group] if self.duplicate_keys else None
                if cur_key and cur_key in self.duplicates and fp in self.duplicates[cur_key]:
                    self.duplicates[cur_key].remove(fp)
            else:
                errors.append(f"{os.path.basename(fp)}: {err}")

        if self.cache:
            self.cache.flush()

        self._finalize_removed()

        m = f"✅ Удалено {deleted:,} файлов."
        if errors:
            m += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
        messagebox.showinfo("Результат", m)
        self.log_message(f"Удалено {deleted} файлов", "success")

    def delete_all_duplicates(self):
        if not self.duplicates:
            messagebox.showwarning("Нет дубликатов", "Ничего не найдено!")
            return

        to_delete = []
        for files in self.duplicates.values():
            to_delete.extend(files[1:])

        if not to_delete:
            messagebox.showinfo("Нет дубликатов", "В каждой группе по одному файлу.")
            return

        protected = [f for f in to_delete if check_protected(f)]
        if protected and not messagebox.askyesno(
            "Защищённые файлы",
            f"⚠️ Среди {len(protected)} файлов есть защищённые.\nПродолжить?"
        ):
            return

        use_trash = self.trash_var.get()
        action = "в корзину" if use_trash else "БЕЗВОЗВРАТНО"
        if not messagebox.askyesno(
            "Удаление всех дубликатов",
            f"Будет удалено {len(to_delete):,} файлов ({action}).\n"
            f"Освободится ~{self.format_size(self.stats.get('wasted_size', 0))}.\nПродолжить?"
        ):
            return

        deleted, errors = 0, []
        for fp in to_delete:
            if use_trash:
                ok, err = move_to_trash(fp, use_send2trash=HAS_SEND2TRASH)
            else:
                try:
                    os.remove(fp)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
            if ok:
                deleted += 1
                if self.cache:
                    self.cache.delete(fp)
                if deleted % 50 == 0:
                    self.log_message(f"… {deleted}/{len(to_delete)}", "info")
            else:
                errors.append(f"{os.path.basename(fp)}: {err}")

        if self.cache:
            self.cache.flush()

        new_dupes = {k: [v[0]] for k, v in self.duplicates.items() if v}
        self.duplicates = new_dupes
        self.duplicate_keys = list(new_dupes.keys())
        self.current_group = 0
        self.stats["duplicate_groups"] = len(new_dupes)
        self.stats["duplicate_files"] = len(new_dupes)
        self.update_group_display()
        self._refresh_all_tree()

        m = f"✅ Удалено {deleted:,} файлов.\n🗑️ Освобождено: {self.format_size(self.stats.get('wasted_size', 0))}"
        if errors:
            m += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
        messagebox.showinfo("Результат", m)

    def replace_with_hardlinks(self):
        if not self.duplicates:
            messagebox.showwarning("Нет данных", "Сначала выполните поиск!")
            return

        total = sum(len(fs) - 1 for fs in self.duplicates.values() if len(fs) > 1)
        if total == 0:
            messagebox.showinfo("Нет дубликатов", "Нечего заменять.")
            return

        if not messagebox.askyesno(
            "Замена хардлинками",
            f"🔗 Заменить {total:,} дубликатов жёсткими ссылками на эталон?\n\n"
            "Место освободится, но файлы останутся доступны по всем путям.\n"
            "Работает в пределах одного тома.\n\nПродолжить?"
        ):
            return

        replaced, errors = 0, []
        for key, files in list(self.duplicates.items()):
            if len(files) < 2:
                continue
            reference = files[0]
            for fp in files[1:]:
                if check_protected(fp):
                    errors.append(f"{os.path.basename(fp)}: защищён")
                    continue
                ok, err = create_hardlink(reference, fp)
                if ok:
                    replaced += 1
                    if self.cache:
                        self.cache.delete(fp)
                    if replaced % 25 == 0:
                        self.log_message(f"… {replaced}/{total}", "info")
                else:
                    errors.append(f"{os.path.basename(fp)}: {err}")

        if self.cache:
            self.cache.flush()

        m = f"✅ Заменено {replaced:,} файлов хардлинками."
        if errors:
            m += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
        messagebox.showinfo("Результат", m)
        self.log_message(f"Хардлинки: {replaced}", "success")

    # ==================================================================
    # НАВИГАЦИЯ ПО ГРУППАМ
    # ==================================================================
    def go_to_group(self, idx):
        if 0 <= idx < len(self.duplicate_keys):
            self.current_group = idx
            self.update_group_display()

    def go_to_group_entry(self):
        try:
            n = int(self.group_entry.get())
            if 1 <= n <= len(self.duplicate_keys):
                self.go_to_group(n - 1)
            else:
                messagebox.showwarning("Ошибка", f"От 1 до {len(self.duplicate_keys)}")
        except ValueError:
            messagebox.showwarning("Ошибка", "Введите число")

    def prev_group(self):
        if self.current_group > 0:
            self.current_group -= 1
            self.update_group_display()

    def next_group(self):
        if self.current_group < len(self.duplicate_keys) - 1:
            self.current_group += 1
            self.update_group_display()

    def go_to_last_group(self):
        if self.duplicate_keys:
            self.current_group = len(self.duplicate_keys) - 1
            self.update_group_display()

    # ==================================================================
    # КЭШ
    # ==================================================================
    def clear_cache(self):
        if not self.cache:
            return
        n = self.cache.count()
        if messagebox.askyesno("Очистка кэша", f"Удалить {n:,} записей?"):
            self.cache.clear()
            self.log_message("Кэш очищен", "success")
            messagebox.showinfo("Успех", "Кэш очищен")

    def show_cache_stats(self):
        if not self.cache:
            return
        n = self.cache.count()
        kb = self.cache.size_bytes() / 1024

        win = tk.Toplevel(self.root)
        win.title("📊 Статистика кэша")
        win.geometry("520x420")
        win.configure(bg=self.theme["bg"])

        ttk.Label(win, text="📊 Статистика SQLite-кэша",
                  font=("Arial", 14, "bold"),
                  foreground=self.theme["accent"]).pack(pady=(15, 10))

        text = (f"Всего записей: {n:,}\n"
                f"Размер БД: {kb:.1f} KB\n"
                f"Файл: {self.cache.db_path}\n")
        total = self._cache_hits + self._cache_misses
        if total:
            eff = self._cache_hits / total * 100
            text += (f"\nТЕКУЩАЯ СЕССИЯ:\n"
                     f"  Попаданий: {self._cache_hits:,}\n"
                     f"  Промахов:  {self._cache_misses:,}\n"
                     f"  Эффективность: {eff:.1f}%\n")

        tw = scrolledtext.ScrolledText(win, height=15, width=60,
                                       font=("Consolas", 10), wrap=tk.WORD)
        tw.pack(padx=15, pady=10)
        tw.insert(tk.END, text)
        tw.config(state=tk.DISABLED)
        ttk.Button(win, text="Закрыть", command=win.destroy,
                   width=15).pack(pady=(0, 15))

    # ==================================================================
    # ХЕШИРОВАНИЕ
    # ==================================================================
    
    # >> hashing.py >>>>>>>>>>
    # def calculate_hash(self, filepath, algorithm="md5"):
    #     h = hashlib.new(algorithm)
    #     try:
    #         with open(filepath, "rb") as f:
    #             for chunk in iter(lambda: f.read(BLOCK_SIZE), b""):
    #                 if self.stop_search:
    #                     break
    #                 h.update(chunk)
    #     except Exception as e:
    #         raise Exception(f"Ошибка чтения {filepath}: {e}")
    #     return h.hexdigest()
    
    # << hashing.py <<< 
    def calculate_hash(self, filepath, algorithm="md5"):
        return _hash_full(filepath, algorithm)

    # >>> hashing.py >>>
    # def calculate_quick_hash(self, filepath, algorithm="md5"):
    #     h = hashlib.new(algorithm)
    #     try:
    #         size = os.path.getsize(filepath)
    #         with open(filepath, "rb") as f:
    #             if size <= BLOCK_SIZE * 2:
    #                 h.update(f.read())
    #             else:
    #                 h.update(f.read(BLOCK_SIZE))
    #                 f.seek(-BLOCK_SIZE, os.SEEK_END)
    #                 h.update(f.read(BLOCK_SIZE))
    #     except Exception:
    #         return None
    #     return h.hexdigest()

    # <<< hashing.py <<<
    def calculate_quick_hash(self, filepath, algorithm="md5"):
        return _hash_quick(filepath, algorithm)


    def get_file_hash_cached(self, filepath, algorithm):
        if self.stop_search:
            return filepath, None
        try:
            size = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)
        except OSError:
            return filepath, None

        if self.use_cache_var.get() and self.cache:
            cached = self.cache.get(filepath, algorithm, size, mtime)
            if cached is not None:
                with self._cache_lock:
                    self._cache_hits += 1
                return filepath, cached

        with self._cache_lock:
            self._cache_misses += 1

        try:
            h = self.calculate_hash(filepath, algorithm)
        except Exception:
            return filepath, None

        if self.use_cache_var.get() and self.cache:
            self.cache.set(filepath, algorithm, h, size, mtime)
        return filepath, h

    # ==================================================================
    # ЗАПУСК
    # ==================================================================
    
    # # Внизу добавлена нормальизация адреса --> проверяется на работоспособность
    # def select_folder(self):
    #     folder = filedialog.askdirectory(title="Выберите папку")
    #     if folder:
    #         self.folder_path.set(folder)
    #         self._remember_path(folder)

    # ЭКСПЕРЕМЕНТАЛЬНЫЙ ВЫВОД ПАПОК 
    def select_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку")
        if folder:
            folder = normalize_path(folder)
            self.folder_path.set(folder)
            self._remember_path(folder)

    def _remember_path(self, path):
        recents = self.settings.get("recent_paths", [])
        if path in recents:
            recents.remove(path)
        recents.insert(0, path)
        recents = recents[:10]
        self.settings["recent_paths"] = recents
        save_settings(self.settings)
        if hasattr(self, "folder_combo"):
            self.folder_combo.configure(values=recents)

    def select_drive(self):
        if os.name == "nt":
            import string
            drives = [f"{d}:\\" for d in string.ascii_uppercase
                      if os.path.exists(f"{d}:\\")]
        else:
            drives = ["/", "/home", "/Users"]

        win = tk.Toplevel(self.root)
        win.title("Выберите диск")
        win.geometry("350x300")
        win.configure(bg=self.theme["bg"])
        ttk.Label(win, text="Выберите диск:", font=("Arial", 11, "bold"),
                  foreground=self.theme["accent"]).pack(pady=15)
        for d in drives:
            ttk.Button(win, text=f"📀 {d}", width=25,
                       command=lambda dd=d: (self.folder_path.set(normalize_path(dd)),
                                             self._remember_path(dd),
                                             win.destroy())).pack(pady=4)
            # # Проверяется normalize_path(dd) ------
            # ttk.Button(win, text=f"📀 {d}", width=25,
            #            command=lambda dd=d: (self.folder_path.set(dd),
            #                                  self._remember_path(dd),
            #                                  win.destroy())).pack(pady=4)
            

    def start_search(self):
        if self.search_in_progress:
            return
        if not self.folder_path.get():
            messagebox.showerror("Ошибка", "Выберите папку!")
            return
        if not os.path.exists(self.folder_path.get()):
            messagebox.showerror("Ошибка", "Папка не существует!")
            return

        # Нормализация перед использованием пути
        normalized = normalize_path(self.folder_path.get())
        self.folder_path.set(normalized)
        self._remember_path(normalized)

        # Эксперемент с нормализацией -- normalize_path
        # self._remember_path(self.folder_path.get())

        self.settings.update({
            "last_algorithm": self.hash_algo.get(),
            "last_threads": self.thread_count.get(),
            "last_recursive": self.recursive_var.get(),
            "last_use_cache": self.use_cache_var.get(),
            "last_extensions": self.extensions_var.get(),
            "delete_to_trash": self.trash_var.get(),
            "ignore_folders": [s.strip() for s in self.ignore_var.get().split(",") if s.strip()],
        })
        save_settings(self.settings)

        self.stop_search = False
        self.duplicates = {}
        self.duplicate_keys = []
        self.current_group = 0
        self._all_checked.clear()
        self._clear_tree()
        for iid in self.all_tree.get_children(""):
            self.all_tree.delete(iid)
        self.text_output.delete(1.0, tk.END)
        self._cache_hits = 0
        self._cache_misses = 0
        self._progress_start_ts = time.time()
        self.stats = {
            "total_files": 0, "duplicate_groups": 0, "duplicate_files": 0,
            "start_time": datetime.now(), "end_time": None,
            "total_size": 0, "wasted_size": 0,
        }

        self.search_in_progress = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_export.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self.progress_label.config(text="Подготовка...")
        self.progress_detail_var.set("")
        self.status_var.set("🔍 Начинаю поиск...")
        self.notebook.select(self.tab_search)

        threading.Thread(target=self.find_duplicates_thread, daemon=True).start()

    def stop_search_func(self):
        self.stop_search = True
        self.btn_stop.config(state=tk.DISABLED)
        self.status_var.set("🛑 Останавливаю...")
        self.log_message("Поиск остановлен пользователем", "warning")

    # ==================================================================
    # ПОТОК ПОИСКА
    # ==================================================================
    def find_duplicates_thread(self):
        try:
            folder = self.folder_path.get()
            algo = self.hash_algo.get()
            recursive = self.recursive_var.get()
            thread_count = max(1, self.thread_count.get())

            exts_raw = self.extensions_var.get().split(",")
            extensions = {e.strip().lower() for e in exts_raw if e.strip()} or None

            ignore = {s.strip() for s in self.ignore_var.get().split(",") if s.strip()}

            self.log_message("=" * 70, "info")
            self.log_message("🚀 НАЧАЛО ПОИСКА", "info")
            self.log_message(f"📁 {folder}", "info")
            self.log_message(f"⚡ {algo.upper()} | Потоки: {thread_count}", "info")
            self.log_message(f"💾 Кэш: {'Да' if self.use_cache_var.get() else 'Нет'}", "info")
            self.log_message("=" * 70, "info")
            log.info("Поиск: folder=%s algo=%s threads=%d", folder, algo, thread_count)

            start_time = time.time()
            self._progress_start_ts = start_time

            self.queue.put(("status", "🔍 Поиск файлов..."))
            all_files = []

            def _match(name):
                if extensions is None:
                    return True
                return os.path.splitext(name)[1].lower() in extensions

            if recursive:
                for root, dirs, files in os.walk(folder):
                    if self.stop_search:
                        break
                    dirs[:] = [d for d in dirs if d not in ignore]
                    for f in files:
                        if _match(f):
                            all_files.append(os.path.join(root, f))
            else:
                try:
                    with os.scandir(folder) as it:
                        for entry in it:
                            if self.stop_search:
                                break
                            try:
                                if entry.is_file() and _match(entry.name):
                                    all_files.append(entry.path)
                            except OSError:
                                pass
                except OSError as e:
                    self.log_message(f"Ошибка чтения папки: {e}", "error")

            total_files = len(all_files)
            self.stats["total_files"] = total_files
            self.log_message(f"✅ Файлов: {total_files:,} ({time.time() - start_time:.2f}s)",
                             "success")
            if total_files == 0:
                self.queue.put(("done", None))
                return

            self.queue.put(("status", "📊 Группировка..."))
            size_groups = defaultdict(list)
            for i, fp in enumerate(all_files):
                if self.stop_search:
                    break
                try:
                    size_groups[os.path.getsize(fp)].append(fp)
                except OSError:
                    continue
                if i % 200 == 0:
                    self.queue.put(("progress_scan", (i, total_files)))

            candidates = [f for s, fs in size_groups.items() if len(fs) > 1 for f in fs]
            if not candidates:
                self.log_message("📭 Нет кандидатов", "warning")
                self.queue.put(("stats_result", (0, 0)))
                self.queue.put(("done", None))
                return

            self.log_message(f"🔍 Кандидатов: {len(candidates):,}", "info")

            self.queue.put(("status", "⚡ Быстрый хеш..."))
            quick = defaultdict(list)
            total_c = len(candidates)
            for i, fp in enumerate(candidates):
                if self.stop_search:
                    break
                qh = self.calculate_quick_hash(fp, algo)
                if qh:
                    try:
                        quick[(os.path.getsize(fp), qh)].append(fp)
                    except OSError:
                        pass
                if i % 50 == 0:
                    self.queue.put(("progress_quick", (i, total_c)))

            to_full = [f for fs in quick.values() if len(fs) > 1 for f in fs]
            self.log_message(f"🎯 После быстрого хеша: {len(to_full):,}", "info")
            if not to_full:
                self.log_message("📭 Дубликатов нет", "warning")
                self.queue.put(("stats_result", (0, 0)))
                self.queue.put(("done", None))
                return

            self.queue.put(("status", "🔐 Полный хеш..."))
            full = defaultdict(list)
            processed = 0
            total_hash = len(to_full)

            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                for i in range(0, total_hash, FUTURES_BATCH):
                    if self.stop_search:
                        break
                    batch = to_full[i:i + FUTURES_BATCH]
                    futs = {executor.submit(self.get_file_hash_cached, fp, algo): fp
                            for fp in batch}
                    for fut in as_completed(futs):
                        if self.stop_search:
                            break
                        try:
                            fp, h = fut.result(timeout=60)
                            if h:
                                full[h].append(fp)
                        except Exception as e:
                            fp = futs[fut]
                            self.log_message(
                                f"⚠️ {os.path.basename(fp)}: {str(e)[:80]}", "error")
                        processed += 1
                        self.queue.put(("progress_hash", (processed, total_hash)))
                    if self.cache:
                        self.cache.flush()

            duplicates = {h: fs for h, fs in full.items() if len(fs) > 1}
            self.duplicates = duplicates
            self.duplicate_keys = list(duplicates.keys())

            dup_groups = len(duplicates)
            dup_files = sum(len(fs) for fs in duplicates.values())

            wasted, total_sz = 0, 0
            for fs in duplicates.values():
                try:
                    s = os.path.getsize(fs[0])
                    total_sz += s * len(fs)
                    wasted += s * (len(fs) - 1)
                except OSError:
                    pass
            self.stats["total_size"] = total_sz
            self.stats["wasted_size"] = wasted
            self.stats["duplicate_groups"] = dup_groups
            self.stats["duplicate_files"] = dup_files
            self.stats["end_time"] = datetime.now()

            total_time = time.time() - start_time
            self.log_message("-" * 70, "info")
            self.log_message(f"📁 Групп: {dup_groups:,}", "info")
            self.log_message(f"📄 Файлов: {dup_files:,}", "info")
            self.log_message(f"✅ Кэш: {self._cache_hits:,}/{self._cache_misses:,}", "info")
            self.log_message(f"⏱️ Время: {total_time:.2f} сек", "info")
            self.log_message("=" * 70, "info")
            log.info("Результат: %d групп, %d файлов", dup_groups, dup_files)

            ######################################################
            ##             обновленный код                      ##
            # self.save_results_to_file(duplicates) # УДАЛИТЬ   ##
            try:
                autosave_report(
                    duplicates, self.stats,
                    folder_path=folder,
                    algorithm=algo,
                    thread_count=thread_count,
                    use_cache=self.use_cache_var.get(),
                    cache_hits=self._cache_hits,
                    cache_misses=self._cache_misses,
                )
            except Exception as e:
                self.log_message(f"⚠️ Не удалось сохранить отчёт: {e}", "warning")
            ##                                                  ##
            ######################################################

            if dup_groups > 0:
                self.queue.put(("show_results", None))

        except Exception as e:
            import traceback
            self.log_message(f"❌ ОШИБКА: {e}", "error")
            self.log_message(traceback.format_exc(), "error")
            log.exception("Критическая ошибка в потоке поиска")
        finally:
            if self.cache:
                try:
                    self.cache.flush()
                except Exception:
                    pass
            self.queue.put(("done", None))

    # ==================================================================
    # СЕССИИ
    # ==================================================================
    
    # >>> session.py >>>
    # def save_session(self):
    #     if not self.duplicates:
    #         messagebox.showwarning("Нет данных", "Нечего сохранять.")
    #         return
    #     fn = filedialog.asksaveasfilename(
    #         defaultextension=".dfsess",
    #         filetypes=[("Session", "*.dfsess"), ("JSON", "*.json"), ("Все", "*.*")],
    #         title="Сохранить сессию",
    #         initialfile=f"session_{datetime.now():%Y%m%d_%H%M%S}.dfsess")
    #     if not fn:
    #         return
    #     data = {
    #         "version": DF_VERSION,
    #         "saved_at": datetime.now().isoformat(),
    #         "search_path": self.folder_path.get(),
    #         "algorithm": self.hash_algo.get(),
    #         "stats": {k: (v.isoformat() if isinstance(v, datetime) else v)
    #                   for k, v in self.stats.items()},
    #         "duplicates": self.duplicates,
    #     }
    #     try:
    #         with open(fn, "w", encoding="utf-8") as f:
    #             json.dump(data, f, ensure_ascii=False, indent=2)
    #         self.log_message(f"Сессия сохранена: {fn}", "success")
    #         messagebox.showinfo("Готово", f"Сессия:\n{fn}")
    #     except Exception as e:
    #         messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")

    # Тонкая обёртка из <<< session.py <<<
    def save_session(self):
        if not self.duplicates:
            messagebox.showwarning("Нет данных", "Нечего сохранять.")
            return
        fn = filedialog.asksaveasfilename(
            defaultextension=".dfsess",
            filetypes=[("Session", "*.dfsess"), ("JSON", "*.json"), ("Все", "*.*")],
            title="Сохранить сессию",
            initialfile=f"session_{datetime.now():%Y%m%d_%H%M%S}.dfsess")
        if not fn:
            return
        ok, err = _save_session(
            fn, self.duplicates, self.stats,
            folder_path=self.folder_path.get(),
            algorithm=self.hash_algo.get(),
            version=DF_VERSION,
        )
        if not ok:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{err}")
            return
        self.log_message(f"Сессия сохранена: {fn}", "success")
        messagebox.showinfo("Готово", f"Сессия:\n{fn}")

    # >>> session.py >>>
    # def load_session(self):
    #     fn = filedialog.askopenfilename(
    #         filetypes=[("Session", "*.dfsess"), ("JSON", "*.json"), ("Все", "*.*")],
    #         title="Загрузить сессию")
    #     if not fn:
    #         return
    #     try:
    #         with open(fn, "r", encoding="utf-8") as f:
    #             data = json.load(f)
    #     except Exception as e:
    #         messagebox.showerror("Ошибка", f"Не удалось прочитать:\n{e}")
    #         return

    #     self.duplicates = {k: [f for f in v if os.path.exists(f)]
    #                        for k, v in data.get("duplicates", {}).items()}
    #     self.duplicates = {k: v for k, v in self.duplicates.items() if len(v) > 1}
    #     self.duplicate_keys = list(self.duplicates.keys())
    #     self.current_group = 0
    #     self._all_checked.clear()

    #     st = data.get("stats", {})
    #     for k in ("total_files", "duplicate_groups", "duplicate_files",
    #               "total_size", "wasted_size"):
    #         if k in st:
    #             self.stats[k] = st[k]
    #     self.stats["duplicate_groups"] = len(self.duplicates)
    #     self.stats["duplicate_files"] = sum(len(v) for v in self.duplicates.values())

    #     if data.get("search_path"):
    #         self.folder_path.set(data["search_path"])

    #     self.log_message(f"Сессия загружена: {fn}", "success")
    #     self.update_group_display()
    #     self._refresh_all_tree()
    #     self.notebook.select(self.tab_all)

    # Тонкая обёртка из <<< sessions.py <<<
    def load_session(self):
        fn = filedialog.askopenfilename(
            filetypes=[("Session", "*.dfsess"), ("JSON", "*.json"), ("Все", "*.*")],
            title="Загрузить сессию")
        if not fn:
            return
        data = _load_session(fn)
        if data is None:
            messagebox.showerror("Ошибка", "Не удалось прочитать сессию.")
            return

        self.duplicates = data["duplicates"]
        self.duplicate_keys = list(self.duplicates.keys())
        self.current_group = 0
        self._all_checked.clear()
        self.stats.update(data["stats"])

        if data["search_path"]:
            self.folder_path.set(data["search_path"])

        self.log_message(f"Сессия загружена: {fn}", "success")
        self.update_group_display()
        self._refresh_all_tree()
        self.notebook.select(self.tab_all)

    # ==================================================================
    # HTML-ОТЧЁТ
    # ==================================================================
    
    # # >>> reporter.py >>>
    # def _build_html_report(self):
    #     t = self.theme
    #     parts = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
    #              "<title>Duplicate Finder — отчёт</title><style>"]
    #     parts.append(
    #         f"body{{font-family:Segoe UI,Arial,sans-serif;background:{t['bg']};"
    #         f"color:{t['fg']};margin:24px}}")
    #     parts.append("h1{color:#3498db} h2{color:#2c3e50;border-bottom:1px solid #ccc;"
    #                  "padding-bottom:4px;margin-top:32px}")
    #     parts.append("table{border-collapse:collapse;width:100%;margin:10px 0;background:#fff}")
    #     parts.append("th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:13px}")
    #     parts.append("th{background:#ecf0f1}")
    #     parts.append(".img-preview{max-width:100px;max-height:100px;border:1px solid #ccc}")
    #     parts.append(".muted{color:#888;font-size:12px}")
    #     parts.append(".stat{display:inline-block;background:#3498db;color:#fff;"
    #                  "padding:8px 14px;border-radius:6px;margin-right:10px;font-weight:bold}")
    #     parts.append("</style></head><body>")

    #     parts.append("<h1>🔍 Отчёт Duplicate Finder</h1>")
    #     parts.append(f"<p class='muted'>Дата: {datetime.now():%Y-%m-%d %H:%M:%S} • "
    #                  f"Папка: {self.folder_path.get()} • Алгоритм: {self.hash_algo.get().upper()}</p>")
    #     parts.append(
    #         f"<div>"
    #         f"<span class='stat'>Групп: {len(self.duplicates):,}</span>"
    #         f"<span class='stat'>Файлов: {sum(len(v) for v in self.duplicates.values()):,}</span>"
    #         f"<span class='stat'>Освободить: {self.format_size(self.stats.get('wasted_size', 0))}</span>"
    #         f"</div>")

    #     for i, (h, files) in enumerate(self.duplicates.items(), 1):
    #         parts.append(f"<h2>Группа #{i} • {len(files)} копий</h2>")
    #         parts.append(f"<p class='muted'>Хеш: {h}</p>")
    #         parts.append("<table><tr><th>#</th><th>Превью</th><th>Путь</th>"
    #                      "<th>Размер</th><th>Изменён</th></tr>")
    #         for j, fp in enumerate(files, 1):
    #             try:
    #                 size = self.format_size(os.path.getsize(fp))
    #                 mt = datetime.fromtimestamp(
    #                     os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    #             except OSError:
    #                 size, mt = "N/A", "N/A"
    #             ext = os.path.splitext(fp)[1].lower()
    #             preview = ""
    #             if ext in IMG_EXTS:
    #                 uri = "file:///" + os.path.abspath(fp).replace("\\", "/")
    #                 preview = f"<img class='img-preview' src='{uri}'>"
    #             parts.append(
    #                 f"<tr><td>{j}</td><td>{preview}</td>"
    #                 f"<td><a href='file:///{os.path.abspath(fp)}'>{fp}</a></td>"
    #                 f"<td>{size}</td><td>{mt}</td></tr>")
    #         parts.append("</table>")

    #     parts.append("</body></html>")
    #     return "\n".join(parts)

    # >>> reporter.py >>>
    # def export_html_report(self):
    #     if not self.duplicates:
    #         messagebox.showwarning("Нет данных", "Сначала выполните поиск!")
    #         return
    #     fn = filedialog.asksaveasfilename(
    #         defaultextension=".html",
    #         filetypes=[("HTML", "*.html"), ("Все", "*.*")],
    #         title="HTML-отчёт",
    #         initialfile=f"duplicates_{datetime.now():%Y%m%d_%H%M%S}.html")
    #     if not fn:
    #         return
    #     try:
    #         with open(fn, "w", encoding="utf-8") as f:
    #             f.write(self._build_html_report())
    #         self.log_message(f"HTML-отчёт: {fn}", "success")
    #         if messagebox.askyesno("Готово", f"Отчёт сохранён:\n{fn}\n\nОткрыть?"):
    #             if os.name == "nt":
    #                 os.startfile(fn)
    #             else:
    #                 subprocess.run(["xdg-open", fn])
    #     except Exception as e:
    #         messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")

    # новый вариант тонкой обертки
    def export_html_report(self):
        if not self.duplicates:
            messagebox.showwarning("Нет данных", "Сначала выполните поиск!")
            return
        fn = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("Все", "*.*")],
            title="HTML-отчёт",
            initialfile=f"duplicates_{datetime.now():%Y%m%d_%H%M%S}.html")
        if not fn:
            return
        try:
            html = build_html_report(
                self.duplicates, self.stats, self.theme,
                folder_path=self.folder_path.get(),
                algorithm=self.hash_algo.get(),
            )
            with open(fn, "w", encoding="utf-8") as f:
                f.write(html)
            self.log_message(f"HTML-отчёт: {fn}", "success")
            if messagebox.askyesno("Готово", f"Отчёт сохранён:\n{fn}\n\nОткрыть?"):
                if os.name == "nt":
                    os.startfile(fn)
                else:
                    subprocess.run(["xdg-open", fn])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")

    # ==================================================================
    #      ЭКСПОРТ
    # ==================================================================
    
    # # >>> reporter.py >>>
    # def export_current_group(self):
    #     if not self.duplicate_keys or self.current_group >= len(self.duplicate_keys):
    #         messagebox.showwarning("Нет данных", "Нет активной группы.")
    #         return
    #     key = self.duplicate_keys[self.current_group]
    #     files = self.duplicates[key]

    #     fn = filedialog.asksaveasfilename(
    #         defaultextension=".txt",
    #         filetypes=[("Текст", "*.txt"), ("JSON", "*.json"), ("CSV", "*.csv")],
    #         title=f"Сохранить группу #{self.current_group + 1}",
    #         initialfile=f"group_{self.current_group + 1}.txt")
    #     if not fn:
    #         return

    #     if fn.endswith(".json"):
    #         data = {"group_number": self.current_group + 1, "hash": key,
    #                 "file_count": len(files), "files": files,
    #                 "export_time": datetime.now().isoformat()}
    #         with open(fn, "w", encoding="utf-8") as f:
    #             json.dump(data, f, ensure_ascii=False, indent=2)
    #     elif fn.endswith(".csv"):
    #         import csv
    #         with open(fn, "w", newline="", encoding="utf-8") as f:
    #             w = csv.writer(f, delimiter=";")
    #             w.writerow(["Группа", "Хеш", "Номер", "Путь", "Размер", "Изменён"])
    #             for j, fp in enumerate(files, 1):
    #                 try:
    #                     size = os.path.getsize(fp)
    #                     mt = datetime.fromtimestamp(
    #                         os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    #                 except OSError:
    #                     size, mt = "N/A", "N/A"
    #                 w.writerow([self.current_group + 1, key, j, fp, size, mt])
    #     else:
    #         with open(fn, "w", encoding="utf-8") as f:
    #             f.write(f"ГРУППА #{self.current_group + 1}\nХеш: {key}\n"
    #                     f"Файлов: {len(files)}\n\n")
    #             for i, fp in enumerate(files, 1):
    #                 try:
    #                     size = self.format_size(os.path.getsize(fp))
    #                 except OSError:
    #                     size = "N/A"
    #                 f.write(f"{i}. {fp} ({size})\n")

    #     self.log_message(f"Группа #{self.current_group + 1} → {fn}", "success")
    #     messagebox.showinfo("Готово", f"Сохранено:\n{fn}")


    # Новая Тонкая обёртка для функции <<< reporter.py <<< 
    def export_current_group(self):
        if not self.duplicate_keys or self.current_group >= len(self.duplicate_keys):
            messagebox.showwarning("Нет данных", "Нет активной группы.")
            return
        key = self.duplicate_keys[self.current_group]
        files = self.duplicates[key]

        fn = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Текст", "*.txt"), ("JSON", "*.json"), ("CSV", "*.csv")],
            title=f"Сохранить группу #{self.current_group + 1}",
            initialfile=f"group_{self.current_group + 1}.txt")
        if not fn:
            return

        group_num = self.current_group + 1
        try:
            if fn.endswith(".json"):
                write_group_json(group_num, key, files, fn)
            elif fn.endswith(".csv"):
                write_group_csv(group_num, key, files, fn)
            else:
                write_group_txt(group_num, key, files, fn)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")
            return

        self.log_message(f"Группа #{group_num} -> {fn}", "success")
        messagebox.showinfo("Готово", f"Сохранено:\n{fn}")

    # >>> reports.py >>>  
    # def export_results(self):
    #     if not self.duplicates:
    #         messagebox.showwarning("Нет данных", "Сначала выполните поиск!")
    #         return
    #     fn = filedialog.asksaveasfilename(
    #         defaultextension=".json",
    #         filetypes=[("JSON", "*.json"), ("Текст", "*.txt"),
    #                    ("CSV", "*.csv"), ("HTML", "*.html")],
    #         title="Сохранить результаты",
    #         initialfile=f"duplicates_{datetime.now():%Y%m%d_%H%M%S}")
    #     if not fn:
    #         return

    #     if fn.endswith(".json"):
    #         data = {
    #             "metadata": {
    #                 "search_path": self.folder_path.get(),
    #                 "algorithm": self.hash_algo.get(),
    #                 "thread_count": self.thread_count.get(),
    #                 "use_cache": self.use_cache_var.get(),
    #                 "timestamp": datetime.now().isoformat(),
    #                 "program_version": DF_VERSION,
    #             },
    #             "statistics": {
    #                 "total_groups": len(self.duplicates),
    #                 "total_files": sum(len(f) for f in self.duplicates.values()),
    #                 "cache_hits": self._cache_hits,
    #                 "cache_misses": self._cache_misses,
    #                 "wasted_size": self.stats.get("wasted_size", 0),
    #             },
    #             "duplicates": self.duplicates,
    #         }
    #         with open(fn, "w", encoding="utf-8") as f:
    #             json.dump(data, f, ensure_ascii=False, indent=2)
    #     elif fn.endswith(".csv"):
    #         import csv
    #         with open(fn, "w", newline="", encoding="utf-8") as f:
    #             w = csv.writer(f, delimiter=";")
    #             w.writerow(["Группа", "Хеш", "Номер", "Путь", "Размер", "Изменён"])
    #             for i, (h, files) in enumerate(self.duplicates.items(), 1):
    #                 for j, fp in enumerate(files, 1):
    #                     try:
    #                         size = os.path.getsize(fp)
    #                         mt = datetime.fromtimestamp(
    #                             os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    #                     except OSError:
    #                         size, mt = "N/A", "N/A"
    #                     w.writerow([i, h, j, fp, size, mt])
    #     elif fn.endswith(".html"):
    #         with open(fn, "w", encoding="utf-8") as f:
    #             f.write(self._build_html_report())
    #     else:
    #         self.save_results_to_file(self.duplicates, target=fn)

    #     messagebox.showinfo("Готово", f"Сохранено:\n{fn}")

    # Тонкая обёртка из <<< reports.py <<< 
    def export_results(self):
        if not self.duplicates:
            messagebox.showwarning("Нет данных", "Сначала выполните поиск!")
            return
        fn = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("Текст", "*.txt"),
                       ("CSV", "*.csv"), ("HTML", "*.html")],
            title="Сохранить результаты",
            initialfile=f"duplicates_{datetime.now():%Y%m%d_%H%M%S}")
        if not fn:
            return

        kwargs = dict(
            stats=self.stats,
            folder_path=self.folder_path.get(),
            algorithm=self.hash_algo.get(),
            thread_count=self.thread_count.get(),
            use_cache=self.use_cache_var.get(),
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
        )

        try:
            if fn.endswith(".json"):
                write_results_json(self.duplicates, fn, **kwargs)
            elif fn.endswith(".csv"):
                write_results_csv(self.duplicates, df)
            elif fn.endswith(".html"):
                html = build_html_report(
                    self.duplicates, self.stats, self.theme,
                    folder_path=self.folder_path.get(),
                    algorithm=self.hash_algo.get(),
                )
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(html)
            else:
                content = build_txt_report(self.duplicates, **kwargs)
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")
            return

        messagebox.showinfo("Готово", f"✅ Сохранено:\n{fn}")

    # ==================================================================
    # УТИЛИТЫ
    # ==================================================================
    
    # >>> utils.py >>>
    # def format_size(self, size_bytes):
    #     if size_bytes == 0:
    #         return "0 Б"
    #     names = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"]
    #     i = 0
    #     while size_bytes >= 1024 and i < len(names) - 1:
    #         size_bytes /= 1024.0
    #         i += 1
    #     return f"{size_bytes:.2f} {names[i]}"

    # <<< utils.py <<<
    def format_size(self, size_bytes):
        return format_size(size_bytes)   

    # # >>> reports.py >>> 
    # def save_results_to_file(self, duplicates, target=None):
    #     if not duplicates:
    #         return
    #     if target is None:
    #         os.makedirs(REPORTS_DIR, exist_ok=True)
    #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    #         target = os.path.join(REPORTS_DIR, f"duplicates_report_{ts}.txt")

    #     total_files = sum(len(fs) for fs in duplicates.values())
    #     wasted = self.stats.get("wasted_size", 0)

    #     with open(target, "w", encoding="utf-8") as f:
    #         f.write("=" * 85 + "\n" + " " * 25 + "ОТЧЕТ О ДУБЛИКАТАХ\n" + "=" * 85 + "\n\n")
    #         f.write(f"📅 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    #         f.write(f"📁 {self.folder_path.get()}\n")
    #         f.write(f"⚡ {self.hash_algo.get().upper()} | Потоки: {self.thread_count.get()}\n")
    #         f.write(f"🔍 Групп: {len(duplicates):,}\n")
    #         f.write(f"📄 Файлов: {total_files:,}\n")
    #         f.write(f"🗑️ Лишний объем: {self.format_size(wasted)}\n\n")
    #         f.write("=" * 85 + "\n" + " " * 30 + "ДЕТАЛЬНО\n" + "=" * 85 + "\n\n")
    #         for i, (h, files) in enumerate(duplicates.items(), 1):
    #             f.write(f"ГРУППА #{i}\nХеш: {h}\nКопий: {len(files)}\n" + "─" * 40 + "\n")
    #             for j, fp in enumerate(files, 1):
    #                 try:
    #                     size = self.format_size(os.path.getsize(fp))
    #                     mt = datetime.fromtimestamp(
    #                         os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    #                 except OSError:
    #                     size, mt = "N/A", "N/A"
    #                 f.write(f"\n{j}. {fp}\n   Размер: {size} | Изменён: {mt}\n")
    #             f.write("\n" + "═" * 85 + "\n\n")
    #         f.write("=" * 85 + "\n" + " " * 30 + "КОНЕЦ ОТЧЕТА\n" + "=" * 85 + "\n")

    #     if target.startswith(REPORTS_DIR):
    #         self.log_message(f"📄 Отчёт: {target}", "success")
    #         self.last_report_file = target

    def open_results_folder(self):
        if os.path.exists(REPORTS_DIR):
            if os.name == "nt":
                os.startfile(REPORTS_DIR)
            elif os.name == "posix":
                subprocess.run(["xdg-open", REPORTS_DIR])
        else:
            messagebox.showinfo("Не найдено", "Папка отчётов ещё не создана.")

    # ==================================================================
    # ОЧЕРЕДЬ
    # ==================================================================
    def log_message(self, message, msg_type="info"):
        self.queue.put(("log", (message, msg_type)))

    def _eta_str(self, current, total):
        if not self._progress_start_ts or current == 0:
            return ""
        elapsed = time.time() - self._progress_start_ts
        speed = current / elapsed if elapsed > 0 else 0
        if speed <= 0:
            return ""
        remain = (total - current) / speed
        return f"ETA: {int(remain // 60)}м {int(remain % 60):02d}с ({speed:.0f} ф/с)"

    def process_queue(self):
        try:
            while True:
                msg_type, data = self.queue.get_nowait()

                if msg_type == "log":
                    message, mtype = data
                    self.text_output.insert(tk.END, message + "\n", mtype)
                    self.text_output.see(tk.END)

                elif msg_type == "status":
                    self.status_var.set(data)

                elif msg_type == "progress_scan":
                    cur, tot = data
                    self.progress_var.set((cur / tot) * 20 if tot else 0)
                    self.progress_label.config(text=f"Сканирование {cur:,}/{tot:,}")
                    self.progress_detail_var.set(self._eta_str(cur, tot))

                elif msg_type == "progress_quick":
                    cur, tot = data
                    self.progress_var.set(20 + (cur / tot) * 30 if tot else 20)
                    self.progress_label.config(text=f"Быстрый хеш {cur:,}/{tot:,}")
                    self.progress_detail_var.set(self._eta_str(cur, tot))

                elif msg_type == "progress_hash":
                    cur, tot = data
                    self.progress_var.set(50 + (cur / tot) * 50 if tot else 50)
                    self.progress_label.config(text=f"Полный хеш {cur:,}/{tot:,}")
                    self.progress_detail_var.set(self._eta_str(cur, tot))

                elif msg_type == "stats_result":
                    groups, files = data
                    self.stats["duplicate_groups"] = groups
                    self.stats["duplicate_files"] = files
                    self.stats["end_time"] = datetime.now()

                elif msg_type == "show_results":
                    self.update_group_display()
                    self._refresh_all_tree()
                    self.notebook.select(self.tab_all)

                elif msg_type == "error":
                    self.text_output.insert(tk.END, f"❌ {data}\n", "error")
                    messagebox.showerror("Ошибка", str(data)[:200])

                elif msg_type == "done":
                    self.search_in_progress = False
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.btn_export.config(state=tk.NORMAL)
                    self.progress_var.set(100)
                    self.progress_label.config(text="Завершено")
                    if self.duplicates:
                        self.status_var.set(
                            f"✅ {self.stats.get('duplicate_groups', 0):,} групп дубликатов")
                    else:
                        self.status_var.set("✅ Поиск завершён. Дубликатов нет.")
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_queue)


# ============================================================================
def main():
    try:
        import tkinter  # noqa
    except ImportError:
        print("Tkinter не установлен!")
        return

    root = tk.Tk()
    app = DuplicateFinderApp(root)

    def on_closing():
        if app.search_in_progress:
            if not messagebox.askyesno("Выход", "Поиск выполняется. Выйти?"):
                return
        try:
            app.settings["window_geometry"] = root.geometry()
            save_settings(app.settings)
        except Exception:
            pass
        try:
            if app.cache:
                app.cache.close()
        except Exception:
            pass
        root.destroy()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", on_closing)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nПрограмма завершена")


if __name__ == "__main__":
    main()