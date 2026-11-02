"""Command-line interface. Gemini web is operated by the accompanying skill."""

import argparse
import json
from pathlib import Path
import sys
from zipfile import BadZipFile

from . import __version__, core


def parser():
    p = argparse.ArgumentParser(description="免费的 Gemini 网页版 EPUB 翻译工作流")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="启动供 Codex 调用的 stdio MCP 服务")
    init = sub.add_parser("init", help="清点 EPUB 并创建可恢复任务")
    init.add_argument("epub")
    init.add_argument("--job", required=True)
    init.add_argument("--target", default="zh-CN")
    init.add_argument("--max-chars", type=int, default=6000)
    for name in ("status", "next", "prompt", "submitted", "import", "approve", "build", "serve", "configure"):
        cmd = sub.add_parser(name)
        cmd.add_argument("job")
        if name in {"prompt", "submitted", "import", "approve"}:
            cmd.add_argument("batch")
        if name == "prompt":
            cmd.add_argument("--output", help="保存提示词到新文本文件，否则写到终端")
        if name == "submitted":
            cmd.add_argument("--conversation", default="")
        if name == "import":
            cmd.add_argument("response", help="Gemini JSON 回复文件，或 - 表示 stdin")
            cmd.add_argument("--provenance", choices=["gemini-web", "human-edit"], default="gemini-web")
        if name == "approve":
            cmd.add_argument("--note", required=True)
        if name == "build":
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--allow-image-pages", action="store_true")
        if name == "serve":
            cmd.add_argument("--port", type=int, default=8765)
        if name == "configure":
            cmd.add_argument("--style")
            cmd.add_argument("--glossary", help="字符串到字符串的术语表 JSON 文件")
    return p


def main(argv=None):
    # Keep JSON and Chinese text stable when piped on Windows (otherwise GBK).
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = core.initialize(args.epub, args.job, args.target, args.max_chars)
        elif args.command == "mcp":
            from .mcp import main as mcp_main
            return mcp_main()
        elif args.command == "status":
            result = core.status(args.job)
        elif args.command == "next":
            result = core.next_batch(args.job)
        elif args.command == "prompt":
            value = core.prompt(args.job, args.batch)
            if args.output:
                with open(args.output, "x", encoding="utf-8") as f:
                    f.write(value)
                result = {"prompt": str(Path(args.output).resolve())}
            else:
                print(value)
                return 0
        elif args.command == "submitted":
            result = core.mark_submitted(args.job, args.batch, args.conversation)
        elif args.command == "import":
            raw = sys.stdin.read() if args.response == "-" else Path(args.response).read_text(encoding="utf-8-sig")
            result = core.import_response(args.job, args.batch, raw, args.provenance)
        elif args.command == "approve":
            result = core.approve(args.job, args.batch, args.note)
        elif args.command == "configure":
            glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8-sig")) if args.glossary else None
            result = core.set_preferences(args.job, args.style, glossary)
        elif args.command == "build":
            result = core.build(args.job, args.output, args.allow_image_pages)
        elif args.command == "serve":
            from .bridge import serve
            serve(args.job, args.port)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (core.JobError, OSError, BadZipFile, KeyError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止；已保存的任务可直接恢复。", file=sys.stderr)
        return 130
