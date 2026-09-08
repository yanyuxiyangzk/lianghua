# QSYS - 量化因子演化与自动交易系统

> **从因子挖掘到实盘交易的完整闭环**：自动生成因子 → 验证评估 → 组合策略 → 自动选股 → 模拟交易 → 持续优化

---

## 什么是QSYS？

**QSYS**（Quantitative System）是一个**全流程自动化**的量化投资系统，它将传统量化研究中分散的因子挖掘、策略构建、回测验证、实盘交易等环节，整合成一个**自我进化、持续学习**的智能系统。

### 核心理念

```
传统量化研究                          QSYS 自动化研究
─────────────────────────────────────────────────────────────
人工发现因子  ──▶  人工验证  ──▶  人工组合     自动发现因子  ──▶  自动验证  ──▶  自动组合
     │                │                │             │                │                │
     ▼                ▼                ▼             ▼                ▼                ▼
  几十个因子      几周验证      几个月策略     37,000+因子     实时验证      每日更新策略
     │                │                │             │                │                │
     ▼                ▼                ▼             ▼                ▼                ▼
  手动交易        手动调整        低效复用     自动交易        持续优化      智能进化
```

### 我们的目标

| 目标 | 描述 | 实现状态 |
|------|------|----------|
| **因子自动化** | 无需人工干预，自动生成、验证、筛选因子 | ✅ 已实现 |
| **策略智能化** | 基于因子库自动构建、优化、更新策略 | ✅ 已实现 |
| **交易自动化** | 从选股到下单全流程自动化 | ✅ 已实现 |
| **学习持续化** | 从历史交易中学习，持续优化系统 | ✅ 已实现 |
| **风控系统化** | 多层次风险控制，自动止盈止损 | ✅ 已实现 |

### 我们的愿景

> **让量化交易不再依赖天才研究员，而是依靠智能系统**

传统量化研究高度依赖少数天才研究员的经验和直觉，QSYS 旨在：

1. **降低门槛**：让普通投资者也能享受专业量化策略
2. **提高效率**：将数月的研究周期缩短到数天
3. **持续进化**：系统自动学习、优化、适应市场变化
4. **风险可控**：严格的回测验证和风控体系

---

## 系统能做什么？

### 1. 自动发现Alpha因子

**问题**：传统量化研究中，发现一个有效的Alpha因子需要研究员数周甚至数月的探索。

**QSYS解决方案**：
- 基于遗传算法，自动搜索因子空间
- 每天生成数百个因子候选
- 自动验证IC、ICIR、胜率等指标
- 自动去重、分类、入库

```python
# 传统方式：人工编写因子
def my_alpha_factor(df):
    # 需要研究员的直觉和经验
    return df['close'].rolling(20).mean() / df['close'].rolling(60).mean()

# QSYS方式：自动生成因子
le_影线_5f5871 = sub(
    decay_linear(sub(decay_linear(roc(open,200),200), sign(amount)), 200),
    sign(delta(ts_min(ts_max(lower_shadow,120),20), 200))
)
# 系统自动发现这个因子在特定市场条件下有效
```

### 2. 自动构建策略组合

**问题**：即使有了有效的因子，如何组合成一个稳健的策略仍然需要大量经验。

**QSYS解决方案**：
- 贪心选择：从空集开始，每轮加入最优因子
- MMR去冗余：确保因子间的低相关性
- 多包投票：多个策略包交叉验证
- Walk-forward验证：严格的样本外测试

```python
# 策略包自动生成流程
1. 从37,000+因子库中筛选Top因子（ICIR > 0.2）
2. 贪心选择最优组合（5-8个因子）
3. Walk-forward验证（OOS胜率 ≥ 50%）
4. 质量门槛检查（OOS与实战差距 < 25%）
5. 自动入库，供选股使用
```

### 3. 自动选股与交易

**问题**：传统选股依赖人工判断，容易受情绪影响，且无法24小时监控市场。

**QSYS解决方案**：
- 多包投票：3个策略包交叉验证
- 行业限制：每个行业最多2只股票
- 实时更新：SSE推送实时行情
- 自动执行：无手动确认，自动下单

```python
# 交易流程
19:00  板块扫描选股（多包投票）
19:30  自动选股（v5因子评分）
09:26  竞价确认
09:30+ 持仓跟踪（每5分钟）
       │
       ├── 开仓条件：止盈+15% / 止损-8% / 持有20天
       └── 自动执行：无手动确认
```

### 4. 持续学习与进化

**问题**：市场在变化，过去有效的策略可能失效。

**QSYS解决方案**：
- 经验库：记录每次选股、交易、战果
- 失败模式库：101,847条失败记录，避免重复错误
- 策略包评分：OOS胜率 vs 实战胜率，持续优化
- 因子生命周期：自动退役失效因子

```sql
-- 系统自动学习
经验库 (experience_db)
├── picks: 25次选股记录
├── trades: 118笔交易记录
├── outcomes: 战果回填
└── 失败模式库: 101,847条记录
    ├── eval_error: 8,846次（因子代码bug）
    ├── overnight相关: 72.3%（隔夜风险）
    └── IC边缘失败: ~8,000次（IC在0.013-0.019）
```

---

## 系统架构

###   因子演化引擎 (LoopEngine)

**自动发现Alpha因子**：基于遗传算法，自动搜索、生成、验证量化因子

```python
# 示例：自动生成的因子表达式
le_影线_5f5871 = sub(
    decay_linear(sub(decay_linear(roc(open,200),200), sign(amount)), 200),
    sign(delta(ts_min(ts_max(lower_shadow,120),20), 200))
)
```

- **多类型因子挖掘**：量价、资金流、板块轮动、龙虎榜、盘口异动、指数
- **自适应预算**：根据成功率动态调整算子权重
- **FSA失败模式库**：自动记录失败骨架，避免重复尝试
- **硬闸门验证**：IC/ICIR/胜率多重过滤

###   因子评估系统

**严格的因子验证流程**：

```
因子候选 → 闸门检查 → Walk-forward验证 → 胜率体检 → 入库评分
   │           │              │              │          │
   │           │              │              │          └── 5维评分
   │           │              │              └── 1/5/20/60/120日多周期
   │           │              └── 样本外真实表现
   │           └── IC > 0.02, IC_winrate > 50%
   └── 去重、骨架分类、机制族归属
```

**5维因子评分**：
- IC得分 (30%): |IC| × IC_winrate
- 稳定性 (25%): IC胜率
- 一致性 (20%): Top组胜率
- 使用度 (15%): 被策略包引用次数
- 新鲜度 (10%): 最近更新时间

###   策略包自动生成

**从因子到策略的自动化流水线**：

1. **因子发现**：从37,000+因子库中筛选Top因子
2. **组合构建**：贪心选择 + MMR去冗余
3. **Walk-forward验证**：样本外胜率 ≥ 50%
4. **质量门槛**：OOS胜率 + 实战差距 < 25%
5. **自动入库**：保存到strategies表

**多包投票机制**：
```python
# Top3策略包投票
pack1_stocks = compute_pack_picks(alpha101, codes)  # Alpha101精华
pack2_stocks = compute_pack_picks(short5, codes)    # 短线5日
pack3_stocks = compute_pack_picks(stable, codes)    # 稳健低波

# 取至少2票的交集
final_stocks = intersection(pack1, pack2, pack3)
```

###   自动选股与交易

**完整的交易闭环**：

```
19:00  pool_scan     → 板块扫描选股（多包投票）
19:30  auto_scan     → 自动选股（v5因子评分）
09:26  auction_confirm → 竞价确认
09:30+ position_track → 持仓跟踪（每5分钟）
        │
        ├── 开仓条件：止盈+15% / 止损-8% / 持有20天
        └── 自动执行：无手动确认
```

**v5自动选股算法**：
- 因子价值评分 (25%): backtest_winrate
- 实盘胜率 (25%): Bayesian shrinkage + time decay
- 策略包投票 (15%): 被多少策略包引用
- 综合评分 (10%): 5维因子评分
- 稳定性 (8%): IC胜率
- 动量 (7%): 最近表现

###   实时数据流

**SSE无感刷新**：

```javascript
// 前端实时更新分时图
const eventSource = new EventSource(`/market/events?code=${stockCode}`);
eventSource.onmessage = (event) => {
    const tick = JSON.parse(event.data);
    Plotly.extendTraces('chart', {x: [[tick.time]], y: [[tick.price]]}, [0]);
};
```

**数据源**：
- Qlib本地数据（回测同源）
- 腾讯API（实时行情）
- iFinD（机构级数据）
- SSE推送（3秒/30秒更新）

###   经验学习系统

**从历史中学习**：

```sql
-- 失败模式库
failure_patterns: 101,847条记录
  · eval_error: 8,846次（因子代码bug）
  · overnight相关: 72.3%（隔夜风险）
  · IC边缘失败: ~8,000次（IC在0.013-0.019）

-- 策略包评分
strategies: 20个策略包
  · Alpha101精华_v1: OOS=86%, 实战=47% (过拟合)
  · 短线5日_v1: OOS=58%, 实战=50% (最稳健)
  · LE_沪深300_0908: OOS=50% (自动生成)
```

---

## 系统优势

### 为什么选择QSYS？

| 传统量化系统 | QSYS | 优势 |
|-------------|------|------|
| 人工发现因子 | 自动演化因子 | 效率提升100倍 |
| 几十个因子 | 37,000+因子库 | 覆盖面更广 |
| 手动组合策略 | 自动构建策略 | 降低人为偏差 |
| 月度更新 | 每日更新 | 更快适应市场 |
| 人工交易 | 自动交易 | 7×24小时监控 |
| 无学习能力 | 经验学习系统 | 持续优化 |

### 核心竞争力

```
1. 全流程自动化
   ┌─────────────────────────────────────────────────────────┐
   │  因子发现 → 因子验证 → 策略构建 → 选股交易 → 战果回填   │
   │      │          │          │          │          │      │
   │      └──────────┴──────────┴──────────┴──────────┘      │
   │                    全程无需人工干预                       │
   └─────────────────────────────────────────────────────────┘

2. 智能进化
   ┌─────────────────────────────────────────────────────────┐
   │                    经验学习循环                          │
   │  ┌──────────┐     ┌──────────┐     ┌──────────┐        │
   │  │ 交易记录  │────▶│ 战果分析  │────▶│ 策略优化  │        │
   │  └──────────┘     └──────────┘     └──────────┘        │
   │       ▲                               │                 │
   │       └───────────────────────────────┘                 │
   │                    持续自我优化                          │
   └─────────────────────────────────────────────────────────┘

3. 风险控制
   ┌─────────────────────────────────────────────────────────┐
   │  多层次风控体系                                          │
   │  ├── 因子层: IC/ICIR硬门槛                              │
   │  ├── 策略层: Walk-forward验证                            │
   │  ├── 选股层: 多包投票 + 行业限制                         │
   │  ├── 交易层: 止盈止损 + 持仓限制                         │
   │  └── 系统层: 实时监控 + 异常告警                         │
   └─────────────────────────────────────────────────────────┘
```

### 实际效果

**系统运行数据**（截至2026年9月）：

| 指标 | 数值 | 说明 |
|------|------|------|
| 因子总数 | 37,181 | 自动发现 |
| 评分因子 | 1,716 | 通过闸门验证 |
| 策略包 | 20个 | 自动构建 |
| 失败模式 | 101,847条 | 经验积累 |
| 最佳策略包 | OOS 86% | Alpha101精华 |
| 最稳健策略包 | OOS 58%, 实战 50% | 短线5日 |

---

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| **因子演化** | Python + 遗传算法 | LoopEngine自适应搜索 |
| **因子评估** | Walk-forward + IC/ICIR | 严格的样本外验证 |
| **策略构建** | 贪心 + MMR + 多包投票 | 自动组合优化 |
| **数据存储** | SQLite (market.db + experience.db) | 轻量级本地存储 |
| **实时推送** | SSE (Server-Sent Events) | 无感刷新分时图 |
| **前端展示** | Streamlit + Plotly | 交互式可视化 |
| **定时调度** | APScheduler | 3进程并行调度 |
| **容器化** | Docker | 一键部署 |

## 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/yanyuxiyangzk/lianghua.git
cd lianghua

# 配置环境变量（需要DeepSeek API Key）
cp .env.example .env
# 编辑 .env 填入 API Key
```

### 2. 启动系统

```bash
# 启动所有服务
docker compose up -d

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f qsys
```

### 3. 访问看板

| 服务 | 地址 | 说明 |
|------|------|------|
| **QSYS看板** | http://localhost:8501 | 主界面 |
| **SSE服务** | http://localhost:8502 | 实时数据推送 |
| **RD-Agent UI** | http://localhost:19899 | 因子演化监控 |

### 4. 核心功能

```bash
# 手动触发因子演化
docker exec lh-qsys python3.11 -c "
import sys; sys.path.insert(0, '/app')
from loopengine.engine import LoopEngine
engine = LoopEngine(pool_name='沪深300')
result = engine.run_round(batch=30)
print(result)
"

# 手动触发策略包生成
docker exec lh-qsys python3.11 -c "
import sys; sys.path.insert(0, '/app')
from scheduler import job_strategy_gen
print(job_strategy_gen())
"

# 手动触发选股
docker exec lh-qsys python3.11 -c "
import sys; sys.path.insert(0, '/app')
from scheduler import job_pool_scan
print(job_pool_scan())
"
```

## 系统架构

### 容器架构

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │   lh-qsys   │  │ lh-rdagent  │  │  lh-ollama  │    │
│  │             │  │             │  │             │    │
│  │ · Streamlit │  │ · RD-Agent  │  │ · bge-m3    │    │
│  │ · Scheduler │  │ · 因子演化   │  │ · Embedding │    │
│  │ · LoopEngine│  │ · Qlib回测   │  │             │    │
│  │ · SSE Server│  │             │  │             │    │
│  └─────────────┘  └─────────────┘  └─────────────┘    │
│       :8501           :19899          :11434           │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 数据流

```
┌─────────────────────────────────────────────────────────┐
│                      数据流向                            │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  市场数据 ──┬──▶ Qlib本地数据 ──▶ 回测引擎              │
│             │                                          │
│             ├──▶ 腾讯API ──▶ 实时行情 ──▶ SSE推送      │
│             │                                          │
│             └──▶ iFinD ──▶ 机构数据 ──▶ 分析层         │
│                                                         │
│  因子数据 ──┬──▶ factor_scorecards ──▶ 评分系统         │
│             │                                          │
│             ├──▶ factor_registry ──▶ 因子库             │
│             │                                          │
│             └──▶ failure_patterns ──▶ 失败模式库       │
│                                                         │
│  交易数据 ──┬──▶ picks ──▶ 选股记录                    │
│             │                                          │
│             ├──▶ trades ──▶ 交易记录                   │
│             │                                          │
│             └──▶ outcomes ──▶ 战果回填                 │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## 核心算法

### 1. 因子演化 (遗传算法)

```python
# 伪代码
while iteration < max_iterations:
    # 1. 生成候选因子
    candidates = generate_candidates(field_weights, momentum)
    
    # 2. 规则审查
    for candidate in candidates:
        if review(candidate):  # 语法、复杂度、重复检查
            # 3. 硬闸门验证
            result = evaluate_gates(candidate, panel)
            if result['pass']:  # IC > 0.02, IC_winrate > 50%
                # 4. 入库
                sync_factor_registry(candidate)
    
    # 5. 更新字段权重
    field_weights.boost_from_factors(accepted_factors)
```

### 2. Walk-forward验证

```python
# 样本外验证
for t in range(est, len(days) - fwd_days, step):
    # 估计窗：[t-est, t-fwd]
    stats = compute_ic_stats(ic_series, est_lo, est_hi)
    
    # 计算权重
    weights = compute_weights(stats, method='ICIR')
    
    # 应用窗：t截面打分
    scores = composite_score(factor_vals, weights)
    picks = scores.nlargest(top_n)
    
    # 记录超额收益
    excess = returns[picks].mean() - returns.median()
```

### 3. 多包投票

```python
# 3个策略包投票
def multi_pack_voting(packs, codes, top_n):
    all_picks = {}
    
    # 每个包独立选股
    for pack in packs:
        picks = compute_pack_picks(pack, codes)
        for stock in picks:
            all_picks[stock] = all_picks.get(stock, 0) + 1
    
    # 取至少2票的股票
    voted = [s for s, n in all_picks.items() if n >= 2]
    
    # 用最佳包的分数排序
    best_scores = compute_pack_picks(packs[0], codes)
    return best_scores[voted].nlargest(top_n)
```

## 配置说明

### 环境变量 (.env)

```bash
# LLM配置
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_MODEL=deepseek-chat

# 因子演化
QLIB_FACTOR_EVOLVING_N=10
QLIB_DOCKER_RUNNING_TIMEOUT_PERIOD=3600

# 交易参数
TAKE_PROFIT=0.15
STOP_LOSS=-0.08
HOLD_DAYS=20
```

### 定时任务

| 任务 | 时间 | 说明 |
|------|------|------|
| pool_scan | 19:00 | 板块扫描选股 |
| auto_scan | 19:30 | 自动选股 |
| auction_confirm | 09:26 | 竞价确认 |
| position_track | 09:30+ | 持仓跟踪（5分钟） |
| le_factor_eval | 12:30/18:00/21:30 | 因子体检 |
| strategy_gen | 18:30 | 策略包自动生成 |

---

## 未来愿景

### 短期目标（3-6个月）

| 目标 | 描述 | 优先级 |
|------|------|--------|
| **因子覆盖率提升** | 从4.6%提升到50% | P0 |
| **策略包自动生成** | LoopEngine整合，每轮自动生成 | P0 |
| **多包投票优化** | 动态权重，基于实战表现调整 | P1 |
| **风控体系完善** | 增加最大回撤、夏普比率等指标 | P1 |

### 中期目标（6-12个月）

| 目标 | 描述 | 优先级 |
|------|------|--------|
| **多市场支持** | 扩展到港股、美股 | P2 |
| **机器学习集成** | 引入深度学习因子 | P2 |
| **实盘接入** | 对接券商API，真实交易 | P2 |
| **移动端支持** | 手机APP监控 | P3 |

### 长期愿景（1-3年）

```
┌─────────────────────────────────────────────────────────────────┐
│                      QSYS 3.0 愿景                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  智能因子    │    │  自适应策略  │    │  全自动交易  │      │
│  │              │    │              │    │              │      │
│  │ · NLP因子    │    │ · 市场状态   │    │ · 多市场     │      │
│  │ · 另类数据   │    │ · 动态调整   │    │ · 多策略     │      │
│  │ · 实时演化   │    │ · 风险自适应 │    │ · 智能风控   │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│                                                                 │
│  目标：成为个人投资者的"量化研究员"                              │
│  使命：让量化交易更智能、更简单、更普惠                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 技术路线图

```
2026 Q3-Q4                              2027 Q1-Q2
─────────────────────────────────────────────────────────────────
✅ 因子演化引擎                          □ 多市场支持
✅ 因子评估系统                          □ 机器学习集成
✅ 策略包自动生成                        □ 实盘接入
✅ 多包投票机制                          □ 移动端支持
✅ 自动选股交易                          □ 云端部署
✅ 经验学习系统                          □ 机构级功能
```

---

## 常见问题

### Q: 如何查看因子演化日志？

```bash
# 查看实时日志
docker compose logs -f qsys | grep "LoopEngine"

# 查看历史日志
docker exec lh-qsys cat /data/scheduler_history.jsonl
```

### Q: 如何手动触发选股？

```bash
docker exec lh-qsys python3.11 -c "
import sys; sys.path.insert(0, '/app')
from scheduler import job_pool_scan
print(job_pool_scan())
"
```

### Q: 如何查看策略包评分？

```bash
docker exec lh-qsys python3.11 -c "
import sys; sys.path.insert(0, '/app')
import library
packs = library.list_strategies()
for name, pk in packs.items():
    print(f'{name}: OOS={pk.get(\"oos_winrate\")}, method={pk.get(\"method\")}')
"
```

### Q: 如何查看失败模式？

```bash
docker exec lh-qsys python3.11 -c "
import sqlite3
c = sqlite3.connect('/data/market.db')
rows = c.execute('''
    SELECT pattern, COUNT(*) as cnt
    FROM failure_patterns
    GROUP BY pattern
    ORDER BY cnt DESC
    LIMIT 10
''').fetchall()
for pattern, cnt in rows:
    print(f'{pattern}: {cnt}次')
"
```

## 贡献指南

我们欢迎各种形式的贡献！

### 如何贡献

```
1. Fork 项目
2. 创建特性分支 (git checkout -b feature/amazing-feature)
3. 提交更改 (git commit -m 'Add amazing feature')
4. 推送到分支 (git push origin feature/amazing-feature)
5. 创建 Pull Request
```

### 贡献类型

| 类型 | 说明 | 示例 |
|------|------|------|
| **代码贡献** | 新功能、Bug修复、性能优化 | 添加新的因子类型 |
| **文档完善** | 改进文档、添加示例 | 完善API文档 |
| **问题反馈** | 报告Bug、提出建议 | 提交Issue |
| **测试验证** | 帮助测试新功能 | 验证策略效果 |
| **社区讨论** | 分享经验、交流想法 | 参与讨论 |

### 开发环境

```bash
# 克隆项目
git clone https://github.com/yanyuxiyangzk/lianghua.git
cd lianghua

# 启动开发环境
docker compose up -d

# 运行测试
docker exec lh-qsys python3.11 -m pytest

# 代码检查
docker exec lh-qsys python3.11 -m flake8 qsys/
```

## 社区

### 联系方式

- **GitHub Issues**: [github.com/yanyuxiyangzk/lianghua/issues](https://github.com/yanyuxiyangzk/lianghua/issues)
- **Email**: [your-email@example.com](mailto:your-email@example.com)

### 致谢

感谢以下开源项目和贡献者：

- **RD-Agent**: 微软研究院的因子演化框架
- **Qlib**: 微软的量化投资平台
- **Streamlit**: 快速构建数据应用
- **Plotly**: 交互式可视化库

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

---

**QSYS** - 让量化交易更智能  
*从因子到收益，全流程自动化*

```
Star历史
─────────────────────────────────────────────────────────────
[![Star History Chart](https://api.star-history.com/svg?repos=yanyuxiyangzk/lianghua&type=Timeline)](https://star-history.com/#yanyuxiyangzk/lianghua&Timeline)
```
