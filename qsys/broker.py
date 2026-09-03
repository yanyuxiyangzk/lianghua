"""💹 模拟柜台（普通交易）：资金账户 + 委托/成交/撤单 + 持仓 + 资金流水。

规则（A股模拟）：
  - 初始资金 100000 元；买入 100 股整数倍；T+1（当日买入不可当日卖出）
  - 费用：佣金 0.025%（最低 5 元）双边；印花税 0.05% 仅卖出（2023-08 减半后口径）
  - 委托：限价单挂出后，最新价触及限价即成交（买入：现价 ≤ 限价；卖出：现价 ≥ 限价）；
    价格为 0/空 = 市价单，按最新快照价立即成交
  - 行情：ifind_realtime 最新快照（盘中 5 分钟一批，同花顺 iFinD 数据）
"""

import sqlite3
from datetime import datetime

import pandas as pd

from common import DATA_DIR

DB_PATH = DATA_DIR / "experience.db"
INIT_CASH = 100000.0
FEE_RATE = 0.00025      # 佣金万 2.5
FEE_MIN = 5.0           # 佣金最低 5 元
TAX_RATE = 0.0005       # 印花税 0.05%（卖出）
TP_RATE = 0.15          # 手动持仓默认止盈 +15%
SL_RATE = 0.08          # 手动持仓默认止损 -8%

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_account (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS broker_positions (
    code TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', name TEXT,
    shares INTEGER DEFAULT 0, sellable INTEGER DEFAULT 0, today_bought INTEGER DEFAULT 0,
    cost REAL, last_buy_date TEXT, updated_at TEXT,
    tp_price REAL, sl_price REAL,
    PRIMARY KEY (code, source));
CREATE TABLE IF NOT EXISTS broker_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ts TEXT, code TEXT, name TEXT, side TEXT,
    price REAL, shares INTEGER, status TEXT,
    filled_price REAL, filled_ts TEXT, cancel_ts TEXT,
    source TEXT DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS broker_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER, date TEXT, ts TEXT, code TEXT, name TEXT, side TEXT,
    price REAL, shares INTEGER, amount REAL, fee REAL, tax REAL,
    source TEXT DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS broker_cashflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, type TEXT, amount REAL, balance REAL, note TEXT);
"""


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    _migrate(c)
    return c


def _migrate(c):
    """老库迁移：broker_positions 拆 (code, source) 双源（手动/AI）+ 止盈止损价。"""
    cols = [r[1] for r in c.execute("PRAGMA table_info(broker_positions)")]
    if "source" in cols and "tp_price" in cols and "today_bought" in cols:
        return
    c.execute("""CREATE TABLE IF NOT EXISTS broker_positions_mig (
        code TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', name TEXT,
        shares INTEGER DEFAULT 0, sellable INTEGER DEFAULT 0, today_bought INTEGER DEFAULT 0,
        cost REAL, last_buy_date TEXT, updated_at TEXT,
        tp_price REAL, sl_price REAL, PRIMARY KEY (code, source))""")
    c.execute("""INSERT OR IGNORE INTO broker_positions_mig
        (code, source, name, shares, sellable, today_bought, cost, last_buy_date, updated_at)
        SELECT b.code,
               CASE WHEN EXISTS (SELECT 1 FROM positions p
                                 WHERE p.code=b.code AND p.status IN ('open','pending'))
                    THEN 'ai' ELSE 'manual' END,
               b.name, b.shares, b.sellable, 0, b.cost, b.last_buy_date, b.updated_at
        FROM broker_positions b""")
    c.execute("DROP TABLE broker_positions")
    c.execute("ALTER TABLE broker_positions_mig RENAME TO broker_positions")
    ocols = [r[1] for r in c.execute("PRAGMA table_info(broker_orders)")]
    if "source" not in ocols:
        c.execute("ALTER TABLE broker_orders ADD COLUMN source TEXT DEFAULT 'manual'")
    fcols = [r[1] for r in c.execute("PRAGMA table_info(broker_fills)")]
    if "source" not in fcols:
        c.execute("ALTER TABLE broker_fills ADD COLUMN source TEXT DEFAULT 'manual'")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 账户
def _init_account():
    with _conn() as c:
        if not c.execute("SELECT 1 FROM broker_account WHERE key='cash'").fetchone():
            c.execute("INSERT INTO broker_account (key, value) VALUES ('cash', ?)",
                      (str(INIT_CASH),))
            c.execute("INSERT INTO broker_cashflows (ts, type, amount, balance, note)"
                      " VALUES (?,?,?,?,?)",
                      (_now(), "初始入金", INIT_CASH, INIT_CASH, "初始资金"))


def _get_cash() -> float:
    with _conn() as c:
        r = c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()
    return float(r[0]) if r else 0.0


def _set_cash(v: float):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO broker_account (key, value) VALUES ('cash', ?)",
                  (str(round(v, 2)),))


def _cashflow(c, typ: str, amount: float, note: str):
    bal = float(c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()[0])
    c.execute("INSERT INTO broker_cashflows (ts, type, amount, balance, note)"
              " VALUES (?,?,?,?,?)", (_now(), typ, round(amount, 2), round(bal, 2), note))


def _settle_today():
    """T+1 日切：新的一天，所有持仓股数转为可卖。"""
    with _conn() as c:
        r = c.execute("SELECT value FROM broker_account WHERE key='settle_date'").fetchone()
        last = r[0] if r else ""
        if last >= _today():
            return
        c.execute("UPDATE broker_positions SET sellable = shares, today_bought = 0")
        c.execute("INSERT OR REPLACE INTO broker_account (key, value) VALUES ('settle_date', ?)",
                  (_today(),))


# ---------------------------------------------------------------- 行情
def _latest_prices(codes: list[str]) -> dict:
    """ifind_realtime 最新快照 {code: (price, prev_close, open)}。"""
    import datasource
    if not codes:
        return {}
    with datasource._qconn() as c:
        df = pd.read_sql(
            f"SELECT code, price, prev_close, open FROM ifind_realtime"
            f" WHERE datetime=(SELECT MAX(datetime) FROM ifind_realtime)"
            f" AND code IN ({','.join('?' * len(codes))})", c, params=codes)
    return {r.code: (r.price, r.prev_close, r.open) for r in df.itertuples()}


def get_name(code: str) -> str:
    import datasource
    try:
        with datasource._qconn() as c:
            r = c.execute("SELECT name FROM ifind_stocklist WHERE code=?", (code,)).fetchone()
            if r:
                return r[0]
            r = c.execute("SELECT name FROM ifind_indexlist WHERE code=?", (code,)).fetchone()
            return r[0] if r else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 委托/成交
def place_order(code: str, side: str, price: float | None, shares: int,
                source: str = "manual") -> str:
    """下单。side: buy/sell。price 空或 0 = 市价单。返回消息。
    source: manual=手动买入页下单；ai=每日名单自动开仓（类型列区分）。"""
    _init_account()
    _settle_today()
    code = code.strip().upper()
    if not code:
        return "请输入代码"
    shares = int(shares)
    if side == "buy" and (shares <= 0 or shares % 100 != 0):
        return "买入数量须为 100 股整数倍"
    if shares <= 0:
        return "数量须大于 0"
    name = get_name(code)
    pr = _latest_prices([code]).get(code)
    cur = pr[0] if pr else None

    with _conn() as c:
        if side == "sell":
            pos = c.execute("SELECT sellable FROM broker_positions WHERE code=? AND source=?",
                            (code, source)).fetchone()
            if not pos or pos[0] < shares:
                return f"可卖数量不足（可卖 {pos[0] if pos else 0} 股，T+1：当日买入不可当日卖出）"
        # 市价单立即成交检查现金
        is_market = not price or price <= 0
        fill_now = is_market or (cur is not None and (
            (side == "buy" and cur <= price) or (side == "sell" and cur >= price)))
        if side == "buy" and fill_now and cur:
            need = cur * shares + max(FEE_MIN, cur * shares * FEE_RATE)
            if _get_cash() < need:
                return f"可用资金不足（约需 {need:,.2f} 元，含佣金）"
        cur_o = c.execute(
            "INSERT INTO broker_orders (date, ts, code, name, side, price, shares, status, source)"
            " VALUES (?,?,?,?,?,?,?, '已报', ?)",
            (_today(), _now(), code, name, side, price or 0, shares, source)).lastrowid
        if fill_now:
            if cur is None:
                c.execute("UPDATE broker_orders SET status='已撤', cancel_ts=? WHERE id=?",
                          (_now(), cur_o))
                return "无最新行情价，市价单无法成交（已撤）"
            _fill(c, cur_o, cur)
            return f"已成交：{'买入' if side == 'buy' else '卖出'} {code} {shares}股 @ {cur:.2f}"
        return f"已挂单（限价 {price:.2f}，等待价格触及后自动成交，可在撤单页撤销）"


def _fill(c, order_id: int, fill_price: float):
    o = c.execute("SELECT code, name, side, shares, COALESCE(source,'manual')"
                  " FROM broker_orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        return
    code, name, side, shares, source = o
    amount = fill_price * shares
    fee = max(FEE_MIN, amount * FEE_RATE)
    tax = amount * TAX_RATE if side == "sell" else 0.0
    tag = "AI" if source == "ai" else "手动"
    cash = float(c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()[0])
    if side == "buy":
        cash -= (amount + fee)
        c.execute("UPDATE broker_account SET value=? WHERE key='cash'", (str(round(cash, 2)),))
        pos = c.execute("SELECT shares, sellable, today_bought, cost FROM broker_positions"
                        " WHERE code=? AND source=?", (code, source)).fetchone()
        if pos:
            new_shares = pos[0] + shares
            new_cost = (pos[3] * pos[0] + amount) / new_shares
            c.execute("UPDATE broker_positions SET shares=?, today_bought=?, cost=?,"
                      " tp_price=?, sl_price=?, last_buy_date=?, updated_at=?"
                      " WHERE code=? AND source=?",
                      (new_shares, pos[2] + shares, round(new_cost, 4),
                       round(new_cost * (1 + TP_RATE), 4), round(new_cost * (1 - SL_RATE), 4),
                       _today(), _now(), code, source))
        else:
            c.execute("INSERT INTO broker_positions (code, source, name, shares, sellable,"
                      " today_bought, cost, last_buy_date, updated_at, tp_price, sl_price)"
                      " VALUES (?,?,?,?,0,?,?,?,?,?,?)",
                      (code, source, name, shares, shares, round(fill_price, 4), _today(), _now(),
                       round(fill_price * (1 + TP_RATE), 4),
                       round(fill_price * (1 - SL_RATE), 4)))
        _cashflow(c, "买入", -(amount + fee),
                  f"[{tag}]买入 {name or code} {shares}股@{fill_price:.2f}")
    else:
        cash += (amount - fee - tax)
        c.execute("UPDATE broker_account SET value=? WHERE key='cash'", (str(round(cash, 2)),))
        c.execute("UPDATE broker_positions SET shares = shares - ?, sellable = sellable - ?,"
                  " updated_at=? WHERE code=? AND source=?", (shares, shares, _now(), code, source))
        c.execute("DELETE FROM broker_positions WHERE code=? AND source=? AND shares <= 0",
                  (code, source))
        _cashflow(c, "卖出", amount - fee - tax,
                  f"[{tag}]卖出 {name or code} {shares}股@{fill_price:.2f}")
    c.execute("UPDATE broker_orders SET status='已成', filled_price=?, filled_ts=? WHERE id=?",
              (round(fill_price, 4), _now(), order_id))
    c.execute("INSERT INTO broker_fills (order_id, date, ts, code, name, side, price, shares,"
              " amount, fee, tax, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (order_id, _today(), _now(), code, name, side, fill_price, shares,
               round(amount, 2), round(fee, 2), round(tax, 2), source))


def fill_pending_orders() -> int:
    """盘中由持仓跟踪任务调用：检查已报挂单，价格触及限价即成交。返回成交笔数。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        pending = c.execute(
            "SELECT id, code, side, price FROM broker_orders WHERE status='已报'").fetchall()
        if not pending:
            return 0
        prices = _latest_prices([p[1] for p in pending])
        n = 0
        for oid, code, side, limit in pending:
            pr = prices.get(code)
            cur = pr[0] if pr else None
            if cur is None:
                continue
            if (side == "buy" and cur <= limit) or (side == "sell" and cur >= limit):
                if side == "buy":
                    shares = c.execute(
                        "SELECT shares FROM broker_orders WHERE id=?", (oid,)).fetchone()[0]
                    need = cur * shares + max(FEE_MIN, cur * shares * FEE_RATE)
                    cash = float(c.execute(
                        "SELECT value FROM broker_account WHERE key='cash'").fetchone()[0])
                    if cash < need:
                        continue  # 资金不足留挂
                _fill(c, oid, cur)
                n += 1
        return n


def check_stop_exits() -> int:
    """盘中自动止盈/止损（手动持仓）：实盘价触及止盈价/止损价即自动卖出（T+1 可卖校验）。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        rows = pd.read_sql(
            "SELECT * FROM broker_positions WHERE source='manual' AND shares > 0", c)
    if rows.empty:
        return 0
    prices = _latest_prices(list(rows["code"]))
    n = 0
    for _, r in rows.iterrows():
        if int(r["sellable"] or 0) <= 0:
            continue  # T+1：当日买入不可卖
        pr = prices.get(r["code"])
        cur = pr[0] if pr else None
        if not cur or not r["cost"]:
            continue
        tp = r["tp_price"] if r["tp_price"] else r["cost"] * (1 + TP_RATE)
        sl = r["sl_price"] if r["sl_price"] else r["cost"] * (1 - SL_RATE)
        if cur >= tp or cur <= sl:
            msg = place_order(r["code"], "sell", None, int(r["sellable"]), source="manual")
            if "已成交" in msg:
                n += 1
    return n


def cancel_order(order_id: int) -> str:
    with _conn() as c:
        r = c.execute("SELECT status FROM broker_orders WHERE id=?", (order_id,)).fetchone()
        if not r:
            return "委托不存在"
        if r[0] != "已报":
            return "只能撤销已报状态的委托"
        c.execute("UPDATE broker_orders SET status='已撤', cancel_ts=? WHERE id=?",
                  (_now(), order_id))
    return f"委托 #{order_id} 已撤销"


# ---------------------------------------------------------------- 查询
def get_account() -> dict:
    """账户总览：总资产/可用资金/持仓市值/持仓盈亏/今日盈亏。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        poss = pd.read_sql("SELECT * FROM broker_positions", c)
    cash = _get_cash()
    if poss.empty:
        return {"总资产": cash, "可用资金": cash, "持仓市值": 0.0,
                "持仓盈亏": 0.0, "今日盈亏": 0.0}
    prices = _latest_prices(list(poss["code"]))
    poss["最新价"] = poss["code"].map(lambda x: (prices.get(x) or (None, None))[0])
    poss["昨收"] = poss["code"].map(lambda x: (prices.get(x) or (None, None))[1])
    mv = (poss["最新价"].fillna(poss["cost"]) * poss["shares"]).sum()
    pos_pnl = ((poss["最新价"].fillna(poss["cost"]) - poss["cost"]) * poss["shares"]).sum()
    day_pnl = ((poss["最新价"].fillna(poss["昨收"]) - poss["昨收"]) * poss["shares"]).sum()
    return {"总资产": cash + mv, "可用资金": cash, "持仓市值": mv,
            "持仓盈亏": pos_pnl, "今日盈亏": day_pnl}


def get_positions() -> pd.DataFrame:
    """持仓列表（含最新价/盈亏/今日盈亏/可卖数量）。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM broker_positions WHERE shares > 0", c)
    if df.empty:
        return df
    prices = _latest_prices(list(df["code"]))
    df["最新价"] = df["code"].map(lambda x: (prices.get(x) or (None, None))[0])
    df["昨收"] = df["code"].map(lambda x: (prices.get(x) or (None, None))[1])
    df["市值"] = df["最新价"].fillna(df["cost"]) * df["shares"]
    df["持仓盈亏"] = (df["最新价"].fillna(df["cost"]) - df["cost"]) * df["shares"]
    df["今日盈亏"] = (df["最新价"].fillna(df["昨收"]) - df["昨收"]) * df["shares"]
    df["盈亏%"] = (df["最新价"].fillna(df["cost"]) / df["cost"] - 1) * 100
    return df


def list_orders(today_only: bool = True) -> pd.DataFrame:
    with _conn() as c:
        q = "SELECT * FROM broker_orders"
        if today_only:
            q += f" WHERE date='{_today()}'"
        return pd.read_sql(q + " ORDER BY id DESC", c)


def list_fills(today_only: bool = True) -> pd.DataFrame:
    with _conn() as c:
        q = "SELECT * FROM broker_fills"
        if today_only:
            q += f" WHERE date='{_today()}'"
        return pd.read_sql(q + " ORDER BY id DESC", c)


def list_cashflows(limit: int = 200) -> pd.DataFrame:
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM broker_cashflows ORDER BY id DESC LIMIT ?", c, params=(limit,))
