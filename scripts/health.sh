#!/usr/bin/env bash
# RD-Agent 环境自检：docker、LLM 连通性、端口
cd "$(dirname "$0")/.."
docker compose exec rdagent rdagent health_check "$@"
