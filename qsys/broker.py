"""💹 模拟柜台（普通交易）：资金账户 + 委托/成交/撤单 + 持仓 + 资金流水。

规则（A股模拟）：
  - 初始资金 200000 元；买入 100 股整数倍；T+1（当日买入不可当日卖出）
  - 费用：佣金 0.025%（最低 5 元）双边；印花税 0.05% 仅卖出（2023-08 减半后口径）
  - 委托：限价单挂出后，最新价触及限价即成交（买入：现价 ≤ 限价；卖出：现价 ≥ 限价）；
    价格为 0/空 = 市价单，按最新快照价立即成交
  - 行情：ifind_realtime 最新快照（盘中 5 分钟一批，同花顺 iFinD 数据）
"""

import sqlite3
import math
from contextlib import nullcontext
from datetime import datetime

import pandas as pd

from common import DATA_DIR

DB_PATH = DATA_DIR / "experience.db"
INIT_CASH = 200000.0
FEE_RATE = 0.00025      # 佣金万 2.5
FEE_MIN = 5.0           # 佣金最低 5 元
TAX_RATE = 0.0005       # 印花税 0.05%（卖出）
TP_RATE = 0.15          # 手动持仓默认止盈 +15%
SL_RATE = 0.08          # 手动持仓默认止损 -8%


def _main_risk_rates() -> tuple[float, float]:
    """读取后台主轨风控参数；导入失败时回退到兼容常量。"""
    try:
        import experience
        rules = experience.get_risk_rules("main")
        return float(rules["take_profit"]), abs(float(rules["stop_loss"]))
    except Exception:
        return TP_RATE, SL_RATE

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
    ts TEXT, type TEXT, amount REAL, balance REAL, note TEXT,
    source TEXT DEFAULT 'manual');
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(_SCHEMA)
    _migrate(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(broker_orders)")}
    if "position_id" not in cols:
        c.execute("ALTER TABLE broker_orders ADD COLUMN position_id INTEGER")
    for col in ("signal_source", "strategy_name"):
        if col not in cols:
            c.execute(f"ALTER TABLE broker_orders ADD COLUMN {col} TEXT")
    c.commit()
    return c


def _migrate(c):
    """老库迁移：broker_positions 拆 (code, source) 双源（手动/AI）+ 止盈止损价。"""
    cols = [r[1] for r in c.execute("PRAGMA table_info(broker_positions)")]
    if "source" in cols and "tp_price" in cols and "today_bought" in cols:
        ccols = [r[1] for r in c.execute("PRAGMA table_info(broker_cashflows)")]
        if "source" not in ccols:
            c.execute("ALTER TABLE broker_cashflows ADD COLUMN source TEXT DEFAULT 'manual'")
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
    ccols = [r[1] for r in c.execute("PRAGMA table_info(broker_cashflows)")]
    if "source" not in ccols:
        c.execute("ALTER TABLE broker_cashflows ADD COLUMN source TEXT DEFAULT 'manual'")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 账户
def _init_account():
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
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


# 卫星轨独立现金池
SATELLITE_INIT_CASH = 20000.0


def _get_satellite_cash() -> float:
    """获取卫星轨可用现金。"""
    with _conn() as c:
        r = c.execute("SELECT value FROM broker_account WHERE key='cash_satellite'").fetchone()
    if r is None:
        # 首次使用，初始化卫星轨现金
        _set_satellite_cash(SATELLITE_INIT_CASH)
        return SATELLITE_INIT_CASH
    return float(r[0])


def _set_satellite_cash(v: float):
    """设置卫星轨现金。"""
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO broker_account (key, value) VALUES ('cash_satellite', ?)",
            (str(round(v, 2)),))

def _cash_key(source: str) -> str:
    return "cash_satellite" if source == "satellite" else "cash"

def _get_cash_for_source(source: str) -> float:
    return _get_satellite_cash() if source == "satellite" else _get_cash()


def _cashflow(c, typ: str, amount: float, note: str, source: str = "manual"):
    key = _cash_key(source)
    bal = float(c.execute("SELECT value FROM broker_account WHERE key=?", (key,)).fetchone()[0])
    c.execute("INSERT INTO broker_cashflows (ts, type, amount, balance, note, source)"
              " VALUES (?,?,?,?,?,?)", (_now(), typ, round(amount, 2), round(bal, 2), note, source))


def _settle_today():
    """T+1 日切：新的一天，所有持仓股数转为可卖。"""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT value FROM broker_account WHERE key='settle_date'").fetchone()
        last = r[0] if r else ""
        if last >= _today():
            return
        c.execute("UPDATE broker_positions SET sellable = shares, today_bought = 0")
        c.execute("INSERT OR REPLACE INTO broker_account (key, value) VALUES ('settle_date', ?)",
                  (_today(),))


# ---------------------------------------------------------------- 行情
def _latest_prices(codes: list[str]) -> dict:
    """ifind_realtime 每代码各自最新快照 {code: (price, prev_close, open, limit_up, limit_down)}。
    按代码取最新：热码高频快照与全市场批次同表共存时冷码不丢（2026-09-11）。"""
    import datasource
    if not codes:
        return {}
    with datasource._qconn() as c:
        df = pd.read_sql(
            f"""SELECT r.code, r.price, r.prev_close, r.open, r.limit_up, r.limit_down
                FROM ifind_realtime r
                JOIN (SELECT code, MAX(datetime) md FROM ifind_realtime
                      WHERE code IN ({','.join('?' * len(codes))}) GROUP BY code) t
                  ON r.code = t.code AND r.datetime = t.md""", c, params=codes)
    return {r.code: (r.price, r.prev_close, r.open, r.limit_up, r.limit_down)
            for r in df.itertuples()}


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


def _market_open() -> bool:
    import experience
    now = datetime.now()
    day, hm = now.strftime('%Y-%m-%d'), now.strftime('%H%M')
    cal = experience._calendar()
    if cal and cal[0] <= day <= cal[-1] and day not in cal:
        return False
    return now.weekday() < 5 and ('0930' <= hm < '1130' or '1300' <= hm < '1500')


def _quote_fresh(code: str) -> bool:
    import experience
    try:
        ts = experience._latest_price_times([code]).get(code)
        dt = datetime.fromisoformat(ts) if ts else None
        return dt is not None and dt.strftime('%Y-%m-%d') == _today() and -60 <= (datetime.now() - dt).total_seconds() <= 600
    except Exception:
        return False


def _available_cash(c, source: str, exclude_order: int = -1) -> float:
    key = _cash_key(source)
    row = c.execute('SELECT value FROM broker_account WHERE key=?', (key,)).fetchone()
    reserved = sum(price * shares + max(FEE_MIN, price * shares * FEE_RATE)
                   for price, shares, src in c.execute(
                       "SELECT price,shares,source FROM broker_orders WHERE side='buy' AND status='已报' AND id!=?",
                       (exclude_order,)) if _cash_key(src) == key)
    return (float(row[0]) if row else 0.0) - reserved


def _available_shares(c, code: str, source: str, exclude_order: int = -1) -> int:
    row = c.execute('SELECT sellable FROM broker_positions WHERE code=? AND source=?', (code, source)).fetchone()
    reserved = c.execute("SELECT COALESCE(SUM(shares),0) FROM broker_orders WHERE code=? AND source=? "
                         "AND side='sell' AND status='已报' AND id!=?", (code, source, exclude_order)).fetchone()[0]
    return max(0, int(row[0] or 0) - int(reserved)) if row else 0


def _buy_rejection(c, code, source, shares, price, exclude_order=-1) -> str:
    import experience
    halt, reason = experience.risk_halt_today(_today())
    if halt:
        return f'风控拦截：{reason}'
    if source == 'satellite':
        return '风控拦截：旧独立卫星交易入口已停用，请使用主轨统一持仓入口'
    if source == 'ai' and (c.execute('SELECT 1 FROM broker_positions WHERE code=? AND shares>0', (code,)).fetchone()
            or c.execute("SELECT 1 FROM broker_orders WHERE code=? AND side='buy' AND status='已报' AND id!=?", (code, exclude_order)).fetchone()):
        return '风控拦截：该股票已有持仓或买入委托，禁止自动重复加仓'
    holdings = c.execute("SELECT code,shares,cost FROM broker_positions WHERE shares>0 AND source!='satellite'").fetchall()
    orders = c.execute("SELECT code,shares,price FROM broker_orders WHERE side='buy' AND status='已报' "
                       "AND source!='satellite' AND id!=?", (exclude_order,)).fetchall()
    occupied = {r[0] for r in holdings + orders}
    if code not in occupied and len(occupied) >= 8:
        return '风控拦截：持仓及买入委托已达8只'
    quotes = _latest_prices([r[0] for r in holdings])
    values = {}
    for cd, sh, cost in holdings:
        mark = (quotes.get(cd) or (cost,))[0] or cost
        if mark is None or not math.isfinite(float(mark)) or mark <= 0:
            return '风控拦截：持仓估值不可用'
        values[cd] = values.get(cd, 0) + sh * mark
    cash_row = c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()
    total = (float(cash_row[0]) if cash_row else 0) + sum(values.values())
    pending = sum(sh * px for _, sh, px in orders)
    single_pending = sum(sh * px for cd, sh, px in orders if cd == code)
    if total <= 0 or not math.isfinite(total) or price <= 0 or not math.isfinite(price):
        return '风控拦截：账户估值或价格无效'
    if values.get(code, 0) + single_pending + price * shares > total * .15:
        return '风控拦截：单票总敞口不得超过总资产15%'
    target = experience.get_account_risk_config()['normal_target']
    try:
        import json
        state = json.loads(experience._RISK_FLAG.read_text())
        if state.get('date') == _today():
            level = state.get('level', 'normal')
            target = min(target, experience.get_account_risk_config().get(level + '_target', target))
            if state.get('target_position_ratio') is not None:
                target = min(target, float(state['target_position_ratio']))
    except FileNotFoundError:
        pass  # risk_halt_today 已负责缺失拦截
    except Exception:
        return '风控拦截：目标仓位不可用'
    if not math.isfinite(target) or not 0 <= target <= 1:
        return '风控拦截：目标仓位无效'
    if sum(values.values()) + pending + price * shares > total * target:
        return '风控拦截：超过账户总仓位上限'
    return ''


def _record_position_sale(c, position_id, order_id, shares, price):
    import experience
    c.row_factory = sqlite3.Row
    row = c.execute('SELECT * FROM positions WHERE id=?', (position_id,)).fetchone()
    c.row_factory = None
    if row is None or row['status'] not in ('open', 'closing') or shares > (row['shares'] or 0):
        raise ValueError('策略持仓与卖出成交不一致')
    remaining = int(row['shares']) - shares
    if remaining:
        c.execute("UPDATE positions SET status='open',shares=?,buy_amount=?,sell_order_id=NULL WHERE id=?",
                  (remaining, round(remaining * row['buy_price'], 2), position_id))
    else:
        rules = experience._get_position_rules(dict(row))
        pnl = price / row['buy_price'] - 1 - rules['cost']
        c.execute("UPDATE positions SET status='closed',sell_date=?,sell_price=?,sell_ts=?,"
                  "pnl_pct=?,hold_days=?,closed_at=?,sell_order_id=? WHERE id=?",
                  (_today(), price, _now(), round(pnl, 6),
                   experience._trade_days_between(row['buy_date'], _today()), _now(), order_id, position_id))


def sell_position(position_id: int, shares: int, price=None, reason='手动卖出') -> str:
    """锁内重读持仓，委托、成交、持仓减记使用同一事务。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT code,status,shares,buy_date FROM positions WHERE id=?', (position_id,)).fetchone()
        if not row or row[1] != 'open':
            return '持仓不存在或已有卖出委托'
        if shares <= 0 or shares > (row[2] or 0):
            return '卖出数量超出持仓'
        if row[3] >= _today():
            return 'T+1：当日买入不可当日卖出'
        c.execute('UPDATE positions SET sell_attempts=COALESCE(sell_attempts,0)+1,last_sell_attempt=? WHERE id=?', (_now(), position_id))
        msg = place_order(row[0], 'sell', price, shares, source='ai', _connection=c, _position_id=position_id)
        if '已挂单' in msg or '已成交' in msg:
            oid = c.execute('SELECT id FROM broker_orders WHERE position_id=? AND side=\'sell\' ORDER BY id DESC LIMIT 1', (position_id,)).fetchone()[0]
            c.execute('UPDATE positions SET sell_reason=?,sell_order_id=? WHERE id=?', (reason, oid, position_id))
            if '已挂单' in msg:
                c.execute("UPDATE positions SET status='closing' WHERE id=?", (position_id,))
        return msg


# ---------------------------------------------------------------- 委托/成交
def place_order(code: str, side: str, price: float | None, shares: int,
                source: str = "manual", *, _connection=None, _max_buy_price=None,
                _position_id=None) -> str:
    """下单。side: buy/sell。price 空或 0 = 市价单。返回消息。
    source: manual=手动买入页下单；ai=每日名单自动开仓（类型列区分）。"""
    if _connection is None:
        _init_account()
        _settle_today()
    code = code.strip().upper()
    if not code:
        return "请输入代码"
    if side not in ("buy", "sell") or source not in ("manual", "ai", "satellite"):
        return "无效的买卖方向或资金来源"
    try:
        if not math.isfinite(float(shares)) or int(shares) != float(shares):
            return "数量须为整数"
        shares = int(shares)
        if price is not None and (not math.isfinite(float(price)) or float(price) < 0):
            return "委托价格无效"
        price = float(price) if price is not None else None
    except (TypeError, ValueError, OverflowError):
        return "委托价格或数量无效"
    if not _market_open():
        return "非交易时段，禁止下单"
    if side == "buy" and (shares <= 0 or shares % 100 != 0):
        return "买入数量须为 100 股整数倍"
    if shares <= 0:
        return "数量须大于 0"
    if not _quote_fresh(code):
        return "行情缺失或过期，禁止下单"
    name = get_name(code)
    pr = _latest_prices([code]).get(code)
    cur = pr[0] if pr else None
    if cur is not None and (not math.isfinite(float(cur)) or cur <= 0):
        return "行情价格无效"
    if side == "buy" and _max_buy_price is not None and (
            cur is None or cur > _max_buy_price):
        return "最新价未触及限价"

    with (_conn() if _connection is None else nullcontext(_connection)) as c:
        if _connection is None:
            c.execute("BEGIN IMMEDIATE")
        if source == 'ai' and side == 'buy':
            import execution_gate
            rejection = execution_gate.position_rejection(c, _position_id, _today())
            if rejection:
                return '执行资格拦截：' + rejection
        if source == 'ai' and _position_id is None:
            return '风控拦截：AI委托必须关联策略持仓任务'
        if side == "buy":
            rejection = _buy_rejection(c, code, source, shares, max(cur or 0, price or 0))
            if rejection:
                return rejection
        if side == "sell":
            available = _available_shares(c, code, source)
            if available < shares:
                return f"可卖数量不足（可用 {available} 股，含T+1及未成交卖单占用）"
        is_market = not price or price <= 0
        # 涨跌停可成交性（实盘规则）：涨停买单/跌停卖单不可立即成交
        limit_up = pr[3] if pr and len(pr) > 3 else None
        limit_down = pr[4] if pr and len(pr) > 4 else None
        if limit_down is None and pr and pr[1]:
            # 缺 lowerLimit 时按 upperLimit 幅度推（各板块幅度对称）
            limit_down = round(pr[1] * (2 - limit_up / pr[1]), 2) if limit_up else None
        at_limit_up = bool(cur and limit_up and cur >= limit_up * 0.999)
        at_limit_down = bool(cur and limit_down and cur <= limit_down * 1.001)
        if side == "buy" and at_limit_up:
            return f"已涨停（{limit_up}），买单无法成交（实盘规则：涨停买不进）"
        if side == "sell" and at_limit_down and is_market:
            # 市价卖单打在跌停板上无法成交 → 自动转为限价挂（略低于现价，等开板）
            price = round(cur * 0.995, 2)
            is_market = False
        # 市价单立即成交检查现金；限价卖单挂在跌停价上也不予成交（等开板）
        fill_now = is_market or (cur is not None and (
            (side == "buy" and cur <= price) or
            (side == "sell" and cur >= price and not at_limit_down)))
        if side == "buy":
            reserve_price = cur if fill_now else price
            if not reserve_price:
                return "无有效价格，无法预留买入资金"
            need = reserve_price * shares + max(FEE_MIN, reserve_price * shares * FEE_RATE)
            if _available_cash(c, source) + 1e-8 < need:
                return f"可用资金不足（约需 {need:,.2f} 元，含佣金及挂单占用）"
        origin = c.execute("SELECT source,pack_name FROM positions WHERE id=?", (_position_id,)).fetchone() if _position_id is not None else None
        signal_source, strategy_name = origin if origin else (source, None)
        cur_o = c.execute(
            "INSERT INTO broker_orders (date, ts, code, name, side, price, shares, status, source, position_id, signal_source, strategy_name)"
            " VALUES (?,?,?,?,?,?,?, '已报', ?, ?, ?, ?)",
            (_today(), _now(), code, name, side, price or 0, shares, source, _position_id, signal_source, strategy_name)).lastrowid
        if fill_now:
            if cur is None:
                c.execute("UPDATE broker_orders SET status='已撤', cancel_ts=? WHERE id=?",
                          (_now(), cur_o))
                return "无最新行情价，市价单无法成交（已撤）"
            if not _fill(c, cur_o, cur):
                c.execute("UPDATE broker_orders SET status='已撤',cancel_ts=? WHERE id=?", (_now(), cur_o))
                return "成交复核未通过，委托已撤销"
            return f"已成交：{'买入' if side == 'buy' else '卖出'} {code} {shares}股 @ {cur:.2f}"
        return (f"已挂单（限价 {price:.2f}，等待价格触及后自动成交，当日有效"
                f"，收盘未成交自动撤销）（委托号 #{cur_o}）")


def buy_position(position_id: int, shares: int) -> str:
    """同一 SQLite 事务内完成柜台成交和策略持仓记账；失败一起回滚。"""
    hm = datetime.now().strftime("%H%M")
    if datetime.now().weekday() >= 5 or not ("0930" <= hm < "1130" or "1300" <= hm < "1500"):
        return "非连续交易时段，禁止自动买入"
    _init_account()
    _settle_today()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT code,status,buy_date,source,limit_price FROM positions WHERE id=?",
                        (position_id,)).fetchone()
        if not row or row[1] != "pending":
            return "该持仓任务已处理"
        if row[2] != _today():
            return "非当日买入任务，禁止成交"
        import experience
        if row[3] == "satellite_scan" and experience.satellite_halt_today(_today())[0]:
            return "风控拦截：卫星开仓暂停"
        if c.execute("SELECT 1 FROM positions WHERE code=? AND id!=? AND status IN ('open','closing')", (row[0], position_id)).fetchone():
            return '风控拦截：策略账本已有持仓，须先完成对账'
        quote = _latest_prices([row[0]]).get(row[0])
        if not quote or not quote[0] or not row[4] or quote[0] > row[4]:
            return "最新价未触及限价"
        if row[3] == 'satellite_scan':
            sat = c.execute("SELECT code,shares,buy_price FROM positions WHERE source='satellite_scan' AND status IN ('open','closing')").fetchall()
            holdings = c.execute("SELECT code,shares,cost FROM broker_positions WHERE shares>0 AND source!='satellite'").fetchall()
            quotes = _latest_prices([x[0] for x in holdings + sat])
            cash = c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()
            total = float(cash[0]) + sum(sh * ((quotes.get(cd) or (cost,))[0] or cost) for cd, sh, cost in holdings)
            sat_value = sum(sh * ((quotes.get(cd) or (cost,))[0] or cost) for cd, sh, cost in sat)
            amount = quote[0] * shares
            if len({x[0] for x in sat}) >= 3 or amount > .05 * total or sat_value + amount > .15 * total:
                return '风控拦截：卫星来源超过3只/单票5%/合计15%上限'
        msg = place_order(row[0], "buy", None, shares, source="ai", _connection=c,
                          _max_buy_price=row[4], _position_id=position_id)
        if "已成交" not in msg:
            return msg
        fill = c.execute("SELECT price,ts FROM broker_fills WHERE code=? AND side='buy' "
                         "AND source='ai' ORDER BY id DESC LIMIT 1", (row[0],)).fetchone()
        c.execute("UPDATE positions SET status='open',buy_price=?,buy_ts=?,shares=?,"
                  "buy_amount=?,max_close=? WHERE id=? AND status='pending'",
                  (fill[0], fill[1], shares, round(fill[0] * shares, 2), fill[0], position_id))
        return msg


def _fill(c, order_id: int, fill_price: float):
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    o = c.execute("SELECT code, name, side, shares, COALESCE(source,'manual'),date,position_id"
                  " FROM broker_orders WHERE id=? AND status='已报'", (order_id,)).fetchone()
    if not o:
        return False
    code, name, side, shares, source, order_date, position_id = o
    if order_date != _today() or not _market_open() or not _quote_fresh(code):
        return False
    if not math.isfinite(fill_price) or fill_price <= 0:
        return False
    if side == "buy":
        if source == 'ai':
            import execution_gate
            if execution_gate.position_rejection(c, position_id, _today()):
                return False
        if source == 'ai' and position_id is None:
            return False  # 旧版无任务关联的自动买单不得直接恢复执行
        if _buy_rejection(c, code, source, shares, fill_price, order_id):
            return False
        need = fill_price * shares + max(FEE_MIN, fill_price * shares * FEE_RATE)
        if _available_cash(c, source, order_id) + 1e-8 < need:
            return False
    elif side == "sell":
        if _available_shares(c, code, source, order_id) < shares:
            return False
    else:
        return False
    amount = fill_price * shares
    fee = max(FEE_MIN, amount * FEE_RATE)
    tax = amount * TAX_RATE if side == "sell" else 0.0
    origin = c.execute('SELECT signal_source,strategy_name FROM broker_orders WHERE id=?', (order_id,)).fetchone()
    tag = "卫星轨" if origin and origin[0] == "satellite_scan" else ("AI" if source == "ai" else "手动")
    if origin and origin[1]:
        tag += f"/{origin[1]}"
    cash_key = _cash_key(source)
    cash = float(c.execute("SELECT value FROM broker_account WHERE key=?", (cash_key,)).fetchone()[0])
    if side == "buy":
        tp_rate, sl_rate = _main_risk_rates()
        cash -= (amount + fee)
        c.execute("UPDATE broker_account SET value=? WHERE key=?", (str(round(cash, 2)), cash_key))
        pos = c.execute("SELECT shares, sellable, today_bought, cost FROM broker_positions"
                        " WHERE code=? AND source=?", (code, source)).fetchone()
        if pos:
            new_shares = pos[0] + shares
            new_cost = (pos[3] * pos[0] + amount) / new_shares
            c.execute("UPDATE broker_positions SET shares=?, today_bought=?, cost=?,"
                      " tp_price=?, sl_price=?, last_buy_date=?, updated_at=?"
                      " WHERE code=? AND source=?",
                      (new_shares, pos[2] + shares, round(new_cost, 4),
                       round(new_cost * (1 + tp_rate), 4), round(new_cost * (1 - sl_rate), 4),
                       _today(), _now(), code, source))
        else:
            c.execute("INSERT INTO broker_positions (code, source, name, shares, sellable,"
                      " today_bought, cost, last_buy_date, updated_at, tp_price, sl_price)"
                      " VALUES (?,?,?,?,0,?,?,?,?,?,?)",
                      (code, source, name, shares, shares, round(fill_price, 4), _today(), _now(),
                       round(fill_price * (1 + tp_rate), 4),
                       round(fill_price * (1 - sl_rate), 4)))
        _cashflow(c, "买入", -(amount + fee),
                  f"[{tag}]买入 {name or code} {shares}股@{fill_price:.2f}", source)
    else:
        cash += (amount - fee - tax)
        c.execute("UPDATE broker_account SET value=? WHERE key=?", (str(round(cash, 2)), cash_key))
        c.execute("UPDATE broker_positions SET shares = shares - ?, sellable = sellable - ?,"
                  " updated_at=? WHERE code=? AND source=?", (shares, shares, _now(), code, source))
        c.execute("DELETE FROM broker_positions WHERE code=? AND source=? AND shares <= 0",
                  (code, source))
        _cashflow(c, "卖出", amount - fee - tax,
                  f"[{tag}]卖出 {name or code} {shares}股@{fill_price:.2f}", source)
    c.execute("UPDATE broker_orders SET status='已成', filled_price=?, filled_ts=? WHERE id=?",
              (round(fill_price, 4), _now(), order_id))
    c.execute("INSERT INTO broker_fills (order_id, date, ts, code, name, side, price, shares,"
              " amount, fee, tax, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (order_id, _today(), _now(), code, name, side, fill_price, shares,
               round(amount, 2), round(fee, 2), round(tax, 2), source))
    if side == "sell" and position_id is not None:
        _record_position_sale(c, position_id, order_id, shares, fill_price)
    return True


def fill_pending_orders() -> int:
    """盘中由持仓跟踪任务调用：检查已报挂单，价格触及限价即成交。返回成交笔数。
    可成交性约束（实盘规则）：买单在涨停价上、卖单在跌停价上不予成交（挂起等开板）。"""
    _init_account()
    _settle_today()
    expire_day_orders()
    if not _market_open():
        return 0
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
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
            limit_up = pr[3] if len(pr) > 3 else None
            limit_down = pr[4] if len(pr) > 4 else None
            if limit_down is None and pr[1] and limit_up:
                limit_down = round(pr[1] * (2 - limit_up / pr[1]), 2)
            if side == "buy" and limit_up and cur >= limit_up * 0.999:
                continue  # 涨停买不进
            if side == "sell" and limit_down and cur <= limit_down * 1.001:
                continue  # 跌停卖不出
            if (side == "buy" and cur <= limit) or (side == "sell" and cur >= limit):
                if _fill(c, oid, cur):
                    n += 1
        return n


def expire_day_orders() -> int:
    """日终撤单（实盘规则：委托当日有效）：15:00 后把当日未成交挂单全部撤销。
    次日由止盈止损/开仓逻辑按当时价格重新评估重新挂单。"""
    today = _today()
    now_hm = datetime.now().strftime("%H%M")
    with _conn() as c:
        n = c.execute(
            "UPDATE broker_orders SET status='已撤', cancel_ts=? WHERE status='已报' "
            "AND (date<? OR (date=? AND ? >= '1500'))",
            (_now(), today, today, now_hm)).rowcount
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
    tp_rate, sl_rate = _main_risk_rates()
    for _, r in rows.iterrows():
        if int(r["sellable"] or 0) <= 0:
            continue  # T+1：当日买入不可卖
        pr = prices.get(r["code"])
        cur = pr[0] if pr else None
        if not cur or not r["cost"]:
            continue
        tp = r["tp_price"] if r["tp_price"] else r["cost"] * (1 + tp_rate)
        sl = r["sl_price"] if r["sl_price"] else r["cost"] * (1 - sl_rate)
        if cur >= tp or cur <= sl:
            msg = place_order(r["code"], "sell", None, int(r["sellable"]), source="manual")
            if "已成交" in msg:
                n += 1
    return n


def cancel_order(order_id: int) -> str:
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT status FROM broker_orders WHERE id=?", (order_id,)).fetchone()
        if not r:
            return "委托不存在"
        if r[0] != "已报":
            return "只能撤销已报状态的委托"
        c.execute("UPDATE broker_orders SET status='已撤', cancel_ts=? WHERE id=?",
                  (_now(), order_id))
    return f"委托 #{order_id} 已撤销"


def cancel_pending_buys(source: str | None = None) -> int:
    """撤销尚未成交的买单；卖单不受账户开仓闸影响。"""
    with _conn() as c:
        sql = ("UPDATE broker_orders SET status='已撤', cancel_ts=? "
               "WHERE status='已报' AND side='buy'")
        params: list = [_now()]
        if source:
            sql += " AND source=?"
            params.append(source)
        return int(c.execute(sql, params).rowcount)


def _day_pnl_by_code(positions: pd.DataFrame, fills: pd.DataFrame, prices: dict) -> dict:
    """昨仓按昨收计价，当日买卖按成交金额计价，计入佣金和税。"""
    held = positions.groupby('code')['shares'].sum().to_dict() if not positions.empty else {}
    codes = set(held) | (set(fills['code']) if not fills.empty else set())
    result = {}
    for code in codes:
        trades = fills[fills['code'] == code] if not fills.empty else fills
        bought = sold = buy_amount = sell_amount = fees = 0.0
        if not trades.empty:
            buys = trades[trades['side'] == 'buy']
            sells = trades[trades['side'] == 'sell']
            bought, sold = buys['shares'].sum(), sells['shares'].sum()
            buy_amount, sell_amount = buys['amount'].sum(), sells['amount'].sum()
            fees = trades['fee'].sum() + trades['tax'].sum()
        closing = held.get(code, 0)
        opening = closing - bought + sold
        quote = prices.get(code) or ()
        last = quote[0] if len(quote) > 0 else None
        previous = quote[1] if len(quote) > 1 else None
        if (closing and (last is None or not math.isfinite(float(last)) or last <= 0)
                or opening and (previous is None or not math.isfinite(float(previous)) or previous <= 0)):
            result[code] = float('nan')  # 缺估值依据时不把未知盈亏显示为零
            continue
        result[code] = (closing * (last or 0) - opening * (previous or 0)
                        + sell_amount - buy_amount - fees)
    return result


# ---------------------------------------------------------------- 查询
def get_account() -> dict:
    """账户总览：总资产/可用资金/持仓市值/持仓盈亏/今日盈亏。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        c.execute('BEGIN')
        poss = pd.read_sql("SELECT * FROM broker_positions WHERE source!='satellite'", c)
        cash = float(c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()[0])
        available = max(0.0, _available_cash(c, 'manual'))
        fills = pd.read_sql("SELECT * FROM broker_fills WHERE date=? AND source!='satellite'", c, params=(_today(),))
    codes = set(poss['code']) | set(fills['code'])
    prices = _latest_prices(list(codes))
    day_pnl = sum(_day_pnl_by_code(poss, fills, prices).values())
    if poss.empty:
        return {"总资产": cash, "可用资金": available, "冻结资金": cash - available, "持仓市值": 0.0,
                "持仓盈亏": 0.0, "今日盈亏": day_pnl}
    poss["最新价"] = poss["code"].map(lambda x: (prices.get(x) or (None, None))[0])
    poss["昨收"] = poss["code"].map(lambda x: (prices.get(x) or (None, None))[1])
    mv = (poss["最新价"].fillna(poss["cost"]) * poss["shares"]).sum()
    pos_pnl = ((poss["最新价"].fillna(poss["cost"]) - poss["cost"]) * poss["shares"]).sum()
    return {"总资产": cash + mv, "可用资金": available, "冻结资金": cash - available, "持仓市值": mv,
            "持仓盈亏": pos_pnl, "今日盈亏": day_pnl}


def get_positions() -> pd.DataFrame:
    """持仓列表（含最新价/盈亏/今日盈亏/可卖数量）。"""
    _init_account()
    _settle_today()
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM broker_positions WHERE shares > 0", c)
        if not df.empty:
            df['sellable'] = [_available_shares(c, r.code, r.source) for r in df.itertuples()]
        fills = pd.read_sql("SELECT * FROM broker_fills WHERE date=?", c, params=(_today(),))
    if df.empty:
        return df
    prices = _latest_prices(list(df["code"]))
    df["最新价"] = df["code"].map(lambda x: (prices.get(x) or (None, None))[0])
    df["昨收"] = df["code"].map(lambda x: (prices.get(x) or (None, None))[1])
    df["市值"] = df["最新价"].fillna(df["cost"]) * df["shares"]
    df["持仓盈亏"] = (df["最新价"].fillna(df["cost"]) - df["cost"]) * df["shares"]
    day_values = {}
    for source, group in df.groupby('source'):
        values = _day_pnl_by_code(group, fills[fills['source'] == source], prices)
        day_values.update({(code, source): value for code, value in values.items()})
    df["今日盈亏"] = [day_values[(r.code, r.source)] for r in df.itertuples()]
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
        q = ("SELECT f.*,o.position_id,o.signal_source,o.strategy_name FROM broker_fills f "
             "LEFT JOIN broker_orders o ON o.id=f.order_id")
        if today_only:
            q += " WHERE f.date=?"
        return pd.read_sql(q + " ORDER BY f.id DESC", c,
                           params=(_today(),) if today_only else ())


def list_cashflows(limit: int = 200) -> pd.DataFrame:
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM broker_cashflows ORDER BY id DESC LIMIT ?", c, params=(limit,))
