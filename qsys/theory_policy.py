"""Bounded research-only theory attribution and discovery ledger."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from common import DATA_DIR

CATALOG = {
    'momentum': ('动量', ('动量', 'momentum')),
    'mean_reversion': ('均值回归', ('均值回归', 'mean_reversion')),
    'trend': ('趋势', ('趋势', 'trend')),
    'value': ('价值', ('价值', 'value')),
    'quality': ('质量', ('质量', 'quality')),
    'low_volatility': ('低波动', ('低波动', 'low_volatility')),
}
DB = DATA_DIR / 'theory_research.db'

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS associations(fingerprint TEXT PRIMARY KEY, payload TEXT);
    CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY, calls INTEGER, reserved INTEGER);
    CREATE TABLE IF NOT EXISTS discoveries(week TEXT PRIMARY KEY, data_hash TEXT, pattern_hash TEXT);
    ''')
    return c

class BudgetExceeded(RuntimeError):
    pass

class Budget:
    """UTF-8 byte upper-bound reservation, including output; failed calls consume budget."""
    def __init__(self, key, calls=1, tokens=12000, input_chars=6000):
        self.key, self.calls, self.tokens, self.input_chars = key, calls, tokens, input_chars

    def chat(self, system, user, max_tokens, label):
        size = len(system) + len(user)
        reserve = len((system + user).encode('utf-8')) + 512 + max_tokens
        with closing(connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO rounds VALUES (?,0,0)', (self.key,))
            calls, tokens = c.execute('SELECT calls,reserved FROM rounds WHERE id=?', (self.key,)).fetchone()
            if size > self.input_chars or calls >= self.calls or tokens + reserve > self.tokens:
                raise BudgetExceeded('理论研究调用或输入/token预留预算不足')
            c.execute('UPDATE rounds SET calls=calls+1,reserved=reserved+? WHERE id=?', (reserve,self.key))
        from llmutil import llm_chat
        return llm_chat(system, user, max_tokens=max_tokens, label=label, max_retries=0)

def associate(factor):
    fields = ('name','kind','code','factor_type','theory_id','theory_family','hypothesis_id','regime_scope','family')
    fingerprint = digest({k:factor.get(k) for k in fields})
    result = {'theory_id': None, 'theory_family': 'unclassified', 'association_source': 'unclassified',
              'association_fingerprint': fingerprint}
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT payload FROM associations WHERE fingerprint=?', (fingerprint,)).fetchone()
        if row:
            return json.loads(row[0])
        # Claim before any network call; interrupted attempts remain unclassified.
        c.execute('INSERT INTO associations VALUES (?,?)', (fingerprint,json.dumps(result)))
    if factor.get('theory_id'):
        result.update(theory_id=factor['theory_id'], theory_family=factor.get('theory_family') or 'unclassified', association_source='supplied')
    else:
        # Explicit mechanism labels only; do not infer economics from arbitrary code substrings.
        label = str(factor.get('theory_family') or factor.get('family') or '').strip()
        matches = [key for key, (name, aliases) in CATALOG.items() if label in aliases]
        if len(matches) == 1:
            key = matches[0]
            result.update(theory_id=key, theory_family=CATALOG[key][0], association_source='rule')
        else:
            try:
                day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
                prompt = json.dumps({k:factor.get(k) for k in ('name','code','factor_type')}, ensure_ascii=False)
                raw = Budget('association:'+day, calls=10, tokens=20000, input_chars=4000).chat(
                    '仅辅助分类，不批准交易。返回JSON: {"theory_id":固定目录编号或null}。目录:'+json.dumps(CATALOG,ensure_ascii=False), prompt, 200, 'theory_association')
                key = json.loads(raw or '{}').get('theory_id')
                if key in CATALOG:
                    result.update(theory_id=key, theory_family=CATALOG[key][0], association_source='llm_suggestion')
            except Exception:
                pass
    result['association_fingerprint'] = fingerprint
    with closing(connect()) as c, c:
        c.execute('UPDATE associations SET payload=? WHERE fingerprint=?', (json.dumps(result,ensure_ascii=False),fingerprint))
    return result

def claim_discovery(data_hash, pattern_hash, now=None):
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    year, week, _ = now.isocalendar()
    key = f'{year}-W{week:02d}'
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM discoveries WHERE week=?', (key,)).fetchone():
            return None, '本周已运行理论发现（含失败尝试）'
        if c.execute('SELECT 1 FROM discoveries WHERE data_hash=? OR pattern_hash=?', (data_hash,pattern_hash)).fetchone():
            return None, '无新增有效数据或模式'
        c.execute('INSERT INTO discoveries VALUES (?,?,?)', (key,data_hash,pattern_hash))
    return key, ''


def research_theory_id(sexpr):
    return 'research_' + hashlib.sha256(sexpr.encode()).hexdigest()[:16]


def inherit_strategy_theory(pack, registry):
    """Snapshot factor provenance; scope is intersection, never a union."""
    factors = []
    scopes = {'bull', 'bear', 'sideways', 'transition'}
    families, evidence = set(), set()
    for item in pack.get('factors', []):
        meta = registry.get(item['name'], {})
        snapshot = dict(item)
        for key in ('theory_id', 'hypothesis_id', 'theory_family', 'regime_scope', 'evidence_type', 'factor_type'):
            snapshot[key] = meta.get(key)
        factors.append(snapshot)
        family = meta.get('theory_family')
        if family:
            families.add(family)
        evidence.add(meta.get('evidence_type') or meta.get('factor_type') or 'unclassified')
        scope = meta.get('regime_scope')
        allowed = {s.strip() for s in scope.split(',')} if isinstance(scope, str) else set(scope or [])
        scopes &= {'bull','bear','sideways','transition'} if 'all' in allowed else allowed
    ids = {f.get('theory_id') for f in factors}
    coherent = bool(factors) and len(ids) == 1 and bool(next(iter(ids))) and bool(scopes)
    tid = next(iter(ids)) if coherent else None
    event = any(str(f.get('name','')).startswith('ev_') for f in factors)
    return {**pack, 'factors': factors, 'theory_id': tid,
            'theory_name': CATALOG[tid][0] if tid in CATALOG else tid,
            'theory_family': ','.join(sorted(families)) or 'unclassified',
            'regime_scope': ','.join(sorted(scopes)), 'evidence_type': ','.join(sorted(evidence)),
            'risk_class': ('event' if event else 'stable') if coherent else 'unclassified',
            'account_scope': ('satellite' if event else 'main') if coherent else 'none'}
