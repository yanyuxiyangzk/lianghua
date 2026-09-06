#!/bin/bash
# 启动 Streamlit + SSE 服务器
set -e

cd /app

# 启动 SSE 服务器（后台）
echo "[start.sh] Starting SSE server on :8502..."
python3.11 -m uvicorn sse_server:app --host 0.0.0.0 --port 8502 --log-level warning &
SSE_PID=$!
echo "[start.sh] SSE server PID: $SSE_PID"

# 等待 SSE 服务器启动
sleep 2

# 启动 Streamlit（前台）
echo "[start.sh] Starting Streamlit on :8501..."
exec python3.11 -m streamlit run /app/app.py --server.address=0.0.0.0 --server.port=8501
