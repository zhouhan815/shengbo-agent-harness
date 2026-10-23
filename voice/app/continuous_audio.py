"""Scoped adoption of a continuous recording, without editing scripts/packages.

An audition approval alone never changes formal playback. A separate explicit
human adoption binds the new audio and timeline to one adopted publication and
one exact script revision. All other versions keep their existing audio.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import wave
from fastapi import Request

from .audio_timeline import align_subtitles
from .prelive.storage import BusinessError, digest, now


def choice_id(publication_id, script_id):
    return digest([publication_id, script_id]).split(":")[1]


def adopt(service, server, room_id, operation_id, authorization_quote, actor_id):
    if not isinstance(authorization_quote, str) or not 1 <= len(authorization_quote) <= 6000 or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
        raise BusinessError("CONTINUOUS_CONSENT_REQUIRED", status=422)
    with service.store.transaction() as c:
        room = service.store.get(c, "config", room_id)
        pubref = room["body"].get("adopted_publication_ref")
        if not pubref:
            raise BusinessError("ADOPTED_VALID_PUBLICATION_REQUIRED")
        review = service._publication_review(c, pubref)
        if review["currentness"]["blockers"]:
            raise BusinessError("CURRENTNESS_BLOCKED")
        row = c.execute("SELECT response_json FROM prelive_operations WHERE principal_id=? AND operation_id=? AND command='preview_script_audio'", (actor_id, operation_id)).fetchone()
        if not row:
            raise BusinessError("CONTINUOUS_PREVIEW_REQUIRED")
        data = json.loads(row[0])["data"]
        if data.get("preparation_id") != review["publication"]["preparation_id"]:
            raise BusinessError("CONTINUOUS_SCOPE_MISMATCH")
        plan = service.store.exact(c, review["publication"]["compiled_plan_ref"])["body"]
        entries = [e for e in plan["entries"] if e["script_revision_ref"] == data["script_ref"]]
        if not entries:
            raise BusinessError("CONTINUOUS_SCRIPT_NOT_SELECTED")
        entry = entries[0]
        if any(e["sentences"] != entry["sentences"] for e in entries):
            raise BusinessError("CONTINUOUS_OPENING_SCOPE_AMBIGUOUS")
        keys = list(dict.fromkeys(s["module_key"] for s in entry["sentences"]))
        if keys != data["module_keys"] or [s["id"] for s in entry["sentences"]] != data["sentence_ids"]:
            raise BusinessError("CONTINUOUS_SENTENCE_SCOPE_MISMATCH")
    identity = data["audio_id"]
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise BusinessError("INVALID_PRECOMPILED_AUDIO")
    source = server.RUNTIME / "audio" / (identity + ".wav")
    subtitles = server.RUNTIME / "audio" / (identity + ".subtitles.json")
    if "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() != data["sha256_bytes"]:
        raise BusinessError("PRECOMPILED_AUDIO_CHANGED")
    if "sha256:" + hashlib.sha256(subtitles.read_bytes()).hexdigest() != data.get("subtitle_file_hash"):
        raise BusinessError("CONTINUOUS_SUBTITLES_CHANGED")
    with wave.open(str(source), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
    try:
        timeline = align_subtitles(json.loads(subtitles.read_text(encoding="utf-8"))["provider_subtitles"], entry["sentences"], duration)
    except (ValueError, KeyError, TypeError) as error:
        raise BusinessError("CONTINUOUS_ALIGNMENT_REQUIRED", "音频可试听，但句尾时间轴未通过校验，尚不能用于自动插播", details={"reason": str(error)}) from None
    directory = server.RUNTIME / "room_audio" / room_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if not target.exists():
        pending = target.with_suffix(".partial.wav")
        shutil.copyfile(source, pending)
        pending.replace(target)
    if "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() != data["sha256_bytes"]:
        raise BusinessError("PRECOMPILED_AUDIO_CHANGED")
    candidate = {"room_id": room_id, "publication_ref": pubref, "script_ref": data["script_ref"],
        "audio": {"audio_id": identity, "path": str(target.resolve()), "sha256_bytes": data["sha256_bytes"],
            "duration": duration, "voice": data["tts_selection"], "style": data["resolved_style"]},
        "timeline": timeline, "timeline_hash": digest(timeline), "source_operation_id": operation_id,
        "human_adoption": {"actor_id": actor_id, "authorization_quote": authorization_quote, "adopted_at": now()}}
    with service.store.transaction(write=True) as c:
        # Re-check after file IO: no stale adoption while the room changes.
        current = service.store.get(c, "config", room_id)
        if current["ref"] != room["ref"] or server.room_instance(room_id).state["status"] not in {"stopped", "completed"}:
            raise BusinessError("CONTINUOUS_ROOM_CHANGED", "请先结束本机测试播出，再采用连续音频")
        service._publication_review(c, pubref)
        identity = choice_id(pubref["id"], data["script_ref"]["id"])
        old = c.execute("SELECT revision FROM records WHERE kind='continuous_audio_choice' AND id=?", (identity,)).fetchone()
        ref = service.store.put(c, "continuous_audio_choice", identity, candidate, expected=old[0] if old else None, create=old is None)
        service.store.event(c, room_id, "continuous_audio.adopted", {"ref": ref, "script_ref": data["script_ref"], "source_operation_id": operation_id})
    return {"ref": ref, "script_ref": data["script_ref"], "duration": duration, "sentence_count": len(timeline),
            "audio_url": "/room-audio/" + room_id + "/" + target.name, "timeline_hash": candidate["timeline_hash"], "started": False}


def overlay(service, c, publication_ref, entry):
    key = choice_id(publication_ref["id"], entry["script_revision_ref"]["id"])
    row = c.execute("SELECT body FROM records WHERE kind='continuous_audio_choice' AND id=?", (key,)).fetchone()
    if not row:
        return entry
    body = json.loads(row[0])
    if body["publication_ref"] != publication_ref or body["script_ref"] != entry["script_revision_ref"] or digest(body["timeline"]) != body["timeline_hash"]:
        raise BusinessError("CONTINUOUS_BINDING_MISMATCH")
    if [s["id"] for s in entry["sentences"]] != [r["sentence_id"] for r in body["timeline"]]:
        raise BusinessError("CONTINUOUS_BINDING_MISMATCH")
    result = deepcopy(entry)
    for sentence, timing in zip(result["sentences"], body["timeline"]):
        sentence["audio"] = {**body["audio"], "continuous_track": {"id": key, "start": timing["start"], "end": timing["end"], "duration": body["audio"]["duration"]}}
    return result


def install(app, server, service, browser_identity):
    from fastapi.responses import JSONResponse

    @app.post("/api/rooms/{room_id}/continuous-audio/adopt")
    async def adopt_route(room_id: str, request: Request):
        try:
            principal, _ = browser_identity(request, write=True)
            data = await request.json()
            result = adopt(service, server, room_id, data.get("operation_id"), data.get("authorization_quote"), principal.actor_id)
            return JSONResponse(result)
        except BusinessError as error:
            return JSONResponse({"error": error.payload()}, status_code=error.status)
