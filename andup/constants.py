"""Глобальные константы и путь ANDUP"""

import os

# ---- версия -----------------------------------------
DF_VERSION              = "0.9.0"
DF_VERSION_TEXT         = f"Версия: {DF_VERSION}"

# ---- размеры буферов и батчей -----------------------
BLOCK_SIZE              = 65536     # 64 КБ - размер блока чтения файла
FUTURES_BATCH           = 200       # сколько задач отдавать ThreadPoolExecutor за раз

# ---- имена файлов и папок, создаваемых программой ---
SETTINGS_FILE           = "duplicate_finder_settings.json"
LOG_FILE                = "duplicate_finder.log"
TRASH_DIR               = "DuplicateFinder_Trash"
REPORTS_DIR             = "DuplicateFinder_Reports"

# ---- защищённые расширения --------------------------
PROTECTED_EXTS          = {".exe", ".dll", ".sys", ".drv", ".so", ".dylib", ".msi"}

# ---- расширения, которые считаются картинками -------
IMG_EXTS                = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}

# ---- защищённые системные пути ----------------------
if os.name == "nt":
    # Windows
    PROTECTED_PATHS = [
        os.environ.get("WINDIR", r"C:\Windows"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
    ]
else:
    # Linux
    PROTECTED_PATHS = ["/System", "/usr", "/bin", "/sbin", "/etc", "/var"]