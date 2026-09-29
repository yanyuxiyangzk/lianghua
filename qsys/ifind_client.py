"""iFinD（同花顺）数据客户端 —— 独立 SDK 模块。

从 qsys/datasource.py 抽出的纯网络/会话层，可脱离 qsys 单独使用：

    import ifind_client as ic
    df, res, err = ic.ths_history(["600519.SH"], "open,close", "2026-09-01", "2026-09-28")
    df, res, err = ic.ths_realtime(["600519.SH"])

凭证来源优先级：环境变量（THS_IFIND_ACCOUNT/PASSWORD/REFRESH_TOKEN）
→ settings 文件（QSYS_SETTINGS_FILE 或 ./settings.json 的 ths_ifind 节）
→ 注入的配置钩子（qsys 里由 datasource 注入数据库读写）。

qsys 集成时由 datasource 在导入期调用 configure() 注入 ifind_config 表读写；
standalone 使用时无需 configure（token 只缓存在内存）。

通道策略：HTTP API（token 鉴权，不占 SDK 会话数）优先，iFinDPy SDK 兜底；
日内快照等 HTTP 无等价端点的调用走 SDK 优先。
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ------------------------------------------------------------ 配置注入（standalone 时全为 None）
_CONFIG_GET = None   # callable(key: str) -> str | None
_CONFIG_SET = None   # callable(key: str, value: str) -> None
_SETTINGS_FILE = None  # Path | None


def configure(config_get=None, config_set=None, settings_file=None):
    """注入配置持久化钩子（qsys 的 datasource 在导入期调用）。standalone 不用调。"""
    global _CONFIG_GET, _CONFIG_SET, _SETTINGS_FILE
    _CONFIG_GET = config_get
    _CONFIG_SET = config_set
    _SETTINGS_FILE = Path(settings_file) if settings_file else None


def _config_get(key):
    try:
        return _CONFIG_GET(key) if _CONFIG_GET else None
    except Exception:
        return None


def _config_set(key, value):
    if _CONFIG_SET:
        try:
            _CONFIG_SET(key, value)
        except Exception:
            pass


def _settings_path() -> Path | None:
    if _SETTINGS_FILE:
        return _SETTINGS_FILE
    env = os.environ.get("QSYS_SETTINGS_FILE")
    if env:
        return Path(env)
    p = Path("./settings.json")
    return p if p.exists() else None





_THS = {"logged_in": False, "cooldown_until": 0.0}





_THS_API = "https://quantapi.51ifind.com/api/v1"


_THS_HTTP = {"access_token": "", "until": 0.0}


_HF_INIT_SPAN_DAYS = 365 * 4


FINANCIAL_INDICATORS = {
    "利润表": {
        "ths营业收入_stock": "营业收入",
        "ths营业成本_stock": "营业成本",
        "ths营业利润_stock": "营业利润",
        "ths净利润_stock": "净利润",
        "ths归属母公司股东净利润_stock": "归母净利润",
        "ths毛利_stock": "毛利",
        "ths每股收益基本_stock": "基本每股收益",
        "ths每股收益稀释_stock": "稀释每股收益",
    },
    "资产负债表": {
        "ths总资产_stock": "总资产",
        "ths总负债_stock": "总负债",
        "ths股东权益合计_stock": "股东权益",
        "ths归属母公司股东权益_stock": "归母权益",
        "ths流动资产合计_stock": "流动资产",
        "ths非流动资产合计_stock": "非流动资产",
        "ths流动负债合计_stock": "流动负债",
        "ths非流动负债合计_stock": "非流动负债",
        "ths货币资金_stock": "货币资金",
        "ths应收账款_stock": "应收账款",
        "ths存货_stock": "存货",
    },
    "现金流量表": {
        "ths经营活动产生的现金流量净额_stock": "经营现金流净额",
        "ths投资活动产生的现金流量净额_stock": "投资现金流净额",
        "ths筹资活动产生的现金流量净额_stock": "筹资现金流净额",
        "ths现金及现金等价物净增加额_stock": "现金净增加额",
        "ths期末现金及现金等价物余额_stock": "期末现金余额",
    },
}


def _ths_credentials() -> tuple[str, str, str]:
    acc = os.environ.get("THS_IFIND_ACCOUNT", "")
    pwd = os.environ.get("THS_IFIND_PASSWORD", "")
    token = os.environ.get("THS_IFIND_REFRESH_TOKEN", "")
    if not (acc or token) and (_settings_path() and _settings_path().exists()):
        try:
            cfg = json.loads(_settings_path().read_text()).get("ths_ifind", {})
            acc, pwd = acc or cfg.get("account", ""), pwd or cfg.get("password", "")
            token = token or cfg.get("refresh_token", "")
        except Exception:
            pass
    # 还没有 token → 注入的配置源（qsys 里是 ifind_config 表）
    if not token:
        token = _config_get("refresh_token") or ""
    return acc, pwd, token


def _ths_login() -> bool:
    """iFinDPy 登录单例。返回 True 表示可用；否则抛带指引的异常。"""
    if _THS["logged_in"]:
        return True
    cool = _THS["cooldown_until"] - time.time()
    if cool > 0:
        raise RuntimeError(
            f"iFinD 登录冷却中（上次被限流，约 {int(cool) // 60 + 1} 分钟后自动重试）；"
            "频繁重试会让服务端锁定窗口一直续期，请稍等")
    try:
        import iFinDPy as ths
    except ImportError:
        raise RuntimeError(
            "未安装 iFinDPy SDK：官方包不在 PyPI 且非 pip 包，"
            "将从 quantapi.51ifind.com 下载的 Linux tar.gz 放入 "
            "qsys/ifind_sdk/ 后重新 build qsys 镜像即可")
    acc, pwd, token = _ths_credentials()
    if acc and pwd:
        ret = ths.THS_iFinDLogin(acc, pwd)
    elif token:
        try:
            # 新版 SDK（Windows 版等）支持单参数 refresh_token 登录
            ret = ths.THS_iFinDLogin(token)
        except TypeError:
            # Linux tar.gz 版只有 THS_iFinDLogin(username, password)，
            # 原生库无 refresh token 处理逻辑（实测返回 -2 认证失败）
            raise RuntimeError(
                "当前 Linux 版 iFinDPy SDK 仅支持账号密码登录（不认 refresh_token）："
                "请在 settings.json 的 ths_ifind 节填 account/password"
                "（数据接口账号密码），或设环境变量 THS_IFIND_ACCOUNT/THS_IFIND_PASSWORD")
    else:
        raise RuntimeError(
            "未配置同花顺凭证：设置 THS_IFIND_ACCOUNT/THS_IFIND_PASSWORD "
            "或 THS_IFIND_REFRESH_TOKEN（.env 或 settings.json 的 ths_ifind 节）")
    # 返回值版本兼容：老版 int(0=成功,-201=已登录也算成功)；新版 dict/对象带 errorcode
    if isinstance(ret, int):
        errcode = ret
    elif isinstance(ret, dict):
        errcode = ret.get("errorcode", -1)
    else:
        errcode = getattr(ret, "errorcode", -1)
    if errcode not in (0, -201):
        _THS["logged_in"] = False
        # -9 会话超限：冷却 10 分钟（与上方注释/提示一致；不频繁重试以免延续服务端锁定）；
        # -1010 账户登出：不冷却，下次调用自动重试；
        # 其余错误 1 分钟
        _THS["cooldown_until"] = time.time() + (600 if errcode == -9 else 0 if errcode == -1010 else 60)
        hint = {-2: "账号或密码错误，请核对 settings.json ths_ifind 节的 account/password",
                -9: "登录会话数超限（短时登录太频繁）。已自动冷却 10 分钟后再试；"
                    "若长时间不恢复，到 quantapi.51ifind.com 查账号状态或联系同花顺客服",
                -1010: "账户已登出（session expired），将自动重新登录"}
        raise RuntimeError(f"iFinD 登录失败(errorcode={errcode})：{hint.get(errcode, '检查账号/权限/网络')}")
    _THS["logged_in"] = True
    return True


def _to_ths_code(code: str) -> str:
    """SH600519 → 600519.SH（同花顺 thscode 版式）。"""
    m = re.match(r"^([A-Za-z]{2})(\d{6})$", code)
    return f"{m.group(2)}.{m.group(1).upper()}" if m else code


def _tables_to_df(tables):
    """把 iFinD JSON 结构 tables=[{thscode, time:[...], table:{指标:[值]}}] 拼成 DataFrame。
    THS_DateSerial 等旧版 outflag 接口不走 dataframe 格式，直接返回这种 dict；
    get_trade_dates 等则返回 {time:[...]} 裸 dict（非列表）。"""
    if isinstance(tables, dict):
        tables = [tables]
    if not isinstance(tables, list) or not tables:
        return None
    frames = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        times = t.get("time") or t.get("times") or []
        tab = t.get("table") or {}
        try:
            f = pd.DataFrame(tab)
        except (ValueError, TypeError):
            f = pd.DataFrame([tab])  # 标量值 dict → 单行
        if f.empty and times:
            f = pd.DataFrame({"time": times})  # 纯时间表（交易日历）
        elif times and len(times) == len(f) and "time" not in f.columns:
            f.insert(0, "time", times)
        if t.get("thscode") and "thscode" not in f.columns:
            f.insert(1 if "time" in f.columns else 0, "thscode", t["thscode"])
        frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else None


def _ths_access_token() -> str:
    # 1. 先检查内存缓存
    if _THS_HTTP["access_token"] and time.time() < _THS_HTTP["until"]:
        return _THS_HTTP["access_token"]
    
    # 2. 从数据库读取 access_token
    db_token = _config_get("access_token")
    db_expires = _config_get("token_expires_at")
    if db_token and db_expires:
        try:
            from datetime import datetime
            expires = datetime.strptime(db_expires, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expires:
                _THS_HTTP["access_token"] = db_token
                _THS_HTTP["until"] = expires.timestamp()
                return db_token
        except Exception:
            pass
    
    # 3. 用 refresh_token 获取新的 access_token
    _, _, token = _ths_credentials()
    if not token:
        raise RuntimeError("iFinD HTTP 通道需要 refresh_token（settings.json 或数据库 ifind_config 表）")
    import requests
    res = requests.post(f"{_THS_API}/get_access_token", timeout=15,
                        headers={"Content-Type": "application/json", "refresh_token": token}).json()
    at = (res.get("data") or {}).get("access_token") or ""
    if not at:
        raise RuntimeError(f"refresh_token 换 access_token 失败：{res.get('errmsg') or str(res)[:120]}"
                           "——请更新数据库 ifind_config 表的 refresh_token")
    # 4. 保存到数据库和内存缓存
    from datetime import datetime, timedelta
    expires = datetime.now() + timedelta(days=6)
    _config_set("access_token", at)
    _config_set("token_expires_at", expires.strftime("%Y-%m-%d %H:%M:%S"))
    _THS_HTTP.update(access_token=at, until=expires.timestamp())
    return at


def _ths_http(endpoint: str, payload: dict, _retried: bool = False):
    """iFinD HTTP API 调用 → (df, res, errcode)；tables JSON 复用 _tables_to_df 解析。
    -1302（access_token 失效/被轮换）时自动作废旧 token 重取一次再重试。"""
    import requests
    at = _ths_access_token()
    res = requests.post(f"{_THS_API}/{endpoint}", json=payload, timeout=30,
                        headers={"Content-Type": "application/json", "access_token": at}).json()
    err = res.get("errorcode", -1)
    if err == -1302 and not _retried:
        # token 失效（可能被其他进程/终端轮换）：作废缓存并重取
        _THS_HTTP.update(access_token="", until=0.0)
        try:
            _config_set("access_token", "")
            _config_set("token_expires_at", "")
        except Exception:
            pass
        try:
            _ths_access_token()  # 用 refresh_token 重取并入库
            return _ths_http(endpoint, payload, _retried=True)
        except Exception:
            pass  # refresh_token 也失效 → 原样返回 -1302，由上层走 SDK 兜底
    return _tables_to_df(res.get("tables")), res, err


def _sdk_or_http(sdk_call, http_call):
    """iFinD 通道分发：HTTP(token) 优先——不占 SDK 会话数、无登录限流；HTTP 异常/错误码非0 时落 SDK。
    SDK 也不可用（限流冷却等）时返回 HTTP 侧（可能为空的）结果，**不再向外抛限流异常**——
    页面统一按"取数失败"提示，而不是被异常带崩（2026-09 踩坑：HTTP 瞬时失败→SDK 冷却异常把页面打崩）。"""
    http_res = (None, None, -1)
    try:
        df, res, err = http_call()
        if err in (0, None):
            return df, res, err
        http_res = (df, res, err)
    except Exception:
        pass
    try:
        return sdk_call()
    except Exception:
        return http_res


def _sdk_first(sdk_call, http_call):
    """SDK 优先（仅日内快照等 HTTP 无等价端点的调用使用）；登录类失败落 HTTP 通道。"""
    try:
        _ths_login()
    except Exception:
        return http_call()
    return sdk_call()


def ths_call(func_name: str, *args, **kwargs):
    """通用 iFinD 调用：登录 → 按函数名分发 → 返回 (DataFrame|None, 原始对象, 错误码)。
    iFinDPy 返回形如 THSData 对象（.data 为 DataFrame，.errorcode 为 0 表示成功）。
    若返回 -1010（账户登出），自动重置登录状态并重试一次。"""
    _ths_login()
    import iFinDPy as ths

    fn = getattr(ths, func_name, None)
    if fn is None:
        raise RuntimeError(f"iFinDPy 没有函数 {func_name}——以官方文档的函数名为准")
    res = fn(*args, **kwargs)

    def _parse_result(r):
        """解析 iFinD 返回结果，统一转为 (DataFrame, errorcode)"""
        if isinstance(r, pd.DataFrame):
            return r, 0
        if isinstance(r, dict):
            return _tables_to_df(r.get("tables")), r.get("errorcode", -1)
        # THSData 对象或类似对象
        err = getattr(r, "errorcode", None)
        data = getattr(r, "data", None)
        # data 可能是 DataFrame、dict、list 或特殊表格对象
        if isinstance(data, pd.DataFrame):
            return data, err
        if isinstance(data, dict):
            return _tables_to_df(data.get("tables")), err
        if isinstance(data, (list, tuple)) and data:
            try:
                return pd.DataFrame(data), err
            except Exception:
                pass
        # 尝试直接转 DataFrame（如 data 是表格字符串或嵌套结构）
        if data is not None:
            try:
                df = pd.DataFrame(data)
                if not df.empty:
                    return df, err
            except Exception:
                pass
        return data, err

    df, err = _parse_result(res)

    # -1010: 账户登出（session expired），重置状态并重试一次
    if err == -1010:
        _THS["logged_in"] = False
        _ths_login()
        res = fn(*args, **kwargs)
        df, err = _parse_result(res)

    return df, res, err


def ths_realtime(codes: list[str], indicators: str = "latest,open,high,low,volume,amount"):
    """实时行情（SDK: THS_RQ / HTTP: real_time_quotation）。indicators 逗号分隔。"""
    cs = ",".join(_to_ths_code(c) for c in codes)
    return _sdk_or_http(
        lambda: ths_call("THS_RQ", cs, indicators),
        lambda: _ths_http("real_time_quotation", {"codes": cs, "indicators": indicators}))


def _parse_fn_params(params: str) -> dict:
    """'Fill:Original,Interval:D' → {'Fill':'Original','Interval':'D'}（HTTP functionpara）。"""
    return dict(kv.split(":", 1) for kv in (params or "").split(",") if ":" in kv)


def ths_history(codes: list[str], indicators: str, start: str, end: str,
                params: str = "Fill:Original,Interval:D"):
    """历史行情（SDK: THS_HQ / HTTP: cmd_history_quotation）。params 含复权/周期。"""
    cs = ",".join(_to_ths_code(c) for c in codes)
    return _sdk_or_http(
        lambda: ths_call("THS_HQ", cs, indicators, params, start, end),
        lambda: _ths_http("cmd_history_quotation",
                          {"codes": cs, "indicators": indicators, "startdate": start,
                           "enddate": end, "functionpara": _parse_fn_params(params)}))


def ths_highfreq(code: str, indicators: str, start: str, end: str, interval: str = "1min"):
    """高频数据（SDK: THS_HF / HTTP: high_frequency）。start/end 形如 2026-08-27 09:30:00。
    实测（2026-08 Linux SDK）：SDK 指标分号分隔、Interval 为裸数字分钟（1 分钟传空参）；
    HTTP 端指标逗号分隔。"""
    m = re.match(r"\s*(\d+)", interval or "")
    sdk_ind = indicators.replace(",", ";")
    sdk_param = f"Interval:{m.group(1)}" if m and m.group(1) != "1" else ""

    def http():
        payload = {"codes": _to_ths_code(code), "indicators": indicators.replace(";", ","),
                   "starttime": start, "endtime": end}
        if m and m.group(1) != "1":
            payload["functionpara"] = {"Interval": m.group(1)}
        return _ths_http("high_frequency", payload)

    return _sdk_or_http(
        lambda: ths_call("THS_HF", _to_ths_code(code), sdk_ind, sdk_param, start, end), http)


def ths_snapshot(codes: list[str], indicators: str, snap_time: str = ""):
    """日内快照（SDK: THS_SS dataframe 版）。snap_time 支持 HH:MM:SS 或完整时间。
    实测：SDK 指标分号分隔；params 必填 dataType:Original；begin==end 返回空，
    必须给时间窗——单时点取 [t-2min, t]；留空=最新：先取最近 10 分钟，
    非交易时段为空则逐日回退尾盘 14:55-15:00 窗口（最多回退 5 天）。
    HTTP 无快照端点（备用通道退化为实时行情），分发保持 SDK 优先（_sdk_first）。"""
    codes_s = ",".join(_to_ths_code(c) for c in codes)

    def http():
        return _ths_http("real_time_quotation",
                         {"codes": codes_s, "indicators": indicators.replace(";", ",")})

    now = datetime.now()
    t = snap_time.strip()
    if t:
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", t):
            t = f"{now:%Y-%m-%d} {t}"
        end = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")  # 格式错误会抛给 _go 提示
        begin = end - timedelta(minutes=2)
        return _sdk_first(
            lambda: ths_call("THS_SS", codes_s, indicators, "dataType:Original",
                             f"{begin:%Y-%m-%d %H:%M:%S}", f"{end:%Y-%m-%d %H:%M:%S}"), http)
    df, res, err = _sdk_first(
        lambda: ths_call("THS_SS", codes_s, indicators, "dataType:Original",
                         f"{now - timedelta(minutes=10):%Y-%m-%d %H:%M:%S}",
                         f"{now:%Y-%m-%d %H:%M:%S}"), http)
    if df is None or df.empty:
        for back in range(1, 6):
            d = now - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            try:
                _ths_login()
            except Exception:
                break  # HTTP 通道无历史快照可回退，直接返回空
            df, res, err = ths_call("THS_SS", codes_s, indicators, "dataType:Original",
                                    f"{d:%Y-%m-%d} 14:55:00", f"{d:%Y-%m-%d} 15:00:00")
            if df is not None and not df.empty:
                break
    return df, res, err


def ths_basic(codes: list[str], indicators: str, params: str = "", date: str = ""):
    """基础数据（SDK: THS_BD / HTTP: basic_data_service）：截面基本面指标。

    官方格式：指标分号分隔；params 为"每指标一组"的参数串（组间分号、组内逗号，
    无参数留空），如 'ths_pe_ttm_stock;ths_stock_short_name_stock' 配 '2026-08-28;'。
    params 留空时每个指标默认给交易日参数（估值/价格类指标必需；名称类会忽略）。
    """
    d = date.strip() or f"{datetime.now():%Y-%m-%d}"
    codes_s = ",".join(_to_ths_code(c) for c in codes)
    inds = [x.strip() for x in indicators.replace("；", ";").split(";") if x.strip()]

    # 组装每指标参数组（与官方 paramOption 同格式）
    if params.strip():
        groups = params.replace("；", ";").split(";")
        groups = [groups[i] if i < len(groups) else groups[-1] for i in range(len(inds))]
    else:
        groups = [d] * len(inds)

    def http():
        # 实测：HTTP 端截面指标 indiparams 日期要 YYYYMMDD（无横线），否则静默 None
        return _ths_http("basic_data_service",
                         {"codes": codes_s,
                          "indipara": [{"indicator": i,
                                        "indiparams": [p.replace("-", "") for p in g.split(",")]}
                                       for i, g in zip(inds, groups)]})

    def sdk():
        # THS_BD 原生多指标（优于 THS_DS 的逐指标循环——实测 THS_DS 多指标恒 -209）
        param_option = ";".join(groups)
        return ths_call("THS_BD", codes_s, ";".join(inds), param_option)

    return _sdk_or_http(sdk, http)


def ths_date_serial(code: str, indicators: str, start: str, end: str, params: str = "",
                    fill: str = "Previous"):
    """日期序列（SDK: THS_DateSerial / HTTP: date_sequence）：基本面/专题指标的时序。"""
    if fill not in ("Previous", "Original"):
        raise ValueError("不支持的日期序列填充方式")
    cs = _to_ths_code(code)
    inds = [x.strip() for x in indicators.replace("；", ";").replace(",", ";").split(";") if x.strip()]
    return _sdk_or_http(
        lambda: ths_call("THS_DateSerial", cs, indicators, params,
                         "Fill:Original" if fill == "Original" else "", start, end),
        lambda: _ths_http("date_sequence",
                          {"codes": cs, "startdate": start, "enddate": end,
                           "functionpara": {"Days": "Tradedays", "Fill": fill, "Interval": "D"},
                           "indipara": [{"indicator": i, "indiparams": [params]} for i in inds]}))


def ths_financial_statement(codes: list[str], statement_type: str = "利润表",
                            start: str = "", end: str = "") -> tuple:
    """获取财务报表数据（三大报表 + 财务指标）。

    Args:
        codes: 股票代码列表
        statement_type: 报表类型（利润表/资产负债表/现金流量表/财务指标）
        start: 开始日期（YYYY-MM-DD）
        end: 结束日期（YYYY-MM-DD）

    Returns:
        (DataFrame, result, error)
    """
    if statement_type == "财务指标":
        indicators = ";".join(FINANCIAL_RATIOS.keys())
    else:
        indicators = ";".join(FINANCIAL_INDICATORS.get(statement_type, {}).keys())

    if not indicators:
        return pd.DataFrame(), None, "未知报表类型"

    codes_s = ",".join(_to_ths_code(c) for c in codes)
    today = datetime.now().strftime("%Y-%m-%d")
    start = start or f"{datetime.now().year}-01-01"
    end = end or today

    # 使用 THS_DateSerial 获取时序财务数据
    # SDK 用中文指标码；HTTP 通道换拼音码 + Interval=Q（见 FIN_HTTP_CODES 注释）
    http_inds = FIN_HTTP_CODES.get(statement_type, {})

    def _http():
        if not http_inds:
            return pd.DataFrame(), None, f"HTTP 通道暂不支持{statement_type}（需 SDK 通道）"
        return _ths_http("date_sequence",
                         {"codes": codes_s, "startdate": start, "enddate": end,
                          "functionpara": {"Days": "Tradedays", "Fill": "Previous", "Interval": "Q"},
                          "indipara": [{"indicator": i, "indiparams": [""]} for i in http_inds]})

    return _sdk_or_http(
        lambda: ths_call("THS_DateSerial", codes_s, indicators, "", "", start, end),
        _http)


def ths_wcquery(query: str, domain: str = "stock"):
    """问财语义查询（SDK: THS_WCQuery / HTTP: smart_stock_picking，HTTP 优先）。"""
    return _sdk_or_http(
        lambda: ths_call("THS_WCQuery", query, domain),
        lambda: _ths_http("smart_stock_picking",
                          {"searchstring": query, "searchtype": domain}))


def ths_dr_report(report_id: str, params: str, columns: str):
    """专题报表（HTTP: api/v1/data_pool 优先 / SDK: THS_DR）。龙虎榜等专题数据用。

    report_id 形如 p04669（每日交易龙虎榜数据）/ p04674（证券营业部交易龙虎榜统计）；
    params 形如 'edate=20260902' 或 'edate=20260902;sbyy=日涨幅偏离值达7%的证券'（键值对）；
    columns 形如 'p04669_f001,p04669_f002'（逗号分隔，自动补 :Y）。
    返回 DataFrame 优先用接口 outParams 的中文名命名（无中文名保留字段代码）。
    """
    colopt = ",".join(f"{c.strip()}:Y" for c in columns.split(",") if c.strip())
    fpara = dict(kv.split("=", 1) for kv in (params or "").split(";") if "=" in kv)

    def http():
        res = _ths_http("data_pool", {"reportname": report_id, "functionpara": fpara,
                                      "outputpara": colopt})
        df, r, err = res
        if df is not None and not df.empty:
            cn = (r.get("outParams") or {})
            df = df.rename(columns={c: cn[c] for c in df.columns
                                    if cn.get(c) and cn[c] != c})
        return df, r, err

    return _sdk_or_http(
        lambda: ths_call("THS_DR", report_id, params, colopt),
        http)


def ths_trade_dates(exchange: str = "SSE", start: str = "", end: str = ""):
    """交易日历（SDK: THS_Date_Query / HTTP: get_trade_dates）。exchange: SSE/SZSE。"""
    start = start or f"{datetime.now().year}-01-01"
    end = end or f"{datetime.now():%Y-%m-%d}"
    mcode = {"SSE": "212001", "SZSE": "212100"}.get(exchange, "212001")
    return _sdk_or_http(
        lambda: ths_call("THS_Date_Query", exchange, "dateType:0", start, end),
        lambda: _ths_http("get_trade_dates", {"marketcode": mcode,
                                              "functionpara": {"dateType": "0"},
                                              "startdate": start, "enddate": end}))


def ths_announce(codes: list[str], days: int = 7):
    """公告查询（SDK: THS_ReportQuery / HTTP: report_query）。
    返回字段：reportDate/thscode/secName/ctime/reportTitle/pdfURL/seq。"""
    end = datetime.now().strftime("%Y-%m-%d")
    begin = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    cs = ",".join(_to_ths_code(c) for c in codes)
    output = "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y"
    return _sdk_or_http(
        lambda: ths_call("THS_ReportQuery", cs, f"beginrDate:{begin};endrDate:{end}", output),
        # 实测：HTTP 端 beginrDate/endrDate 是顶层字段，塞进 functionpara 会被忽略
        lambda: _ths_http("report_query", {"codes": cs, "beginrDate": begin, "endrDate": end,
                                           "outputpara": output}))


def _hf_splits(start: str, end: str, days: int) -> list[tuple[str, str]]:
    """把日期区间切成不超过 days 天的首尾相接分段。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    while s <= e:
        nxt = min(s + pd.Timedelta(days=days - 1), e)
        out.append((s.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        s = nxt + pd.Timedelta(days=1)
    return out


def _hf_collect(ths_code: str, indicators: str, start: str, end: str,
                interval: str) -> list:
    """分段调用 ths_highfreq 并合并结果。

    THS_HF 单次请求上限 200 万数据点（行×指标，错误码 -4304；实测 8 年窗口
    288 万点被拒、661 天窗口 95 万点通过）。初始按 _HF_INIT_SPAN_DAYS 分段，
    仍超限的分段自动对半拆分重试，直到单日为止。
    """
    frames = []
    pending = _hf_splits(start, end, _HF_INIT_SPAN_DAYS)
    while pending:
        chunk_start, chunk_end = pending.pop(0)
        df, _res, err = ths_highfreq(
            ths_code, indicators,
            f"{chunk_start} 09:25:00", f"{chunk_end} 15:05:00", interval)
        if err == -4304:
            s, e = pd.Timestamp(chunk_start), pd.Timestamp(chunk_end)
            if (e - s).days < 2:
                raise RuntimeError(
                    f"同花顺分钟历史单日也返回错误码 -4304（{chunk_start}）")
            mid = s + (e - s) / 2
            # 左半段插到队首，保持整体时间顺序
            pending[0:0] = [(chunk_start, mid.strftime("%Y-%m-%d")),
                            ((mid + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), chunk_end)]
            continue
        if err not in (0, None):
            raise RuntimeError(
                f"同花顺分钟历史返回错误码 {err}（分段 {chunk_start}~{chunk_end}）")
        # 早期年份可能没有分钟数据，分段为空属正常；全部为空才报错
        if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
            frames.append(df)
    return frames
