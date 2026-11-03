import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from ebook_translate import agent, core, mcp, wire
from test_workflow import fixture


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        first, self.book = self.root / 'first.epub', self.root / 'book.epub'
        fixture(first)
        with ZipFile(first) as src, ZipFile(self.book, 'w') as dst:
            for item in src.infolist():
                raw = src.read(item.filename)
                if item.filename == 'OEBPS/text/ch.xhtml':
                    raw = raw.replace(b'</body>', (''.join(f'<p>文章{i}。</p>' for i in range(8)) + '</body>').encode())
                dst.writestr(item, raw)
        self.job = agent.start(self.book, self.root)['job']
        self.batch = core.load(self.job)['batches'][0]

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, identifiers=None):
        return [{'id': u['id'], 'source_hash': u['source_hash'], 'translation': u['source'].replace('文', '译')}
                for u in self.batch['units'] if identifiers is None or u['id'] in identifiers]

    def initial(self, items):
        return agent.receive(self.job, self.batch['id'], json.dumps({'batch_id': self.batch['id'], 'items': items}, ensure_ascii=False))

    def response(self, step, items=None):
        items = copy.deepcopy(self.items(step['repair_unit_ids']) if items is None else items)
        if step.get('response_codec') == wire.SLOTS:
            for item in items:
                chunks = core.TOKEN.split(item.pop('translation'))
                item['parts'] = [{'id': f'p{index:03d}', 'translation': text} for index, text in enumerate(chunks, 1)]
        return json.dumps({'batch_id': self.batch['id'], 'request_id': step['request_id'],
                           'items': items}, ensure_ascii=False)

    def next(self):
        return agent.next_action(self.job)

    def test_partial_reply_retains_valid_items_and_repairs_only_missing(self):
        good = self.items()[:3]
        self.initial(good)
        step = self.next()
        self.assertEqual(step['retained_items'], 3)
        self.assertFalse(set(it['id'] for it in good) & set(step['repair_unit_ids']))
        self.assertLessEqual(len(step['repair_unit_ids']), 5)
        request_id = step['request_id']
        self.assertEqual(self.next()['request_id'], request_id)
        result = agent.receive(self.job, self.batch['id'], self.response(step))
        while result['action'] == 'translate_in_gemini_web':
            result = agent.receive(self.job, self.batch['id'], self.response(result))
        self.assertEqual(result['action'], 'review_translation')
        self.assertEqual(result['translation']['items'][:3], good)
        self.assertEqual([i['id'] for i in result['translation']['items']], [u['id'] for u in self.batch['units']])
        self.assertEqual(core.load(self.job)['batches'][0]['status'], 'review')

    def test_unparseable_reply_halves_until_single_then_stops(self):
        self.initial([])
        sizes = []
        for _ in range(8):
            step = self.next()
            if step['action'] == 'blocked':
                break
            sizes.append(len(step['repair_unit_ids']))
            agent.receive(self.job, self.batch['id'], '{broken')
        self.assertEqual(sizes, [5, 2, 1, 1, 1])
        self.assertEqual(self.next()['action'], 'blocked')
        self.assertTrue(self.next()['errors'])
        with self.assertRaises(core.JobError):
            agent.finish(self.job)
        resumed = agent.resume(self.job)
        self.assertEqual(resumed['action'], 'translate_in_gemini_web')
        self.assertEqual(len(resumed['repair_unit_ids']), 1)
        self.assertEqual(core.load(self.job)['batches'][0]['recovery']['history'][-1]['event'], 'retry_requested')

    def test_truncated_json_retains_only_complete_prefix(self):
        good = self.items()[:2]
        raw = '{"batch_id":"b0001","items":[' + ','.join(json.dumps(i) for i in good) + ',{"id":"unfinished'
        agent.receive(self.job, self.batch['id'], raw)
        self.assertEqual(self.next()['retained_items'], 2)
        self.assertEqual(core.load(self.job)['batches'][0]['recovery']['candidates'], {it['id']: it for it in good})

    def test_wrong_envelope_duplicate_id_hash_and_markers_not_salvaged(self):
        items = self.items()
        bad_id = items[0]['id']
        duplicated = dict(items[0], translation='ambiguous')
        items.append(duplicated)
        items[1]['translation'] = '保护标签被删'
        items[2]['source_hash'] = 'old'
        self.initial(items)
        stored = core.load(self.job)['batches'][0]['recovery']['candidates']
        self.assertNotIn(bad_id, stored)
        self.assertNotIn(items[1]['id'], stored)
        self.assertNotIn(items[2]['id'], stored)
        self.assertEqual(len(stored), 7)

    def test_old_request_does_not_replace_candidates_or_consume_budget(self):
        self.initial([])
        first = self.next()
        agent.receive(self.job, 'b0001', self.response(first))
        second = self.next()
        before = copy.deepcopy(core.load(self.job)['batches'][0]['recovery'])
        stale = agent.receive(self.job, 'b0001', self.response(first))
        self.assertIn('validation_error', stale)
        after = core.load(self.job)['batches'][0]['recovery']
        self.assertEqual(after['candidates'], before['candidates'])
        self.assertEqual(after['single_failures'], before['single_failures'])
        self.assertEqual(self.next()['request_id'], second['request_id'])

    def test_submitted_repair_resumes_same_conversation(self):
        self.initial([])
        step = self.next()
        agent.submitted(self.job, 'b0001', 'https://gemini.google.com/app/test-repair')
        resumed = self.next()
        self.assertEqual(resumed['action'], 'collect_gemini_response')
        self.assertEqual(resumed['submissions'][-1]['request_id'], step['request_id'])
        agent.receive(self.job, 'b0001', self.response(step))
        self.assertEqual(self.next()['retained_items'], len(step['repair_unit_ids']))

    def test_corrupted_unknown_request_id_shrinks_instead_of_looping(self):
        self.initial([])
        step = self.next()
        raw = json.loads(self.response(step))
        raw['request_id'] = 'model-corrupted-id'
        agent.receive(self.job, 'b0001', json.dumps(raw))
        after = self.next()
        self.assertEqual(after['retained_items'], 0)
        self.assertEqual(len(after['repair_unit_ids']), 2)

    def test_duplicate_envelope_and_invalid_suffix_cannot_supply_candidates(self):
        good = json.dumps(self.items()[:1])
        for raw in ('{"batch_id":"b0001","batch_id":"b0001","items":' + good + '}',
                    '{"batch_id":"b0001","items":' + good + ',"batch_id":broken'):
            from ebook_translate import recovery
            self.assertEqual(recovery.decoded_items(raw, 'b0001')[0], [])

    def test_cli_cannot_overwrite_active_recovery(self):
        self.initial([])
        before = core.load(self.job)
        with self.assertRaisesRegex(core.JobError, 'ebook_receive'):
            core.import_response(self.job, 'b0001', json.dumps({'batch_id': 'b0001', 'items': self.items()}))
        self.assertEqual(core.load(self.job), before)

    def test_semantic_repair_preserves_others_and_reassembles_for_review(self):
        self.initial(self.items())
        old = copy.deepcopy(core.load(self.job)['batches'][0]['translation']['items'])
        identifier = old[2]['id']
        step = mcp.call_tool('ebook_repair', {'job': self.job, 'batch_id': 'b0001', 'unit_ids_json': json.dumps([identifier]), 'reason': '这一句否定关系译反，需要重译'})
        self.assertEqual(step['repair_unit_ids'], [identifier])
        corrected = self.items([identifier])
        corrected[0]['translation'] = '只修改这一句。'
        result = agent.receive(self.job, 'b0001', self.response(step, corrected), provenance='human-edit')
        self.assertEqual(result['action'], 'review_translation')
        self.assertEqual(result['translation']['items'][:2], old[:2])
        self.assertEqual(result['translation']['items'][3:], old[3:])
        self.assertEqual(result['translation']['items'][2]['translation'], '只修改这一句。')
        self.assertEqual(core.load(self.job)['batches'][0]['provenance'], 'mixed')

    def test_semantic_repair_validation_is_atomic(self):
        self.initial(self.items())
        previous = core.load(self.job)
        for ids in ([], ['unknown'], [self.items()[0]['id']]*2):
            with self.assertRaises(core.JobError):
                agent.repair(self.job, 'b0001', json.dumps(ids), 'bad request')
            self.assertEqual(core.load(self.job), previous)

    def test_semantic_rejection_budget_is_per_unit_and_requires_explicit_resume(self):
        self.initial(self.items())
        identifier = self.items()[2]['id']
        for _ in range(3):
            step = agent.repair(self.job, 'b0001', json.dumps([identifier]), '否定含义不准确')
            self.assertEqual(step['action'], 'translate_in_gemini_web')
            self.assertEqual(agent.receive(self.job, 'b0001', self.response(step))['action'], 'review_translation')
        blocked = agent.repair(self.job, 'b0001', json.dumps([identifier]), '依然漏掉否定')
        self.assertEqual(blocked['action'], 'blocked')
        self.assertEqual(blocked['unit_ids'], [identifier])
        with self.assertRaises(core.JobError):
            agent.review(self.job, 'b0001', '不能绕过被拒绝的内容')
        resumed = agent.resume(self.job)
        self.assertEqual(resumed['action'], 'translate_in_gemini_web')
        state = core.load(self.job)['batches'][0]['recovery']
        self.assertEqual(state['semantic_rounds'][identifier], 1)
        self.assertEqual(len(state['candidates']), 9)

    def test_per_item_issues_survive_format_failure_without_cross_contamination(self):
        self.initial(self.items())
        ids = [self.items()[2]['id'], self.items()[3]['id']]
        reasons = {ids[0]: '否定漏译', ids[1]: '时间顺序颠倒'}
        step = agent.repair(self.job, 'b0001', json.dumps(ids), '逐项问题', json.dumps(reasons))
        agent.receive(self.job, 'b0001', '{broken')
        again = self.next()
        self.assertIn('否定漏译', again['prompt'])
        self.assertIn('request_id', again['prompt'])
        self.assertTrue(core.load(self.job)['batches'][0]['recovery']['format_errors'])
        self.assertNotIn('时间顺序颠倒', again['prompt'])
        self.assertEqual(core.load(self.job)['batches'][0]['recovery']['semantic_issues'], reasons)

    def test_blocked_group_does_not_give_another_unit_an_extra_round(self):
        self.initial(self.items())
        first, second = [item['id'] for item in self.items()[2:4]]
        for identifier, count in ((first, 3), (second, 2)):
            for _ in range(count):
                step = agent.repair(self.job, 'b0001', json.dumps([identifier]), '合成夹具复核未通过')
                agent.receive(self.job, 'b0001', self.response(step))
        blocked = agent.repair(self.job, 'b0001', json.dumps([first, second]), '两条依然未通过')
        self.assertEqual(blocked['action'], 'blocked')
        self.assertEqual(blocked['unit_ids'], [first])
        state = core.load(self.job)['batches'][0]['recovery']
        self.assertEqual(state['semantic_rounds'][second], 3)
        self.assertNotIn(second, state['candidates'])
        resumed = agent.resume(self.job)
        agent.receive(self.job, 'b0001', self.response(resumed))
        self.assertEqual(agent.repair(self.job, 'b0001', json.dumps([second]), '仍需修订')['action'], 'blocked')

    def test_submitted_legacy_request_keeps_original_protocol_until_collected(self):
        self.initial([])
        with core.locked(self.job):
            data = core.load(self.job)
            b = data['batches'][0]
            b['recovery']['request'].pop('codec')
            b['status'] = 'submitted'
            core.save(self.job, data)
        action = self.next()
        self.assertEqual(action['action'], 'collect_gemini_response')
        state = core.load(self.job)['batches'][0]['recovery']
        old_id = state['request']['id']
        raw = json.dumps({'batch_id': 'b0001', 'request_id': old_id, 'items': self.items(state['request']['unit_ids'])})
        agent.receive(self.job, 'b0001', raw)
        self.assertEqual(self.next()['retained_items'], 5)

    def test_unsent_legacy_request_upgrades_and_rejects_its_late_reply(self):
        self.initial([])
        with core.locked(self.job):
            data = core.load(self.job)
            state = data['batches'][0]['recovery']
            state['request'].pop('codec')
            old_id = state['request']['id']
            core.save(self.job, data)
        upgraded = self.next()
        self.assertNotEqual(upgraded['request_id'], old_id)
        self.assertEqual(upgraded['response_codec'], wire.SLOTS)
        before = core.load(self.job)['batches'][0]['recovery']['single_failures']
        late = json.dumps({'batch_id': 'b0001', 'request_id': old_id, 'items': self.items()})
        self.assertIn('validation_error', agent.receive(self.job, 'b0001', late))
        self.assertEqual(core.load(self.job)['batches'][0]['recovery']['single_failures'], before)
        self.assertEqual(self.next()['request_id'], upgraded['request_id'])

    def test_repaired_batch_exports_once_with_per_item_sources(self):
        self.initial(self.items()[:1] + self.items()[2:])
        step = self.next()
        self.assertEqual(step['response_codec'], wire.SLOTS)
        review = agent.receive(self.job, 'b0001', self.response(step))
        self.assertEqual(review['slot_review'][0]['unit_id'], self.items()[1]['id'])
        self.assertTrue(all('source' in part and 'translation' in part for part in review['slot_review'][0]['parts']))
        agent.review(self.job, 'b0001', '合成夹具逐项核对修复结果')
        for b in core.load(self.job)['batches'][1:]:
            raw = {'batch_id': b['id'], 'items': [{'id': u['id'], 'source_hash': u['source_hash'], 'translation': u['source'].replace('文', '译')} for u in b['units']]}
            agent.receive(self.job, b['id'], json.dumps(raw))
            agent.review(self.job, b['id'], '合成夹具验证')
        result = agent.finish(self.job)
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        recovered = report['batches'][0]['recovery']
        self.assertEqual(len(recovered['item_sources']), 10)
        self.assertEqual(recovered['item_sources'][self.items()[1]['id']]['codec'], wire.SLOTS)
        self.assertEqual(report['translated_units'], 14)
        self.assertTrue(all((Path(self.job)/entry['raw_file']).is_file() for entry in recovered['item_sources'].values()))


if __name__ == '__main__':
    unittest.main()
