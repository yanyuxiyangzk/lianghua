"""从 market.db 提取板块日线数据和股票行业映射，生成 HDF5 文件。

数据来源：宿主机 qsys/data/market.db（RD-Agent 容器通过挂载可见）

输出：
1. sector_daily.h5 - 板块日线面板 (datetime × sector_name)
2. stock_industry.h5 - 股票→行业映射表

数据结构：
- sector_daily.h5:
  Index: (datetime, sector_name)
  Columns: $avg_chg_pct, $total_amount, $flow_net, $up_count, $down_count, $members

- stock_industry.h5:
  Index: (instrument)
  Columns: $sector_name
"""
import sqlite3
from pathlib import Path

import pandas as pd

MARKET_DB = Path("/home/zk/code/lianghua/qsys/data/market.db")


def generate_sector_daily():
    """从 market.db 提取 sector_daily 表，生成 HDF5。"""
    if not MARKET_DB.exists():
        print(f"WARNING: {MARKET_DB} not found, skipping sector_daily generation")
        return False

    try:
        conn = sqlite3.connect(str(MARKET_DB))

        # 读取 sector_daily 表
        df = pd.read_sql(
            "SELECT date, sector_name, avg_chg_pct, total_amount, flow_net, "
            "up_count, down_count, members FROM sector_daily "
            "ORDER BY date, sector_name",
            conn,
        )
        conn.close()

        if df.empty:
            print("WARNING: sector_daily table is empty")
            return False

        # 转换日期格式
        df["date"] = pd.to_datetime(df["date"])

        # 为每个指标创建 pivot table
        indicators = ["avg_chg_pct", "total_amount", "flow_net", "up_count", "down_count", "members"]
        pivots = {}
        for ind in indicators:
            pivot = df.pivot_table(index="date", columns="sector_name", values=ind)
            pivots[f"${ind}"] = pivot

        # 将所有 pivot stack 成 (datetime, sector_name) MultiIndex Series
        stacked_series = []
        for field_name, pivot_df in pivots.items():
            stacked = pivot_df.stack()
            stacked.name = field_name
            stacked_series.append(stacked)

        # 拼接所有 Series 成 DataFrame
        result = pd.concat(stacked_series, axis=1)
        result.index.names = ["datetime", "sector_name"]
        result = result.sort_index()

        # 保存为 HDF5
        output_path = Path(__file__).parent / "sector_daily.h5"
        result.to_hdf(str(output_path), key="data")
        print(f"Generated {output_path}: {result.shape[0]} rows, {result.shape[1]} columns")
        return True

    except Exception as e:
        import traceback
        print(f"ERROR generating sector_daily: {e}")
        traceback.print_exc()
        return False


def generate_stock_industry():
    """从 market.db 提取 stock_industry 表，生成 HDF5。

    输出格式：
    - Index: instrument (如 SH600000, SZ000001)
    - Columns: $sector_name (行业名称)
    """
    if not MARKET_DB.exists():
        print(f"WARNING: {MARKET_DB} not found, skipping stock_industry generation")
        return False

    try:
        conn = sqlite3.connect(str(MARKET_DB))

        # 读取 stock_industry 表
        df = pd.read_sql(
            "SELECT code, sector_name FROM stock_industry ORDER BY code",
            conn,
        )
        conn.close()

        if df.empty:
            print("WARNING: stock_industry table is empty")
            return False

        # 设置索引为 instrument (code)
        df = df.set_index("code")
        df.index.name = "instrument"

        # 重命名列为带 $ 前缀
        df.columns = ["$sector_name"]

        # 保存为 HDF5
        output_path = Path(__file__).parent / "stock_industry.h5"
        df.to_hdf(str(output_path), key="data")
        print(f"Generated {output_path}: {df.shape[0]} rows, {df.shape[1]} columns")
        return True

    except Exception as e:
        import traceback
        print(f"ERROR generating stock_industry: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    generate_sector_daily()
    generate_stock_industry()
