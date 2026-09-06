"""在 Streamlit 进程内启动 SSE 服务器（后台线程）。"""

import logging
import threading

logger = logging.getLogger("sse_bootstrap")
_sse_started = False


def start_sse_server(port: int = 8502):
    """在后台线程启动 uvicorn SSE 服务器（共享进程内 event_bus）。"""
    global _sse_started
    if _sse_started:
        return None
    _sse_started = True

    def _run():
        try:
            import uvicorn
            from sse_server import app

            config = uvicorn.Config(
                app, host="0.0.0.0", port=port,
                log_level="warning", access_log=False,
            )
            server = uvicorn.Server(config)
            server.run()
        except Exception as e:
            logger.warning("SSE server failed to start: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="sse-server")
    t.start()
    logger.info("SSE server starting on :%d (background thread)", port)
    return t
