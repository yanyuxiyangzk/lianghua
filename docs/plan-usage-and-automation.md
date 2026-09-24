# 使用指南 & 自动化方案（待审批，未实施）

> 状态：**方案待确认**。确认后按分期实施，每期独立可用。

---

## 一、现有系统 60 秒上手

| 我要… | 操作 |
|---|---|
| 启动/停止整个系统 | `docker compose up -d` / `docker compose down` |
| 看进化过程（假设、因子代码、IC 对比） | http://localhost:8501 → 🧬进化看板 → 选最新 trace |
| 看某轮回测的净值曲线 | :8501 → 📊回测浏览 → 选 recorder |
| 看自选股 K 线 | :8501 → 🕯️自选K线（可搜索加自选） |
| 开始/继续因子进化 | `./scripts/factor.sh`（前台）或 `docker compose exec -d rdagent bash -c "rdagent fin_factor >> log/factor_run.out 2>&1"`（后台） |
| 停止进化 | `docker compose restart rdagent`（产出保留） |
| 看官方过程监控（LLM 对话细节） | `./scripts/ui.sh` → :19899 |
| 更新行情数据 | `./scripts/update_data.sh` |
| 拿出挖到的因子 | 进化看板里复制因子代码，或到 `git_ignore_folder/RD-Agent_workspace/*/factor.py` |

---

## 二、QSYS 新增「📘 使用指南」tab（P0）

把现有"📖 说明"tab 升级为完整操作手册：

1. **操作手册**：上面的一节内容 + 截图级分步说明
2. **运行状态卡**：进化循环是否在跑（探测 log 最新写入时间）、当前是第几轮、最新 SOTA 指标摘要、数据截止日期
3. **快捷命令**：常用命令以代码块形式列出，一键复制
4. **FAQ**：本次部署踩过的坑（shm、续跑、文件属主等）

工作量：小（只动 qsys/app.py + 重建镜像）。

---

## 三、定时任务自动化方案（核心）

### 3.1 任务清单与时间线（交易日）

```
17:30  ① 数据更新        update_data.sh（T日收盘数据傍晚发布；带重试+日历校验）
18:30  ② 自选股信号计算   用最新 SOTA 因子 × 最新数据，算自选股因子值/排名 → 存 parquet
20:00  ③ 进化续跑 N 轮    接着主 session 继续进化（知识库累积，而非每次重开）
周日   ④ 维护            清理旧 workspace、备份 SOTA 因子库（log/ 打包）
```

### 3.2 调度器选型（推荐 B）

| 方案 | 做法 | 优劣 |
|---|---|---|
| A. 宿主机 cron | crontab 直接调 `scripts/*.sh` | 最简单；但游离在 compose 之外 |
| **B. ofelia 容器（推荐）** | compose 加一个 `ofelia` 服务，任务以 label/ini 声明 | 与系统一体、配置进版本管理、自带任务日志 |

### 3.3 "个股的"——每日自选股信号（P1 重点）

- 数据源：每日更新后的 cn_data + **最新一轮被接受（✅）的 SOTA 因子代码**（从进化日志自动提取）
- 计算：对自选股算因子值、横截面 z-score、较昨日变化
- 展示：QSYS 新页「🎯 每日信号」表格（股票 × 因子值 × 排名变化），页面只读计算产物
- 边界遵守：信号计算只是"执行已有因子"，因子生产仍 100% 在 RD-Agent 闭环内

### 3.4 "板块的"——自定义股票池进化（P3）

RD-Agent 的 qlib 场景默认 `market=csi300`。做板块有两条路：

| 路径 | 说明 | 成本 |
|---|---|---|
| 现成指数池 | csi500 / csi800 / csi1000（数据自带 instruments 文件，改配置即可） | 低 |
| 行业/概念板块 | 申万等行业成分 qlib 社区数据**没有**，需用 akshare 等拉成分 → 生成 qlib instruments 文件挂入 cn_data → 模板 `market`/`benchmark` 指向它（vendor 副本可直接改模板 conf.yaml） | 中 |

注意：板块成分太少时截面 IC 统计意义弱，建议股票池 ≥100 只（小行业可合并）。

### 3.5 风险与护栏

- **token 预算**：进化续跑固定 `--loop-n N`（如每晚 5 轮），并在 .env 设限额提醒；DeepSeek 后台可看消耗
- **并发约束**：同一时刻只跑一个进化进程（内存 + /dev/shm）；信号计算是只读，可并行
- **数据源延迟**：qlib_bin 有时晚间才发布，①失败要重试并校验 `calendars/day.txt` 末日期，太旧则跳过后续任务并告警
- **断点续跑**：主会话固定，③用 `__session__` 路径续跑，保持知识库连续

---

## 四、分期实施建议

| 期 | 内容 | 依赖 | 体量 |
|---|---|---|---|
| **P0** | 📘使用指南 tab（含运行状态卡） | 无 | 小 |
| **P1** | ofelia 调度 + ①每日数据更新 + ②每日信号 + 「🎯每日信号」页 | P0 | 中 |
| **P2** | ③主会话续跑机制 + token 预算护栏 + ④维护任务 | P1 | 中 |
| **P3** | 自定义股票池/板块进化（含行业成分导入管线） | P1 | 较大 |

建议顺序 P0 → P1 → P2 → P3，每期交付即可用。

---

## 五、Redis 会话持久层与研究记忆（新增开发规划）

### 5.1 建设目标和边界

Redis 用于会话、任务状态、缓存和研究记忆检索；SQLite 继续作为资金、委托、成交、持仓、审批和风控事实源。Redis 不得直接授权下单，也不得成为交易账本的唯一副本。Redis 故障时，系统必须保持“禁止未经批准的新策略开仓、允许查询持仓、允许卖出”的安全降级状态。

### 5.2 数据分层

| 数据 | 存储 | 生命周期 | 用途 |
|---|---|---|---|
| 用户会话 | Redis `session:*` | 1–7 天 | 页面状态、筛选条件、最近查看 |
| 缓存 | Redis `cache:*` | 分钟到小时 | 行情和计算结果缓存 |
| 循环任务状态 | Redis `job:*` + SQLite 审计 | 任务周期 | 当前阶段、进度、心跳、错误、断点 |
| 研究记忆 | Redis `memory:*` + 原始报告文件 | 长期 | 因子、策略、回测和复盘检索 |
| 交易事实 | SQLite | 永久审计 | 资金、委托、成交、持仓、风控 |
| 原始报告和模型产物 | 文件/对象存储 | 长期 | 回测报告、日志、模型、证据 |

记忆必须区分 `fact`、`research_result`、`reflection`、`proposal`、`approval`。AI 只能新增反思和候选方案，不能修改事实和审批记录。

### 5.3 任务进度可查询设计

Redis 接入后，所有数据更新、因子挖掘、回测、选股、反思和进化任务统一登记任务状态。每个任务使用稳定的 `job_id`，并记录：

```text
job_id, job_type, run_id, stage, status, progress_pct,
current_item, total_items, started_at, updated_at,
heartbeat_at, error_code, error_message, checkpoint_ref,
result_ref, worker, version
```

推荐键：

```text
job:{job_id}                         # 当前状态 Hash/JSON
job:{job_id}:events                  # 状态变更 Stream
jobs:active                          # 活跃任务集合
jobs:recent                          # 最近任务有序集合
run:{run_id}:jobs                    # 某轮进化的任务清单
```

状态统一为：`queued`、`running`、`paused`、`succeeded`、`failed`、`cancelled`、`stale`。任务每次阶段变更都写事件，心跳超过阈值则标记 `stale`，不能只依赖进程日志判断进度。

QSYS 增加“任务进度”只读页面/状态卡，支持按任务类型、运行批次、状态和时间筛选，显示当前阶段、完成比例、最近心跳、错误原因和断点链接。页面读取 Redis；任务结果和审计仍链接到 SQLite 或文件报告。

### 5.4 分期实施

| 版本 | 内容 | 验收标准 |
|---|---|---|
| R0 | Redis Stack Docker 服务、AOF、备份、ACL、连接池、健康检查 | 容器重启后数据恢复；Redis 不可用时交易安全降级 |
| R1 | 会话持久化和任务状态登记 | 页面刷新可恢复；任务可查阶段、进度、心跳和错误 |
| R2 | 结构化研究记忆 | 因子/策略/回测/风控拦截/选股证据可按字段检索，均有 `evidence_ref` |
| R3 | RediSearch 向量检索 | 先结构化过滤，再向量排序；向量可重建并记录模型版本 |
| R4 | 接入 AI 反思和自动进化 | AI 只能生成候选和反思；必须经过回测、留出集、影子运行和执行资格闸 |

### 5.5 记忆质量和时间约束

长期记忆必须包含 `asof_date`、`available_at`、`confidence`、`sample_count`、`evaluation_period`、`evidence_ref` 和 `status`。回测按历史时间切片读取记忆，禁止读取未来生成的复盘结论。向量记录 embedding 模型、维度、版本和索引版本，模型升级后支持全量重建。

### 5.6 可靠性和安全要求

- Redis 开启 AOF，并定期 RDB/目录备份；设置内存上限、慢查询和阻塞监控。
- 使用连接超时、重试上限、熔断和幂等写入；重复任务不得重复产生记忆或进度事件。
- Redis 只绑定内网，不暴露公网，使用 ACL/密码。
- `session:*`、`cache:*` 使用 TTL；审批、成交和风控记录不得依赖 TTL。
- Redis 彻底不可用时，不能自动放开交易，卖出和账本查询仍可运行。

### 5.7 进度查询接口规划

第一版提供内部 Python 接口：`job_start()`、`job_heartbeat()`、`job_progress()`、`job_event()`、`job_finish()`、`job_get()`、`job_list()`；第二版在 QSYS 页面提供只读查询。任何自动任务必须在启动、阶段切换、成功、失败和退出前写入状态。
