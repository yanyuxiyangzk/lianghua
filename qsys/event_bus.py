"""线程安全的事件总线：LoopEngine → SSE → 前端。

架构：
  - Ring buffer 保留最近 200 条事件（内存可控）
  - 新订阅者只收订阅后的事件
  - 线程安全：queue.Queue + threading.Lock
"""

import json
import threading
import time
from collections import deque
from datetime import datetime
from enum import Enum
from queue import Empty, Queue


class EventType(str, Enum):
    ROUND_START = "round_start"
    STEP_UPDATE = "step_update"
    CANDIDATE_GEN = "candidate_gen"
    REVIEW_RESULT = "review_result"
    LLM_RESULT = "llm_result"
    GATE_EVAL = "gate_eval"
    GATE_PASS = "gate_pass"
    ROUND_COMPLETE = "round_complete"


class EventBus:
    """进程内事件总线（单例）。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_bus()
            return cls._instance

    def _init_bus(self):
        self._buffer = deque(maxlen=200)          # ring buffer
        self._subscribers: list[Queue] = []       # SSE 客户端队列
        self._sub_lock = threading.Lock()
        self._stats = {"total_events": 0, "subscribers": 0}

    def push(self, event_type: EventType, **data):
        """发布事件（线程安全，可从任意线程调用）。"""
        event = {
            "type": event_type.value,
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            **data,
        }
        self._buffer.append(event)
        self._stats["total_events"] += 1

        dead = []
        with self._sub_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
                self._stats["subscribers"] -= 1

    def subscribe(self) -> Queue:
        """创建一个新订阅者（返回 Queue）。"""
        q = Queue(maxsize=500)
        with self._sub_lock:
            self._subscribers.append(q)
            self._stats["subscribers"] += 1
        return q

    def unsubscribe(self, q: Queue):
        """取消订阅。"""
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
                self._stats["subscribers"] -= 1

    @property
    def stats(self):
        return {**self._stats, "buffer_size": len(self._buffer)}


# 全局单例
bus = EventBus()
