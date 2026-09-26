"""Dated iFinD execution constraints, isolated from OHLCV caches.

Only exact stock/date observations are joined. Missing/unknown observations
remain null; no previous-value fill, percentage estimates or live snapshots.
"""
import argparse
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd
from common import DATA_DIR

DB = DATA_DIR / 'execution_constraints.db'
INDICATORS = {'suspended': 'ths_trading_status_stock',
              'limit_up': 'ths_max_up_stock', 'limit_down': 'ths_max_down_stock'}


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS observations(
        source TEXT,code TEXT,date TEXT,field TEXT,value REAL,raw TEXT,fetched_at TEXT,
        PRIMARY KEY(source,code,date,field))''')
    return c


def normalize(field, raw):
    if field == 'suspended':
        # Restrictive whitelist: unknown status is NOT tradable by default.
        text = str(raw).strip()
        if text in ('交易', '停牌'):
            return {'交易': 0., '停牌': 1.}[text]
        # Observed provider wording for continuous, full-session suspension.
        # Do not treat intraday suspension or resumption notices as full-day suspension.
        import re
        if re.fullmatch(r'.*，停牌1天', text):
            return 1.
        if re.fullmatch(r'.*，停牌自\d{4}-\d{2}-\d{2}起连续停牌', text):
            return 1.
        return None
    try:
        value = float(raw)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def sync(codes, start, end, fields=None):
    """Explicit bounded backfill. Three non-filled date-series requests per code."""
    import datasource
    codes = list(dict.fromkeys(codes))
    if not codes or len(codes) > 30 or pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError('每批1至30只股票，日期范围必须有效')
    fields = list(INDICATORS) if fields is None else list(fields)
    if not fields or any(f not in INDICATORS for f in fields):
        raise ValueError('未知成交约束字段')
    results = []
    for code in codes:
        for field in fields:
            indicator = INDICATORS[field]
            try:
                df, _, err = datasource.ths_date_serial(code, indicator, start, end, fill='Original')
                if err != 0 or df is None or df.empty:
                    raise ValueError(f'接口返回错误或空结果: {err}')
                df = df.copy()
                df.columns = [str(c).lower() for c in df.columns]
                if not {'time', 'thscode', indicator}.issubset(df.columns):
                    raise ValueError(f'返回缺少日期、代码或字段{indicator}')
                expected = datasource._to_ths_code(code)
                if not df.thscode.eq(expected).all():
                    raise ValueError('供应商返回股票代码不匹配')
                df['date'] = pd.to_datetime(df.time, errors='raise').dt.strftime('%Y-%m-%d')
                if df.date.duplicated().any() or not df.date.between(start, end).all():
                    raise ValueError('供应商返回日期重复或越界')
                now = datetime.now().isoformat(timespec='seconds')
                rows = [('ths_ifind', code, row.date, field, normalize(field, getattr(row, indicator)),
                         str(getattr(row, indicator)), now) for row in df.itertuples()]
                with closing(connect()) as c, c:
                    c.executemany('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)', rows)
                results.append(dict(code=code, field=field, rows=len(rows), known=sum(r[4] is not None for r in rows)))
            except Exception as exc:
                results.append(dict(code=code, field=field, error=str(exc)))
    return results


def attach(prices, source):
    """Add constraints to a (date,code) frame without changing prices or rows."""
    result = prices.copy()
    if source != 'ths_ifind':
        return result
    keys = pd.DataFrame({'date': pd.to_datetime(prices.index.get_level_values('date')).strftime('%Y-%m-%d'),
                         'code': prices.index.get_level_values('code')})
    records = pd.DataFrame(columns=['date', 'code', 'field', 'value'])
    if DB.exists() and not keys.empty:
        with closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            codes = keys.code.unique().tolist()
            chunks = []
            for offset in range(0, len(codes), 400):
                batch = codes[offset:offset+400]
                chunks.append(pd.read_sql('SELECT date,code,field,value FROM observations WHERE source=? AND date BETWEEN ? AND ? AND code IN (' + ','.join('?' for _ in batch) + ')',
                                          c, params=['ths_ifind', keys.date.min(), keys.date.max()] + batch))
            records = pd.concat(chunks, ignore_index=True)
    for field in INDICATORS:
        subset = records[records.field == field]
        mapping = {(r.date,r.code): r.value for r in subset.itertuples()}
        values = pd.Series([mapping.get((r.date,r.code),float('nan')) for r in keys.itertuples()], index=result.index, dtype=float)
        if field in result:
            result[field] = result[field].where(result[field].notna(), values)
        else:
            result[field] = values
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='补充历史停牌与涨跌停价（每批最多30股）')
    p.add_argument('--codes', required=True)
    p.add_argument('--start', required=True)
    p.add_argument('--end', required=True)
    args = p.parse_args()
    print(json.dumps(sync(args.codes.split(','),args.start,args.end),ensure_ascii=False,indent=2))


def coverage(code, dates):
    """Check the exact backtest dates without substituting live quotes."""
    dates=sorted(set(str(d)[:10] for d in dates))
    prices=pd.DataFrame({'date':dates,'code':code}).set_index(['date','code'])
    attached=attach(prices,'ths_ifind')
    missing={}
    for field in INDICATORS:
        values=pd.to_numeric(attached[field],errors='coerce')
        valid=values.isin([0.,1.]) if field=='suspended' else values.gt(0) & values.map(lambda x: math.isfinite(x) if pd.notna(x) else False)
        missing[field]=[str(d) for d,ok in zip(dates,valid) if not ok]
    return dict(code=code,expected_days=len(dates),missing=missing,
                missing_fields=sum(len(v) for v in missing.values()),
                complete=bool(dates) and not any(missing.values()))


def repair_missing(code, dates, progress=None):
    """Bounded per-year, per-field requests; verify actual coverage after fetch."""
    before=coverage(code,dates);results=[]
    tasks=[]
    for field,missing in before['missing'].items():
        for year in sorted({d[:4] for d in missing}):
            part=[d for d in missing if d.startswith(year)]
            tasks.append((field,part[0],part[-1]))
    for index,(field,start,end) in enumerate(tasks):
        if progress:progress(5+int(85*index/max(1,len(tasks))),f'补齐 {field}：{start}～{end} ({index+1}/{len(tasks)})')
        import subprocess,sys
        script='import execution_constraints as e,json,sys; print(json.dumps(e.sync([sys.argv[1]],sys.argv[2],sys.argv[3],fields=[sys.argv[4]]),ensure_ascii=False))'
        result=subprocess.run([sys.executable,'-c',script,code,start,end,field],capture_output=True,text=True,timeout=90)
        if result.returncode:raise ValueError(f'{field}补抓进程失败，退出码{result.returncode}')
        results.extend(json.loads(result.stdout.strip().splitlines()[-1]))
    after=coverage(code,dates)
    return dict(before=before,after=after,requests=results)
