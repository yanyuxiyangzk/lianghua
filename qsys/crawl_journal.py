"""爬取事件日志：每次数据抓取消作一行（什么时候、抓了什么、多少行、成败、耗时）。

datasource 的抓取函数与调度任务在关键点调用 record()；监控页（数据采集中心）
的"今日爬取时间线"从这里读。写入失败静默——日志永远不能拖垮抓取主流程。
"""
import time
from datetime import datetime


def _conn():
    import datasource
    return datasource._conn()


def setup():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS crawl_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,            -- 结束时间
            source TEXT NOT NULL,        -- 调用方（datasource.函数名 / scheduler.任务键 / bulk 等）
            action TEXT NOT NULL,        -- 动作（daily_fetch / minute_fetch / realtime_poll / ...）
            target TEXT,                 -- 对象（股票代码 / 表 / 日期区间）
            rows INTEGER,                -- 写入行数
            status TEXT NOT NULL,        -- ok / failed
            detail TEXT,                 -- 备注（错误摘要等）
            duration_sec REAL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_crawl_ts ON crawl_events(ts)")


def record(source: str, action: str, target: str = "", rows: int | None = None,
           status: str = "ok", detail: str = "", duration_sec: float | None = None):
    """写一条爬取事件；任何异常静默吞掉。"""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO crawl_events(ts,source,action,target,rows,status,detail,duration_sec)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source, action, target,
                 rows, status, detail[:300], duration_sec))
    except Exception:
        pass


class timed:
    """上下文管理器：计时 + 结束自动 record。用法：

        with timed("datasource._ths_fetch_daily", "daily_fetch", code) as t:
            n = ...抓取...
            t.rows = n            # 成功行数
        # 异常时自动 status=failed + detail=异常摘要
    """

    def __init__(self, source: str, action: str, target: str = ""):
        self.source, self.action, self.target = source, action, target
        self.rows = None
        self.detail = ""
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        record(self.source, self.action, self.target,
               rows=self.rows if exc_type is None else None,
               status="failed" if exc_type else "ok",
               detail=(f"{exc_type.__name__}: {exc}" if exc_type else self.detail),
               duration_sec=round(time.time() - self._t0, 2))
        return False  # 不吞异常
