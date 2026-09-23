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
    c1, c2, c3 = st.columns([1, 1, 3])
    build = c1.button("▶ 构建/更新联合模型", type="primary", use_container_width=True)
    build_llm = c2.button("🤖 重试LLM画像", use_container_width=True)
    c3.caption("统计程序负责概率与回测；LLM负责该股票的模型画像、脆弱性和验证建议，二者均按股票ID保存。")
    if build:
        try:
            with st.spinner("正在构建统计概率并生成该股票的LLM模型画像…"):
                sp.build_model(code)
                llm_result = sp.build_llm_profile(code)
            if llm_result.get("status") == "ok":
                st.success("联合模型构建完成：统计概率和LLM画像均已关联保存。")
            else:
                st.warning("统计概率已保存；LLM画像等待重试："
                           + str(llm_result.get("reason") or llm_result.get("status")))
            st.rerun()
        except Exception as exc:
            st.error(f"模型构建失败：{exc}")
    if build_llm:
        try:
            with st.spinner("正在审查该股票的统计证据并生成独立模型画像…"):
                result = sp.build_llm_profile(code)
            if result.get("status") == "ok":
                st.success("LLM模型画像已保存并关联到该股票。")
                st.rerun()
            else:
                st.error(f"LLM模型画像生成失败：{result.get('reason') or result.get('status')}")
        except Exception as exc:
            st.error(f"LLM模型画像生成失败：{exc}")

    model = sp.load_latest(code)
    if not model:
        st.info("尚无模型结果。请先在“单股票历史数据”抓取日线，再点击构建模型。")
    else:
        a, b, c, d, e = st.columns(5)
        a.metric("模型日期", model["asof_date"])
        b.metric("相似历史样本", model["sample_count"])
        c.metric("样本外验证次数", model["oos_count"])
        d.metric("证据等级", "充足" if model.get("evidence") == "sufficient" else "有限")
        e.metric("完整分钟日", int(model.get("intraday_days") or 0))
        iq = model.get("intraday_quality") or {}
        st.caption(f"训练区间：{model['train_start']} 至 {model['train_end']} · "
                   f"匹配方式：{model.get('match_method') or '-'} "
                   f"({model.get('selected_scheme') or '-'}) · 版本：{model['model_version']}")
        if model.get("uses_intraday"):
            st.success("本次样本外评估选择了日线 + 日内行为模型。")
        elif model.get("intraday_days"):
            st.info("分钟特征已参加候选模型评估，但本次样本外指标仍选择日线模型；系统不会为使用分钟数据而强行采用较差模型。")
        if iq:
            st.caption(f"分钟质量：{iq.get('quality', '-')} · 完整日 {iq.get('complete_days', 0)} / 总日 {iq.get('days', 0)} · "
                       f"覆盖率 {float(iq.get('coverage') or 0):.1%} · 重复日 {iq.get('duplicate_days', 0)}")

        st.markdown("##### 📊 数据资产")
        st.dataframe(sp.data_inventory(code), hide_index=True, width="stretch")

        rows = []
        labels = {"up_1d": "未来1日上涨", "up_3d": "未来3日上涨",
                  "up_5d": "未来5日上涨", "up_10d": "未来10日上涨",
                  "up_atr_5d": "未来5日先涨1×ATR", "down_atr_5d": "未来5日先跌1×ATR"}
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

        st.subheader("📈 K线与相似历史样本")
        match = sp.get_match_detail(code)
        if not match or match["matched"].empty:
            st.caption("暂无相似样本明细（kernel 方案不可用或历史为空）。")
        else:
            st.caption(
                f"高亮标记 = 模型按「{match['method']}」匹配到的 {match['matched_total']} 个相似历史日；"
                "颜色为该样本后5日路径结局（红=先涨1×ATR、绿=先跌1×ATR、灰=未触及），"
                "★ 为当前交易日。上方概率即由这些样本统计而来。")
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            UP, DOWN, BG, GRID = "#e54545", "#26a69a", "#101010", "#2a2a2a"
            k = match["kline"].tail(250).reset_index(drop=True)
            detail = match["matched"].copy()
            detail["date_str"] = pd.to_datetime(detail["date"]).dt.strftime("%Y-%m-%d")
            in_win = detail[detail["date_str"].isin(set(k["date"]))]
            marker_colors = {"up": UP, "down": DOWN, "no_hit": "#888888"}
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                row_heights=[0.75, 0.25], vertical_spacing=0.02)
            fig.add_trace(go.Candlestick(
                x=k["date"], open=k["open"], high=k["high"], low=k["low"],
                close=k["close"], increasing_line_color=UP, increasing_fillcolor=UP,
                decreasing_line_color=DOWN, decreasing_fillcolor=DOWN, name="K线"),
                row=1, col=1)
            vol_colors = [UP if c0 >= o0 else DOWN
                          for o0, c0 in zip(k["open"], k["close"])]
            fig.add_trace(go.Bar(x=k["date"], y=k["volume"], name="VOL",
                                 marker_color=vol_colors), row=2, col=1)
            if not in_win.empty:
                lows = dict(zip(k["date"], k["low"]))
                fig.add_trace(go.Scatter(
                    x=in_win["date_str"],
                    y=[float(lows.get(d, 0)) * 0.985 for d in in_win["date_str"]],
                    mode="markers", name="相似样本",
                    marker=dict(symbol="diamond", size=7, line=dict(width=0.5, color="#101010"),
                                color=[marker_colors.get(str(p), "#888888")
                                       for p in in_win["path_5"]]),
                    hovertext=[f"{d} 后5日:{(r or 0):+.1%} 路径:{p}"
                               for d, r, p in zip(in_win["date_str"], in_win["fwd_5"],
                                                  in_win["path_5"])],
                    hoverinfo="text"), row=1, col=1)
            cur = match["current_date"]
            cur_row = k[k["date"] == cur]
            if not cur_row.empty:
                fig.add_trace(go.Scatter(
                    x=[cur], y=[float(cur_row["low"].iloc[0]) * 0.97],
                    mode="markers", name="当前",
                    marker=dict(symbol="star", size=13, color="#ffd54f")),
                    row=1, col=1)
            fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
                              height=560, showlegend=False, xaxis_rangeslider_visible=False,
                              margin=dict(l=10, r=10, t=10, b=10))
            fig.update_xaxes(gridcolor=GRID)
            fig.update_yaxes(gridcolor=GRID)
            st.plotly_chart(fig, width="stretch")

            st.markdown("##### 相似样本明细")
            disp = detail.drop(columns=["date_str"], errors="ignore").copy()
            disp["date"] = disp["date"].astype(str).str[:10]
            path_labels = {"up": "先涨1×ATR", "down": "先跌1×ATR", "no_hit": "未触及"}
            disp["path_5"] = disp["path_5"].map(
                lambda p: path_labels.get(str(p), str(p)))
            rename = {"date": "日期", "close": "收盘", "ret_5": "5日收益",
                      "ret_20": "20日收益", "vol_20": "年化波动", "volume_ratio": "量比",
                      "atr_pct": "ATR占比", "trend_state": "趋势", "momentum_state": "动量",
                      "volume_state": "量能", "vol_state": "波动状态",
                      "market_trend_state": "市场趋势",
                      "intraday_direction_state": "日内方向",
                      "ob_imbalance_state": "盘口失衡", "fwd_1": "后1日",
                      "fwd_5": "后5日", "fwd_10": "后10日", "path_5": "路径结局",
                      "path_threshold": "路径阈值", "weight": "核权重"}
            disp = disp.rename(columns=rename)
            pct_cols = [c for c in ("5日收益", "20日收益", "年化波动", "ATR占比",
                                    "后1日", "后5日", "后10日", "路径阈值")
                        if c in disp.columns]
            st.dataframe(disp, hide_index=True, width="stretch",
                         column_config={c: st.column_config.NumberColumn(c, format="%.2%")
                                        for c in pct_cols} | (
                             {"核权重": st.column_config.NumberColumn("核权重", format="%.3f")}
                             if "核权重" in disp.columns else {}))

        st.subheader("当前状态")
        state = model.get("state") or {}
        state_labels = {"trend_state": "20日趋势", "momentum_state": "5日动量",
                        "volume_state": "成交量", "vol_state": "波动率",
                        "market_trend_state": "市场趋势", "market_vol_state": "市场波动",
                        "ret_5": "5日收益", "ret_20": "20日收益",
                        "vol_20": "20日年化波动", "volume_ratio": "量比",
                        "atr_pct": "ATR占价格"}
        st.dataframe(pd.DataFrame([{"指标": state_labels.get(k, k), "值": str(v)}
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

        llm_profile = sp.load_latest_llm_profile(code)
        st.subheader("该股票的LLM模型画像")
        if not llm_profile:
            st.caption("尚未生成。LLM不会修改概率，只审查统计模型并保存该股票专属画像。")
        else:
            profile = llm_profile.get("profile") or {}
            p1, p2, p3 = st.columns(3)
            p1.metric("关联统计模型", f"#{llm_profile['model_id']}")
            p2.metric("LLM置信度", f"{float(profile.get('confidence') or 0):.0%}")
            p3.metric("使用权限", "仅影子观察" if profile.get("trading_use") == "shadow_only" else "人工评审")
            if llm_profile.get("status") == "unavailable":
                st.warning("LLM画像待重试：" + str(profile.get("summary") or "服务暂不可用"))
            else:
                st.write(profile.get("summary") or profile.get("model_character") or "暂无摘要")
            if profile.get("risk_flags"):
                st.markdown("**模型风险：** " + "；".join(profile["risk_flags"]))
            if profile.get("validation_plan"):
                st.markdown("**后续验证：** " + "；".join(profile["validation_plan"]))
            guidance = pd.DataFrame(profile.get("feature_guidance") or [])
            if not guidance.empty:
                st.dataframe(guidance, hide_index=True, width="stretch")
            st.caption(f"模型日期 {llm_profile['asof_date']} · Prompt {llm_profile['prompt_version']} · "
                       f"数据指纹 {llm_profile['data_hash']} · 保存于 {llm_profile['created_at']}")

        candidates = pd.DataFrame(model.get("model_candidates") or [])
        if not candidates.empty:
            st.subheader("状态模型选择")
            st.caption("日线与日线+日内候选只按无标签泄漏的滚动样本外 Brier、校准误差和样本量选择，不按当前预测结果挑选。")
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
            m1, m2, _m3 = st.columns(3)
            m1.metric("概率影子5日收益增益", f"{summary['lift']:+.2%}",
                      help="同一批候选中，影子排序评估截面平均收益减去原排序评估截面平均收益。")
            if summary.get("baseline_lift") is not None:
                m2.metric("ATR基线增益（对照）", f"{summary['baseline_lift']:+.2%}",
                          help="ATR倒数一行规则的同口径增益。overlay 必须跑赢它才不是多余复杂。")
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

st.divider()
st.subheader("🧬 因子健康度元模型（机制B·影子）")
st.caption("池化全部名单历史估计 P(跑赢同批中位 | 分数位置 × 名单源IC状态 × 市场regime)，"
           "直接捕捉因子/策略包的过拟合失效期；影子模式只记录对照排序，不改正式名单。")
import factor_health as fh
hs = fh.health_summary()
h1, h2, h3, h4 = st.columns(4)
h1.metric("影子记录", hs["total"])
h2.metric("已成熟记录", hs["evaluated"])
h3.metric("已评估名单", hs["groups"])
if hs.get("lift") is not None:
    h4.metric("健康压缩5日增益", f"{hs['lift']:+.2%}",
              help="健康压缩排序 Top 与原排序 Top 的名单级配对平均收益差。")
haudit = fh.health_governance(persist=False)
if haudit["status"] == "eligible_for_manual_review":
    st.success("健康压缩达到人工晋级评审条件；系统仍不会自动开启正式修正。")
else:
    st.info("健康压缩继续影子观察。")
with st.expander("晋级门槛明细"):
    st.dataframe(pd.DataFrame(haudit["reasons"]), hide_index=True, width="stretch")
with st.expander("最近健康影子明细"):
    hdetail = fh.health_detail(200)
    if hdetail.empty:
        st.caption("暂无健康影子记录。")
    else:
        st.dataframe(hdetail, hide_index=True, width="stretch")
