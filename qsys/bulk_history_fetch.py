"""全市场批量历史数据补齐（非 ST 股票）——替代逐股手动点击。

阶段说明：
- daily：补齐日线缺失/过期的股票（_ths_fetch_daily，单股单区间一次调用）
- minute：分钟线覆盖不足的股票逐只抓取一年（fetch_minute_period_to_db，
  THS_HF 单股约 7 秒；限速 pause 秒/只）。实测全市场约 11-13 小时，适合隔夜跑。
- all：先 daily 后 minute

特性：
- 断点续跑：进度写 bulk_fetch_progress 表，中断重跑自动跳过已完成
- iFinD 会话保护：遇 -9（会话超限）退避 60s 重试一次，仍失败则终止并明示
- 非 ST 过滤：按 ifind_stocklist 名称

用法（容器内）：
  python /app/bulk_history_fetch.py --mode minute                # 分钟线，最近一年
  python /app/bulk_history_fetch.py --mode daily                 # 只补日线
  python /app/bulk_history_fetch.py --mode all --days 365        # 两阶段
  python /app/bulk_history_fetch.py --mode minute --start-from SZ300750  # 从某票续跑
  python /app/bulk_history_fetch.py --status                     # 只看进度
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import datasource

PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_fetch_progress(
    mode TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT, updated_at TEXT,
    PRIMARY KEY(mode, code));
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _progress_conn():
    c = datasource._conn()
    c.executescript(PROGRESS_SCHEMA)
    return c


def non_st_codes() -> list[str]:
    """非 ST 股票清单（名称口径）。"""
    with datasource._conn() as c:
        rows = c.execute("SELECT code, name FROM ifind_stocklist").fetchall()
    return sorted(code for code, name in rows if "ST" not in str(name).upper())


def pending_daily(codes: list[str], start: str, end: str) -> list[str]:
    """日线缺失、覆盖不足或最新日期落后的股票（落后即补，停牌票重抓无害）。"""
    with datasource._conn() as c:
        latest = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT code, MAX(date), COUNT(*) FROM market_daily WHERE source='ths_ifind'"
            " AND date BETWEEN ? AND ? GROUP BY code", (start, end))}
    expected = len(datasource.expected_trade_days(start, end)) or 1
    out = []
    for code in codes:
        row = latest.get(code)
        if not row or not row[0]:
            out.append(code)  # 完全没有
        elif row[1] < expected * 0.9:
            out.append(code)  # 覆盖不足（补全区间）
        elif row[0] < end:
            out.append(code)  # 最新日期落后
    return out


def pending_minute(codes: list[str], start: str, end: str) -> list[str]:
    """分钟线完整度 <95% 的股票。"""
    out = []
    for code in codes:
        try:
            comp = datasource.minute_completeness(code, start, end)
            if comp["completeness"] < 0.95:
                out.append(code)
        except Exception:
            out.append(code)
    return out


def _mark(c, mode: str, code: str, status: str, detail: str = ""):
    c.execute("INSERT OR REPLACE INTO bulk_fetch_progress VALUES(?,?,?,?,?)",
              (mode, code, status, detail[:400], _now()))
    c.commit()


def _run(mode: str, codes: list[str], start: str, end: str, pause: float,
         fetch_one, label: str) -> dict:
    with _progress_conn() as c:
        done = {r[0] for r in c.execute(
            "SELECT code FROM bulk_fetch_progress WHERE mode=? AND status='done'", (mode,))}
    todo = [c0 for c0 in codes if c0 not in done]
    print(f"[{label}] 待处理 {len(todo)} / 总 {len(codes)}（已完成 {len(done)}）", flush=True)
    ok = fail = 0
    t0 = time.time()
    with _progress_conn() as c:
        for i, code in enumerate(todo, 1):
            try:
                detail = fetch_one(code)
                _mark(c, mode, code, "done", detail)
                ok += 1
            except Exception as exc:
                msg = str(exc)
                if "-9" in msg or "会话" in msg:
                    print(f"[{label}] iFinD 会话超限，60s 退避后重试一次 {code}…", flush=True)
                    time.sleep(60)
                    try:
                        detail = fetch_one(code)
                        _mark(c, mode, code, "done", detail)
                        ok += 1
                        continue
                    except Exception as exc2:
                        _mark(c, mode, code, "failed", f"会话退避后仍失败: {exc2}")
                        print(f"[{label}] ⛔ 会话持续异常，终止（已完成的可续跑）：{exc2}", flush=True)
                        return {"ok": ok, "failed": fail, "aborted": True}
                _mark(c, mode, code, "failed", msg)
                fail += 1
            if i % 25 == 0:
                rate = (time.time() - t0) / i
                eta = rate * (len(todo) - i) / 3600
                print(f"[{label}] {i}/{len(todo)} 完成 {ok} 失败 {fail} · "
                      f"单均 {rate:.1f}s · 预计剩余 {eta:.1f}h", flush=True)
            time.sleep(pause)
    return {"ok": ok, "failed": fail, "aborted": False}


def run_daily(start: str, end: str, pause: float = 0.3) -> dict:
    codes = non_st_codes()
    todo = pending_daily(codes, start, end)
    print(f"[日线] 非ST {len(codes)} 只 · 需补 {len(todo)} 只", flush=True)
    return _run("daily", todo, start, end, pause,
                lambda code: f"{datasource._ths_fetch_daily(code, start, end)} 行",
                "日线")


def run_minute(start: str, end: str, pause: float = 1.5, start_from: str = "") -> dict:
    codes = non_st_codes()
    todo = pending_minute(codes, start, end)
    if start_from and start_from in todo:
        todo = todo[todo.index(start_from):]
    print(f"[分钟线] 非ST {len(codes)} 只 · 覆盖不足 {len(todo)} 只", flush=True)

    def _one(code: str) -> str:
        r = datasource.fetch_minute_period_to_db(code, start, end)
        return f"{r['written']}行 {r['complete_days']}/{r['days']}天"

    return _run("minute", todo, start, end, pause, _one, "分钟线")


def show_status():
    with _progress_conn() as c:
        rows = c.execute(
            "SELECT mode, status, COUNT(*), MAX(updated_at) FROM bulk_fetch_progress"
            " GROUP BY mode, status").fetchall()
    if not rows:
        print("暂无进度记录")
        return
    for mode, status, n, ts in rows:
        print(f"{mode:8s} {status:8s} {n:5d} 只 · 最近更新 {ts}")


def main():
    ap = argparse.ArgumentParser(description="全市场批量历史补齐（非ST）")
    ap.add_argument("--mode", choices=["daily", "minute", "all"], default="minute")
    ap.add_argument("--days", type=int, default=365, help="补齐最近 N 天（默认 365）")
    ap.add_argument("--pause", type=float, default=1.5, help="每分钟线股间隔秒数（默认 1.5）")
    ap.add_argument("--start-from", default="", help="从某只股票续跑（分钟线）")
    ap.add_argument("--status", action="store_true", help="只看进度")
    args = ap.parse_args()
    if args.status:
        show_status()
        return
    # 结束日 = 最近已收盘交易日（不含今天：今天的日线由晚间同步任务处理，
    # 否则 4900 只股票的"最新日期 < 今天"会造成无谓的全量重抓）
    today = datetime.now().strftime("%Y-%m-%d")
    days = [d for d in datasource.expected_trade_days(
        (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"), today) if d < today]
    end = days[-1] if days else (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"补齐区间: {start} ~ {end}（非 ST）", flush=True)
    if args.mode in ("daily", "all"):
        print(run_daily(start, end), flush=True)
    if args.mode in ("minute", "all"):
        print(run_minute(start, end, args.pause, args.start_from), flush=True)


if __name__ == "__main__":
    main()
