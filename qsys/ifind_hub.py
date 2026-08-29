"""📡 iFinD 数据中心 hub：侧栏菜单板块「📡 iFinD数据中心」各子页共用的逻辑。

每个子页（实时/历史/高频/快照/基本面/特色）只渲染自己那一份数据；
打开页面即按默认参数自动查一次，参数收进折叠面板；字段名中文化，
字段说明折叠面板保留英文代码对照（方便按官方文档查指标）。

通道：SDK（账号密码，进程内单例会话）优先；SDK 未装/限流(-9)自动落
HTTP API 通道（refresh_token 鉴权，不占会话数）——见 datasource.py。
SDK：iFinDPy 不在 PyPI 且非 pip 包——官方 tar.gz 放 qsys/ifind_sdk/ 后
     重新 build qsys 镜像即自动装入（解压 /opt/iFinD + .pth）。
"""

import pandas as pd
import streamlit as st

import datasource
from datetime import datetime, timedelta

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None


# ---------------------------------------------------------------- 状态头
def header():
    """每个 iFinD 子页顶部的连通性/凭证/自动入库状态条。"""
    acc, pwd, token = datasource._ths_credentials()
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("🔌 连通性自检", type="primary"):
            with st.spinner("登录并拉取测试数据…"):
                st.info(datasource.ths_selftest())
    with c2:
        st.caption(f"SDK 通道（账号密码）：{'✅ 已配置' if acc else '❌ 未配置'}　·　"
                   f"HTTP 通道（refresh_token）：{'✅ 已配置' if token else '❌ 未配置'}　·　"
                   "SDK 被限流/未安装时自动走 HTTP 通道")
    try:
        from scheduler import get_scheduler as _get_sched
        _sync = _get_sched().view().get("ifind_daily_sync", {})
        if _sync.get("enabled"):
            st.success(f"📦 日线自动入库：**已开启** · 每日 {_sync.get('hour', 15):02d}:{_sync.get('minute', 40):02d}"
                       f" · 范围 {_sync['params'].get('pool_name', '自选股')} · 下次 {_sync.get('next') or '-'}"
                       "（⏰定时任务 页可改）")
    except Exception:
        pass


# iFinD 字段/指标代码 → 中文名 对照（没收录的按原代码显示，不挡数据）
FIELD_CN = {
    # 通用
    "time": "时间", "date": "日期", "thscode": "代码", "数据源": "数据源",
    "tradeDate": "交易日期", "tradeTime": "交易时间",
    # 行情（实时/历史/高频/快照返回的字段）
    "latest": "最新价", "open": "开盘价", "high": "最高价", "low": "最低价",
    "close": "收盘价", "preClose": "昨收", "preclose": "昨收",
    "volume": "成交量", "amount": "成交额", "avgPrice": "均价",
    "change": "涨跌额", "changeRatio": "涨跌幅", "turnoverRatio": "换手率",
    "pe": "市盈率", "pb": "市净率", "totalShares": "总股本",
    "floatShares": "流通股本", "marketValue": "总市值",
    # 基本面指标（ths_ 前缀是同花顺指标代码，可输进"指标"框查询）
    "ths_pe_ttm_stock": "市盈率TTM", "ths_pb_stock": "市净率",
    "ths_ps_ttm_stock": "市销率TTM", "ths_pcf_ocf_ttm_stock": "市现率TTM",
    "ths_market_value_stock": "总市值", "ths_float_market_value_stock": "流通市值",
    "ths_total_share_stock": "总股本", "ths_float_share_stock": "流通股本",
    "ths_np_atoopc_pit_stock": "归母净利润", "ths_or_stock": "营业收入",
    "ths_eps_basic_stock": "每股收益", "ths_bps_stock": "每股净资产",
    "ths_roe_weighted_stock": "净资产收益率", "ths_dividend_yield_stock": "股息率",
    # HTTP 行情端点风格指标（cmd_history_quotation/real_time_quotation）
    "pe_ttm": "市盈率TTM", "ps": "市销率", "pcf": "市现率",
    "totalCapital": "总市值", "floatCapitalOfAShares": "流通市值",
    "floatSharesOfAShares": "流通股本", "transactionAmount": "成交笔数",
    # 公告查询返回字段
    "reportDate": "公告日期", "secName": "证券简称", "ctime": "发布时间",
    "reportTitle": "公告标题", "pdfURL": "PDF链接", "seq": "编号",
}

# 实时行情默认指标集（2026-08 实测这版 Linux SDK 支持的全部 RQ 字段）
RQ_DEFAULT_IND = ("latest,preClose,open,high,low,change,changeRatio,"
                  "volume,amount,turnoverRatio,avgPrice,pb,totalShares")


# ---------------------------------------------------------------- 结果渲染
def _show(df, res, err, source="同花顺 iFinD", key="", ts=None):
    if err not in (0, None):
        st.error(f"调用失败 errorcode={err}：{getattr(res, 'errmsg', res)}")
        return
    if df is None:
        st.code(str(res)[:3000], language="text")
        return
    if df.empty:
        st.warning("调用成功但返回为空（检查指标名/参数/权限）")
        return

    shown = df.copy()
    # 列名去重（THS_WC 等接口可能返回同名列，重复列名会让 Arrow 序列化直接崩）
    seen: dict[str, int] = {}
    deduped = []
    for c in shown.columns.astype(str):
        if c in seen:
            seen[c] += 1
            deduped.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            deduped.append(c)
    shown.columns = deduped
    # 在表格中保留来源列，导出/截图后仍能追溯数据出处。
    shown.insert(0, "数据源", source)
    nrow, ncol = shown.shape

    # 一行小字交代规模/来源/时间
    st.caption(f"{nrow} 行 × {ncol} 列　·　数据源：**{source}**"
               + (f"　·　查询于 {ts:%H:%M:%S}" if ts else ""))

    # ---- 数据本体：统一原始表格，全部行 × 全部列，中文列名
    disp: dict[str, str] = {}
    used: set = set()
    for c in shown.columns:
        name = FIELD_CN.get(c) or c
        if name in used:
            name = f"{name}({c})"  # 两个代码撞同一中文名时保留英文消歧
        used.add(name)
        disp[c] = name
    shown_cn = shown.rename(columns=disp)
    st.dataframe(shown_cn, width="stretch", height=min(35 * (nrow + 1) + 3, 600))

    # ---- 字段说明（类型/中文名/示例）折叠，不占首屏
    with st.expander(f"🔤 字段说明（{ncol} 个字段 · 类型 / 中文名 / 示例）", expanded=False):
        meta = pd.DataFrame({
            "字段": shown.columns.astype(str),
            "中文名": [FIELD_CN.get(c, "") for c in shown.columns],
            "类型": [str(t) for t in shown.dtypes],
            "非空": [int(shown[c].notna().sum()) for c in shown.columns],
            "示例": [next((str(v) for v in shown[c] if pd.notna(v)), "") for c in shown.columns],
        })
        st.dataframe(meta, width="stretch", hide_index=True,
                     height=min(35 * (ncol + 1) + 3, 420))


def _go(fn, *args, key="", **kwargs):
    """执行查询并把结果与调用参数存进 session_state——
    结果在页面重跑（切控件/自动刷新/切菜单）后不丢，直到下次查询覆盖。"""
    k = key or fn.__name__
    try:
        with st.spinner("查询中…"):
            st.session_state[f"ifind_res_{k}"] = fn(*args, **kwargs)
            st.session_state[f"ifind_call_{k}"] = (fn, args, kwargs)
            st.session_state[f"ifind_ts_{k}"] = datetime.now()
    except Exception as e:
        st.session_state.pop(f"ifind_res_{k}", None)
        st.error(str(e))


def _auto(key, fn, *args, **kwargs):
    """首次进页面（无缓存结果）时按默认参数自动查一次——打开页面即见数据。"""
    if f"ifind_res_{key}" not in st.session_state:
        _go(fn, *args, key=key, **kwargs)


def _render(key, refresh=False, source="同花顺 iFinD"):
    """渲染某查询点位的上次结果（若有）。refresh=True 且开着实况轮询时，
    每个周期先用缓存的参数重查一次再展示，自动刷新才真正出活数据。"""
    res = st.session_state.get(f"ifind_res_{key}")
    if res is None:
        return
    if refresh:
        fn, args, kwargs = st.session_state.get(f"ifind_call_{key}", (None, (), {}))
        if fn is not None:
            try:
                res = fn(*args, **kwargs)
                st.session_state[f"ifind_res_{key}"] = res
                st.session_state[f"ifind_ts_{key}"] = datetime.now()
            except Exception:
                pass  # 刷新失败保留上次结果
    ts = st.session_state.get(f"ifind_ts_{key}")
    try:
        _show(*res, source=source, key=key, ts=ts)
    except Exception as e:
        # 通用控制台会调到形状未知的返回，渲染兜底——挂一处不拖垮整页
        st.error(f"结果渲染失败：{e}")


# ---------------------------------------------------------------- 小件
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


def _ltd() -> datetime:
    """最近交易日（近似：盘中/盘前回退一天，周末回退到周五；节假日误差无害）。"""
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _live_row(key_prefix: str, default_on: bool = False) -> bool:
    """卡片内的自动刷新行：开关 + 间隔选择，返回是否开启。"""
    c1, c2 = st.columns([1.4, 1])
    live = c1.toggle("⏱ 自动实时刷新", value=default_on, key=f"{key_prefix}_live")
    sec = c2.selectbox("刷新间隔（秒）", [3, 5, 10, 15, 30], index=2,
                       key=f"{key_prefix}_sec", label_visibility="collapsed",
                       disabled=not live)
    if live:
        if st_autorefresh:
            st_autorefresh(interval=sec * 1000, key=f"{key_prefix}_autorefresh")
            st.caption(f"⏱ 每 {sec} 秒自动刷新中 · {datetime.now():%H:%M:%S}")
        else:
            st.warning("未安装 streamlit-autorefresh，无法自动刷新；请安装后重启看板")
    return live


# ---------------------------------------------------------------- 子页：行情
def page_realtime():
    st.title("⚡ 实时行情（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.caption("每只股票一行：最新价、昨收、今开、最高、最低、涨跌幅、成交量、成交额、换手率、均价、市净率、总股本。")
        with st.expander("⚙️ 查询参数（默认 茅台+平安，点开可改）", expanded=False):
            codes = _codes_input("代码", "600519,000001", "rq_codes")
            ind = st.text_input("指标", RQ_DEFAULT_IND, key="rq_ind",
                                help="逗号分隔。这版 SDK 支持的实时字段已全部列入默认；"
                                     "指标代码 ↔ 中文名对照见结果下方的「字段说明」")
            if st.button("重新查询", key="rq_go") and codes:
                _go(datasource.ths_realtime, codes, ind)
        rq_live = _live_row("rq", default_on=True)
        _auto("ths_realtime", datasource.ths_realtime, codes, ind)
        _render("ths_realtime", refresh=rq_live)


def page_history():
    st.title("📅 历史行情 · 日K（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.caption("每个交易日一行：开盘价、最高、最低、收盘、成交量、成交额——就是日 K 线的数据。")
        with st.expander("⚙️ 查询参数", expanded=False):
            codes = _codes_input("代码", "600519", "hq_codes")
            ind = st.text_input("指标", "open,high,low,close,volume,amount", key="hq_ind")
            c1, c2, c3 = st.columns(3)
            d0 = c1.text_input("开始", (_ltd() - timedelta(days=40)).strftime("%Y-%m-%d"), key="hq_s")
            d1 = c2.text_input("结束", _ltd().strftime("%Y-%m-%d"), key="hq_e")
            params = c3.text_input("参数", "Fill:Original,Interval:D", key="hq_p",
                                   help="复权/周期等，见官方文档（如前复权 Fill:Forward）")
            if st.button("重新查询", key="hq_go") and codes:
                _go(datasource.ths_history, codes, ind, d0, d1, params)
        _auto("ths_history", datasource.ths_history, codes, ind, d0, d1, params)
        _render("ths_history")


def page_highfreq():
    st.title("⏱️ 高频行情 · 分钟K（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.caption("每 N 分钟一行：开盘、最高、最低、收盘、成交量——一天之内的分时 K 线。")
        with st.expander("⚙️ 查询参数", expanded=False):
            codes = _codes_input("代码（单只）", "600519", "hf_codes")
            ind = st.text_input("指标", "open;high;low;close;volume", key="hf_ind",
                                help="分号分隔，指标名见官方文档-高频序列")
            c1, c2, c3 = st.columns(3)
            ltd = _ltd().strftime("%Y-%m-%d")
            h0 = c1.text_input("开始时间", f"{ltd} 09:30:00", key="hf_s")
            h1 = c2.text_input("结束时间", f"{ltd} 15:00:00", key="hf_e")
            interval = c3.selectbox("粒度", ["1min", "5min", "15min", "30min", "60min"], key="hf_iv")
            if st.button("重新查询", key="hf_go") and codes:
                _go(datasource.ths_highfreq, codes[0], ind, h0, h1, interval)
        hf_live = _live_row("hf")
        _auto("ths_highfreq", datasource.ths_highfreq, codes[0], ind, h0, h1, interval)
        _render("ths_highfreq", refresh=hf_live)


def page_snapshot():
    st.title("📸 日内快照（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.caption("指定时刻附近、每 3 秒一笔的盘口快照（最新价/成交量/成交额等）。")
        with st.expander("⚙️ 查询参数", expanded=False):
            codes = _codes_input("代码", "600519,000001", "ss_codes")
            ind = st.text_input("指标", "latest;volume;amount;open;high;low", key="ss_ind",
                                help="分号分隔，指标名见官方文档-日内快照")
            stime = st.text_input("快照时间（留空=最近可用）", "", key="ss_t",
                                  help="支持 HH:MM:SS 或 YYYY-MM-DD HH:MM:SS；自动取前后2分钟窗口")
            if st.button("重新查询", key="ss_go") and codes:
                _go(datasource.ths_snapshot, codes, ind, stime)
        _auto("ths_snapshot", datasource.ths_snapshot, codes, ind, stime)
        _render("ths_snapshot")


# ---------------------------------------------------------------- 子页：基本面 / 特色
def page_basic():
    st.title("🏢 基本面数据（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.subheader("基础数据（市盈率/市值等）", anchor=False)
        st.caption("某只股票在某个交易日的基本面指标值：市盈率TTM、总市值、市净率……每只股票一行。")
        with st.expander("⚙️ 查询参数", expanded=False):
            codes = _codes_input("代码", "600519", "ds_codes")
            ind = st.text_input("指标", "ths_pe_ttm_stock;ths_market_value_stock", key="ds_ind",
                                help="同花顺指标代码，分号分隔。指标后缀即证券类型：_stock=股票 "
                                     "_index=指数 _fund=基金 _bond=债券；查指数就把代码和指标后缀一起换")
            c1, c2 = st.columns(2)
            ds_date = c1.text_input("交易日", _ltd().strftime("%Y-%m-%d"), key="ds_d")
            params = c2.text_input("指标参数（可空）", "", key="ds_p",
                                   help="官方格式：每个指标一组、组间分号、组内逗号，无参数留空。"
                                        "如指标 ths_a;ths_b 配 '2026-08-28;'（b 无参数）。")
            if st.button("重新查询", key="ds_go") and codes:
                _go(datasource.ths_basic, codes, ind, params, ds_date)
        _auto("ths_basic", datasource.ths_basic, codes, ind, params, ds_date)
        _render("ths_basic")

    with st.container(border=True):
        st.subheader("日期序列（指标的历史走势）", anchor=False)
        st.caption("某个基本面指标逐日的历史值——比如市盈率TTM 每天怎么变。每个交易日一行。")
        with st.expander("⚙️ 查询参数", expanded=False):
            codes = _codes_input("代码（单只）", "600519", "dss_codes")
            ind = st.text_input("指标", "ths_pe_ttm_stock", key="dss_ind")
            c1, c2 = st.columns(2)
            ds0 = c1.text_input("开始", f"{_ltd().year}-01-01", key="dss_s")
            ds1 = c2.text_input("结束", _ltd().strftime("%Y-%m-%d"), key="dss_e")
            params = st.text_input("参数（可空）", "", key="dss_p")
            if st.button("重新查询", key="dss_go") and codes:
                _go(datasource.ths_date_serial, codes[0], ind, ds0, ds1, params)
        _auto("ths_date_serial", datasource.ths_date_serial, codes[0], ind, ds0, ds1, params)
        _render("ths_date_serial")

    with st.container(border=True):
        st.subheader("专题报表（高级）", anchor=False)
        with st.expander("⚙️ 函数控制台（按官方文档填函数名与参数）", expanded=False):
            st.caption("函数名/报表名/参数以官方文档-专题报表章节为准（如 THS_Report），填好直接调：")
            fname = st.text_input("函数名", "THS_Report", key="rp_fn")
            args = st.text_input("位置参数（JSON 数组）", '["600519.SH"]', key="rp_args")
            kws = st.text_input("关键字参数（JSON 对象）", '{}', key="rp_kwargs")
            if st.button("调用专题报表", key="rp_go"):
                import json as _json
                try:
                    _go(datasource.ths_call, fname, *_json.loads(args), **_json.loads(kws), key="rp")
                except _json.JSONDecodeError as e:
                    st.error(f"参数 JSON 解析失败：{e}")
        _render("rp")


def page_feature():
    st.title("✨ 特色数据（同花顺 iFinD）")
    header()
    with st.container(border=True):
        st.caption("公告查询 / 智能选股 / 期股联动 / 公告下载——函数名与参数**以官方文档-特色数据章节为准**。")
        with st.expander("⚙️ 函数控制台（按官方文档填函数名与参数）", expanded=False):
            feat = st.selectbox("功能", ["智能选股", "公告查询", "期股联动", "公告下载"], key="ft_sel")
            presets = {
                "智能选股": ("THS_WC", '["涨停 且 市值小于50亿", "stock"]'),
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
                    _go(datasource.ths_call, fname, *_json.loads(args), **_json.loads(kws), key="ft")
                except _json.JSONDecodeError as e:
                    st.error(f"参数 JSON 解析失败：{e}")
        _render("ft")
