"""Automatic account reductions, with optional manual fallback and execution history."""
import pandas as pd
import streamlit as st
import account_controls as controls


def render():
    with st.expander(
        '账户超限检查与降仓执行', expanded=False,
        key='account_controls_panel', on_change='rerun',
    ):
        try:
            mode = controls.automatic_state()
            if mode['enabled']:
                st.success('自动降仓已启用：后台每分钟核验，满足条件后自动提交模拟卖单，无需逐笔审核。')
                st.caption(f"最近状态更新：{mode.get('time','尚未运行')} · {mode.get('message','等待后台检查')}")
                if st.button('暂停自动降仓（撤销未成交自动卖单）',key='account_auto_pause'):
                    controls.set_automatic(False)
                    st.rerun()
            else:
                st.info('自动降仓已暂停；下方仍可使用手动方案。')
                if st.button('启用自动降仓（无需审核）',key='account_auto_enable'):
                    controls.set_automatic(True)
                    st.rerun()
            snap = controls.snapshot()
            if not snap['valid']:
                st.error('持仓估值不完整，不能确认仓位是否合规。新增买入将被风控拦截。')
                return
            over = [r for r in snap['rows'] if r['excess_value'] > .01]
            if over or snap['total_excess'] > .01:
                st.warning(f"存在仓位超限：账户股票仓位 {snap['position_ratio']:.2%}，目标上限 {snap['target']:.0%}；"
                           '存量单股超限期间禁止全部新增买入。卖出仍按交易规则核验。')
            else:
                st.success('当前估算未发现单股或账户总仓位超限。')
            if not snap['fresh']:
                st.info('以下按最后可用行情估算；后台等待有效行情，恢复后自动重试。' if mode['enabled'] else
                        '以下按最后可用行情估算；行情未就绪时只能审核方案，不能提交。')
            if not snap['risk_ready']:
                st.info(f"当日风险目标尚未就绪：{snap['risk_reason']}。暂按正常仓位上限展示；" +
                        ('后台会自动重新评估。' if mode['enabled'] else '提交前必须重新校验风险目标。'))
            if not mode['enabled'] and st.button('更新风险评估（可能撤销违规买单，不卖出持仓）',key='account_risk_refresh',disabled=not snap['fresh']):
                st.info(controls.refresh_risk())
                st.rerun()
            st.dataframe(pd.DataFrame([{'代码':r['code'],'名称':r['name'],'持仓市值':round(r['market_value'],2),
                '持仓权重':f"{r['weight']:.2%}",'单股上限':'15%','待成交买入额':round(r['pending_buy'],2),
                '超限金额（含买单）':round(r['excess_value'],2)} for r in snap['rows']]),hide_index=True,width='stretch')
            if not mode['enabled'] and st.button('生成降仓方案（不下单）',key='account_plan_generate'):
                controls.generate_plan()
                st.success('已保存待审核方案，尚未下单。')
            plan = controls.latest_plan()
            if not plan:
                return
            st.markdown(f"**方案状态：{plan['status']}**")
            st.caption(f"方案 {plan['id']} · {plan['date']} · 只关联本方案的委托与成交，不把其他卖出算成本方案完成。")
            st.caption('数量按整手向上取整（不足整手的剩余持仓可全额卖出），并预留保守费用余量；最终以成交后仓位复核为准。')
            if plan['items']:
                table = pd.DataFrame(plan['items']).rename(columns={'code':'代码','source':'账户来源','position_id':'持仓批次',
                    'shares':'计划卖出股数','reference_price':'审核参考价','reason':'原因','status':'执行状态',
                    'message':'处理说明','order_id':'委托号','filled_shares':'已成交股数'})
                st.dataframe(table,hide_index=True,width='stretch')
            if not mode['enabled'] and plan['state']=='review' and plan['date']==snap['date'] and plan['items']:
                st.caption('仅提交未受限的明细。价格较参考价变动超过 2%、账户变化或方案跨日时，需重新生成并审核。')
                approved = st.checkbox('我已审核上述数量，确认提交模拟卖出委托',key='approve_'+plan['id'])
                ready = snap['fresh'] and snap['risk_ready'] and any(x['status']=='待确认' for x in plan['items'])
                if st.button('确认提交模拟降仓',key='submit_'+plan['id'],disabled=not (approved and ready)):
                    st.info(controls.submit_plan(plan['id'],confirmed=True))
                    st.rerun()
            if st.button('刷新执行状态',key='account_plan_refresh'):
                st.rerun()
            with st.expander('最近降仓记录'):
                st.dataframe(pd.DataFrame(controls.recent_plans()),hide_index=True,width='stretch')
        except Exception as exc:
            st.error(f'账户风险检查未完成：{exc}')
