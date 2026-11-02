"""Loopback workbench: exchange prompts and replies via visible browser forms."""

from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from . import core


def make_server(job, port):
    job = Path(job).resolve()
    core.load(job)
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, content_type="text/html; charset=utf-8"):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def host_ok(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def page(self, batch_id=None, message="", response=""):
            data = core.load(job)
            chosen = core.get_batch(data, batch_id) if batch_id else (data["batches"][0] if data["batches"] else None)
            options = "".join(f'<option value="{escape(b["id"])}" {"selected" if b is chosen else ""}>{escape(b["id"])} · {escape(b["status"])} · {escape(b["document"])}</option>' for b in data["batches"])
            stats = json.dumps(core.status(job)["counts"], ensure_ascii=False)
            warnings = escape(json.dumps(data["warnings"], ensure_ascii=False, indent=2))
            body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>ebook-translate · Gemini 网页翻译工作台</title>
<style>body{{font:16px/1.7 system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#182639;background:#f5f7fa}}section{{background:white;padding:24px;border-radius:12px;margin:20px 0}}textarea{{box-sizing:border-box;width:100%;padding:12px;font:14px/1.6 monospace}}button,select,input{{font:inherit;padding:8px;margin:6px 0;max-width:100%}}button{{background:#153f65;color:white;border:0;border-radius:5px;cursor:pointer}}pre{{white-space:pre-wrap}}.notice{{color:#8a3300}}label{{display:block}}</style>
<h1>ebook-translate</h1><p>免费的 Gemini 网页翻译工作台 · 本地 EPUB 工程与断点恢复</p>
<p>{escape(data['source_name'])} → {escape(data['target'])} · {escape(stats)}</p>
<p class="notice" role="status">{escape(message)}</p>
<section><form method="get" action="/"><label>选择批次 <select name="batch">{options}</select></label><button>打开批次</button></form>
<p>恢复建议：{escape(json.dumps(core.next_batch(job), ensure_ascii=False))}</p></section>'''
            if chosen:
                bid = chosen["id"]
                hidden = f'<input type="hidden" name="token" value="{csrf}"><input type="hidden" name="batch" value="{bid}">'
                body += f'''<section><h2>{bid} · {escape(chosen['status'])}</h2><p>将完整提示词复制到 <a href="https://gemini.google.com/app" target="_blank" rel="noreferrer">Gemini 网页版</a>。确认网页收到后记录提交；恢复 submitted 批次时先核对网页，避免重复发送。</p>
<label>翻译提示词<textarea readonly rows="14">{escape(core.prompt(job, bid))}</textarea></label>
<form method="post" action="/submitted">{hidden}<label>Gemini 对话地址（可选）<input name="conversation" type="url" placeholder="https://gemini.google.com/app/..."></label><button>记录已提交</button></form></section>
<section><h2>收取回复</h2><p>等待生成结束，将完整 JSON 回复粘贴到下方。原始回复会保留，结构不通过时不会验收。</p>
<form method="post" action="/import">{hidden}<label>Gemini JSON 回复<textarea name="response" rows="12" required>{escape(response)}</textarea></label>
<label>来源<select name="provenance"><option value="gemini-web">Gemini 网页原始回复</option><option value="human-edit">人工编辑</option></select></label><button>保存并校验译文</button></form></section>'''
                if chosen["translation"]:
                    body += f'<section><h2>对照复核</h2><pre>{escape(json.dumps(chosen["translation"], ensure_ascii=False, indent=2))}</pre><pre>{escape(json.dumps(chosen.get("warnings", []), ensure_ascii=False, indent=2))}</pre>'
                    if chosen["status"] == "review":
                        body += f'<form method="post" action="/approve">{hidden}<label>对照复核说明<input name="note" required placeholder="记录已核对的内容与警告处置"></label><button>确认复核并验收此批次</button></form>'
                    body += '</section>'
            body += f'<section><h2>书籍覆盖与格式警告</h2><pre>{warnings}</pre><p>导出请执行 CLI build；通过结构校验不等于文学质量认证。</p></section></html>'
            return body

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, "Host rejected")
            url = urlsplit(self.path)
            if url.path != "/":
                return self.send(404, "Not found")
            try:
                batch = parse_qs(url.query).get("batch", [None])[0]
                self.send(200, self.page(batch))
            except (core.JobError, OSError) as exc:
                self.send(400, escape(str(exc)))

        def do_POST(self):
            if not self.host_ok():
                return self.send(403, "Host rejected")
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Origin") not in (None, expected):
                return self.send(403, "Origin rejected")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4 * 1024 * 1024:
                    return self.send(413, "Request too large or empty")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                if not secrets.compare_digest(form.get("token", [""])[0], csrf):
                    return self.send(403, "Invalid form token")
                batch = form.get("batch", [""])[0]
                action = urlsplit(self.path).path
                if action == "/submitted":
                    result = core.mark_submitted(job, batch, form.get("conversation", [""])[0])
                elif action == "/import":
                    result = core.import_response(job, batch, form.get("response", [""])[0], form.get("provenance", ["gemini-web"])[0])
                elif action == "/approve":
                    result = core.approve(job, batch, form.get("note", [""])[0])
                else:
                    return self.send(404, "Not found")
                self.send(200, self.page(batch, json.dumps(result, ensure_ascii=False)))
            except (core.JobError, ValueError, OSError) as exc:
                batch = form.get("batch", [None])[0] if 'form' in locals() else None
                raw = form.get("response", [""])[0] if 'form' in locals() else ""
                try:
                    page = self.page(batch, str(exc), raw)
                except core.JobError:
                    page = self.page(None, str(exc), raw)
                self.send(400, page)

        def log_message(self, *args):
            pass  # Do not log book text, form data, or conversation URLs.

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(job, port=8765):
    server = make_server(job, port)
    print(f"本地工作台：http://127.0.0.1:{server.server_port}/\nCtrl+C 停止；任务已落盘。", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
