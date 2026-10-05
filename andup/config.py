"""Загрузка и сохранение пользовательских настроек."""

import json
import logging
import os

from andup.constants import SETTINGS_FILE

log = logging.getLogger("df")

DEFAULT_SETTINGS = {
    "recent_paths":         [],
    "dark_theme":           False,
    "delete_to_trash":      True,
    "ignore_folders":       ["node_modules", ".git", "__pycache__", ".cache",
                       "System Volume Information", "$RECYCLE.BIN", ".venv"],
    "last_algorithm":       "sha256",
    "last_threads":         2,
    "last_recursive":       True,
    "last_use_cache":       True,
    "last_extensions":      ".epub,.pdf,.fb2,.mobi,.txt,.doc,.docx,.jpg,.jpeg,.png,"
                       ".mp3,.mp4,.avi,.mkv,.zip,.rar,.djvu,.chm",
    "auto_select_strategy": "oldest",
    "window_geometry":      "1400x900",
}

def load_settings():
    """Прочитать настройки из файла, дополнить значениями по умолчанию."""
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if k in s:
                    s[k] = v
    except Exception as e:
        log.warning("Ошибка загрузки настроек: %s", e)
    return s

def save_settings(s):
    """Сохранить настройки на диск."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Ошибка сохранения настроек: %s", e)