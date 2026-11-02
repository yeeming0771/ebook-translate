"""Codex-facing protocol and autonomous orchestration, without external models."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from ebook_translate import agent, core, mcp
from test_workflow import fixture, reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.book = self.workspace / 'attachment.epub'
        fixture(self.book)

    def tearDown(self):
        self.tmp.cleanup()

    def translate_all(self, job):
        while True:
            step = agent.next_action(job)
            if step['action'] == 'export':
                return
            if step['action'] == 'translate_in_gemini_web':
                agent.submitted(job, step['batch_id'], 'https://gemini.google.com/app/test')
                batch = core.get_batch(core.load(job), step['batch_id'])
                agent.receive(job, step['batch_id'], json.dumps(reply(batch)))
            elif step['action'] == 'review_translation':
                agent.review(job, step['batch_id'], '合成任务复核：人物叙述、标签及结尾与夹具预期一致')
            else:
                self.fail(str(step))

    def test_uploaded_file_resume_to_export_without_workbench(self):
        first = mcp.call_tool('ebook_translate', {'input_path': str(self.book), 'workspace': str(self.workspace)})
        self.assertEqual(first['next']['action'], 'translate_in_gemini_web')
        second = agent.start(self.book, self.workspace)
        self.assertTrue(second['resumed'])
        self.assertEqual(second['job'], first['job'])
        self.translate_all(first['job'])
        result = mcp.call_tool('ebook_finish', {'job': first['job']})
        self.assertEqual(result['action'], 'complete')
        self.assertTrue(Path(result['output']).is_file())
        self.assertTrue(agent.finish(first['job'])['reused'])
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(report['checks']['resource_links_and_anchors'], 'passed')

    def test_failure_is_recoverable_action_and_not_silent_approval(self):
        job = agent.start(self.book, self.workspace)['job']
        result = agent.receive(job, 'b0001', '{broken')
        self.assertIn('validation_error', result)
        self.assertEqual(result['next']['action'], 'translate_in_gemini_web')
        self.assertEqual(result['next']['status'], 'needs_revision')
        self.assertTrue(result['next']['last_error'])

    def test_preferences_flow_into_prompt_before_first_submission(self):
        job = agent.start(self.book, self.workspace)['job']
        action = mcp.call_tool('ebook_configure', {'job': job, 'style': '保留冷幽默', 'glossary_json': '{"猫":"猫咪"}'})
        self.assertIn('保留冷幽默', action['prompt'])
        self.assertIn('猫咪', action['prompt'])
        agent.submitted(job, 'b0001')
        with self.assertRaises(core.JobError):
            agent.configure(job, style='另一种文风')

    def test_cover_image_preservation_is_explicit_and_reported(self):
        fixture(self.book, image=True)
        job = agent.start(self.book, self.workspace)['job']
        task = core.load(job)['image_tasks'][0]
        raw = json.dumps({'document': task['document'], 'source_hash': task['source_hash'], 'kind': 'preserve',
                          'source_paragraphs': [], 'translation_paragraphs': [], 'note': '封面插画，无正文'})
        agent.receive(job, task['id'], raw)
        agent.review(job, task['id'], '对照合成夹具确认装饰插画保留')
        self.translate_all(job)
        result = agent.finish(job)
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(report['image_pages'][0]['kind'], 'preserve')
        self.assertFalse(report['image_pages'][0]['layout_changed'])
        with ZipFile(self.book) as original, ZipFile(result['output']) as translated:
            self.assertEqual(original.read('OEBPS/scan.xhtml'), translated.read('OEBPS/scan.xhtml'))

    def test_external_blocker_persists_and_resumes_original_submission(self):
        job = agent.start(self.book, self.workspace)['job']
        agent.submitted(job, 'b0001', 'https://gemini.google.com/app/retained')
        self.assertEqual(agent.pause(job, 'Gemini 需要重新登录')['action'], 'blocked')
        with self.assertRaises(core.JobError):
            agent.finish(job)
        action = agent.resume(job)
        self.assertEqual(action['action'], 'collect_gemini_response')
        self.assertEqual(action['submissions'][0]['conversation'], 'https://gemini.google.com/app/retained')

    def test_image_page_is_translated_and_original_resource_preserved(self):
        fixture(self.book, image=True)
        job = agent.start(self.book, self.workspace)['job']
        step = agent.next_action(job)
        self.assertEqual(step['action'], 'translate_images_in_gemini_web')
        self.assertTrue(Path(step['assets'][0]['path']).is_file())
        task = core.load(job)['image_tasks'][0]
        raw = json.dumps({'document': task['document'], 'source_hash': task['source_hash'], 'kind': 'text',
                          'source_paragraphs': ['雨が降る。'], 'translation_paragraphs': ['下雨了。'], 'note': '正文文字页，阅读顺序清晰'})
        agent.submitted(job, step['batch_id'], 'https://gemini.google.com/app/vision')
        self.assertEqual(agent.receive(job, step['batch_id'], raw)['action'], 'review_image_translation')
        agent.review(job, step['batch_id'], '对照合成图片识别夹具确认一句原文和对应译文')
        self.translate_all(job)
        result = agent.finish(job)
        with ZipFile(result['output']) as z:
            scan = core.parse(z.read('OEBPS/scan.xhtml'))
            self.assertIn('下雨了。', ''.join(scan.itertext()))
            self.assertEqual(z.read('OEBPS/scan.png'), b'fixture-image')
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertTrue(report['image_pages'][0]['layout_changed'])

    def test_image_page_cannot_be_skipped_and_bad_ocr_reply_rejected(self):
        fixture(self.book, image=True)
        job = agent.start(self.book, self.workspace)['job']
        task = core.load(job)['image_tasks'][0]
        raw = json.dumps({'document': task['document'], 'source_hash': task['source_hash'], 'kind': 'text',
                          'source_paragraphs': ['雨'], 'translation_paragraphs': [], 'note': 'missing translation'})
        self.assertIn('validation_error', agent.receive(job, task['id'], raw))
        with self.assertRaises(core.JobError):
            agent.finish(job)

    def test_stdio_initialization_discovery_validation_and_error_recovery(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'ebook_translate', 'arguments': {'input_path': str(self.book), 'workspace': str(self.workspace)}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'ebook_translate', 'arguments': {'input_path': [], 'workspace': str(self.workspace)}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'ping'},
        ]
        lines = '\n'.join(json.dumps(r) for r in requests) + '\n'
        process = subprocess.run([sys.executable, '-m', 'ebook_translate.mcp'], input=lines, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4, 5])
        self.assertIn('ebook_translate', [t['name'] for t in replies[1]['result']['tools']])
        action = json.loads(replies[2]['result']['content'][0]['text'])
        self.assertEqual(action['next']['action'], 'translate_in_gemini_web')
        self.assertTrue(replies[3]['result']['isError'])
        self.assertEqual(replies[4]['result'], {})

    def test_protocol_errors_do_not_return_spurious_notification_replies(self):
        self.assertIsNone(mcp.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertEqual(mcp.dispatch([])['error']['code'], -32600)
        self.assertEqual(mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': []})['error']['code'], -32602)


if __name__ == '__main__':
    unittest.main()
