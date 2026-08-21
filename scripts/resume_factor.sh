#!/usr/bin/env bash
# 从最近一次进化会话的断点续跑（手动用；常驻自动看护请用 supervisor）
cd "$(dirname "$0")/.."
TRACE=$(ls -dt log/20*/ 2>/dev/null | head -1)
if [ -z "$TRACE" ] || [ ! -d "${TRACE}__session__" ]; then
  echo "没有可续跑的 session，直接跑 ./scripts/factor.sh"
  exit 1
fi
# load() 会自动定位 __session__ 下最新检查点，传 trace 根目录即可
echo "续跑自: $TRACE"
docker compose exec -d rdagent bash -c "rdagent fin_factor --path ${LIANGHUA_ROOT:-/home/zk/code/lianghua}/${TRACE%/} >> $(pwd)/log/factor_run.out 2>&1"
echo "已在后台续跑，日志: log/factor_run.out"
