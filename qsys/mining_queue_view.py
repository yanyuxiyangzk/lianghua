"""Presentation of recorded queue states, with explicit unknown legacy state."""
LABELS = {'queued':'排队中', 'preparing':'准备中', 'running':'运行中',
          'round_complete':'本轮完成', 'skipped':'已跳过', 'failed':'失败',
          'not_run':'未执行（批次中断）'}


def queue_rows(progress, types):
    progress = progress or {}
    queue = progress.get('queue')
    if isinstance(queue, list) and queue:
        rows = [{'轮动轮次': item['rotation'], '因子类型': item['factor_type'],
                 '状态': LABELS.get(item.get('status'), '待确认'),
                 '说明': item.get('reason', '')} for item in queue]
        next_item = next((item for item in queue if item.get('status') == 'queued'), None)
        next_label = (f"第 {next_item['rotation']} 轮 · {next_item['factor_type']}（排队中）"
                      if next_item else '没有剩余排队项')
        return rows, next_label
    rows = [{'轮动轮次':'待确认', '因子类型':ft,
             '状态':LABELS.get(progress.get('status'), '待确认')
                    if ft == progress.get('factor_type') else '待确认',
             '说明':'旧批次未提供完整队列记录'} for ft in types]
    return rows, '待确认：当前批次未提供完整队列，不能据目录顺序推断下一项'
