from typing import List

import pandas as pd

from rdagent.components.coder.CoSTEER.evaluators import CoSTEERMultiFeedback
from rdagent.core.conf import RD_AGENT_SETTINGS
from rdagent.core.exception import FactorEmptyError
from rdagent.core.utils import multiprocessing_wrapper
from rdagent.log import rdagent_logger as logger
from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorExperiment


def _rss_gb() -> float:
    """本地插桩：读 /proc/self/statm 取 RSS GB（定位 SOTA 处理 OOM 点）。"""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * 4096 / 1073741824
    except Exception:
        return -1.0


def align_concat_cols(factor_dfs: List[pd.DataFrame]) -> pd.DataFrame:
    """本地补丁：替代 pd.concat(axis=1) 的内存友好列合并。

    pd.concat(axis=1) 会对每个 1400 万行 MultiIndex 做 outer join：
    索引并集/reindex 引擎逐帧叠加且不及时释放，实测 RSS 2.1GB → >12GB 被 OOM Kill；
    手动 np.full + 逐帧 union 同样爆（union 每帧 +0.6~1.3GB 不还内存）。
    这里以最长帧索引为基准左连接：索引引擎只在基准上建一次并缓存复用，
    不在基准内的行（必含 NaN）直接丢弃——下游 dropna 后与 outer join 语义等价。
    峰值 ≈ 父进程 + 结果矩阵 + 单个索引引擎（≈4GB）。
    """
    import numpy as np

    base_i = max(range(len(factor_dfs)), key=lambda i: len(factor_dfs[i]))
    base = factor_dfs[base_i]
    base_idx = base.index
    col_names = [c for d in factor_dfs for c in d.columns]
    widths = [d.shape[1] for d in factor_dfs]
    arr = np.full((len(base_idx), sum(widths)), np.nan, dtype="float32")
    off = 0
    for d, w in zip(factor_dfs, widths):
        if d.index.equals(base_idx):
            arr[:, off : off + w] = d.to_numpy(dtype="float32", copy=False)
        else:
            idxer = base_idx.get_indexer(d.index)
            keep = idxer >= 0  # 不在基准索引内的行：本帧独有（其它帧必 NaN），下游 dropna 等价丢弃
            arr[idxer[keep], off : off + w] = d.to_numpy(dtype="float32", copy=False)[keep]
        off += w
    return pd.DataFrame(arr, index=base_idx, columns=col_names)


def process_factor_data(exp_or_list: List[QlibFactorExperiment] | QlibFactorExperiment) -> pd.DataFrame:
    """
    Process and combine factor data from experiment implementations.

    Args:
        exp (ASpecificExp): The experiment containing factor data.

    Returns:
        pd.DataFrame: Combined factor data without NaN values.
    """
    if isinstance(exp_or_list, QlibFactorExperiment):
        exp_or_list = [exp_or_list]
    factor_dfs = []
    logger.info(f"[MEM] process_factor_data 入口 RSS={_rss_gb():.2f}GB, 实验数={len(exp_or_list)}")

    # Collect all exp's dataframes
    for exp in exp_or_list:
        if isinstance(exp, QlibFactorExperiment):
            if len(exp.sub_tasks) > 0:
                # if it has no sub_tasks, the experiment is results from template project.
                # otherwise, it is developed with designed task. So it should have feedback.
                assert isinstance(exp.prop_dev_feedback, CoSTEERMultiFeedback)
                # Iterate over sub-implementations and execute them to get each factor data
                message_and_df_list = multiprocessing_wrapper(
                    [
                        (implementation.execute, ("All",))
                        for implementation, fb in zip(exp.sub_workspace_list, exp.prop_dev_feedback)
                        if implementation and fb
                    ],  # only execute successfully feedback
                    n=RD_AGENT_SETTINGS.multi_proc_n,
                )
                logger.info(f"[MEM] 实验因子执行完(父进程收到结果) RSS={_rss_gb():.2f}GB")
                error_message = ""
                for i, (message, df) in enumerate(message_and_df_list):
                    # Check if factor generation was successful
                    if df is not None and "datetime" in df.index.names:
                        time_diff = df.index.get_level_values("datetime").to_series().diff().dropna().unique()
                        if pd.Timedelta(minutes=1) not in time_diff:
                            # 本地补丁：float64 -> float32，并回写 list 替换掉原件引用。
                            # qlib 原生数据本就是 float32，精度无损、内存减半；
                            # 不回写则 float64 原件被 message_and_df_list 持有至整个
                            # exp 处理完，副本与原件同时驻留，SOTA 因子库逐轮累积后
                            # 在 concat 时撑爆 WSL2 内存（OOM Kill）。
                            float64_cols = df.select_dtypes("float64").columns
                            if len(float64_cols) > 0:
                                df = df.astype({c: "float32" for c in float64_cols}, copy=False)
                                message_and_df_list[i] = (message, df)
                            factor_dfs.append(df)
                            logger.info(
                                f"Factor data from {exp.hypothesis.concise_justification} is successfully generated."
                            )
                        else:
                            logger.warning(f"Factor data from {exp.hypothesis.concise_justification} is not generated.")
                    else:
                        error_message += f"Factor data from {exp.hypothesis.concise_justification} is not generated because of {message}"
                        logger.warning(
                            f"Factor data from {exp.hypothesis.concise_justification} is not generated because of {message}"
                        )

    # Combine all successful factor data
    if factor_dfs:
        n_rows = sum(len(d) for d in factor_dfs) / max(len(factor_dfs), 1)
        logger.info(
            f"[MEM] concat 前 RSS={_rss_gb():.2f}GB, 因子数={len(factor_dfs)}, "
            f"平均行数≈{int(n_rows)}, 估计副本≈{len(factor_dfs) * n_rows * 4 / 1073741824:.1f}GB"
        )
        out = align_concat_cols(factor_dfs)
        del factor_dfs
        logger.info(f"[MEM] concat(基准左连接) 后 RSS={_rss_gb():.2f}GB, 形状={out.shape}")
        return out
    else:
        raise FactorEmptyError(
            f"No valid factor data found to merge (in process_factor_data) because of {error_message}."
        )
