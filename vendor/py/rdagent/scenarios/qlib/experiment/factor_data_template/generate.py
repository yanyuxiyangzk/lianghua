import qlib

qlib.init(provider_uri="~/.qlib/qlib_data/cn_data")

from qlib.data import D

instruments = D.instruments()
fields = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
data = D.features(instruments, fields, freq="day").swaplevel().sort_index().loc["2008-12-29":].sort_index()

data.to_hdf("./daily_pv_all.h5", key="data")


fields = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
# 修复（lianghua 本地补丁）：原实现 .loc[标量列表] 在新版 pandas 的 MultiIndex 上
# 会按"完整索引键"匹配而抛 KeyError；改为对 instrument 层显式 isin 过滤，语义不变：
# 全量数据中的前 100 只标的 × 2018~2019 区间，最终索引 (instrument, datetime)。
first100 = data.reset_index()["instrument"].unique()[:100]
data = D.features(instruments, fields, start_time="2018-01-01", end_time="2019-12-31", freq="day")
data = data[data.index.get_level_values("instrument").isin(first100)].sort_index()

data.to_hdf("./daily_pv_debug.h5", key="data")
