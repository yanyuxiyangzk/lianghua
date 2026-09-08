# QSYS - 量化因子演化与自动交易系统

> **从因子挖掘到实盘交易的完整闭环**：自动生成因子 → 验证评估 → 组合策略 → 自动选股 → 模拟交易 → 持续优化

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          QSYS 量化系统架构                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                 │
│   │  LoopEngine  │───▶│ Factor Eval  │───▶│ Strategy Gen │                 │
│   │  因子演化引擎 │    │  因子评估系统  │    │  策略包生成   │                 │
│   │              │    │              │    │              │                 │
│   │ · 遗传算法    │    │ · IC/ICIR    │    │ · 贪心选择    │                 │
│   │ · 多类型挖掘  │    │ · Walk-forward│    │ · 多包投票    │                 │
│   │ · 自适应预算  │    │ · 胜率体检    │    │ · 质量门槛    │                 │
│   └──────────────┘    └──────────────┘    └──────────────┘                 │
│           │                  │                  │                           │
│           ▼                  ▼                  ▼                           │
│   ┌──────────────────────────────────────────────────────────────┐         │
│   │                    因子库 (37,000+ 因子)                      │         │
│   │   · gate_status: 闸门通过  · quality_score: 质量评分          │         │
│   │   · skeleton: 骨架去重      · family: 机制族分类              │         │
│   └──────────────────────────────────────────────────────────────┘         │
│           │                                                                │
│           ▼                                                                │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                │
│   │  Pool Scan   │───▶│ Position Mgr │───▶│   Trading    │                │
│   │  板块扫描选股  │    │  持仓管理     │    │  交易执行     │                │
│   │              │    │              │    │              │                │
│   │ · 多包投票    │    │ · 止盈止损    │    │ · 竞价确认    │                │
│   │ · 行业限制    │    │ · 持仓跟踪    │    │ · 自动开平仓  │                │
│   │ · 实时更新    │    │ · 风险控制    │    │ · 模拟回填    │                │
│   └──────────────┘    └──────────────┘    └──────────────┘                │
│           │                                                                │
│           ▼                                                                │
│   ┌──────────────────────────────────────────────────────────────┐         │
│   │              Experience DB (经验库)                           │         │
│   │   · picks: 选股记录  · trades: 交易记录  · outcomes: 战果    │         │
│   │   · 失败模式库       · 策略包评分        · 因子生命周期       │         │
│   └──────────────────────────────────────────────────────────────┘         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 核心特性

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

欢迎贡献代码、报告问题或提出建议！

1. Fork 项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

---

**QSYS** - 让量化交易更智能  
*从因子到收益，全流程自动化*
