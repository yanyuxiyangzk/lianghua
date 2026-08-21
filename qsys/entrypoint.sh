#!/usr/bin/env bash
# QSYS 容器入口：调度器与 Streamlit 同起（调度器独立进程常驻，不依赖页面访问）
set -e

python - <<'PY' &
import sys, time
sys.path.insert(0, "/app")
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
