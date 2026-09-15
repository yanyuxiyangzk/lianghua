import sys; sys.path.insert(0, "/app")
import akshare as ak
import sqlite3
import pandas as pd
from datetime import datetime

DB = "/data/market.db"
from scheduler import all_pools
codes_raw = all_pools()["沪深300"]
codes = [c.replace("SH","").replace("SZ","") for c in codes_raw]

conn = sqlite3.connect(DB)
existing = set(r[0] for r in conn.execute("SELECT DISTINCT code FROM ifind_financial").fetchall())
todo = [c for c in codes if c not in existing]
print(f"Total: {len(codes)}, Existing: {len(existing)}, Todo: {len(todo)}", flush=True)

total = 0
for i, code in enumerate(todo):
    for stmt, sym in [("利润表", "利润表"), ("资产负债表", "资产负债表"), ("现金流量表", "现金流量表")]:
        try:
            df = ak.stock_financial_report_sina(stock=code, symbol=sym)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                rdate = str(row.get("报告日", ""))[:8]
                if len(rdate) == 8:
                    rdate = f"{rdate[:4]}-{rdate[4:6]}-{rdate[6:8]}"
                for col in df.columns:
                    if col in ("报告日", "数据源", "是否审计", "公告日期", "币种", "类型", "更新日期"):
                        continue
                    v = row.get(col)
                    if pd.notna(v):
                        try:
                            v = float(v)
                        except Exception:
                            continue
                        conn.execute(
                            "INSERT OR REPLACE INTO ifind_financial(code,report_date,statement_type,indicator,value,fetched_at) VALUES (?,?,?,?,?,?)",
                            (code, rdate, stmt, col, v, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        total += 1
        except Exception:
            pass
    if (i + 1) % 10 == 0:
        conn.commit()
        print(f"{i+1}/{len(todo)} done, +{total} rows, now {len(existing)+i+1} stocks", flush=True)

conn.commit()
conn.close()
print(f"DONE: +{total} rows, {len(existing)+len(todo)} total stocks")
