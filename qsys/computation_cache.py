"""计算缓存层：缓存因子计算、IC序列、walk-forward结果。

核心功能：
1. 内存缓存：LRU缓存热数据
2. SQLite缓存：持久化缓存
3. 缓存装饰器：简化缓存使用
4. 缓存失效：基于时间/数据版本的失效策略
"""

import hashlib
import json
import logging
import pickle
import sqlite3
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

log = logging.getLogger("cache")

# 缓存配置
DEFAULT_TTL = 3600 * 4  # 4小时
MAX_CACHE_SIZE = 1000  # 内存缓存最大条目


class LRUCache:
    """简单的LRU缓存（内存）"""
    
    def __init__(self, maxsize: int = MAX_CACHE_SIZE):
        self.maxsize = maxsize
        self._cache: dict[str, tuple[Any, float]] = {}
        self._order: list[str] = []
    
    def get(self, key: str) -> Any | None:
        if key in self._cache:
            value, ts = self._cache[key]
            self._order.remove(key)
            self._order.append(key)
            return value
        return None
    
    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL):
        if len(self._cache) >= self.maxsize:
            oldest = self._order.pop(0)
            del self._cache[oldest]
        self._cache[key] = (value, time.time() + ttl)
        self._order.append(key)
    
    def invalidate(self, prefix: str = ""):
        """失效指定前缀的缓存"""
        keys_to_remove = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._cache[k]
            self._order.remove(k)
    
    def clear(self):
        self._cache.clear()
        self._order.clear()


class SQLiteCache:
    """SQLite持久化缓存"""
    
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value BLOB,
                    created_at REAL,
                    ttl INTEGER,
                    access_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_created 
                ON cache(created_at)
            """)
    
    def get(self, key: str) -> Any | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value, created_at, ttl FROM cache WHERE key = ?",
                (key,)
            ).fetchone()
            
            if row is None:
                return None
            
            value, created_at, ttl = row
            if time.time() - created_at > ttl:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
            
            # 更新访问计数
            conn.execute(
                "UPDATE cache SET access_count = access_count + 1 WHERE key = ?",
                (key,)
            )
            
            return pickle.loads(value)
    
    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL):
        blob = pickle.dumps(value)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at, ttl) VALUES (?, ?, ?, ?)",
                (key, blob, time.time(), ttl)
            )
    
    def invalidate(self, prefix: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            if prefix:
                conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
            else:
                conn.execute("DELETE FROM cache")
    
    def cleanup(self, max_age: int = 86400 * 7):
        """清理超过max_age秒的缓存"""
        cutoff = time.time() - max_age
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
    
    def stats(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(access_count) FROM cache"
            ).fetchone()
            return {
                "entries": row[0] or 0,
                "total_accesses": row[1] or 0
            }


class ComputationCache:
    """计算结果缓存管理器"""
    
    def __init__(self, db_path: str | Path | None = None):
        self.memory = LRUCache()
        self.disk = SQLiteCache(db_path) if db_path else None
    
    def _make_key(self, prefix: str, *args, **kwargs) -> str:
        """生成缓存key"""
        raw = f"{prefix}:{args}:{sorted(kwargs.items())}"
        return hashlib.md5(raw.encode()).hexdigest()
    
    def get(self, key: str) -> Any | None:
        # 先查内存
        value = self.memory.get(key)
        if value is not None:
            return value
        # 再查磁盘
        if self.disk:
            value = self.disk.get(key)
            if value is not None:
                self.memory.set(key, value)  # 回填内存
            return value
        return None
    
    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL, persist: bool = True):
        self.memory.set(key, value, ttl)
        if persist and self.disk:
            self.disk.set(key, value, ttl)
    
    def invalidate(self, prefix: str = ""):
        self.memory.invalidate(prefix)
        if self.disk:
            self.disk.invalidate(prefix)
    
    def clear(self):
        self.memory.clear()
        if self.disk:
            self.disk.invalidate()


# 全局缓存实例
_cache: ComputationCache | None = None


def get_cache(db_path: str | Path | None = None) -> ComputationCache:
    """获取全局缓存实例"""
    global _cache
    if _cache is None:
        if db_path is None:
            from common import DATA_DIR
            db_path = DATA_DIR / "cache.db"
        _cache = ComputationCache(db_path)
    return _cache


def cached(prefix: str, ttl: int = DEFAULT_TTL, persist: bool = True):
    """缓存装饰器
    
    Usage:
        @cached("ic_series", ttl=3600)
        def compute_ic_series(factor_name, codes, end):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache = get_cache()
            key = cache._make_key(prefix, *args, **kwargs)
            
            # 尝试从缓存获取
            result = cache.get(key)
            if result is not None:
                log.debug(f"缓存命中: {prefix}")
                return result
            
            # 计算并缓存
            result = func(*args, **kwargs)
            cache.set(key, result, ttl=ttl, persist=persist)
            return result
        
        wrapper.invalidate = lambda: get_cache().invalidate(prefix)
        return wrapper
    return decorator


def cache_factor_values(factor_name: str, codes: list[str], end: str):
    """缓存因子值计算"""
    return cached(f"fv:{factor_name}", ttl=3600 * 2)


def cache_ic_series(factor_name: str, codes: list[str], end: str):
    """缓存IC序列计算"""
    return cached(f"ic:{factor_name}", ttl=3600 * 4)


def cache_walk_forward(factor_name: str, method: str, top_n: int):
    """缓存walk-forward结果"""
    return cached(f"wf:{factor_name}:{method}", ttl=3600 * 8)
