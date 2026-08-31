#!/usr/bin/env python3
"""
涨停/跌停延续因子 - 用于研究涨停后是否继续涨、跌停后是否继续跌
基于A股涨跌停制度（10%涨跌幅限制）
"""
import pandas as pd
import numpy as np


def calculate_limit_factors():
    """
    计算涨停/跌停相关因子：
    1. 连板天数：连续涨停/跌停的天数
    2. 涨停封单强度：涨停时成交量相对前5日均量的倍数
    3. 打板情绪指数：滚动窗口内涨停数-跌停数
    4. 涨停后次日收益：涨停后第二天的收益率（作为标签/因子）
    5. 跌停后次日收益：跌停后第二天的收益率
    """
    df = pd.read_hdf('daily_pv.h5', key='data')
    df = df.sort_index(level=['instrument', 'datetime'])

    # 计算日收益率
    df['pct_change'] = df.groupby(level='instrument')['$close'].pct_change()

    # 涨停/跌停判定（10%涨跌幅限制）
    df['is_limit_up'] = (df['$close'] == df['$high']) & (df['pct_change'] >= 0.095)
    df['is_limit_down'] = (df['$close'] == df['$low']) & (df['pct_change'] <= -0.095)

    # 因子1: 连板天数（连续涨停天数）
    def count_consecutive_limits(series):
        """计算连续涨停/跌停天数"""
        result = pd.Series(0, index=series.index)
        for i in range(1, len(series)):
            if series.iloc[i]:
                result.iloc[i] = result.iloc[i-1] + 1
            else:
                result.iloc[i] = 0
        return result

    df['consecutive_limit_up'] = df.groupby(level='instrument')['is_limit_up'].transform(count_consecutive_limits)
    df['consecutive_limit_down'] = df.groupby(level='instrument')['is_limit_down'].transform(count_consecutive_limits)

    # 因子2: 涨停封单强度（涨停日成交量/前5日均量）
    df['avg_volume_5d'] = df.groupby(level='instrument')['$volume'].transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    )
    df['limit_up_strength'] = df['consecutive_limit_up'].apply(lambda x: 1.0) * \
                              (df['$volume'] / df['avg_volume_5d'].replace(0, np.nan))
    df.loc[~df['is_limit_up'], 'limit_up_strength'] = 0

    # 因子3: 打板情绪指数（滚动20日内涨停数-跌停数）
    df['limit_up_count_20d'] = df.groupby(level='instrument')['is_limit_up'].transform(
        lambda x: x.astype(int).rolling(20, min_periods=1).sum()
    )
    df['limit_down_count_20d'] = df.groupby(level='instrument')['is_limit_down'].transform(
        lambda x: x.astype(int).rolling(20, min_periods=1).sum()
    )
    df['trading_sentiment'] = df['limit_up_count_20d'] - df['limit_down_count_20d']

    # 因子4: 涨停后次日收益（用于分析涨停延续性）
    df['next_day_return'] = df.groupby(level='instrument')['pct_change'].shift(-1)
    df['limit_up_next_return'] = df['next_day_return'].where(df['is_limit_up'], 0)

    # 因子5: 跌停后次日收益
    df['limit_down_next_return'] = df['next_day_return'].where(df['is_limit_down'], 0)

    # 输出因子（每只股票一个因子值）
    factors = pd.DataFrame({
        'consecutive_limit_up': df['consecutive_limit_up'],
        'consecutive_limit_down': df['consecutive_limit_down'],
        'limit_up_strength': df['limit_up_strength'],
        'trading_sentiment': df['trading_sentiment'],
        'limit_up_next_return': df['limit_up_next_return'],
        'limit_down_next_return': df['limit_down_next_return']
    }, index=df.index)

    factors = factors.dropna()
    return factors


if __name__ == '__main__':
    factors = calculate_limit_factors()
    factors.to_hdf('result.h5', key='data', mode='w')
    print(f"Generated {len(factors)} factor values")
    print(f"Columns: {list(factors.columns)}")
    print(factors.head(20))
