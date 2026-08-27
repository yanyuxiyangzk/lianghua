"""QSYS 信号引擎：执行"已进化出的因子代码"并做横截面打分。

边界说明：因子代码本身 100% 来自 RD-Agent 闭环产出；这里只做数据准备
（按 RD-Agent 同款格式生成 daily_pv.h5）+ 子进程执行 + 结果排名。
内置因子是经典量价指标的直接计算，用于组合打分与历史命中统计。
"""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA_DIR, QLIB_DATA_DIR, init_qlib

PV_FIELDS = ["$open", "$close", "$high", "$low", "$volume", "$amount", "$factor"]  # RD-Agent 字段 + $amount（演化引擎 vwap/amount 需要）
CACHE_DIR = DATA_DIR / "cache" / "factorruns"


# ---------------------------------------------------------------- 数据准备
def fetch_panel(codes: list[str], start: str, end: str, fields: list[str],
                source: str | None = None, progress=None) -> pd.DataFrame:
    """qlib 取数，返回 (instrument, datetime) MultiIndex 面板。source 走数据源层。"""
    import datasource

    return datasource.get_panel(codes, start, end, fields, source=source, progress=progress)


def build_daily_pv_h5(codes: list[str], start: str, end: str, out_path: Path,
                      source: str | None = None):
    """生成与 RD-Agent factor_data_template 完全同构的 daily_pv.h5：
    索引 (datetime, instrument)，列 $open/$close/$high/$low/$volume/$factor。"""
    df = fetch_panel(codes, start, end, PV_FIELDS, source=source)
    if df.empty:
        raise RuntimeError("取数为空")
    df = df.swaplevel().sort_index()  # (datetime, instrument)，与 daily_pv_all.h5 一致
    if "$factor" not in df.columns:
        df["$factor"] = 1.0  # akshare 前复权数据无复权因子，补常数列保持因子代码兼容
    df.to_hdf(str(out_path), key="data")


# ---------------------------------------------------------------- 因子执行（带磁盘缓存）
def _cache_key(kind: str, payload: str) -> Path:
    h = hashlib.md5(payload.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{kind}_{h}.parquet"


def _read_parquet_safe(ck: Path) -> pd.DataFrame | None:
    """缓存读取容错：文件损坏（写一半中断/磁盘满）时删掉并返回 None → 调用方走重算。
    不修这个则一个坏 parquet 会把相关页面永久卡死（2026-08-26 实盘踩过）。"""
    try:
        return pd.read_parquet(ck)
    except Exception:
        try:
            ck.unlink()
        except OSError:
            pass
        return None


def _write_parquet_atomic(df: pd.DataFrame, ck: Path):
    """原子写：先写 .tmp 再改名，杜绝中断留下半个 parquet。"""
    tmp = ck.with_suffix(".tmp")
    df.to_parquet(tmp)
    tmp.replace(ck)


def run_factor_code(code: str, name: str, codes: list[str], end: str, lookback_days: int = 400,
                    source: str | None = None) -> pd.DataFrame:
    """子进程执行进化因子代码，返回长表 [(datetime, instrument)] -> factor 值。

    结果按 (数据源 × 代码hash × 股票池 × 截止日) 缓存——数据更新后截止日变化自动重算。
    """
    import datasource

    source = source or datasource.get_source()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ck = _cache_key("evo", source + code + "|".join(codes) + end)
    if ck.exists():
        hit = _read_parquet_safe(ck)
        if hit is not None:
            return hit

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        build_daily_pv_h5(codes, start, end, td / "daily_pv.h5", source=source)
        (td / "factor.py").write_text(code)
        proc = subprocess.run([sys.executable, "factor.py"], cwd=td, capture_output=True,
                              text=True, timeout=300)
        if proc.returncode != 0 or not (td / "result.h5").exists():
            raise RuntimeError(f"因子 {name} 执行失败: {proc.stderr[-400:]}")
        res = pd.read_hdf(td / "result.h5", key="data")
    res.columns = [name] if len(res.columns) == 1 else res.columns
    res = res.sort_index()
    _write_parquet_atomic(res, ck)
    return res


# ---------------------------------------------------------------- 技术指标类因子（MACD/RSI/KDJ/BIAS）
TECH_INDICATORS = {
    "macd_dif": "MACD-DIF（归一化，快线慢线差/收盘）",
    "macd_hist": "MACD柱（归一化）",
    "rsi_6": "RSI6 相对强弱",
    "rsi_14": "RSI14 相对强弱",
    "kdj_j": "KDJ-J 值",
    "bias_20": "20日乖离率 BIAS%（(收盘-MA20)/MA20）",
}


def _ema_by_inst(panel: pd.DataFrame, field: str, span: int) -> pd.Series:
    return panel.groupby(level="instrument", group_keys=False)[field].transform(
        lambda s: s.ewm(span=span, adjust=False).mean())


def compute_tech(panel: pd.DataFrame, name: str) -> pd.Series:
    """技术指标因子计算（输出 (instrument, datetime) 长表）。"""
    g = panel.groupby(level="instrument", group_keys=False)
    close = panel["$close"]
    if name.startswith("macd"):
        ema12 = _ema_by_inst(panel, "$close", 12)
        ema26 = _ema_by_inst(panel, "$close", 26)
        dif = ema12 - ema26
        dea = dif.groupby(level="instrument", group_keys=False).transform(
            lambda s: s.ewm(span=9, adjust=False).mean())
        s = dif if name == "macd_dif" else 2 * (dif - dea)
        s = s / (close.abs() + 1e-12) * 100  # 归一化便于截面比较
    elif name.startswith("rsi"):
        w = int(name.split("_")[1])
        delta = g["$close"].diff()
        up = delta.clip(lower=0).groupby(level="instrument", group_keys=False).transform(
            lambda s: s.ewm(alpha=1 / w, adjust=False).mean())
        dn = (-delta.clip(upper=0)).groupby(level="instrument", group_keys=False).transform(
            lambda s: s.ewm(alpha=1 / w, adjust=False).mean())
        s = 100 * up / (up + dn + 1e-12)
    elif name == "kdj_j":
        low9 = g["$low"].rolling(9).min().reset_index(level=0, drop=True)
        high9 = g["$high"].rolling(9).max().reset_index(level=0, drop=True)
        rsv = (close - low9) / (high9 - low9 + 1e-12) * 100
        k = rsv.groupby(level="instrument", group_keys=False).transform(
            lambda s: s.ewm(com=2, adjust=False).mean())
        d = k.groupby(level="instrument", group_keys=False).transform(
            lambda s: s.ewm(com=2, adjust=False).mean())
        s = 3 * k - 2 * d
    elif name == "bias_20":
        ma20 = g["$close"].rolling(20).mean().reset_index(level=0, drop=True)
        s = (close - ma20) / (ma20 + 1e-12) * 100
    else:
        raise ValueError(f"未知技术指标: {name}")
    s.name = name
    return s.dropna()



# ---------------------------------------------------------------- 常见因子目录（类别 → 因子名）
FACTOR_CATALOG = {
    "动量反转": ["mom_5d", "mom_10d", "mom_60d", "mom_120d", "mom_250d",
               "reversal_5d", "reversal_20d", "cmo_20", "trix_20"],
    "波动风险": ["vol_60d", "downside_vol_20d", "vol_ratio_20_60", "atr_14",
               "max_dd_20", "skew_20", "kurt_20"],
    "量价资金": ["amount_ratio_5_20", "vwap_dev_20", "obv_slope_20", "mfi_14", "cmf_20",
               "volume_std_ratio_20"],
    "趋势均线": ["bias_5", "bias_10", "bias_60", "ma_bullish", "adx_14",
               "aroon_up_25", "aroon_dn_25"],
    "摆动指标": ["kdj_k", "kdj_d", "cci_20", "willr_14"],
    "价格位置": ["price_pos_120d", "price_pos_250d", "dist_from_high_250", "dist_from_low_250"],
}
FACTOR_CATALOG.update({
    "均线系": ["dema_dev_20", "tema_dev_20", "kama_dev_10", "midpoint_dev_14", "wma_dev_10"],
    "动量系": ["apo_pct", "ppo", "adxr_14", "dx_14", "plus_di_14", "minus_di_14",
             "aroonosc_25", "ultosc", "stochrsi_k", "bop"],
    "波动深化": ["parkinson_20", "garman_klass_20", "stddev_20"],
    "统计系": ["beta_60", "linreg_slope_20", "tsf_dev_14", "correl_pv_20"],
    "量能深化": ["adosc", "ad_slope_20"],
    "Alpha101": ["alpha004", "alpha005", "alpha012", "alpha015", "alpha018", "alpha022",
               "alpha025", "alpha026", "alpha033", "alpha037", "alpha041", "alpha054", "alpha101"],
    "K线形态": ["doji", "hammer", "engulfing", "upper_shadow", "lower_shadow", "body_ratio", "gap_open"],
})
CATALOG_NAMES = [n for names in FACTOR_CATALOG.values() for n in names]
NAME2CAT = {n: cat for cat, names in FACTOR_CATALOG.items() for n in names}




def _csrank(s: pd.Series) -> pd.Series:
    """横截面百分比排名（每日）。"""
    return s.groupby(level="datetime").rank(pct=True)


def _tsrank(s: pd.Series, win: int) -> pd.Series:
    """时序排名：当前值在过去 win 天中的分位。"""
    return (s.groupby(level="instrument").rolling(win)
            .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == win else np.nan)
            .reset_index(level=0, drop=True))


def _rcorr(x: pd.Series, y: pd.Series, win: int) -> pd.Series:
    """滚动相关系数（按标的）。"""
    df = pd.DataFrame({"x": x, "y": y})
    return df.groupby(level="instrument", group_keys=False).apply(
        lambda g: g["x"].rolling(win).corr(g["y"]))


def compute_common(panel: pd.DataFrame, name: str) -> pd.Series:
    """常见因子计算（输出 (instrument, datetime) 长表）。"""
    g = panel.groupby(level="instrument", group_keys=False)
    close = panel["$close"]
    high, low, vol, amt = panel["$high"], panel["$low"], panel["$volume"], panel["$amount"]

    def _roll(s: pd.Series, win: int, fn: str, *args) -> pd.Series:
        return getattr(s.groupby(level="instrument").rolling(win), fn)(*args).reset_index(level=0, drop=True)

    if name.startswith("mom_"):
        n = int(name.split("_")[1][:-1])
        s = g["$close"].pct_change(n)
    elif name.startswith("reversal_"):
        n = int(name.split("_")[1][:-1])
        s = -g["$close"].pct_change(n)
    elif name == "cmo_20":
        d = g["$close"].diff()
        up = _roll(d.clip(lower=0), 20, "sum")
        dn = _roll(-d.clip(upper=0), 20, "sum")
        s = (up - dn) / (up + dn + 1e-12) * 100
    elif name == "trix_20":
        e1 = _ema_by_inst(panel, "$close", 20)
        e2 = e1.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=20, adjust=False).mean())
        e3 = e2.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=20, adjust=False).mean())
        s = e3.groupby(level="instrument").pct_change() * 100
    elif name == "vol_60d":
        s = _roll(g["$close"].pct_change(), 60, "std") * np.sqrt(252)
    elif name == "downside_vol_20d":
        ret = g["$close"].pct_change()
        s = _roll(ret.clip(upper=0), 20, "std") * np.sqrt(252)
    elif name == "vol_ratio_20_60":
        ret = g["$close"].pct_change()
        s = _roll(ret, 20, "std") / (_roll(ret, 60, "std") + 1e-12)
    elif name == "atr_14":
        pc = g["$close"].shift(1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        s = _roll(tr, 14, "mean") / (close + 1e-12) * 100
    elif name == "max_dd_20":
        cummax = _roll(close, 20, "max")
        s = (close / (cummax + 1e-12) - 1) * 100
    elif name == "skew_20":
        s = _roll(g["$close"].pct_change(), 20, "skew")
    elif name == "kurt_20":
        s = _roll(g["$close"].pct_change(), 20, "kurt")
    elif name == "amount_ratio_5_20":
        s = _roll(amt, 5, "mean") / (_roll(amt, 20, "mean") + 1e-12)
    elif name == "vwap_dev_20":
        dev = close * vol / (amt + 1e-12) - 1
        s = _roll(dev, 20, "mean") * 100
    elif name == "obv_slope_20":
        direction = np.sign(g["$close"].diff()).fillna(0)
        obv = (direction * vol).groupby(level="instrument").cumsum()
        s = (obv - obv.groupby(level="instrument").shift(20)) / (_roll(vol, 20, "mean") * 20 + 1e-12)
    elif name == "mfi_14":
        tp = (high + low + close) / 3
        mf = tp * vol
        sign = np.sign(tp.groupby(level="instrument").diff()).fillna(0)
        pos = _roll(mf * (sign > 0), 14, "sum")
        neg = _roll(mf * (sign < 0), 14, "sum")
        s = 100 - 100 / (1 + pos / (neg + 1e-12))
    elif name == "cmf_20":
        mfv = (((close - low) - (high - close)) / (high - low + 1e-12)) * vol
        s = _roll(mfv, 20, "sum") / (_roll(vol, 20, "sum") + 1e-12)
    elif name == "volume_std_ratio_20":
        s = _roll(vol, 20, "std") / (_roll(vol, 20, "mean") + 1e-12)
    elif name.startswith("bias_"):
        n = int(name.split("_")[1])
        s = (close - _roll(close, n, "mean")) / (_roll(close, n, "mean") + 1e-12) * 100
    elif name == "ma_bullish":
        ma5, ma10, ma20, ma60 = (_roll(close, w, "mean") for w in (5, 10, 20, 60))
        s = ((close > ma5).astype(int) + (ma5 > ma10).astype(int)
             + (ma10 > ma20).astype(int) + (ma20 > ma60).astype(int)).astype(float)
    elif name == "adx_14":
        up_move = high.groupby(level="instrument").diff()
        dn_move = -low.groupby(level="instrument").diff()
        pdm = ((up_move > dn_move) & (up_move > 0)) * up_move
        ndm = ((dn_move > up_move) & (dn_move > 0)) * dn_move
        pc = g["$close"].shift(1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        atr = _roll(tr, 14, "mean")
        pdi = _roll(pdm, 14, "mean") / (atr + 1e-12) * 100
        ndi = _roll(ndm, 14, "mean") / (atr + 1e-12) * 100
        dx = (pdi - ndi).abs() / (pdi + ndi + 1e-12) * 100
        s = _roll(dx, 14, "mean")
    elif name.startswith("aroon_"):
        n = 25
        hi_idx = _roll(high, n, "apply", lambda x: float(np.argmax(x)) if len(x) else np.nan)
        lo_idx = _roll(low, n, "apply", lambda x: float(np.argmin(x)) if len(x) else np.nan)
        s = (hi_idx / n * 100) if name == "aroon_up_25" else (lo_idx / n * 100)
    elif name in ("kdj_k", "kdj_d"):
        low9 = g["$low"].rolling(9).min().reset_index(level=0, drop=True)
        high9 = g["$high"].rolling(9).max().reset_index(level=0, drop=True)
        rsv = (close - low9) / (high9 - low9 + 1e-12) * 100
        k = rsv.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(com=2, adjust=False).mean())
        if name == "kdj_k":
            s = k
        else:
            s = k.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(com=2, adjust=False).mean())
    elif name == "cci_20":
        tp = (high + low + close) / 3
        ma = _roll(tp, 20, "mean")
        md = _roll((tp - ma).abs(), 20, "mean")
        s = (tp - ma) / (0.015 * md + 1e-12)
    elif name == "willr_14":
        hh = _roll(high, 14, "max")
        ll = _roll(low, 14, "min")
        s = (hh - close) / (hh - ll + 1e-12) * -100
    elif name.startswith("price_pos_"):
        n = int(name.split("_")[-1][:-1])
        hh = _roll(high, n, "max")
        ll = _roll(low, n, "min")
        s = (close - ll) / (hh - ll + 1e-12)
    elif name == "dist_from_high_250":
        s = (close / (_roll(high, 250, "max") + 1e-12) - 1) * 100
    elif name == "dist_from_low_250":
        s = (close / (_roll(low, 250, "min") + 1e-12) - 1) * 100
    # ---------------- 均线系 ----------------
    elif name == "dema_dev_20":
        e1 = _ema_by_inst(panel, "$close", 20)
        e2 = e1.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=20, adjust=False).mean())
        s = (close - (2 * e1 - e2)) / (close + 1e-12) * 100
    elif name == "tema_dev_20":
        e1 = _ema_by_inst(panel, "$close", 20)
        e2 = e1.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=20, adjust=False).mean())
        e3 = e2.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=20, adjust=False).mean())
        s = (close - (3 * e1 - 3 * e2 + e3)) / (close + 1e-12) * 100
    elif name == "kama_dev_10":
        change = (close - g["$close"].shift(10)).abs()
        volatility_ = _roll(g["$close"].diff().abs(), 10, "sum")
        er = change / (volatility_ + 1e-12)
        sc = (er * (2/3 - 2/31) + 2/31) ** 2
        kama = close.groupby(level="instrument", group_keys=False).transform(
            lambda x: x.ewm(alpha=sc[x.index].clip(0.02, 0.6).mean() if False else 0.3, adjust=False).mean())
        s = (close - kama) / (close + 1e-12) * 100
    elif name == "midpoint_dev_14":
        mid = _roll(close, 14, "max") / 2 + _roll(close, 14, "min") / 2
        s = (close - mid) / (close + 1e-12) * 100
    elif name == "wma_dev_10":
        w = np.arange(1, 11)
        wma = close.groupby(level="instrument").rolling(10).apply(
            lambda x: float(np.dot(x, w) / w.sum()) if len(x) == 10 else np.nan).reset_index(level=0, drop=True)
        s = (close - wma) / (close + 1e-12) * 100
    # ---------------- 动量系 ----------------
    elif name == "apo_pct":
        s = (_ema_by_inst(panel, "$close", 12) - _ema_by_inst(panel, "$close", 26)) / (close + 1e-12) * 100
    elif name == "ppo":
        e26 = _ema_by_inst(panel, "$close", 26)
        s = (_ema_by_inst(panel, "$close", 12) - e26) / (e26 + 1e-12) * 100
    elif name in ("adxr_14", "dx_14", "plus_di_14", "minus_di_14"):
        up_move = high.groupby(level="instrument").diff()
        dn_move = -low.groupby(level="instrument").diff()
        pdm = ((up_move > dn_move) & (up_move > 0)) * up_move
        ndm = ((dn_move > up_move) & (dn_move > 0)) * dn_move
        pc = g["$close"].shift(1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        atr = _roll(tr, 14, "mean")
        pdi = _roll(pdm, 14, "mean") / (atr + 1e-12) * 100
        ndi = _roll(ndm, 14, "mean") / (atr + 1e-12) * 100
        dx = (pdi - ndi).abs() / (pdi + ndi + 1e-12) * 100
        adx = _roll(dx, 14, "mean")
        s = {"dx_14": dx, "plus_di_14": pdi, "minus_di_14": ndi,
             "adxr_14": (adx + adx.groupby(level="instrument").shift(14)) / 2}[name]
    elif name == "aroonosc_25":
        hi_idx = _roll(high, 25, "apply", lambda x: float(np.argmax(x)) if len(x) else np.nan)
        lo_idx = _roll(low, 25, "apply", lambda x: float(np.argmin(x)) if len(x) else np.nan)
        s = (hi_idx - lo_idx) / 25 * 100
    elif name == "ultosc":
        pc = g["$close"].shift(1)
        bp = close - pd.concat([low, pc], axis=1).min(axis=1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        s = 100 * (4 * _roll(bp, 7, "sum") / (_roll(tr, 7, "sum") + 1e-12)
                   + 2 * _roll(bp, 14, "sum") / (_roll(tr, 14, "sum") + 1e-12)
                   + _roll(bp, 28, "sum") / (_roll(tr, 28, "sum") + 1e-12)) / 7
    elif name == "stochrsi_k":
        rsi = compute_tech(panel, "rsi_14")
        lo = _roll(rsi, 14, "min"); hi = _roll(rsi, 14, "max")
        s = (rsi - lo) / (hi - lo + 1e-12) * 100
    elif name == "bop":
        s = (close - panel["$open"]) / (high - low + 1e-12)
    # ---------------- 波动深化 ----------------
    elif name == "parkinson_20":
        s = np.sqrt(_roll(np.log(high / (low + 1e-12)) ** 2, 20, "mean") / (4 * np.log(2))) * np.sqrt(252) * 100
    elif name == "garman_klass_20":
        s = np.sqrt(_roll(0.5 * np.log(high / (low + 1e-12)) ** 2
                          - (2 * np.log(2) - 1) * np.log(close / (panel["$open"] + 1e-12)) ** 2,
                          20, "mean").clip(lower=0)) * np.sqrt(252) * 100
    elif name == "stddev_20":
        s = _roll(g["$close"].pct_change(), 20, "std") * 100
    # ---------------- 统计系 ----------------
    elif name == "beta_60":
        ret = g["$close"].pct_change()
        mkt = ret.groupby(level="datetime").mean()
        cov = ret.groupby(level="instrument").rolling(60).cov().reset_index(level=0, drop=True)
        var = mkt.rolling(60).var()
        s = cov / (ret.index.get_level_values("datetime").map(var) + 1e-12)
    elif name == "linreg_slope_20":
        s = _roll(close, 20, "apply", lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] / (np.mean(x) + 1e-12) * 100 if len(x) == 20 else np.nan)
    elif name == "tsf_dev_14":
        tsf = _roll(close, 14, "apply", lambda x: (lambda k: k[0] * (len(x) - 1) + k[1])(np.polyfit(np.arange(len(x)), x, 1)) if len(x) == 14 else np.nan)
        s = (close - tsf) / (close + 1e-12) * 100
    elif name == "correl_pv_20":
        s = _rcorr(close, vol, 20)
    # ---------------- 量能深化 ----------------
    elif name == "adosc":
        mfv = (((close - low) - (high - close)) / (high - low + 1e-12)) * vol
        ad = mfv.groupby(level="instrument").cumsum()
        e3 = ad.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=3, adjust=False).mean())
        e10 = ad.groupby(level="instrument", group_keys=False).transform(lambda x: x.ewm(span=10, adjust=False).mean())
        s = (e3 - e10) / (_roll(vol, 20, "mean") + 1e-12)
    elif name == "ad_slope_20":
        mfv = (((close - low) - (high - close)) / (high - low + 1e-12)) * vol
        ad = mfv.groupby(level="instrument").cumsum()
        s = (ad - ad.groupby(level="instrument").shift(20)) / (_roll(vol, 20, "mean") * 20 + 1e-12)
    # ---------------- Alpha101 ----------------
    elif name == "alpha004":
        s = -_tsrank(_csrank(low), 9)
    elif name == "alpha005":
        vwap = amt / (vol + 1e-12)
        s = _csrank(panel["$open"] - _roll(vwap, 10, "mean")) * (-_csrank(close - vwap).abs())
    elif name == "alpha012":
        s = np.sign(vol.groupby(level="instrument").diff(1)) * (-g["$close"].diff(1))
    elif name == "alpha015":
        s = -_roll(_csrank(_rcorr(_csrank(high), _csrank(vol), 3)), 3, "sum")
    elif name == "alpha018":
        oc = close - panel["$open"]
        s = -_csrank(_roll(oc.abs(), 5, "std") + oc + _rcorr(close, panel["$open"], 10))
    elif name == "alpha022":
        s = -(_rcorr(high, vol, 5).groupby(level="instrument").diff(5)) * _csrank(_roll(close, 20, "std"))
    elif name == "alpha025":
        vwap = amt / (vol + 1e-12)
        ret = g["$close"].pct_change()
        s = _csrank((-ret) * _roll(vol, 20, "mean") * vwap * (high - close))
    elif name == "alpha026":
        ts5_v = _tsrank(vol, 5)
        ts5_h = _tsrank(high, 5)
        s = -_roll(_rcorr(ts5_v, ts5_h, 5), 3, "max")
    elif name == "alpha033":
        s = _csrank(-(1 - panel["$open"] / (close + 1e-12))) * (-1)
    elif name == "alpha037":
        oc = panel["$open"] - close
        s = _csrank(_rcorr(oc.groupby(level="instrument").shift(1), close, 200)) + _csrank(oc)
    elif name == "alpha041":
        vwap = amt / (vol + 1e-12)
        s = np.sqrt(high * low) - vwap
    elif name == "alpha054":
        o = panel["$open"]
        s = -(low - close) * (o ** 5) / ((low - high) * (close ** 5) + 1e-12)
    elif name == "alpha101":
        s = (close - panel["$open"]) / (high - low + 0.001)
    # ---------------- K线形态 ----------------
    elif name == "doji":
        s = -(close - panel["$open"]).abs() / (high - low + 1e-12)
    elif name == "hammer":
        body = (close - panel["$open"]).abs()
        lower_sh = pd.concat([close, panel["$open"]], axis=1).min(axis=1) - low
        s = lower_sh / (body + 1e-12) * ((high - pd.concat([close, panel["$open"]], axis=1).max(axis=1)) < body)
    elif name == "engulfing":
        o, c = panel["$open"], close
        po, pc2 = g["$open"].shift(1), g["$close"].shift(1)
        bull = ((c > po) & (pc2 < po) & (c > o) & (o < pc2)).astype(float)
        bear = ((c < po) & (pc2 > po) & (c < o) & (o > pc2)).astype(float)
        s = bull - bear
    elif name == "upper_shadow":
        s = (high - pd.concat([close, panel["$open"]], axis=1).max(axis=1)) / (high - low + 1e-12)
    elif name == "lower_shadow":
        s = (pd.concat([close, panel["$open"]], axis=1).min(axis=1) - low) / (high - low + 1e-12)
    elif name == "body_ratio":
        s = (close - panel["$open"]) / (high - low + 1e-12)
    elif name == "gap_open":
        s = (panel["$open"] / (g["$close"].shift(1) + 1e-12) - 1) * 100
    else:
        raise ValueError(f"目录外因子: {name}")
    s.name = name
    return s.dropna()



BUILTIN_FACTORS = {
    "mom_5d": "5日动量（近5日涨幅）",
    "mom_20d": "20日动量",
    "vol_20d": "20日波动率（年化）",
    "volume_ratio_5_20": "5日/20日量比",
    "amihud_20d": "Amihud非流动性20日（|收益|/成交额）",
    "price_pos_60d": "60日价格位置（0=最低 1=最高）",
}
_PANEL_FIELDS = ["$open", "$high", "$low", "$close", "$volume", "$amount"]


def get_panel_cached(codes: list[str], end: str, lookback_days: int = 400,
                     source: str | None = None) -> pd.DataFrame:
    import datasource

    source = source or datasource.get_source()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ck = _cache_key("panel", source + "|".join(sorted(codes)) + end + str(lookback_days))
    if ck.exists():
        hit = _read_parquet_safe(ck)
        if hit is not None:
            return hit
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y-%m-%d")
    df = fetch_panel(codes, start, end, _PANEL_FIELDS, source=source)
    if df.empty:
        raise RuntimeError("取数为空")
    _write_parquet_atomic(df, ck)
    return df


def compute_builtin(panel: pd.DataFrame, name: str, asof: str | None = None) -> pd.Series:
    """在面板上计算内置因子。asof 给定则只用该日及之前数据（时点严谨）。"""
    df = panel if asof is None else panel[panel.index.get_level_values("datetime") <= asof]
    g = df.groupby(level="instrument", group_keys=False)
    close = df["$close"]
    ret = g["$close"].pct_change()

    if name == "mom_5d":
        s = g["$close"].pct_change(5)
    elif name == "mom_20d":
        s = g["$close"].pct_change(20)
    elif name == "vol_20d":
        s = ret.groupby(level="instrument").rolling(20).std().reset_index(level=0, drop=True) * np.sqrt(252)
    elif name == "volume_ratio_5_20":
        v5 = g["$volume"].rolling(5).mean().reset_index(level=0, drop=True)
        v20 = g["$volume"].rolling(20).mean().reset_index(level=0, drop=True)
        s = v5 / (v20 + 1e-12)
    elif name == "amihud_20d":
        amihud = ret.abs() / (df["$amount"] + 1e-12)
        s = amihud.groupby(level="instrument").rolling(20).mean().reset_index(level=0, drop=True)
    elif name == "price_pos_60d":
        hi = g["$high"].rolling(60).max().reset_index(level=0, drop=True)
        lo = g["$low"].rolling(60).min().reset_index(level=0, drop=True)
        s = (close - lo) / (hi - lo + 1e-12)
    else:
        raise ValueError(f"未知内置因子: {name}")
    s.name = name
    return s.dropna()


# ---------------------------------------------------------------- 组合打分
def zscore(s: pd.Series) -> pd.Series:
    z = (s - s.mean()) / (s.std() + 1e-12)
    return z.clip(-3, 3)


def composite_score(factor_series: dict[str, pd.Series], weights: dict[str, tuple[float, int]],
                    asof: str | None = None) -> pd.Series:
    """factor_series: {name: 长表 Series((instrument, datetime) 或 (datetime, instrument))}
    weights: {name: (权重, 方向±1)}。返回 asof（默认最新日）横截面综合分。"""
    z_list, w_total = [], 0.0
    for name, s in factor_series.items():
        w, direction = weights.get(name, (1.0, 1))
        if w <= 0:
            continue
        s = s.dropna()
        if s.empty:
            continue
        dt_level = "datetime" if "datetime" in s.index.names else s.index.names[0]
        day = asof or s.index.get_level_values(dt_level).max()
        cross = s[s.index.get_level_values(dt_level) == day]
        cross.index = cross.index.get_level_values("instrument")
        z_list.append(zscore(cross) * w * direction)
        w_total += w
    if not z_list:
        return pd.Series(dtype=float)
    combo = pd.concat(z_list, axis=1).mean(axis=1, skipna=True)
    return (combo / max(w_total, 1e-12)).dropna().sort_values(ascending=False)


def industry_cap_select(score: pd.Series, cap: int = 2) -> pd.Series:
    """行业分散：按综合分降序贪心选取，每个行业最多 cap 只（防单一赛道扎堆回撤）。
    行业映射用 stock_industry 表；无映射的票不受限。"""
    import sectorflow

    imap = sectorflow.industry_map()
    if imap.empty:
        return score
    code2sec = dict(zip(imap["code"], imap["sector_name"]))
    kept, cnt = [], {}
    for code in score.sort_values(ascending=False).index:
        sec = code2sec.get(code)
        if sec is not None and cnt.get(sec, 0) >= cap:
            continue
        cnt[sec] = cnt.get(sec, 0) + 1
        kept.append(code)
    return score.loc[kept]


def resonance_select(f_series: dict, weights_a: dict, weights_b: dict,
                     top_n: int, k: int | None = None) -> pd.Series:
    """多周期共振：两套权重（如 1日口径 + 5日口径）各打一次综合分，
    双 Top(2N) 交集优先（按名次和排序），不足部分用主口径 A 名单补。
    返回按共振顺序的前 k 名（分数取 A 口径），调用方再做过滤/截断。"""
    sa = composite_score(f_series, weights_a)
    sb = composite_score(f_series, weights_b)
    k = k or top_n * 2
    top_a, top_b = list(sa.head(k).index), list(sb.head(k).index)
    bset = set(top_b)
    both = [c for c in top_a if c in bset]
    both.sort(key=lambda c: top_a.index(c) + top_b.index(c))
    order = both + [c for c in top_a if c not in bset]
    return sa.loc[order[:k]]


# ---------------------------------------------------------------- 策略过滤（基于最新行情）
STRATEGY_FILTERS = {
    "tradable": "排除停牌（最后一日成交量>0）",
    "bullish_ma": "多头排列（收盘 > MA20 > MA60）",
    "high_20d": "创20日新高",
    "shrink_vol": "缩量（5日均量 < 0.8×20日均量）",
    "not_toppy": "位置不过高（60日价格位置 < 0.85）",
    "macd_gold": "MACD金叉（DIF 上穿 DEA）",
    "rsi_oversold": "RSI6 超卖（<30，博反弹）",
}


def apply_filters(codes: list[str], panel: pd.DataFrame, filters: list[str]) -> list[str]:
    if not filters:
        return codes
    last_day = panel.index.get_level_values("datetime").max()
    snap = panel[panel.index.get_level_values("datetime") == last_day]
    snap.index = snap.index.get_level_values("instrument")
    g = panel.groupby(level="instrument", group_keys=False)

    def _last(s: pd.Series) -> pd.Series:  # 每只标的最新值快照（一次性算好，避免逐股扫描）
        return s.groupby(level="instrument").last()

    ma20 = _last(g["$close"].rolling(20).mean().reset_index(level=0, drop=True))
    ma60 = _last(g["$close"].rolling(60).mean().reset_index(level=0, drop=True))
    v5 = _last(g["$volume"].rolling(5).mean().reset_index(level=0, drop=True))
    v20 = _last(g["$volume"].rolling(20).mean().reset_index(level=0, drop=True))
    hi20 = _last(g["$high"].rolling(20).max().reset_index(level=0, drop=True))
    pos60 = _last(compute_builtin(panel, "price_pos_60d"))
    # MACD/RSI 过滤器序列（DIF/DEA 今昨两点、RSI6 最新值）
    dif_s = compute_tech(panel, "macd_dif")
    hist_s = compute_tech(panel, "macd_hist")
    dea_s = dif_s - hist_s / 2  # macd_hist = 2*(dif-dea) → dea = dif - hist/2
    rsi6_s = compute_tech(panel, "rsi_6")
    def _last2(s):
        g2 = s.groupby(level="instrument")
        return g2.last(), s.groupby(level="instrument").nth(-2)
    dif_now, dif_prev = _last2(dif_s)
    dea_now, dea_prev = _last2(dea_s)
    rsi6_now = _last(rsi6_s)

    ok = []
    for c in codes:
        if c not in snap.index:
            continue
        row = snap.loc[c]
        try:
            if "tradable" in filters and not (row["$volume"] > 0):
                continue
            if "bullish_ma" in filters and not (row["$close"] > ma20.get(c, np.nan) > ma60.get(c, np.nan)):
                continue
            if "high_20d" in filters and not (row["$close"] >= hi20.get(c, np.nan) * 0.999):
                continue
            if "shrink_vol" in filters and not (v5.get(c, np.nan) < 0.8 * (v20.get(c, np.nan) + 1e-12)):
                continue
            if "not_toppy" in filters and not (pos60.get(c, np.nan) < 0.85):
                continue
            if "macd_gold" in filters and not (dif_now.get(c, -9e9) > dea_now.get(c, 9e9)
                                               and dif_prev.get(c, 9e9) <= dea_prev.get(c, -9e9)):
                continue
            if "rsi_oversold" in filters and not (rsi6_now.get(c, 100) < 30):
                continue
        except (TypeError, ValueError):
            continue
        ok.append(c)
    return ok


# ---------------------------------------------------------------- 历史命中统计（仅内置因子，时点严谨）
def forward_hit_stats(codes: list[str], end: str, weights: dict[str, tuple[float, int]],
                      top_n: int = 10, periods: int = 12, step_days: int = 20,
                      forward_days: int = 20) -> pd.DataFrame:
    """过去 periods 个调仓点：按内置因子综合分取 top_n，看 forward_days 远期收益。"""
    panel = get_panel_cached(codes, end, lookback_days=periods * step_days + forward_days + 250)
    days = sorted(panel.index.get_level_values("datetime").unique())
    if len(days) < periods * step_days + forward_days + 60:
        return pd.DataFrame()
    close = panel["$close"].unstack("instrument")  # datetime × instrument

    rows = []
    for i in range(periods):
        t_idx = len(days) - 1 - forward_days - i * step_days
        if t_idx < 61:
            break
        t_day = str(days[t_idx])[:10]
        f_series = {name: compute_builtin(panel, name, asof=t_day)
                    for name, (w, d) in weights.items() if w > 0}
        if not f_series:
            continue
        score = composite_score(f_series, weights, asof=t_day)
        picks = score.head(top_n).index.tolist()
        fwd = close.shift(-forward_days) / close - 1
        fwd_t = fwd.loc[days[t_idx]].dropna()
        if fwd_t.empty:
            continue
        pick_ret = fwd_t[fwd_t.index.isin(picks)].mean()
        rows.append({"调仓日": t_day, f"Top{top_n}平均{forward_days}日收益": pick_ret,
                     "池内中位收益": fwd_t.median(), "超额": pick_ret - fwd_t.median()})
    return pd.DataFrame(rows).sort_values("调仓日")


# ---------------------------------------------------------------- 「为什么选它」白话解释
def factor_contributions(f_series: dict[str, pd.Series], weights: dict[str, tuple[float, int]],
                         code: str, asof: str | None = None) -> list[tuple[str, float]]:
    """某只股票综合分的因子贡献分解：z_i(c)×w_i×d_i，按贡献降序。
    与 composite_score 同口径（z-score 截面标准化），正负号=该因子推/拉这只票上榜。"""
    out = []
    for name, s in f_series.items():
        w, direction = weights.get(name, (0.0, 1))
        if w <= 0:
            continue
        s = s.dropna()
        if s.empty:
            continue
        dt_level = "datetime" if "datetime" in s.index.names else s.index.names[0]
        day = asof or s.index.get_level_values(dt_level).max()
        cross = s[s.index.get_level_values(dt_level) == day]
        cross.index = cross.index.get_level_values("instrument")
        if code not in cross.index:
            continue
        out.append((name, float(zscore(cross)[code]) * w * direction))
    return sorted(out, key=lambda x: -x[1])


def plain_factor_name(name: str) -> str:
    """因子名 → 白话短标签（「为什么选它」用）。
    内置/技术指标用中文字典描述；目录因子挂机制族；LoopEngine 因子名自带族前缀（le_跳空_xxx）。"""
    if name in BUILTIN_FACTORS:
        return BUILTIN_FACTORS[name].split("（")[0]
    if name in TECH_INDICATORS:
        return TECH_INDICATORS[name].split("（")[0]
    if name.startswith("le_"):  # le_{族}_{hash}
        parts = name.split("_")
        if len(parts) >= 2 and parts[1]:
            return f"{parts[1]}类因子"
    if name in NAME2CAT:
        return f"{NAME2CAT[name]}类·{name}"
    return name  # RD-Agent 进化因子名通常是描述性英文，原样展示
