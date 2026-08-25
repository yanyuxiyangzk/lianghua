# 量化系统（lianghua）：RD-Agent + Qlib 进化闭环 × QSYS 看板

## 一、总体方案

```
                        ┌──────────────────────────────────────────────┐
                        │              docker compose                  │
                        │                                              │
  LLM API               │  ┌────────────────────────────┐              │
  (DeepSeek / OpenAI)   │  │  lh-rdagent                │              │
        ▲               │  │  RD-Agent 本体              │              │
        │  因子假设/代码  │  │  · fin_factor 进化闭环      │              │
        └───────────────┼─▶│  · 通过 docker.sock ────────┼──┐           │
                        │  └────────────────────────────┘  │           │
                        │                                  ▼           │
                        │                     ┌─────────────────────┐  │
                        │                     │ local_qlib 子容器    │  │
                        │                     │ qrun conf.yaml      │  │
                        │                     │ (Qlib 真实回测)      │  │
                        │                     └──────────┬──────────┘  │
                        │                                │ 写回测产物   │
                        │  ┌────────────────────────────┼──┐           │
                        │  │  lh-qsys (QSYS/QuantSys)   │  │           │
                        │  │  Streamlit :8501            │◀─┘ 只读     │
                        │  │  · 🧬 进化看板              │              │
                        │  │  · 📊 回测浏览(mlruns)      │              │
                        │  │  · 🕯️ 自选K线(cn_data)      │              │
                        │  └────────────────────────────┘              │
                        └──────────────────────────────────────────────┘
```

**职责边界（硬约束）**
- 因子发掘、假设生成、编码、回测、反馈进化 → 全部在 **RD-Agent + Qlib** 闭环内（`lh-rdagent` + `local_qlib`）。
- **QSYS 只读**：解析 RD-Agent 的进化日志（`log/**.pkl`）、Qlib 的回测产物（`mlruns/**/artifacts`）、行情数据（`cn_data`），不做任何因子生成/进化逻辑。

## 二、目录结构

```
lianghua/
├── docker-compose.yml          # 编排：rdagent + qsys
├── .env                        # LLM key + 项目根路径（⚠️ 需填 key）
├── rdagent/
│   ├── Dockerfile              # RD-Agent 宿主侧镜像（lianghua/rdagent）
│   └── Dockerfile.qlib         # RD-Agent 官方 qlib 执行镜像 → local_qlib:latest
├── qsys/
│   ├── Dockerfile              # 看板镜像（streamlit + pyqlib + rdagent）
│   ├── app.py                  # 看板应用（三页签）
│   └── data/watchlist.json     # 自选股
├── scripts/
│   ├── health.sh               # RD-Agent 环境自检（LLM/docker/端口）
│   ├── factor.sh               # 启动因子进化闭环（前台实时日志）
│   ├── ui.sh                   # RD-Agent 官方监控 UI → :19899
│   └── update_data.sh          # 更新 A 股日线数据
├── data/qlib_home/.qlib/qlib_data/cn_data/   # A股日线（全市场，每日更新源）
├── log/                        # RD-Agent 进化日志（trace）
└── git_ignore_folder/          # RD-Agent workspace（每轮实验 + qlib mlruns）
```

## 三、关键设计

1. **docker-out-of-docker**：RD-Agent 原生就要调 Docker 跑 Qlib。本方案在 `lh-rdagent`
   容器里挂载宿主机 `docker.sock`，并把项目目录**同路径挂载**（容器内路径 == 宿主机路径），
   同时把容器 `HOME` 指到 `data/qlib_home`——这样 RD-Agent 为子容器生成的 bind mount
   源路径对宿主机 dockerd 有效。这是整套方案能跑通的核心。
2. **QLIB_DOCKER_ENABLE_GPU=False**：本机无 GPU，关闭子容器 GPU 申请。
3. **QLIB_DOCKER_BUILD_FROM_DOCKERFILE=False**：`local_qlib:latest` 已按官方 Dockerfile
   预构建（pytorch 2.2.1 基座 + qlib 固定 commit + catboost/xgboost/tables），避免每次启动重建。
4. **数据**：`chenditc/investment_data` 每日发布的 A 股 qlib 日线（约 5000+ 标的，
   2005 年至今），`update_data.sh` 可随时增量更新。
5. **看板解析一致性**：qsys 镜像内安装与主容器**同版本**的 rdagent（0.8.0），
   保证 `log/*.pkl` 里的 Hypothesis/Experiment 对象能正确反序列化。
6. **LLM 双通道**：chat 用 DeepSeek 官方 API；DeepSeek 无 embedding 模型，
   由 compose 内置的 `lh-ollama` 服务本地跑 bge-m3（1024 维，免费无配额），
   rdagent 经内网 `http://ollama:11434` 调用。想换硅基流动在线 embedding，
   改 `.env` 里对应注释段即可。
7. **数据源层**（QSYS 分析/展示层）：`qsys/datasource.py` 统一管理行情来源——
   `qlib_local`（默认，回测同源）与 `akshare`（东财日线·前复权，读穿缓存进
   `qsys/data/market.db`，`market_daily.source` 字段 + `data_sources` 表标识出处）。
   侧边栏可全局切换；经验库 picks 表记录每次选股的 `data_source`。
   **边界：RD-Agent 进化/回测始终只用 qlib_local**，切换不影响闭环。

## 四、使用流程

```bash
# 0. 一次性：.env 里已配好 DeepSeek（chat）+ 本地 ollama bge-m3（embedding，随 compose 自启）
docker compose up -d          # 启动 lh-rdagent + lh-qsys + lh-ollama

# 1. 自检（LLM 连通性 + docker）——当前已全部通过
./scripts/health.sh

# 2. 启动因子进化闭环（Ctrl+C 可中断；支持 --loop-n/--all-duration）
./scripts/factor.sh

# 3. 看板
#    QSYS:         http://localhost:8501   （进化看板 / 回测浏览 / 自选K线）
#    RD-Agent 官方UI: ./scripts/ui.sh  → http://localhost:19899

# 4. 数据更新（建议每周）
./scripts/update_data.sh

# 5. 磁盘清理（默认 dry-run 预览，确认后加 -y；详见 ./scripts/cleanup.sh -h）
./scripts/cleanup.sh           # 预览:旧假设工作区 + 因子执行缓存 + 循环日志
./scripts/cleanup.sh -y        # 执行(各保留 7 天;不会动会话检查点与 qlib 数据)
```

## 五、调参入口（.env 追加，按需）

| 变量 | 默认 | 说明 |
|---|---|---|
| `QLIB_FACTOR_EVOLVING_N` | 10 | 每轮因子进化的迭代次数 |
| `QLIB_FACTOR_MODEL` | — | 换评估模型等，见 `rdagent/app/qlib_rd_loop/conf.py` |
| `QLIB_DOCKER_RUNNING_TIMEOUT_PERIOD` | 3600 | 单次回测超时（秒） |
| `FACTOR_CODER_*` | — | 编码器行为，如 `coder_use_cache` |

完整可配项：`docker compose exec rdagent python -c "from rdagent.app.qlib_rd_loop.conf import FACTOR_PROP_SETTING as s; print(s.model_fields.keys())"`

## 五点五、同花顺 iFinD 接入（QSYS 分析层可选数据源）

分两条独立通道，按需开通：

**A. iFinD API → QSYS 数据源（日线行情，让资金趋势/轮动走势每日变新）**

1. 到 [quantapi.51ifind.com](https://quantapi.51ifind.com) 注册并开通数据接口权限，下载 **Linux 版 iFinDPy SDK**
2. SDK 装进 qsys 容器并重建镜像（SDK 不在 PyPI，需手动放入）：
   `docker cp iFinDPy*.whl lh-qsys:/tmp/ && docker exec lh-qsys pip install /tmp/iFinDPy*.whl`（验证后再 `docker compose build qsys` 固化）
3. `.env` 配置凭证（三选二之一）：`THS_IFIND_ACCOUNT` + `THS_IFIND_PASSWORD`，或 `THS_IFIND_REFRESH_TOKEN`
4. `docker compose up -d qsys` 后自检：`docker exec lh-qsys python -c "import datasource; print(datasource.ths_selftest())"`
5. 看板 ⚙️设置页切换数据源为「同花顺 iFinD」——板块日线回填会跟随全局源（在线源首次全量回填较慢，之后逐日增量）

**B. iFinD MCP → Claude Code（对话式查数据，辅助开发）**

1. 到 [mcp.51ifind.com](https://mcp.51ifind.com) → 密钥管理获取 Token（无 iFinD 账号可走快查开放平台 open.kuaicha365.com，内测有免费额度）
2. `export IFIND_AUTH_TOKEN=<你的token>`（写进 ~/.bashrc），重启 Claude Code
3. 项目根 `.mcp.json` 已预置 `ifind-stock` 服务（StreamableHTTP，注意该端点不支持 SSE），首次会话批准即可用

## 六、常见问题

- **fin_factor 报 docker 权限**：确认 `docker.sock` 已挂载且宿主机当前用户可 `docker ps`。
- **子容器找不到数据**：`data/qlib_home/.qlib/qlib_data/cn_data` 需含 `calendars/ features/ instruments/`。
- **LLM 报错 401/404**：跑 `./scripts/health.sh` 看具体模型/embedding 连通性。
- **看板"进化看板"为空**：第一轮因子回测完成前没有指标，属正常。
- **log/、git_ignore_folder/、vendor/ 里文件属 root**：rdagent 容器以 root 运行所致，
  宿主机要清理时执行 `docker compose exec rdagent chown -R 1000:1000 ${LIANGHUA_ROOT}/{log,git_ignore_folder,vendor}`。
- **vendor/py 是 rdagent 0.8.0 的可编辑副本**（PYTHONPATH 优先于镜像 site-packages），
  内含三处本地补丁（generate.py 的 pandas 兼容、workspace.py 的 MLFLOW_ALLOW_FILE_STORE、
  utils.py 的因子数据 float32 降内存——防 SOTA 因子库累积撑爆 WSL2 内存被 OOM Kill），
  升级 rdagent 版本时需同步。
- **WSL 里删了文件但 Windows D 盘空间没回来**：WSL2 的 VHDX 只自动膨胀不自动缩。
  步骤：① 先在 WSL 里发 TRIM（`./scripts/cleanup.sh -y` 已内置，或手动
  `docker run --rm --privileged -v /:/host lianghua/rdagent:0.8.0 fstrim -v /host`）；
  ② Windows 管理员 PowerShell 执行 `wsl --shutdown`，然后
  `diskpart` → `select vdisk file="D:\WSL\Ubuntu2404\ext4.vhdx"` → `attach vdisk readonly`
  → `compact vdisk` → `detach vdisk`（有 Hyper-V 模块也可 `Optimize-VHD -Mode Full`）。
  ③ 重开 WSL，容器与进化循环自动恢复。
