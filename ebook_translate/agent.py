"""Agent-facing orchestration: one uploaded file becomes a resumable session job."""

from pathlib import Path
import json
import re

from . import core, vision, recovery


def start(input_path, workspace, target="zh-CN", max_chars=6000):
    source, workspace = Path(input_path).resolve(), Path(workspace).resolve()
    if not source.is_file():
        raise core.JobError("会话附件路径不存在；请使用宿主提供的真实本地路径")
    if not re.fullmatch(r"[A-Za-z0-9-]{2,24}", target):
        raise core.JobError("目标语言必须是语言代码，例如 zh-CN")
    fingerprint = core.digest(source.read_bytes())
    job = workspace / "work" / "ebook-translate" / f"{fingerprint[:20]}-{target}"
    resumed = job.exists()
    if not resumed:
        core.initialize(source, job, target, max_chars)
    data = core.load(job)
    if data["source_hash"] != fingerprint or data["target"] != target:
        raise core.JobError("已有任务与本次附件不一致")
    descriptor = job / "agent.json"
    if not descriptor.exists():
        core.atomic_json(descriptor, {"workspace": str(workspace), "source_hash": fingerprint,
                                     "output": str(workspace / "outputs" / f"{source.stem}.{target}.{fingerprint[:8]}.epub")})
    vision.prepare(job)
    return {"job": str(job), "resumed": resumed, "next": next_action(job)}


def next_action(job):
    data = core.load(job)
    # Upgrade legacy failed batches and replay compatible archived replies once.
    if not data.get('agent_pause'):
        for failed in data['batches']:
            if failed['status'] == 'needs_revision' and 'recovery' not in failed:
                recovery.begin(job, failed['id'])
        data = core.load(job)
    step = core.next_batch(job)
    all_tasks = data["batches"] + data.get("image_tasks", [])
    progress = {"accepted": sum(b["status"] == "accepted" for b in all_tasks), "total": len(all_tasks)}
    result = {"job": str(Path(job).resolve()), "progress": progress}
    if data.get("agent_pause"):
        return {**result, "action": "blocked", "reason": data["agent_pause"],
                "instruction": "保存进度，只向用户报告需要其处理的具体阻碍。解除后调用 ebook_resume。"}
    for state in ("submitted", "needs_revision", "review", "pending"):
        for task in data.get("image_tasks", []):
            if task["status"] != state:
                continue
            image_step = {**result, "batch_id": task["id"], "document": task["document"], "status": state,
                          "assets": task["assets"]}
            if not task["assets"]:
                return {**image_step, "action": "blocked", "reason": "图片页没有可提取图片资源"}
            if state in {"pending", "needs_revision"}:
                return {**image_step, "action": "translate_images_in_gemini_web", "prompt": vision.prompt(data, task),
                        "last_error": next((a["error"] for a in reversed(task["attempts"]) if "error" in a), None),
                        "instruction": "Codex 用宿主浏览器将 assets 指向的图片和 prompt 发送 Gemini，随后 ebook_submitted；不要让用户上传中间图片。"}
            if state == "submitted":
                return {**image_step, "action": "collect_gemini_response", "submissions": task["attempts"],
                        "instruction": "等待可见生成完成，读取图片识别与译文 JSON 后 ebook_receive。"}
            return {**image_step, "action": "review_image_translation", "translation": task["translation"],
                    "instruction": "Codex 查看原图，核对阅读顺序、识别和完整译文，检查跨页衔接。封面或装饰页可保留；文字页不可伪装成装饰页。通过后 ebook_review。"}
    if step["status"] == "done":
        result.update(action="export", instruction="调用 ebook_finish 导出并返回 EPUB 与报告，不要求用户执行 CLI。")
        return result
    b = core.get_batch(data, step["id"])
    result.update(batch_id=b["id"], document=b["document"], status=b["status"])
    if b["status"] in {"pending", "needs_revision"}:
        errors = [a["error"] for a in b["attempts"] if "error" in a]
        result['last_error'] = errors[-1] if errors else None
        if 'recovery' in b:
            result.update(recovery.plan(job, b['id']))
            result['instruction'] = ('在新 Gemini 对话中仅补译 repair_unit_ids 指定条目；保留 request_id，不重发已通过项。收取后继续 ebook_receive。'
                                     if result['action'] != 'blocked' else '已保存有效条目和原始回复，向用户报告单条连续失败的具体问题。')
        else:
            result.update(action='translate_in_gemini_web', prompt=core.prompt(job, b['id']),
                          instruction='用宿主浏览器将完整 prompt 提交 Gemini 网页；确认提交后调用 ebook_submitted。不要让用户复制粘贴。')
    elif b["status"] == "submitted":
        result.update(action="collect_gemini_response", submissions=[a for a in b["attempts"] if a["event"] == "submitted"],
                      instruction="先查看保存的 Gemini 对话，等待可见生成完成并收取完整回复，调用 ebook_receive；不盲目重新发送。")
    else:
        result.update(action="review_translation", sources=b["units"], translation=b["translation"],
                      warnings=b.get("warnings", []),
                      instruction="Codex 对照原文复核。通过则调用 ebook_review 并写具体复核说明；有错则让 Gemini 修订，再 ebook_receive。无需用户逐批验收。")
        result['instruction'] += ' 发现局部语义问题时调用 ebook_repair，只重译出错 ID。'
    return result


def submitted(job, batch_id, conversation=""):
    if batch_id.startswith("i"):
        vision.submitted(job, batch_id, conversation)
    else:
        core.mark_submitted(job, batch_id, conversation)
    return next_action(job)


def receive(job, batch_id, response, provenance="gemini-web"):
    try:
        if batch_id.startswith("i"):
            vision.receive(job, batch_id, response, provenance)
        else:
            data = core.load(job)
            if 'recovery' in core.get_batch(data, batch_id):
                result = recovery.receive(job, batch_id, response, provenance)
                if 'validation_error' in result:
                    return {**result, 'next': next_action(job)}
            else:
                core.import_response(job, batch_id, response, provenance)
    except core.JobError as exc:
        return {"validation_error": str(exc), "next": next_action(job)}
    return next_action(job)


def review(job, batch_id, note):
    if batch_id.startswith("i"):
        vision.approve(job, batch_id, note)
    else:
        core.approve(job, batch_id, note)
    return next_action(job)


def repair(job, batch_id, unit_ids_json, reason):
    recovery.request_repair(job, batch_id, json.loads(unit_ids_json), reason)
    return next_action(job)


def pause(job, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise core.JobError("必须提供具体阻碍")
    with core.locked(job):
        data = core.load(job)
        data["agent_pause"] = {"reason": reason, "at": core.now()}
        core.save(job, data)
    return next_action(job)


def resume(job):
    with core.locked(job):
        data = core.load(job)
        data.pop("agent_pause", None)
        for batch in data['batches']:
            state = batch.get('recovery')
            if not state or batch['status'] != 'needs_revision':
                continue
            exhausted = [identifier for identifier, count in state['single_failures'].items()
                         if count >= recovery.MAX_SINGLE_FAILURES]
            if exhausted:
                state['history'].append({'event': 'retry_requested', 'unit_ids': exhausted, 'at': core.now()})
                for identifier in exhausted:
                    state['single_failures'].pop(identifier)
        core.save(job, data)
    return next_action(job)


def configure(job, style=None, glossary_json=None):
    data = core.load(job)
    if any(t["status"] != "pending" for t in data.get("image_tasks", [])):
        raise core.JobError("图片翻译已开始，不能中途改写全书规范")
    glossary = json.loads(glossary_json) if glossary_json is not None else None
    core.set_preferences(job, style, glossary)
    return next_action(job)


def finish(job):
    data = core.load(job)
    if data.get("agent_pause"):
        raise core.JobError("任务仍有未解除阻碍，不能导出")
    descriptor_path = Path(job) / "agent.json"
    if not descriptor_path.is_file():
        raise core.JobError("不是 agent 任务，请先使用 ebook_translate 入口")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    output = Path(descriptor["output"])
    report_path = output.with_suffix(".report.json")
    # A completed invocation is safe to repeat after interruption, but not after
    # any new import/review changed the accepted job state.
    revision = core.digest(json.dumps([data["batches"], data.get("image_tasks", [])], ensure_ascii=False, sort_keys=True).encode("utf-8"))
    exported = descriptor.get("exported", {})
    if (exported.get("revision") == revision and output.is_file() and report_path.is_file()
            and exported.get("hash") == core.digest(output.read_bytes())
            and exported.get("report_hash") == core.digest(report_path.read_bytes())):
        return {"action": "complete", "output": str(output), "report": str(report_path), "reused": True}
    result = core.build(job, output)
    descriptor["exported"] = {"revision": revision, "hash": core.digest(output.read_bytes()),
                              "report_hash": core.digest(report_path.read_bytes())}
    core.atomic_json(descriptor_path, descriptor)
    return {"action": "complete", **result, "reused": False,
            "instruction": "向用户返回 EPUB 和报告链接；报告只证明已执行的检查，不自动认证文学质量。"}
