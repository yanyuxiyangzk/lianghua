"""📡 iFinD 数据：同花顺量化接口查询台（行情 / 基本面 / 特色数据）。

凭证：settings.json 的 ths_ifind.refresh_token（已 gitignore，不入库）。
SDK：iFinDPy 不在 PyPI，需从 quantapi.51ifind.com 下载 Linux 版装进容器
     （下载后放宿主机任意目录，叫我一声我来装并进镜像）。
特色数据（公告/智能选股/期股联动）的函数名与参数以官方文档为准——
本页提供函数名可编辑的通用控制台，填好直接调，原始返回全展示。
"""

import pandas as pd
import streamlit as st

import datasource

st.title("📡 iFinD 数据")

# ---------------------------------------------------------------- 连通性
acc, pwd, token = datasource._ths_credentials()
c1, c2 = st.columns([1, 3])
with c1:
    if st.button("🔌 连通性自检", type="primary"):
        with st.spinner("登录并拉取测试数据…"):
            st.info(datasource.ths_selftest())
with c2:
    st.caption(f"凭证：{'✅ 已配置 refresh_token' if token else ('✅ 已配置账号密码' if acc else '❌ 未配置')}　·　"
               "SDK：未安装时点任何查询会给出安装指引")


def _show(df, res, err):
    if err not in (0, None):
        st.error(f"调用失败 errorcode={err}：{getattr(res, 'errmsg', res)}")
    elif df is not None and not df.empty:
        st.dataframe(df, width='stretch')
    elif df is not None:
        st.warning("调用成功但返回为空（检查指标名/参数/权限）")
    else:
        st.code(str(res)[:3000], language="text")


def _go(fn, *args, **kwargs):
    try:
        with st.spinner("查询中…"):
            _show(*fn(*args, **kwargs))
    except Exception as e:
        st.error(str(e))


def _codes_input(label, default="600519", key=""):
    t = st.text_input(label, default, key=key, help="多只逗号分隔；600519 或 600519.SH 或 SH600519 都行")
    out = []
    for x in t.replace("，", ",").split(","):
        x = x.strip().upper().replace(".", "")
        if not x:
            continue
        if x[:2] in ("SH", "SZ", "BJ"):
            out.append(x)
        elif len(x) == 6 and x.isdigit():
            out.append(("SH" if x.startswith("6") else "BJ" if x.startswith(("4", "8")) else "SZ") + x)
    return out


t1, t2, t3 = st.tabs(["📈 行情数据", "🏢 基本面数据", "✨ 特色数据"])

# ---------------------------------------------------------------- 行情数据
with t1:
    st.markdown("**实时数据**（THS_RQ）")
    codes = _codes_input("代码", "600519,000001", "rq_codes")
    ind = st.text_input("指标", "latest,open,high,low,volume,amount", key="rq_ind",
                        help="逗号分隔，指标名见官方文档-实时数据")
    if st.button("查询实时", key="rq_go") and codes:
        _go(datasource.ths_realtime, codes, ind)

    st.markdown("**历史数据**（THS_HQ）")
    codes = _codes_input("代码", "600519", "hq_codes")
    ind = st.text_input("指标", "open,high,low,close,volume,amount", key="hq_ind")
    c1, c2, c3 = st.columns(3)
    d0 = c1.text_input("开始", "2026-08-01", key="hq_s")
    d1 = c2.text_input("结束", "2026-08-27", key="hq_e")
    params = c3.text_input("参数", "Fill:Original,Interval:D", key="hq_p",
                           help="复权/周期等，见官方文档（如前复权 Fill:Forward）")
    if st.button("查询历史", key="hq_go") and codes:
        _go(datasource.ths_history, codes, ind, d0, d1, params)

    st.markdown("**高频数据**（THS_HF）")
    codes = _codes_input("代码（单只）", "600519", "hf_codes")
    ind = st.text_input("指标", "open,high,low,close,volume", key="hf_ind")
    c1, c2, c3 = st.columns(3)
    h0 = c1.text_input("开始时间", "2026-08-26 09:30:00", key="hf_s")
    h1 = c2.text_input("结束时间", "2026-08-26 15:00:00", key="hf_e")
    interval = c3.selectbox("粒度", ["1min", "5min", "15min", "30min", "60min"], key="hf_iv")
    if st.button("查询高频", key="hf_go") and codes:
        _go(datasource.ths_highfreq, codes[0], ind, h0, h1, interval)

    st.markdown("**日内快照**（THS_Snapshot）")
    codes = _codes_input("代码", "600519,000001", "ss_codes")
    ind = st.text_input("指标", "latest,volume,amount,open,high,low", key="ss_ind")
    stime = st.text_input("快照时间（留空=最新）", "", key="ss_t")
    if st.button("查询快照", key="ss_go") and codes:
        _go(datasource.ths_snapshot, codes, ind, stime)

# ---------------------------------------------------------------- 基本面数据
with t2:
    st.markdown("**基础数据**（THS_DS：截面指标，如市盈率/市值/营收）")
    codes = _codes_input("代码", "600519", "ds_codes")
    ind = st.text_input("指标", "ths_pe_ttm_stock,ths_market_value_stock", key="ds_ind",
                        help="指标名见官方文档-基础数据")
    params = st.text_input("参数（可空）", "", key="ds_p")
    if st.button("查询基础数据", key="ds_go") and codes:
        _go(datasource.ths_basic, codes, ind, params)

    st.markdown("**日期序列**（THS_DateSerial：指标的时序）")
    codes = _codes_input("代码（单只）", "600519", "dss_codes")
    ind = st.text_input("指标", "ths_pe_ttm_stock", key="dss_ind")
    c1, c2 = st.columns(2)
    ds0 = c1.text_input("开始", "2026-01-01", key="dss_s")
    ds1 = c2.text_input("结束", "2026-08-27", key="dss_e")
    params = st.text_input("参数（可空）", "", key="dss_p")
    if st.button("查询日期序列", key="dss_go") and codes:
        _go(datasource.ths_date_serial, codes[0], ind, ds0, ds1, params)

    st.markdown("**专题报表**")
    st.caption("函数名/报表名/参数以官方文档-专题报表章节为准（如 THS_Report），填好直接调：")
    fname = st.text_input("函数名", "THS_Report", key="rp_fn")
    args = st.text_input("位置参数（JSON 数组）", '["600519.SH"]', key="rp_args")
    kws = st.text_input("关键字参数（JSON 对象）", '{}', key="rp_kwargs")
    if st.button("调用专题报表", key="rp_go"):
        import json as _json
        try:
            _go(datasource.ths_call, fname, *_json.loads(args), **_json.loads(kws))
        except _json.JSONDecodeError as e:
            st.error(f"参数 JSON 解析失败：{e}")

# ---------------------------------------------------------------- 特色数据
with t3:
    st.caption("公告查询 / 智能选股 / 期股联动 / 公告下载——函数名与参数**以官方文档-特色数据章节为准**；"
               "下面是函数名可编辑的通用控制台，返回结果原样展示。")
    feat = st.selectbox("功能", ["智能选股", "公告查询", "期股联动", "公告下载"], key="ft_sel")
    presets = {
        "智能选股": ("THS_WC", '["涨停 且 市值小于50亿"]'),
        "公告查询": ("THS_ANN", '["600519.SH"]'),
        "期股联动": ("THS_QGLD", '["600519.SH"]'),
        "公告下载": ("THS_ANNDOWN", '["600519.SH"]'),
    }
    dfn, dargs = presets[feat]
    fname = st.text_input("函数名", dfn, key="ft_fn",
                          help="默认名为占位猜测——请对照官方文档改成实际函数名")
    args = st.text_input("位置参数（JSON 数组）", dargs, key="ft_args")
    kws = st.text_input("关键字参数（JSON 对象）", '{}', key="ft_kwargs")
    if st.button("调用", key="ft_go"):
        import json as _json
        try:
            _go(datasource.ths_call, fname, *_json.loads(args), **_json.loads(kws))
        except _json.JSONDecodeError as e:
            st.error(f"参数 JSON 解析失败：{e}")
