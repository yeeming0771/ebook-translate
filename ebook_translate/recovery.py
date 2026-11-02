"""Lossless per-item salvage and bounded adaptive repair for text batches.

Never invent or rewrite model text. Only independently validated, unambiguous
items are retained. Every candidate points to an archived original response.
"""

from collections import Counter
import json
from pathlib import Path
import re

from . import core

MAX_SINGLE_FAILURES = 3


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
            core.validate_response({'id': batch['id'], 'units': [originals[identifier]]},
                                   json.dumps({'batch_id': batch['id'], 'items': [item]}, ensure_ascii=False), target)
        except core.JobError as exc:
            errors[identifier] = str(exc)
            continue
        # Canonical keys only; translation is copied byte-for-byte as a string.
        state['candidates'][identifier] = {k: item[k] for k in ('id', 'source_hash', 'translation')}
        state['item_sources'][identifier] = {'raw_file': event['raw_file'], 'provenance': event['provenance']}
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
        exhausted = [u['id'] for u in unresolved if state['single_failures'].get(u['id'], 0) >= MAX_SINGLE_FAILURES]
        if exhausted:
            return {'action': 'blocked', 'reason': '单条局部补译连续失败，保留进度', 'unit_ids': exhausted,
                    'errors': {identifier: state['errors'].get(identifier) for identifier in exhausted}}
        if not unresolved:
            raise core.JobError('没有待修复条目')
        if 'request' not in state:
            selected = unresolved[:state['limit']]
            state['serial'] += 1
            fingerprint = core.digest(json.dumps([(u['id'], u['source_hash']) for u in selected]).encode())[:12]
            state['request'] = {'id': f"{batch_id}-r{state['serial']:04d}-{fingerprint}",
                                'unit_ids': [u['id'] for u in selected], 'created_at': core.now()}
            core.save(job, data)
        request = state['request']
        units = [u for u in batch['units'] if u['id'] in request['unit_ids']]
        subset = dict(batch, units=units)
        prompt = core.render_prompt(data, subset, request['id'])
        prompt += ('\n这是局部修复请求，只返回本次列出的条目，不得重发整批或已通过条目。'
                   f"\n输出 JSON 顶层字段顺序必须为 batch_id、request_id、items，其中 request_id={json.dumps(request['id'])}。"
                   '\n待修复问题：' + json.dumps({u['id']: state['errors'].get(u['id'], '尚未取得有效译文') for u in units}, ensure_ascii=False))
        # Nearby source helps translation of isolated sentences; not output items.
        indexes = [i for i, u in enumerate(batch['units']) if u['id'] in request['unit_ids']]
        context = [batch['units'][i]['source'][:500] for i in sorted({min(indexes)-1, max(indexes)+1})
                   if 0 <= i < len(batch['units']) and batch['units'][i]['id'] not in request['unit_ids']]
        prompt += '\n相邻原文仅供语境，不要输出：' + json.dumps(context, ensure_ascii=False)
        return {'action': 'translate_in_gemini_web', 'prompt': prompt, 'request_id': request['id'],
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
        state['errors'].update(errors)
        state['history'].append({'event': 'repair', 'request_id': request['id'], 'retained': sorted(retained),
                                 'failed': sorted(failed), 'next_limit': state['limit'], 'raw_file': event['raw_file']})
        state.pop('request')
        assembled = assemble(batch, data['target'])
        core.save(job, data)
        return {'assembled': assembled, 'retained': len(retained), 'failed': sorted(failed)}


def request_repair(job, batch_id, unit_ids, reason):
    if not isinstance(unit_ids, list) or not unit_ids or not all(isinstance(x, str) for x in unit_ids) or len(set(unit_ids)) != len(unit_ids):
        raise core.JobError('unit_ids 必须是非空且不重复的 ID 数组')
    if not reason.strip():
        raise core.JobError('必须记录局部修复原因')
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
        state.pop('request', None)
        for identifier in unit_ids:
            state['candidates'].pop(identifier, None)
            state['item_sources'].pop(identifier, None)
            state['errors'][identifier] = reason
            state['single_failures'].pop(identifier, None)
        state['limit'] = min(3, len(unit_ids))
        state['history'].append({'event': 'semantic_repair_requested', 'unit_ids': unit_ids, 'reason': reason, 'at': core.now()})
        batch.update(status='needs_revision', translation=None)
        batch.pop('approval', None)
        core.save(job, data)
