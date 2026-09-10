"""因子衰减检测系统：实时监控因子预测能力衰减，自动退役失效因子。

核心思想：因子的预测能力会随时间衰减，需要实时监控并自动退役。
衰减检测通过滑动窗口监控IC序列变化，自动识别衰减因子并采取相应措施。

衰减阈值：
- 轻度衰减: IC衰减率 < -30%  → 降低权重
- 中度衰减: IC衰减率 < -50%  → 标记警告
- 重度衰减: IC衰减率 < -70%  → 父本权重软惩罚至 0.2（验证显示重度组存在 OOS 恢复，不硬退役）
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

import factor_eval as fe
import library

log = logging.getLogger("decay")

# 衰减检测参数
DECAY_THRESHOLDS = {
    "mild": -0.30,      # 轻度衰减阈值
    "moderate": -0.50,  # 中度衰减阈值
    "severe": -0.70,    # 重度衰减阈值
}

# 窗口参数
LOOKBACK_SHORT = 60    # 短期窗口（60天）
LOOKBACK_LONG = 500    # 长期窗口（500天）
MIN_DATA_DAYS = 300    # 最少数据天数
MAX_DETECT_PER_ROUND = 30  # 每轮最多检测的因子数（按最久未检测优先，轮转覆盖全部）


def _get_ic_series(factor_name: str, codes: list[str], end: str) -> pd.Series:
    """获取因子的IC时间序列。"""
    try:
        code = None
        reg = library.get_factor_registry()
        if not reg.empty:
            r = reg[reg["name"] == factor_name]
            if not r.empty:
                code = r.iloc[0].get("code")
        if not code:
            log.warning(f"因子 {factor_name} 无代码，跳过衰减检测")
            return pd.Series(dtype=float)
        fac = {"name": factor_name, "kind": "loopengine", "code": code}
        ic = fe.get_ic_series(fac, codes, end, source="qlib_local")
        return ic
    except Exception as e:
        log.warning(f"获取因子 {factor_name} 的IC序列失败: {e}")
        return pd.Series(dtype=float)


def detect_factor_decay(factor_name: str, codes: list[str], end: str,
                       lookback_short: int = LOOKBACK_SHORT,
                       lookback_long: int = LOOKBACK_LONG) -> dict:
    """
    检测因子衰减
    
    Args:
        factor_name: 因子名称
        codes: 股票池代码
        end: 截止日期
        lookback_short: 短期窗口（60天）
        lookback_long: 长期窗口（500天）
    
    Returns:
        dict: {
            'decay_status': 'normal', 'mild', 'moderate', 'severe', 'insufficient_data'
            'decay_rate': 衰减率
            'ic_long': 长期IC
            'ic_short': 短期IC
            'ic_std': IC标准差
            'check_time': 检查时间
        }
    """
    # 获取IC序列
    ic_series = _get_ic_series(factor_name, codes, end)
    
    # 数据不足
    if len(ic_series) < MIN_DATA_DAYS:
        return {
            'decay_status': 'insufficient_data',
            'decay_rate': 0.0,
            'ic_long': 0.0,
            'ic_short': 0.0,
            'ic_std': 0.0,
            'check_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    # 确保窗口不超过数据长度
    lookback_long = min(lookback_long, len(ic_series) - 10)  # 留10天缓冲
    lookback_short = min(lookback_short, lookback_long - 30)  # 短期窗口至少比长期窗口小30天
    
    # 计算长期IC（排除最近的短期窗口，避免重叠）
    ic_long = ic_series[-lookback_long:-lookback_short].mean()
    
    # 计算短期IC
    ic_short = ic_series[-lookback_short:].mean()
    
    # 计算IC标准差（衡量稳定性）
    ic_std = ic_series[-lookback_long:].std()
    
    # 计算衰减率
    if abs(ic_long) < 1e-6:
        decay_rate = 0.0
    else:
        decay_rate = (ic_short - ic_long) / abs(ic_long)
    
    # 判断衰减状态
    if decay_rate < DECAY_THRESHOLDS["severe"]:
        decay_status = 'severe'  # 重度衰减，自动退役
    elif decay_rate < DECAY_THRESHOLDS["moderate"]:
        decay_status = 'moderate'  # 中度衰减，警告
    elif decay_rate < DECAY_THRESHOLDS["mild"]:
        decay_status = 'mild'  # 轻度衰减，降低权重
    else:
        decay_status = 'normal'  # 正常
    
    return {
        'decay_status': decay_status,
        'decay_rate': round(decay_rate, 4),
        'ic_long': round(float(ic_long), 4),
        'ic_short': round(float(ic_short), 4),
        'ic_std': round(float(ic_std), 4),
        'check_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def batch_detect_decay(factor_names: list[str], codes: list[str], end: str) -> pd.DataFrame:
    """
    批量检测因子衰减
    
    Args:
        factor_names: 因子名称列表
        codes: 股票池代码
        end: 截止日期
    
    Returns:
        pd.DataFrame: 衰减检测结果
    """
    results = []
    for name in factor_names:
        try:
            result = detect_factor_decay(name, codes, end)
            result['factor_name'] = name
            results.append(result)
        except Exception as e:
            log.warning(f"检测因子 {name} 衰减失败: {e}")
            results.append({
                'factor_name': name,
                'decay_status': 'error',
                'decay_rate': 0.0,
                'ic_long': 0.0,
                'ic_short': 0.0,
                'ic_std': 0.0,
                'check_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    
    return pd.DataFrame(results)


def update_decay_status(factor_name: str, decay_result: dict):
    """
    更新因子衰减状态到数据库
    
    Args:
        factor_name: 因子名称
        decay_result: detect_factor_decay 的返回结果
    """
    with library._lconn() as c:
        # 确保表存在
        c.execute("""CREATE TABLE IF NOT EXISTS factor_decay (
            factor_name TEXT PRIMARY KEY,
            decay_status TEXT,
            decay_rate REAL,
            ic_long REAL,
            ic_short REAL,
            ic_std REAL,
            check_time TEXT,
            updated_at TEXT
        )""")
        
        # 更新衰减状态
        c.execute("""INSERT OR REPLACE INTO factor_decay 
            (factor_name, decay_status, decay_rate, ic_long, ic_short, ic_std, check_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (factor_name, decay_result['decay_status'], decay_result['decay_rate'],
             decay_result['ic_long'], decay_result['ic_short'], decay_result['ic_std'],
             decay_result['check_time'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        
        # 同时更新 factor_registry 表的衰减状态
        c.execute("""UPDATE factor_registry 
            SET decay_status = ?, decay_rate = ?
            WHERE name = ?""",
            (decay_result['decay_status'], decay_result['decay_rate'], factor_name))
    
    log.info(f"因子 {factor_name} 衰减状态更新: {decay_result['decay_status']} "
             f"(衰减率: {decay_result['decay_rate']})")


def get_decay_status(factor_name: str) -> Optional[dict]:
    """
    获取因子衰减状态
    
    Args:
        factor_name: 因子名称
    
    Returns:
        dict: 衰减状态信息，如果不存在返回 None
    """
    with library._lconn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS factor_decay (
            factor_name TEXT PRIMARY KEY,
            decay_status TEXT,
            decay_rate REAL,
            ic_long REAL,
            ic_short REAL,
            ic_std REAL,
            check_time TEXT,
            updated_at TEXT
        )""")
        
        row = c.execute("SELECT * FROM factor_decay WHERE factor_name=?", 
                       (factor_name,)).fetchone()
        
        if row:
            return {
                'factor_name': row[0],
                'decay_status': row[1],
                'decay_rate': row[2],
                'ic_long': row[3],
                'ic_short': row[4],
                'ic_std': row[5],
                'check_time': row[6],
                'updated_at': row[7]
            }
        return None


def get_decayed_factors(status: str = 'severe') -> list[str]:
    """
    获取指定衰减状态的因子列表
    
    Args:
        status: 衰减状态 ('mild', 'moderate', 'severe')
    
    Returns:
        list: 因子名称列表
    """
    with library._lconn() as c:
        rows = c.execute(
            "SELECT factor_name FROM factor_decay WHERE decay_status=?", 
            (status,)
        ).fetchall()
        return [row[0] for row in rows]


def adjust_factor_weight(factor_name: str, decay_status: str) -> float:
    """
    根据衰减状态调整因子权重
    
    Args:
        factor_name: 因子名称
        decay_status: 衰减状态
    
    Returns:
        float: 调整后的权重系数
    """
    weight_adjustments = {
        'normal': 1.0,
        'mild': 0.7,      # 轻度衰减，权重降低30%
        'moderate': 0.4,   # 中度衰减，权重降低60%
        # 重度：软惩罚 0.2 而非 0.0 —— walk-forward 验证（250天OOS）显示
        # 重度组中相当比例因子在 OOS 恢复（市场状态切换时集体误报），硬归零会误杀
        'severe': 0.2,
        'insufficient_data': 1.0,
        'error': 1.0
    }
    
    return weight_adjustments.get(decay_status, 1.0)


def run_decay_detection(codes: list[str], end: str, 
                       factor_names: Optional[list[str]] = None) -> dict:
    """
    运行衰减检测流程
    
    Args:
        codes: 股票池代码
        end: 截止日期
        factor_names: 要检测的因子名称列表，如果为 None 则检测所有已入库因子
    
    Returns:
        dict: 检测结果统计
    """
    log.info("开始因子衰减检测...")
    
    # 获取要检测的因子列表
    if factor_names is None:
        registry = library.get_factor_registry()
        loop_reg = registry[registry["engine"] == "loopengine"] if not registry.empty else registry
        if not loop_reg.empty and "gate_status" in loop_reg.columns:
            loop_reg = loop_reg[loop_reg["gate_status"] == 1]
        factor_names = loop_reg["name"].tolist()
        
        # 每轮限量：优先检测最久未检测的因子，多轮轮转覆盖全部
        with library._lconn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS factor_decay (
                factor_name TEXT PRIMARY KEY, decay_status TEXT, decay_rate REAL,
                ic_long REAL, ic_short REAL, ic_std REAL, check_time TEXT, updated_at TEXT
            )""")
            checked = {r[0]: (r[1] or "") for r in
                       c.execute("SELECT factor_name, check_time FROM factor_decay").fetchall()}
        factor_names = sorted(factor_names, key=lambda n: checked.get(n, ""))
        factor_names = factor_names[:MAX_DETECT_PER_ROUND]
    
    if not factor_names:
        log.info("没有需要检测的因子")
        return {'total': 0, 'normal': 0, 'mild': 0, 'moderate': 0, 'severe': 0}
    
    # 批量检测
    results = batch_detect_decay(factor_names, codes, end)
    
    # 更新数据库
    for _, row in results.iterrows():
        update_decay_status(row['factor_name'], row.to_dict())
    
    # 统计结果
    stats = {
        'total': len(results),
        'normal': len(results[results['decay_status'] == 'normal']),
        'mild': len(results[results['decay_status'] == 'mild']),
        'moderate': len(results[results['decay_status'] == 'moderate']),
        'severe': len(results[results['decay_status'] == 'severe']),
        'insufficient_data': len(results[results['decay_status'] == 'insufficient_data']),
    }
    
    log.info(f"衰减检测完成: 总计 {stats['total']} 个因子, "
             f"正常 {stats['normal']}, 轻度衰减 {stats['mild']}, "
             f"中度衰减 {stats['moderate']}, 重度衰减 {stats['severe']}")
    
    return stats


def detect_factor_decay_regime_aware(factor_name: str, codes: list[str], end: str,
                                     regime: str = "sideways") -> dict:
    """Regime-aware 衰减检测：根据市场环境调整检测参数。

    不同市场环境下，因子衰减的判断标准不同：
    - 高波动期（bear/high_vol）：缩短检测窗口，更敏感
    - 低波动期（bull/low_vol）：延长检测窗口，更稳定
    - 转换期（transition）：使用标准窗口

    Args:
        factor_name: 因子名称
        codes: 股票池代码
        end: 截止日期
        regime: 当前市场环境 (bull/bear/sideways/transition)

    Returns:
        dict: 衰减检测结果
    """
    # 根据 regime 调整窗口参数
    regime_windows = {
        "bull": {"short": 40, "long": 400},      # 牛市：因子衰减慢，延长窗口
        "bear": {"short": 30, "long": 300},      # 熊市：因子衰减快，缩短窗口
        "sideways": {"short": 60, "long": 500},  # 震荡：标准窗口
        "transition": {"short": 45, "long": 450}, # 转换：中等窗口
    }

    windows = regime_windows.get(regime, regime_windows["sideways"])

    return detect_factor_decay(
        factor_name, codes, end,
        lookback_short=windows["short"],
        lookback_long=windows["long"]
    )


def adjust_weight_by_regime_and_decay(factor_name: str, decay_status: str,
                                      regime: str = "sideways") -> float:
    """根据 regime 和 decay 状态综合调整因子权重。

    在不同市场环境下，衰减因子的惩罚力度不同：
    - 熊市：衰减因子惩罚更重（市场变化快）
    - 牛市：衰减因子惩罚更轻（可能只是暂时波动）
    - 转换期：标准惩罚

    Args:
        factor_name: 因子名称
        decay_status: 衰减状态
        regime: 当前市场环境

    Returns:
        float: 综合调整后的权重系数
    """
    # 基础衰减权重
    base_weight = adjust_factor_weight(factor_name, decay_status)

    # regime 调整系数
    regime_multipliers = {
        "bull": 1.1,        # 牛市：轻微放宽（因子可能只是暂时波动）
        "bear": 0.8,        # 熊市：收紧（市场变化快，衰减更可信）
        "sideways": 1.0,    # 震荡：标准
        "transition": 0.9,  # 转换：略收紧
    }

    multiplier = regime_multipliers.get(regime, 1.0)
    return base_weight * multiplier
