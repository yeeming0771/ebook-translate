"""Integration tests at job, CLI, HTTP and EPUB artifact boundaries."""

import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

from ebook_translate import core
from ebook_translate.bridge import make_server


def fixture(path, image=False, nested=False, fixed=False, unsafe=False, epub2=False):
    ns = 'xmlns="http://www.w3.org/1999/xhtml"'
    package = '''<package xmlns="http://www.idpf.org/2007/opf" version="VERSION" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:test</dc:identifier><dc:title>物語</dc:title><dc:language>ja</dc:language>LAYOUT</metadata>
<manifest><item id="ch" href="text/ch.xhtml" media-type="application/xhtml+xml"/><item id="note" href="text/note.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>IMAGE</manifest>
<spine page-progression-direction="rtl"><itemref idref="ch"/><itemref idref="note"/>SPINE</spine></package>'''
    package = package.replace('VERSION', '2.0' if epub2 else '3.0').replace('LAYOUT', '<meta property="rendition:layout">pre-paginated</meta>' if fixed else '')
    package = package.replace('IMAGE', '<item id="scan" href="scan.xhtml" media-type="application/xhtml+xml"/><item id="img" href="scan.png" media-type="image/png"/>' if image else '')
    package = package.replace('SPINE', '<itemref idref="scan"/>' if image else '')
    with ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf"/></rootfiles></container>')
        z.writestr('OEBPS/package.opf', package)
        content = '<h1 id="title">第一話</h1><p id="p1" class="body">私は<em class="voice">猫</em>。<a href="note.xhtml#n1">注</a></p>'
        if nested:
            content += '<ul><li><p>夢を見る。</p><ul><li>終わり。</li></ul></li></ul>'
        z.writestr('OEBPS/text/ch.xhtml', f'<html {ns} xml:lang="ja"><head><title>物語</title><link rel="stylesheet" href="../style.css"/></head><body>{content}</body></html>')
        z.writestr('OEBPS/text/note.xhtml', f'<html {ns}><head><title>注</title></head><body><p id="n1">注釈です。</p><a href="ch.xhtml#p1">戻る</a></body></html>')
        z.writestr('OEBPS/nav.xhtml', f'<html {ns} xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目次</title></head><body><nav epub:type="toc"><ol><li><a href="text/ch.xhtml#title">第一話</a></li></ol></nav></body></html>')
        z.writestr('OEBPS/style.css', '.body { writing-mode: vertical-rl; }')
        if image:
            z.writestr('OEBPS/scan.xhtml', f'<html {ns}><head><title>scan</title></head><body><img src="scan.png" alt=""/></body></html>')
            z.writestr('OEBPS/scan.png', b'fixture-image')
        if unsafe:
            z.writestr('../escape.txt', 'bad')


def reply(batch):
    translations = {
        '第一話': '第一篇', '私は': '我是', '猫': '猫咪', '。': '。', '注': '注释',
        '注釈です。': '这是注释。', '戻る': '返回', '夢を見る。': '做一个梦。', '終わり。': '结束。', '物語': '故事',
    }
    items = []
    for u in batch['units']:
        parts = re.split(r'(\[\[ET\d+\]\])', u['source'])
        translated = ''.join(p if core.TOKEN.fullmatch(p) else translations.get(p, p) for p in parts)
        items.append({'id': u['id'], 'source_hash': u['source_hash'], 'translation': translated})
    return {'batch_id': batch['id'], 'items': items}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'book.epub'
        self.job = self.root / 'run'
        fixture(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def accept_all(self):
        for batch in core.load(self.job)['batches']:
            core.mark_submitted(self.job, batch['id'], 'https://gemini.google.com/app/test')
            core.import_response(self.job, batch['id'], json.dumps(reply(batch), ensure_ascii=False))
            core.approve(self.job, batch['id'], '合成夹具测试：逐项核对内容、标签与链接')

    def test_roundtrip_preserves_inline_markup_links_css_spine_and_translates_nav_metadata(self):
        core.initialize(self.source, self.job)
        self.accept_all()
        output = self.root / 'translated.epub'
        result = core.build(self.job, output)
        with ZipFile(output) as z:
            self.assertEqual(z.infolist()[0].filename, 'mimetype')
            self.assertEqual(z.infolist()[0].compress_type, ZIP_STORED)
            self.assertEqual(z.read('OEBPS/style.css'), b'.body { writing-mode: vertical-rl; }')
            ch = ET.fromstring(z.read('OEBPS/text/ch.xhtml'))
            p = next(e for e in ch.iter() if e.get('id') == 'p1')
            self.assertEqual(''.join(p.itertext()), '我是猫咪。注释')
            self.assertEqual(p[0].tag, f'{{{core.XHTML}}}em')
            self.assertEqual(p[0].get('class'), 'voice')
            self.assertEqual(p[1].get('href'), 'note.xhtml#n1')
            nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
            self.assertIn('第一篇', ''.join(nav.itertext()))
            opf = ET.fromstring(z.read('OEBPS/package.opf'))
            self.assertEqual(opf.find(f'.//{{{core.DC}}}title').text, '故事')
            self.assertEqual(opf.find(f'.//{{{core.DC}}}language').text, 'zh-CN')
            self.assertEqual(opf.find(f'.//{{{core.OPF}}}spine').get('page-progression-direction'), 'rtl')
            self.assertEqual(core.validate_links(z), [])
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(report['checks']['epubcheck'], 'not_run')
        self.assertEqual(report['checks']['literary_quality'], 'not_automatically_certified')

    def test_missing_duplicate_reordered_id_hash_and_tokens_are_rejected_and_raw_saved(self):
        core.initialize(self.source, self.job)
        batch = core.load(self.job)['batches'][0]
        mutations = [lambda r: r['items'].pop(), lambda r: r['items'].append(r['items'][0]),
                     lambda r: r['items'].reverse(), lambda r: r['items'][0].update(source_hash='stale'),
                     lambda r: r['items'][1].update(translation='删除了标签')]
        for mutate in mutations:
            value = reply(batch)
            mutate(value)
            with self.assertRaises(core.JobError):
                core.import_response(self.job, batch['id'], json.dumps(value))
            self.assertEqual(core.next_batch(self.job)['status'], 'needs_revision')
        self.assertEqual(len(list((self.job / 'responses').glob('*.txt'))), 5)

    def test_resume_prioritizes_submitted_and_blocks_unreviewed_export(self):
        core.initialize(self.source, self.job)
        core.mark_submitted(self.job, 'b0001', 'https://gemini.google.com/app/test')
        self.assertEqual(core.next_batch(self.job)['status'], 'submitted')
        batch = core.load(self.job)['batches'][0]
        core.import_response(self.job, batch['id'], json.dumps(reply(batch)))
        self.assertEqual(core.next_batch(self.job)['status'], 'review')
        with self.assertRaises(core.JobError):
            core.build(self.job, self.root / 'blocked.epub')
        self.assertFalse((self.root / 'blocked.epub').exists())

    def test_changed_source_rejected(self):
        core.initialize(self.source, self.job)
        (self.job / 'source.epub').write_bytes(b'changed')
        with self.assertRaises(core.JobError):
            core.prompt(self.job, 'b0001')

    def test_reimport_invalidates_approval_and_retains_provenance(self):
        core.initialize(self.source, self.job)
        batch = core.load(self.job)['batches'][0]
        raw = json.dumps(reply(batch))
        core.import_response(self.job, batch['id'], raw)
        core.approve(self.job, batch['id'], 'fixture review')
        core.import_response(self.job, batch['id'], raw, provenance='human-edit')
        updated = core.load(self.job)['batches'][0]
        self.assertEqual(updated['status'], 'review')
        self.assertNotIn('approval', updated)
        self.assertEqual(updated['provenance'], 'human-edit')
        self.assertEqual(len(list((self.job / 'responses').glob('*.txt'))), 2)

    def test_empty_translation_and_duplicate_json_keys_rejected(self):
        core.initialize(self.source, self.job)
        batch = core.load(self.job)['batches'][0]
        value = reply(batch)
        value['items'][0]['translation'] = '   '
        with self.assertRaises(core.JobError):
            core.import_response(self.job, batch['id'], json.dumps(value))
        raw = '{"batch_id":"b0001","batch_id":"b0002","items":[]}'
        with self.assertRaisesRegex(core.JobError, '重复字段'):
            core.import_response(self.job, batch['id'], raw)

    def test_export_does_not_overwrite_existing_file(self):
        core.initialize(self.source, self.job)
        self.accept_all()
        output = self.root / 'existing.epub'
        output.write_bytes(b'previous-result')
        with self.assertRaises(core.JobError):
            core.build(self.job, output)
        self.assertEqual(output.read_bytes(), b'previous-result')

    def test_image_pages_require_explicit_override_and_stay_in_report(self):
        fixture(self.source, image=True)
        core.initialize(self.source, self.job)
        self.accept_all()
        with self.assertRaises(core.JobError):
            core.build(self.job, self.root / 'result.epub')
        result = core.build(self.job, self.root / 'result.epub', allow_image_pages=True)
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertFalse(report['image_text_coverage_verified'])
        with ZipFile(result['output']) as z:
            self.assertEqual(z.read('OEBPS/scan.png'), b'fixture-image')

    def test_nested_blocks_extracted_once(self):
        fixture(self.source, nested=True)
        core.initialize(self.source, self.job)
        sources = [u['source'] for b in core.load(self.job)['batches'] for u in b['units']]
        self.assertEqual(sources.count('夢を見る。'), 1)
        self.assertEqual(sources.count('終わり。'), 1)
        self.accept_all()
        core.build(self.job, self.root / 'nested.epub')

    def test_fixed_layout_epub2_and_unsafe_zip_refused_without_creating_job(self):
        for options in ({'fixed': True}, {'unsafe': True}, {'epub2': True}):
            fixture(self.source, **options)
            with self.assertRaises(core.JobError):
                core.initialize(self.source, self.job)
            self.assertFalse(self.job.exists())

    def test_broken_anchor_prevents_export(self):
        core.initialize(self.source, self.job)
        self.accept_all()
        # A separate source contains a pre-existing broken link.
        broken = self.root / 'broken.epub'
        with ZipFile(self.source) as src, ZipFile(broken, 'w') as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename).replace(b'note.xhtml#n1', b'note.xhtml#missing'))
        other = self.root / 'broken-run'
        core.initialize(broken, other)
        for batch in core.load(other)['batches']:
            core.import_response(other, batch['id'], json.dumps(reply(batch)))
            core.approve(other, batch['id'], 'fixture review')
        with self.assertRaisesRegex(core.JobError, '锚点'):
            core.build(other, self.root / 'invalid.epub')
        self.assertFalse((self.root / 'invalid.epub').exists())

    def test_cli_public_commands(self):
        def run(*args):
            return subprocess.run([sys.executable, '-m', 'ebook_translate', *map(str, args)], capture_output=True, text=True, encoding='utf-8')
        initialized = run('init', self.source, '--job', self.job)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        resumed = run('next', self.job)
        self.assertEqual(json.loads(resumed.stdout)['status'], 'pending')
        prompt = run('prompt', self.job, 'b0001')
        self.assertIn('source_hash', prompt.stdout)
        missing = run('build', self.job, '--output', self.root / 'not-ready.epub')
        self.assertEqual(missing.returncode, 2)

    def test_http_forms_import_review_and_reject_cross_origin(self):
        core.initialize(self.source, self.job)
        server = make_server(self.job, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base) as response:
                html = response.read().decode('utf-8')
            token = re.search(r'name="token" value="([^"]+)"', html)[1]
            data = {'token': token, 'batch': 'b0001', 'conversation': 'https://gemini.google.com/app/test'}
            request = Request(base + '/submitted', data=urlencode(data).encode(), headers={'Origin': 'https://example.com'})
            with self.assertRaises(HTTPError) as denied:
                urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            request = Request(base + '/submitted', data=urlencode(data).encode(), headers={'Origin': base})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(core.next_batch(self.job)['status'], 'submitted')
            data['response'] = json.dumps(reply(core.load(self.job)['batches'][0]))
            with urlopen(Request(base + '/import', data=urlencode(data).encode(), headers={'Origin': base})) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(core.next_batch(self.job)['status'], 'review')
            data['note'] = 'HTTP fixture review'
            with urlopen(Request(base + '/approve', data=urlencode(data).encode(), headers={'Origin': base})) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(core.load(self.job)['batches'][0]['status'], 'accepted')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
