# How to read files.
For example, if you want to read `filename.h5`
```Python
import pandas as pd
df = pd.read_hdf("filename.h5", key="data")
```
NOTE: **key is always "data" for all hdf5 files**.

# Here is a short description about the data

| Filename              | Description                                                      |
| --------------------- | -----------------------------------------------------------------|
| "daily_pv.h5"         | Adjusted daily price and volume data with derived features.      |

## Daily price and volume data (daily_pv.h5)
Index: (instrument, datetime)

### Basic Price/Volume
- $open: open price of the stock on that day.
- $close: close price of the stock on that day.
- $high: high price of the stock on that day.
- $low: low price of the stock on that day.
- $volume: volume of the stock on that day.
- $factor: factor value of the stock on that day.

### Derived Features
- $vwap: volume weighted average price (amount/volume).
- return_1d: daily return = (close_t / close_{t-1}) - 1.
- momentum_5d: 5-day momentum = (close_t / close_{t-5}) - 1.
- momentum_20d: 20-day momentum = (close_t / close_{t-20}) - 1.
- volatility_20d: 20-day volatility = Std(close, 20) / close.
- volume_change: volume change = (volume_t / volume_{t-1}) - 1.
- corr_price_volume_20d: 20-day correlation between close and volume.
