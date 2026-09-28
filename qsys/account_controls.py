"""Account exposure controls and authorized automatic/manual simulated reductions.

Generating or refreshing a plan never places orders. Submission and its order links
share the broker transaction; retries cannot submit the same plan twice.
"""
import hashlib
import json
import math
import uuid
from contextlib import nullcontext
from datetime import datetime

import broker
import experience

SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_reduction_plans(
 id TEXT PRIMARY KEY, created_at TEXT, day TEXT, status TEXT, snapshot_hash TEXT,
 target REAL, payload TEXT, submitted_at TEXT);
CREATE TABLE IF NOT EXISTS account_reduction_items(
 plan_id TEXT, item_id INTEGER, code TEXT, source TEXT, position_id INTEGER,
 shares INTEGER, reference_price REAL, reason TEXT, status TEXT, message TEXT,
 order_id INTEGER, PRIMARY KEY(plan_id,item_id));
CREATE TABLE IF NOT EXISTS account_reduction_settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS account_reduction_events(
 id INTEGER PRIMARY KEY,created_at TEXT,kind TEXT,details TEXT);
'''


def _connection():
    c = broker._conn()
    c.executescript(SCHEMA)
    return c


def _enabled(c):
    row = c.execute("SELECT value FROM account_reduction_settings WHERE key='enabled'").fetchone()
    return bool(row and row[0]=='true')


def automatic_state():
    with _connection() as c:
        enabled = _enabled(c)
        row = c.execute("SELECT value FROM account_reduction_settings WHERE key='last_run'").fetchone()
    return dict(enabled=enabled, **(json.loads(row[0]) if row else {}))


def set_automatic(enabled, reason='页面操作'):
    """Persist automatic authority. Pausing also cancels its unfilled sell orders."""
    if not isinstance(enabled,bool): raise ValueError('自动模式必须是布尔值')
    cancelled = 0
    with _connection() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT OR REPLACE INTO account_reduction_settings VALUES ('enabled',?)",('true' if enabled else 'false',))
        if not enabled:
            orders = c.execute("SELECT o.id,p.payload FROM broker_orders o JOIN account_reduction_plans p "
                               "ON p.id=o.risk_plan_id WHERE o.side='sell' AND o.status='已报'").fetchall()
            for oid,payload in orders:
                if json.loads(payload).get('execution_mode') == 'automatic':
                    cancelled += c.execute("UPDATE broker_orders SET status='已撤',cancel_ts=? WHERE id=? AND status='已报'",(broker._now(),oid)).rowcount
            broker._reopen_cancelled_sales(c)
        c.execute('INSERT INTO account_reduction_events(created_at,kind,details) VALUES (?,?,?)',
                  (broker._now(),'enabled' if enabled else 'paused',json.dumps(dict(reason=reason,cancelled=cancelled),ensure_ascii=False)))
        c.execute("DELETE FROM account_reduction_settings WHERE key='last_attempt'")
        state = dict(time=broker._now(),status='enabled' if enabled else 'paused',
                     message='已启用，等待后台自动检查' if enabled else f'已暂停，撤销未成交自动卖单 {cancelled} 笔')
        c.execute("INSERT OR REPLACE INTO account_reduction_settings VALUES ('last_run',?)",(json.dumps(state,ensure_ascii=False),))
    return state


def snapshot(connection=None):
    with (broker._conn() if connection is None else nullcontext(connection)) as c:
        if connection is None:
            c.execute('BEGIN')
        holdings = c.execute("SELECT code,source,name,shares,sellable,last_buy_date FROM broker_positions "
                             "WHERE shares>0 AND source!='satellite' ORDER BY code,source").fetchall()
        orders = c.execute("SELECT id,code,side,shares,price,source FROM broker_orders WHERE status='已报' "
                           "AND source!='satellite' ORDER BY id").fetchall()
        lots = (c.execute("SELECT id,code,shares,buy_date,status FROM positions WHERE status IN ('open','closing') ORDER BY id").fetchall()
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='positions'").fetchone() else [])
        cash_row = c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()
    cash = float(cash_row[0]) if cash_row else float('nan')
    prices = broker._latest_prices(sorted({h[0] for h in holdings}))
    codes, missing, stale = {}, [], []
    for code, source, name, shares, sellable, bought in holdings:
        quote = prices.get(code) or ()
        price = quote[0] if quote else None
        if price is None or not math.isfinite(float(price)) or price <= 0:
            missing.append(code)
            continue
        if not broker._quote_fresh(code):
            stale.append(code)
        row = codes.setdefault(code, dict(code=code,name=name or code,shares=0,market_value=0.,pending_buy=0.,price=price))
        row['shares'] += shares
        row['market_value'] += shares*price
    for _, code, side, shares, price, _ in orders:
        if side == 'buy':
            if price is None or not math.isfinite(float(price)) or price <= 0 or not shares or shares < 0:
                missing.append(code)
                continue
            row = codes.setdefault(code, dict(code=code,name=code,shares=0,market_value=0.,pending_buy=0.,price=price))
            row['pending_buy'] += shares*price
    valid = not missing and math.isfinite(cash) and cash >= 0
    total = cash + sum(r['market_value'] for r in codes.values()) if valid else None
    valid = valid and total > 0
    target = experience.get_account_risk_config()['normal_target']
    risk_ready = False
    risk_reason = '当日风险评估缺失'
    try:
        state = json.loads(experience._RISK_FLAG.read_text())
        risk_reason = str(state.get('reason') or risk_reason)
        proposed = float(state['target_position_ratio'])
        risk_ready = (state.get('date') == broker._today() and math.isfinite(proposed)
                      and 0 <= proposed <= 1 and isinstance(state.get('halt'),bool)
                      and state.get('level') in ('normal','yellow','orange','red'))
        if risk_ready:
            target = min(target, proposed)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    for row in codes.values():
        row['weight'] = row['market_value']/total if valid else None
        row['limit_value'] = total*.15 if valid else None
        row['excess_value'] = max(0.,row['market_value']+row['pending_buy']-total*.15) if valid else None
    mv = sum(r['market_value'] for r in codes.values())
    pending = sum(r['pending_buy'] for r in codes.values())
    digest = hashlib.sha256(json.dumps([holdings,orders,lots,cash_row],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return dict(date=broker._today(),total=total,valid=valid,fresh=valid and not stale,
                missing=sorted(set(missing)),stale=sorted(set(stale)),risk_ready=risk_ready,risk_reason=risk_reason,
                target=target,position_ratio=mv/total if valid else None,
                total_excess=max(0.,mv+pending-total*target) if valid else None,
                holdings=holdings,lots=lots,orders=orders,rows=list(codes.values()),snapshot_hash=digest)


def refresh_risk(prefix='手动核验'):
    """Explicit risk refresh. May cancel prohibited buys; never sells or calls an LLM."""
    from scheduler import _apply_account_risk
    result = experience.portfolio_risk(use_live=True)
    if not isinstance(result,dict) or not result.get('ok'):
        reason = result.get('reason','风险计算结果无效') if isinstance(result,dict) else '风险计算结果无效'
        experience._write_risk_flag(broker._today(),True,reason,level='red')
        raise ValueError(reason)
    return _apply_account_risk(broker._today(),result,prefix=prefix,include_llm=False)


def reduction_amounts(snap):
    """Shared advisory/execution sizing, with room for fees and no implicit orders."""
    if not snap['valid']:
        raise ValueError('持仓估值缺失，无法生成可靠的降仓数量')
    needs = snap['total_excess'] > .01 or any(r['excess_value']>.01 for r in snap['rows'])
    fee_allowance = (sum(r['market_value'] for r in snap['rows'])*(broker.FEE_RATE+broker.TAX_RATE)
                     + broker.FEE_MIN*(len(snap['holdings'])+len(snap['lots'])))
    risk_total = max(0.,snap['total']-fee_allowance)
    amounts = {r['code']:max(0.,r['market_value']-risk_total*.15) if needs else 0. for r in snap['rows']}
    remaining = max(0.,sum(r['market_value'] for r in snap['rows'])-risk_total*snap['target']-sum(amounts.values())) if needs else 0.
    for row in sorted(snap['rows'],key=lambda r:r['market_value'],reverse=True):
        extra = min(max(0.,row['market_value']-amounts[row['code']]),remaining)
        amounts[row['code']] += extra
        remaining -= extra
    return amounts, fee_allowance


def generate_plan(*, automatic=False):
    """Persist reviewable quantities, including blocked quantities; never submit here."""
    with _connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if automatic and not _enabled(c): raise ValueError('自动降仓已暂停')
        snap = snapshot(c)
        snap['execution_mode'] = 'automatic' if automatic else 'manual'
        if not snap['valid']:
            raise ValueError('持仓估值缺失，无法生成可靠的降仓数量')
        if snap['orders']:
            raise ValueError('存在未完成委托，请先核实或撤销后重新生成降仓方案')
        by_code = {r['code']:r for r in snap['rows']}
        amounts, fee_allowance = reduction_amounts(snap)
        snap['fee_allowance'] = fee_allowance
        items = []
        for cd, amount in amounts.items():
            if amount <= .01:
                continue
            row = by_code[cd]
            needed = min(row['shares'],math.ceil(amount/row['price']/100)*100)
            reason = '单股超限' if row['excess_value'] > .01 else '账户总仓位超限'
            for code, source, name, held, sellable, bought in snap['holdings']:
                if code != cd or needed <= 0:
                    continue
                qty = min(needed,held)
                source_lots = [(None,held,bought,'open')]
                if source == 'ai':
                    source_lots = [(p[0],p[2],p[3],p[4]) for p in snap['lots'] if p[1] == cd]
                    if sum(p[1] for p in source_lots) != held:
                        source_lots = [(None,held,bought,'mismatch')]
                available = broker._available_shares(c,cd,source)
                to_allocate = qty
                for pid, lot_shares, buy_date, state in source_lots:
                    take = min(to_allocate,lot_shares)
                    if take <= 0:
                        continue
                    block = ''
                    if state != 'open': block = '策略持仓对账不一致或已有卖单，需核对'
                    elif source == 'ai' and (not buy_date or buy_date >= broker._today()):
                        block = 'T+1或买入日期缺失'
                    tradable = 0 if block else min(take,available)
                    if tradable:
                        items.append((cd,source,pid,tradable,row['price'],reason,'review',''))
                    if take > tradable:
                        items.append((cd,source,pid,take-tradable,row['price'],reason,'blocked',block or 'T+1或可卖股数不足'))
                    to_allocate -= take
                    available -= tradable
                needed -= qty
        identifier = uuid.uuid4().hex
        c.execute("UPDATE account_reduction_plans SET status='superseded' WHERE status='review'")
        c.execute('INSERT INTO account_reduction_plans VALUES (?,?,?,?,?,?,?,NULL)',
                  (identifier,broker._now(),broker._today(),'review',snap['snapshot_hash'],snap['target'],json.dumps(snap,ensure_ascii=False)))
        c.executemany('INSERT INTO account_reduction_items VALUES (?,?,?,?,?,?,?,?,?,?,NULL)',
                      [(identifier,i,*item) for i,item in enumerate(items)])
    return identifier


def submit_plan(identifier, *, confirmed=False, automatic=False):
    if not automatic and confirmed is not True:
        raise ValueError('必须确认已审核方案及模拟卖出数量')
    broker._settle_today()
    with _connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if automatic and not _enabled(c): raise ValueError('自动降仓已暂停')
        plan = c.execute('SELECT day,status,snapshot_hash,target,payload FROM account_reduction_plans WHERE id=?',(identifier,)).fetchone()
        if not plan: raise ValueError('降仓方案不存在')
        if automatic and json.loads(plan[4]).get('execution_mode') != 'automatic':
            raise ValueError('自动执行必须重新生成并校验方案，不能直接执行旧审核方案')
        if plan[1] == 'submitted': return '该方案已提交，请查看执行状态；未重复下单'
        if plan[1] != 'review' or plan[0] != broker._today(): raise ValueError('方案已失效，请重新生成并审核')
        snap = snapshot(c)
        if snap['snapshot_hash'] != plan[2]: raise ValueError('账户持仓、资金或委托已变化，请重新生成方案')
        if not snap['fresh'] or not snap['risk_ready']: raise ValueError('当日行情或风险状态未就绪，暂不提交')
        if abs(snap['target']-plan[3]) > 1e-8: raise ValueError('账户目标仓位已变化，请重新审核方案')
        if not broker._market_open(): raise ValueError('非确认交易时段，暂不提交')
        items = c.execute("SELECT item_id,code,source,position_id,shares,reference_price FROM account_reduction_items "
                          "WHERE plan_id=? AND status='review' ORDER BY item_id",(identifier,)).fetchall()
        quotes = broker._latest_prices([x[1] for x in items])
        if any(not quotes.get(cd) or abs(quotes[cd][0]/reference-1) > .02 for _,cd,_,_,_,reference in items):
            raise ValueError('行情较审核参考价变动超过2%，请重新生成方案')
        for item_id, code, source, pid, shares, reference in items:
            before = c.execute('SELECT COALESCE(MAX(id),0) FROM broker_orders').fetchone()[0]
            if pid is not None:
                lot = c.execute('SELECT status,shares,buy_date FROM positions WHERE id=?',(pid,)).fetchone()
                if not lot or lot[0] != 'open' or lot[1] < shares or lot[2] >= broker._today():
                    c.execute("UPDATE account_reduction_items SET status='failed',message='策略持仓变化或T+1限制' WHERE plan_id=? AND item_id=?",(identifier,item_id))
                    continue
            msg = broker.place_order(code,'sell',None,shares,source=source,_connection=c,
                                     _position_id=pid,_decision_reason=('自动账户降仓：' if automatic else '账户降仓方案：')+identifier)
            order = c.execute('SELECT id,status FROM broker_orders WHERE id>? ORDER BY id DESC LIMIT 1',(before,)).fetchone()
            if order:
                c.execute('UPDATE broker_orders SET risk_plan_id=? WHERE id=?',(identifier,order[0]))
                if pid is not None:
                    c.execute('UPDATE positions SET sell_reason=?,sell_order_id=? WHERE id=?',('账户降仓',order[0],pid))
                    if order[1] == '已报': c.execute("UPDATE positions SET status='closing' WHERE id=?",(pid,))
            c.execute('UPDATE account_reduction_items SET order_id=?,status=?,message=? WHERE plan_id=? AND item_id=?',
                      (order[0] if order else None,'submitted' if order else 'failed',msg,identifier,item_id))
        c.execute("UPDATE account_reduction_plans SET status='submitted',submitted_at=? WHERE id=?",(broker._now(),identifier))
    return '方案已提交核验，请查看逐笔状态；提交不等于成交或风险解除'


def latest_plan():
    with _connection() as c:
        row = c.execute('SELECT id,day,status,payload FROM account_reduction_plans ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        if not row: return {}
        enabled = _enabled(c)
        items = c.execute('SELECT i.item_id,i.code,i.source,i.position_id,i.shares,i.reference_price,i.reason,i.status,i.message,i.order_id,o.status,'
                          'COALESCE((SELECT SUM(f.shares) FROM broker_fills f WHERE f.order_id=i.order_id),0) '
                          'FROM account_reduction_items i LEFT JOIN broker_orders o ON o.id=i.order_id WHERE i.plan_id=? ORDER BY i.item_id',(row[0],)).fetchall()
    payload = json.loads(row[3])
    is_auto = payload.get('execution_mode') == 'automatic'
    result=[]
    for iid,cd,src,pid,qty,price,reason,state,message,oid,order_state,filled in items:
        status = ('已成交' if filled >= qty else '部分成交（已撤销）' if filled and order_state=='已撤' else
                  '部分成交' if filled else '等待成交' if order_state=='已报' else '已撤销' if order_state=='已撤' else
                  '成交记录待核对' if order_state=='已成' else '委托记录缺失' if oid else
                  '受限，未提交' if state=='blocked' else '提交失败' if state=='failed' else
                  '等待自动提交' if is_auto else '等待自动重算' if enabled else '待确认')
        result.append(dict(code=cd,source=src,position_id=pid,shares=qty,reference_price=price,
                           reason=reason,status=status,message=message,order_id=oid,filled_shares=filled))
    status = ('已被新方案替代' if row[2]=='superseded' else '方案已过期' if row[1]!=broker._today() and row[2]=='review' else
              '无需减仓' if not result else ('计划数量已成交，系统自动复核仓位' if is_auto else '计划数量已成交，须复核剩余风险') if all(x['filled_shares']>=x['shares'] for x in result) else
              '执行中' if any(x['status'] in ('等待成交','部分成交') for x in result) else
              '部分完成，仍有未完成项' if any(x['filled_shares'] for x in result) else
              '未完成，自动重试中' if row[2]=='submitted' and is_auto and enabled else
              '未完成，请查看原因' if row[2]=='submitted' else
              '自动降仓已暂停' if is_auto and not enabled else
              '受限，等待自动重试' if is_auto and all(x['status']=='受限，未提交' for x in result) else
              '等待自动校验执行' if enabled else '待审核，尚未下单')
    return dict(id=row[0],date=row[1],state=row[2],status=status,snapshot=payload,items=result)


def recent_plans(limit=10):
    with _connection() as c:
        rows = c.execute('SELECT p.id,p.created_at,p.status,p.payload,'
                         '(SELECT COALESCE(SUM(i.shares),0) FROM account_reduction_items i WHERE i.plan_id=p.id),'
                         '(SELECT COALESCE(SUM(f.shares),0) FROM broker_fills f JOIN broker_orders o ON o.id=f.order_id WHERE o.risk_plan_id=p.id) '
                         'FROM account_reduction_plans p ORDER BY p.created_at DESC,p.rowid DESC LIMIT ?', (int(limit),)).fetchall()
    return [dict(方案=r[0],创建时间=r[1],模式='自动' if json.loads(r[3]).get('execution_mode')=='automatic' else '手动',
                 提交状态={'review':'尚未提交','submitted':'已提交','superseded':'已被新方案替代'}.get(r[2],r[2]),
                 计划股数=r[4],实际成交股数=r[5]) for r in rows]


def _automatic_result(status, message, plan_id=None):
    result = dict(time=broker._now(),status=status,message=message,plan_id=plan_id)
    with _connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if not _enabled(c):
            result.update(status='paused',message='自动降仓已暂停')
        c.execute("INSERT OR REPLACE INTO account_reduction_settings VALUES ('last_run',?)",(json.dumps(result,ensure_ascii=False),))
    return result


def _automatic_locked():
    if not automatic_state()['enabled']:
        return _automatic_result('paused','自动降仓已暂停')
    if not broker._market_open():
        return _automatic_result('waiting_market','等待已确认交易时段')
    broker._settle_today()
    broker.expire_day_orders()
    refresh_risk(prefix='自动核验')  # 每轮以当前行情重评，不复用盘前错误或旧目标；不调用模型
    with _connection() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM broker_orders WHERE side='sell' AND status='已报' AND risk_plan_id IS NOT NULL")]
    if ids:
        broker.fill_pending_orders(order_ids=ids)
    snap = snapshot()
    if not snap['fresh'] or not snap['risk_ready']:
        return _automatic_result('waiting_data','等待有效当日行情与风险评估')
    if snap['orders']:
        return _automatic_result('waiting_orders','已有未完成委托，等待成交或到期后再核算，避免重复减仓')
    if snap['total_excess'] <= .01 and all(r['excess_value'] <= .01 for r in snap['rows']):
        return _automatic_result('within_limits','当前仓位符合限制，无需继续降仓')
    amounts,_ = reduction_amounts(snap)
    quantities = [(r['code'],min(r['shares'],math.ceil(amounts[r['code']]/r['price']/100)*100)) for r in snap['rows']]
    fingerprint = hashlib.sha256(json.dumps([snap['date'],snap['snapshot_hash'],snap['target'],quantities],sort_keys=True).encode()).hexdigest()
    with _connection() as c:
        row = c.execute("SELECT value FROM account_reduction_settings WHERE key='last_attempt'").fetchone()
        last = json.loads(row[0]) if row else {}
        if last.get('fingerprint') == fingerprint:
            elapsed = (datetime.fromisoformat(broker._now())-datetime.fromisoformat(last['time'])).total_seconds()
            if last.get('blocked') or elapsed < 300:
                return _automatic_result('waiting_retry','受限或失败后等待条件变化；相同失败最多每5分钟重试一次',last.get('plan_id'))
        attempt = dict(fingerprint=fingerprint,time=broker._now())
        c.execute("INSERT OR REPLACE INTO account_reduction_settings VALUES ('last_attempt',?)",(json.dumps(attempt),))
    identifier = generate_plan(automatic=True)
    plan = latest_plan()
    if plan['id'] != identifier:
        raise ValueError('方案已被更新，下一轮重新计算')
    if not any(item['status']=='等待自动提交' for item in plan['items']):
        attempt.update(blocked=True,plan_id=identifier)
        with _connection() as c:
            c.execute("INSERT OR REPLACE INTO account_reduction_settings VALUES ('last_attempt',?)",(json.dumps(attempt),))
        return _automatic_result('blocked','T+1、可卖数量或对账限制，条件变化后自动重算',identifier)
    submit_plan(identifier,automatic=True)
    completed = latest_plan()
    filled = sum(x['filled_shares'] for x in completed['items'])
    return _automatic_result('processed',f"自动处理完成：{completed['status']}；本方案已成交 {filled} 股",identifier)


def run_automatic():
    """Periodic autonomous execution; process lock + broker transaction prevent duplicates."""
    import fcntl
    path = broker.DB_PATH.with_suffix('.auto-reduction.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='busy',message='另一轮自动降仓正在处理')
        try:
            return _automatic_locked()
        except Exception as exc:
            return _automatic_result('retry',f'本轮未完成，将自动重试：{type(exc).__name__}: {exc}')
        finally:
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
