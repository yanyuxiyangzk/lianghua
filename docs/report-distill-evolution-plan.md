# 战报蒸馏 → 因子进化 方案 v2（已过专家评审）

> 目标：把每日量化战报的 LLM 分析结果**结构化**后接入因子生成，作为进化条件之一。
> v2 并入专家评审（有条件通过）的 3 项必修 + 6 项高价值补充。
> **实施进度（2026-09-16）**：蒸馏侧+shadow+prompt 层已落地（默认 shadow 模式，引擎行为不变）——`evolution_signals` 表、蒸馏 job（18:55，outcome_backfill 后）、5 路原料采集、退化检测、平衡括号抽取、schema 校验（support 标签/insufficient_evidence/boost clamp）、引擎 shadow 挂钩（只记快照）、prompt 层（mode=prompt 时启用，hypotheses 分槽）、record_tested 打标（signal_id）、shadow_eval.py 验收脚本。测试 11 个，全量 70 测绿。待办：shadow 跑满一周 → shadow_eval 验收（偏置方向 vs 实际过闸族分布 rank 相关）→ 开 prompt 层 → 结构层（全局 cap×2+熵监控，未开工）。

## 现状（已核实的事实）

- 战报原文 ≤900 字直接截断塞进 LLM 出题 prompt(`engine._llm_generate`)，只覆盖 LLM 生成源；变异/交叉/扰动/随机路径无证据输入；
- `regime.detect_regime_from_reports` 是死代码（零真实调用）；
- 战报的结构化原料（factor_leaderboard）已经通过 `family_live_stats` 接进引擎——蒸馏层的增量价值**不是排行榜**，而是归因叙事/跨表综合/前瞻假设；
- Budget 实为 **7 个预算槽位但 `_gen_candidate` 只映射 5 种行为**（mutate_op/perturb_field 选中后静默走 random_tree 却按原名记账——既有 Bug，与本案无关但按源分层度量时要留意）;
- 涨停对话页内容零消费（二期预留）;
- **时序陷阱（评审必修 1）**:`job_daily_report` 18:35 跑在 `outcome_backfill` 18:45 **之前**——战报里的 leaderboard 用的是前一日战果。蒸馏若挂在战报后 = 天天蒸馏一天前的归因。

## 核心设计：蒸馏层

```
战报原文 + 多源结构化原料 ──LLM 蒸馏(每日1次)──> evolution_signals 表(严格 schema)
        │                                              │
        │        ┌── 阶段1 文本层:替换 prompt 里 900 字原文为紧凑信号
        │        ├── 阶段2 结构层:族/字段/类型偏置(全局乘数 cap,非单路 cap)
        │        └── 阶段3 事件轮:涨停类假设路由 run_event_round
        └──── 度量闭环:过闸率 lift(Wilson 区间/按源分层)+ 一致性自检 + 族分布熵监控
```

### ① 蒸馏 job（挂载点已修正：**outcome_backfill 之后**，约 18:55）

**输入清单**（评审补充 3，每项一行 SQL、只进 top-N 摘要）:
- 战报原文（当日）+ 从 DB **重取**的 factor_leaderboard/pack_leaderboard（不用战报落库时的快照——那是回填前算的）;
- `limit_up_watch` 当日清单摘要（17:25 盘后+14:00 盘中两场，首板/连板/开板回封模式——hypotheses 最肥的原料）;
- `sr_scan_daily` 共振 Top、板块资金流 Top/Bottom（板块轮动/支撑阻力族的 steer 证据）;
- `chat_contexts` 当日数据包索引（纯结构化，无 LLM 二手失真——即使 chat 源二期才接，数据包一期就进）;
- **退化检测（评审补充 5）**:LLM 不可用时战报落库的是 `"⚠️ LLM 服务不可用…"` 错误提示——蒸馏侧必须识别退化文本并跳过，否则把错误提示蒸馏成信号。

**schema(v2 修订）**:

```json
{
  "date": "2026-09-16",
  "report_date": "2026-09-16",
  "insufficient_evidence": false,
  "regime_hint": "bull|bear|sideways|transition|null",
  "effective": [{"target": "族名/类型/字段", "evidence": "一句话", "support": "data|narrative|hypothesis"}],
  "decaying":  [{"target": "...", "evidence": "...", "support": "data|narrative|hypothesis"}],
  "hypotheses": ["自由文本机制假设，1~3 条"],
  "steer": {"families_boost": {"动量": 0.3}, "fields_boost": {}, "types_boost": {}},
  "confidence": 0.0
}
```

修订点（评审 schema 意见）:
- **删 per-item strength，改 `support` 标签**(data/narrative/hypothesis)——LLM 对"证据多强"和"整体把握多大"没有稳定区分能力，strength/confidence 会被填成一团；support 可编程校验（data 类必须能对上 leaderboard 数字），同时显化了不确定性来源；
- 加 `insufficient_evidence` 顶层字段并在蒸馏 prompt 明示"宁空勿编"（模型有硬编条目填满的倾向）;
- 记 `report_date`，消费端校验 `report_date == get_last_trade_day()` 才启用结构层——长假后"昨日战报"其实是一周前的（交易日语义见运营节）;
- 落库带 `norm_scheme` 列（归一化口径轴，lift 分层用，见耦合节）。

**蒸馏侧 JSON 抽取（评审必修 2）**:`llm_review._extract_json` 是扁平正则 `\{[^{}]*\}`,**抓不了嵌套的 steer**——需平衡括号抽取器（从首个 `{` 按深度扫到配平 `}`，复用 `_extract_sexpr` 的深度计数思路）;temperature=0 与"失败=当天无信号"的 fail-quiet 语义直接搬。

### ② 消费点（分阶段；负面清单先于正列表写死）

**绝不该碰（评审补充 4——信号是出题层，验证层永不可达）**:

| 组件 | 为什么不可碰 |
|---|---|
| `Budget.p` 直改 | 只能经 record 自然适应，直改破坏自适应账 |
| FSA 冻结/解冻、failure_patterns | 失败拦截是验证层 |
| factor_decay 权重、risk_guard 熔断 | 风控生命线 |
| `_try_generate_pack` 门槛（score>0.2 / OOS 胜率 0.50)、outcomes 归因口径 | 打包验证层 |

**正列表**:
| 阶段 | 消费点 | 机制 |
|---|---|---|
| 1 文本层 | LLM 出题 prompt 证据段 | 原文 900 字 → 蒸馏 JSON 紧凑渲染；**hypotheses 与 `_family_fewshots` 分槽渲染**（fewshots=已验证结构范例，hypotheses=待验证机制想法，混排会让模型误以为假设也是已验证口味——评审补充） |
| 2 结构层 | 族选择 / `FieldWeights` / 类型轮转 | steer 加权；**全局乘数 cap ×2**（相对无偏置基线，非各环节各自 cap——见风险修正） |
| 3 事件轮 | hypotheses 涨停/事件模式 → run_event_round 族权重偏置 | 关键词路由；停牌日 fwd 标签稀释问题三期再处理 |
| 互证 | regime_hint × 价格法 detect_regime 投票 | 一致→置信度上调；分歧→降当日 LLM 出题预算；**一致率本身记为影子期指标** |

### ③ 护栏（回音壁 + 三重同向放大——评审风险修正）

核实出的叠加链路：`family_live_stats`(proven 族+父本适应度）× `FieldWeights`（入库频次倾斜，×4 封顶）× `Budget`（源胜率自适应）——**三者全由"近期成功"驱动**，蒸馏 steer 是第四个同向信号，原方案的单路 ±50% cap 不够：

- **族偏置合成后设全局乘数 cap ×2**（各环节各自 cap 之外的总闸）;
- **族分布熵周监控**：熵连续 2 周下降 → 自动减半所有 boost（回音壁的提前量，比 lift=0 的滞后判据快）;
- 信号有效期 **3 个交易日**，按交易日历算（`common.trade_day_offset` 现成——自然日算法撞上国庆就全过期了）;
- confidence < 0.4 或 insufficient_evidence=true → 只进文本层；
- 信号错了的代价永远只是浪费挖掘预算——闸门不放水。

### ④ 度量闭环（v2 加固，评审补充 6/9）

**打标落库具体化**:`tested_hashes` 加 `signal_id` 可空列（关联 evolution_signals.date);shadow 期 `evolution_signals` 加 `shadow_bias_json` 列（记录当日若启用会产生的族/字段权重快照）。

**shadow 期量化验收标准**（评审必修 3——"人工过目一周"不够）:
- shadow_bias 的族偏置方向与**当日实际过闸因子族分布**的 rank 相关 > 0，且一周中多数日为正 → 信号有信息量，才允许开阶段 1;
- regime_hint 与价格法 regime 的一致率（记录即指标）。

**lift 度量的统计陷阱**（写死进周报脚本）:
- 引导组每周样本可能只有几十条 → **Wilson 置信区间 + 最小样本**（引导组 ≥30 条过闸评估）才允许下结论，否则"4 周 lift≈0 自动降级"会被噪声触发；
- **按生成源分层对比**（llm 源引导期 vs llm 源无信号期），全池混合比无效（Budget 同时在改源构成）;
- **预先指定唯一主指标**：过闸率 lift；族/字段命中率仅作诊断，不做判据（防多重比较假阳性）;
- **基线窗口取信号生效前的等长窗口**（family_live_stats 的 20/5/1 回退结算与生效期重叠会污染基线）。

**LLM² 一致性自检**（评审补充 5，每周出 violation 率）:
- effective/decaying 里点名因子的声称方向 vs 当日 leaderboard 重算值的符号一致性（"胜率高"但实际 <50% → 记 violation);
- regime_hint vs 价格法一致率；
- 退化战报跳过率。

### ⑤ 灰度 rollout（开关 evolution_signals.json: shadow|prompt|weights|off，每次调用重读）

1. **第 1 周 shadow**：蒸馏照常落库 + shadow_bias_json 记录，**不动行为**；按上述量化标准验收信号质量；
2. **第 2 周**：开阶段 1(prompt 替换 + hypotheses 种子），盯 `llm_gen_fail` 率与过闸率；
3. **第 3~4 周**：开阶段 2（权重偏置，全局 cap ×2 + 熵监控）;
4. 4 周后按主指标（Wilson 区间排除 0、lift>30%）决定保留/降级回阶段 1。

### ⑥ 与归一化 typed_v2 的二阶耦合（评审补充 8，显式声明）

norm 分派 → 选股分数 → picks → outcomes → leaderboard → 战报 → 蒸馏信号，口径切换会沿此链传导。**norm 灰度切换周与蒸馏 shadow/度量周互斥**（不同时动两个旋钮）;`evolution_signals` 落库记 `norm_scheme` 列，lift 对比按该列分层——否则 4 周后分不清 lift 是信号的功劳还是口径的功劳。

## 工作量估算（评审修正：~550-600 行，原 ~400 偏乐观）

| 模块 | 内容 | 行数 |
|---|---|---|
| scheduler.py | 蒸馏 job（挂 outcome_backfill 后）+ 平衡括号抽取 + schema 校验 + 退化检测 | ~160 |
| experience.py | evolution_signals 表（含 shadow_bias_json/norm_scheme 列）+ tested_hashes.signal_id | ~60 |
| engine.py | 证据段替换（分槽渲染）+ 族/类型偏置（全局 cap)+ 打标 | ~100 |
| genetics.py | FieldWeights 外部合成入口（clamp)+ Budget 注释 | ~30 |
| scripts/ | 周报（Wilson/分层/熵/violation 率） | ~120 |
| tests/ | schema/平衡抽取/过期（交易日历）/cap/无信号照常/打标/退化跳过 | ~120 |

## 二期预留

涨停对话页复盘结论走同一蒸馏管线（signals 表加 source 列：report/chat)。先验证 report 源 lift,chat 源再接。

## 开放问题

1. 蒸馏模型用推理版（每日一次成本可忽略）——倾向 deepseek-v4-pro;
2. 信号有效期 3 个交易日——shadow 周顺便观察信号衰减速度再校准；
3. 主指标门槛：Wilson 区间排除 0 且 lift>30% 才保留结构层（评审确认）;
4. 既有 Bug 备忘（本案不修）:`Budget.SOURCES` 7 槽位 vs `_gen_candidate` 5 分支，mutate_op/perturb_field 名实不符静默走 random_tree。
