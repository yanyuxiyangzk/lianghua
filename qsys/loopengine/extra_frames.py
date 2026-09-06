"""LoopEngine 多类型因子帧构建器。

为每种因子类型（资金流/板块轮动/龙虎榜/盘口异动/指数）构建 datetime×instrument
帧，供 evaluate_tree 使用。每个 build_xxx_frames 函数返回 {field_name: DataFrame}。
"""

import numpy as np
import pandas as pd


def build_fundflow_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """个股资金流帧：从 stock_fundflow_daily 表构建。

    字段：main_net_pct, super_net_pct, big_net_pct, small_net_pct,
          net_inflow_ratio（主力/散户净流比）, main_small_spread（主力-散户差）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                "SELECT code, date, main_pct, super_pct, big_pct, small_pct "
                "FROM stock_fundflow_daily WHERE date >= ? AND date <= ?",
                c, params=(start, end))
    except Exception:
        return {}
    if df.empty:
        return {}
    df = df.sort_values("date")
    # 透视为 datetime × instrument 宽表
    frames = {}
    for col, field in [("main_pct", "main_net_pct"), ("super_pct", "super_net_pct"),
                       ("big_pct", "big_net_pct"), ("small_pct", "small_net_pct")]:
        pivot = df.pivot_table(index="date", columns="code", values=col)
        pivot.index = pd.to_datetime(pivot.index)
        frames[field] = pivot
    # 派生字段
    if "main_net_pct" in frames and "small_net_pct" in frames:
        main = frames["main_net_pct"]
        small = frames["small_net_pct"]
        frames["net_inflow_ratio"] = main / (small.abs() + 1e-6) * np.sign(main)
        frames["main_small_spread"] = main - small
    return frames


def build_sector_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """板块轮动帧：从 sector_daily + stock_industry 构建个股维度的板块因子。

    字段：sector_momentum, sector_net_flow, sector_breadth, sector_rank, sector_amount_ratio
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            # 板块日线
            sector_df = pd.read_sql(
                "SELECT date, sector_name, avg_chg_pct, total_amount, flow_net, "
                " up_count, down_count, members FROM sector_daily WHERE date >= ? AND date <= ?",
                c, params=(start, end))
            # 个股→板块映射
            ind_df = pd.read_sql(
                "SELECT code, sector_name FROM stock_industry", c)
    except Exception:
        return {}
    if sector_df.empty or ind_df.empty:
        return {}
    sector_df = sector_df.sort_values("date")
    sector_df["date"] = pd.to_datetime(sector_df["date"])
    # 每只股票映射到其板块的因子值
    code_to_sector = dict(zip(ind_df["code"], ind_df["sector_name"]))
    frames = {}
    for field, col in [("sector_momentum", "avg_chg_pct"), ("sector_net_flow", "flow_net"),
                       ("sector_breadth", None), ("sector_amount_ratio", "total_amount")]:
        if col:
            pivot = sector_df.pivot_table(index="date", columns="sector_name", values=col)
        else:
            # breadth = up / (up + down)
            up = sector_df.pivot_table(index="date", columns="sector_name", values="up_count")
            down = sector_df.pivot_table(index="date", columns="sector_name", values="down_count")
            pivot = up / (up + down + 1e-6)
        pivot.index = pd.to_datetime(pivot.index)
        # 映射到个股维度
        instrument_map = {}
        for code in codes:
            sec = code_to_sector.get(code)
            if sec and sec in pivot.columns:
                instrument_map[code] = sec
        if instrument_map:
            result = pd.DataFrame(index=pivot.index, columns=codes, dtype=float)
            for code, sec in instrument_map.items():
                if sec in pivot.columns:
                    result[code] = pivot[sec]
            frames[field] = result
    # sector_rank: 每日板块排名百分位
    if "sector_momentum" in frames:
        sec_pivot = sector_df.pivot_table(index="date", columns="sector_name", values="avg_chg_pct")
        sec_rank = sec_pivot.rank(axis=1, pct=True)
        rank_frame = pd.DataFrame(index=sec_rank.index, columns=codes, dtype=float)
        for code in codes:
            sec = code_to_sector.get(code)
            if sec and sec in sec_rank.columns:
                rank_frame[code] = sec_rank[sec]
        frames["sector_rank"] = rank_frame
    return frames


def build_lhb_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """龙虎榜帧：从 lhb_daily 表构建。

    字段：lhb_net_buy, lhb_inst_ratio, lhb_hot_count, lhb_win_rate, lhb_consecutive
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                "SELECT code, date, net_buy, inst_count, hot_dept_count, "
                " win_rate, consecutive_days FROM lhb_daily WHERE date >= ? AND date <= ?",
                c, params=(start, end))
    except Exception:
        return {}
    if df.empty:
        return {}
    df = df.sort_values("date")
    df["date"] = pd.to_datetime(df["date"])
    frames = {}
    for field, col in [("lhb_net_buy", "net_buy"), ("lhb_inst_ratio", "inst_count"),
                       ("lhb_hot_count", "hot_dept_count"), ("lhb_win_rate", "win_rate"),
                       ("lhb_consecutive", "consecutive_days")]:
        pivot = df.pivot_table(index="date", columns="code", values=col)
        pivot.index = pd.to_datetime(pivot.index)
        frames[field] = pivot
    return frames


def build_tick_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """盘口异动帧：从 quote_snapshots 表按日聚合构建。

    字段：bid_ask_ratio, outer_inner_ratio, quantity_ratio_dev, tick_vol_ratio, bid_ask_spread
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                "SELECT code, DATE(trade_time) as date, "
                " AVG(bid_vol_sum) as avg_bid_vol, AVG(ask_vol_sum) as avg_ask_vol, "
                " AVG(outer_vol) as avg_outer, AVG(inner_vol) as avg_inner, "
                " AVG(quantity_ratio) as avg_qr, AVG(last_tick_vol) as avg_tick_vol, "
                " AVG(bid1) as avg_bid1, AVG(ask1) as avg_ask1 "
                "FROM quote_snapshots WHERE trade_time >= ? AND trade_time <= ? "
                " GROUP BY code, DATE(trade_time)",
                c, params=(start, end + " 23:59:59"))
    except Exception:
        return {}
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    frames = {}
    # bid_ask_ratio
    pivot_bid = df.pivot_table(index="date", columns="code", values="avg_bid_vol")
    pivot_ask = df.pivot_table(index="date", columns="code", values="avg_ask_vol")
    frames["bid_ask_ratio"] = pivot_bid / (pivot_ask + 1e-6)
    # outer_inner_ratio
    pivot_outer = df.pivot_table(index="date", columns="code", values="avg_outer")
    pivot_inner = df.pivot_table(index="date", columns="code", values="avg_inner")
    frames["outer_inner_ratio"] = pivot_outer / (pivot_inner + 1e-6)
    # quantity_ratio_dev
    pivot_qr = df.pivot_table(index="date", columns="code", values="avg_qr")
    frames["quantity_ratio_dev"] = pivot_qr - pivot_qr.rolling(20, min_periods=5).mean()
    # tick_vol_ratio
    pivot_tick = df.pivot_table(index="date", columns="code", values="avg_tick_vol")
    frames["tick_vol_ratio"] = pivot_tick / (pivot_tick.rolling(20, min_periods=5).mean() + 1e-6)
    # bid_ask_spread
    frames["bid_ask_spread"] = (pivot_ask - pivot_bid) / (pivot_bid + 1e-6)
    return frames


def build_index_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """指数因子帧：基于个股与宽基指数的关系构建。

    字段：idx_beta, idx_rs（相对强弱）, idx_vol_ratio, idx_corr, idx_alpha
    """
    import signals as sig
    from datasource import _qconn

    # 主要宽基指数
    idx_codes = ["000300.SH", "000905.SH", "000852.SH"]
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        # 获取指数面板
        idx_panel = sig.get_panel_cached(idx_codes, end, lookback, source="qlib_local")
        # 获取个股面板
        stock_panel = sig.get_panel_cached(codes, end, lookback, source="qlib_local")
    except Exception:
        return {}
    if idx_panel.empty or stock_panel.empty:
        return {}
    # 计算指数收益率
    idx_unstacked = idx_panel.unstack("instrument")
    idx_ret = idx_unstacked["$close"].pct_change()
    # 用沪深300作为主基准
    bench_ret = idx_ret.get("000300.SH")
    if bench_ret is None:
        return {}
    # 计算个股收益率
    stock_unstacked = stock_panel.unstack("instrument")
    stock_ret = stock_unstacked["$close"].pct_change()
    stock_vol = stock_ret.rolling(20, min_periods=5).std()
    bench_vol = bench_ret.rolling(20, min_periods=5).std()
    # 逐只股票计算因子
    frames = {f: pd.DataFrame(index=stock_ret.index, columns=codes, dtype=float)
              for f in ["idx_beta", "idx_rs", "idx_vol_ratio", "idx_corr", "idx_alpha"]}
    for code in codes:
        if code not in stock_ret.columns:
            continue
        sr = stock_ret[code]
        # beta = cov(r_i, r_b) / var(r_b)
        cov = sr.rolling(60, min_periods=20).cov(bench_ret)
        var = bench_ret.rolling(60, min_periods=20).var()
        frames["idx_beta"][code] = cov / (var + 1e-12)
        # relative strength
        cum_sr = (1 + sr).rolling(20, min_periods=5).apply(lambda x: x.prod(), raw=True)
        cum_br = (1 + bench_ret).rolling(20, min_periods=5).apply(lambda x: x.prod(), raw=True)
        frames["idx_rs"][code] = cum_sr / (cum_br + 1e-12)
        # vol ratio
        sv = stock_vol[code] if code in stock_vol.columns else pd.Series(dtype=float)
        frames["idx_vol_ratio"][code] = sv / (bench_vol + 1e-12)
        # correlation
        frames["idx_corr"][code] = sr.rolling(60, min_periods=20).corr(bench_ret)
        # alpha = stock_ret - beta * bench_ret (Jensen's alpha annualized)
        beta = frames["idx_beta"][code]
        frames["idx_alpha"][code] = (sr - beta * bench_ret).rolling(20, min_periods=5).mean() * 252
    return frames


# 构建器注册表
BUILDERS = {
    "资金流": build_fundflow_frames,
    "板块轮动": build_sector_frames,
    "龙虎榜": build_lhb_frames,
    "盘口异动": build_tick_frames,
    "指数": build_index_frames,
}


def build_extra_frames(factor_type: str, codes: list[str], end: str,
                       lookback: int = 800) -> dict:
    """统一入口：根据因子类型构建额外帧。"""
    builder = BUILDERS.get(factor_type)
    if builder:
        return builder(codes, end, lookback)
    return {}
