"""数据源层：统一行情访问、来源标识、全局切换。

设计：
  - SOURCES 注册表：qlib_local（本地社区库）/ akshare（东财日线·前复权）
  - 全局当前源存 /data/settings.json，分析层各页面统一读取
  - akshare 数据"读穿缓存"进 market.db（market_daily 表带 source 字段），
    data_sources 表登记各源状态 —— 每条数据都有出处
  - 边界：RD-Agent 进化/回测固定用 qlib_local，本层只服务 QSYS 分析/展示
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("QSYS_DATA_DIR", "/data"))
QLIB_DATA_DIR = Path(os.environ.get("QLIB_DATA_DIR", "/data/qlib/cn_data"))
MKT_DB = DATA_DIR / "market.db"
SETTINGS_FILE = DATA_DIR / "settings.json"

SOURCES = {
    "qlib_local": {"name": "Qlib 本地库（社区日线）", "minute": False, "note": "回测同源"},
    "akshare": {"name": "akshare·新浪日线（前复权）", "minute": True, "note": "网页接口，首次抓取较慢"},
    "easytdx": {"name": "easy-tdx·通达信（日线/分钟/逐笔竞价）", "minute": True,
                "note": "TDX TCP 行情，含集合竞价逐笔标记"},
    "ths_ifind": {"name": "同花顺 iFinD（日线·官方接口）", "minute": False,
                  "note": "需 iFinDPy SDK + 账号凭证，见 README 同花顺接入"},
}

_QLIB_READY = False


# ---------------------------------------------------------------- 设置
def get_source() -> str:
    try:
        return json.loads(SETTINGS_FILE.read_text()).get("data_source", "qlib_local")
    except Exception:
        return "qlib_local"


def set_source(source: str):
    if source not in SOURCES:
        raise ValueError(f"未知数据源: {source}")
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if SETTINGS_FILE.exists():
        try:
            cfg = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            cfg = {}
    cfg["data_source"] = source
    SETTINGS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- market.db（来源标识）
def _conn():
    c = sqlite3.connect(MKT_DB)
    c.execute("PRAGMA journal_mode=WAL")  # 并发读写更稳（后台采集线程 + 页面读取）
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market_daily(
        source TEXT NOT NULL, code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        fetched_at TEXT, PRIMARY KEY(source, code, date));
    CREATE TABLE IF NOT EXISTS data_sources(
        source TEXT PRIMARY KEY, name TEXT, last_sync TEXT, rows INTEGER, note TEXT);
    -- iFinD 自动入库（⏰定时任务 ifind_*）：
    CREATE TABLE IF NOT EXISTS ifind_basic_daily(
        code TEXT NOT NULL, date TEXT NOT NULL, indicator TEXT NOT NULL,
        value REAL, fetched_at TEXT, PRIMARY KEY(code, date, indicator));
    CREATE TABLE IF NOT EXISTS ifind_announcements(
        seq TEXT PRIMARY KEY, code TEXT, report_date TEXT, title TEXT,
        pdf_url TEXT, ctime TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS ifind_calendar(
        exchange TEXT NOT NULL, date TEXT NOT NULL, PRIMARY KEY(exchange, date));
    CREATE TABLE IF NOT EXISTS ifind_stocklist(
        code TEXT PRIMARY KEY, name TEXT, market TEXT,
        price REAL, prev_close REAL, open REAL, high REAL, low REAL,
        change_pct REAL, volume REAL, amount REAL, turnover REAL,
        quantity_ratio REAL, amplitude REAL,
        pe_ttm REAL, pb REAL, total_mv REAL, float_mv REAL,
        float_shares REAL, total_shares REAL, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS ifind_indexlist(
        code TEXT PRIMARY KEY, name TEXT, market TEXT, category TEXT,
        price REAL, prev_close REAL, open REAL, high REAL, low REAL,
        change_pct REAL, volume REAL, amount REAL, amplitude REAL,
        fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS ifind_realtime(
        code TEXT NOT NULL, datetime TEXT NOT NULL,
        price REAL, prev_close REAL, open REAL, high REAL, low REAL,
        change_pct REAL, volume REAL, amount REAL, turnover REAL,
        quantity_ratio REAL, amplitude REAL,
        float_shares REAL, float_mv REAL,
        PRIMARY KEY(code, datetime));
    CREATE TABLE IF NOT EXISTS ifind_config(
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(market_daily)")]
    if "outstanding_share" not in cols:
        c.execute("ALTER TABLE market_daily ADD COLUMN outstanding_share REAL")
    # ifind_stocklist 补列（升级兼容）
    sl_cols = [r[1] for r in c.execute("PRAGMA table_info(ifind_stocklist)")]
    for col, typ in [("quantity_ratio", "REAL"), ("amplitude", "REAL")]:
        if col not in sl_cols:
            c.execute(f"ALTER TABLE ifind_stocklist ADD COLUMN {col} {typ}")
    # ifind_realtime 索引（按datetime查询优化）
    c.execute("CREATE INDEX IF NOT EXISTS idx_realtime_dt ON ifind_realtime(datetime)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_realtime_code ON ifind_realtime(code)")
    # ifind_realtime 补列（升级兼容）
    rt_cols = [r[1] for r in c.execute("PRAGMA table_info(ifind_realtime)")]
    for col, typ in [("speed", "REAL"), ("pe_ttm", "REAL")]:
        if col not in rt_cols:
            c.execute(f"ALTER TABLE ifind_realtime ADD COLUMN {col} {typ}")
    return c


def _touch_source(source: str):
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM market_daily WHERE source=?", (source,)).fetchone()[0]
        c.execute("INSERT OR REPLACE INTO data_sources (source, name, last_sync, rows, note)"
                  " VALUES (?,?,?,?,?)",
                  (source, SOURCES[source]["name"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   n, SOURCES[source]["note"]))


def source_status() -> list[dict]:
    out = []
    with _conn() as c:
        rows = {r[0]: r for r in c.execute("SELECT source, last_sync, rows FROM data_sources")}
    for key, meta in SOURCES.items():
        r = rows.get(key)
        out.append({"source": key, "name": meta["name"],
                    "last_sync": r[1] if r else "—", "rows": (r[2] if r else 0), "note": meta["note"]})
    return out


# ---------------------------------------------------------------- qlib 通道
def _ensure_qlib():
    global _QLIB_READY
    if not _QLIB_READY:
        import qlib

        # expression_cache/dataset_cache 置 None：禁用 qlib 磁盘缓存
        # （多进程共享 /root/.qlib 缓存会产生竞态，偶发返回空数据——已实测复现）
        qlib.init(provider_uri=str(QLIB_DATA_DIR), region="cn",
                  expression_cache=None, dataset_cache=None)
        _QLIB_READY = True


def _qlib_daily(codes: list[str], fields: list[str], start: str, end: str) -> pd.DataFrame:
    _ensure_qlib()
    from qlib.data import D

    df = D.features(codes, fields, start_time=start, end_time=end)
    return df if df is not None else pd.DataFrame()


# ---------------------------------------------------------------- akshare 通道（读穿缓存）
def _to_ak_symbol(code: str) -> str:
    """SH600519 → sh600519（新浪通道用小写交易所前缀）。"""
    m = re.match(r"^([A-Za-z]{2})(\d{6})$", code)
    return (m.group(1).lower() + m.group(2)) if m else code.lower()


def _ak_normalize(df: pd.DataFrame) -> pd.DataFrame:
    # 东财接口为中文列，新浪接口为英文列，统一兼容
    m = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
         "成交量": "volume", "成交额": "amount"}
    df = df.rename(columns=m)
    df["date"] = df["date"].astype(str)
    keep = ["date", "open", "high", "low", "close", "volume", "amount"]
    if "outstanding_share" in df.columns:
        keep.append("outstanding_share")
    return df[keep]


def _ak_fetch_daily(code: str, start: str, end: str) -> int:
    """抓日线（前复权）写入 market.db，返回写入行数。

    主通道：新浪（东财接口在本机网络被重置，已实测）。"""
    import akshare as ak

    sym = _to_ak_symbol(code)
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                             end_date=end.replace("-", ""), adjust="qfq")
    if df is None or df.empty:
        return 0
    df = _ak_normalize(df)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        rows_vals = [(code, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount,
                      getattr(r, "outstanding_share", None), now) for r in df.itertuples()]
        c.executemany(
            "INSERT OR REPLACE INTO market_daily (source, code, date, open, high, low, close, volume, amount, outstanding_share, fetched_at)"
            " VALUES ('akshare',?,?,?,?,?,?,?,?,?,?)",  # 1 literal + 10 占位 = 11 列
            rows_vals)
    return len(df)


def _ak_daily_cached(code: str, start: str, end: str) -> pd.DataFrame:
    """读穿缓存：库内覆盖不足则补抓（仅补缺段）。"""
    with _conn() as c:
        have = c.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM market_daily"
                         " WHERE source='akshare' AND code=?", (code,)).fetchone()
    need_fetch = True
    if have and have[2] > 0 and have[0] <= start and have[1] >= end:
        need_fetch = False
    elif have and have[2] > 0 and have[0] <= end and have[1] >= start:
        # 部分覆盖：补抓缺口（简化：直接重抓全段，INSERT OR REPLACE 幂等）
        need_fetch = True
    if need_fetch:
        try:
            _ak_fetch_daily(code, start, end)
            time.sleep(0.15)  # 礼貌限速
            _touch_source("akshare")
        except Exception as e:
            import logging
            logging.getLogger("datasource").warning(f"akshare 抓取失败 {code}: {e}")
    with _conn() as c:
        df = pd.read_sql("SELECT date, open, high, low, close, volume, amount FROM market_daily"
                         " WHERE source='akshare' AND code=? AND date BETWEEN ? AND ? ORDER BY date",
                         c, params=(code, start, end))
    return df


# ---------------------------------------------------------------- 同花顺 iFinD 通道
# SDK 不在 PyPI 且非 pip 包：官方 tar.gz 放入 qsys/ifind_sdk/ 后随镜像构建安装
# （解压到 /opt/iFinD + site-packages/iFinDPy.pth，见 Dockerfile）。
# 凭证（三选二之一）：环境变量 THS_IFIND_ACCOUNT + THS_IFIND_PASSWORD，
# 或 THS_IFIND_REFRESH_TOKEN；也可写在 settings.json 的 "ths_ifind" 节。
# cooldown：登录失败（尤其 -9 会话超限）后熔断一段时间再重试——
# 页面自动刷新会反复触发登录，不限流会把服务端锁定窗口一直续期。
_THS = {"logged_in": False, "cooldown_until": 0.0}


def _ths_credentials() -> tuple[str, str, str]:
    acc = os.environ.get("THS_IFIND_ACCOUNT", "")
    pwd = os.environ.get("THS_IFIND_PASSWORD", "")
    token = os.environ.get("THS_IFIND_REFRESH_TOKEN", "")
    if not (acc or token) and SETTINGS_FILE.exists():
        try:
            cfg = json.loads(SETTINGS_FILE.read_text()).get("ths_ifind", {})
            acc, pwd = acc or cfg.get("account", ""), pwd or cfg.get("password", "")
            token = token or cfg.get("refresh_token", "")
        except Exception:
            pass
    # 如果还是没有 token，从数据库读取
    if not token:
        try:
            with _qconn() as c:
                row = c.execute("SELECT value FROM ifind_config WHERE key='refresh_token'").fetchone()
                if row:
                    token = row[0]
        except Exception:
            pass
    return acc, pwd, token


def _get_config_value(key: str) -> str | None:
    """从数据库 ifind_config 表读取配置值。"""
    try:
        with _qconn() as c:
            row = c.execute("SELECT value FROM ifind_config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _set_config_value(key: str, value: str):
    """写入数据库 ifind_config 表。"""
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _qconn() as c:
        c.execute("INSERT OR REPLACE INTO ifind_config (key, value, updated_at) VALUES (?, ?, ?)",
                  (key, value, now))


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


def _ths_fetch_daily(code: str, start: str, end: str) -> int:
    """iFinD 日线 → market.db（source='ths_ifind'），返回写入行数。
    走 ths_history（SDK 优先 / HTTP 兜底）。

    联调注意（拿到账号后核对一次）：
      - volume 单位（股/手）与其他源是否一致，不一致则在此 ×100 对齐
      - THS_HQ 默认不复权；如需前复权在第三个参数加复权标志（以官方文档为准）
    """
    df, _res, _err = ths_history([code], "open,high,low,close,volume,amount", start, end, "")
    if df is None or df.empty:
        return 0
    # 列名归一：time/date/trade_date → date；数值列小写对齐
    df.columns = [str(c).strip().lower() for c in df.columns]
    date_col = next((c for c in ("time", "date", "trade_date") if c in df.columns), None)
    if date_col is None:
        return 0
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    if df.empty:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO market_daily (source, code, date, open, high, low, close, volume, amount, fetched_at)"
            " VALUES ('ths_ifind',?,?,?,?,?,?,?,?,?)",
            [(code, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount, now)
             for r in df.itertuples()])
    return len(df)


def ths_selftest() -> str:
    """凭证/连通性自检：SDK 登录 + 拉茅台近 10 天日线；SDK 不可用自动改测 HTTP 通道。
    供命令行快速验证：
    docker exec lh-qsys python -c "import datasource; print(datasource.ths_selftest())"
    """
    try:
        _ths_login()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        n = _ths_fetch_daily("SH600519", start, end)
        return f"OK：SDK 登录成功，SH600519 近10天日线写入 {n} 行" if n else "SDK 登录成功但未取到数据（检查权限）"
    except Exception as e:
        try:
            df, res, err = _ths_http("real_time_quotation",
                                     {"codes": "600519.SH", "indicators": "latest"})
            if err == 0 and df is not None and not df.empty:
                return f"OK：SDK 不可用（{e}）；HTTP 通道正常，茅台最新价 {df.iloc[-1].get('latest')}"
            return f"FAIL：SDK（{e}）；HTTP 返回 errorcode={err}"
        except Exception as e2:
            return f"FAIL：SDK（{e}）；HTTP（{e2}）"


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


# ---------------------------------------------------------------- iFinD HTTP API 主通道（token 鉴权）
# refresh_token → access_token（7天有效，进程内缓存6天），不占 SDK 会话数、无登录频次限制。SDK 仅作兜底：_sdk_or_http 分发 HTTP 优先；
# 仅日内快照等 HTTP 无等价端点的调用走 _sdk_first。端点/报文格式见官方 HTTPAPI 文档（quantapi 下载中心）。
# 不占 SDK 会话数、无登录频次限制。端点/报文格式见官方 HTTPAPI 文档（quantapi 下载中心）。
_THS_HTTP = {"access_token": "", "until": 0.0}
_THS_API = "https://quantapi.51ifind.com/api/v1"


def _ths_access_token() -> str:
    # 1. 先检查内存缓存
    if _THS_HTTP["access_token"] and time.time() < _THS_HTTP["until"]:
        return _THS_HTTP["access_token"]
    
    # 2. 从数据库读取 access_token
    db_token = _get_config_value("access_token")
    db_expires = _get_config_value("token_expires_at")
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
    _set_config_value("access_token", at)
    _set_config_value("token_expires_at", expires.strftime("%Y-%m-%d %H:%M:%S"))
    _THS_HTTP.update(access_token=at, until=expires.timestamp())
    return at


def _ths_http(endpoint: str, payload: dict):
    """iFinD HTTP API 调用 → (df, res, errcode)；tables JSON 复用 _tables_to_df 解析。"""
    import requests
    at = _ths_access_token()
    res = requests.post(f"{_THS_API}/{endpoint}", json=payload, timeout=30,
                        headers={"Content-Type": "application/json", "access_token": at}).json()
    return _tables_to_df(res.get("tables")), res, res.get("errorcode", -1)


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


# ---------------------------------------------------------------- iFinD 通用调用（📡 iFinD数据 页面用）
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
            lambda: ths_call("THS_SS", codes_s, ind, "dataType:Original",
                             f"{begin:%Y-%m-%d %H:%M:%S}", f"{end:%Y-%m-%d %H:%M:%S}"), http)
    df, res, err = _sdk_first(
        lambda: ths_call("THS_SS", codes_s, ind, "dataType:Original",
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
            df, res, err = ths_call("THS_SS", codes_s, ind, "dataType:Original",
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


def ths_date_serial(code: str, indicators: str, start: str, end: str, params: str = ""):
    """日期序列（SDK: THS_DateSerial / HTTP: date_sequence）：基本面/专题指标的时序。"""
    cs = _to_ths_code(code)
    inds = [x.strip() for x in indicators.replace("；", ";").replace(",", ";").split(";") if x.strip()]
    return _sdk_or_http(
        lambda: ths_call("THS_DateSerial", cs, indicators, params, "", start, end),
        lambda: _ths_http("date_sequence",
                          {"codes": cs, "startdate": start, "enddate": end,
                           "functionpara": {"Days": "Tradedays", "Fill": "Previous", "Interval": "D"},
                           "indipara": [{"indicator": i, "indiparams": [params]} for i in inds]}))


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


# ---------------------------------------------------------------- easy-tdx（通达信 TCP）通道
_TDX = {"client": None}
# 实测数据质量+速度双优的服务器（2026-08 验证；from_best_host 会选到返回空数据的坏节点，故钉死）
_TDX_HOSTS = ["180.153.18.170", "115.238.56.198", "115.238.90.165", "218.75.126.9",
              "175.178.128.227", "124.223.163.242"]


def _tdx_client():
    """TDX 连接单例：按优选列表逐个建连并做数据质量校验（防空响应服务器）。"""
    if _TDX["client"] is not None:
        return _TDX["client"]
    from easy_tdx import KlineCategory, Market, TdxClient

    for host in _TDX_HOSTS:
        try:
            c = TdxClient(host=host, port=7709, timeout=6)
            df = c.get_security_bars(Market.SH, "600519", KlineCategory.DAY, 0, 2)
            if df is not None and not df.empty and float(df["close"].iloc[-1]) > 100:
                _TDX["client"] = c
                return c
        except Exception:
            continue
    # 全部不可用时退回自动选路
    _TDX["client"] = TdxClient.from_best_host()
    return _TDX["client"]


def _tdx_market(code: str):
    from easy_tdx import Market

    prefix = code[:2].upper()
    return {"SH": Market.SH, "SZ": Market.SZ, "BJ": Market.BJ}.get(prefix, Market.SH), code[2:]


def _tdx_fetch_daily(code: str, start: str, end: str) -> int:
    """easy-tdx 日线分页抓取 → market.db（source='easytdx'，单位：股/元）。"""
    from easy_tdx import KlineCategory

    market, sym = _tdx_market(code)
    frames = []
    start_idx = 0
    while True:
        df = _tdx_client().get_security_bars(market, sym, KlineCategory.DAY, start_idx, 800)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < 800:
            break
        start_idx += 800
        if len(frames) > 40:  # 安全上限
            break
    if not frames:
        return 0
    big = pd.concat(frames).drop_duplicates(subset=["date"]).sort_values("date")
    big["date"] = pd.to_datetime(big["date"]).dt.strftime("%Y-%m-%d")
    big = big[(big["date"] >= start) & (big["date"] <= end)]
    if big.empty:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO market_daily (source, code, date, open, high, low, close, volume, amount, fetched_at)"
            " VALUES ('easytdx',?,?,?,?,?,?,?,?,?)",
            [(code, r.date, r.open, r.high, r.low, r.close, r.vol, r.amount, now)
             for r in big.itertuples()])
    return len(big)


def _cached_daily(code: str, start: str, end: str, source: str) -> pd.DataFrame:
    """通用读穿缓存（akshare/easytdx/ths_ifind 共用）。"""
    fetcher = {"akshare": _ak_fetch_daily, "easytdx": _tdx_fetch_daily,
               "ths_ifind": _ths_fetch_daily}[source]
    with _conn() as c:
        have = c.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM market_daily"
                         " WHERE source=? AND code=?", (source, code)).fetchone()
    if not (have and have[2] > 0 and have[0] <= start and have[1] >= end):
        try:
            fetcher(code, start, end)
            time.sleep(0.1)
            _touch_source(source)
        except Exception as e:
            import logging
            logging.getLogger("datasource").warning(f"{source} 抓取失败 {code}: {e}")
    with _conn() as c:
        return pd.read_sql(
            "SELECT date, open, high, low, close, volume, amount FROM market_daily"
            " WHERE source=? AND code=? AND date BETWEEN ? AND ? ORDER BY date",
            c, params=(source, code, start, end))


# ---------------------------------------------------------------- 统一接口
def get_daily(code: str, start: str, end: str, source: str | None = None) -> pd.DataFrame:
    """单票日线 → ($open..$amount, datetime 索引)，与 qlib 版式一致。"""
    source = source or get_source()
    if source == "qlib_local":
        df = _qlib_daily([code], ["$open", "$high", "$low", "$close", "$volume", "$amount"], start=start, end=end)
        if df.empty:
            return df
        return df.droplevel("instrument").sort_index()
    df = _cached_daily(code, start, end, source)
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df.index.name = "datetime"
    return df.rename(columns={c: f"${c}" for c in ["open", "high", "low", "close", "volume", "amount"]})


def get_panel(codes: list[str], start: str, end: str, fields: list[str],
              source: str | None = None, progress=None) -> pd.DataFrame:
    """多票面板 → (instrument, datetime) MultiIndex，列 $open...（与 qlib 版式一致）。"""
    source = source or get_source()
    if source == "qlib_local":
        df = _qlib_daily(codes, fields, start=start, end=end)
        return df.sort_index() if not df.empty else df

    frames = []
    for i, code in enumerate(codes):
        if progress:
            progress(f"{source} 抓取 {i + 1}/{len(codes)} {code}")
        d = _cached_daily(code, start, end, source)
        if d.empty:
            continue
        d["instrument"] = code
        d["date"] = pd.to_datetime(d["date"])
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames)
    big = big.rename(columns={c: f"${c}" for c in ["open", "high", "low", "close", "volume", "amount"]})
    keep = [f for f in fields if f in big.columns]
    panel = big.set_index(["instrument", "date"])[keep].sort_index()
    # 与 qlib 通道保持同名索引层（下游统一按 datetime 取层）
    panel.index = panel.index.set_names(["instrument", "datetime"])
    return panel


# ---------------------------------------------------------------- 腾讯展示通道（分时/竞价）
def _to_tx_symbol(code: str) -> str:
    """SH600519 → sh600519（腾讯行情通道）。"""
    return _to_ak_symbol(code)


def get_minute_today(code: str) -> dict:
    """腾讯当日分钟线 + 快照。返回 {"date", "prev_close", "minutes": DataFrame, "name"}。

    minutes 列: time(str 'HHMM'), price, volume(手), cum_amount(元)。
    腾讯语义：volume / cum_amount 均为**当日累计**值（每分钟递增）。
    首行 09:30 = 集合竞价成交（price=竞价, volume=竞价量, cum_amount=竞价金额）。
    """
    import requests

    sym = _to_tx_symbol(code)
    r = requests.get(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}",
                     timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    d = r.json()["data"][sym]
    rows = d["data"]["data"]
    date = d["data"]["date"]
    qt = d.get("qt", {}).get(sym, [])
    name = qt[1] if len(qt) > 1 else code
    prev_close = float(qt[4]) if len(qt) > 4 and qt[4] else None
    m = pd.DataFrame([str(r).split()[:4] for r in rows], columns=["time", "price", "volume", "cum_amount"])
    for c in ["price", "volume", "cum_amount"]:
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m["time"] = m["time"].astype(str).str.zfill(4)
    m = m.dropna()
    # 竞价/盘前时段腾讯会返回 0930 占位行（volume=0），剔除避免误判为竞价成交
    m = m[(m["volume"] > 0) | (m["time"] > "0930")]
    if m.empty:
        return {"date": date, "name": name, "prev_close": prev_close, "minutes": m}
    # 累计量 → 每分量（首行即竞价量，保持原值）
    m["minute_vol"] = m["volume"].diff().fillna(m["volume"])
    return {"date": date, "name": name, "prev_close": prev_close, "minutes": m}


def get_latest_outstanding(code: str) -> float | None:
    """最近一次 akshare 抓取缓存的流通股本（股）。"""
    with _conn() as c:
        r = c.execute("SELECT outstanding_share FROM market_daily WHERE source='akshare'"
                      " AND code=? AND outstanding_share IS NOT NULL ORDER BY date DESC LIMIT 1",
                      (code,)).fetchone()
    return float(r[0]) if r and r[0] else None


# ---------------------------------------------------------------- easy-tdx 分钟线/逐笔（竞价）
def get_minute_tdx(code: str, count: int = 240) -> pd.DataFrame:
    """当日 1 分钟线（通达信），返回 datetime/price/vol(股)/amount 帧。"""
    from easy_tdx import KlineCategory

    market, sym = _tdx_market(code)
    df = _tdx_client().get_security_bars(market, sym, KlineCategory.MIN_1, 0, count)
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("datetime").reset_index(drop=True)


def get_ticks_tdx(code: str, max_pages: int = 20) -> pd.DataFrame:
    """当日逐笔成交（含集合竞价标记 buyorsell=8）。分页回溯到 09:25 竞价为止。"""
    market, sym = _tdx_market(code)
    frames = []
    for page in range(max_pages):
        df = _tdx_client().get_transaction_data(market, sym, page * 800, 800)
        if df is None or df.empty:
            break
        frames.append(df)
        earliest = df["datetime"].astype(str).min()
        if earliest <= "09:30" or len(df) < 800:
            break
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames).drop_duplicates().sort_values("datetime").reset_index(drop=True)
    return big


# ---------------------------------------------------------------- 行情快照（腾讯批量）与本地持久化
_QUOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_snapshots (
    ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    price REAL, prev_close REAL, open REAL, high REAL, low REAL,
    bid1 REAL, ask1 REAL, volume REAL, amount REAL,
    bid_vol_sum REAL, ask_vol_sum REAL, last_tick_vol REAL,
    turnover REAL, limit_up REAL, limit_down REAL, avg_price REAL,
    outer_vol REAL, inner_vol REAL, quantity_ratio REAL,
    trade_time TEXT, source TEXT DEFAULT 'tencent',
    PRIMARY KEY (ts, code));
CREATE INDEX IF NOT EXISTS idx_quote_code_ts ON quote_snapshots(code, ts);
"""


def _qconn():
    c = _conn()
    c.executescript(_QUOTE_SCHEMA)
    return c


def _parse_tx_line(line: str) -> dict | None:
    """解析腾讯快照单行 v_sh600519="1~贵州茅台~..."。字段索引按公开映射，越界防御。"""
    m = re.match(r'v_([a-z]{2}\d{6})="(.*)"', line.strip().rstrip(";"))
    if not m:
        return None
    sym, body = m.group(1), m.group(2)
    p = body.split("~")

    def _f(i):
        try:
            return float(p[i]) if p[i] not in ("", None) else None
        except (IndexError, ValueError):
            return None

    def _vols(start):  # 五档量求和：价/量成对，量在后一位
        total = 0.0
        for i in range(start, start + 10, 2):
            v = _f(i)
            if v:
                total += v
        return total

    code = sym.upper()
    return {
        "code": code,
        "name": p[1] if len(p) > 1 else code,
        "price": _f(3), "prev_close": _f(4), "open": _f(5),
        "volume": _f(6),                          # 总手
        "outer_vol": _f(7), "inner_vol": _f(8),
        "bid1": _f(9), "ask1": _f(19),
        "bid_vol_sum": _vols(10), "ask_vol_sum": _vols(20),
        "last_tick_vol": _f(29),                  # 现手（盘中有效，盘后常为空）
        "trade_time": p[30] if len(p) > 30 else "",
        "high": _f(33), "low": _f(34),
        "amount": (_f(37) or 0) * 1e4,            # 成交额（万→元）
        "turnover": _f(38),
        "limit_up": _f(47), "limit_down": _f(48),
        "quantity_ratio": _f(49),                 # 量比（部分档位提供）
        "avg_price": _f(51),
    }


def get_batch_snapshots(codes: list[str], chunk: int = 50) -> list[dict]:
    """腾讯批量快照：qt.gtimg.cn/q=code1,code2,...（每批约50只）。"""
    import requests

    out = []
    for i in range(0, len(codes), chunk):
        syms = [_to_tx_symbol(c) for c in codes[i:i + chunk]]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            for line in r.text.strip().split(";"):
                row = _parse_tx_line(line)
                if row:
                    out.append(row)
        except Exception:
            continue
        time.sleep(0.2)
    return out


def _parse_tx_float_mv(line: str) -> dict | None:
    """从腾讯快照行提取流通市值(field44)和总市值(field45)，单位：亿元。"""
    m = re.match(r'v_([a-z]{2}\d{6})="(.*)"', line.strip().rstrip(";"))
    if not m:
        return None
    sym = m.group(1)
    p = m.group(2).split("~")
    def _f(i):
        try:
            return float(p[i]) if p[i] not in ("", None) else None
        except (IndexError, ValueError):
            return None
    code = sym.upper()
    float_mv = _f(44)    # 流通市值（亿元）
    total_mv = _f(45)    # 总市值（亿元）
    total_shares = _f(46)  # 总股本（亿股）
    price = _f(3)
    return {
        "code": code,
        "float_mv_yi": float_mv,
        "total_mv_yi": total_mv,
        "total_shares_yi": total_shares,
        "price": price,
    }


def fetch_tencent_float_mv(codes: list[str], chunk: int = 50) -> dict[str, dict]:
    """从腾讯 API 批量获取流通市值/总市值（亿元），返回 {code: {float_mv, total_mv, float_shares}}。"""
    import requests
    result = {}
    for i in range(0, len(codes), chunk):
        syms = [_to_tx_symbol(c) for c in codes[i:i + chunk]]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            for line in r.text.strip().split(";"):
                row = _parse_tx_float_mv(line)
                if row and row.get("code"):
                    code = row["code"]
                    float_mv_yi = row.get("float_mv_yi")
                    total_mv_yi = row.get("total_mv_yi")
                    price = row.get("price")
                    float_mv = round(float_mv_yi * 1e8, 2) if float_mv_yi else None
                    float_shares = round(float_mv / price, 2) if float_mv and price else None
                    result[code] = {
                        "float_mv": float_mv,
                        "float_shares": float_shares,
                    }
        except Exception:
            continue
        time.sleep(0.2)
    return result


def save_snapshots(rows: list[dict], ts: str | None = None) -> int:
    """快照批次落库（quote_snapshots，保留3天）。"""
    if not rows:
        return 0
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _qconn() as c:
        vals = [(ts, r["code"], r["name"], r["price"], r["prev_close"], r["open"], r["high"], r["low"],
                 r["bid1"], r["ask1"], r["volume"], r["amount"], r["bid_vol_sum"], r["ask_vol_sum"],
                 r["last_tick_vol"], r["turnover"], r["limit_up"], r["limit_down"], r["avg_price"],
                 r["outer_vol"], r["inner_vol"], r["quantity_ratio"], r["trade_time"]) for r in rows]
        assert all(len(v) == 23 for v in vals), "字段数不匹配"
        c.executemany(
            "INSERT OR REPLACE INTO quote_snapshots (ts, code, name, price, prev_close, open, high, low,"
            " bid1, ask1, volume, amount, bid_vol_sum, ask_vol_sum, last_tick_vol, turnover,"
            " limit_up, limit_down, avg_price, outer_vol, inner_vol, quantity_ratio, trade_time, source)"
            " VALUES (" + ",".join(["?"] * 23) + ",'tencent')", vals)
        # 保留最近3天（每批一次廉价清理）
        cutoff = (datetime.now() - pd.Timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("DELETE FROM quote_snapshots WHERE ts < ?", (cutoff,))
    return len(rows)


def get_speed_1min(codes: list[str], ref_ts: str) -> dict:
    """1分钟涨速基准价：每只股票在 ref_ts 45秒**之前**的最新一条快照价。

    （原实现取 45~90 秒窗口，按 10 秒采集节奏永远匹配不到 → 恒为空；
    改为"45 秒前最近一条"，任意采集节奏都能正确取到约 1 分钟前的基准。）
    """
    out = {}
    if not codes:
        return out
    cutoff = (pd.Timestamp(ref_ts) - pd.Timedelta(seconds=45)).strftime("%Y-%m-%d %H:%M:%S")
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, MAX(ts) FROM quote_snapshots WHERE code IN ({marks}) AND ts <= ?"
            f" GROUP BY code", (*codes, cutoff)).fetchall()
        for code, mts in rows:
            v = c.execute("SELECT price FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if v and v[0]:
                out[code] = v[0]
    return out


def get_prev_snapshot_volumes(codes: list[str], ref_ts: str) -> dict:
    """最近一条早于 ref_ts 的快照总手（用于估算现手=两次总手差）。"""
    out = {}
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, MAX(ts) FROM quote_snapshots WHERE code IN ({marks}) AND ts < ?"
            f" GROUP BY code", (*codes, ref_ts)).fetchall()
        for code, mts in rows:
            v = c.execute("SELECT volume FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if v and v[0] is not None:
                out[code] = v[0]
    return out


def get_latest_snapshots(codes: list[str]) -> tuple[list[dict], str | None]:
    """每只股票最新一条快照（读库，供页面无网络渲染）。返回 (rows, 最新采集时间)。"""
    if not codes:
        return [], None
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        latest = c.execute(
            f"SELECT code, MAX(ts) AS mts FROM quote_snapshots WHERE code IN ({marks})"
            f" GROUP BY code", codes).fetchall()
        if not latest:
            return [], None
        ts_map = {code: mts for code, mts in latest}
        rows = []
        cols = ["ts", "code", "name", "price", "prev_close", "open", "high", "low",
                "bid1", "ask1", "volume", "amount", "bid_vol_sum", "ask_vol_sum",
                "last_tick_vol", "turnover", "limit_up", "limit_down", "avg_price",
                "outer_vol", "inner_vol", "quantity_ratio", "trade_time"]
        for code, mts in ts_map.items():
            r = c.execute(f"SELECT {','.join(cols)} FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if r:
                rows.append(dict(zip(cols, r)))
    max_ts = max(ts_map.values()) if ts_map else None
    return rows, max_ts


def get_realtime_snapshot(code: str) -> dict:
    """腾讯实时快照（qt.gtimg.cn，约 3 秒级更新）。

    竞价时段（09:15-09:25）：price/volume 字段为**虚拟匹配价/匹配量**。
    返回: {name, price, prev_close, open, volume(手), amount(万), high, low,
           turnover, limit_up, limit_down, avg_price, time}
    """
    import requests

    sym = _to_tx_symbol(code)
    r = requests.get(f"https://qt.gtimg.cn/q={sym}", timeout=8,
                     headers={"User-Agent": "Mozilla/5.0"})
    parts = r.text.split("~")

    def _f(i):
        try:
            return float(parts[i]) if parts[i] not in ("", None) else None
        except (IndexError, ValueError):
            return None

    return {
        "name": parts[1] if len(parts) > 1 else code,
        "price": _f(3), "prev_close": _f(4), "open": _f(5),
        "volume": _f(6),                      # 手（竞价时段=匹配量）
        "amount_wan": _f(37),                 # 成交额（万）
        "time": parts[30] if len(parts) > 30 else "",
        "high": _f(33), "low": _f(34),
        "turnover": _f(38),
        "limit_up": _f(47), "limit_down": _f(48),
        "avg_price": _f(51),
    }


# ---------------------------------------------------------------- 全市场股票/指数列表
def get_all_a_stocks() -> pd.DataFrame:
    """获取全市场A股列表（通过 akshare）。

    返回: DataFrame，包含 code, name, market 等字段
    """
    import akshare as ak

    try:
        # 使用 akshare 获取A股实时行情，包含所有A股代码和名称
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return pd.DataFrame()

        # 整理列名
        result = pd.DataFrame({
            "code": df["代码"],
            "name": df["名称"],
            "market": df["代码"].apply(lambda x: "SH" if x.startswith("6") else "SZ"),
        })
        return result.sort_values("code").reset_index(drop=True)
    except Exception as e:
        import logging
        logging.getLogger("datasource").warning(f"获取A股列表失败: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------- 指数列表（iFinD → 落库）
# 宽基种子：问财"沪深指数"只覆盖交易所发布的指数，不含中证公司发布的规模指数
# （沪深300/中证500 等实测问财查不到），手工兜底主要宽基。
# 种子代码均经 iFinD real_time_quotation 实测可取数（2026-09）。
_SEED_INDICES = [
    ("000001.SH", "上证指数"), ("000016.SH", "上证50"), ("000010.SH", "上证180"),
    ("000688.SH", "科创50"), ("000300.SH", "沪深300"), ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"), ("932000.CSI", "中证2000"), ("000985.CSI", "中证全指"),
    ("000903.SH", "中证100"), ("000922.CSI", "中证红利"),
    ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"), ("399005.SZ", "中小100"),
    ("399106.SZ", "深证综指"), ("399330.SZ", "深证100"), ("899050.BJ", "北证50"),
]

# 问财指数语义查询 → 页面分类标签（SDK: THS_WCQuery / HTTP: smart_stock_picking）
_INDEX_WC_QUERIES = [("沪深指数", "沪深指数"), ("行业指数", "行业指数"), ("主题指数", "主题指数")]


def _wc_index_query(query: str) -> pd.DataFrame:
    """问财指数查询（SDK 优先，登录限流自动落 HTTP）。返回 code/name 两列。"""
    df, _res, err = _sdk_or_http(
        lambda: ths_call("THS_WCQuery", query, "index"),
        lambda: _ths_http("smart_stock_picking",
                          {"searchstring": query, "searchtype": "index"}))
    if err not in (0, None) or df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    code_col = next((c for c in df.columns if "代码" in c or "thscode" in c.lower()), None)
    name_col = next((c for c in df.columns if "简称" in c or "名称" in c), None)
    if not code_col:
        return pd.DataFrame()
    out = pd.DataFrame({"code": df[code_col].astype(str)})
    out["name"] = df[name_col].astype(str) if name_col else ""
    return out


def fetch_index_list() -> pd.DataFrame:
    """拉取指数列表与实时行情（全 iFinD 数据源，token 鉴权无需登录，不写库）。

    步骤：1) 问财三组语义查询取指数代码+名称+分类（沪深/行业/主题）
          2) 叠加手工宽基种子（中证公司发布的规模指数问财覆盖不到）
          3) iFinD 实时行情补价格字段（HTTP 优先，每批50只）
    返回 DataFrame：code/name/market/category/price/.../fetched_at，
    按 宽基→沪深→行业→主题、同类按代码 排序。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1)+2) 指数全集：问财分类 + 宽基种子（种子覆盖同名问财条目的分类与名称）
    # 后缀说明：SH/SZ/BJ 交易所发布、CSI 中证公司、TI 同花顺自研（行业/主题指数）
    idx_map: dict[str, dict] = {}  # code -> {name, category}
    for query, cat in _INDEX_WC_QUERIES:
        try:
            for r in _wc_index_query(query).itertuples():
                if re.match(r"^\w+\.(SH|SZ|BJ|CSI|TI)$", r.code):
                    idx_map.setdefault(r.code, {"name": r.name, "category": cat})
        except Exception:
            continue
    for code, name in _SEED_INDICES:
        idx_map[code] = {"name": name, "category": "宽基指数"}
    if not idx_map:
        return pd.DataFrame()

    # 3) iFinD 实时行情（HTTP 优先；SDK 兜底）
    codes = sorted(idx_map)
    rq_indicators = "latest,preClose,open,high,low,changeRatio,volume,amount,amplitude"
    rq_data: dict[str, dict] = {}
    for i in range(0, len(codes), 50):
        batch = ",".join(codes[i:i + 50])
        try:
            df_rq, _res, err = _ths_http("real_time_quotation", {
                "codes": batch, "indicators": rq_indicators})
            if df_rq is None or df_rq.empty:
                df_rq, _res, err = ths_call("THS_RQ", batch, rq_indicators, "")
            if df_rq is not None and not df_rq.empty:
                code_col = next((c for c in df_rq.columns
                                 if "code" in c.lower() or "代码" in c), df_rq.columns[0])
                for _, row in df_rq.iterrows():
                    rq_data[str(row[code_col])] = {
                        "price": _safe_float(row.get("latest")),
                        "prev_close": _safe_float(row.get("preClose")),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "change_pct": _safe_float(row.get("changeRatio")),
                        "volume": _safe_float(row.get("volume")),
                        "amount": _safe_float(row.get("amount")),
                        "amplitude": _safe_float(row.get("amplitude")),
                    }
        except Exception:
            continue

    rows = []
    for code, meta in idx_map.items():
        rq = rq_data.get(code, {})
        rows.append({
            "code": code, "name": meta["name"], "market": code.split(".")[-1],
            "category": meta["category"],
            "price": rq.get("price"), "prev_close": rq.get("prev_close"),
            "open": rq.get("open"), "high": rq.get("high"), "low": rq.get("low"),
            "change_pct": rq.get("change_pct"), "volume": rq.get("volume"),
            "amount": rq.get("amount"), "amplitude": rq.get("amplitude"),
            "fetched_at": now})
    df = pd.DataFrame(rows)
    cat_order = {"宽基指数": 0, "沪深指数": 1, "行业指数": 2, "主题指数": 3}
    return df.sort_values(
        by=["category", "code"],
        key=lambda s: s.map(cat_order) if s.name == "category" else s
    ).reset_index(drop=True)


def fetch_indexlist_to_db() -> int:
    """指数列表落库（⏰定时任务 ifind_indexlist_sync 用；页面本身直调 fetch_index_list）。"""
    df = fetch_index_list()
    if df.empty:
        return 0
    cols = ["code", "name", "market", "category", "price", "prev_close", "open",
            "high", "low", "change_pct", "volume", "amount", "amplitude", "fetched_at"]
    with _qconn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO ifind_indexlist"
            "(code,name,market,category,price,prev_close,open,high,low,change_pct,"
            "volume,amount,amplitude,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            list(df[cols].itertuples(index=False, name=None)))
    return len(df)


def get_indexlist_from_db() -> pd.DataFrame:
    """从 ifind_indexlist 表读取指数列表（宽基 → 沪深 → 行业 → 主题，同类按代码）。"""
    with _qconn() as c:
        return pd.read_sql_query(
            "SELECT * FROM ifind_indexlist ORDER BY"
            " CASE category WHEN '宽基指数' THEN 0 WHEN '沪深指数' THEN 1"
            " WHEN '行业指数' THEN 2 WHEN '主题指数' THEN 3 ELSE 4 END, code", c)


# ---------------------------------------------------------------- 北交所股票列表（腾讯 API 补充）
def fetch_bj_stocklist_from_tencent() -> list[dict]:
    """从腾讯 API 扫描北交所股票代码，返回 [{code, name, price, ...}]。

    北交所代码范围：43xxxx, 83xxxx, 87xxxx。
    """
    import urllib.request
    results = []
    # 扫描范围：430001-432000, 830001-832000, 870001-872000
    ranges = list(range(430001, 432000)) + list(range(830001, 832000)) + list(range(870001, 872000))
    batch_size = 80
    for i in range(0, len(ranges), batch_size):
        batch = ranges[i:i + batch_size]
        codes = [f"bj{code}" for code in batch]
        url = "http://qt.gtimg.cn/q=" + ",".join(codes)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode("gbk")
            for line in data.strip().split(";"):
                line = line.strip()
                if not line or "pv_none" in line or '=""' in line:
                    continue
                parts = line.split("~")
                if len(parts) > 4 and parts[1] and parts[2] and parts[3]:
                    try:
                        price = float(parts[3])
                        if price > 0:
                            code6 = parts[2]  # e.g. "430047"
                            results.append({
                                "code": f"BJ{code6}",
                                "name": parts[1],
                                "price": price,
                                "prev_close": _safe_float(parts[4]),
                                "open": _safe_float(parts[5]),
                                "high": _safe_float(parts[33]),
                                "low": _safe_float(parts[34]),
                                "change_pct": _safe_float(parts[32]),
                                "volume": _safe_float(parts[6]),
                                "amount": (_safe_float(parts[37]) or 0) * 1e4,
                                "turnover": _safe_float(parts[38]),
                                "pe_ttm": _safe_float(parts[39]),
                                "total_mv": (_safe_float(parts[45]) or 0) * 1e8,
                                "float_mv": (_safe_float(parts[44]) or 0) * 1e8,
                            })
                    except (IndexError, ValueError):
                        pass
        except Exception:
            continue
    return results


# ---------------------------------------------------------------- 全市场A股列表（纯 iFinD 数据源 → 落库）
def fetch_stocklist_to_db() -> int:
    """拉取全市场A股数据并写入 ifind_stocklist 表（全部使用 iFinD 数据源）。

    步骤：
    1. iFinD 获取全量代码+名称列表（stock_list → SDK WCQuery → HTTP 问财；北交所单独问财"北交所股票"）
    2. iFinD 获取基本面（动态PE/PB/总市值，每批50只；流通股本仅 SDK 通道支持）
    3. iFinD HTTP API 获取实时行情（现价/涨跌/成交/换手/量比等，每批50只）
    4. 流通股本/市值：只用 iFinD（SDK 通道），限流期间为空，页面 fallback 总股本（也是 iFinD 数据）
    5. 合并写入 ifind_stocklist 表（全同花顺 iFinD 数据，不用腾讯/akshare）
    返回写入行数。
    """
    import re as _re
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")

    def _norm(c):
        m = _re.match(r"(\d{6})\.([A-Z]{2})", c.strip())
        return f"{m.group(2)}{m.group(1)}" if m else c.strip()

    # 1) 获取全量代码列表：三条路依次尝试
    #    a. HTTP stock_list 端点（部分账号无此端点，404）
    #    b. SDK THS_WCQuery（需登录，-9 限流时不可用）
    #    c. HTTP 问财 smart_stock_picking（不占会话数，token 鉴权，实测可用）
    all_codes_raw = []
    all_names = {}
    all_codes = []

    def _parse_list_df(df_wc):
        nonlocal all_codes_raw, all_names, all_codes
        code_col = next((c for c in df_wc.columns if "代码" in c or "code" in c.lower()), df_wc.columns[0])
        name_col = next((c for c in df_wc.columns if "简称" in c or "名称" in c or "name" in c.lower()), None)
        all_codes_raw = df_wc[code_col].astype(str).tolist()
        if name_col:
            all_names = dict(zip(df_wc[code_col].astype(str), df_wc[name_col].astype(str)))
        all_codes = [_norm(c) for c in all_codes_raw]

    def _ok(df_wc, err):
        return err in (0, None) and isinstance(df_wc, pd.DataFrame) and not df_wc.empty

    try:
        df_wc, _res, err = _ths_http("stock_list", {"indicator": "all", "params": ""})
        if _ok(df_wc, err):
            _parse_list_df(df_wc)
    except Exception:
        pass
    if not all_codes:
        try:
            df_wc, _res, err = ths_call("THS_WCQuery", "全部A股", "stock")
            if _ok(df_wc, err):
                _parse_list_df(df_wc)
        except Exception:
            pass
    if not all_codes:
        try:
            df_wc, _res, err = _ths_http("smart_stock_picking",
                                         {"searchstring": "全部A股", "searchtype": "stock"})
            if _ok(df_wc, err):
                _parse_list_df(df_wc)
        except Exception:
            pass

    # 1+) 北交所股票：问财补充（同花顺 iFinD 源；"全部A股"问财只含沪深（实测 5214 只，不含 BJ）
    #     北交所 2025 年切换 920xxx 新代码段：iFinD 对新格式全支持（920002.BJ 实测 RQ/BD 可用）
    #     旧 43x/83x/87x 旧代码 iFinD 已不支持（-4001）——统一用新代码段落库
    try:
        df_bj, _res, err = _sdk_or_http(
            lambda: ths_call("THS_WCQuery", "北交所股票", "stock"),
            lambda: _ths_http("smart_stock_picking",
                              {"searchstring": "北交所股票", "searchtype": "stock"}))
        if _ok(df_bj, err):
            bj_code_col = next((c for c in df_bj.columns if "代码" in c or "code" in c.lower()), df_bj.columns[0])
            bj_name_col = next((c for c in df_bj.columns if "简称" in c or "名称" in c or "name" in c.lower()), None)
            bj_raw = df_bj[bj_code_col].astype(str).tolist()
            all_codes_raw += bj_raw
            if bj_name_col:
                all_names.update(dict(zip(df_bj[bj_code_col].astype(str), df_bj[bj_name_col].astype(str))))
            all_codes += [_norm(c) for c in bj_raw]
    except Exception:
        pass

    # 1++) 问财取 市盈率(pe)/流通a股/a股市值（不含限售股）——同花顺终端口径（实测 301688 与终端 39.18 一致）
    #      新股 iFinD BD 接口不计算 PE/流通股本（实测全为 None），问财覆盖新股+全市场，一个调用搞定
    wc_ind: dict[str, dict] = {}
    for wc_query in ["全部A股，市盈率，流通股本，流通市值", "北交所股票，市盈率，流通股本，流通市值"]:
        try:
            df_wc2, _res, err = _sdk_or_http(
                lambda q=wc_query: ths_call("THS_WCQuery", q, "stock"),
                lambda q=wc_query: _ths_http("smart_stock_picking",
                                             {"searchstring": q, "searchtype": "stock"}))
            if err in (0, None) and isinstance(df_wc2, pd.DataFrame) and not df_wc2.empty:
                code_col = next((c for c in df_wc2.columns if "代码" in c or "code" in c.lower()), None)
                pe_col = next((c for c in df_wc2.columns if str(c).startswith("市盈率")), None)
                fs_col = next((c for c in df_wc2.columns if str(c).startswith(("流通a股", "流通A股"))), None)
                fm_col = next((c for c in df_wc2.columns if "不含限售股" in str(c) or str(c).startswith("a股市值")), None)
                if code_col:
                    for _, row in df_wc2.iterrows():
                        code2 = _norm(str(row[code_col]))
                        wc_ind[code2] = {
                            "pe": _safe_float(row.get(pe_col)) if pe_col else None,
                            "float_shares": _safe_float(row.get(fs_col)) if fs_col else None,
                            "float_mv": _safe_float(row.get(fm_col)) if fm_col else None,
                        }
        except Exception:
            continue

    # 2) iFinD THS_BD 基本面指标（逐指标调用，每批50只）
    #    ths_pe_stock 第二参数选口径：2=静态 3=动态（PE 主源是问财，这里动态口径仅作兜底）
    today_dash = datetime.now().strftime("%Y-%m-%d")
    today_compact = today_dash.replace("-", "")
    bd_indicator_list = ["ths_pe_stock", "ths_pb_stock", "ths_market_value_stock"]
    bd_data: dict[str, dict] = {}
    batch_size = 50

    for i in range(0, len(all_codes), batch_size):
        batch_codes = all_codes_raw[i:i + batch_size]
        codes_s = ",".join(batch_codes)
        batch_result: dict[str, dict] = {}
        for ind in bd_indicator_list:
            if ind == "ths_pe_stock":
                sdk_params, http_params = f"{today_dash},3", [today_compact, "3"]
            else:
                sdk_params, http_params = "", []
            try:
                df_bd, _res, err = _sdk_or_http(
                    lambda c=codes_s, ii=ind, p=sdk_params: ths_call("THS_BD", c, ii, p),
                    lambda ii=ind, p=http_params: _ths_http("basic_data_service", {
                        "codes": codes_s,
                        "indipara": [{"indicator": ii, "indiparams": p}]}))
                if df_bd is not None and not df_bd.empty:
                    bd_code_col = next((c for c in df_bd.columns
                                        if "code" in c.lower() or "代码" in c or "thscode" in c.lower()),
                                       df_bd.columns[0])
                    for _, row in df_bd.iterrows():
                        raw = str(row[bd_code_col])
                        normed = _norm(raw)
                        if normed not in batch_result:
                            batch_result[normed] = {}
                        batch_result[normed][ind] = _safe_float(row.get(ind))
            except Exception:
                pass
        for code, vals in batch_result.items():
            if code not in bd_data:
                bd_data[code] = {}
            bd_data[code].update(vals)

    # 3) iFinD THS_RQ 实时行情（分批，每批50只）
    #    SDK字段: latest,preClose,open,high,low,change,changeRatio,volume,amount,
    #             turnoverRatio,quantityRatio,amplitude,priceSpeed
    #    HTTP字段(补充): totalShares, totalCapital（floatCapital暂不可用）
    #    注意：HTTP 端量比字段名是 vol_ratio（quantityRatio 会被静默丢弃，priceSpeed/speed 也不支持）
    rq_sdk_indicators = "latest,preClose,open,high,low,change,changeRatio,volume,amount,turnoverRatio,quantityRatio,amplitude"
    rq_http_indicators = "latest,preClose,open,high,low,change,changeRatio,volume,amount,turnoverRatio,vol_ratio,amplitude,totalShares,totalCapital"
    rq_data: dict[str, dict] = {}

    for i in range(0, len(all_codes), batch_size):
        batch_codes = all_codes_raw[i:i + batch_size]
        codes_s = ",".join(batch_codes)
        try:
            df_rq, _res, err = _sdk_or_http(
                lambda c=codes_s: ths_call("THS_RQ", c, rq_sdk_indicators, ""),
                lambda: _ths_http("real_time_quotation", {
                    "codes": codes_s,
                    "indicators": rq_http_indicators.replace(";", ",")}))
            if df_rq is not None and not df_rq.empty:
                rq_code_col = next((c for c in df_rq.columns
                                    if "code" in c.lower() or "代码" in c or "thscode" in c.lower()),
                                   df_rq.columns[0])
                for _, row in df_rq.iterrows():
                    raw = str(row[rq_code_col])
                    normed = _norm(raw)
                    rq_data[normed] = {
                        "price": _safe_float(row.get("latest")),
                        "prev_close": _safe_float(row.get("preClose")),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "change_pct": _safe_float(row.get("changeRatio")),
                        "volume": _safe_float(row.get("volume")),
                        "amount": _safe_float(row.get("amount")),
                        "turnover": _safe_float(row.get("turnoverRatio")),
                        "quantity_ratio": _safe_float(row.get("vol_ratio")),
                        "amplitude": _safe_float(row.get("amplitude")),
                        "float_shares": None,  # HTTP API 暂不支持
                        "float_mv": _safe_float(row.get("floatCapital")),
                        "total_shares": _safe_float(row.get("totalShares")),
                        "total_mv": _safe_float(row.get("totalCapital")),
                    }
        except Exception:
            pass

    # 4) 合并写入
    #    流通股本/市值：只用 iFinD（ths_float_share_stock 仅 SDK 通道支持；HTTP 端实测 -4210 不支持，SDK 恢复后自动生效，SDK 限流期间流通列为空（页面 fallback 显示总股本，也是 iFinD 数据，不用腾讯兜底——保持全同花顺数据
    with _qconn() as c:
        vals = []
        for code, raw_code in zip(all_codes, all_codes_raw):
            market = code[:2] if code[:2] in ("SH", "SZ", "BJ") else "BJ"
            name = all_names.get(raw_code, "")
            rq = rq_data.get(code, {})
            bd = bd_data.get(code, {})
            price = rq.get("price")
            prev_close = rq.get("prev_close")
            high = rq.get("high")
            low = rq.get("low")
            change_pct = rq.get("change_pct")
            # 优先用THS_RQ返回的amplitude，否则计算
            amplitude = rq.get("amplitude")
            if amplitude is None and high and low and prev_close:
                amplitude = round((high - low) / prev_close * 100, 2)
            # 市盈率/流通股本/流通市值：问财（同花顺终端口径，实测 301688 与终端 39.18 一致）优先，iFinD BD 兜底
            wc = wc_ind.get(code, {})
            pe = wc.get("pe") if wc.get("pe") is not None else bd.get("ths_pe_stock")
            float_shares = wc.get("float_shares") or rq.get("float_shares")
            float_mv = wc.get("float_mv") or rq.get("float_mv")
            if float_mv is None and float_shares and price:
                float_mv = round(float_shares * price, 2)
            vals.append((
                code, name, market,
                price, prev_close, rq.get("open"), high, low,
                change_pct, rq.get("volume"), rq.get("amount"), rq.get("turnover"),
                rq.get("quantity_ratio"), amplitude,
                pe, bd.get("ths_pb_stock"),
                rq.get("total_mv") or bd.get("ths_market_value_stock"),
                float_mv,
                float_shares,
                rq.get("total_shares"),
                now,
            ))
        c.executemany(
            "INSERT OR REPLACE INTO ifind_stocklist"
            "(code,name,market,price,prev_close,open,high,low,change_pct,"
            "volume,amount,turnover,quantity_ratio,amplitude,pe_ttm,pb,total_mv,float_mv,"
            "float_shares,total_shares,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            vals)

        # 清理腾讯扫描时代的旧格式北交所代码（BJ43x/83x/87x 旧代码段，iFinD 已不支持）——北交所数据改为 iFinD 问财"北交所股票"（920xxx 新代码段）
        c.execute("DELETE FROM ifind_stocklist WHERE market='BJ' AND code NOT LIKE 'BJ9%'")

    return len(vals)


def _safe_float(v):
    """安全转 float，None/NaN/非数字返回 None。"""
    if v is None:
        return None
    try:
        import math
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def fetch_realtime_to_db() -> int:
    """拉取全市场A股实时行情并写入 ifind_realtime 表。

    用于盘中定时任务（每15分钟），写入当前时刻快照。
    返回写入行数。
    """
    import re as _re
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _norm(c):
        m = _re.match(r"(\d{6})\.([A-Z]{2})", c.strip())
        return f"{m.group(2)}{m.group(1)}" if m else c.strip()

    # 1) 获取全量代码列表（从 ifind_stocklist 表读取，避免重复调用API）
    with _qconn() as c:
        rows = c.execute("SELECT code FROM ifind_stocklist").fetchall()
    if not rows:
        return 0
    all_codes_raw = [r[0] for r in rows]

    # 2) 获取上一次快照的价格（用于计算涨速）
    #    涨速=本次快照价/上一快照价-1。上一快照超过 40 分钟（跨日/任务中断后首轮）不计算——
    #    否则涨速会变成跨日跳空幅度，严重失真（实测踩坑 2026-09）
    prev_prices: dict[str, float] = {}
    with _qconn() as c:
        latest_dt_row = c.execute("SELECT MAX(datetime) FROM ifind_realtime").fetchone()
        latest_dt = latest_dt_row[0] if latest_dt_row else None
        if latest_dt:
            try:
                age_sec = (datetime.now() - datetime.strptime(latest_dt, "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                age_sec = 999999
            if age_sec <= 2400:  # 40 分钟内的连续采集序列才算涨速
                cursor = c.execute("SELECT code, price FROM ifind_realtime WHERE datetime = ?", (latest_dt,))
                for row in cursor.fetchall():
                    if row[1] is not None:
                        prev_prices[row[0]] = row[1]

    # 3) iFinD THS_RQ 实时行情（分批，每批50只）
    # SDK只支持基础字段，HTTP API额外支持totalShares/totalCapital（floatCapital暂不可用）
    # 注：不再取 pe_ttm——RQ 的 pe_ttm 是 TTM 口径，会覆盖每日同步的动态PE（ths_pe_stock,3）
    rq_sdk_indicators = "latest,preClose,open,high,low,change,changeRatio,volume,amount,turnoverRatio,quantityRatio,amplitude"
    rq_http_indicators = "latest,preClose,open,high,low,change,changeRatio,volume,amount,turnoverRatio,vol_ratio,amplitude,totalShares,totalCapital"
    batch_size = 50
    rq_data: dict[str, dict] = {}

    for i in range(0, len(all_codes_raw), batch_size):
        batch_codes = all_codes_raw[i:i + batch_size]
        # 转换为 iFinD 格式：SH600519 → 600519.SH
        batch_ifind = []
        for code in batch_codes:
            m = _re.match(r"^([A-Za-z]{2})(\d{6})$", code)
            if m:
                batch_ifind.append(f"{m.group(2)}.{m.group(1).upper()}")
            else:
                batch_ifind.append(code)
        codes_s = ",".join(batch_ifind)
        try:
            # 优先用HTTP API（可获取总股本/总市值）
            df_rq, _res, err = _ths_http("real_time_quotation", {
                "codes": codes_s,
                "indicators": rq_http_indicators.replace(";", ",")})
            if df_rq is None or df_rq.empty:
                # 回退到SDK
                df_rq, _res, err = ths_call("THS_RQ", codes_s, rq_sdk_indicators, "")
            if df_rq is not None and not df_rq.empty:
                rq_code_col = next((c for c in df_rq.columns
                                    if "code" in c.lower() or "代码" in c or "thscode" in c.lower()),
                                   df_rq.columns[0])
                for _, row in df_rq.iterrows():
                    raw = str(row[rq_code_col])
                    normed = _norm(raw)
                    # 计算振幅（如果API未返回）
                    high = _safe_float(row.get("high"))
                    low = _safe_float(row.get("low"))
                    prev = _safe_float(row.get("preClose"))
                    amplitude = _safe_float(row.get("amplitude"))
                    if amplitude is None and high and low and prev:
                        amplitude = round((high - low) / prev * 100, 2)
                    # 计算涨速：(当前价格 - 上次价格) / 上次价格 * 100
                    price = _safe_float(row.get("latest"))
                    speed = 0.0  # 默认涨速为 0
                    if normed in prev_prices and prev_prices[normed]:
                        prev_price = prev_prices[normed]
                        if prev_price > 0 and price:
                            speed = round((price - prev_price) / prev_price * 100, 4)
                    rq_data[normed] = {
                        "price": price,
                        "prev_close": prev,
                        "open": _safe_float(row.get("open")),
                        "high": high,
                        "low": low,
                        "change_pct": _safe_float(row.get("changeRatio")),
                        "volume": _safe_float(row.get("volume")),
                        "amount": _safe_float(row.get("amount")),
                        "turnover": _safe_float(row.get("turnoverRatio")),
                        "quantity_ratio": _safe_float(row.get("vol_ratio")),
                        "amplitude": amplitude,
                        "float_shares": None,  # HTTP API 暂不支持
                        "float_mv": _safe_float(row.get("floatCapital")),
                        "speed": speed,
                    }
        except Exception:
            pass

    # 4) 流通股本/市值：不再用腾讯补充（保持全同花顺数据一致性，用户要求 2026-09）
    #    iFinD HTTP 端不支持 floatCapital（实测静默丢弃），流通列在 SDK 恢复前为空

    # 5) 写入 ifind_realtime 表（使用事务包裹）
    with _qconn() as c:
        # 添加 speed 列（如果不存在）
        rt_cols = [r[1] for r in c.execute("PRAGMA table_info(ifind_realtime)")]
        for col in ["float_shares", "float_mv", "speed"]:
            if col not in rt_cols:
                c.execute(f"ALTER TABLE ifind_realtime ADD COLUMN {col} REAL")

        vals = []
        for code in all_codes_raw:
            rq = rq_data.get(code, {})
            if rq:  # 只写入有数据的记录
                vals.append((
                    code, now,
                    rq.get("price"), rq.get("prev_close"), rq.get("open"),
                    rq.get("high"), rq.get("low"), rq.get("change_pct"),
                    rq.get("volume"), rq.get("amount"), rq.get("turnover"),
                    rq.get("quantity_ratio"), rq.get("amplitude"),
                    rq.get("float_shares"), rq.get("float_mv"),
                    rq.get("speed"),
                ))
        if vals:
            c.executemany(
                "INSERT OR REPLACE INTO ifind_realtime"
                "(code,datetime,price,prev_close,open,high,low,change_pct,"
                "volume,amount,turnover,quantity_ratio,amplitude,float_shares,float_mv,speed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                vals)
    return len(vals)


def get_announcements_from_db() -> pd.DataFrame:
    """从 ifind_announcements 表读取公告（新→旧），附带股票名称（从 ifind_stocklist 映射）。

    数据由 ⏰定时任务 job_ifind_announce 每日抓取（自选股近7天），保留7天自动清理。
    """
    with _qconn() as c:
        df = pd.read_sql_query(
            "SELECT seq, code, report_date, title, pdf_url, ctime, fetched_at"
            " FROM ifind_announcements ORDER BY ctime DESC", c)
        if df.empty:
            return df
        names = dict(c.execute("SELECT code, name FROM ifind_stocklist").fetchall())

    def _norm(code):
        m = re.match(r"(\d{6})\.([A-Z]{2})", str(code).strip())
        return f"{m.group(2)}{m.group(1)}" if m else str(code)

    df["name"] = df["code"].map(lambda x: names.get(_norm(x), ""))
    return df


def cleanup_old_data(retention_days: dict = None):
    """清理过期数据。

    Args:
        retention_days: 各表保留天数配置，默认使用配置值
    """
    if retention_days is None:
        retention_days = {
            "ifind_realtime": 7,
            "market_daily": 15,
            "ifind_basic_daily": 15,
            "ifind_announcements": 7,
        }

    with _qconn() as c:
        for table, days in retention_days.items():
            if table == "ifind_announcements":
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                c.execute(f"DELETE FROM {table} WHERE ctime < ?", (cutoff,))
            elif table == "ifind_realtime":
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute(f"DELETE FROM {table} WHERE datetime < ?", (cutoff,))
            elif table in ("market_daily", "ifind_basic_daily"):
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                c.execute(f"DELETE FROM {table} WHERE date < ?", (cutoff,))


def get_realtime_from_db(codes: list[str] = None, limit: int = 100) -> pd.DataFrame:
    """从 ifind_realtime 表读取最新快照数据。

    Args:
        codes: 股票代码列表，None 则返回全部
        limit: 每只股票返回的记录数（默认1条，即最新快照）
    """
    with _qconn() as c:
        if codes:
            placeholders = ",".join("?" * len(codes))
            query = f"""
                SELECT r.* FROM ifind_realtime r
                INNER JOIN (
                    SELECT code, MAX(datetime) as max_dt
                    FROM ifind_realtime
                    WHERE code IN ({placeholders})
                    GROUP BY code
                ) latest ON r.code = latest.code AND r.datetime = latest.max_dt
            """
            df = pd.read_sql_query(query, c, params=codes)
        else:
            query = """
                SELECT r.* FROM ifind_realtime r
                INNER JOIN (
                    SELECT code, MAX(datetime) as max_dt
                    FROM ifind_realtime
                    GROUP BY code
                ) latest ON r.code = latest.code AND r.datetime = latest.max_dt
            """
            df = pd.read_sql_query(query, c)
    return df


def get_daily_from_db(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """从 market_daily 表读取日线数据。

    Args:
        code: 股票代码
        start_date: 开始日期（YYYY-MM-DD）
        end_date: 结束日期（YYYY-MM-DD）
    """
    with _qconn() as c:
        query = "SELECT * FROM market_daily WHERE code = ? AND source = 'ths_ifind'"
        params = [code]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        df = pd.read_sql_query(query, c, params=params)
    return df


def get_stocklist_from_db() -> pd.DataFrame:
    """从 ifind_stocklist 表读取全市场A股列表，并用 ifind_realtime 最新数据覆盖。"""
    with _qconn() as c:
        df = pd.read_sql_query("SELECT * FROM ifind_stocklist ORDER BY code", c)
        # 用 ifind_realtime 最新快照覆盖价格类和实时字段
        # 注：不覆盖 pe_ttm——ifind_realtime 不再写 PE（RQ 的 pe_ttm 是 TTM 口径），
        #     PE 统一用每日同步的动态 PE（ths_pe_stock,3，与同花顺终端口径一致）
        if not df.empty:
            rt = pd.read_sql_query(
                """SELECT code, price, prev_close, open, high, low,
                          change_pct, volume, amount, turnover,
                          quantity_ratio, amplitude, float_shares, float_mv,
                          speed
                   FROM ifind_realtime
                   WHERE datetime = (SELECT MAX(datetime) FROM ifind_realtime)""",
                c,
            )
            if not rt.empty:
                # 去重，保留每个 code 最新一条
                rt = rt.drop_duplicates(subset=["code"], keep="last")
                # 价格类列：ifind_realtime 有值时优先使用
                price_cols = [
                    "price", "prev_close", "open", "high", "low",
                    "change_pct", "volume", "amount", "turnover",
                    "quantity_ratio", "amplitude", "speed",
                ]
                rt_indexed = rt.set_index("code")
                for col in price_cols:
                    if col in rt_indexed.columns:
                        df[col] = df["code"].map(rt_indexed[col]).combine_first(df[col])
                # 流通股/流通市值：只在 ifind_realtime 有值时覆盖（避免 None 覆盖有效值）
                for col in ["float_shares", "float_mv"]:
                    if col in rt_indexed.columns:
                        mapped = df["code"].map(rt_indexed[col])
                        # 只覆盖 ifind_realtime 有值的行
                        mask = mapped.notna()
                        df.loc[mask, col] = mapped[mask]
    return df
