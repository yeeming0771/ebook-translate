"""Lossless per-item salvage and bounded adaptive repair for text batches.

Never invent or rewrite model text. Only independently validated, unambiguous
items are retained. Every candidate points to an archived original response.
"""

from collections import Counter
import json
from pathlib import Path
import re

from . import core, wire

MAX_SINGLE_FAILURES = 3
MAX_SEMANTIC_REPAIRS = 3


def semantic_rounds(state):
    if 'semantic_rounds' not in state:
        state['semantic_rounds'] = dict(Counter(identifier for event in state['history']
                                                if event['event'] == 'semantic_repair_requested'
                                                for identifier in event['unit_ids']))
    return state['semantic_rounds']


def semantic_issues(state):
    if 'semantic_issues' not in state:
        state['semantic_issues'] = {}
        for event in state['history']:
            if event['event'] in {'semantic_repair_requested', 'semantic_repair_blocked'}:
                for identifier in event['unit_ids']:
                    state['semantic_issues'][identifier] = (event.get('reasons') or {}).get(identifier, event['reason'])
    return state['semantic_issues']


def decoded_items(raw, batch_id):
    """Read complete JSON, or a provably complete prefix of its items array.

    Truncated/malformed strings are never repaired or scanned for guessed items.
    Duplicate envelope keys and a different batch invalidate the entire reply.
    """
    value = raw.strip().lstrip('\ufeff')
    if value.startswith('```'):
        value = re.sub(r'\A```(?:json)?\s*|\s*```\Z', '', value)
    decoder = json.JSONDecoder(object_pairs_hook=core.pairs_unique)
    try:
        result = decoder.decode(value)
    except core.JobError:
        return [], None, 'duplicate_json_key'
    except json.JSONDecodeError:
        pass
    else:
        if not isinstance(result, dict) or result.get('batch_id') != batch_id or not isinstance(result.get('items'), list):
            return [], None, 'invalid_envelope'
        return result['items'], result.get('request_id'), 'complete_json'

    def whitespace(pos):
        while pos < len(value) and value[pos].isspace():
            pos += 1
        return pos

    # Parse the envelope in order. For prefix salvage batch_id (and any repair
    # request_id) must appear before items; otherwise identity is unproven.
    position = whitespace(0)
    if value[position:position+1] != '{':
        return [], None, 'invalid_json'
    position += 1
    fields = {}
    try:
        while True:
            key, position = decoder.raw_decode(value, whitespace(position))
            if not isinstance(key, str) or key in fields:
                return [], None, 'invalid_envelope'
            position = whitespace(position)
            if value[position:position+1] != ':':
                return [], None, 'invalid_envelope'
            position = whitespace(position + 1)
            if key == 'items':
                if fields.get('batch_id') != batch_id or value[position:position+1] != '[':
                    return [], None, 'invalid_envelope'
                items = []
                position += 1
                while True:
                    position = whitespace(position)
                    if value[position:position+1] == ']':
                        return (items, fields.get('request_id'), 'missing_envelope_closure') if not value[position+1:].strip() else ([], None, 'invalid_envelope_suffix')
                    try:
                        item, position = decoder.raw_decode(value, position)
                    except (ValueError, core.JobError):
                        return items, fields.get('request_id'), 'complete_prefix_only'
                    items.append(item)
                    position = whitespace(position)
                    if value[position:position+1] == ']':
                        return (items, fields.get('request_id'), 'missing_envelope_closure') if not value[position+1:].strip() else ([], None, 'invalid_envelope_suffix')
                    if value[position:position+1] != ',':
                        return items, fields.get('request_id'), 'complete_prefix_only'
                    position += 1
            fields[key], position = decoder.raw_decode(value, position)
            position = whitespace(position)
            if value[position:position+1] != ',':
                return [], None, 'invalid_envelope'
            position += 1
    except (ValueError, core.JobError):
        return [], None, 'invalid_json'


def retain(batch, raw, target, event, scope, request_id=None):
    items, returned_request, parse_mode = decoded_items(raw, batch['id'])
    state = batch['recovery']
    if request_id is not None and returned_request != request_id:
        known_old = any(event.get('request_id') == returned_request for event in state['history']
                        if event.get('request_id') is not None)
        if known_old:
            return set(), {'_reply': 'request_id 属于旧请求；未合并'}, parse_mode
        return set(), {identifier: '缺少或损坏本次 request_id，回复未合并' for identifier in scope}, parse_mode
    counts = Counter(item.get('id') for item in items if isinstance(item, dict) and isinstance(item.get('id'), str))
    originals = {u['id']: u for u in batch['units']}
    retained, errors = set(), {}
    for identifier in scope:
        if counts[identifier] != 1:
            errors[identifier] = '缺少条目' if counts[identifier] == 0 else '重复 ID，无法判断应采用哪项'
            continue
        item = next(it for it in items if isinstance(it, dict) and it.get('id') == identifier)
        try:
            if event.get('codec') == wire.SLOTS:
                item = wire.materialize(originals[identifier], item)
            core.validate_response({'id': batch['id'], 'units': [originals[identifier]]},
                                   json.dumps({'batch_id': batch['id'], 'items': [item]}, ensure_ascii=False), target)
        except core.JobError as exc:
            errors[identifier] = str(exc)
            continue
        # Slot text is copied unchanged; only original format boundaries are inserted.
        state['candidates'][identifier] = {k: item[k] for k in ('id', 'source_hash', 'translation')}
        state['item_sources'][identifier] = {'raw_file': event['raw_file'], 'provenance': event['provenance']}
        if event.get('codec'):
            state['item_sources'][identifier]['codec'] = event['codec']
        retained.add(identifier)
    return retained, errors, parse_mode


def assemble(batch, target):
    state = batch['recovery']
    if not all(u['id'] in state['candidates'] for u in batch['units']):
        batch.update(status='needs_revision', translation=None)
        return False
    result = {'batch_id': batch['id'], 'items': [state['candidates'][u['id']] for u in batch['units']]}
    _, warnings = core.validate_response(batch, json.dumps(result, ensure_ascii=False), target)
    origins = {entry['provenance'] for entry in state['item_sources'].values()}
    batch.update(status='review', translation=result, warnings=warnings,
                 provenance=next(iter(origins)) if len(origins) == 1 else 'mixed')
    return True


def begin(job, batch_id):
    """Migrate a failed legacy batch by replaying archived, hash-matching items."""
    with core.locked(job):
        data = core.load(job)
        batch = core.get_batch(data, batch_id)
        if 'recovery' in batch or batch['status'] != 'needs_revision':
            return
        batch['recovery'] = {'candidates': {}, 'item_sources': {}, 'limit': max(1, (len(batch['units']) + 1)//2),
                             'serial': 0, 'single_failures': {}, 'errors': {}, 'history': []}
        scope = [u['id'] for u in batch['units']]
        for event in batch['attempts']:
            if event['event'] != 'import':
                continue
            raw = (Path(job) / event['raw_file']).read_text(encoding='utf-8')
            retained, errors, mode = retain(batch, raw, data['target'], event, scope)
            batch['recovery']['history'].append({'event': 'salvage', 'raw_file': event['raw_file'],
                                                'retained': sorted(retained), 'parse_mode': mode})
            batch['recovery']['errors'].update(errors)
        assemble(batch, data['target'])
        core.save(job, data)


def plan(job, batch_id):
    with core.locked(job):
        data = core.load(job)
        batch = core.get_batch(data, batch_id)
        state = batch['recovery']
        unresolved = [u for u in batch['units'] if u['id'] not in state['candidates']]
        semantic_issues(state)
        if state.get('semantic_blocked'):
            return {'action': 'blocked', 'reason': '同一条目经三轮语义修订仍未通过复核，保留进度',
                    'unit_ids': state['semantic_blocked'],
                    'errors': {identifier: state['errors'].get(identifier) for identifier in state['semantic_blocked']}}
        exhausted = [u['id'] for u in unresolved if state['single_failures'].get(u['id'], 0) >= MAX_SINGLE_FAILURES]
        if exhausted:
            return {'action': 'blocked', 'reason': '单条局部补译连续失败，保留进度', 'unit_ids': exhausted,
                    'errors': {identifier: state['errors'].get(identifier) for identifier in exhausted}}
        if not unresolved:
            raise core.JobError('没有待修复条目')
        # Upgrade only an unsent legacy request. Submitted work must be collected
        # with its original protocol and conversation, never silently replaced.
        if 'request' in state and 'codec' not in state['request'] and batch['status'] != 'submitted':
            state['history'].append({'event': 'protocol_upgrade', 'request_id': state['request']['id'], 'at': core.now()})
            state.pop('request')
        if 'request' not in state:
            selected = unresolved[:state['limit']]
            state['serial'] += 1
            codec = wire.SLOTS if any(core.TOKEN.search(u['source']) for u in selected) else 'items-v1'
            fingerprint = core.digest(json.dumps([codec, [(u['id'], u['source_hash']) for u in selected]]).encode())[:12]
            state['request'] = {'id': f"{batch_id}-r{state['serial']:04d}-{fingerprint}",
                                'unit_ids': [u['id'] for u in selected], 'codec': codec, 'created_at': core.now()}
            core.save(job, data)
        request = state['request']
        units = [u for u in batch['units'] if u['id'] in request['unit_ids']]
        subset = dict(batch, units=units)
        issues = {u['id']: state['semantic_issues'].get(u['id'], state['errors'].get(u['id'], '尚未取得有效译文')) for u in units}
        prompt = (wire.prompt(data, subset, request['id'], issues) if request.get('codec') == wire.SLOTS
                  else core.render_prompt(data, subset, request['id']))
        prompt += ('\n这是局部修复请求，只返回本次列出的条目，不得重发整批或已通过条目。'
                   f"\n输出 JSON 顶层字段顺序必须为 batch_id、request_id、items，其中 request_id={json.dumps(request['id'])}。"
                   '\n待修复问题：' + json.dumps(issues, ensure_ascii=False))
        format_issues = {u['id']: state.get('format_errors', {}).get(u['id']) for u in units
                         if state.get('format_errors', {}).get(u['id'])}
        if format_issues:
            prompt += '\n上次回复的结构错误（本次按当前协议纠正）：' + json.dumps(format_issues, ensure_ascii=False)
        # Nearby source helps translation of isolated sentences; not output items.
        indexes = [i for i, u in enumerate(batch['units']) if u['id'] in request['unit_ids']]
        neighbors = sorted({index + offset for index in indexes for offset in (-1, 1)})
        context = [{'id': batch['units'][i]['id'], 'source': core.TOKEN.sub('', batch['units'][i]['source'])[:500]}
                   for i in neighbors
                   if 0 <= i < len(batch['units']) and batch['units'][i]['id'] not in request['unit_ids']]
        prompt += '\n相邻原文仅供语境，不要输出：' + json.dumps(context, ensure_ascii=False)
        return {'action': 'translate_in_gemini_web', 'prompt': prompt, 'request_id': request['id'],
                'response_codec': request.get('codec', 'items-v1'),
                'repair_unit_ids': request['unit_ids'], 'retained_items': len(state['candidates']),
                'remaining_items': len(unresolved), 'fresh_conversation': True}


def receive(job, batch_id, raw, provenance):
    if provenance not in {'gemini-web', 'human-edit'}:
        raise core.JobError('无效译文来源')
    with core.locked(job):
        data = core.load(job)
        batch = core.get_batch(data, batch_id)
        state = batch['recovery']
        if batch['status'] == 'accepted' or 'request' not in state:
            raise core.JobError('没有活动修复请求，或批次已验收')
        request = state['request']
        name = f"{batch_id}-repair-{state['serial']:04d}-{len(batch['attempts'])+1:04d}-{core.digest(raw.encode('utf-8'))[:12]}.txt"
        (Path(job) / 'responses' / name).write_text(raw, encoding='utf-8')
        event = {'event': 'import', 'at': core.now(), 'raw_file': f'responses/{name}',
                 'provenance': provenance, 'request_id': request['id'], 'unit_ids': request['unit_ids']}
        if request.get('codec'):
            event['codec'] = request['codec']
        retained, errors, mode = retain(batch, raw, data['target'], event, request['unit_ids'], request['id'])
        event.update(parse_mode=mode, retained=sorted(retained), errors=errors)
        if errors:
            event['error'] = '局部回复未完全通过校验'
        batch['attempts'].append(event)
        batch.pop('approval', None)
        if '_reply' in errors:
            # Stale replies neither replace candidates nor consume retry budget.
            core.save(job, data)
            return {'validation_error': errors['_reply']}
        failed = set(request['unit_ids']) - retained
        if failed:
            state['limit'] = max(1, len(request['unit_ids'])//2)
            if len(request['unit_ids']) == 1:
                identifier = request['unit_ids'][0]
                state['single_failures'][identifier] = state['single_failures'].get(identifier, 0) + 1
        # Keep semantic findings separate from transport/format errors.
        state.setdefault('format_errors', {}).update(errors)
        for identifier in errors:
            if identifier not in state.get('semantic_issues', {}):
                state['errors'][identifier] = errors[identifier]
        state['history'].append({'event': 'repair', 'request_id': request['id'], 'retained': sorted(retained),
                                 'failed': sorted(failed), 'next_limit': state['limit'], 'raw_file': event['raw_file']})
        state.pop('request')
        assembled = assemble(batch, data['target'])
        core.save(job, data)
        return {'assembled': assembled, 'retained': len(retained), 'failed': sorted(failed)}


def request_repair(job, batch_id, unit_ids, reason, reasons=None):
    if not isinstance(unit_ids, list) or not unit_ids or not all(isinstance(x, str) for x in unit_ids) or len(set(unit_ids)) != len(unit_ids):
        raise core.JobError('unit_ids 必须是非空且不重复的 ID 数组')
    if not reason.strip():
        raise core.JobError('必须记录局部修复原因')
    if reasons is not None and (not isinstance(reasons, dict) or set(reasons) != set(unit_ids)
                               or not all(isinstance(value, str) and value.strip() for value in reasons.values())):
        raise core.JobError('逐项原因必须完整对应指定 ID，且每项为非空字符串')
    with core.locked(job):
        data = core.load(job)
        batch = core.get_batch(data, batch_id)
        if batch['status'] != 'review':
            raise core.JobError('语义局部修复只允许对待复核批次发起')
        if set(unit_ids) - {u['id'] for u in batch['units']}:
            raise core.JobError('存在未知条目 ID')
        if 'recovery' not in batch:
            event = next(e for e in reversed(batch['attempts']) if e['event'] == 'import')
            batch['recovery'] = {'candidates': {it['id']: it for it in batch['translation']['items']},
                                 'item_sources': {it['id']: {'raw_file': event['raw_file'], 'provenance': batch['provenance']} for it in batch['translation']['items']},
                                 'serial': 0, 'single_failures': {}, 'errors': {}, 'history': []}
        state = batch['recovery']
        semantic_issues(state)
        rounds = semantic_rounds(state)
        exhausted = [identifier for identifier in unit_ids if rounds.get(identifier, 0) >= MAX_SEMANTIC_REPAIRS]
        state.pop('request', None)
        for identifier in unit_ids:
            state['candidates'].pop(identifier, None)
            state['item_sources'].pop(identifier, None)
            state['errors'][identifier] = reasons[identifier] if reasons is not None else reason
            state.setdefault('semantic_issues', {})[identifier] = state['errors'][identifier]
            state.get('format_errors', {}).pop(identifier, None)
            state['single_failures'].pop(identifier, None)
            if identifier not in exhausted:
                rounds[identifier] = rounds.get(identifier, 0) + 1
        state['limit'] = min(3, len(unit_ids))
        state['semantic_blocked'] = exhausted
        state['history'].append({'event': 'semantic_repair_blocked' if exhausted else 'semantic_repair_requested',
                                 'unit_ids': unit_ids, 'reason': reason, 'reasons': reasons, 'at': core.now()})
        batch.update(status='needs_revision', translation=None)
        batch.pop('approval', None)
        core.save(job, data)
