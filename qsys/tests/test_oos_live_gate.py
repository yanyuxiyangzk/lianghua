"""track_oos_vs_live 统计功效门槛：n<30 个名单日不得报过拟合（2026-09-29 误报修复）。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experience as exp


def _fixture(tmpdir: str, n: int, hit_ratio: float):
    """造 n 个名单日：每日一个 pick（OOS 胜率 0.74）+ 一条 5 日前瞻结果。"""
    db = Path(tmpdir) / "experience.db"
    with patch.object(exp, "DB_PATH", db):
        with exp._conn() as c:
            hits = int(n * hit_ratio)
            for i in range(n):
                day = f"2026-08-{i % 28 + 1:02d}"
                c.execute(
                    "INSERT INTO picks(combo_hash,trade_date,created_at,source,pack_name,"
                    "oos_winrate_at_save) VALUES(?,?,?,'sched_pool_scan','p1',0.74)",
                    (f"h{i}", day, "x"))
                pick_id = c.execute("SELECT id FROM picks WHERE combo_hash=?", (f"h{i}",)).fetchone()[0]
                c.execute(
                    "INSERT INTO outcomes(pick_id,fwd_days,eval_date,avg_ret,pool_median,"
                    "excess,hit) VALUES(?,5,?,?,?,?,?)",
                    (pick_id, f"2026-09-{i % 28 + 1:02d}", 0.01, 0.0,
                     0.01 if i < hits else -0.01, 1 if i < hits else 0))
    return db


def test_small_sample_never_claims_overfitting():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp, n=9, hit_ratio=0.28)  # 复现误报现场：74% vs 28%
        with patch.object(exp, "DB_PATH", db):
            r = exp.track_oos_vs_live(365)
            assert r["n_samples"] == 9
            assert r["status"] == "样本不足", r["status"]  # 不得报"过拟合加剧"
    print("PASS: test_small_sample_never_claims_overfitting")


def test_large_sample_can_flag_overfitting():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp, n=40, hit_ratio=0.2)  # 74% vs 20%，40 个名单日
        with patch.object(exp, "DB_PATH", db):
            r = exp.track_oos_vs_live(365)
            assert r["n_samples"] == 40
            assert r["status"] == "⚠️ 过拟合加剧", r["status"]
    print("PASS: test_large_sample_can_flag_overfitting")


if __name__ == "__main__":
    test_small_sample_never_claims_overfitting()
    test_large_sample_can_flag_overfitting()
