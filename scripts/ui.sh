#!/usr/bin/env bash
# RD-Agent 官方监控 UI（端口 19899）。可选参数: trace 目录名（默认最新一次）
cd "$(dirname "$0")/.."
source .env
TRACE=${1:-$(ls -1 log | grep -E '^[0-9]{4}-' | sort -r | head -1)}
if [ -z "$TRACE" ]; then echo "log/ 下还没有 trace，先跑 ./scripts/factor.sh"; exit 1; fi
echo "监控 trace: log/$TRACE  →  http://localhost:19899"
docker compose exec rdagent rdagent ui --port 19899 --log_dir "${LIANGHUA_ROOT}/log/${TRACE}"
