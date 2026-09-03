# iFinD 数据中心架构设计 - 最终确认版

## 一、已确认决策

| 决策项 | 确认值 | 说明 |
|--------|--------|------|
| 实时快照频率 | **每15分钟** | 盘中09:30-15:00写入 |
| `ifind_realtime` 保留 | **7天** | 自动清理过期数据 |
| `market_daily` 保留 | **15天** | 可在系统设置中调整 |
| 手动刷新 | **触发调度器任务** | 页面不直接写DB |
| 调度器失败 | **日志 + 页面提示** | 阶段2实现邮件通知 |
| 首次部署 | **拉取3天历史** | 减少首次等待时间 |
| 阶段划分 | **阶段1做4个表** | MVP验证 |
| 数据降级 | **暂不实现** | 后续按需添加 |
| 实时快照范围 | **全市场5200+只** | 全量监控 |
| 调度器 | **复用现有 scheduler.py** | 不新增容器 |

---

## 二、核心架构

### 数据流

```
iFinD API → 调度器(定时任务) → SQLite数据库 → 页面(只读)
```

### 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      iFinD API                              │
│  THS_RQ (实时) │ THS_BD (基本面) │ THS_HQ (历史) │ THS_WCQuery │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│               lh-qsys (Streamlit + 调度器)                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ 09:00        │  │ 09:30-15:00  │  │ 15:05        │      │
│  │ 更新A股列表   │  │ 每15分钟写入  │  │ 收盘后写入    │      │
│  │ ifind_       │  │ ifind_       │  │ market_daily │      │
│  │ stocklist    │  │ realtime     │  │ + basic_daily│      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐                        │
│  │ 首次部署     │  │ 软件重启     │                        │
│  │ 拉取3天历史  │  │ 检测并同步   │                        │
│  └──────────────┘  └──────────────┘                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    SQLite DB (/data/market.db)              │
├─────────────────────────────────────────────────────────────┤
│  ifind_stocklist │ ifind_realtime │ market_daily │ ...      │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│               页面展示（只读数据库）                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ A股列表页     │  │ 实时行情页    │  │ 历史K线页    │      │
│  │ SELECT FROM  │  │ SELECT FROM  │  │ SELECT FROM  │      │
│  │ ifind_       │  │ ifind_       │  │ market_daily │      │
│  │ stocklist    │  │ realtime     │  │              │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  所有页面只读数据库，不调用 iFinD API                         │
│  手动刷新按钮 → 触发调度器任务（不直接写DB）                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、数据库表设计

### 表1：`ifind_stocklist`（A股列表 - 已有，保留）

**用途**：全市场A股基础信息 + 最新行情快照

| 字段 | 类型 | 说明 | 数据来源 |
|------|------|------|---------|
| code | TEXT PK | 股票代码 | THS_WCQuery |
| name | TEXT | 名称 | THS_WCQuery |
| market | TEXT | 市场(SH/SZ/BJ) | 计算 |
| price | REAL | 最新价 | THS_RQ |
| prev_close | REAL | 昨收 | THS_RQ |
| open | REAL | 今开 | THS_RQ |
| high | REAL | 最高 | THS_RQ |
| low | REAL | 最低 | THS_RQ |
| change_pct | REAL | 涨跌幅(%) | THS_RQ |
| volume | REAL | 成交量 | THS_RQ |
| amount | REAL | 成交额 | THS_RQ |
| turnover | REAL | 换手率(%) | THS_RQ |
| quantity_ratio | REAL | 量比 | THS_RQ |
| amplitude | REAL | 振幅(%) | THS_RQ |
| float_shares | REAL | 流通股本 | THS_RQ(floatSharesOfAShares) |
| float_mv | REAL | 流通市值 | THS_RQ(floatCapitalOfAShares) |
| total_shares | REAL | 总股本 | THS_RQ(totalShares) |
| total_mv | REAL | 总市值 | THS_RQ(totalCapital) |
| pe_ttm | REAL | 市盈率 | THS_BD |
| pb | REAL | 市净率 | THS_BD |
| fetched_at | TEXT | 最后更新时间 | 系统 |

**更新频率**：每日1次（09:00开盘前）
**数据保留**：永久保留

---

### 表2：`market_daily`（日线行情 - 已有，保留）

**用途**：历史K线数据

| 字段 | 类型 | 说明 | 数据来源 |
|------|------|------|---------|
| source | TEXT PK | 数据源 | 固定 'ths_ifind' |
| code | TEXT PK | 股票代码 | THS_HQ |
| date | TEXT PK | 交易日期 | THS_HQ |
| open | REAL | 开盘价 | THS_HQ |
| high | REAL | 最高价 | THS_HQ |
| low | REAL | 最低价 | THS_HQ |
| close | REAL | 收盘价 | THS_HQ |
| volume | REAL | 成交量 | THS_HQ |
| amount | REAL | 成交额 | THS_HQ |
| fetched_at | TEXT | 抓取时间 | 系统 |

**更新频率**：每日1次（15:05收盘后）
**数据保留**：15天（可在系统设置中调整）

---

### 表3：`ifind_realtime`（实时快照 - 新增）

**用途**：盘中实时行情快照，用于形成历史数据

| 字段 | 类型 | 说明 | 数据来源 |
|------|------|------|---------|
| code | TEXT PK | 股票代码 | THS_RQ |
| datetime | TEXT PK | 快照时间 | 系统 |
| price | REAL | 最新价 | THS_RQ |
| prev_close | REAL | 昨收 | THS_RQ |
| open | REAL | 今开 | THS_RQ |
| high | REAL | 最高 | THS_RQ |
| low | REAL | 最低 | THS_RQ |
| change_pct | REAL | 涨跌幅(%) | THS_RQ |
| volume | REAL | 成交量 | THS_RQ |
| amount | REAL | 成交额 | THS_RQ |
| turnover | REAL | 换手率(%) | THS_RQ |
| quantity_ratio | REAL | 量比 | THS_RQ |
| amplitude | REAL | 振幅(%) | THS_RQ |

**更新频率**：盘中每15分钟写入一次（09:30-15:00）
**数据保留**：7天（可在系统设置中调整）

---

### 表4：`ifind_basic_daily`（基本面指标 - 已有，保留）

**用途**：历史基本面数据（PE、PB等）

| 字段 | 类型 | 说明 | 数据来源 |
|------|------|------|---------|
| code | TEXT PK | 股票代码 | THS_BD |
| date | TEXT PK | 交易日期 | THS_BD |
| indicator | TEXT PK | 指标名称 | THS_BD |
| value | REAL | 指标值 | THS_BD |
| fetched_at | TEXT | 抓取时间 | 系统 |

**更新频率**：每日1次（15:20收盘后）
**数据保留**：15天（可在系统设置中调整）

---

## 四、定时任务时间表

```
┌──────────┬──────────────────────────────────────────────────┐
│ 时间     │ 任务                                              │
├──────────┼──────────────────────────────────────────────────┤
│ 08:30    │ 更新交易日历（ifind_calendar）                      │
│ 09:00    │ 更新A股列表（ifind_stocklist）                      │
├──────────┼──────────────────────────────────────────────────┤
│ 09:30    │ ──────── 开盘 ────────                           │
│ 09:30-   │ 每15分钟写入实时快照（ifind_realtime）              │
│ 15:00    │ 仅写入当日数据，自动清理过期数据                     │
│ 15:00    │ ──────── 收盘 ────────                           │
├──────────┼──────────────────────────────────────────────────┤
│ 15:05    │ 写入当日日线（market_daily）                       │
│ 15:20    │ 写入基本面指标（ifind_basic_daily）                 │
│ 16:30    │ 写入公告（ifind_announcements）                    │
└──────────┴──────────────────────────────────────────────────┘
```

---

## 五、页面展示策略

### 页面：A股列表（p_ifind_stocklist.py）

**数据来源**：`ifind_stocklist` 表（每日09:00更新）

**展示字段**：
- 代码、名称、现价、涨跌幅、涨跌、换手率、量比、振幅、成交额、流通股、流通市值、市盈率

**刷新策略**：
- 首次打开：检测数据是否>1天，是则自动刷新
- 手动刷新：点击按钮触发调度器任务
- 不直接调用 iFinD API

---

### 页面：实时行情（p_ifind_realtime.py）

**数据来源**：`ifind_realtime` 表（盘中每15分钟更新）

**展示字段**：
- 代码、名称、最新价、涨跌幅、成交量、换手率、量比

**刷新策略**：
- 页面显示数据库中最新快照
- 显示"数据更新于 X 分钟前"
- 手动刷新按钮触发调度器任务

---

### 页面：历史K线（p_ifind_history.py）

**数据来源**：`market_daily` 表（收盘后更新）

**展示字段**：
- 日期、开盘、最高、最低、收盘、成交量、成交额

**刷新策略**：
- 直接查询数据库
- 选择日期范围查询

---

## 六、首次部署 + 重启同步策略

### 首次部署流程

```
1. 容器启动 → 检测数据库是否为空
2. 数据库为空 → 显示"首次部署，正在初始化..."
3. 自动执行：
   a. 拉取交易日历（近1年）
   b. 拉取A股列表（当前全量）
   c. 拉取近3天日线数据（market_daily）
   d. 拉取近3天基本面数据（ifind_basic_daily）
4. 完成后显示"初始化完成，共写入 X 条数据"
5. 启动定时任务调度器
```

### 软件重启流程

```
1. 容器启动 → 检查数据库最后更新时间
2. ifind_stocklist.fetched_at > 24小时 → 显示"数据需要同步"
3. 自动执行：
   a. 更新A股列表（ifind_stocklist）
   b. 检查当日日线是否已写入，未写入则补写
4. 启动定时任务调度器
```

---

## 七、配置参数（可在系统设置中调整）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `realtime_retention_days` | 7 | ifind_realtime 保留天数 |
| `daily_retention_days` | 15 | market_daily 保留天数 |
| `basic_retention_days` | 15 | ifind_basic_daily 保留天数 |
| `announcement_retention_days` | 30 | ifind_announcements 保留天数 |
| `realtime_interval_minutes` | 15 | 实时快照写入间隔（分钟） |
| `auto_refresh_threshold_hours` | 24 | 自动刷新阈值（小时） |

---

## 八、磁盘空间估算

| 数据类型 | 每日增量 | 保留天数 | 总空间 |
|----------|---------|---------|--------|
| ifind_stocklist | 5200行 | 永久 | <1MB |
| market_daily | 5200行 | 15天 | <50MB |
| ifind_realtime | 5200×26次/天 | 7天 | <30MB |
| ifind_basic_daily | 5200行 | 15天 | <10MB |
| ifind_calendar | <1行 | 永久 | <1MB |
| ifind_announcements | ~1000行 | 30天 | <5MB |
| **总计** | | | **<100MB** |

---

## 九、阶段划分

### 阶段1（MVP，1-2天实现）

**调度器**：复用现有 `scheduler.py`，不新增容器

**表结构**：
- `ifind_stocklist` - A股列表（已有）
- `market_daily` - 日线行情（已有）
- `ifind_realtime` - 实时快照（新增）
- `ifind_basic_daily` - 基本面历史（已有）

**定时任务**：
- `sync_stocklist` - 09:00 更新A股列表
- `sync_realtime` - 09:30-15:00 每15分钟写入实时快照
- `sync_daily` - 15:05 写入日线行情
- `sync_basic` - 15:20 写入基本面数据

**页面改造**：
- `p_ifind_stocklist.py` - 改为读 `ifind_stocklist` 表
- `p_ifind_realtime.py` - 改为读 `ifind_realtime` 表
- `p_ifind_history.py` - 改为读 `market_daily` 表

### 阶段2（扩展，按需添加）

- 板块资金流：新增 `ifind_sector_flow` 表
- 指数列表：新增 `ifind_index_list` 表
- 公告：使用现有 `ifind_announcements` 表
- 高频数据：新增 `ifind_minute` 表
- 数据降级：iFinD不可用时切换到腾讯行情
- 邮件告警：调度器失败时发送邮件通知

---

## 十、文件结构

```
qsys/
├── scheduler.py                 # 现有调度器（复用，添加新任务）
├── datasource.py                # 共享数据库读写函数
├── views/
│   ├── p_ifind_stocklist.py     # 只读 ifind_stocklist
│   ├── p_ifind_realtime.py      # 只读 ifind_realtime
│   └── p_ifind_history.py       # 只读 market_daily
└── docker-compose.yml           # 无需修改（复用现有 lh-qsys）
```

---

## 十一、待实现功能（阶段2）

| 功能 | 优先级 | 说明 |
|------|--------|------|
| 数据降级 | 中 | iFinD不可用时切换到腾讯行情 |
| 邮件告警 | 低 | 调度器失败时发送邮件通知 |
| 页面配置 | 低 | 在系统设置页面调整保留天数等参数 |
| 日志查看 | 低 | 在页面上查看调度器执行日志 |
| 板块资金流 | 中 | 新增 ifind_sector_flow 表 |
| 指数列表 | 低 | 新增 ifind_index_list 表 |
