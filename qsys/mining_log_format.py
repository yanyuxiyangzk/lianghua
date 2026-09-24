"""Readable ordered mining logs, without inventing missing candidate numbers."""
STEPS = ['构建面板', '机制族引导', 'FSA重算', '生成候选', '规则审查',
         'LLM审查', '去重', 'FSA拦截', '硬闸门', '入库']
STATUS = {'running':'开始执行', 'done':'完成', 'pass':'通过', 'fail':'未通过',
          'skip':'跳过', 'dup':'发现重复，跳过本候选', 'frozen':'已拦截，跳过本候选', 'error':'发生异常'}
SOURCES = {'random':'随机生成', 'mutate':'变异生成', 'crossover':'交叉组合',
           'perturb':'扰动生成', 'llm':'模型生成'}


def log_line(e):
    kind = e.get('type')
    prefix = str(e.get('ts', '')).replace('T', ' ') + '  '
    name = e.get('name') or (STEPS[e['step']-1] if isinstance(e.get('step'), int) and 1 <= e['step'] <= 10 else '步骤')
    if kind == 'round_start':
        text = f"开始第 {e.get('iteration', '?')} 轮 · {e.get('factor_type', '类型未记录')} · 计划生成 {e.get('batch', '?')} 个候选"
    elif kind == 'round_complete':
        s = e.get('stats') or {}
        text = f"第 {e.get('iteration', '?')} 轮完成 · {s.get('factor_type', '')} · 测试 {s.get('tested', 0)} 个，入库 {s.get('passed', 0)} 个，重复 {s.get('dup', 0)} 个，拦截 {s.get('frozen', 0)} 个"
    elif kind == 'step_update':
        text = f"{e.get('step', '')}. {name}：{STATUS.get(e.get('status'), e.get('status', '更新'))}"
        if e.get('source'):
            text += ' · ' + SOURCES.get(e['source'], e['source'])
        if e.get('gaps'):
            text += ' · 优先探索：' + '、'.join(e['gaps'])
        if e.get('proven'):
            text += ' · 已有实战表现的机制族：' + '、'.join(e['proven'])
        if e.get('regime'):
            text += ' · 市场环境：' + str(e['regime'])
    elif kind == 'gate_eval':
        text = f"硬闸门评估：{e.get('factor_name', '候选因子')} · {'通过' if e.get('passed') else '未通过'}"
    elif kind == 'gate_pass':
        text = '因子入库：' + str(e.get('factor_name', ''))
    elif kind in ('review_result', 'llm_result'):
        text = ('规则审查' if kind == 'review_result' else 'LLM 风险审查') + ('：通过' if e.get('passed') else '：未通过／风险标记')
    else:
        text = {'candidate_gen':'生成候选'}.get(kind, '挖掘进度更新')
    reason = e.get('reason') or e.get('error') or e.get('skip_reason')
    if reason == 'llm-error-fallback':
        text = 'LLM 风险审查：调用失败，已走回退处理（不代表模型审查通过）'
    elif reason:
        text += ' · 原因：' + str(reason)
    return prefix + text


def round_sections(events):
    """Display latest available round, preparation then separate candidates."""
    start = max((i for i,e in enumerate(events) if e.get('type') == 'round_start'), default=-1)
    current = events[start:] if start >= 0 else events
    batch = current[0].get('batch') if start >= 0 else None
    preparation, candidates = [], []
    ended = False
    for e in current:
        if e.get('type') == 'round_complete':
            ended = True
            continue
        if e.get('type') == 'round_start':
            preparation.append(e)
            continue
        step = e.get('step')
        if e.get('type') == 'step_update' and step == 4:
            left = e.get('batch_left')
            number = batch-left+1 if isinstance(batch,int) and isinstance(left,int) else None
            candidates.append({'number':number,'remaining':left,'events':[]})
        if isinstance(step,int) and step <= 3:
            preparation.append(e)
        else:
            if not candidates:
                candidates.append({'number':None,'remaining':None,'events':[]})
            candidates[-1]['events'].append(e)
    return dict(preparation=preparation, candidates=candidates, ended=ended,
                partial=start<0, batch=batch)
