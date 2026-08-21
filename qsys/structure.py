"""P2：结构骨架提取 + 机制族分类 + FSA 反同质化。

骨架 = 算子序列@字段签名（如 "rolling-pct_change-mean@$close"），
用于 FSA 冻结、失败模式归因、机制族统计。
"""

import re

# ---------------------------------------------------------------- 骨架提取
_CODE_OPS = ["rolling", "pct_change", "diff", "shift", "corr", "cov", "std", "mean", "max",
             "min", "abs", "sign", "log", "sqrt", "rank", "ewm", "cumsum", "clip", "skew", "kurt"]
_CODE_FIELDS = ["$open", "$high", "$low", "$close", "$volume", "$amount", "$factor"]


def extract_skeleton(name: str, code: str | None = None) -> str:
    """从因子代码提取结构骨架；无代码（内置/目录因子）则用名称本身。"""
    if not code:
        return name
    ops = [op for op in _CODE_OPS if op in code]
    fields = sorted({f for f in _CODE_FIELDS if f in code})
    ops_sig = "-".join(ops[:6]) if ops else "custom"
    fields_sig = ",".join(fields) if fields else "derived"
    return f"{ops_sig}@{fields_sig}"


# ---------------------------------------------------------------- 机制族（13 族）
MECHANISM_FAMILIES = {
    "跳空": ["gap", "overnight", "alpha041"],
    "振幅": ["amplitude", "hl_ratio", "atr", "parkinson", "garman", "range", "stddev"],
    "影线": ["upper_shadow", "lower_shadow", "hammer", "doji"],
    "量价背离": ["volume_price", "correl_pv", "alpha015", "alpha022", "alpha026", "alpha012"],
    "动量": ["mom", "cmo", "trix", "apo", "ppo", "adx", "dx", "aroon", "macd", "linreg"],
    "反转": ["reversal", "bias", "rsi", "willr", "kdj", "stochrsi", "alpha004", "alpha005", "alpha054"],
    "波动": ["vol", "downside", "kurt", "skew", "max_dd", "ultosc"],
    "流动性": ["amihud", "turnover", "volume_std", "vol_ratio", "volume_ratio"],
    "趋势均线": ["ma_bullish", "dema", "tema", "kama", "wma", "midpoint", "tsf", "plus_di", "minus_di", "adxr"],
    "价格位置": ["price_pos", "dist_from", "midpoint"],
    "资金流": ["cmf", "mfi", "obv", "adosc", "ad_slope", "amount_ratio", "bop"],
    "统计": ["beta", "var"],
    "形态": ["engulfing", "body_ratio", "doji"],
}


def assign_family(name: str, skeleton: str = "") -> str:
    low = (name + " " + (skeleton or "")).lower()
    for fam, keys in MECHANISM_FAMILIES.items():
        if any(k.lower() in low for k in keys):
            return fam
    return "其他"


def family_coverage(registry_df) -> dict:
    """机制族覆盖矩阵：{family: count}（含 0 覆盖族）。"""
    cov = {fam: 0 for fam in MECHANISM_FAMILIES}
    if registry_df is None or registry_df.empty or "family" not in registry_df.columns:
        return cov
    for fam in registry_df["family"].dropna():
        cov[fam] = cov.get(fam, 0) + 1
    return cov
