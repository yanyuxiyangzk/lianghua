# 特征归一化改进方案 v2（合成打分层）——已过专家评审

> **实施进度（2026-09-16）**：第 0 阶段（composite.py 前视修复）✅；cs_norm/resolve_norms ✅（14 测）；接线 ✅（全部打分路径含归因，legacy 开关默认不变行为，20 测）；A/B 回放脚本 ✅（`scripts/ab_norm_replay.py`，3 个合成数据冒烟测）。**待办：容器内跑 A/B 报告 → 人工审阅 → 灰度切换（写 norm_scheme.json + 清 SIGNALS_DIR 扫描缓存）。**

> 背景：对照"特征归一化"主题视频体检后确认——本系统因子选拔（RankIC，尺度不变）与 RD-Agent 闭环（LGBM 树模型）天然免疫量纲问题；缺口集中在**多因子合成打分层**对重尾因子一刀切 Z-Score、小截面稳健性，以及评审发现的**一条被遗漏的第三条打分路径（带现存前视 Bug）**。
> v2 已并入专家评审（有条件通过）的全部必修项与裁决。

## 第 0 阶段：先修现存 Bug（独立于本方案，最优先）

**`composite.py:78` 全历史 Z-Score 前视**（评审发现，已人工复核确认）：

```python
z = (v - v.mean()) / (v.std() + 1e-12) * direction   # v 是整条 ~800 日长序列
```

- `v` 为全历史长表，均值/方差用了**全样本**——复合因子历史值被未来数据污染；紧接着 `ic_series(comp…)` / `_overall_sharpe(comp…)`（composite.py:85-86）把污染序列拿去做 IC/夏普验证，"Top5复合因子"包的广告指标带前视、系统性虚高。
- 上一行 `cross` 算了却未使用（死变量）——作者本意是截面 z，写成了全局 z。
- **修法**：改为逐日截面归一化（复用本方案的 `cs_norm`），删除死变量；此修复先于一切落地，且是红线 1 入档的前提（否则 CLAUDE.md 写明禁止的事，代码里正犯着）。

## 现状（关键事实，评审已逐条核实）

- 合成打分主路径两处，调用方共用：
  - `signals.py:554` `zscore()`（clip ±3），`signals.py:559` `composite_score()`
  - `factor_eval.py:993` `_score_at()`（walk_forward/static_backtest/包回放共用）
- **第三条路径**：`composite.py:69-82` build_top5_composite（见第 0 阶段）。
- **平行实现**：`signals.py:899` `factor_contributions`（上榜归因）直调 `zscore`，**不经过** composite_score——必须显式接线，否则切换后"为什么选它"的分解值与综合分对不上。
- 明确不动的归一化：`density_sr.py:418` sr_entry 内部合成（因子构造层，z-of-z 无害）、`signals.py:671-674/764/772` 过滤层截面 z（非打分）。
- `factor_registry.factor_type` 实测分布：量价 36244 / 爆量抢筹 585 / 盘口异动 785 / 资金流 673 / 龙虎榜 127 / 板块轮动 117 / 指数 100 / 财务 24 / 事件记忆 17 / 支撑阻力 17；builtin+tech 均有记录（95 条），sr_entry 等实为'量价'类型（碰巧都落 zscore，结果不错但说明落地前必须 dry-run 全类型分布）。
- `factor_eval._norm` 只做索引归一——**不改名**（gates.py:53/80/392、composite.py:74、strategy_backtest.py:73 共 5 处外部调用），只补 docstring。
- 惰性 import 链已验证无环（signals→library 函数级；library 顶层只依赖 common/datasource）。
- scipy 容器可用、本地 venv 无 → 不引入硬依赖。

## 缺口 1：按因子类型分派归一化方式 ★核心

### cs_norm 语义（按专家裁决 1 定稿）

```
cs_norm(cross: Series, method: str) -> Series
  method="zscore" : 现状（(x-μ)/σ, clip ±3）
  method="rank"   : 截面 rank(pct=True) → 对秩本身做 z-score → clip(±3)
```

专家裁决理由（取代 v1 的线性 (p−0.5)×2√3 定标）：
1. **中心化偏差**：rank(pct=True) 值域 (0,1]，E[p]=(N+1)/2N，线性定标列均值 = +√3/N——N=15 时 +0.115，与缺口 2（小截面降级 rank）互相打架；秩的 z 分天然居中，免疫。
2. **ties 方差缩水**：事件记忆/龙虎榜计数大量并列使 rank 实现 σ≪1，因子被系统性欠权；秩的 z 分按当日实现离散度自动重标定，免疫。
3. **尾部对称**：线性 rank 输出 ±1.73 vs zscore ±3，Top-N 选拔由右尾驱动，名义权重相等但头部影响力不等；两者同 clip ±3 后对称。
4. 已知代价：99 并列 + 1 离群时离群者 z≈9.9 被 clip 到 3，与 zscore 列同待遇，可接受。
5. INT（rank→Φ⁻¹）作为 `rank_gauss` 第三选项预留，本期不做（二阶改进，不引 scipy）。

**N<3 熔断（专家裁决 2，新增）**：截面有效票数 N∈{1,2} 时该因子当日判无效（置 NaN 剔除）——rank N=1 会白送满分榜一，zscore N=1 时 std(ddof=1)=NaN 列静默消失，两种失败模式都不可接受。

### 分派与元数据

- `library.resolve_norms(names) -> dict[name, method]`：registry.norm 列（人工覆盖） > factor_type 默认映射 > 兜底 zscore。registry 无记录时按 NAME2CAT/kind 推断，**不允许报错**；函数级**按日缓存**（_lconn 每次连接跑全量迁移检查，UI 每次点击不能都走一遍）。
- factor_registry 迁移：`("norm", "TEXT")` 加入 `_lconn` 迁移块（NULL=自动，不回填）。
- factor_type 默认映射：**rank** ← 财务/资金流/龙虎榜/盘口异动/爆量抢筹/事件记忆；**zscore** ← 量价/板块轮动/指数/支撑阻力/tech。落地前先对 registry 全量 dry-run 打印分派结果人工过目。
- 明确声明：norm 映射**非 PIT**（回放时读的是当前 registry），以 pack 快照为准——写入文档与 docstring。

### 接线（覆盖全部打分路径）

1. `signals.composite_score(..., norms=None)`：None 时惰性 import library 解析；library 不可用静默兜底全 zscore。
2. `signals.factor_contributions`（signals.py:899）：改走同一 cs_norm 分派（**必修**，评审打回项）；docstring"同口径（z-score)"改为"同口径（cs_norm 分派）"，切换说明告知用户贡献值域含义变化。
3. `factor_eval._score_at` 加 `norms` 参数；walk_forward/static_backtest/包回放（:1472）入口解析一次传入。
4. `strategy_backtest.py:70` 段入口解析一次。
5. `composite.py` 第 0 阶段修复后复用 cs_norm。

### 可复现性与口径版本（专家裁决 4）

- pack payload `factors[]` 增加 `"norm"` 快照 + 顶层 `"norm_scheme": "typed_v2"`；快照语义为**"规则 + 打包时池规模解析结果"**（小截面降级使 norm 是池大小的函数，换池复用包时需按包内池规模重解析，不是固定字符串）。
- `experience.save_pick` 落库随 final_scores 记 norm_scheme（实战归因按包名累积，norm 切换是新增口径轴，engine.py:719-720 已有同类混杂教训）。
- `walk_forward_log` 加 scheme 列（或 run_id 打标），否则切换前后历史行不可比且无迹可查。

## 缺口 2：小截面降级（按专家裁决 2 定稿）

- 静态阈值 **N<30** 降级 rank（常量 `CS_ZSCORE_MIN_N`，可配）；**不要** P20 自适应（非平稳、回放不可复现、快照语义变浑）。
- N<3 熔断（见上）。
- 涨停观察 14:00 盘中场（scheduler.py:1465）小截面是真实受益场景。

## 缺口 3：红线入档（第 0 阶段修复完成后）

CLAUDE.md 三条，指向本文档：
1. 截面归一化只用当天横截面，禁止全历史均值方差时序 z 化后参与合成/IC（现存反例 composite.py:78，先修后立）。
2. 时序归一化只用滚动过去窗。
3. 未来引入线性/神经网络类模型时才补全套流水线（按列分类缩放 + 每折内拟合 + scaler 落盘复用）；当前 LGBM 不需要。

## 验证方案（切换前的硬闸门，按评审修正）

1. **单测**：
   - 断言改为**逐日 Spearman=1 或相差正常数倍**——composite_score 末尾多除 w_total（signals.py:580），与 _score_at 按字面断言相等必然失败（v1 测试规格自身有 Bug）。
   - `tests/test_standalone.py:35-37` 把 signals 整个 mock 成 fetch_panel + zscore：mock 必须同步加 `cs_norm`（含 clip 语义），否则两个 walk_forward 测试直接 AttributeError——library 兜底救不了 signals mock 缺函数。
   - 重尾分布极值票影响力有界（≤3w）；N<30 降级；N<3 熔断；registry 缺记录/library 不可用兜底。
   - ties 日 rank 列实现 σ 不被压缩（裁决 1 的核心收益，须被度量）。
2. **回放 A/B**（scripts/ 一次性脚本，markdown 报告）：
   - legacy vs typed_v2 双跑在役包 walk_forward：OOS 胜率、扣费超额、逐日分 Spearman、Top-N 重合度。
   - **新增闸门指标：各因子列实现 σ 的逐日分布**——本方案立论是"拉平有效权重"，必须度量而非假设。
   - 顺带用新口径重估在役包 OOS 胜率（_best_pack 的 OOS≥55% 门槛面对的是 legacy 口径历史数字）。
   - 人工审阅通过才允许切换。
3. **灰度与回滚**：
   - 开关落点：DATA_DIR 下 JSON（沿用 `common.load_json`，仓库无 settings 模块）；解析顺序 **包快照 > 开关 > 自动映射**。
   - 调度器长驻进程**按任务重解析**（双进程 owner 锁同族坑，避免切换后当天任务仍用旧映射）。
   - **切换日清 SIGNALS_DIR 扫描缓存**（scan_{pool}_{end}.parquet 无 scheme 键，同日文件会被当新鲜缓存展示旧口径名单，tab_sched.py:103 按 mtime 读）；因子值缓存是原始值，无需失效。
   - 回滚 = 开关切回 legacy，无数据迁移依赖。

## 落地顺序（评审建议）

1. **修 composite.py:78 前视**（独立 Bug，先行）
2. 按裁决定稿 cs_norm 语义（rank→z→clip、N<3 熔断）
3. 接线含 factor_contributions；更新 test_standalone mock
4. registry dry-run 全类型分派过目
5. 回放 A/B 报告 → 人工审阅
6. 灰度切换（清扫描缓存、记 norm_scheme）

## 工作量与影响面

- 核心 ~350 行：signals.py（cs_norm+分派+归因接线）、library.py（resolve_norms+迁移+按日缓存）、factor_eval/strategy_backtest/composite/engine 接线、单测、回放脚本。
- 不改：挖掘引擎、体检管线、RD-Agent 闭环、树语法、现存因子定义。
- 风险已列档：存量策略历史指标换口径不可比（A/B 报告重估）、归因值域含义变化（切换说明）、调度器生效时点（按任务重解析）。
