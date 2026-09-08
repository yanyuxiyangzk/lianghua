"""价格监控器：事件驱动的交易触发机制。

核心思路：
  - 数据采集层（定时）负责拉取行情
  - PriceMonitor 监听价格更新事件，立即评估触发条件
  - 触发后立即执行交易（止盈/止损/到期/限价单成交）

优势：
  - 响应及时：价格变动立即触发，无轮询延迟
  - 资源节省：只监控活跃标的，不全量扫描
  - 架构清晰：数据采集与交易逻辑解耦
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger("price_monitor")


class TriggerType(str, Enum):
    TAKE_PROFIT = "止盈"
    STOP_LOSS = "止损"
    EXPIRE = "到期"
    LIMIT_BUY = "限价买入"
    LIMIT_SELL = "限价卖出"


@dataclass
class TriggerCondition:
    """触发条件"""
    position_id: int
    code: str
    trigger_type: TriggerType
    threshold: float  # 止盈/止损价 或 限价
    buy_price: float
    shares: int
    buy_date: str
    expire_date: Optional[str] = None  # 到期日期
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class PriceMonitor:
    """价格监控器：被关注的代码+触发条件，在价格更新时立即评估。

    使用方式：
        monitor = PriceMonitor.get_instance()
        monitor.register_position(position_id, code, buy_price, shares, buy_date, rules)
        # 当价格更新时
        monitor.on_price_update(code, price)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_monitor()
            return cls._instance

    def _init_monitor(self):
        self._watched: dict[str, list[TriggerCondition]] = {}  # {code: [TriggerCondition]}
        self._trigger_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {"registered": 0, "triggered": 0, "evaluated": 0}

    @classmethod
    def get_instance(cls) -> "PriceMonitor":
        return cls()

    def register_position(self, position_id: int, code: str, buy_price: float,
                          shares: int, buy_date: str, rules: dict) -> None:
        """开仓后注册止盈/止损/到期条件。

        Args:
            position_id: 持仓ID
            code: 股票代码
            buy_price: 买入价
            shares: 持仓股数
            buy_date: 买入日期
            rules: {"take_profit": 0.15, "stop_loss": -0.08, "hold_days": 20, "cost": 0.0025}
        """
        tp_price = buy_price * (1 + rules["take_profit"])
        sl_price = buy_price * (1 + rules["stop_loss"])

        # 计算到期日期（简单用日历日，精确应交易日历）
        from datetime import timedelta
        buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
        expire_dt = buy_dt + timedelta(days=rules["hold_days"] + 10)  # 多加几天容错
        expire_date = expire_dt.strftime("%Y-%m-%d")

        conditions = [
            TriggerCondition(position_id, code, TriggerType.TAKE_PROFIT, tp_price,
                             buy_price, shares, buy_date, expire_date),
            TriggerCondition(position_id, code, TriggerType.STOP_LOSS, sl_price,
                             buy_price, shares, buy_date, expire_date),
            TriggerCondition(position_id, code, TriggerType.EXPIRE, 0,
                             buy_price, shares, buy_date, expire_date),
        ]

        with self._trigger_lock:
            if code not in self._watched:
                self._watched[code] = []
            self._watched[code].extend(conditions)
        with self._stats_lock:
            self._stats["registered"] += len(conditions)

        logger.info(f"注册监控 {code}: 止盈={tp_price:.2f} 止损={sl_price:.2f} 到期={expire_date}")

    def register_pending_order(self, order_id: int, code: str, limit_price: float,
                               side: str, shares: int) -> None:
        """挂单后注册限价触发条件。

        Args:
            order_id: 挂单ID
            code: 股票代码
            limit_price: 限价
            side: "buy" 或 "sell"
            shares: 委托股数
        """
        trigger_type = TriggerType.LIMIT_BUY if side == "buy" else TriggerType.LIMIT_SELL
        condition = TriggerCondition(
            position_id=order_id, code=code, trigger_type=trigger_type,
            threshold=limit_price, buy_price=0, shares=shares, buy_date=""
        )

        with self._trigger_lock:
            if code not in self._watched:
                self._watched[code] = []
            self._watched[code].append(condition)
        with self._stats_lock:
            self._stats["registered"] += 1

        logger.info(f"注册挂单监控 {code}: {side} @ {limit_price:.2f} x {shares}")

    def unregister(self, code: str, position_id: int = None) -> None:
        """取消监控（平仓/撤单后调用）。"""
        with self._trigger_lock:
            if code not in self._watched:
                return
            if position_id is not None:
                self._watched[code] = [
                    c for c in self._watched[code] if c.position_id != position_id
                ]
                if not self._watched[code]:
                    del self._watched[code]
            else:
                del self._watched[code]

    def on_price_update(self, code: str, price: float, timestamp: str = None) -> list[dict]:
        """价格更新时立即评估所有关联条件。

        Args:
            code: 股票代码
            price: 最新价
            timestamp: 时间戳（可选）

        Returns:
            触发的事件列表 [{"type": "止盈", "code": "SH600000", ...}]
        """
        if code not in self._watched:
            return []

        events = []
        now = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = now[:10]

        # 拷贝条件列表，防止并发修改异常
        with self._trigger_lock:
            conditions = list(self._watched.get(code, []))

        for cond in conditions:
            event = None

            if cond.trigger_type == TriggerType.TAKE_PROFIT:
                if price >= cond.threshold:
                    event = self._make_event(cond, "止盈", price, now)

            elif cond.trigger_type == TriggerType.STOP_LOSS:
                if price <= cond.threshold:
                    event = self._make_event(cond, "止损", price, now)

            elif cond.trigger_type == TriggerType.EXPIRE:
                if cond.expire_date and today >= cond.expire_date:
                    event = self._make_event(cond, "到期", price, now)

            elif cond.trigger_type == TriggerType.LIMIT_BUY:
                if price <= cond.threshold:
                    event = self._make_event(cond, "限价成交", price, now)

            elif cond.trigger_type == TriggerType.LIMIT_SELL:
                if price >= cond.threshold:
                    event = self._make_event(cond, "限价成交", price, now)

            if event:
                events.append(event)

        # 批量更新统计
        with self._stats_lock:
            self._stats["evaluated"] += len(conditions)
            self._stats["triggered"] += len(events)

        # 发布事件到EventBus，由订阅者决定是否执行
        if events:
            try:
                from event_bus import bus, EventType
                for event in events:
                    bus.push(EventType.PRICE_ALERT, **event)
            except Exception as e:
                logger.error(f"发布事件到EventBus失败: {e}")

        return events

    def _make_event(self, cond: TriggerCondition, reason: str,
                    price: float, now: str) -> dict:
        """构造触发事件"""
        return {
            "type": reason,
            "code": cond.code,
            "position_id": cond.position_id,
            "trigger_price": price,
            "buy_price": cond.buy_price,
            "shares": cond.shares,
            "buy_date": cond.buy_date,
            "timestamp": now,
        }

    def get_watched_codes(self) -> list[str]:
        """获取当前监控的股票代码列表"""
        with self._trigger_lock:
            return list(self._watched.keys())

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._stats_lock:
            stats = {**self._stats}
        with self._trigger_lock:
            stats["watched_codes"] = len(self._watched)
            stats["total_conditions"] = sum(len(v) for v in self._watched.values())
        return stats

    def load_from_db(self) -> int:
        """从数据库加载当前持仓，注册监控条件。

        Returns:
            注册的条件数量
        """
        db_path = Path("/data/experience.db")
        if not db_path.exists():
            return 0

        try:
            with sqlite3.connect(str(db_path)) as c:
                opens = pd.read_sql(
                    "SELECT id, code, buy_price, shares, buy_date FROM positions WHERE status='open'",
                    c
                )
        except Exception as e:
            logger.error(f"加载持仓失败: {e}")
            return 0

        # 清空现有条件，防止重复注册导致条件无限累积
        with self._trigger_lock:
            self._watched.clear()
            self._stats["registered"] = 0

        if opens.empty:
            return 0

        rules = {"take_profit": 0.15, "stop_loss": -0.08, "hold_days": 20, "cost": 0.0025}
        count = 0
        for _, row in opens.iterrows():
            self.register_position(
                position_id=row["id"],
                code=row["code"],
                buy_price=row["buy_price"],
                shares=row["shares"],
                buy_date=row["buy_date"],
                rules=rules
            )
            count += 1

        logger.info(f"从数据库加载 {count} 个持仓，注册 {self._stats['registered']} 个监控条件")
        return count


# 全局单例
monitor = PriceMonitor.get_instance()


def init_monitor() -> int:
    """初始化监控器：从数据库加载持仓，注册监控条件。"""
    return monitor.load_from_db()


def on_price_tick(code: str, price: float, timestamp: str = None) -> list[dict]:
    """价格更新回调：供数据采集层调用。"""
    return monitor.on_price_update(code, price, timestamp)
