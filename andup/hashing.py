"""Хеширование файлов: быстрый и полный варианты"""

import hashlib
import os

from andup.constants import BLOCK_SIZE


def calculate_hash(filepath, algorithm="md5", is_stopped=None):
    """
    Полный хеш файла.

    is_stopped - необязательный callable, возвращающий True, если пора прервать.
    Бросает Exception при ошибке чтения.
    """
    h = hashlib.new(algorithm)
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(BLOCK_SIZE), b""):
                if is_stopped and is_stopped():
                    break
                h.update(chunk)
    except Exception as e:
        raise Exception(f"Ошибка чтения {filepath}: {e}")
    return h.hexdigest()


def calculate_quick_hash(filepath, algorithm="md5"):
    """
    Быстрый хеш: только первые и последние 64 кб.

    Для файлов <= 2xBLOCK_SIZE читается целиком.
    Возвращает str или None при ошибке.
    """
    h = hashlib.new(algorithm)
    try:
        size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            if size <= BLOCK_SIZE * 2:
                h.update(f.read())
            else:
                h.update(f.read(BLOCK_SIZE))
                f.seek(-BLOCK_SIZE, os.SEEK_END)
                h.update(f.read(BLOCK_SIZE))
    except Exception:
        return None
    return h.hexdigest()