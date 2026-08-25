"""QSYS 选股体检后台预热：内置+技术指标+RD-Agent进化因子 → 评分卡落库。

用法(在 lh-qsys 容器内跑，结果写入 market.db，页面自动加载):
    docker cp scripts/eval_warmup.py lh-qsys:/tmp/ && docker exec -d lh-qsys python3 /tmp/eval_warmup.py
进度: tail -f qsys/data/eval_warmup.log
"""
import sys
import time
import warnings

sys.path.insert(0, "/app")
warnings.filterwarnings("ignore")

import pandas as pd

import common
import factor_eval as fe
import library
import tab_picker as tp

POOL = "沪深300"


def main():
    codes = common.get_instruments("csi300")
    end = common.get_last_trade_day()
    # 与页面默认一致：预选防未来函数，统计截到 250 交易日前
    train_end = common.trade_day_offset(end, -250)
    facs = [f for f in tp._factor_universe() if f["kind"] in ("builtin", "tech", "evolved")]
    print(f"共 {len(facs)} 个因子, end={end}, train_end={train_end}, 池={POOL}({len(codes)}只)", flush=True)
    rows = []
    for i, fac in enumerate(facs):
        t0 = time.time()
        try:
            card = fe.build_scorecard([fac], codes, end, train_end=train_end)
            rows.append(card)
            print(f"[{i + 1}/{len(facs)}] {fac['name']} ICIR={card.iloc[0]['ICIR']} ({time.time() - t0:.0f}s)",
                  flush=True)
        except Exception as e:
            print(f"[{i + 1}/{len(facs)}] {fac['name']} FAIL {str(e)[:120]}", flush=True)
    if rows:
        big = pd.concat(rows, ignore_index=True)
        library.save_scorecard(big, POOL, end)
        library.sync_factor_registry(facs)
    print("WARMUP_DONE", flush=True)


if __name__ == "__main__":
    main()
