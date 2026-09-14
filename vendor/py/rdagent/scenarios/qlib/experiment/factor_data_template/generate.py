import qlib

# 使用宿主机上的 Qlib 数据路径
import os
qlib_data_path = os.environ.get("QLIB_DATA_DIR", "/home/zk/code/lianghua/data/qlib_home/.qlib/qlib_data/cn_data")
qlib.init(provider_uri=qlib_data_path)

from qlib.data import D

instruments = D.instruments()

# P1: 增强字段 - 从 6 个扩展到 15 个（包含 Alpha158 衍生字段）
# 时间范围与 Qlib 完全对齐（2008-至今）
fields = [
    # 原有：基础价量
    "$open", "$close", "$high", "$low", "$volume", "$factor",
    # 新增：价格衍生
    "$vwap",                        # 成交均价 (amount/volume)
    "Ref($close, 1)/$close - 1",   # 日收益率 (return_1d)
    "Ref($close, 5)/$close - 1",   # 5日动量 (momentum_5d)
    "Ref($close, 20)/$close - 1",  # 20日动量 (momentum_20d)
    "Std($close, 20)/$close",      # 20日波动率 (volatility_20d)
    # 新增：量价衍生
    "$volume/Ref($volume, 1) - 1", # 成交量变化 (volume_change)
    "Corr($close, $volume, 20)",   # 20日价量相关性 (corr_price_volume_20d)
    "Ref($high, 20)/Ref($low, 20) - 1",  # 20日振幅 (amplitude_20d)
    "Mean($close, 5)/Mean($close, 20) - 1",  # 均线偏离 (ma偏离)
]

# 为新字段生成别名（Qlib 表达式无法直接作为列名）
field_aliases = {
    "$vwap": "$vwap",
    "Ref($close, 1)/$close - 1": "return_1d",
    "Ref($close, 5)/$close - 1": "momentum_5d",
    "Ref($close, 20)/$close - 1": "momentum_20d",
    "Std($close, 20)/$close": "volatility_20d",
    "$volume/Ref($volume, 1) - 1": "volume_change",
    "Corr($close, $volume, 20)": "corr_price_volume_20d",
    "Ref($high, 20)/Ref($low, 20) - 1": "amplitude_20d",
    "Mean($close, 5)/Mean($close, 20) - 1": "ma_deviation",
}

# 提取全量数据
data = D.features(instruments, fields, freq="day").swaplevel().sort_index().loc["2008-12-29":].sort_index()

# 重命名列（将 Qlib 表达式转换为简洁别名）
data = data.rename(columns=field_aliases)

data.to_hdf("./daily_pv_all.h5", key="data")
print(f"Generated daily_pv_all.h5: {data.shape[0]} rows, {data.shape[1]} columns")
print(f"Columns: {data.columns.tolist()}")


# P1: debug 数据也使用增强字段
first100 = data.reset_index()["instrument"].unique()[:100]
fields_debug = [
    "$open", "$close", "$high", "$low", "$volume", "$factor",
    "$vwap", "Ref($close, 1)/$close - 1", "Ref($close, 5)/$close - 1",
    "Ref($close, 20)/$close - 1", "Std($close, 20)/$close",
    "$volume/Ref($volume, 1) - 1", "Corr($close, $volume, 20)",
]
data_debug = D.features(instruments, fields_debug, start_time="2018-01-01", end_time="2019-12-31", freq="day")
data_debug = data_debug[data_debug.index.get_level_values("instrument").isin(first100)].sort_index()
data_debug = data_debug.rename(columns=field_aliases)

data_debug.to_hdf("./daily_pv_debug.h5", key="data")
print(f"Generated daily_pv_debug.h5: {data_debug.shape[0]} rows, {data_debug.shape[1]} columns")
