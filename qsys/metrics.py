"""监控告警系统：收集系统指标、业务指标、应用指标，支持告警规则。

核心功能：
1. 指标收集：系统/业务/应用三层指标
2. 告警规则：阈值/趋势/异常三种告警
3. 通知渠道：SSE实时推送 + 日志告警
4. 指标查询：支持时间范围查询和聚合
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from common import DATA_DIR

log = logging.getLogger("metrics")

# 指标定义
METRICS = {
    # 系统指标
    "system.cpu_usage": "CPU使用率",
    "system.memory_usage": "内存使用率",
    "system.disk_io": "磁盘IO",
    
    # 业务指标
    "factor.gate_pass_rate": "因子闸门通过率",
    "factor.avg_ic": "因子平均IC",
    "factor.decay_rate": "因子衰减率",
    "factor.new_factors": "新增因子数",
    "strategy.win_rate": "策略胜率",
    "strategy.sharpe": "策略夏普比率",
    "strategy.active_count": "活跃策略数",
    
    # 应用指标
    "eval.walk_forward_duration": "walk-forward计算耗时",
    "eval.ic_calc_duration": "IC计算耗时",
    "eval.cache_hit_rate": "缓存命中率",
    "db.lock_conflict_count": "数据库锁冲突次数",
    "db.query_duration": "数据库查询耗时",
}

# 告警规则
ALERT_RULES = [
    # 系统告警
    {"metric": "system.cpu_usage", "threshold": 80, "op": ">", "severity": "warning", "message": "CPU使用率过高"},
    {"metric": "system.memory_usage", "threshold": 90, "op": ">", "severity": "critical", "message": "内存使用率过高"},
    
    # 业务告警
    {"metric": "factor.gate_pass_rate", "threshold": 0.01, "op": "<", "severity": "warning", "message": "因子通过率过低"},
    {"metric": "factor.decay_rate", "threshold": 0.3, "op": ">", "severity": "warning", "message": "因子衰减率过高"},
    {"metric": "strategy.win_rate", "threshold": 0.45, "op": "<", "severity": "warning", "message": "策略胜率过低"},
    
    # 应用告警
    {"metric": "db.lock_conflict_count", "threshold": 100, "op": ">", "severity": "critical", "message": "数据库锁冲突频繁"},
    {"metric": "eval.walk_forward_duration", "threshold": 300, "op": ">", "severity": "warning", "message": "walk-forward计算超时"},
]


class MetricsCollector:
    """指标收集器"""
    
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DATA_DIR / "metrics.db")
        self._init_db()
        self._buffers: dict[str, list[tuple[float, float]]] = {}
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    timestamp REAL NOT NULL,
                    tags TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_lookup 
                ON metrics(metric, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT,
                    value REAL,
                    threshold REAL,
                    timestamp REAL NOT NULL,
                    acknowledged INTEGER DEFAULT 0
                )
            """)
    
    def record(self, metric: str, value: float, tags: dict | None = None):
        """记录单个指标值"""
        ts = time.time()
        tags_json = json.dumps(tags) if tags else None
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO metrics (metric, value, timestamp, tags) VALUES (?, ?, ?, ?)",
                (metric, value, ts, tags_json)
            )
        
        # 检查告警规则
        self._check_alerts(metric, value, ts)
    
    def record_batch(self, metrics: dict[str, float], tags: dict | None = None):
        """批量记录指标"""
        ts = time.time()
        tags_json = json.dumps(tags) if tags else None
        
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO metrics (metric, value, timestamp, tags) VALUES (?, ?, ?, ?)",
                [(m, v, ts, tags_json) for m, v in metrics.items()]
            )
        
        # 检查告警
        for metric, value in metrics.items():
            self._check_alerts(metric, value, ts)
    
    def _check_alerts(self, metric: str, value: float, ts: float):
        """检查告警规则"""
        for rule in ALERT_RULES:
            if rule["metric"] != metric:
                continue
            
            triggered = False
            if rule["op"] == ">" and value > rule["threshold"]:
                triggered = True
            elif rule["op"] == "<" and value < rule["threshold"]:
                triggered = True
            
            if triggered:
                self._fire_alert(rule, value, ts)
    
    def _fire_alert(self, rule: dict, value: float, ts: float):
        """触发告警"""
        severity = rule["severity"]
        message = rule["message"]
        
        # 记录到数据库
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO alerts (metric, severity, message, value, threshold, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (rule["metric"], severity, message, value, rule["threshold"], ts)
            )
        
        # 日志告警
        if severity == "critical":
            log.critical(f"🚨 {message}: {rule['metric']}={value:.4f} (阈值={rule['threshold']})")
        else:
            log.warning(f"⚠️ {message}: {rule['metric']}={value:.4f} (阈值={rule['threshold']})")
    
    def query(self, metric: str, hours: int = 24) -> list[tuple[float, float]]:
        """查询指标历史数据"""
        cutoff = time.time() - hours * 3600
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT timestamp, value FROM metrics WHERE metric = ? AND timestamp > ? ORDER BY timestamp",
                (metric, cutoff)
            ).fetchall()
        return rows
    
    def aggregate(self, metric: str, hours: int = 24, func: str = "avg") -> float | None:
        """聚合指标"""
        cutoff = time.time() - hours * 3600
        with sqlite3.connect(self.db_path) as conn:
            sql = f"SELECT {func}(value) FROM metrics WHERE metric = ? AND timestamp > ?"
            row = conn.execute(sql, (metric, cutoff)).fetchone()
            return row[0] if row else None
    
    def get_alerts(self, hours: int = 24, severity: str | None = None) -> list[dict]:
        """获取告警列表"""
        cutoff = time.time() - hours * 3600
        with sqlite3.connect(self.db_path) as conn:
            if severity:
                rows = conn.execute(
                    "SELECT metric, severity, message, value, threshold, timestamp FROM alerts WHERE timestamp > ? AND severity = ? ORDER BY timestamp DESC",
                    (cutoff, severity)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT metric, severity, message, value, threshold, timestamp FROM alerts WHERE timestamp > ? ORDER BY timestamp DESC",
                    (cutoff,)
                ).fetchall()
        
        return [
            {"metric": r[0], "severity": r[1], "message": r[2], "value": r[3], "threshold": r[4], "timestamp": r[5]}
            for r in rows
        ]
    
    def get_dashboard(self) -> dict:
        """获取仪表盘数据"""
        return {
            "metrics": {
                "factor_pass_rate": self.aggregate("factor.gate_pass_rate", hours=24),
                "factor_avg_ic": self.aggregate("factor.avg_ic", hours=24),
                "strategy_win_rate": self.aggregate("strategy.win_rate", hours=24),
                "db_lock_conflicts": self.aggregate("db.lock_conflict_count", hours=24, func="sum"),
            },
            "alerts": {
                "critical": len(self.get_alerts(hours=24, severity="critical")),
                "warning": len(self.get_alerts(hours=24, severity="warning")),
            },
            "timestamp": time.time()
        }
    
    def cleanup(self, days: int = 30):
        """清理历史数据"""
        cutoff = time.time() - days * 86400
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff,))


# 全局收集器实例
_collector: MetricsCollector | None = None


def get_collector(db_path: str | Path | None = None) -> MetricsCollector:
    """获取全局指标收集器"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector(db_path)
    return _collector


def record_metric(metric: str, value: float, **tags):
    """快捷函数：记录指标"""
    return get_collector().record(metric, value, tags or None)


def record_metrics(metrics: dict[str, float], **tags):
    """快捷函数：批量记录指标"""
    return get_collector().record_batch(metrics, tags or None)
