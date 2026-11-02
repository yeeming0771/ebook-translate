"""Small dependency-free MCP stdio server. Browser actions stay in the Codex host."""

import json
import sys
from zipfile import BadZipFile

from . import __version__, agent, core


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STRING = {"type": "string"}
JOB = {"type": "string", "description": "此前工具返回的绝对 job 路径"}
TOOLS = [
    ("ebook_translate", "入口：用户上传 EPUB 后创建或恢复任务。返回 Codex 下一步动作；翻译全程使用免费 Gemini 网页，不要求用户操作中间流程。",
     schema({"input_path": {"type": "string", "description": "会话上传附件的真实本地绝对路径"}, "workspace": {"type": "string", "description": "当前会话可写工作区的绝对路径"}, "target": {"type": "string", "default": "zh-CN"}}, ["input_path", "workspace"]), agent.start),
    ("ebook_next", "恢复当前任务并获取下一步，包括完整 Gemini 提示词或待复核原译文。", schema({"job": JOB}, ["job"]), agent.next_action),
    ("ebook_configure", "翻译开始前根据用户偏好和样章设定全文文风、人物译名术语表。", schema({"job": JOB, "style": STRING, "glossary_json": {"type": "string", "description": "字符串到字符串的术语表 JSON 对象文本"}}, ["job"]), agent.configure),
    ("ebook_submitted", "仅在宿主浏览器确认 Gemini 已收到请求后记录提交与对话地址。", schema({"job": JOB, "batch_id": STRING, "conversation": STRING}, ["job", "batch_id"]), agent.submitted),
    ("ebook_receive", "将宿主浏览器实际读取的完整 Gemini 回复直接落盘、校验，并返回下一步。不要伪造回复。", schema({"job": JOB, "batch_id": STRING, "response": STRING, "provenance": {"type": "string", "enum": ["gemini-web", "human-edit"], "default": "gemini-web"}}, ["job", "batch_id", "response"]), agent.receive),
    ("ebook_review", "Codex 对照原译文复核后验收当前批次，记录具体说明并返回下一步，不要求用户验收每批。", schema({"job": JOB, "batch_id": STRING, "note": STRING}, ["job", "batch_id", "note"]), agent.review),
    ("ebook_finish", "全部批次完成后导出 EPUB 和报告，返回交付路径；不允许静默跳过扫描页。", schema({"job": JOB}, ["job"]), agent.finish),
    ("ebook_pause", "遇到登录、验证码、额度等外部阻碍时记录并交接；不切换付费模型。", schema({"job": JOB, "reason": STRING}, ["job", "reason"]), agent.pause),
    ("ebook_resume", "外部阻碍解除后清除阻碍标记，恢复原任务。", schema({"job": JOB}, ["job"]), agent.resume),
]


def call_tool(name, arguments):
    for tool_name, _, spec, function in TOOLS:
        if name != tool_name:
            continue
        if not isinstance(arguments, dict):
            raise core.JobError("工具参数必须是对象")
        if set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
            raise core.JobError("工具参数包含未知字段或缺少必填字段")
        for key, value in arguments.items():
            if not isinstance(value, str):
                raise core.JobError(f"{key} 必须是字符串")
            allowed = spec["properties"][key].get("enum")
            if allowed and value not in allowed:
                raise core.JobError(f"{key} 值不受支持")
        if name == "ebook_translate":
            from pathlib import Path
            if not Path(arguments["input_path"]).is_absolute() or not Path(arguments["workspace"]).is_absolute():
                raise core.JobError("附件与会话工作区必须使用绝对路径")
        return function(**arguments)
    raise core.JobError(f"未知工具：{name}")


def dispatch(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in request:
        return None
    identifier = request["id"]
    method, params = request["method"], request.get("params", {})
    base = {"jsonrpc": "2.0", "id": identifier}
    if not isinstance(params, dict):
        return {**base, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
        return {**base, "result": {"protocolVersion": requested if requested in supported else "2025-06-18",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ebook-translate", "version": __version__},
                "instructions": "用户上传 EPUB 后调用 ebook_translate，按返回动作由 Codex 操作 Gemini 网页并完成全流程。用户不用手动操作工作台。浏览器能力由宿主提供，本服务不含付费 API。"}}
    if method == "ping":
        return {**base, "result": {}}
    if method == "tools/list":
        return {**base, "result": {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}}
    if method == "tools/call":
        try:
            result = call_tool(params.get("name"), params.get("arguments", {}))
            return {**base, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}}
        except (core.JobError, OSError, BadZipFile, KeyError, json.JSONDecodeError) as exc:
            return {**base, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
    return {**base, "error": {"code": -32601, "message": "Method not found"}}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = dispatch(request)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
