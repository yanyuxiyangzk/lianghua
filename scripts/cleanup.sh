#!/usr/bin/env bash
# 清理 lianghua 运行产物:RD-Agent 假设工作区 / 因子执行缓存 / 循环日志
#
# 用法:
#   scripts/cleanup.sh               预览(dry-run,不删任何东西)
#   scripts/cleanup.sh -y            实际执行全部清理(工作区/缓存各保留 7 天 + 截断日志)
#   scripts/cleanup.sh -w 3 -y       只清 3 天前的工作区
#   scripts/cleanup.sh -c 0 -y       清空全部因子缓存
#
# 选项:
#   -w [天数]   清理 git_ignore_folder/RD-Agent_workspace 里 N 天前的工作区(默认 7)
#   -c [天数]   清理 pickle_cache 里 N 天前的因子执行缓存(默认 7;0 = 全清)
#   -l          截断 log/factor_run.out 和 selector.log(不断循环,append 模式安全)
#   -a          以上全部(与不带选项相同,显式声明)
#   -y          实际执行(不带此选项只预览)
#
# 注意:
# - pickle_cache 与日志文件属 root(容器产出),脚本自动借 lh-rdagent 容器以 root 操作
# - 清缓存后下一轮循环会重算被删的因子(一次性变慢、内存冲高),之后重新落缓存
# - 不会动:log/ 下的会话检查点(__session__)、data/ qlib 数据、factor_implementation_source_data
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

WS_DAYS=7
CACHE_DAYS=7
DO_WS=0; DO_CACHE=0; DO_LOG=0; APPLY=0
SEEN_TARGET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -w|--workspaces) DO_WS=1; SEEN_TARGET=1
            if [[ "${2:-}" =~ ^[0-9]+$ ]]; then WS_DAYS="$2"; shift; fi; shift ;;
        -c|--cache) DO_CACHE=1; SEEN_TARGET=1
            if [[ "${2:-}" =~ ^[0-9]+$ ]]; then CACHE_DAYS="$2"; shift; fi; shift ;;
        -l|--logs) DO_LOG=1; SEEN_TARGET=1; shift ;;
        -a|--all) DO_WS=1; DO_CACHE=1; DO_LOG=1; SEEN_TARGET=1; shift ;;
        -y|--yes) APPLY=1; shift ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
        *) echo "未知参数: $1(-h 看用法)" >&2; exit 1 ;;
    esac
done
# 不带目标选项 = 全部三类都处理
if [[ $SEEN_TARGET -eq 0 ]]; then DO_WS=1; DO_CACHE=1; DO_LOG=1; fi

[[ $APPLY -eq 0 ]] && echo "=== 预览模式(加 -y 实际执行)==="

# 容器需在运行(借它的 root 删 root 属主文件;容器内项目路径与宿主相同)
need_container() {
    if ! docker ps --format '{{.Names}}' | grep -qx lh-rdagent; then
        echo "!! 需要 root 权限但 lh-rdagent 容器未运行,请改用 sudo 或先启动容器" >&2
        exit 1
    fi
}

list_size() { xargs -a "$1" -r du -sch 2>/dev/null | tail -1 | cut -f1; }  # 汇总路径列表大小

# ---------- 1. RD-Agent 假设工作区 ----------
if [[ $DO_WS -eq 1 ]]; then
    echo; echo "[工作区] git_ignore_folder/RD-Agent_workspace,保留 ${WS_DAYS} 天内"
    LIST=$(mktemp)
    find "$ROOT/git_ignore_folder/RD-Agent_workspace" -mindepth 1 -maxdepth 1 -type d -mtime +"$WS_DAYS" 2>/dev/null | sort > "$LIST"
    n=$(wc -l < "$LIST")
    if [[ $n -eq 0 ]]; then
        echo "  无可清理项"
    else
        echo "  待删 ${n} 个目录,共 $(list_size "$LIST")"
        if [[ $APPLY -eq 1 ]]; then
            # 目录内文件多为容器 root 属主,宿主删不动 → 借容器删
            need_container
            docker exec -i lh-rdagent xargs -r rm -rf < "$LIST"
            echo "  已删除 ✓"
        fi
    fi
    rm -f "$LIST"
fi

# ---------- 2. 因子执行缓存(root 属主,借容器删) ----------
if [[ $DO_CACHE -eq 1 ]]; then
    echo; echo "[因子缓存] pickle_cache/,保留 ${CACHE_DAYS} 天内(0=全清)"
    LIST=$(mktemp)
    if [[ $CACHE_DAYS -gt 0 ]]; then
        find "$ROOT/pickle_cache" -type f -mtime +"$CACHE_DAYS" 2>/dev/null | sort > "$LIST"
    else
        find "$ROOT/pickle_cache" -type f 2>/dev/null | sort > "$LIST"
    fi
    n=$(wc -l < "$LIST")
    if [[ $n -eq 0 ]]; then
        echo "  无可清理项"
    else
        echo "  待删 ${n} 个文件,共 $(list_size "$LIST")"
        echo "  ⚠ 下一轮循环将重算被删因子(SOTA 处理一次性变慢、内存冲高)"
        if [[ $APPLY -eq 1 ]]; then
            need_container
            docker exec -i lh-rdagent xargs -r rm -f < "$LIST"
            docker exec lh-rdagent find "$ROOT/pickle_cache" -mindepth 1 -type d -empty -delete
            echo "  已删除 ✓"
        fi
    fi
    rm -f "$LIST"
fi

# ---------- 3. 循环日志截断(root 属主,借容器) ----------
if [[ $DO_LOG -eq 1 ]]; then
    echo; echo "[日志] 截断 log/factor_run.out 与 selector.log"
    for f in log/factor_run.out selector.log; do
        if [[ -f $f ]]; then
            sz=$(du -h "$f" | cut -f1)
            if [[ $APPLY -eq 1 ]]; then
                need_container
                docker exec lh-rdagent truncate -s 0 "$ROOT/$f"
                echo "  $f: $sz -> 0 ✓"
            else
                echo "  $f: $sz(待截断)"
            fi
        fi
    done
fi

echo
if [[ $APPLY -eq 1 ]]; then
    # WSL2 稀疏磁盘打孔：删文件只释放 ext4 内部空间，VHDX 不 TRIM 就不会缩
    if docker ps --format '{{.Names}}' | grep -qx lh-rdagent; then
        echo "[磁盘] 对 WSL 文件系统发 TRIM（稀疏 VHD 释放块）…"
        docker run --rm --privileged -v /:/host lianghua/rdagent:0.8.0 fstrim -v /host 2>/dev/null | tail -1
        echo "  若 Windows 侧 VHDX 未立即缩小，需 wsl --shutdown 后 diskpart compact（见 README）"
    fi
    df -h / | awk 'NR==2 {printf "磁盘: %s 已用 / %s 可用\n", $3, $4}'
else
    echo "(预览结束,确认无误后加 -y 执行)"
fi
