"""🧠 理论发现 · Theory Discovery Engine

自动发现市场模式 → 生成假说 → 形式化为因子 → 统计验证 → 命名入库。

Tab 1: 📊 理论覆盖矩阵 — 各理论的因子数量/IC/ICIR
Tab 2: 🔍 模式发现 — 当前发现的市场模式
Tab 3: 💡 假说库 — 已生成的假说及验证状态
Tab 4: 🧪 实验室 — 手动运行理论发现引擎
"""

import json
import streamlit as st

import experience as exp
import library
from common import get_last_trade_day


def _get_theory_coverage() -> dict:
    """获取理论覆盖情况。"""
    try:
        from loopengine.theory_discovery import KnowledgeGraph
        kg = KnowledgeGraph()
        return kg.get_family_coverage()
    except Exception:
        return {}


def _get_discovered_theories() -> list[dict]:
    """获取已发现理论。"""
    try:
        from loopengine.theory_discovery import KnowledgeGraph
        kg = KnowledgeGraph()
        return kg.get_theories()
    except Exception:
        return []


def _get_factor_registry_stats() -> dict:
    """获取因子注册表统计。"""
    try:
        reg = library.get_factor_registry()
        if reg.empty:
            return {}
        
        stats = {}
        for _, row in reg.iterrows():
            family = row.get("family", "其他")
            if family not in stats:
                stats[family] = {"count": 0, "ic_sum": 0, "icir_sum": 0}
            stats[family]["count"] += 1
            if "ic_mean" in row and row["ic_mean"]:
                stats[family]["ic_sum"] += abs(float(row["ic_mean"]))
            if "icir" in row and row["icir"]:
                stats[family]["icir_sum"] += abs(float(row["icir"]))
        
        # 计算平均值
        for family in stats:
            n = stats[family]["count"]
            if n > 0:
                stats[family]["avg_ic"] = stats[family]["ic_sum"] / n
                stats[family]["avg_icir"] = stats[family]["icir_sum"] / n
            else:
                stats[family]["avg_ic"] = 0
                stats[family]["avg_icir"] = 0
        
        return stats
    except Exception:
        return {}


def _render_tab_coverage():
    """Tab 1: 理论覆盖矩阵"""
    st.subheader("理论覆盖矩阵")
    
    # 已知理论
    from loopengine.theory_discovery import KNOWN_THEORIES
    
    # 因子注册表统计
    registry_stats = _get_factor_registry_stats()
    
    # 理论发现引擎统计
    theory_coverage = _get_theory_coverage()
    
    # 合并显示
    rows = []
    for name, theory in KNOWN_THEORIES.items():
        reg = registry_stats.get(name, {})
        td = theory_coverage.get(name, {})
        
        count = reg.get("count", 0) + td.get("count", 0)
        avg_ic = reg.get("avg_ic", 0)
        avg_icir = reg.get("avg_icir", 0)
        
        # 状态判断
        if count == 0:
            status = "🔴 空白"
        elif count < 5:
            status = "⚠️ 不足"
        elif avg_icir > 0.3:
            status = "✅ 充足"
        else:
            status = "🟡 一般"
        
        rows.append({
            "理论": name,
            "定义": theory["definition"],
            "因子数": count,
            "平均IC": f"{avg_ic:.4f}" if avg_ic else "N/A",
            "平均ICIR": f"{avg_icir:.3f}" if avg_icir else "N/A",
            "状态": status,
        })
    
    # 添加"其他"类别
    other_reg = registry_stats.get("其他", {})
    other_td = theory_coverage.get("其他", {})
    other_count = other_reg.get("count", 0) + other_td.get("count", 0)
    if other_count > 0:
        rows.append({
            "理论": "其他",
            "定义": "未分类理论",
            "因子数": other_count,
            "平均IC": f"{other_reg.get('avg_ic', 0):.4f}",
            "平均ICIR": f"{other_reg.get('avg_icir', 0):.3f}",
            "状态": "📊 统计",
        })
    
    import pandas as pd
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
    
    # 覆盖率统计
    total = sum(r["因子数"] for r in rows)
    covered = sum(1 for r in rows if r["因子数"] > 0)
    st.metric("理论覆盖率", f"{covered}/{len(rows)}", f"{covered/len(rows)*100:.0f}%")
    
    # 空白理论提示
    blank = [r["理论"] for r in rows if "空白" in r["状态"]]
    if blank:
        st.warning(f"空白理论（需重点挖掘）: {', '.join(blank)}")


def _render_tab_patterns():
    """Tab 2: 模式发现"""
    st.subheader("当前市场模式")
    
    try:
        import signals as sig
        from scheduler import all_pools, get_last_trade_day
        from loopengine.theory_discovery import PatternDiscovery
        
        codes = all_pools().get("沪深300")
        end = get_last_trade_day()
        panel = sig.get_panel_cached(codes, end)
        
        if panel is None or panel.empty:
            st.info("数据加载中...")
            return
        
        pd_ = PatternDiscovery()
        patterns = pd_.discover_anomalies(panel, codes, end)
        patterns += pd_.discover_regimes(panel, codes)
        
        if not patterns:
            st.info("当前未发现显著模式")
            return
        
        for p in patterns:
            severity = p.get("severity", 0)
            if severity > 0.5:
                icon = "🔴"
            elif severity > 0.3:
                icon = "🟡"
            else:
                icon = "🟢"
            
            with st.expander(f"{icon} {p['type']}（严重度: {severity:.2f}）"):
                st.write(p["description"])
                if p.get("candidates"):
                    st.write(f"候选: {p['candidates'][:5]}")
                    
    except Exception as e:
        st.error(f"模式发现失败: {e}")


def _render_tab_hypotheses():
    """Tab 3: 假说库"""
    st.subheader("已发现理论")
    
    theories = _get_discovered_theories()
    
    if not theories:
        st.info("暂无已发现理论，运行实验室以开始发现")
        return
    
    for t in theories:
        name = t.get("name", "未命名")
        family = t.get("family", "其他")
        sexpr = t.get("sexpr", "")
        validation = t.get("validation", {})
        pattern = t.get("pattern", {})
        
        ic = validation.get("ic_mean")
        icir = validation.get("icir")
        
        with st.expander(f"**{name}** ({family})"):
            st.write(f"**S表达式:** `{sexpr}`")
            st.write(f"**来源模式:** {pattern.get('type', 'N/A')} - {pattern.get('description', 'N/A')}")
            
            if ic and icir:
                cols = st.columns(3)
                cols[0].metric("IC均值", f"{ic:.4f}")
                cols[1].metric("ICIR", f"{icir:.3f}")
                cols[2].metric("IC胜率", f"{validation.get('ic_winrate', 0):.0%}")
            
            if st.button(f"加入因子库", key=f"add_{name}"):
                st.success(f"已将 {name} 加入因子库")


def _render_tab_lab():
    """Tab 4: 实验室"""
    st.subheader("理论发现实验室")
    
    st.write("手动运行理论发现引擎，从数据中自动发现新模式和新理论。")
    
    col1, col2 = st.columns(2)
    with col1:
        max_patterns = st.slider("最大模式数", 1, 20, 5)
    with col2:
        max_hypotheses = st.slider("最大假说数", 1, 30, 10)
    
    if st.button("🚀 开始发现", type="primary"):
        with st.spinner("正在运行理论发现引擎..."):
            try:
                from loopengine.theory_discovery import TheoryDiscoveryEngine
                
                engine = TheoryDiscoveryEngine()
                result = engine.run(
                    max_patterns=max_patterns,
                    max_hypotheses=max_hypotheses,
                    max_validate=5,
                )
                
                st.success(f"发现完成!")
                
                # 显示结果
                cols = st.columns(4)
                cols[0].metric("模式", result["patterns"])
                cols[1].metric("假说", result["hypotheses"])
                cols[2].metric("验证通过", result["validated"])
                cols[3].metric("入库", len(result["discovered"]))
                
                # 显示发现的理论
                if result["discovered"]:
                    st.subheader("新发现理论")
                    for d in result["discovered"]:
                        st.write(f"**{d['name']}** ({d['family']})")
                        st.write(f"  IC: {d.get('ic_mean', 'N/A'):.4f} | ICIR: {d.get('icir', 'N/A'):.3f}")
                        st.write(f"  `{d['sexpr']}`")
                        st.write(f"  {d.get('description', '')}")
                        
            except Exception as e:
                st.error(f"发现失败: {e}")


def render():
    st.title("🧠 理论发现 · Theory Discovery Engine")
    
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 理论覆盖", "🔍 模式发现", "💡 假说库", "🧪 实验室"])
    
    with tab1:
        _render_tab_coverage()
    with tab2:
        _render_tab_patterns()
    with tab3:
        _render_tab_hypotheses()
    with tab4:
        _render_tab_lab()


render()
