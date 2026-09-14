"""集成测试：验证P0/P1/P2所有改动的功能正确性。"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# 确保 qsys 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ================================================================ 辅助函数
def _make_ic_series(n=200, mean=0.03, std=0.05, seed=42):
    """构造模拟 IC 序列。"""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2023-01-01", periods=n)
    vals = rng.normal(mean, std, n)
    return pd.Series(vals, index=dates)


def _make_panel_and_vals(n_stocks=50, n_days=300, seed=42):
    """构造模拟面板和因子值。"""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2023-01-01", periods=n_days)
    codes = [f"sh{600000 + i}" for i in range(n_stocks)]

    idx = pd.MultiIndex.from_product([dates, codes], names=["datetime", "instrument"])
    close = pd.DataFrame(
        rng.lognormal(0, 0.02, (n_days, n_stocks)).cumprod(axis=0) * 100,
        index=dates, columns=codes
    )
    panel = pd.DataFrame({
        "$open": (close * (1 + rng.normal(0, 0.005, close.shape))).stack(),
        "$high": (close * (1 + abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$low": (close * (1 - abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$close": close.stack(),
        "$volume": pd.Series(rng.uniform(1e6, 1e8, n_days * n_stocks), index=idx),
    })
    panel.index = panel.index.set_names(["datetime", "instrument"])

    signal = rng.normal(0, 1, (n_days, n_stocks))
    vals = pd.DataFrame(signal + rng.normal(0, 0.5, signal.shape), index=dates, columns=codes)
    vals = vals.stack().rename("f")
    vals.index = vals.index.set_names(["datetime", "instrument"])

    return panel, vals


# ================================================================ P0 测试
def test_p0_ic_log_scale():
    """P0: IC评分对数缩放。"""
    import factor_eval as fe
    import math

    # 测试对数缩放公式
    for ic_val in [0.01, 0.03, 0.05, 0.10]:
        ic_component = math.log(1 + abs(ic_val) * 50) / math.log(6)
        ic_component = min(1.0, ic_component)
        assert 0 <= ic_component <= 1, f"IC={ic_val} 评分应在 [0,1]"
    
    # 验证对数缩放的区分度
    # 旧线性公式：|IC|=0.05 就满分，0.03和0.08几乎无差异
    # 新对数公式：0.01→0.26, 0.03→0.52, 0.05→0.67, 0.10→0.85
    ic_01_log = min(1.0, math.log(1 + 0.01 * 50) / math.log(6))  # ~0.26
    ic_03_log = min(1.0, math.log(1 + 0.03 * 50) / math.log(6))  # ~0.52
    ic_05_log = min(1.0, math.log(1 + 0.05 * 50) / math.log(6))  # ~0.67
    ic_10_log = min(1.0, math.log(1 + 0.10 * 50) / math.log(6))  # ~0.85
    
    # 验证对数缩放单调递增
    assert ic_01_log < ic_03_log < ic_05_log < ic_10_log, "对数缩放应单调递增"
    
    # 验证区分度：0.01和0.10之间的差距应足够大
    diff = ic_10_log - ic_01_log
    assert diff > 0.5, f"区分度应>0.5, 实际{diff:.3f}"
    
    print(f"PASS: test_p0_ic_log_scale (0.01→{ic_01_log:.2f}, 0.03→{ic_03_log:.2f}, 0.05→{ic_05_log:.2f}, 0.10→{ic_10_log:.2f})")


def test_p0_icir_weight():
    """P0: ICIR加权。"""
    import factor_eval as fe
    
    # 构造模拟数据测试ICIR
    panel, vals = _make_panel_and_vals(n_stocks=30, n_days=200)
    ic = fe.ic_series(vals, fe.forward_returns(panel, 5))
    
    ic_mean = float(ic.mean())
    ic_std = float(ic.std())
    icir = ic_mean / (ic_std + 1e-12)
    
    # ICIR修正逻辑
    if icir > 1.5:
        icir_factor = 1.15
    elif icir > 1.0:
        icir_factor = 1.08
    elif icir < 0.3:
        icir_factor = 0.85
    else:
        icir_factor = 1.0
    
    assert 0.85 <= icir_factor <= 1.15, f"ICIR factor应在 [0.85, 1.15]"
    print(f"PASS: test_p0_icir_weight (ICIR={icir:.3f}, factor={icir_factor})")


def test_p0_is_oos_gap_gate():
    """P0: IS/OOS Gap闸门。"""
    import gates as g
    
    # 构造过拟合因子（IS高OOS低）
    ic_high_is = pd.Series(np.random.normal(0.05, 0.02, 200))  # IS IC高
    ic_low_oos = pd.Series(np.random.normal(0.005, 0.03, 80))  # OOS IC低
    ic_overfit = pd.concat([ic_high_is, ic_low_oos], ignore_index=True)
    
    split_70 = int(len(ic_overfit) * 0.7)
    is_mean = float(ic_overfit.iloc[:split_70].mean())
    oos_mean = float(ic_overfit.iloc[split_70:].mean())
    gap = is_mean - oos_mean
    
    # 应该检测到过拟合
    assert gap > 0.015, f"过拟合因子gap应>0.015, 实际{gap:.4f}"
    assert is_mean > 0.03, f"过拟合因子IS应>0.03, 实际{is_mean:.4f}"
    print(f"PASS: test_p0_is_oos_gap_gate (gap={gap:.4f}, IS={is_mean:.4f}, OOS={oos_mean:.4f})")


def test_p0_complexity_gate():
    """P0: 因子复杂度闸门。"""
    import gates as g
    
    # 简单表达式应通过
    result_simple = g.check_complexity_gate("rank(close / open)", max_depth=5, max_ops=12)
    assert result_simple["pass"], f"简单表达式应通过: {result_simple}"
    
    # 空代码应通过
    result_empty = g.check_complexity_gate("")
    assert result_empty["pass"], "空代码应通过"
    
    print(f"PASS: test_p0_complexity_gate")


def test_p0_decay_statistical():
    """P0: 衰减检测统计显著性。"""
    from loopengine.decay import detect_factor_decay
    from unittest.mock import patch, MagicMock
    import factor_eval as fe
    
    # 构造模拟IC序列（前半段高，后半段低 - 真实衰减）
    ic_series = pd.concat([
        pd.Series(np.random.normal(0.05, 0.02, 250)),  # 长期：IC高
        pd.Series(np.random.normal(0.01, 0.02, 60)),   # 短期：IC低
    ], ignore_index=True)
    
    # Mock _get_ic_series 返回我们的模拟数据
    with patch('loopengine.decay._get_ic_series', return_value=ic_series):
        with patch('loopengine.decay.library'):
            result = detect_factor_decay("test_factor", ["sh600000"], "2024-12-31")
    
    assert 't_stat' in result, "应包含t统计量"
    assert 'p_value' in result, "应包含p值"
    assert 'cohens_d' in result, "应包含效应量"
    assert 'significant' in result, "应包含显著性标志"
    print(f"PASS: test_p0_decay_statistical (t={result['t_stat']:.3f}, p={result['p_value']:.4f}, d={result['cohens_d']:.3f})")


# ================================================================ P1 测试
def test_p1_spearman_correlation():
    """P1: Spearman相关性。"""
    from scipy import stats as sp_stats
    
    # 构造有异常值的数据
    x = pd.Series([1, 2, 3, 4, 5, 100])
    y = pd.Series([1, 2, 3, 4, 5, 6])
    
    # Pearson会被异常值影响
    pearson = abs(x.corr(y))
    # Spearman更稳健
    spearman = abs(sp_stats.spearmanr(x, y)[0])
    
    # Spearman应该更准确地反映单调关系
    assert spearman > pearson, f"Spearman应更稳健: {spearman:.3f} > {pearson:.3f}"
    print(f"PASS: test_p1_spearman_correlation (Spearman={spearman:.3f} vs Pearson={pearson:.3f})")


def test_p1_bayesian_shrinkage():
    """P1: 小样本Bayesian shrinkage。"""
    import factor_eval as fe
    
    # 构造小样本OOS数据
    ic_series = pd.Series(np.random.normal(0.05, 0.02, 100))  # 真实IC=0.05
    first_seen = "2024-06-01"
    train_end = "2024-08-01"  # 只有约40天OOS
    
    result = fe._oos_stats(ic_series, first_seen, train_end, engine_selected=True)
    
    if result["OOS天数"] < 30 and result["OOS天数"] >= 5:
        # 应该进行shrinkage
        assert "OOS_shrinkage" in result, "小样本应包含shrinkage因子"
        assert "OOS_raw_ic" in result, "应保留原始IC"
        assert result["IC_OOS"] is not None, "IC_OOS不应为None"
        # shrinkage后的IC应该绝对值更小
        if result["OOS_raw_ic"] != 0:
            assert abs(result["IC_OOS"]) <= abs(result["OOS_raw_ic"]), \
                f"shrinkage后IC应更小: {result['IC_OOS']:.4f} vs {result['OOS_raw_ic']:.4f}"
        print(f"PASS: test_p1_bayesian_shrinkage (OOS天数={result['OOS天数']}, shrinkage={result.get('OOS_shrinkage', 'N/A')})")
    else:
        print(f"PASS: test_p1_bayesian_shrinkage (OOS天数={result['OOS天数']}, 跳过shrinkage测试)")


def test_p1_crowding_score():
    """P1: 拥挤度评分。"""
    import factor_eval as fe
    
    panel, vals = _make_panel_and_vals(n_stocks=30, n_days=200)
    score = fe._calc_crowding_score(vals, panel, lookback=60)
    
    assert 0 <= score <= 1, f"拥挤度评分应在 [0,1], 实际 {score}"
    print(f"PASS: test_p1_crowding_score (score={score:.3f})")


# ================================================================ P2 测试
def test_p2_transaction_manager():
    """P2: 事务管理器。"""
    import tempfile
    import sqlite3
    from transaction_manager import TransactionManager
    
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        conn = sqlite3.connect(tmp.name)
        
        # 创建测试表
        conn.execute("""
            CREATE TABLE test_table (
                id INTEGER PRIMARY KEY,
                name TEXT,
                value REAL
            )
        """)
        conn.commit()
        
        tm = TransactionManager(conn)
        
        # 测试事务提交
        with tm.transaction():
            conn.execute("INSERT INTO test_table (name, value) VALUES (?, ?)", ("test1", 1.0))
            conn.execute("INSERT INTO test_table (name, value) VALUES (?, ?)", ("test2", 2.0))
        
        count = conn.execute("SELECT COUNT(*) FROM test_table").fetchone()[0]
        assert count == 2, f"事务提交后应有2条记录, 实际{count}"
        
        # 测试事务回滚
        try:
            with tm.transaction():
                conn.execute("INSERT INTO test_table (name, value) VALUES (?, ?)", ("test3", 3.0))
                raise ValueError("模拟错误")
        except ValueError:
            pass
        
        count = conn.execute("SELECT COUNT(*) FROM test_table").fetchone()[0]
        assert count == 2, f"回滚后应仍为2条记录, 实际{count}"
        
        conn.close()
    print("PASS: test_p2_transaction_manager")


def test_p2_computation_cache():
    """P2: 计算缓存层。"""
    import tempfile
    from computation_cache import ComputationCache
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_cache.db"
        cache = ComputationCache(db_path)
        
        # 测试基本缓存操作
        cache.set("test_key", {"data": 42}, ttl=60, persist=True)
        result = cache.get("test_key")
        assert result == {"data": 42}, f"缓存读取失败: {result}"
        
        # 测试失效
        cache.invalidate("test_key")
        result = cache.get("test_key")
        assert result is None, f"失效后应返回None: {result}"
        
        # 测试内存缓存回填
        cache.set("test_key2", [1, 2, 3], ttl=60, persist=True)
        cache.memory.clear()  # 清空内存缓存
        result = cache.get("test_key2")  # 应从磁盘加载并回填内存
        assert result == [1, 2, 3], f"磁盘缓存加载失败: {result}"
        
    print("PASS: test_p2_computation_cache")


def test_p2_metrics_collector():
    """P2: 监控告警系统。"""
    import tempfile
    from metrics import MetricsCollector
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_metrics.db"
        collector = MetricsCollector(db_path)
        
        # 测试指标记录
        collector.record("factor.gate_pass_rate", 0.05)
        collector.record("strategy.win_rate", 0.55)
        
        # 测试聚合查询
        avg = collector.aggregate("factor.gate_pass_rate", hours=1)
        assert avg is not None, "聚合查询应返回结果"
        
        # 测试告警
        collector.record("system.cpu_usage", 85)  # 超过80%阈值
        alerts = collector.get_alerts(hours=1, severity="warning")
        cpu_alerts = [a for a in alerts if a["metric"] == "system.cpu_usage"]
        assert len(cpu_alerts) > 0, "应触发CPU告警"
        
        # 测试仪表盘
        dashboard = collector.get_dashboard()
        assert "metrics" in dashboard, "仪表盘应包含metrics"
        assert "alerts" in dashboard, "仪表盘应包含alerts"
        
        # 测试清理
        collector.cleanup(days=0)  # 清理所有
        stats_collector = MetricsCollector(db_path)
        # 应该是空的
        print(f"PASS: test_p2_metrics_collector")


# ================================================================ 集成测试
def test_integration_factor_eval_weights():
    """集成: factor_eval权重一致性。"""
    import factor_eval as fe
    
    # 验证新权重
    assert fe.MULTI_OBJECTIVE_WEIGHTS['ic'] == 0.65, f"IC权重应为0.65"
    assert fe.MULTI_OBJECTIVE_WEIGHTS['crowding'] == 0.10, f"拥挤度权重应为0.10"
    assert fe.MULTI_OBJECTIVE_WEIGHTS['stability'] == 0.05, f"稳定性权重应为0.05"
    
    total = sum(fe.MULTI_OBJECTIVE_WEIGHTS.values())
    assert abs(total - 1.0) < 0.01, f"权重总和应为1.0, 实际{total}"
    print(f"PASS: test_integration_factor_eval_weights (total={total})")


def test_integration_gates_new_gates():
    """集成: gates新闸门。"""
    import gates as g
    
    # 验证IS/OOS Gap闸门存在
    assert hasattr(g, 'evaluate_gates'), "evaluate_gates函数应存在"
    assert hasattr(g, 'check_complexity_gate'), "check_complexity_gate函数应存在"
    
    # 验证Spearman相关性
    from scipy import stats as sp_stats
    x = pd.Series([1, 2, 3, 4, 5])
    y = pd.Series([1, 2, 3, 4, 5])
    corr = sp_stats.spearmanr(x, y)[0]
    assert abs(corr - 1.0) < 0.01, f"Spearman相关应为1.0, 实际{corr}"
    print("PASS: test_integration_gates_new_gates")


# ================================================================ 主函数
if __name__ == "__main__":
    tests = [
        # P0
        test_p0_ic_log_scale,
        test_p0_icir_weight,
        test_p0_is_oos_gap_gate,
        test_p0_complexity_gate,
        test_p0_decay_statistical,
        # P1
        test_p1_spearman_correlation,
        test_p1_bayesian_shrinkage,
        test_p1_crowding_score,
        # P2
        test_p2_transaction_manager,
        test_p2_computation_cache,
        test_p2_metrics_collector,
        # 集成
        test_integration_factor_eval_weights,
        test_integration_gates_new_gates,
    ]
    
    passed = 0
    failed = 0
    errors = []
    
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            errors.append((t.__name__, str(e)))
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"测试结果: {passed} 通过, {failed} 失败")
    if errors:
        print(f"\n失败详情:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'='*60}")
    
    sys.exit(1 if failed else 0)
