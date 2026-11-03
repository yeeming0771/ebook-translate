import copy
import json
import unittest

from ebook_translate import core, wire


class TextSlotTests(unittest.TestCase):
    def setUp(self):
        self.root = core.parse('<p xmlns="http://www.w3.org/1999/xhtml">雨に<ruby><rb>晒</rb><rt>さら</rt></ruby>され、<a href="#note"><em>声</em></a>を聞いた。<span id="note"/></p>'.encode())
        source, _ = core.slots(self.root)
        self.unit = {'id': 'u1', 'path': [], 'field': None, 'source': source, 'source_hash': core.digest(source.encode())}
        self.item = {'id': 'u1', 'source_hash': self.unit['source_hash'], 'parts': []}
        mapping = {'雨に': '被雨', '晒': '淋', 'され、': '着，', '声': '声音', 'を聞いた。': '传入耳中。'}
        for part in wire.parts(self.unit):
            self.item['parts'].append({'id': part['id'], 'translation': mapping.get(part['source'], part['source'])})

    def test_materialization_preserves_model_text_and_original_tree(self):
        item = wire.materialize(self.unit, self.item)
        core.validate_response({'id': 'b1', 'units': [self.unit]}, json.dumps({'batch_id': 'b1', 'items': [item]}), 'zh-CN')
        self.assertEqual(core.TOKEN.sub('', item['translation']), ''.join(p['translation'] for p in self.item['parts']))
        core.apply_translation(self.root, self.unit, item['translation'])
        self.assertEqual(self.root[0][0].text, '淋')
        self.assertEqual(self.root[0][1].text, 'さら')
        self.assertEqual(self.root[1].get('href'), '#note')
        self.assertEqual(self.root[1][0].text, '声音')
        self.assertEqual(self.root[2].get('id'), 'note')

    def test_missing_duplicate_reordered_empty_and_fabricated_slots_rejected(self):
        variants = []
        for mutation in ('missing', 'duplicate', 'reorder', 'empty', 'token', 'injected', 'nonstring', 'ambiguous'):
            item = copy.deepcopy(self.item)
            if mutation == 'missing': item['parts'].pop()
            if mutation == 'duplicate': item['parts'].append(item['parts'][0])
            if mutation == 'reorder': item['parts'].reverse()
            if mutation == 'empty': item['parts'][0]['translation'] = ''
            if mutation == 'token': item['parts'][0]['translation'] = '[[ET0001]]'
            if mutation == 'injected': next(p for p, s in zip(item['parts'], wire.parts(self.unit)) if not s['source'])['translation'] = '杜撰内容'
            if mutation == 'nonstring': item['parts'][0]['translation'] = None
            if mutation == 'ambiguous': item['translation'] = '另一份结果'
            variants.append(item)
        for item in variants:
            with self.assertRaises(core.JobError):
                wire.materialize(self.unit, item)

    def test_wire_prompt_exposes_full_context_but_no_original_format_tokens(self):
        prompt = wire.prompt({'target': 'zh-CN', 'style': '忠实自然', 'glossary': {}},
                             {'id': 'b1', 'units': [self.unit]}, 'r1', {'u1': '别把淋雨译为曝晒'})
        self.assertIsNone(core.TOKEN.search(prompt))
        self.assertIn('雨に晒され、声を聞いた。', prompt)
        self.assertIn('别把淋雨译为曝晒', prompt)

    def test_boundary_repetition_is_reported_without_changing_model_text(self):
        item = wire.materialize(self.unit, self.item)
        item['translation'] = item['translation'].replace('被雨[[ET0001]]淋', '三浦[[ET0001]]三浦')
        raw = json.dumps({'batch_id': 'b1', 'items': [item]})
        result, warnings = core.validate_response({'id': 'b1', 'units': [self.unit]}, raw, 'zh-CN')
        self.assertEqual(result['items'][0]['translation'], item['translation'])
        repeated = [warning for warning in warnings if warning['code'] == 'boundary_repetition']
        self.assertEqual(len(repeated), 1)
        self.assertIn('三浦', repeated[0]['message'])


if __name__ == '__main__':
    unittest.main()
