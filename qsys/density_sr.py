"""Density-SR 支撑/阻力扫描引擎（纯本地数据，零外部 API）。

四信号融合：
  1. 成交量分布（VPVR）：ATR 自适应分桶，日内 [low,high] 均分量权，高量节点合并成区间
  2. ATR 归一化：桶宽/距离全部用 ATR(14) 量纲，跨股票可比
  3. 极端波动率：近 20 日 |涨跌|>2σ 天数 + ATR 分位（惩罚"守不住"）
  4. 多周期共振：60/120/250 日三窗区间，中心距 <0.5 ATR 合并，共振数=命中窗口数

输出（规则化启发式评分，非真实概率，仅供参考）：
  - 触及概率 p_touch：反射原理近似 2·(1−Φ(d/√N))，d=现价到支撑上沿的 σ 距离
  - 守住概率 p_hold：区间量强度 + 共振 + 历史触碰守住率 − 极端波动惩罚
  - 机会分 score = p_touch × p_hold × 盈亏比（封顶 3），降序排名

数据：market_daily(source='ths_ifind') 全市场日线（5564 只，不复权——
成交量本就发生在真实成交价上，VPVR 语义正确）。
"""

import json
import math
from datetime import datetime
from statistics import NormalDist

import numpy as np
import pandas as pd

import datasource

_NORM = NormalDist()

# 调参常量（有意集中在顶部，便于审计调整）
ATR_N = 14                 # ATR 窗口
WINDOWS = (60, 120, 250)   # 多周期共振窗口（交易日）
BIN_ATR_FRAC = 0.5         # VPVR 桶宽 = 0.5 × ATR
ZONE_Q = 0.70              # 区间阈值：桶量 ≥ 全窗桶量 70 分位
MAX_ZONES_SIDE = 3         # 支撑/阻力各保留最强 N 档
MERGE_ATR = 0.5            # 跨窗共振合并：中心距阈值（ATR 倍数）
ZONE_MAX_HALF_ATR = 0.75   # 区间半宽上限（ATR 倍数）——防止平坦分布产生 4ATR 宽的伪区间
TOUCH_DAYS = 5             # 触及概率的展望天数
TOUCH_EDGE = 0.25          # 触碰判定缓冲（ATR 倍数）
HOLD_K = 5                 # 触碰后判定守住/失败的天数
FAIL_ATR = 0.5             # 跌破区间下沿该距离判失败（ATR 倍数）
EXTREME_DAYS = 20          # 极端波动统计窗口
EXTREME_SIGMA = 2.0        # |日收益| > 2σ 记一次极端波动
SCORE_RR_CAP = 3.0         # 机会分盈亏比封顶
MIN_AMOUNT_AVG = 3e7       # 流动性门槛：近 20 日均成交额 <3000 万跳过

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sr_scan_daily(
    date TEXT NOT NULL, code TEXT NOT NULL,
    close REAL, atr REAL,
    sup_lo REAL, sup_hi REAL, sup_strength REAL, sup_dist_atr REAL,
    res_lo REAL, res_hi REAL, res_dist_atr REAL,
    p_touch REAL, p_hold REAL, vol_extreme REAL, resonance INTEGER,
    score REAL, zones_json TEXT, computed_at TEXT,
    PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS idx_sr_scan_score ON sr_scan_daily(date, score DESC);
"""


# ---------------------------------------------------------------- 数据加载
def load_bars(code: str, days: int = 400) -> pd.DataFrame:
    """读单票日线（market_daily, ths_ifind），按日期升序。"""
    with datasource._conn() as c:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume, amount FROM market_daily "
            "WHERE source='ths_ifind' AND code=? ORDER BY date DESC LIMIT ?",
            c, params=(code, days))
    return df.iloc[::-1].reset_index(drop=True)


def _load_all_bars(days: int = 400) -> dict[str, pd.DataFrame]:
    """一次 SQL 读全市场窗口日线，按 code 分组成 dict（比逐票查快两个量级）。"""
    since = (datetime.now() - pd.Timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
    with datasource._conn() as c:
        df = pd.read_sql_query(
            "SELECT code, date, open, high, low, close, volume, amount FROM market_daily "
            "WHERE source='ths_ifind' AND date>=?", c, params=(since,))
    return {code: g.sort_values("date").reset_index(drop=True)
            for code, g in df.groupby("code")}


# ---------------------------------------------------------------- 基础指标
def calc_atr(df: pd.DataFrame, n: int = ATR_N) -> pd.Series:
    """Wilder ATR。"""
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def extreme_vol_count(df: pd.DataFrame, atr: pd.Series, days: int = EXTREME_DAYS) -> float:
    """近 days 日内 |日收益| > EXTREME_SIGMA×σ 的天数（σ 用 ATR/close 近似）。"""
    if len(df) < days + 2:
        return 0.0
    tail = df.iloc[-(days + 1):]
    sigma = (atr.iloc[-(days + 1):] / tail["close"]).replace(0, np.nan)
    ret = tail["close"].pct_change().abs()
    return float((ret > EXTREME_SIGMA * sigma).sum())


# ---------------------------------------------------------------- VPVR 区间
def volume_profile(df: pd.DataFrame, window: int, atr_val: float) -> tuple[np.ndarray, np.ndarray]:
    """窗口内成交量分布：桶宽 = BIN_ATR_FRAC×ATR；日内 [low,high] 均分量权。
    返回 (桶中心价数组, 桶量数组)。"""
    w = df.iloc[-window:] if len(df) > window else df
    lo_p, hi_p = float(w["low"].min()), float(w["high"].max())
    bw = max(atr_val * BIN_ATR_FRAC, hi_p * 1e-4)  # 桶宽下限防 0
    if not math.isfinite(bw) or bw <= 0 or hi_p <= lo_p:
        return np.array([]), np.array([])
    n_bins = int((hi_p - lo_p) / bw) + 1
    if n_bins < 3 or n_bins > 400:
        return np.array([]), np.array([])
    vols = np.zeros(n_bins)
    lows = ((w["low"].to_numpy() - lo_p) / bw).astype(int)
    highs = np.minimum(((w["high"].to_numpy() - lo_p) / bw).astype(int), n_bins - 1)
    v = w["volume"].to_numpy(dtype=float)
    for i in range(len(w)):
        b0, b1 = lows[i], max(highs[i], lows[i])
        vols[b0:b1 + 1] += v[i] / (b1 - b0 + 1)
    centers = lo_p + (np.arange(n_bins) + 0.5) * bw
    # 3 桶滑动平均平滑
    vols = np.convolve(vols, np.ones(3) / 3, mode="same")
    return centers, vols


def zones_from_profile(centers: np.ndarray, vols: np.ndarray, atr_val: float) -> list[dict]:
    """把 VPVR 直方图切成区间：连续桶量 ≥ ZONE_Q 分位 的一段为一个区间，
    并从峰值桶向两侧收缩到半宽 ≤ ZONE_MAX_HALF_ATR×ATR（防平坦分布出宽区间）。
    返回 [{lo, hi, center, strength(区间量占比)}]，按强度降序。"""
    if len(centers) == 0 or vols.sum() <= 0:
        return []
    thr = np.quantile(vols, ZONE_Q)
    if thr <= 0:
        return []
    zones = []
    bw = centers[1] - centers[0] if len(centers) > 1 else 0
    max_half = ZONE_MAX_HALF_ATR * atr_val
    i = 0
    while i < len(centers):
        if vols[i] >= thr:
            j = i
            while j + 1 < len(centers) and vols[j + 1] >= thr:
                j += 1
            # 从峰值桶向两侧扩展，限制半宽
            pk = i + int(np.argmax(vols[i:j + 1]))
            l, r = pk, pk
            while l - 1 >= i and centers[pk] - centers[l - 1] <= max_half:
                l -= 1
            while r + 1 <= j and centers[r + 1] - centers[pk] <= max_half:
                r += 1
            seg_vol = float(vols[i:j + 1].sum())  # 强度仍记整段量（密度信息在中心位置）
            zones.append({
                "lo": round(float(centers[l] - bw / 2), 3),
                "hi": round(float(centers[r] + bw / 2), 3),
                "center": float(centers[pk]),
                "strength": seg_vol / float(vols.sum()),
            })
            i = j + 1
        else:
            i += 1
    zones.sort(key=lambda z: -z["strength"])
    return zones


def merge_resonance(zones_by_window: dict[int, list[dict]], atr_val: float) -> list[dict]:
    """跨窗口区间共振合并：中心距 < MERGE_ATR×ATR 聚成一簇。
    返回 [{lo, hi, center, strength(加权), resonance(命中窗口数)}]，强度降序。"""
    items = []  # (center, lo, hi, strength, window)
    for w, zs in zones_by_window.items():
        for z in zs:
            items.append((z["center"], z["lo"], z["hi"], z["strength"], w))
    if not items:
        return []
    items.sort(key=lambda x: x[0])
    thr = max(atr_val * MERGE_ATR, 1e-9)
    clusters, cur = [], [items[0]]
    for it in items[1:]:
        if abs(it[0] - np.average([c[0] for c in cur])) <= thr:
            cur.append(it)
        else:
            clusters.append(cur)
            cur = [it]
    clusters.append(cur)
    merged = []
    for cl in clusters:
        sw = sum(c[3] for c in cl)
        center = float(np.average([c[0] for c in cl], weights=[c[3] for c in cl]))
        half = ZONE_MAX_HALF_ATR * atr_val
        merged.append({
            # 簇边界取并集后按半宽上限裁剪（跨窗 union 可能超宽）
            "lo": round(max(min(c[1] for c in cl), center - half), 3),
            "hi": round(min(max(c[2] for c in cl), center + half), 3),
            "center": round(center, 3),
            "strength": round(sw / len(cl), 4),
            "resonance": len({c[4] for c in cl}),
        })
    merged.sort(key=lambda z: -z["strength"] * z["resonance"])
    return merged


# ---------------------------------------------------------------- 概率模型
def calc_p_touch(dist_atr: float, atr_val: float, close: float, days: int = TOUCH_DAYS) -> float:
    """触及概率：反射原理近似 P ≈ 2·(1−Φ(d/√N))。
    d 即 dist_atr——σ_daily ≈ ATR/close，所以 ATR 量纲距离就是 σ 量纲距离。"""
    if close <= 0 or atr_val <= 0:
        return 0.0
    return round(min(0.999, max(0.0, 2 * (1 - _NORM.cdf(dist_atr / math.sqrt(days))))), 3)


def touch_hold_rate(df: pd.DataFrame, zone: dict, atr: pd.Series) -> tuple[float, int]:
    """历史触碰守住率：low 进入 [lo−0.25ATR, hi] 记一次触碰；
    随后 HOLD_K 日内 close<lo−FAIL_ATR×ATR = 失败，close>hi = 守住。
    返回 (守住率, 触碰次数)；无触碰返回 (0.5, 0) 中性。"""
    held = failed = 0
    atr_np = atr.to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    i = len(df) - 1  # 由近及远
    while i >= HOLD_K:
        a = atr_np[i]
        if np.isfinite(a) and lows[i] <= zone["hi"] and lows[i] >= zone["lo"] - TOUCH_EDGE * a:
            # 触碰成立，向前看结果
            outcome = None
            for k in range(1, HOLD_K + 1):
                if i + k >= len(df):
                    break
                if closes[i + k] < zone["lo"] - FAIL_ATR * a:
                    outcome = "fail"
                    break
                if closes[i + k] > zone["hi"]:
                    outcome = "held"
                    break
            if outcome == "held":
                held += 1
            elif outcome == "fail":
                failed += 1
            i -= HOLD_K  # 同一次触碰不重复计数
        else:
            i -= 1
    total = held + failed
    if total == 0:
        return 0.5, 0
    return held / total, total


def calc_p_hold(strength: float, resonance: int, hold_rate: float, extreme_cnt: float) -> float:
    """守住概率（0-1 信号强度）：区间量占比 + 共振 + 历史守住率 − 极端波动惩罚。
    权重刻意压低使正常组合不触顶（0.95 截断只挡极端值），保住排名区分度。"""
    raw = (0.42
           + 0.30 * min(strength, 0.5)          # 区间量占比（0~0.5 → 0~0.15）
           + 0.06 * (resonance - 1)             # 每多一个窗口共振 +6%
           + 0.25 * (hold_rate - 0.5)           # 历史守住率 ±12.5%
           - 0.05 * extreme_cnt)                # 近期极端波动每次 −5%
    return round(min(0.95, max(0.05, raw)), 3)


# ---------------------------------------------------------------- 单票分析
def analyze(code: str, df: pd.DataFrame) -> dict | None:
    """单票四信号融合分析。数据不足返回 None。"""
    if df is None or len(df) < min(WINDOWS):
        return None
    df = df.dropna(subset=["high", "low", "close", "volume"]).reset_index(drop=True)
    if len(df) < min(WINDOWS):
        return None
    atr = calc_atr(df)
    atr_val = float(atr.iloc[-1])
    close = float(df["close"].iloc[-1])
    if not (np.isfinite(atr_val) and atr_val > 0 and close > 0):
        return None

    zones_by_window = {w: zones_from_profile(*volume_profile(df, w, atr_val), atr_val)
                       for w in WINDOWS}
    merged = merge_resonance(zones_by_window, atr_val)
    if not merged:
        return None

    supports = [z for z in merged if z["center"] < close]
    resists = [z for z in merged if z["center"] >= close]
    sup = min(supports, key=lambda z: close - z["center"]) if supports else None
    res = min(resists, key=lambda z: z["center"] - close) if resists else None

    extreme_cnt = extreme_vol_count(df, atr)
    if sup:
        # 现价在区间内时距离按 0 计（正在触碰），不为负
        dist_atr = max(close - sup["hi"], 0.0) / atr_val
        p_touch = calc_p_touch(dist_atr, atr_val, close)
        hold_rate, touches = touch_hold_rate(df, sup, atr)
        p_hold = calc_p_hold(sup["strength"], sup["resonance"], hold_rate, extreme_cnt)
    else:
        dist_atr, p_touch, p_hold, touches = None, None, None, 0

    res_dist_atr = max(res["lo"] - close, 0.0) / atr_val if res else None
    # 盈亏比：到阻力距离 / 到支撑距离（支撑缺失时用 2×ATR 兜底）
    if res and res_dist_atr is not None:
        risk = dist_atr if (dist_atr and dist_atr > 0.2) else 2.0
        rr = min(res_dist_atr / risk, SCORE_RR_CAP)
    else:
        rr = 1.0
    score = round((p_touch or 0) * (p_hold or 0) * rr, 4)

    return {
        "code": code, "close": round(close, 3), "atr": round(atr_val, 3),
        "sup_lo": sup["lo"] if sup else None, "sup_hi": sup["hi"] if sup else None,
        "sup_strength": round(sup["strength"], 4) if sup else None,
        "sup_dist_atr": round(dist_atr, 2) if dist_atr is not None else None,
        "res_lo": res["lo"] if res else None, "res_hi": res["hi"] if res else None,
        "res_dist_atr": round(res_dist_atr, 2) if res_dist_atr is not None else None,
        "p_touch": p_touch, "p_hold": p_hold,
        "vol_extreme": extreme_cnt, "resonance": sup["resonance"] if sup else 0,
        "score": score,
        "zones_json": json.dumps({"support": supports[:MAX_ZONES_SIDE],
                                  "resistance": resists[:MAX_ZONES_SIDE],
                                  "touches": touches}, ensure_ascii=False),
    }


# ---------------------------------------------------------------- 全市场扫描
def scan_and_store(trade_date: str | None = None, min_amount: float = MIN_AMOUNT_AVG,
                   codes: list[str] | None = None,
                   progress=None) -> int:
    """全市场扫描并落库 sr_scan_daily（幂等：同日同 code 覆盖）。
    trade_date 默认最新交易日；返回写入行数。"""
    bars = _load_all_bars(days=int(max(WINDOWS) * 1.6))
    if codes:
        bars = {c: b for c, b in bars.items() if c in set(codes)}
    if not bars:
        return 0
    if trade_date is None:
        trade_date = max(b["date"].iloc[-1] for b in bars.values() if len(b))

    # 流动性过滤：近 20 日均成交额
    liquid = {}
    for code, b in bars.items():
        amt = b["amount"].iloc[-20:].mean() if len(b) >= 20 else b["amount"].mean()
        if amt and amt >= min_amount:
            liquid[code] = b

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    total = len(liquid)
    for i, (code, b) in enumerate(sorted(liquid.items())):
        r = analyze(code, b)
        if r:
            rows.append((trade_date, code, r["close"], r["atr"],
                         r["sup_lo"], r["sup_hi"], r["sup_strength"], r["sup_dist_atr"],
                         r["res_lo"], r["res_hi"], r["res_dist_atr"],
                         r["p_touch"], r["p_hold"], r["vol_extreme"], r["resonance"],
                         r["score"], r["zones_json"], now))
        if progress and i % 500 == 0:
            progress(i, total)
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        c.execute("DELETE FROM sr_scan_daily WHERE date=?", (trade_date,))
        c.executemany(
            "INSERT OR REPLACE INTO sr_scan_daily (date, code, close, atr, sup_lo, sup_hi,"
            " sup_strength, sup_dist_atr, res_lo, res_hi, res_dist_atr, p_touch, p_hold,"
            " vol_extreme, resonance, score, zones_json, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_scan(date: str | None = None) -> tuple[pd.DataFrame, str | None]:
    """读某次扫描结果（默认最新），带股票名称。返回 (df, date)。"""
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        if date is None:
            row = c.execute("SELECT MAX(date) FROM sr_scan_daily").fetchone()
            date = row[0] if row else None
        if not date:
            return pd.DataFrame(), None
        df = pd.read_sql_query("SELECT * FROM sr_scan_daily WHERE date=? ORDER BY score DESC",
                               c, params=(date,))
        if not df.empty:
            names = pd.read_sql_query("SELECT code, name FROM ifind_stocklist", c)
            df = df.merge(names, on="code", how="left")
    return df, date


def list_scan_dates(limit: int = 20) -> list[str]:
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        rows = c.execute("SELECT DISTINCT date FROM sr_scan_daily ORDER BY date DESC LIMIT ?",
                         (limit,)).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- 因子化（接入选股体系）
# SR 因子即每日扫描快照的截面列，名字注册进 signals.FACTOR_CATALOG["支撑阻力"]
SR_FACTOR_NAMES = {"sr_entry", "sr_hold", "sr_strength"}


def factor_series(name: str, codes: list[str], end: str,
                  lookback_days: int = 800) -> pd.Series:
    """SR 因子长表 Series[(datetime, instrument)]，接 factor_eval.get_factor_values。

    数据源 sr_scan_daily（每日盘后任务落库 + 历史回填），只取 date<=end（无未来信息）。
    - sr_hold     : 守住概率（0-1）
    - sr_strength : 支撑强度×共振窗数
    - sr_entry    : 逐日截面合成 z(p_hold)+z(强度×共振)−z(距离ATR)
    """
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y-%m-%d")
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        df = pd.read_sql_query(
            "SELECT date, code, p_hold, sup_strength, resonance, sup_dist_atr "
            "FROM sr_scan_daily WHERE date>=? AND date<=?", c, params=(start, end))
    if df.empty:
        return pd.Series(dtype=float)
    if codes:
        df = df[df["code"].isin(set(codes))]
    if df.empty:
        return pd.Series(dtype=float)

    df["strength_res"] = df["sup_strength"].fillna(0) * df["resonance"].fillna(1)
    if name == "sr_hold":
        val = df["p_hold"]
    elif name == "sr_strength":
        val = df["strength_res"]
    else:  # sr_entry：逐日截面 zscore 合成
        def _z(s):
            sd = s.std()
            return (s - s.mean()) / sd if sd and sd > 0 else s * 0
        df["_entry"] = (df.groupby("date")["p_hold"].transform(_z)
                        + df.groupby("date")["strength_res"].transform(_z)
                        - df.groupby("date")["sup_dist_atr"].transform(_z))
        val = df["_entry"]

    s = pd.Series(val.to_numpy(), index=pd.MultiIndex.from_arrays(
        [df["date"], df["code"]], names=["datetime", "instrument"]),
        name=name).dropna()
    return s


def latest_sr_map(codes: list[str] | None = None,
                  asof: str | None = None) -> pd.DataFrame:
    """最新一次扫描（或 asof 前最近一次）的逐股快照——供 apply_filters 用。
    返回以 code 为索引的 DataFrame（p_hold/sup_dist_atr/resonance/...）。"""
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        if asof:
            row = c.execute("SELECT MAX(date) FROM sr_scan_daily WHERE date<=?", (asof,)).fetchone()
        else:
            row = c.execute("SELECT MAX(date) FROM sr_scan_daily").fetchone()
        date = row[0] if row else None
        if not date:
            return pd.DataFrame()
        df = pd.read_sql_query("SELECT * FROM sr_scan_daily WHERE date=?", c, params=(date,))
    if codes:
        df = df[df["code"].isin(set(codes))]
    return df.set_index("code")


def backfill(days: int = 120, min_amount: float = MIN_AMOUNT_AVG,
             progress=None) -> int:
    """历史回填：对最近 days 个交易日逐日重算（analyze 只用 ≤当日 K 线，point-in-time 安全）。
    用于评分卡立刻获得 IC/胜率历史；日常增量由 sr_scan 任务每日追加。返回写入行数。"""
    bars = _load_all_bars(days=int(max(WINDOWS) * 1.6 + days * 1.1))
    if not bars:
        return 0
    # 交易日历：取全市场出现过的最近 days 个交易日
    all_dates = sorted({d for b in bars.values() for d in b["date"]})
    trade_days = all_dates[-days:]
    day_set = set(trade_days)

    # 流动性过滤（用全窗均额近似）
    liquid = {c: b for c, b in bars.items()
              if len(b) >= min(WINDOWS) + days // 2
              and (b["amount"].iloc[-20:].mean() or 0) >= min_amount}

    total_rows = 0
    with datasource._conn() as c:
        c.executescript(_SCHEMA)
        for di, d in enumerate(trade_days):
            rows = []
            for code, b in liquid.items():
                sl = b[b["date"] <= d]
                if len(sl) < min(WINDOWS):
                    continue
                r = analyze(code, sl)
                if r:
                    rows.append((d, code, r["close"], r["atr"],
                                 r["sup_lo"], r["sup_hi"], r["sup_strength"], r["sup_dist_atr"],
                                 r["res_lo"], r["res_hi"], r["res_dist_atr"],
                                 r["p_touch"], r["p_hold"], r["vol_extreme"], r["resonance"],
                                 r["score"], r["zones_json"], f"backfill"))
            c.executemany(
                "INSERT OR REPLACE INTO sr_scan_daily (date, code, close, atr, sup_lo, sup_hi,"
                " sup_strength, sup_dist_atr, res_lo, res_hi, res_dist_atr, p_touch, p_hold,"
                " vol_extreme, resonance, score, zones_json, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            c.commit()  # 逐日提交：释放写锁，别卡住 market.db 上的其它写者（回填一次数分钟）
            total_rows += len(rows)
            if progress:
                progress(di + 1, len(trade_days), len(rows))
    return total_rows
