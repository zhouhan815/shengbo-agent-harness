"""Independent human consent and one continuous, durable formal-script audition."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from shengbo_contracts.business import VoiceSelection, command_wire_candidates
from .storage import BusinessError, digest, now
from .voice_preview import VoicePreviewService


APPROVAL_KIND = "script_audio_preview_approval"


def _same(left, right):
    return all(left.get(key) == right.get(key) for key in ("kind", "id", "revision", "content_hash"))


def _current_binding(service, c, preparation_id, script_ref, module_keys):
    prep = service._prep(c, preparation_id)
    ref = script_ref.model_dump(exclude_none=True) if hasattr(script_ref, "model_dump") else script_ref
    if not any(_same(ref, row) for row in prep["body"]["script_refs"]):
        raise BusinessError("PREVIEW_SCRIPT_STALE", "试听目标不是本场当前正式话术")
    script = service._artifact(c, ref, "script_revision", preparation_id)
    if (len(module_keys) != len(set(module_keys)) or not module_keys
            or any(key == "transition" for key in module_keys)):
        raise BusinessError("PREVIEW_MODULE_SCOPE_INVALID", "试听须为不重复的有效模块")
    refs, parts, sentences = [], [], []
    for key in module_keys:
        module_ref = script["body"]["module_refs"].get(key)
        if not module_ref:
            raise BusinessError("PREVIEW_MODULE_MISSING", "指定模块没有正式原文")
        module = service._artifact(c, module_ref, "module")["body"]
        rows = module["sentences"]
        text = "".join(row["text"] for row in rows)
        if not text.strip():
            raise BusinessError("PREVIEW_MODULE_EMPTY", "指定模块原文为空")
        refs.append(module_ref)
        parts.append(text)
        sentences.extend(row["id"] for row in rows)
    text = "\n".join(parts)
    if len(text) > 60000:
        raise BusinessError("PREVIEW_TEXT_TOO_LONG")
    return {"preparation_id": preparation_id, "script_ref": script["ref"],
        "product_ref": script["body"]["product_ref"], "module_keys": list(module_keys),
        "module_refs": refs, "sentence_ids": sentences, "ordered_text": text,
        "ordered_text_hash": digest(text), "output_mode": "continuous"}


def approve(service, c, a, m, p, s):
    service._prep(c, a.preparation_id, a.expected_revision)
    binding = _current_binding(service, c, a.preparation_id, a.script_ref, a.module_keys)
    expected_refs = [value.model_dump(exclude_none=True) for value in a.module_refs]
    if len(expected_refs) != len(binding["module_refs"]) or any(not _same(left, right)
            for left, right in zip(expected_refs, binding["module_refs"])):
        raise BusinessError("PREVIEW_MODULE_REF_MISMATCH", "模块引用与指定原稿不一致")
    if a.ordered_text_hash != binding["ordered_text_hash"]:
        raise BusinessError("PREVIEW_TEXT_HASH_MISMATCH", "试听文字与人工授权内容不一致")
    voice = a.tts_selection.model_dump()
    if voice["provider"] == "minimax" and len(binding["ordered_text"]) >= 10000:
        raise BusinessError("PREVIEW_TEXT_TOO_LONG", "MiniMax单次连续试听原文须少于10000字符；本次未发出语音请求", status=422)
    server = getattr(service.adapter, "server", None)
    if server is None:
        raise BusinessError("TTS_NOT_CONFIGURED")
    server.voice_options({"tts_provider": voice["provider"], "tts_voice": voice["voice"], "tts_model": voice["model"]})
    binding.update(run_id=a.run_id, tts_selection=voice, style=deepcopy(a.style))
    human = {"actor_id": p.actor_id, "approved_at": now(), "operation_id": m.operation_id,
        "authorization_quote": a.authorization_quote, "authorization_source": "explicit_user_message"}
    body = {"binding": binding, "binding_hash": digest(binding), "human_receipt": human}
    ref = service.store.artifact(c, APPROVAL_KIND, {"preparation_id": a.preparation_id, **body})
    return {"preview_approval_ref": ref, **body, "changed_paths": []}


def read(service, c, a, p, s):
    reference = a.ref
    if reference is None:
        if p.kind != "agent" or not a.preparation_id:
            raise BusinessError("PREVIEW_APPROVAL_REF_REQUIRED")
        matches = [record for record in service.store.all(c, APPROVAL_KIND)
            if record["body"]["binding"].get("run_id") == p.run_id
            and record["body"]["binding"].get("preparation_id") == a.preparation_id
            and (not a.operation_id or record["body"]["human_receipt"]["operation_id"] == a.operation_id)]
        if not matches:
            return {"status": "missing", "whole_package_approval": False,
                "message": "本任务的局部试听人工授权尚未登记；未合成语音。"}
        record = matches[-1]
    else:
        record = service._artifact(c, reference, APPROVAL_KIND)
    binding = record["body"]["binding"]
    service._assert_scope(c, p, s, binding["preparation_id"])
    if p.kind == "agent" and p.run_id != binding["run_id"]:
        raise BusinessError("PREVIEW_APPROVAL_RUN_MISMATCH", status=403)
    current = _current_binding(service, c, binding["preparation_id"], binding["script_ref"], binding["module_keys"])
    if any(current[key] != binding[key] for key in current):
        raise BusinessError("PREVIEW_APPROVAL_STALE")
    if digest(binding) != record["body"]["binding_hash"]:
        raise BusinessError("PREVIEW_APPROVAL_INVALID")
    return {**record, "status": "current", "whole_package_approval": False}


async def preview(service, a, m, p, s):
    # All gates precede the durable claim and the potential paid request.
    with service.store.transaction() as c:
        service._assert_scope(c, p, s, a.preparation_id)
        service._grant(c, "preview_script_audio", m, p, s, a)
        service._prep(c, a.preparation_id, a.expected_revision)
        for ref in m.expected_versions:
            current = service.store.get(c, ref.kind, ref.id)
            if not _same(current["ref"], ref.model_dump(exclude_none=True)):
                raise BusinessError("REVISION_CONFLICT")
        if m.request_hash and m.request_hash not in {digest({"command": "preview_script_audio", "args": candidate,
                "scope": s.model_dump(exclude_none=True) if s else None}) for candidate in
                command_wire_candidates("preview_script_audio", a.model_dump(exclude_none=True))}:
            raise BusinessError("REQUEST_HASH_MISMATCH")
        approved = read(service, c, SimpleNamespace(ref=a.preview_approval_ref), p, s)
        if a.binding_hash != approved["body"]["binding_hash"]:
            raise BusinessError("PREVIEW_APPROVAL_BINDING_MISMATCH")
        binding = approved["body"]["binding"]
        if binding["preparation_id"] != a.preparation_id:
            raise BusinessError("SCOPE_CONFLICT", status=403)
    server = getattr(service.adapter, "server", None)
    if server is None:
        raise BusinessError("TTS_NOT_CONFIGURED")
    previews = getattr(service, "_scoped_previews", None)
    if previews is None:
        previews = service._scoped_previews = VoicePreviewService(service.store, server)
    value = SimpleNamespace(operation_id=m.operation_id, text=binding["ordered_text"],
        tts_selection=VoiceSelection.model_validate(binding["tts_selection"]))
    return await previews.preview(value, p.actor_id, style=binding["style"],
        request_binding={"approval_ref": approved["ref"], "binding_hash": a.binding_hash, "run_id": binding["run_id"]},
        command="preview_script_audio", result_context={
            "preview_approval_ref": approved["ref"], "binding_hash": a.binding_hash,
            "preparation_id": a.preparation_id, "script_ref": binding["script_ref"], "run_id": binding["run_id"],
            "module_keys": binding["module_keys"], "module_refs": binding["module_refs"],
            "sentence_ids": binding["sentence_ids"], "ordered_text_hash": binding["ordered_text_hash"],
            "text_characters": len(binding["ordered_text"]), "output_mode": "continuous",
            "tts_selection": binding["tts_selection"], "resolved_style": binding["style"],
            "human_receipt": approved["body"]["human_receipt"], "whole_package_approval": False})


def list_previews(service, c, a, p, s):
    import json
    from .voice_preview import read_preview_receipt
    prep = service._prep(c, a.preparation_id)
    refs = prep["body"]["script_refs"]
    selected = a.ref.model_dump(exclude_none=True) if a.ref else None
    if selected and not any(_same(selected, ref) for ref in refs):
        raise BusinessError("PREVIEW_SCRIPT_STALE")
    items = []
    for row in c.execute("SELECT principal_id,operation_id,response_json FROM prelive_operations WHERE command='preview_script_audio' ORDER BY created_at DESC"):
        receipt = json.loads(row["response_json"])
        data = receipt.get("data", {})
        if data.get("preparation_id") != a.preparation_id:
            continue
        if p.kind == "agent" and data.get("run_id") != p.run_id:
            continue
        ref = data.get("script_ref") or {}
        if not any(_same(ref, candidate) for candidate in refs) or selected and not _same(ref, selected):
            continue
        status = read_preview_receipt(c, row["principal_id"], row["operation_id"])
        items.append({**data, "operation_id": row["operation_id"],
            "preview_state": status["preview_state"], "playable": status["state"] == "committed"})
    page = items[a.cursor:a.cursor + a.limit]
    return {"items": page, "total": len(items),
        "next_cursor": a.cursor + len(page) if a.cursor + len(page) < len(items) else None}
