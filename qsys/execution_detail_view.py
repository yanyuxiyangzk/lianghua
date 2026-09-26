"""Date-indexed execution report tables; all amounts come from the ledger."""
import pandas as pd

LABELS = {'signal_date': '信号日期', 'execution_date': '计划执行日期', 'date': '日期',
          'code': '股票代码', 'side': '方向', 'shares': '数量（股）', 'price': '成交价',
          'fee': '佣金', 'tax': '印花税', 'status': '状态', 'reason': '原因',
          'amount': '资金变动', 'balance': '现金余额'}
VALUES = {'buy': '买入', 'sell': '卖出', 'filled': '已成交', 'rejected': '未成交',
          'unknown_volume': '成交量未知，保守拒单（数据不完整）',
          'unknown_execution_constraint': '停牌或涨跌停数据未知，保守拒单',
          'insufficient_cash': '现金不足', 'limit_up': '涨停限制', 'limit_down': '跌停限制',
          'suspended_or_zero_volume': '停牌或零成交量',
          't_plus_one_or_insufficient_position': 'T+1或持仓不足'}


def daily_table(report):
    if not report.get('ok') or report.get('mode') != 'execution':
        raise ValueError('只接受成功的执行回放报告')
    ledger = report['execution']
    frame = pd.DataFrame(ledger['equity']).sort_values('date').reset_index(drop=True)
    previous = frame.equity.shift(1).fillna(float(ledger['initial_cash']))
    out = pd.DataFrame({'日期': frame.date, '现金': frame.cash, '持仓市值': frame.market_value,
                        '账户权益': frame.equity, '当日盈亏': frame.equity - previous,
                        '当日收益率（%）': (frame.equity / previous - 1) * 100,
                        '累计收益率（%）': (frame.equity / ledger['initial_cash'] - 1) * 100,
                        '净值': frame.equity / ledger['initial_cash'],
                        '持仓股票数': frame.positions.map(lambda p: sum(n > 0 for n in p.values())),
                        '持仓明细（代码:股数）': frame.positions.map(lambda p: '；'.join(f'{c}:{n}' for c,n in p.items() if n))})
    for key, title, rejected in [('fills','成交笔数',False), ('orders','拒单笔数',True)]:
        rows = pd.DataFrame(ledger.get(key, []))
        if not rows.empty and rejected:
            rows = rows[rows.status == 'rejected']
        counts = rows.groupby('date').size() if not rows.empty else pd.Series(dtype=int)
        out[title] = frame.date.map(counts).fillna(0).astype(int)
    fills = pd.DataFrame(ledger.get('fills', []))
    fees = fills.assign(cost=fills.fee+fills.tax).groupby('date').cost.sum() if not fills.empty else pd.Series(dtype=float)
    out['当日费用'] = frame.date.map(fees).fillna(0.)
    return out


def detail_table(rows):
    frame = pd.DataFrame(rows)
    for col in ('side', 'status', 'reason'):
        if col in frame:
            frame[col] = frame[col].map(lambda v: VALUES.get(v, v))
    return frame.rename(columns=LABELS)


def render():
    import streamlit as st
    import library
    from strategy_backtest import backtest_strategy

    st.subheader('按日期查看回测明细')
    st.caption('历史日频执行模拟 · 收盘决策，下一交易日开盘执行 · 初始资金20万元 · 最近约500个交易日（以实际数据为准）')
    with st.expander('补充历史停牌和涨跌停数据'):
        st.caption('iFinD独立历史字段，按股票和日期补抓；每批最多30只，不使用当前快照或前值填充。补充后需重新运行回测。')
        with st.form('execution_constraint_sync'):
            constraint_codes = st.text_input('股票代码（逗号分隔）', value='SH600664,SH603893,SZ001216')
            from datetime import date, timedelta
            c1, c2 = st.columns(2)
            begin = c1.date_input('补充开始日期', value=date.today()-timedelta(days=400))
            finish = c2.date_input('补充结束日期', value=date.today()-timedelta(days=1))
            sync_requested = st.form_submit_button('补充历史成交约束')
        if sync_requested:
            from execution_constraints import sync
            try:
                with st.spinner('正在补抓历史交易状态与涨跌停价…'):
                    sync_rows = sync([x.strip().upper() for x in constraint_codes.replace('，', ',').split(',') if x.strip()],
                                     begin.isoformat(), finish.isoformat())
                st.dataframe(sync_rows, hide_index=True, width='stretch')
                if any('error' in r or r.get('known', 0) < r.get('rows', 0) for r in sync_rows):
                    st.warning('部分字段仍缺失或抓取失败，请查看结果；不会用零值代替。')
                else:
                    st.success('返回记录已保存，请重新运行回测。日期全覆盖以回测中的缺失检查为准。')
            except Exception as exc:
                st.error(f'补抓失败：{exc}')
    from common import DATA_DIR
    daily_status = DATA_DIR / 'daily_repair_status.json'
    if daily_status.exists():
        with st.expander('基础日线缺失修复进度'):
            import json
            try:
                st.write(json.loads(daily_status.read_text()))
                st.caption('按本地交易日历检查历史缺日和无效OHLCV；停牌、无数据等未补齐项单独记录，不伪造价格。')
            except (ValueError, OSError):
                st.caption('暂无法读取日线补抓进度。')
    progress_file = DATA_DIR / 'constraint_worker_status.json'
    if progress_file.exists():
        with st.expander('全库成交约束补抓与自动增量进度'):
            import json
            try:
                progress = json.loads(progress_file.read_text())
                st.caption(f"状态：{progress.get('state')} · 更新时间：{progress.get('updated_at')} · 股票数：{progress.get('stocks')}")
                st.write(progress.get('tasks', {}))
                if progress.get('current'): st.write(progress['current'])
                if progress.get('failures'): st.dataframe(progress['failures'], hide_index=True, width='stretch')
                st.caption('任务按股票、年份、字段计数。自动跟随本地日线新增日期；失败退避重试，连续3次未补齐则阻塞。done不代表其他行情字段完整。')
            except (ValueError, OSError):
                st.caption('暂无法读取后台进度。')
    packs = library.list_strategies()
    names = [name for name, pack in packs.items() if pack.get('factors')]
    if not names:
        st.info('暂无含因子的策略包，请先在因子策略库配置策略。')
        return
    with st.form('execution_detail_form'):
        name = st.selectbox('回测策略', names)
        c1, c2 = st.columns(2)
        top_n = c1.number_input('选股数量', min_value=1, max_value=100, value=10, step=1)
        hold = c2.number_input('调仓间隔（交易日）', min_value=1, max_value=60, value=5, step=1)
        run = st.form_submit_button('运行回测并显示列表', type='primary')
    if run:
        st.session_state.pop('execution_detail_report', None)
        try:
            with st.spinner('正在计算历史回放，完成后显示真实账本明细…'):
                result = backtest_strategy(name, top_n=int(top_n), hold_days=int(hold), mode='execution')
            if result.get('ok'):
                st.session_state['execution_detail_report'] = result
            else:
                st.error(result.get('msg', '回测失败，未生成明细'))
                if result.get('factor_errors'):
                    st.dataframe(pd.DataFrame(result['factor_errors']).rename(columns={
                        'factor': '失败因子', 'kind': '因子类型', 'error_type': '异常类型', 'reason': '具体原因'}),
                        hide_index=True, width='stretch')
        except Exception as exc:
            st.error(f'回测失败，未生成明细：{exc}')
    report = st.session_state.get('execution_detail_report')
    if report is None:
        st.info('选择策略后运行回测；没有结果时不展示模拟填充数据。')
        return
    st.caption(f"当前显示已完成结果：{report['strategy']} · 股票池 {report['pool']} · {report['period']}")
    st.warning(report['execution_limitations'])
    if report.get('data_quality_status') == 'incomplete':
        st.error('数据不完整：部分委托因成交约束数据未知被拒绝。以下仅用于排查和保守模拟，不是完整数据下的策略绩效或验收通过结果。')
        with st.expander('查看行情缺失明细', expanded=True):
            st.dataframe(pd.DataFrame(report['data_issues']).rename(columns={
                'date': '日期', 'code': '股票代码', 'field': '缺失字段', 'reason': '处理原因'}),
                hide_index=True, width='stretch')
    missing = report['execution'].get('constraints_missing', [])
    if missing:
        st.caption('本报告缺少的成交约束字段：' + '、'.join(missing))
    st.caption('尚未提供执行基准、超额收益、持仓成本、逐笔盈亏和逐成交因子排名。本结果仅保存在当前会话，可下载留存。')
    frame = daily_table(report)
    start, end = st.select_slider('显示日期范围', options=frame['日期'].tolist(),
                                 value=(frame['日期'].iloc[0], frame['日期'].iloc[-1]))
    shown = frame[frame['日期'].between(start, end)]
    st.dataframe(shown, hide_index=True, use_container_width=True,
                 column_config={c: st.column_config.NumberColumn(c, format='%.4f' if c == '净值' else '%.2f')
                                for c in ['现金','持仓市值','账户权益','当日盈亏','当日收益率（%）','累计收益率（%）','净值','当日费用']})
    st.download_button('下载当前日期范围CSV', shown.to_csv(index=False).encode('utf-8-sig'),
                       file_name='backtest_daily.csv', mime='text/csv')
    import json
    st.download_button('下载完整执行报告JSON', json.dumps(report, ensure_ascii=False, allow_nan=False),
                       file_name='backtest_execution.json', mime='application/json')
    day = st.selectbox('查看某日记录', shown['日期'].tolist())
    equity = next(r for r in report['execution']['equity'] if r['date'] == day)
    sections = [('日终持仓', [dict(code=c, shares=n) for c,n in equity['positions'].items() if n]),
                ('当日收盘选股', [dict(signal_date=r['date'], execution_date=r['execution_date'], code=c)
                                for r in report['picks'] if r['date'] == day for c in r['picks']])]
    sections += [(label, [r for r in report['execution'][key] if r['date'] == day])
                 for label,key in [('委托及拒单','orders'),('实际成交','fills'),('现金流水','cashflows')]]
    for label, rows in sections:
        with st.expander(label, expanded=label == '实际成交'):
            if rows:
                st.dataframe(detail_table(rows), hide_index=True, use_container_width=True)
            else:
                st.caption('当日无此类记录。')
