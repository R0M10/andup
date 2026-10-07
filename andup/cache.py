"""SQLite-кэш хешей файло.

Потокобезопасный. Использвет WAL-режим для быстрой паралельной записи.
Ключ - путь файла; валидность проверяется по размеру и mtime.
"""

import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("df")


class HashCache:
    """Кэш соответствий путь -> хеш, хранящийся в SQLite."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS hash_cache (
            path TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            algo TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            timestamp REAL
        );
        CREATE INDEX IF NOT EXISTS idx_path ON hash_cache(path);
    """

    def __init__(self, db_path="hash_cache.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def get(self, filepath, algorithm, size, mtime):
        """Вернуть хеш из кэша или None, если записи нет или она устарела."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT hash, size, mtime from hash_cache WHERE path = ? and algo = ?",
                (filepath, algorithm),
            )
            row = cur.fetchone()
        if row is None:
            return None
        h, cs, cm = row
        if cs == size and abs(cm - mtime) < 1:
            return h
        return None

    def set(self, filepath, algorithm, hash_val, size, mtime):
        """Запись хеш в кэш (перезаписать, если есть)."""
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO hash_cache "
                "(path, hash, algo, size, mtime, timestamp) VALUES (?,?,?,?,?,?)",
                (filepath, hash_val, algorithm, size, mtime, time.time())
            )

    def delete(self, filepath):
        """Удалить запись по пути."""
        with self.lock:
            self.conn.execute("DELETE FROM hash_cache WHERE path = ?", (filepath,))

    def flush(self):
        """Зафиксировать транзакцию (commit)."""
        with self.lock:
            self.conn.commit()

    def clear(self):
        """Полностью очистить кэш и освободить место (VACUUM)."""
        with self.lock:
            self.conn.execute("DELETE FROM hash_cache")
            self.conn.commit()
            try:
                self.conn.execute("VACUUM;")
            except sqlite3.OperationalError:
                pass

    def count(self):
        """Число записей в кэше."""
        with self.lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM hash_cashe")
            return cur.fetchone()[0] or 0

    def size_butes(self):
        """Размер файла БД в байтах."""
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    def close(self):
        """Закрыть соединение (с автоматическим commit)"""
        with self.lock:
            if self.conn:
                try:
                    self.conn.commit()
                except Exception:
                    pass
                self.conn.close()
                self.conn = None