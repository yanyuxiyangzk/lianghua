#!/usr/bin/env bash
# RD-Agent 进化循环看门狗（在 lh-rdagent 容器内常驻）
# 能力：① 进程崩溃/退出 → 自动从最新检查点断点续跑
#       ② 日志 90 分钟无更新（卡死）→ 杀掉重启续跑
# 启动：docker compose exec -d rdagent bash ${LIANGHUA_ROOT}/rdagent/factor_supervisor.sh
cd "${LIANGHUA_ROOT:-/home/zk/code/lianghua}"
LOG=log/factor_run.out
STALE_SEC=5400  # 90分钟，给LLM调用和Qlib回测更多时间

latest_trace() { ls -dt log/20*/ 2>/dev/null | head -1; }

start_loop() {
    # 清掉可能残留的旧进程
    pkill -f "rdagent fin_factor" 2>/dev/null
    sleep 2
    local trace=$(latest_trace)
    if [ -n "$trace" ] && [ -d "${trace}__session__" ]; then
        echo "[supervisor] $(date '+%F %T') 从检查点续跑: $trace" >> "$LOG"
        # load() 会自动定位 __session__ 下最新检查点，直接传 trace 根目录
        rdagent fin_factor --path "${trace%/}" >> "$LOG" 2>&1 &
    else
        echo "[supervisor] $(date '+%F %T') 全新启动" >> "$LOG"
        rdagent fin_factor >> "$LOG" 2>&1 &
    fi
    LOOP_PID=$!
    echo "[supervisor] $(date '+%F %T') 循环进程 PID=$LOOP_PID" >> "$LOG"
}

start_loop
while true; do
    sleep 300
    if ! kill -0 "$LOOP_PID" 2>/dev/null; then
        echo "[supervisor] $(date '+%F %T') 进程退出，自动重启续跑" >> "$LOG"
        start_loop
        continue
    fi
    if [ -f "$LOG" ]; then
        mtime=$(stat -c %Y "$LOG")
        now=$(date +%s)
        if [ $((now - mtime)) -gt $STALE_SEC ]; then
            echo "[supervisor] $(date '+%F %T') 日志 ${STALE_SEC}s 无更新，判定卡死，强制重启" >> "$LOG"
            kill -9 "$LOOP_PID" 2>/dev/null
            sleep 3
            start_loop
        fi
    fi
done
