"""Сохранение и загрузка сессий ANDUP (.dfsess)

формат - JSON  с метаданными и словарём `duplicates`.
При загрузке фильтруются записи о файлах, которых больше нет на диске.
"""

import json
import logging
import os
from datetime import datetime
from andup.constants import DF_VERSION

log = logging.getLogger("df")


def save_session(path, duplicates, stats, folder_path="", algorithm="sha256", version=DF_VERSION):
    """Сохраняет сессию в файл.
    
    Возвращает (успех: bool, ошибка: str | None).
    """
    data = {
        "version": version,
        "saved_at": datetime.now().isoformat(),
        "search_path": folder_path,
        "stats": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in stats.items()},
        "duplicates": duplicates,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True, None
    except Exception as e:
        log.exception("Ошибка сохранения сессии")
        return False, str(e)


def load_session(path):
    """Загружает сессию из файла.
    
    Возвращает dict с полями:
    - duplicates: dict[str, list[str]] (только существующие файлы)
    - stats: dict (пересчитанные под фактическое содержимое)
    - search_path: str
    - algorithm: str
    - version: str
    или None при ошибке.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.exception("Ошибка чтения сессии")
        return None

    raw = data.get("duplicates", {})
    # Фильтруем исчезнувшие файлы и группы, где осталось < 2 файлов
    duplicates = {}
    for h, files in raw.items():
        alive = [f for f in files if os.path.exists(f)]
        if len(alive) > 1:
            duplicates[h] = alive

    # Пересчитываем статистику по факту
    total_size = 0
    wasted_size = 0
    for files in duplicates.values():
        try:
            s = os.path.getsize(files[0])
            total_size += s * len(files)
            wasted_size += s * (len(files) - 1)
        except OSError:
            pass

    old_stats = data.get("stats", {})
    stats = {
        "total_files": old_stats.get("total_files", 0),
        "duplicate_groups": len(duplicates),
        "duplicate_files": sum(len(v) for v in duplicates.values()),
        "start_time": old_stats.get("start_time"),
        "end_time": old_stats.get("end_time"),
        "total_size": total_size,
        "wasted_size": wasted_size,
    }

    return {
        "version": data.get("version", "unknown"),
        "duplicates": duplicates,
        "stats": stats,
        "search_path": data.get("search_path", ""),
        "algorithm": data.get("algorithm", "sha256"),
    }