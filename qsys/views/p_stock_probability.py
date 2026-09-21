"""📐 单股票概率模型：单票历史相似状态、贝叶斯收缩、时间顺序样本外验证。"""
import pandas as pd
import streamlit as st

import stock_probability as sp


def _db_code(raw: str) -> str:
    s = (raw or "").strip().upper()
    if "." in s:
        number, market = s.split(".", 1)
        return f"{market}{number}"
    if s.startswith(("SH", "SZ", "BJ")) and len(s) == 8:
        return s
    if len(s) == 6:
        market = "SH" if s.startswith("6") else "BJ" if s.startswith(("4", "8", "92")) else "SZ"
        return market + s
    return s

st.set_page_config(page_title="单股票概率模型", layout="wide")
st.title("📐 单股票概率模型")
st.caption("仅使用该股票自身历史数据；模型结果独立落库，删除原始行情后仍可查看。概率是历史条件频率，不是收益保证。")

raw_code = st.text_input("股票代码", placeholder="例如 SZ001216、001216.SZ 或 001216")
code = _db_code(raw_code)
valid = bool(code and len(code) == 8 and code[2:].isdigit())
if not valid:
    st.info("请输入有效股票代码。")
else:
    c1, c2 = st.columns([1, 3])
    build = c1.button("▶ 构建/更新模型", type="primary", use_container_width=True)
    c2.caption("模型使用日线状态构建；建议至少准备1年，2年以上更稳健。")
    if build:
        try:
            with st.spinner("正在构建状态、历史标签和滚动样本外验证…"):
                sp.build_model(code)
            st.success("模型构建完成。")
            st.rerun()
        except Exception as exc:
            st.error(f"模型构建失败：{exc}")

    model = sp.load_latest(code)
    if not model:
        st.info("尚无模型结果。请先在“单股票历史数据”抓取日线，再点击构建模型。")
    else:
        a, b, c, d = st.columns(4)
        a.metric("模型日期", model["asof_date"])
        b.metric("相似历史样本", model["sample_count"])
        c.metric("样本外验证次数", model["oos_count"])
        d.metric("证据等级", "充足" if model.get("evidence") == "sufficient" else "有限")
        st.caption(f"训练区间：{model['train_start']} 至 {model['train_end']} · "
                   f"匹配方式：{model.get('match_method') or '-'} "
                   f"({model.get('selected_scheme') or '-'}) · 版本：{model['model_version']}")

        rows = []
        labels = {"up_1d": "未来1日上涨", "up_3d": "未来3日上涨",
                  "up_5d": "未来5日上涨", "up_10d": "未来10日上涨",
                  "up_3pct_5d": "未来5日先上涨3%", "down_3pct_5d": "未来5日先下跌3%"}
        for key, label in labels.items():
            p = model["predictions"].get(key, {})
            rows.append({"事件": label, "收缩后概率": p.get("shrunk"),
                         "原始概率": p.get("raw"), "95%下限": p.get("low"),
                         "95%上限": p.get("high"), "有效样本": p.get("n")})
        prob_df = pd.DataFrame(rows)
        st.subheader("条件概率")
        st.dataframe(prob_df, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(c, format="%.1%%")
                                    for c in ["收缩后概率", "原始概率", "95%下限", "95%上限"]})

        st.subheader("当前状态")
        state = model.get("state") or {}
        state_labels = {"trend_state": "20日趋势", "momentum_state": "5日动量",
                        "volume_state": "成交量", "vol_state": "波动率",
                        "ret_5": "5日收益", "ret_20": "20日收益",
                        "vol_20": "20日年化波动", "volume_ratio": "量比",
                        "atr_pct": "ATR占价格"}
        st.dataframe(pd.DataFrame([{"指标": state_labels.get(k, k), "值": v}
                                   for k, v in state.items()]), hide_index=True, width="stretch")

        st.subheader("样本外可信度")
        oos = model.get("oos") or {}
        o1, o2, o3 = st.columns(3)
        o1.metric("5日方向准确率", f"{float(oos.get('accuracy') or 0):.1%}")
        o2.metric("Brier分数", f"{float(oos.get('brier') or 0):.3f}",
                  help="越低越好；二分类无信息基准约为0.25。")
        o3.metric("概率校准误差", f"{float(oos.get('calibration_error') or 0):.1%}",
                  help="预测概率与实际频率的加权偏差，越低越好。")
        if model.get("evidence") != "sufficient":
            st.warning("当前相似样本或样本外验证次数不足，概率只作探索参考，不应进入自动交易。")

        candidates = pd.DataFrame(model.get("model_candidates") or [])
        if not candidates.empty:
            st.subheader("状态模型选择")
            st.caption("严格、平衡、宽松三种匹配只按滚动样本外 Brier 与校准误差选择，不按当前预测结果挑选。")
            st.dataframe(candidates, hide_index=True, width="stretch")

        st.subheader("因子 / 策略组合接口")
        st.caption("当前为影子模式：策略包选股后计算概率修正建议，但不改变正式名单。只有证据充足、"
                   "模型日期新鲜且样本≥50时才会产生有限修正；单票修正上限为截面分数标准差的15%。")
        if model.get("evidence") == "sufficient":
            st.success("该模型通过质量闸门，可进入影子组合统计；尚未开启正式执行。")
        else:
            st.info("该模型未通过质量闸门，组合接口会记录覆盖情况，但修正值保持为0。")

        summary = sp.shadow_summary()
        st.markdown("##### 影子验证闭环")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("影子记录", summary["total"])
        s2.metric("模型可用记录", summary["usable"])
        s3.metric("已成熟记录", summary["evaluated"])
        s4.metric("已评估名单", summary["groups"])
        if summary.get("lift") is not None:
            st.metric("概率影子5日收益增益", f"{summary['lift']:+.2%}",
                      help="同一批候选中，影子排序评估截面平均收益减去原排序评估截面平均收益。")
        else:
            st.caption("影子记录满5个交易日后，将自动回填真实收益并比较排序增益。")

        audit = sp.governance_audit(persist=False)
        st.markdown("##### 影子晋级治理")
        if audit["status"] == "eligible_for_manual_review":
            st.success("全部晋级门槛已通过，可以提交人工评审；系统仍不会自动开启正式修正。")
        else:
            st.info("当前继续影子观察，未达到人工晋级评审条件。")
        metrics = audit["metrics"]
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("成熟名单", metrics["groups"])
        g2.metric("模型可用覆盖率", f"{metrics['usable_coverage']:.1%}")
        g3.metric("正增益名单占比", f"{float(metrics.get('positive_group_rate') or 0):.1%}")
        g4.metric("95%增益下限", f"{float(metrics.get('ci_low') or 0):+.2%}")
        st.dataframe(pd.DataFrame(audit["reasons"]), hide_index=True, width="stretch")

        with st.expander("查看最近影子明细"):
            detail = sp.shadow_detail(200)
            if detail.empty:
                st.caption("暂无影子记录。")
            else:
                st.dataframe(detail, hide_index=True, width="stretch")
