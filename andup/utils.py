"""Утилиты: защита путей, корзина, хардлинки, форматирование размеров."""

import logging
import os
import shutil
import tempfile
import time
from datetime import datetime

from andup.constants import TRASH_DIR, PROTECTED_EXTS, PROTECTED_PATHS

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

log = logging.getLogger("df")

def check_protected(filepath):
    """True, если путь защищен (системная папка или опасное расширение)"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in PROTECTED_EXTS:
        return True
    try:
        real = os.path.realpath(filepath).lower()
    except OSError:
        return False
    for p in PROTECTED_PATHS:
        try:
            rp = os.path.realpath(p).lower()
        except OSError:
            continue
        if real == rp or real.startswith(rp + os.sep):
            return True
    return False

def move_to_trash(filepath, use_send2trash=True):
    """
    Пытается отправить файл в систмную корзину, иначе - локальную.
    
    Возвращает (успех: bool, ошибка: str | None)
    """
    if use_send2trash and HAS_SEND2TRASH:
        try:
            # Нормализация пути: send2trash на Windows
            normalized = os.path.abspath(filepath)
            if normalized.startswith("\\\\?\\"):
                normalized = normalized[4:]
            send2trash(normalized)
            return True, None
        except Exception as e:
            #log.warning("send2trash не сработал: %s", e)
            log.info("send2trash недоступен для %s, использую локальную корзину: %s",
                     filepath, e)

    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        drive, rest = os.path.splitdrive(os.path.abspath(filepath))
        safe = rest.lstrip("\\/").replace(":", "_")
        target_dir = os.path.join(TRASH_DIR, ts, os.path.dirname(safe))
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(filepath))
        if os.path.exists(target):
            target += f"_{int(time.time() * 1000) % 100000}"
        shutil.move(filepath, target)
        return True, None
    except Exception as e:
        return False, str(e)

def create_hardlink(src, dst):
    """
    Безопасно заменяет dst жёсткой ссылкой на src (атомарно)

    Возвращает (успех: bool, ошибка: str | None)
    """
    try:
        if not os.path.exists(src):
            return False, "Источник не найден"
        if os.path.exists(dst):
            try:
                if os.path.samefile(src, dst):
                    return True, None
            except OSError:
                pass
            tmp_dir = os.path.dirname(dst) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".dflink_", dir=tmp_dir)
            os.close(fd)
            os.remove(tmp_path)
            try:
                os.link(src, tmp_path)
                os.replace(tmp_path, dst)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        else:
            os.link(src, dst)
        return True, None
    except Exception as e:
        return False, str(e)


##### Эксперемент выравнивания адреса к файлу #######
def normalize_path(path):
    """
    Приводит путь к каноничному виду OS (os.sep)

    tkinter.filedialog на Windows возвращает пути с прямыми слешами ,
    а os.walk - с обратрными. Смешение может путать SQLite-кэш
    и внешние утилиты, поэтому нормализуем.
    """
    if not path:
        return path
    # windows 
    if len(path) >= 2 and path[1] == ":" and (len(path) < 3 or path[2] not in ("\\", "/")):
        path = path[:2] + os.sep + path[2:]
    return os.path.normpath(path)


def format_size(size_bytes):
    """Человеческий размер: 1.23 МБ, 4.56 ГБ и т.п."""
    if size_bytes == 0:
        return "0 Б"
    names = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"]
    i = 0
    while size_bytes >= 1024 and i < len(names) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {names[i]}"