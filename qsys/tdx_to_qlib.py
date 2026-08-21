"""easy-tdx → Qlib 数据转换器（阶段1：抓取落 CSV）。

qlib 不能直接读 TDX 数据，转换链路（两阶段）：
  阶段1（本脚本，qsys 容器）：easy-tdx 日线 → 每股 CSV（/data/tdx_csv/）
  阶段2（local_qlib 容器）：qlib/scripts/dump_bin.py → 标准 provider 目录

用法：
  python /app/tdx_to_qlib.py --codes SH600519 SZ000001
  python /app/tdx_to_qlib.py --pool csi300
宿主一键：./scripts/convert_tdx_qlib.sh --pool csi300
"""

import argparse
import time
from pathlib import Path

import pandas as pd

import datasource as ds

CSV_DIR = Path("/data/tdx_csv")


def fetch_to_csv(code: str, out_dir: Path, start: str = "2005-01-01") -> bool:
    """单票日线 → CSV（dump_bin 期望格式：date,open,high,low,close,volume,amount,factor）。"""
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    ds._tdx_fetch_daily(code, start, end)
    with ds._conn() as c:
        df = pd.read_sql("SELECT date, open, high, low, close, volume, amount FROM market_daily"
                         " WHERE source='easytdx' AND code=? ORDER BY date", c, params=(code,))
    if df.empty:
        return False
    df["factor"] = 1.0
    df.to_csv(out_dir / f"{code}.csv", index=False)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--pool", default=None, help="csi300/csi500/all")
    ap.add_argument("--start", default="2005-01-01")
    args = ap.parse_args()

    if args.codes:
        codes = args.codes
    elif args.pool:
        from common import get_instruments
        codes = get_instruments(args.pool)
    else:
        ap.error("需要 --codes 或 --pool")

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[阶段1] 抓取 {len(codes)} 只 → CSV {CSV_DIR}", flush=True)
    ok = 0
    for i, code in enumerate(codes):
        try:
            if fetch_to_csv(code, CSV_DIR, args.start):
                ok += 1
            if (i + 1) % 100 == 0:
                print(f"  已抓 {i + 1}/{len(codes)}（成功 {ok}）", flush=True)
        except Exception as e:
            print(f"  {code} 失败: {e}", flush=True)
        time.sleep(0.05)
    print(f"[阶段1完成] {ok}/{len(codes)} → {CSV_DIR}，继续阶段2（宿主机执行 ./scripts/convert_tdx_qlib.sh 的 dump 段）", flush=True)


if __name__ == "__main__":
    main()

