"""事务管理器：确保SQLite写操作的原子性和一致性。

核心功能：
1. 事务包装：所有写操作自动事务化
2. 原子提交：多表写操作原子提交
3. 失败回滚：异常时自动回滚
4. 批量操作：支持批量更新的事务管理
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable

log = logging.getLogger("txn")


class TransactionManager:
    """SQLite事务管理器 - 确保写操作原子性"""
    
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
    
    @contextmanager
    def transaction(self):
        """事务上下文管理器
        
        Usage:
            with txn_mgr.transaction():
                conn.execute("INSERT INTO ...")
                conn.execute("UPDATE ...")
                # 自动提交或回滚
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            log.error(f"事务回滚: {e}")
            raise
    
    @contextmanager
    def savepoint(self, name: str = "sp"):
        """嵌套事务（Savepoint）"""
        try:
            self.conn.execute(f"SAVEPOINT {name}")
            yield self.conn
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
        except Exception as e:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            log.warning(f"Savepoint '{name}' 回滚: {e}")
            raise
    
    def batch_execute(self, operations: list[tuple[str, tuple | list[tuple]]]):
        """批量执行SQL操作（原子性）
        
        Args:
            operations: [(sql, params), ...] 或 [(sql, [params1, params2, ...]), ...]
        """
        with self.transaction() as conn:
            for sql, params in operations:
                if isinstance(params[0], (list, tuple)):
                    # 批量插入
                    conn.executemany(sql, params)
                else:
                    conn.execute(sql, params)
    
    def upsert_factor(self, name: str, **kwargs):
        """因子注册表upsert（原子性）"""
        fields = list(kwargs.keys())
        values = list(kwargs.values())
        
        sql = f"""
            INSERT INTO factor_registry (name, {', '.join(fields)})
            VALUES (?, {', '.join(['?'] * len(fields))})
            ON CONFLICT(name) DO UPDATE SET
            {', '.join([f'{f} = excluded.{f}' for f in fields])}
        """
        params = [name] + values
        
        with self.transaction() as conn:
            conn.execute(sql, params)
    
    def batch_upsert_factors(self, factors: list[dict]):
        """批量因子upsert（原子性）"""
        with self.transaction() as conn:
            for f in factors:
                name = f.pop("name")
                fields = list(f.keys())
                values = list(f.values())
                sql = f"""
                    INSERT INTO factor_registry (name, {', '.join(fields)})
                    VALUES (?, {', '.join(['?'] * len(fields))})
                    ON CONFLICT(name) DO UPDATE SET
                    {', '.join([f'{f_} = excluded.{f_}' for f_ in fields])}
                """
                conn.execute(sql, [name] + values)
    
    def update_strategy(self, name: str, **kwargs):
        """策略包更新（原子性）"""
        fields = list(kwargs.keys())
        values = list(kwargs.values())
        
        sql = f"""
            INSERT INTO strategies (name, {', '.join(fields)})
            VALUES (?, {', '.join(['?'] * len(fields))})
            ON CONFLICT(name) DO UPDATE SET
            {', '.join([f'{f} = excluded.{f}' for f in fields])}
        """
        params = [name] + values
        
        with self.transaction() as conn:
            conn.execute(sql, params)
    
    def delete_factor(self, name: str):
        """删除因子（级联清理相关数据）"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM factor_scorecards WHERE name = ?", (name,))
            conn.execute("DELETE FROM factor_usage WHERE factor_name = ?", (name,))
            conn.execute("DELETE FROM factor_registry WHERE name = ?", (name,))
    
    def execute_with_retry(self, func: Callable, max_retries: int = 3):
        """带重试的执行（处理锁冲突）"""
        import time
        for attempt in range(max_retries):
            try:
                with self.transaction() as conn:
                    return func(conn)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    wait = 0.1 * (2 ** attempt)  # 指数退避
                    log.warning(f"数据库锁冲突，{wait:.1f}s后重试 ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                else:
                    raise


def with_transaction(conn: sqlite3.Connection):
    """快捷函数：返回事务管理器"""
    return TransactionManager(conn)
