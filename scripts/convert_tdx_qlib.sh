#!/usr/bin/env bash
# easy-tdx → Qlib 一键转换（两阶段编排）
# 用法: ./scripts/convert_tdx_qlib.sh --pool csi300   （或 --codes SH600519 SZ000001）
set -e
cd "$(dirname "$0")/.."

echo "=== 阶段1: easy-tdx 抓取 → CSV（qsys 容器）==="
docker compose exec -T qsys python /app/tdx_to_qlib.py "$@"

echo "=== 阶段2: CSV → qlib bin（local_qlib 容器）==="
docker run --rm \
  -v "$(pwd)/qsys/data/tdx_csv:/tmp/csv" \
  -v "$(pwd)/qsys/data/qlib_tdx:/data/out" \
  local_qlib:latest \
  python /workspace/qlib/scripts/dump_bin.py dump_all \
    --data_path /tmp/csv --qlib_dir /data/out --freq day \
    --include_fields open,high,low,close,volume,amount,factor \
    --date_field_name date

echo "=== 完成 → qsys/data/qlib_tdx/（RD-Agent/Qlib 把 provider_uri 指向它即可）==="
ls qsys/data/qlib_tdx/
