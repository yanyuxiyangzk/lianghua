"""⏰ 定时任务 tab：手动启停的进程内调度（个股信号 / 板块池扫描 / 数据更新）。"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from common import (SCHED_LAST_FILE, SIGNALS_DIR, all_pools, get_last_trade_day, load_json)
from scheduler import JOBS, get_scheduler


def render():
    mgr = get_scheduler()
    st.subheader("⏰ 定时任务（程序内调度）")
    st.caption(
        f"数据截至 **{get_last_trade_day()}** · 机制：手动启动后按交易日一直跑；"
        "QSYS 容器停止 = 调度停止；容器重启后需回到本页重新启动任务。"
    )

    view = mgr.view()
    pools = list(all_pools().keys())

    for key, cfg in view.items():
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            with c1:
                st.markdown(f"**{cfg['label']}**")
                on = st.toggle("启用", value=cfg["enabled"], key=f"tg_{key}")
                if on != cfg["enabled"]:
                    mgr.set_enabled(key, on)
                    st.rerun()
            with c2:
                if cfg.get("trigger") == "interval":
                    sec = st.slider("采集间隔（秒）", 10, 300, int(cfg["params"].get("interval_sec", 30)),
                                    key=f"sec_{key}")
                    p2 = st.selectbox("股票池/板块", pools,
                                      index=pools.index(cfg["params"].get("pool_name", "沪深300"))
                                      if cfg["params"].get("pool_name") in pools else 0, key=f"pool2_{key}")
                    new_params = {"pool_name": p2, "interval_sec": sec}
                    if new_params != {k: cfg["params"].get(k) for k in new_params}:
                        mgr.set_params(key, new_params)
                        mgr._apply_state()
                else:
                    t = st.time_input("执行时间（交易日）", value=pd.Timestamp(2000, 1, 1, cfg["hour"], cfg["minute"]),
                                      key=f"tm_{key}")
                    if (t.hour, t.minute) != (cfg["hour"], cfg["minute"]):
                        mgr.set_schedule(key, t.hour, t.minute)
                        st.rerun()
                st.caption(f"下次运行：{cfg['next'] or '未启用'}")
            with c3:
                if key == "pool_scan":
                    import library
                    packs = library.list_strategies()
                    pack_opts = ["（默认组合）"] + list(packs.keys())
                    cur_pack = cfg["params"].get("pack") or "（默认组合）"
                    pk = st.selectbox("策略包", pack_opts,
                                      index=pack_opts.index(cur_pack) if cur_pack in pack_opts else 0,
                                      key=f"pack_{key}", help="在 🪄选股组合 页固化")
                    p = st.selectbox("股票池/板块", pools, index=pools.index(cfg["params"].get("pool_name", "沪深300"))
                                     if cfg["params"].get("pool_name") in pools else 0, key=f"pool_{key}")
                    n = st.slider("Top-N", 5, 50, int(cfg["params"].get("top_n", 20)), key=f"n_{key}")
                    new_params = {"pool_name": p, "top_n": n, "pack": "" if pk == "（默认组合）" else pk}
                    if new_params != {k: cfg["params"].get(k) for k in new_params}:
                        mgr.set_params(key, new_params)
            with c4:
                if st.button("▶️ 立即执行一次", key=f"run_{key}"):
                    mgr.run_now(key)
                    st.toast(f"已触发：{cfg['label']}（后台执行，稍后刷新看结果）")
                last = cfg["last"]
                if last:
                    icon = "✅" if last["ok"] else "❌"
                    st.caption(f"{icon} 上次：{last['time']}")
                    st.caption(last["msg"][:120])

    # ---------------- 最近产物 ----------------
    with st.expander("📦 任务产物（signals 目录）"):
        files = sorted(SIGNALS_DIR.glob("*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            st.caption("还没有产物。先「立即执行一次」个股信号或板块扫描。")
        for f in files[:10]:
            st.caption(f"`{f.name}` — {pd.Timestamp(f.stat().st_mtime, unit='s').strftime('%m-%d %H:%M')}")
            if st.button("预览", key=f"pv_{f.name}"):
                try:
                    st.dataframe(pd.read_parquet(f), width='stretch')
                except Exception as e:
                    st.error(f"文件损坏（可删除后重跑任务重新生成）：{e}")

    with st.expander("📜 运行历史"):
        hist = Path(SCHED_LAST_FILE).parent / "scheduler_history.jsonl"
        if hist.exists():
            lines = hist.read_text().splitlines()[-20:]
            st.dataframe(pd.DataFrame([json.loads(x) for x in lines][::-1]), width='stretch')
        else:
            st.caption("暂无历史")

    if "update_data" in view and view["update_data"]["enabled"]:
        st.warning("数据更新任务会覆盖行情文件：请确保执行时段内 RD-Agent 进化循环未在跑，"
                   "避免 qlib 子容器读到半更新状态。", icon="⚠️")
