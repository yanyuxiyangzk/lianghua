#!/usr/bin/env bash
# 启动 RD-Agent 因子自动发掘/进化闭环（前台运行，日志实时输出）
# 用法: ./scripts/factor.sh [--loop_n 10] [--all_duration 12h] ...
cd "$(dirname "$0")/.."
docker compose exec -it rdagent rdagent fin_factor "$@"
