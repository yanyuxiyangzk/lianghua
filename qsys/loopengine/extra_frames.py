"""LoopEngine 多类型因子帧构建器。

为每种因子类型（资金流/板块轮动/龙虎榜/盘口异动/指数/爆量抢筹）构建 datetime×instrument
帧，供 evaluate_tree 使用。每个 build_xxx_frames 函数返回 {field_name: DataFrame}。
"""

import numpy as np
import pandas as pd


def build_fundflow_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """个股资金流帧：从 stock_fundflow_daily 表构建（同花顺 iFinD 数据源）。

    字段：main_net_inflow（主力净流入额）, net_inflow_ratio（主力/散户净流比）,
          main_small_spread（主力-散户差）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                "SELECT code, date, main_net FROM stock_fundflow_daily WHERE date >= ? AND date <= ?",
                c, params=(start, end))
    except Exception:
        return {}
    if df.empty:
        return {}
    df = df.sort_values("date")
    # 透视为 datetime × instrument 宽表
    pivot = df.pivot_table(index="date", columns="code", values="main_net")
    frames = {}
    frames["main_net_inflow"] = pivot
    # 净流入占比
    total = pivot.abs().sum(axis=1)
    frames["net_inflow_ratio"] = pivot.div(total + 1e-6, axis=0)
    # 主力-散户差
    frames["main_small_spread"] = pivot - pivot.rolling(20, min_periods=5).mean()
    return frames


def build_sector_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """板块轮动帧：从 sector_flow_snapshots / sector_inflow_snapshots 构建。

    字段：sector_momentum（板块动量）, sector_net_flow（板块净流入）,
          sector_breadth（板块广度）, sector_rank（板块排名）, sector_amount_ratio（成交额占比）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            flow_df = pd.read_sql(
                """SELECT sector_name, DATE(ts) as date,
                          SUM(net_inflow) as net_flow
                   FROM sector_inflow_snapshots
                   WHERE ts >= ? AND ts <= ?
                   GROUP BY sector_name, date
                   ORDER BY date""",
                c, params=(start, end + " 23:59:59"))
    except Exception:
        return {}
    if flow_df.empty:
        return {}
    flow_df["date"] = pd.to_datetime(flow_df["date"])
    pivot_flow = flow_df.pivot_table(index="date", columns="sector_name", values="net_flow")
    frames = {}
    frames["sector_net_flow"] = pivot_flow
    frames["sector_momentum"] = pivot_flow.rolling(5, min_periods=2).mean()
    rank_df = pivot_flow.rank(axis=1, ascending=False)
    frames["sector_rank"] = rank_df
    frames["sector_breadth"] = (pivot_flow > 0).sum(axis=1) / pivot_flow.shape[1]
    total_flow = pivot_flow.abs().sum(axis=1)
    frames["sector_amount_ratio"] = pivot_flow.div(total_flow + 1e-6, axis=0)
    return frames


def build_lhb_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """龙虎榜帧：从 lhb_daily 表构建（同花顺 iFinD 数据源）。

    字段：lhb_net_buy（龙虎榜净买入）, lhb_inst_ratio（机构占比）,
          lhb_hot_count（上榜次数）, lhb_win_rate（次日胜率）, lhb_consecutive（连续上榜）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                "SELECT code, date as trade_date, net_buy, inst_buy_pct as inst_ratio FROM lhb_daily WHERE date >= ? AND date <= ?",
                c, params=(start, end))
    except Exception:
        return {}
    if df.empty:
        return {}
    df = df.sort_values("trade_date")
    frames = {}
    pivot_net = df.pivot_table(index="trade_date", columns="code", values="net_buy")
    frames["lhb_net_buy"] = pivot_net
    pivot_inst = df.pivot_table(index="trade_date", columns="code", values="inst_ratio")
    frames["lhb_inst_ratio"] = pivot_inst
    # 近20日上榜次数
    frames["lhb_hot_count"] = pivot_net.rolling(20, min_periods=1).count()
    # 次日胜率（简化：用净买入方向作为胜率代理）
    frames["lhb_win_rate"] = (pivot_net > 0).rolling(20, min_periods=5).mean()
    # 连续上榜
    frames["lhb_consecutive"] = pivot_net.rolling(5, min_periods=1).count()
    return frames


def build_tick_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """盘口异动帧：从 quote_snapshots 快照数据构建。

    字段：bid_ask_ratio（买卖比）, outer_inner_ratio（外内比）,
          quantity_ratio_dev（量比偏离）, tick_vol_ratio（逐笔量比）, bid_ask_spread（买卖价差）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                """SELECT code, DATE(ts) as date,
                          AVG(bid_vol_sum) as avg_bid_vol,
                          AVG(ask_vol_sum) as avg_ask_vol,
                          AVG(outer_vol) as avg_outer,
                          AVG(inner_vol) as avg_inner,
                          AVG(quantity_ratio) as avg_qr,
                          AVG(amount) as avg_amount,
                          AVG(turnover) as avg_turnover,
                          AVG(bid1) as avg_bid1,
                          AVG(ask1) as avg_ask1
                   FROM quote_snapshots
                   WHERE ts >= ? AND ts <= ? AND volume > 0
                   GROUP BY code, DATE(ts)
                   ORDER BY code, date""",
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
    pivot_tick = df.pivot_table(index="date", columns="code", values="avg_turnover")
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


def build_burst_frames(codes: list[str], end: str, lookback: int = 800) -> dict:
    """爆量抢筹因子帧：基于日内快照数据，识别主力吸筹行为。

    数据基础：quote_snapshots 日内快照（~700条/天/股），含 volume/bid_vol/outer_vol 增量。
    限制：无逐笔tick数据，无法做真正的"拆单检测"。以下字段基于可观测的盘口信号设计。

    字段：
    - vol_spike: 量比异动（日成交量/20日均量）
    - bid_pressure: 买盘压力（买一挂单量净增加/成交量，正=挂单等货，负=撤单）
    - outer_dominance: 外盘主导度（主动买入占比，中心化到0）
    - accumulation_composite: 吸筹综合信号（bid_pressure×0.5 + outer适中×0.3 + vol×0.2）
    """
    from datasource import _qconn

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback * 1.6))).strftime("%Y-%m-%d")
    try:
        with _qconn() as c:
            df = pd.read_sql(
                """SELECT code, ts, volume, bid_vol_sum, outer_vol, inner_vol
                   FROM quote_snapshots
                   WHERE ts >= ? AND ts <= ? AND volume > 0
                   ORDER BY code, ts""",
                c, params=(start, end + " 23:59:59"))
    except Exception:
        return {}
    if df.empty:
        return {}
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.date

    frames = {}
    daily_results = []

    for code in df["code"].unique():
        cdf = df[df["code"] == code].copy()
        if len(cdf) < 10:
            continue

        cdf["d_volume"] = cdf["volume"].diff().fillna(0)
        cdf["d_outer"] = cdf["outer_vol"].diff().fillna(0)
        cdf["d_inner"] = cdf["inner_vol"].diff().fillna(0)
        cdf["d_bid"] = cdf["bid_vol_sum"].diff().fillna(0)

        for date, grp in cdf.groupby("date"):
            day_vol = grp["d_volume"].sum()
            if day_vol <= 0:
                continue

            bid_inc = grp["d_bid"].sum()
            bid_pressure = (bid_inc / (day_vol + 1e-6)).clip(-1, 1)

            outer_inc = grp["d_outer"].clip(0).sum()
            inner_inc = grp["d_inner"].clip(0).sum()
            outer_ratio = outer_inc / (outer_inc + inner_inc + 1e-6)

            daily_results.append({
                "code": code,
                "date": pd.Timestamp(date),
                "vol": day_vol,
                "bid_pressure": bid_pressure,
                "outer_ratio": outer_ratio,
            })

    if not daily_results:
        return {}

    result_df = pd.DataFrame(daily_results)
    pivot_vol = result_df.pivot_table(index="date", columns="code", values="vol")
    pivot_bid = result_df.pivot_table(index="date", columns="code", values="bid_pressure")
    pivot_outer = result_df.pivot_table(index="date", columns="code", values="outer_ratio")

    if len(pivot_vol) >= 2:
        ma20 = pivot_vol.rolling(20, min_periods=2).mean()
        frames["vol_spike"] = pivot_vol / (ma20 + 1e-6)
    else:
        frames["vol_spike"] = pd.DataFrame(1.0, index=pivot_vol.index, columns=pivot_vol.columns)

    frames["bid_pressure"] = pivot_bid
    frames["outer_dominance"] = pivot_outer - 0.5

    bid_norm = (pivot_bid.clip(-1, 1) + 1) / 2
    outer_score = 1 - (frames["outer_dominance"].abs() / 0.5)
    vol_norm = frames["vol_spike"].clip(0, 3) / 3
    frames["accumulation_composite"] = bid_norm * 0.5 + outer_score * 0.3 + vol_norm * 0.2

    return frames


# 构建器注册表
BUILDERS = {
    "资金流": build_fundflow_frames,
    "板块轮动": build_sector_frames,
    "龙虎榜": build_lhb_frames,
    "盘口异动": build_tick_frames,
    "指数": build_index_frames,
    "爆量抢筹": build_burst_frames,
}


def build_extra_frames(factor_type: str, codes: list[str], end: str,
                       lookback: int = 800) -> dict:
    """统一入口：根据因子类型构建额外帧。"""
    builder = BUILDERS.get(factor_type)
    if builder:
        return builder(codes, end, lookback)
