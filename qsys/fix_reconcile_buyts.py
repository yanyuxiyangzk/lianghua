"""修复 position_reconcile 产生的错误 buy_ts：
将 source='reconcile_fix' 的持仓记录的 buy_ts 改为 broker_fills 中的实际成交时间。

因原始数据库文件为 root 所有，脚本会：
1. 复制数据库到同目录 backup
2. 在副本上修复
3. 提示用 sudo 覆盖回去
"""

import shutil
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
EXP_DB = DATA_DIR / "experience.db"
BACKUP = DATA_DIR / "experience_fix.db"


def fix():
    if not EXP_DB.exists():
        print("experience.db 不存在，跳过")
        return

    shutil.copy2(EXP_DB, BACKUP)
    print(f"已复制到 {BACKUP}")

    with sqlite3.connect(str(BACKUP), timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")

        # 查看现有数据
        total = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        reconcile = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE source='reconcile_fix'"
        ).fetchone()[0]
        print(f"positions 总计: {total}, reconcile_fix: {reconcile}")

        rows = conn.execute(
            "SELECT id, code, buy_ts, buy_date FROM positions"
            " WHERE source='reconcile_fix' AND status='open'"
        ).fetchall()
        if not rows:
            print("无需修复（无 reconcile_fix 持仓）")
            return

        fixed = 0
        for pos_id, code, old_buy_ts, buy_date in rows:
            fill_row = conn.execute(
                "SELECT ts FROM broker_fills WHERE code=? AND side='buy'"
                " AND source='ai' ORDER BY id DESC LIMIT 1", (code,)
            ).fetchone()
            if fill_row and fill_row[0] and fill_row[0] != old_buy_ts:
                conn.execute(
                    "UPDATE positions SET buy_ts=?, created_at=? WHERE id=?",
                    (fill_row[0], fill_row[0], pos_id))
                fixed += 1
                print(f"  修复 {code}: buy_ts {old_buy_ts} -> {fill_row[0]}")
            else:
                print(f"  跳过 {code}: 无匹配成交记录或时间一致")
        conn.commit()

    print(f"\n修复完成：{fixed}/{len(rows)} 条")
    print(f"\n请执行以下命令替换原数据库：")
    print(f"  sudo cp {BACKUP} {EXP_DB}")
    print(f"  sudo chown root:root {EXP_DB}")


if __name__ == "__main__":
    fix()
