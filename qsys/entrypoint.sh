#!/usr/bin/env bash
# QSYS 容器入口：调度器 + SSE 同进程 + Streamlit
set -e

# Scheduler + SSE server (同一进程，共享 event_bus)
python - <<'PY' &
import sys, time, threading
sys.path.insert(0, "/app")

# 启动 SSE server (FastAPI) 在后台线程
def start_sse():
    try:
        import uvicorn
        from sse_server import app
        uvicorn.run(app, host="0.0.0.0", port=8502, log_level="warning")
    except Exception as e:
        print(f"[entrypoint] SSE server 启动失败: {e}", flush=True)

sse_thread = threading.Thread(target=start_sse, daemon=True)
sse_thread.start()
print("[entrypoint] SSE server 已启动 (port 8502)", flush=True)

# 启动调度器
try:
    from scheduler import get_scheduler
    mgr = get_scheduler()
    enabled = [k for k, v in mgr.view().items() if v["enabled"]]
    print(f"[entrypoint] 调度器已启动，启用任务: {enabled}", flush=True)
except Exception as e:
    print(f"[entrypoint] 调度器启动失败: {e}", flush=True)

while True:
    time.sleep(3600)
PY

exec streamlit run /app/app.py --server.address=0.0.0.0 --server.port=8501
