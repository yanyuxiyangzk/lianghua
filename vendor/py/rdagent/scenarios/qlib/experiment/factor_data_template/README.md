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
| "sector_daily.h5"     | Daily sector/industry data (market sentiment).                   |
| "stock_industry.h5"   | Stock to sector/industry mapping table.                          |


# For different data, We have some basic knowledge for them

## Daily price and volume data (daily_pv.h5)
Index: (datetime, instrument)

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
- amplitude_20d: 20-day amplitude = (high_{t-20} / low_{t-20}) - 1.
- ma_deviation: MA deviation = (MA5 / MA20) - 1.

## Sector daily data (sector_daily.h5)
Index: (datetime, sector_name)
- $avg_chg_pct: average daily change percentage of stocks in the sector.
- $total_amount: total trading amount of the sector (in CNY).
- $flow_net: net money flow into/out of the sector (in CNY).
- $up_count: number of stocks that went up in the sector.
- $down_count: number of stocks that went down in the sector.
- $members: total number of stocks in the sector.

## Stock industry mapping (stock_industry.h5)
Index: (instrument)
- $sector_name: the sector/industry name of the stock.

# How to join data
To join sector data with stock data:
```Python
# Load data
df_stock = pd.read_hdf("daily_pv.h5", key="data")
df_industry = pd.read_hdf("stock_industry.h5", key="data")
df_sector = pd.read_hdf("sector_daily.h5", key="data")

# Join stock with industry
df_stock = df_stock.join(df_industry, on="instrument")

# Join with sector daily data
df_merged = df_stock.merge(
    df_sector.reset_index(),
    on=["datetime", "sector_name"],
    how="left"
)
```
