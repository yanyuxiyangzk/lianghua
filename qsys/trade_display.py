"""交易列表的新旧返回格式兼容；仅补展示字段，不改写历史来源。"""
import pandas as pd


def with_signal_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """热更新时柜台模块可能仍返回旧列，页面须兼容缺失的来源快照。"""
    result = frame.copy()
    for column in ("signal_source", "strategy_name", "strategy_version", "signal_batch_id",
                   "attribution_status", "decision_reason", "factor_snapshot", "risk_plan_id"):
        if column not in result.columns:
            result[column] = pd.Series(pd.NA, index=result.index, dtype="object")
    result['attribution_status'] = result['attribution_status'].fillna('historical_unknown').replace({
        'historical_unknown':'历史来源未完整记录', 'manual':'手动委托', 'verified_snapshot':'已保存版本快照'})
    return result
