"""行情后台 Feed：抓取与渲染分离（根治刷新白屏）。

架构：
  - 后台线程按间隔批量抓取快照并写本地库（quote_snapshots）
  - 页面 fragment 只读库渲染（<100ms），不做任何网络请求
  - 类似 AJAX：数据在后台流动，页面只见结果不见过程
"""

import threading
import time
from datetime import datetime

import streamlit as st

import datasource


class QuoteFeed:
    def __init__(self):
        self._feeds = {}  # key -> {"stop": Event, "ts": str|None, "count": int}

    def ensure(self, key: str, codes: list[str], interval: int = 10):
        """确保某个池的后台采集线程在跑（幂等；间隔变化时自动重启）。"""
        fn = lambda: datasource.save_snapshots(datasource.get_batch_snapshots(codes))
        self._ensure_fn(key, fn, interval)

    def ensure_custom(self, key: str, fn, interval: int = 30):
        """通用后台周期任务（如板块行情轮询）。fn 无参、返回写入行数。"""
        self._ensure_fn(key, fn, interval)

    def _ensure_fn(self, key: str, fn, interval: int):
        if key in self._feeds:
            if self._feeds[key].get("interval") == interval:
                return
            self.stop(key)  # 间隔变了，重启线程
        stop = threading.Event()
        state = {"stop": stop, "ts": None, "count": 0, "err": None, "interval": interval}

        def loop():
            while not stop.is_set():
                try:
                    n = fn()
                    state["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    state["count"] = n
                    state["err"] = None
                except Exception as e:
                    state["err"] = str(e)[:100]
                stop.wait(interval)

        t = threading.Thread(target=loop, daemon=True, name=f"quotefeed-{key}")
        t.start()
        self._feeds[key] = state

    def stop(self, key: str):
        if key in self._feeds:
            self._feeds[key]["stop"].set()
            del self._feeds[key]

    def stop_all_except(self, keep_key: str | None = None):
        for key in list(self._feeds.keys()):
            if key != keep_key:
                self.stop(key)

    def status(self, key: str) -> dict | None:
        return self._feeds.get(key)

    def running(self, key: str) -> bool:
        return key in self._feeds


@st.cache_resource
def get_feed() -> QuoteFeed:
    return QuoteFeed()
