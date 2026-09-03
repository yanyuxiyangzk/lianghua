"""QSYS 定时任务调度（程序内调度，非系统 cron）。

机制：
  - APScheduler BackgroundScheduler 跑在 QSYS 容器进程内
  - 手动启动后按计划一直跑；容器停止 = 调度停止；容器重启后需在看板重新启动
  - 任务状态持久化在 /data/scheduler_state.json，运行结果在 /data/scheduler_last.json
"""

import json
import tarfile
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from common import (QLIB_DATA_DIR, SCHED_LAST_FILE, SCHED_STATE_FILE, SIGNALS_DIR,
                    all_pools, get_evolved_factors, get_last_trade_day, load_watchlist, save_json, load_json,
                    trade_day_offset)
import datasource
import signals as sig

TZ = "Asia/Shanghai"

# ---------------------------------------------------------------- 任务实现

def job_update_data() -> str:
    """每日行情更新：下载最新 qlib_bin 并解压覆盖（走 gh 代理，带校验）。"""
    import requests

    urls = ["https://gh-proxy.com/", "https://ghfast.top/", ""]
    base = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
    last_err = None
    for prefix in urls:
        try:
            with tempfile.TemporaryDirectory() as td:
                pkg = Path(td) / "qlib_bin.tar.gz"
                with requests.get(prefix + base, stream=True, timeout=600) as r:
                    r.raise_for_status()
                    with pkg.open("wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if pkg.stat().st_size < 100_000_000:  # 正常 ~560MB，太小视为失败
                    raise RuntimeError(f"包大小异常: {pkg.stat().st_size}")
                with tarfile.open(pkg) as tar:
                    tar.extractall(td, filter="data")
                src = Path(td) / "qlib_bin"
                # 覆盖式同步（qlib 按文件读，单文件覆盖安全）
                import shutil
                shutil.copytree(src, QLIB_DATA_DIR, dirs_exist_ok=True)
            new_last = get_last_trade_day()
            return f"数据已更新至 {new_last}"
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"全部下载源失败: {last_err}")


def _pick_evolved_factors(max_n: int = 3) -> list[dict]:
    """优先被接受的 SOTA 因子；没有则取最新轮次的因子。"""
    fac = get_evolved_factors(only_accepted=True)
    if not fac:
        fac = get_evolved_factors(only_accepted=False)
    return fac[:max_n]


def job_watchlist_signals() -> str:
    """个股任务：自选股 × 最新进化因子 → 最新值与5日变化。"""
    codes = load_watchlist()
    if not codes:
        return "自选股为空，跳过"
    factors = _pick_evolved_factors(3)
    if not factors:
        return "尚无带代码的进化因子（先跑 RD-Agent 进化），跳过"
    end = get_last_trade_day()
    rows = []
    for f in factors:
        df = sig.run_factor_code(f["code"], f["name"], codes, end)
        s = df.iloc[:, 0]
        dt_level = "datetime" if "datetime" in s.index.names else s.index.names[0]
        days = sorted(s.index.get_level_values(dt_level).unique())
        latest, prev = days[-1], days[max(0, len(days) - 6)]
        cur = s[s.index.get_level_values(dt_level) == latest]
        old = s[s.index.get_level_values(dt_level) == prev]
        cur.index = cur.index.get_level_values("instrument")
        old.index = old.index.get_level_values("instrument")
        for c in codes:
            rows.append({"code": c, "factor": f["name"],
                         "最新值": cur.get(c, float("nan")), "5日前": old.get(c, float("nan"))})
    out = pd.DataFrame(rows).pivot(index="code", columns="factor", values=["最新值", "5日前"])
    out.columns = [f"{f}|{k}" for k, f in out.columns]
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"watchlist_{end}.parquet")
    return f"{end} 自选股信号完成：{len(codes)} 只 × {len(factors)} 因子"


def _best_pack(packs: dict) -> str:
    """OOS 胜率最高（% 格式）的策略包名；无有效胜率则空串。"""
    best, best_wr = "", -1.0
    for name, pk in packs.items():
        v = str(pk.get("oos_winrate") or "")
        if not v.endswith("%"):
            continue
        try:
            wr = float(v.rstrip("%"))
        except ValueError:
            continue
        if wr > best_wr:
            best, best_wr = name, wr
    return best


def compute_pack_picks(pk: dict, codes: list[str], end: str, top_n: int):
    """按策略包配置计算 Top-N 名单（job_pool_scan 与 🧩选股组合页共用，同一套逻辑）。
    返回 (picks 综合分 Series, note, weights, f_series)；因子全部无法解析时抛错。"""
    import library

    f_series, weights = {}, {}
    panel = sig.get_panel_cached(codes, end)
    # 策略包：按其因子+权重+方向+过滤器
    evolved_by_name = {f["name"]: f for f in get_evolved_factors(only_accepted=False)}
    # LoopEngine 因子从注册表取代码（之前只查 evolved，loopengine/tech 因子会被静默丢弃）
    try:
        reg = library.get_factor_registry()
        le_code = {r["name"]: r["code"] for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
    except Exception:
        le_code = {}
    dropped = []
    for f in pk["factors"]:
        kind, fname = f.get("kind"), f["name"]
        if kind == "builtin":
            s = sig.compute_builtin(panel, fname)
        elif kind == "tech":
            s = sig.compute_common(panel, fname) if fname in sig.CATALOG_NAMES \
                else sig.compute_tech(panel, fname)
        else:
            ef = evolved_by_name.get(fname)
            code = (ef or {}).get("code") or le_code.get(fname)
            if not code:
                dropped.append(fname)
                continue
            if code.startswith("# sexpr:"):
                # 树因子进程内向量直算（和 le_factor_eval 同款快速路径，~0.1s/个；
                # 避开 run_factor_code 的子进程——慢且会因子代码缺陷挂起，2026-09 实测把扫描拖死）
                try:
                    from loopengine.tree import build_field_frames, evaluate_tree, parse

                    tree = parse(code.split("\n", 1)[0][len("# sexpr: "):])
                    s = evaluate_tree(tree, build_field_frames(panel)).stack().rename(fname)
                    s.index = s.index.set_names(["datetime", "instrument"])
                except Exception:
                    df = sig.run_factor_code(code, fname, codes, end)
                    s = df.iloc[:, 0]
            else:
                df = sig.run_factor_code(code, fname, codes, end)
                s = df.iloc[:, 0]
        f_series[fname] = s
        weights[fname] = (f["weight"], f["direction"])
    if not weights:
        raise RuntimeError("策略包因子全部无法解析，未出名单")
    score = sig.composite_score(f_series, weights)
    survived = sig.apply_filters(score.index.tolist(), panel, pk.get("filters", []))
    sel = score[score.index.isin(survived)]
    reso_note = ""
    # 多周期共振：包带持有期时，用最新评分卡在 主口径+另一短线口径 各配权取交集
    try:
        sc = library.get_latest_scorecard(pk["pool_name"])
        if not sc.empty and pk.get("horizon"):
            scm = sc.drop_duplicates(subset=["因子"]).set_index("因子")
            h_main = pk["horizon"] if pk["horizon"] in ("1日", "5日") else "5日"
            h_pair = "1日" if h_main == "5日" else "5日"

            def _hw(h):
                col = f"{h}胜率"
                out = {}
                for f in pk["factors"]:
                    n = f["name"]
                    if n in scm.index and col in scm.columns and pd.notna(scm.loc[n, col]):
                        out[n] = (max(float(scm.loc[n, col]) - 0.5, 0.0), f["direction"])
                t = sum(w for w, _ in out.values())
                return {n: (w / t, d) for n, (w, d) in out.items()} if t > 0 else None

            wa, wb = _hw(h_main), _hw(h_pair)
            if wa and wb and len(wa) >= 2:
                sel = sig.resonance_select(f_series, wa, wb, top_n, k=top_n * 3)
                sel = sel[sel.index.isin(survived)]
                reso_note = f"·{h_main}+{h_pair}共振"
    except Exception:
        pass
    picks = sig.industry_cap_select(sel, cap=2).head(top_n)
    note = f"{len(weights)} 因子·行业≤2{reso_note}" + \
        (f"，{len(dropped)} 个无法解析已跳过" if dropped else "")
    return picks, note, weights, f_series


def _satellite_pack_name(packs: dict) -> str | None:
    """卫星轨策略包：含 ev_ 事件因子最多的包；其次名字含 涨停/事件 的包。"""
    best, best_n = None, 0
    for n, pk in packs.items():
        k = sum(1 for f in pk.get("factors", []) if str(f["name"]).startswith("ev_"))
        if k > best_n:
            best, best_n = n, k
    if best:
        return best
    return next((n for n in packs if "涨停" in n or "事件" in n), None)


def job_pool_scan(pool_name: str = "沪深300", top_n: int = 10, pack: str = "") -> str:
    """板块/池任务：综合打分输出 Top-N。pack 为空时自动选用 OOS 胜率最高的策略包。
    主包扫完后顺带扫卫星包（涨停轨），今日执行页两条轨每天都有当天名单。"""
    end = get_last_trade_day()
    import library
    packs = library.list_strategies()
    if not pack:
        pack = _best_pack(packs)
    pk = packs.get(pack) if pack else None

    if pk:
        pool_name = pk["pool_name"]
        # 每日自动名单以任务参数为上限(默认10只);包的 Top-N 更大时取分更高的前段
        top_n = min(int(pk["top_n"]), int(top_n))
    pools = all_pools()
    codes = pools.get(pool_name) or pools.get("沪深300")

    if pk:
        picks, pnote, weights, f_series = compute_pack_picks(pk, codes, end, top_n)
        note = f"策略包「{pack}」（{pnote}）"
    else:  # 默认组合：最新进化因子 + 内置三件套
        panel = sig.get_panel_cached(codes, end)
        f_series, weights = {}, {}
        factors = _pick_evolved_factors(2)
        for f in factors:
            df = sig.run_factor_code(f["code"], f["name"], codes, end)
            f_series[f["name"]] = df.iloc[:, 0]
            weights[f["name"]] = (1.0, 1)
        for b in ["mom_20d", "vol_20d", "volume_ratio_5_20"]:
            f_series[b] = sig.compute_builtin(panel, b)
        weights.update({"mom_20d": (1.0, 1), "vol_20d": (1.0, -1), "volume_ratio_5_20": (1.0, -1)})
        score = sig.composite_score(f_series, weights)
        picks = score.head(top_n)
        note = f"默认组合（进化因子 {len(factors)} 个参与）"

    out = pd.DataFrame({"score": picks})
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    safe_pool = pool_name.replace("/", "_")
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"scan_{safe_pool}_{end}.parquet")

    # 经验库落库（不管对错都记，到期由 outcome_backfill 回填战果）
    import experience
    fcfg = [{"name": n, "kind": ("builtin" if n in sig.BUILTIN_FACTORS else "evolved"),
             "weight": float(w), "direction": int(d)} for n, (w, d) in weights.items()]
    oos = None
    if pk and pk.get("oos_winrate"):
        try:
            oos = float(str(pk["oos_winrate"]).strip("%")) / 100
        except (TypeError, ValueError):
            oos = None
    experience.save_pick(source="sched_pool_scan", pool_name=pool_name, top_n=top_n,
                         method=(pk.get("method") if pk else "默认组合"), filters=(pk.get("filters", []) if pk else []),
                         factors=fcfg, final_scores=picks, pack_name=(pack or None),
                         oos_winrate=oos, trade_date=end)

    # 卫星包顺带扫描：给「博涨停」轨出每日名单（今日执行页卫星轨按包名读取）
    sat_msg = ""
    try:
        sat_name = _satellite_pack_name(packs)
        if sat_name and sat_name != pack:
            spk = packs[sat_name]
            scodes = pools.get(spk["pool_name"]) or codes
            spicks, _sn, _sw, _sf = compute_pack_picks(spk, scodes, end, int(spk["top_n"]))
            experience.save_pick(source="sched_satellite_scan", pool_name=spk["pool_name"],
                                 top_n=int(spk["top_n"]), method=spk.get("method"),
                                 filters=spk.get("filters", []), factors=spk["factors"],
                                 final_scores=spicks, pack_name=sat_name, trade_date=end)
            sat_msg = f" · 卫星包「{sat_name}」Top{len(spicks)}"
    except Exception as e:
        sat_msg = f" · 卫星包扫描失败({e})"
    return f"{end} {pool_name} 扫描完成：Top{top_n} 已出（{note}）{sat_msg}"


def job_outcome_backfill() -> str:
    """经验库战果回填：到期的历史名单按交易日历结算 5/10/20 日战绩。"""
    import experience
    return experience.backfill_outcomes()


def job_gate_check(pool_name: str = "沪深300") -> str:
    """因子库硬闸门筛查（每日）：新因子过 11 项闸门 + 重算 FSA。"""
    import gaterun

    res = gaterun.run_gates_for_pool(pool_name, only_pending=True)
    return f"硬闸门：评估 {res['evaluated']} · 通过 {res['passed']} · FSA冻结 {res['frozen']}"


def job_loopengine(batch: int = 30, **_ignored) -> str:
    """LoopEngine 演化引擎：每轮 生成→审查→验证→入库（检查点自动保存）。
    容忍调度界面写入的多余参数（如 pool_name）。"""
    from loopengine.engine import LoopEngine

    eng = LoopEngine("沪深300")
    r = eng.run_round(batch=batch)
    return (f"第{r['iteration']}轮 · 测试{r['tested']} · 过审拒绝{r['rejected_review']} · "
            f"LLM否决{r.get('llm_rejected', 0)} · 重复{r['dup']} · FSA拦截{r['frozen']} · 入库{r['passed']} {r['new'][:3]}")


def job_event_mine(kind: str = "涨停", batch: int = 30, horizon: int = 5,
                   pool_name: str = "沪深300", **_ignored) -> str:
    """事件定向挖因子：围绕「涨停/大涨/跌停/创新高」做事件目标演化，
    入库前缀 ev_（gate_status=2 事件闸门，区别于收益管线）。"""
    from loopengine.engine import LoopEngine

    eng = LoopEngine(pool_name)
    r = eng.run_event_round(kind, batch=batch, horizon=horizon)
    return (f"事件[{kind}|{horizon}日] 第{r['iteration']}轮 · 测试{r['tested']} · "
            f"重复{r['dup']} · FSA拦截{r['frozen']} · 入库{r['passed']} {r['new'][:3]}")


def job_top5_composite() -> str:
    """Top5 复合因子：过硬闸门因子按夏普取 Top5，方向修正等权合成并固化策略包。"""
    import composite

    r = composite.build_top5_composite("沪深300")
    if not r.get("ok"):
        return r.get("msg", "合成失败")
    members = "、".join(f"{m['name']}({'+' if m['direction'] > 0 else '-'})" for m in r["members"])
    return f"Top5复合：IC={r['IC']} 夏普={r['sharpe']} 年化超额={r['年化超额']:.1%} | {members}"


def job_position_track(**_ignored) -> str:
    """持仓跟踪（盘中每5分钟）：名单挂限价委托 → 触及成交开仓 → 持仓止盈/止损/到期平仓（T+1）。

    流程贴近实盘：委托（参考价=名单价）→ 现价触及才成交 → 买入日当天不卖（T+1）。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    if not ("0930" <= now.strftime("%H%M") <= "1500"):
        return "非交易时段，跳过"

    import experience
    today = now.strftime("%Y-%m-%d")
    latest = experience.list_pick_dates(limit=1)
    m1 = experience.position_open_from_picks(latest[0], today) if latest else "无名单"
    m_fill = experience.position_fill_check(today)
    m2 = experience.position_close_check(today)
    # 顺带撮合模拟柜台的挂单（限价单价格触及即成交）
    import broker
    n_fill = broker.fill_pending_orders()
    parts = [m1, m_fill, m2] + ([f"挂单成交 {n_fill} 笔"] if n_fill else [])
    return "；".join(p for p in parts if p)


def job_trade_simulate() -> str:
    """模拟交易回填：对经验库新名单按默认规则（止盈15%/止损-8%/持有20日）逐笔模拟平仓。"""
    import experience

    return experience.backfill_trades()


def job_auction_confirm() -> str:
    """竞价确认（09:26，集合竞价落锤后）：对昨晚名单逐只检查竞价表现，标记回避信号。

    规则（保守，宁缺毋滥）：
      回避 = 竞价低开 ≤ -2%（隔夜利空跳空）或 竞价量 < 20日均量的 0.5%（无量承接）
    结果存 signals/auction_<当日>.parquet，选股列表页次日名单旁显示确认状态。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience

    dates = experience.list_pick_dates(5)
    if not dates:
        return "无选股名单，跳过"
    # 名单必须足够新：最近名单日期 = 上一交易日（周末近似往前推）
    prev = now - pd.Timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= pd.Timedelta(days=1)
    if dates[0] < prev.strftime("%Y-%m-%d"):
        return f"最新名单为 {dates[0]}（过旧），跳过"
    picks = experience.picks_on_date(dates[0])
    items = experience.pick_items_detail(int(picks.iloc[0]["id"]))
    rows = []
    for code in items["code"]:
        try:
            snap = datasource.get_realtime_snapshot(code)
            price, prev_close = snap.get("price"), snap.get("prev_close")
            if not price or not prev_close:
                continue
            gap = price / prev_close - 1
            d40 = (now - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
            daily = datasource.get_daily(code, d40, now.strftime("%Y-%m-%d"))
            avg20 = daily["$volume"].tail(20).mean() if not daily.empty else None
            # 快照 volume 单位为手，×100 对齐日线（股）
            ratio = (snap.get("volume") or 0) * 100 / avg20 if avg20 else None
            verdict = "回避" if (gap <= -0.02 or (ratio is not None and ratio < 0.005)) else "确认"
            rows.append({"code": code, "竞价涨幅%": round(gap * 100, 2),
                         "竞价量比%": round(ratio * 100, 2) if ratio is not None else None,
                         "竞价结论": verdict})
        except Exception:
            continue
    if not rows:
        return "竞价数据为空（可能尚未开盘）"
    out = pd.DataFrame(rows)
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    day = now.strftime("%Y-%m-%d")
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"auction_{day}.parquet")
    avoid = out[out["竞价结论"] == "回避"]["code"].tolist()
    return f"{day} 竞价确认：{len(rows)} 只 · 回避 {len(avoid)} 只（{','.join(avoid) or '无'}）"


def job_quote_collect(pool_name: str = "沪深300", interval_sec: int = 30) -> str:
    """行情快照采集：交易时段内批量拉取并落库（给 1分钟涨速/现手 供历史）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5 or not ("0915" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"
    pools = all_pools()
    codes = pools.get(pool_name) or []
    if not codes:
        return f"池 {pool_name} 为空，跳过"
    rows = datasource.get_batch_snapshots(codes)
    n = datasource.save_snapshots(rows)
    return f"{now.strftime('%H:%M:%S')} 采集 {pool_name} {n} 只快照"


def job_sector_flow_collect(interval_sec: int = 30, **_ignored) -> str:
    """板块资金流采集：交易时段内抓板块快照+资金净流入落库
    （sector_flow_snapshots / sector_inflow_snapshots），给 🌐资金趋势/🏛板块行情 页供数。
    页面开关的采集线程随容器重启消失，此任务让采集不依赖页面是否打开。"""
    from zoneinfo import ZoneInfo

    import sectorflow as sf

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5 or not ("0915" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"
    n = sf.save_sector_spot(sf.fetch_sector_spot())
    sf.save_sector_inflow_snapshot()
    return f"{now.strftime('%H:%M:%S')} 板块快照 {n} 个 + 资金流快照已存"


def job_ifind_daily_sync(pool_name: str = "自选股", lookback_days: int = 10, **_ignored) -> str:
    """iFinD 日线自动入库：每日盘后把自选股/池子的日线增量写入 market.db
    （market_daily 表，source='ths_ifind'）。INSERT OR REPLACE 幂等，
    lookback 留冗余覆盖缺数；交易日判断交给 cron（mon-fri），节假日空跑无害。"""
    from zoneinfo import ZoneInfo

    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"
    now = datetime.now(ZoneInfo(TZ))
    end = now.strftime("%Y-%m-%d")
    # 日历日 ≈ 交易日×2+5，保证覆盖 lookback_days 个交易日
    start = (now - pd.Timedelta(days=int(lookback_days) * 2 + 5)).strftime("%Y-%m-%d")
    total, failed = 0, []
    for code in codes:
        try:
            total += datasource._ths_fetch_daily(code, start, end)
        except Exception:
            failed.append(code)
    msg = f"{end} iFinD 日线入库：{len(codes)} 只 → {total} 行（回看 {lookback_days} 个交易日）"
    if failed:
        msg += f" · 失败 {len(failed)} 只（{','.join(failed[:5])}{'…' if len(failed) > 5 else ''}）"
    return msg


def job_ifind_calendar(exchange: str = "SSE", **_ignored) -> str:
    """iFinD 交易日历入库（ifind_calendar 表）——给各页面提供真实交易日历。"""
    df, res, err = datasource.ths_trade_dates(exchange)
    if err not in (0, None) or df is None or df.empty:
        return f"交易日历拉取失败 err={err}（凭证问题见 📡 iFinD 页状态）"
    col = next((c for c in df.columns if "date" in c.lower() or "time" in c.lower()), df.columns[0])
    dates = sorted(pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d").tolist())
    with datasource._conn() as c:
        c.executemany("INSERT OR IGNORE INTO ifind_calendar(exchange, date) VALUES (?,?)",
                      [(exchange, d) for d in dates])
    return f"{exchange} 交易日历 {len(dates)} 天（{dates[0]}~{dates[-1]}）"


def job_ifind_basic_daily(pool_name: str = "自选股", **_ignored) -> str:
    """基本面指标包每日入库（ifind_basic_daily 长表 code/date/indicator/value）。
    走行情端点（cmd_history_quotation/THS_HQ）单日截面——实测比 basic_data_service
    的 indiparams 规则稳得多：收盘价/PE_TTM/PB/总股本/总市值/流通市值/换手率。"""
    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"
    inds = "close,pe_ttm,pb,totalShares,totalCapital,floatCapitalOfAShares,turnoverRatio"
    today = datetime.now().strftime("%Y-%m-%d")
    df, res, err = datasource.ths_history(codes, inds, today, today, "")
    if err not in (0, None) or df is None or df.empty:
        return f"基本面拉取失败 err={err}（凭证问题见 📡 iFinD 页状态）"
    date_col = "date" if "date" in df.columns else ("time" if "time" in df.columns else None)
    ind_cols = [c for c in df.columns if c not in ("time", "date", "thscode")]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, r in df.iterrows():
        rdate = str(r[date_col])[:10] if date_col else today  # HTTP 截面返回无日期列→用当天
        for ind in ind_cols:
            v = r.get(ind)
            if pd.notna(v):
                rows.append((str(r.get("thscode")), rdate, ind, float(v), now))
    with datasource._conn() as c:
        c.executemany("INSERT OR REPLACE INTO ifind_basic_daily"
                      "(code,date,indicator,value,fetched_at) VALUES (?,?,?,?,?)", rows)
    return f"{today} 基本面入库 {len(rows)} 行（{len(codes)} 只 × {len(ind_cols)} 指标）"


def job_ifind_announce(pool_name: str = "自选股", days: int = 7, **_ignored) -> str:
    """公告每日抓取得入 ifind_announcements 表（按 seq 去重，幂等）。"""
    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"
    df, res, err = datasource.ths_announce(codes, days=int(days))
    if err not in (0, None) or df is None or df.empty:
        return f"近 {days} 天无公告或拉取失败 err={err}"
    df.columns = [str(c).lower() for c in df.columns]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with datasource._conn() as c:
        for _, r in df.iterrows():
            seq = str(r.get("seq") or "")
            if not seq:
                continue
            cur = c.execute("INSERT OR IGNORE INTO ifind_announcements"
                            "(seq,code,report_date,title,pdf_url,ctime,fetched_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (seq, str(r.get("thscode", "")), str(r.get("reportdate", ""))[:10],
                             str(r.get("reporttitle", "")), str(r.get("pdfurl", "")),
                             str(r.get("ctime", "")), now))
            n += cur.rowcount
    return f"公告入库：拉到 {len(df)} 条，新增 {n} 条（seq 去重）"


def job_ifind_stocklist_sync(**_ignored) -> str:
    """iFinD 全市场A股列表同步（每日09:00执行）。

    调用 datasource.fetch_stocklist_to_db() 拉取全量数据。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"

    n = datasource.fetch_stocklist_to_db()
    if n > 0:
        return f"{now.strftime('%Y-%m-%d')} A股列表同步完成：{n} 只"
    else:
        return "A股列表同步失败（可能iFinD限流或凭证问题）"


def job_ifind_indexlist_sync(**_ignored) -> str:
    """iFinD 指数列表同步（每日09:05执行，A股列表之后）。

    调用 datasource.fetch_indexlist_to_db()：问财取指数全集（沪深/行业/主题）
    + 宽基种子，iFinD 实时行情补价格，写入 ifind_indexlist 表。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"

    n = datasource.fetch_indexlist_to_db()
    if n > 0:
        return f"{now.strftime('%Y-%m-%d')} 指数列表同步完成：{n} 条"
    else:
        return "指数列表同步失败（可能iFinD限流或凭证问题）"


def job_ifind_realtime_sync(**_ignored) -> str:
    """iFinD 实时行情快照同步（盘中每15分钟执行）。

    调用 datasource.fetch_realtime_to_db() 写入 ifind_realtime 表。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"
    # 交易时段判断：09:30-15:00
    if not ("0930" <= now.strftime("%H%M") <= "1500"):
        return "非交易时段，跳过"

    n = datasource.fetch_realtime_to_db()
    if n > 0:
        return f"{now.strftime('%H:%M:%S')} 实时快照写入完成：{n} 只"
    else:
        return "实时快照写入失败（可能iFinD限流或无数据）"


def job_ifind_cleanup(**_ignored) -> str:
    """清理过期数据（每日16:00执行）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    datasource.cleanup_old_data()
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} 过期数据清理完成"


def job_le_factor_eval(batch: int = 60, pool_name: str = "沪深300") -> str:
    """LoopEngine 因子滚动体检（每晚）：族配额优先取一批出评分卡。

    演化引擎日产出数百因子，全量体检不现实；每晚一批滚动覆盖：已评估覆盖越少的
    机制族越优先，族内按最久未评估轮询（因子会衰减）。
    速度：树因子（代码首行带 # sexpr:）在进程内向量化直算并预填因子值缓存，
    跳过子进程（~10s/个 → ~0.1s/个），只有非树因子才回退子进程执行。
    """
    import factor_eval as fe
    import library

    reg = library.get_factor_registry()
    le = reg[reg["engine"] == "loopengine"] if not reg.empty else reg
    if le.empty:
        return "无 LoopEngine 因子，跳过"
    with library._lconn() as c:
        evaluated = dict(c.execute(
            "SELECT name, MAX(updated_at) FROM factor_scorecards GROUP BY name").fetchall())
    le = le.assign(_eval_at=le["name"].map(lambda n: evaluated.get(n, "")))
    # 族配额优先：库内因子同质化严重（波动族占绝大多数），按"最久未评估"轮询会把
    # 体检预算全花在波动族克隆上。改为：已评估覆盖越少的机制族越优先，族内按最旧轮询。
    le["_fam"] = le["family"].fillna("其他") if "family" in le.columns else "其他"
    fam_cov = le.groupby("_fam")["_eval_at"].apply(lambda s: int((s != "").sum()))
    fam_order = fam_cov.sort_values().index.tolist()
    by_fam = {f: g.sort_values("_eval_at") for f, g in le.groupby("_fam")}
    picked_idx, cursor = [], {f: 0 for f in fam_order}
    while len(picked_idx) < batch and any(cursor[f] < len(by_fam[f]) for f in fam_order):
        for f in fam_order:
            if len(picked_idx) >= batch:
                break
            if cursor[f] < len(by_fam[f]):
                picked_idx.append(by_fam[f].index[cursor[f]])
                cursor[f] += 1
    picked = le.loc[picked_idx]
    codes = all_pools().get(pool_name) or []
    if len(codes) < 30:
        return f"池 {pool_name} 为空，跳过"
    end = get_last_trade_day()
    train_end = trade_day_offset(end, -250)
    facs = [{"name": r["name"], "kind": "loopengine", "code": r["code"]} for _, r in picked.iterrows()]

    # 树直算快速路径：预填 get_factor_values 同款缓存，build_scorecard 随后全部命中
    fast_done = 0
    try:
        from loopengine.tree import build_field_frames, evaluate_tree, parse

        panel = sig.get_panel_cached(codes, end, 800, source=datasource.get_loop_source())
        frames = build_field_frames(panel)
        ck_prefix = "|".join(sorted(codes))
        for fac in facs:
            code = fac.get("code") or ""
            if not code.startswith("# sexpr: "):
                continue
            try:
                ck = fe._cache("fvals", f"qlib_local|{fac['name']}|{fac['kind']}|{ck_prefix}|{end}|800")
                if ck.exists():
                    fast_done += 1
                    continue
                tree = parse(code.split("\n", 1)[0][len("# sexpr: "):])
                vals = evaluate_tree(tree, frames).stack().rename("f").dropna()
                vals.index = vals.index.set_names(["datetime", "instrument"])
                sig._write_parquet_atomic(fe._norm(vals).to_frame(fac["name"]), ck)
                fast_done += 1
            except Exception:
                continue  # 单个失败回退 build_scorecard 的子进程路径
    except Exception:
        pass

    card = fe.build_scorecard(facs, codes, end, train_end=train_end)
    library.save_scorecard(card, pool_name, end)
    ok = card.dropna(subset=["ICIR"])
    return (f"LoopEngine 体检 {len(facs)} 个（树直算 {fast_done} · 有效 {len(ok)} 个），"
            f"累计已评估 {len(evaluated) + len(facs) - len([n for n in picked['name'] if n in evaluated])}"
            f"/{len(reg[reg['engine']=='loopengine'])}")


# ---------------------------------------------------------------- 调度器
JOBS = {
    "update_data": {"name": "📥 每日数据更新", "func": job_update_data,
                    "default": {"enabled": False, "hour": 17, "minute": 35, "params": {}}},
    "ifind_daily_sync": {"name": "📡 iFinD 日线入库（盘后）", "func": job_ifind_daily_sync,
                         "default": {"enabled": False, "hour": 15, "minute": 40,
                                     "params": {"pool_name": "自选股", "lookback_days": 10}}},
    "ifind_calendar": {"name": "🗓 iFinD 交易日历入库", "func": job_ifind_calendar,
                       "default": {"enabled": False, "hour": 8, "minute": 30,
                                   "params": {"exchange": "SSE"}}},
    "ifind_basic_daily": {"name": "🏢 iFinD 基本面指标入库（盘后）", "func": job_ifind_basic_daily,
                          "default": {"enabled": False, "hour": 15, "minute": 50,
                                      "params": {"pool_name": "自选股"}}},
    "ifind_announce": {"name": "📜 iFinD 公告抓取入库", "func": job_ifind_announce,
                       "default": {"enabled": False, "hour": 16, "minute": 30,
                                   "params": {"pool_name": "自选股", "days": 7}}},
    "ifind_stocklist_sync": {"name": "📋 iFinD A股列表同步（每日）", "func": job_ifind_stocklist_sync,
                             "default": {"enabled": False, "hour": 9, "minute": 0, "params": {}}},
    "ifind_indexlist_sync": {"name": "📉 iFinD 指数列表同步（每日）", "func": job_ifind_indexlist_sync,
                             "default": {"enabled": False, "hour": 9, "minute": 5, "params": {}}},
    "ifind_realtime_sync": {"name": "📊 iFinD 实时快照同步（盘中）", "func": job_ifind_realtime_sync,
                            "default": {"enabled": False, "hour": 0, "minute": 0,
                                        "params": {"interval_sec": 300},
                                        "trigger": "interval"}},  # interval_sec 必须放 params 里（调度器从 params 读）
    "ifind_cleanup": {"name": "🧹 iFinD 过期数据清理", "func": job_ifind_cleanup,
                      "default": {"enabled": False, "hour": 16, "minute": 0, "params": {}}},
    "watchlist_signals": {"name": "📈 个股信号（自选股 × 进化因子）", "func": job_watchlist_signals,
                          "default": {"enabled": False, "hour": 18, "minute": 30, "params": {}}},
    "pool_scan": {"name": "🏛️ 板块/股票池扫描（Top-N）", "func": job_pool_scan,
                  "default": {"enabled": False, "hour": 19, "minute": 0,
                              "params": {"pool_name": "沪深300", "top_n": 10, "pack": ""}}},
    "outcome_backfill": {"name": "🎯 战果回填（经验库）", "func": job_outcome_backfill,
                         "default": {"enabled": False, "hour": 18, "minute": 45, "params": {}}},
    "gate_check": {"name": "🛡 硬闸门筛查（因子库）", "func": job_gate_check,
                   "default": {"enabled": False, "hour": 18, "minute": 0,
                               "params": {"pool_name": "沪深300"}}},
    "quote_collect": {"name": "📡 行情快照采集（盘中）", "func": job_quote_collect,
                      "default": {"enabled": False, "hour": 0, "minute": 0,
                                  "params": {"pool_name": "沪深300", "interval_sec": 30},
                                  "trigger": "interval"}},
    "sector_flow_collect": {"name": "🌐 板块资金流采集（盘中·资金趋势页供数）", "func": job_sector_flow_collect,
                            "default": {"enabled": False, "hour": 0, "minute": 0,
                                        "params": {"interval_sec": 30},
                                        "trigger": "interval"}},
    "loopengine": {"name": "🧬 LoopEngine 演化引擎", "func": job_loopengine,
                   "default": {"enabled": False, "hour": 0, "minute": 0,
                               "params": {"batch": 30, "interval_sec": 300},
                               "trigger": "interval"}},
    "top5_composite": {"name": "🏆 Top5 复合因子（每日合成）", "func": job_top5_composite,
                       "default": {"enabled": False, "hour": 18, "minute": 20, "params": {}}},
    "trade_simulate": {"name": "📈 模拟交易回填（每日）", "func": job_trade_simulate,
                       "default": {"enabled": False, "hour": 20, "minute": 5, "params": {}}},
    "position_track": {"name": "📦 持仓跟踪（盘中开平仓）", "func": job_position_track,
                       "default": {"enabled": False, "hour": 0, "minute": 0,
                                   "params": {"interval_sec": 300},
                                   "trigger": "interval"}},
    "auction_confirm": {"name": "🔔 竞价确认（09:26 对最新名单）", "func": job_auction_confirm,
                        "default": {"enabled": False, "hour": 9, "minute": 26, "params": {}}},
    "le_factor_eval": {"name": "🧪 LoopEngine 因子滚动体检（每晚一批）", "func": job_le_factor_eval,
                       "default": {"enabled": False, "hour": 21, "minute": 30,
                                   "params": {"batch": 60, "pool_name": "沪深300"}}},
    "event_mine": {"name": "🧬 事件定向挖因子（涨停等）", "func": job_event_mine,
                   "default": {"enabled": False, "hour": 22, "minute": 30,
                               "params": {"kind": "涨停", "batch": 30, "horizon": 5}}},
}


class SchedulerManager:
    def __init__(self):
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler

        # 线程隔离：loopengine/quote_collect 等高频 interval 任务走独立小线程池，
        # 否则它们长时间占满默认线程池，cron 任务(数据更新/扫描/回填)会被饿死跳过
        # ——2026-08-19~24 实测 pool_scan 连续缺席即此因。
        self.sched = BackgroundScheduler(
            timezone=TZ,
            executors={"default": ThreadPoolExecutor(3), "interval": ThreadPoolExecutor(2)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )
        self.sched.start()
        self._apply_state()

    # ---- 状态持久化 ----
    def _state(self) -> dict:
        saved = load_json(SCHED_STATE_FILE, {})
        return {k: {**v["default"], **saved.get(k, {})} for k, v in JOBS.items()}

    def _save_state(self, st_: dict):
        save_json(SCHED_STATE_FILE, st_)

    def _apply_state(self):
        state = self._state()
        for key, cfg in state.items():
            self.sched.remove_job(key) if self.sched.get_job(key) else None
            if not cfg["enabled"]:
                continue
            if cfg.get("trigger") == "interval":
                self.sched.add_job(lambda k=key: self._run(k), "interval", id=key,
                                   seconds=int(cfg["params"].get("interval_sec", 30)),
                                   executor="interval", replace_existing=True)
            else:
                self.sched.add_job(lambda k=key: self._run(k), "cron", id=key,
                                   day_of_week="mon-fri", hour=cfg["hour"], minute=cfg["minute"],
                                   replace_existing=True)

    # ---- 运行与记录 ----
    def _run(self, key: str):
        cfg = self._state()[key]
        try:
            msg = JOBS[key]["func"](**cfg.get("params", {}))
            ok, detail = True, msg
        except Exception as e:
            ok, detail = False, f"{e}"
            traceback.print_exc()
        last = load_json(SCHED_LAST_FILE, {})
        last[key] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "msg": detail}
        save_json(SCHED_LAST_FILE, last)
        hist = Path(SCHED_LAST_FILE).parent / "scheduler_history.jsonl"
        with hist.open("a") as f:
            f.write(json.dumps({"job": key, **last[key]}, ensure_ascii=False) + "\n")

    # ---- 对外 API ----
    def view(self) -> dict:
        state, last = self._state(), load_json(SCHED_LAST_FILE, {})
        out = {}
        for key, cfg in state.items():
            job = self.sched.get_job(key)
            out[key] = {**cfg, "label": JOBS[key]["name"],
                        "next": (job.next_run_time.strftime("%m-%d %H:%M") if job else None),
                        "last": last.get(key)}
        return out

    def set_enabled(self, key: str, enabled: bool):
        state = self._state()
        state[key]["enabled"] = enabled
        self._save_state(state)
        self._apply_state()

    def set_schedule(self, key: str, hour: int, minute: int):
        state = self._state()
        state[key].update(hour=hour, minute=minute)
        self._save_state(state)
        self._apply_state()

    def set_params(self, key: str, params: dict):
        state = self._state()
        state[key]["params"] = params
        self._save_state(state)

    def run_now(self, key: str):
        import threading

        threading.Thread(target=self._run, args=(key,), daemon=True).start()


@st.cache_resource
def get_scheduler() -> SchedulerManager:
    """Streamlit 进程级单例：容器存活期间调度器一直存在；容器停 = 调度停。"""
    return SchedulerManager()
