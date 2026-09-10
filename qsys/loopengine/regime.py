"""市场环境识别模块：基于多维度指标判断当前市场 regime。

使用大盘指数数据（上证/深成/创业板）计算：
1. 趋势方向（均线多头/空头排列）
2. 波动率水平（高/中/低）
3. 成交额变化（放量/缩量）
4. 市场宽度（涨跌家数比）

输出：bull / bear / sideways / transition
"""

import logging
from datetime import datetime

import numpy as np
import pandas as pd

log = logging.getLogger("regime")

# 主要指数权重（用于合成市场信号）
_INDEX_WEIGHTS = {
    "SH000001": 0.4,   # 上证指数
    "SZ399001": 0.3,   # 深证成指
    "SZ399006": 0.3,   # 创业板指
}

# Regime 因子权重映射
REGIME_FACTOR_WEIGHTS = {
    "bull": {
        "mom_60d": 1.5, "mom_20d": 1.3,
        "vol_20d": 0.5, "garman_klass_20": 0.6,
        "amihud_20d": 0.8, "price_pos_60d": 1.2,
    },
    "bear": {
        "mom_60d": 0.3, "mom_20d": 0.4,
        "vol_20d": 1.5, "garman_klass_20": 1.3,
        "amihud_20d": 1.2, "price_pos_60d": 0.6,
    },
    "sideways": {
        "mom_60d": 0.7, "mom_20d": 0.8,
        "vol_20d": 1.0, "garman_klass_20": 1.0,
        "amihud_20d": 1.0, "price_pos_60d": 0.9,
    },
    "transition": {
        "mom_60d": 0.5, "mom_20d": 0.6,
        "vol_20d": 1.2, "garman_klass_20": 1.1,
        "amihud_20d": 1.1, "price_pos_60d": 0.8,
    },
}


def detect_regime(codes: list[str] | None = None, end: str | None = None,
                  lookback_days: int = 120) -> dict:
    """检测当前市场环境。

    Args:
        codes: 指数代码列表，默认使用主要指数
        end: 截止日期，默认最新交易日
        lookback_days: 回溯天数

    Returns:
        {
            "regime": "bull" | "bear" | "sideways" | "transition",
            "confidence": 0.0 ~ 1.0,
            "details": {
                "trend_score": float,  # 趋势得分 -1~1
                "volatility_regime": str,  # high/low/normal
                "volume_regime": str,  # expanding/contracting/normal
                "momentum_score": float,  # 动量得分
            },
            "regime_weights": dict,  # 当前 regime 下的因子权重
        }
    """
    if codes is None:
        codes = list(_INDEX_WEIGHTS.keys())

    # 获取指数面板数据
    try:
        panel = _fetch_index_panel(codes, end, lookback_days)
        if panel.empty or len(panel) < 30:
            return _default_regime()
    except Exception as e:
        log.warning(f"获取指数数据失败: {e}")
        return _default_regime()

    # 计算各维度信号
    trend_score = _compute_trend_score(panel)
    vol_regime = _compute_volatility_regime(panel)
    vola_regime = _compute_volume_regime(panel)
    momentum_score = _compute_momentum_score(panel)

    # 综合判断 regime
    regime, confidence = _classify_regime(trend_score, vol_regime, vola_regime, momentum_score)

    return {
        "regime": regime,
        "confidence": confidence,
        "details": {
            "trend_score": round(trend_score, 4),
            "volatility_regime": vol_regime,
            "volume_regime": vola_regime,
            "momentum_score": round(momentum_score, 4),
        },
        "regime_weights": REGIME_FACTOR_WEIGHTS.get(regime, REGIME_FACTOR_WEIGHTS["sideways"]),
    }


def _fetch_index_panel(codes: list[str], end: str | None, lookback_days: int) -> pd.DataFrame:
    """获取指数面板数据。"""
    import datasource
    import signals as sig

    if end is None:
        end = datasource.get_last_trade_day_q() or datetime.now().strftime("%Y-%m-%d")

    start = (pd.Timestamp(end) - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y-%m-%d")

    try:
        panel = sig.fetch_panel(codes, start, end, ["$close", "$volume", "$amount"],
                                source=datasource.get_loop_source())
        return panel
    except Exception:
        # 回退：直接从数据库读取
        return _fetch_index_panel_from_db(codes, end, lookback_days)


def _fetch_index_panel_from_db(codes: list[str], end: str, lookback_days: int) -> pd.DataFrame:
    """从数据库获取指数数据。"""
    import sqlite3
    from pathlib import Path

    db_path = Path("/data/market.db")
    if not db_path.exists():
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            dfs = []
            for code in codes:
                df = pd.read_sql(
                    "SELECT trade_date, close, volume, amount FROM ifind_daily"
                    " WHERE code=? AND trade_date>=date(?,?) ORDER BY trade_date",
                    conn, params=(code, end, f"-{lookback_days} days"))
                if not df.empty:
                    df = df.set_index("trade_date")
                    df.columns = [f"$close", "$volume", "$amount"]
                    df["instrument"] = code
                    dfs.append(df)
            if not dfs:
                return pd.DataFrame()
            result = pd.concat(dfs)
            result.index = pd.MultiIndex.from_arrays(
                [result["instrument"], result.index],
                names=["instrument", "datetime"])
            return result.drop(columns=["instrument"])
    except Exception as e:
        log.warning(f"从DB获取指数数据失败: {e}")
        return pd.DataFrame()


def _compute_trend_score(panel: pd.DataFrame) -> float:
    """计算趋势得分：基于均线排列。

    MA5 > MA20 > MA60 = +1 (多头排列)
    MA5 < MA20 < MA60 = -1 (空头排列)
    """
    scores = []
    for code in panel.index.get_level_values("instrument").unique():
        try:
            close = panel.loc[code, "$close"]
            if len(close) < 60:
                continue
            ma5 = close.rolling(5).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            ma60 = close.rolling(60).mean().iloc[-1]
            # 计算排列得分
            score = 0
            if ma5 > ma20:
                score += 0.33
            else:
                score -= 0.33
            if ma20 > ma60:
                score += 0.33
            else:
                score -= 0.33
            if ma5 > ma60:
                score += 0.34
            else:
                score -= 0.34
            scores.append(score * _INDEX_WEIGHTS.get(code, 0.33))
        except Exception:
            continue
    return sum(scores) if scores else 0.0


def _compute_volatility_regime(panel: pd.DataFrame) -> str:
    """计算波动率 regime：高/中/低。"""
    vol_ratios = []
    for code in panel.index.get_level_values("instrument").unique():
        try:
            close = panel.loc[code, "$close"]
            if len(close) < 60:
                continue
            ret = close.pct_change().dropna()
            vol_20 = ret.iloc[-20:].std() * np.sqrt(252)
            vol_60 = ret.iloc[-60:].std() * np.sqrt(252)
            vol_ratio = vol_20 / (vol_60 + 1e-12)
            vol_ratios.append(vol_ratio * _INDEX_WEIGHTS.get(code, 0.33))
        except Exception:
            continue
    if not vol_ratios:
        return "normal"
    avg_ratio = sum(vol_ratios)
    if avg_ratio > 1.3:
        return "high"
    elif avg_ratio < 0.7:
        return "low"
    return "normal"


def _compute_volume_regime(panel: pd.DataFrame) -> str:
    """计算成交额 regime：放量/缩量/正常。"""
    vol_changes = []
    for code in panel.index.get_level_values("instrument").unique():
        try:
            amount = panel.loc[code, "$amount"]
            if len(amount) < 40:
                continue
            avg_5 = amount.iloc[-5:].mean()
            avg_20 = amount.iloc[-20:].mean()
            ratio = avg_5 / (avg_20 + 1e-12)
            vol_changes.append(ratio * _INDEX_WEIGHTS.get(code, 0.33))
        except Exception:
            continue
    if not vol_changes:
        return "normal"
    avg_change = sum(vol_changes)
    if avg_change > 1.3:
        return "expanding"
    elif avg_change < 0.7:
        return "contracting"
    return "normal"


def _compute_momentum_score(panel: pd.DataFrame) -> float:
    """计算动量得分：短期(5d) vs 中期(20d) 动量。"""
    scores = []
    for code in panel.index.get_level_values("instrument").unique():
        try:
            close = panel.loc[code, "$close"]
            if len(close) < 20:
                continue
            mom_5 = (close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0
            mom_20 = (close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0
            # 短期+中期动量综合
            score = mom_5 * 0.4 + mom_20 * 0.6
            scores.append(score * _INDEX_WEIGHTS.get(code, 0.33))
        except Exception:
            continue
    return sum(scores) if scores else 0.0


def _classify_regime(trend_score: float, vol_regime: str,
                     volume_regime: str, momentum_score: float) -> tuple[str, float]:
    """根据多维度信号分类 regime。

    Returns:
        (regime, confidence)
    """
    # 趋势 + 动量综合
    combined = trend_score * 0.6 + momentum_score * 10 * 0.4  # momentum 缩放到相近量级

    # 判断 regime
    if combined > 0.3:
        regime = "bull"
        confidence = min(1.0, 0.5 + abs(combined) * 0.5)
    elif combined < -0.3:
        regime = "bear"
        confidence = min(1.0, 0.5 + abs(combined) * 0.5)
    else:
        # 震荡或转换
        if vol_regime == "high" or volume_regime in ("expanding", "contracting"):
            regime = "transition"
            confidence = 0.4 + abs(combined) * 0.3
        else:
            regime = "sideways"
            confidence = 0.5 - abs(combined) * 0.3

    # 波动率修正
    if vol_regime == "high" and regime == "bull":
        regime = "transition"
        confidence = 0.5

    return regime, round(confidence, 3)


def _default_regime() -> dict:
    """默认 regime（数据不足时）。"""
    return {
        "regime": "sideways",
        "confidence": 0.3,
        "details": {
            "trend_score": 0.0,
            "volatility_regime": "normal",
            "volume_regime": "normal",
            "momentum_score": 0.0,
        },
        "regime_weights": REGIME_FACTOR_WEIGHTS["sideways"],
    }


def get_regime_factor_weight(regime: str, factor_name: str) -> float:
    """获取指定 regime 下的因子权重倍数。"""
    weights = REGIME_FACTOR_WEIGHTS.get(regime, REGIME_FACTOR_WEIGHTS["sideways"])
    return weights.get(factor_name, 1.0)


def detect_regime_from_reports(reports: list[dict]) -> str | None:
    """从历史战报中提取市场环境信息（辅助判断）。

    如果最近战报提到"市场风格切换"、"震荡"等关键词，辅助判断 regime。
    """
    if not reports:
        return None

    keywords_map = {
        "bull": ["牛市", "上涨趋势", "多头", "放量上涨", "突破"],
        "bear": ["熊市", "下跌趋势", "空头", "缩量下跌", "破位"],
        "sideways": ["震荡", "横盘", "盘整", "区间", "窄幅"],
        "transition": ["风格切换", "转换", "变盘", "分化", "轮动"],
    }

    regime_counts = {"bull": 0, "bear": 0, "sideways": 0, "transition": 0}

    for report in reports[:5]:  # 最近5份战报
        content = report.get("content", "")
        for regime, keywords in keywords_map.items():
            for kw in keywords:
                if kw in content:
                    regime_counts[regime] += 1

    # 返回出现最多的 regime
    max_regime = max(regime_counts, key=regime_counts.get)
    if regime_counts[max_regime] > 0:
        return max_regime
    return None
