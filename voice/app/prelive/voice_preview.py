"""Durable human TTS audition attempts; an uncertain render is never retried.

The claim commits before calling the provider.  Request cancellation cannot
cancel its background reconciliation.  A process restart leaves a durable
started attempt with no live owner; it is reported as unknown, never reissued.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from .storage import BusinessError, canonical, digest, now


_ACTIVE: dict[tuple[str, str], asyncio.Task] = {}
_AUDIO_URL = re.compile(r"^/audio/([A-Za-z0-9_-]+\.wav)$")
_UNKNOWN_MESSAGE = "试听请求已发出，但结果尚未确定。不会自动重新生成；请先核实原操作，避免重复付费。"


def _available(row):
    path = row["audio_path"]
    try:
        if not (path and Path(path).is_file() and Path(path).stat().st_size > 0):
            return False
        result = json.loads(row["response_json"]) if row["response_json"] else {}
        if result.get("command") == "preview_script_audio":
            import hashlib
            return (result.get("data") or {}).get("sha256_bytes") == "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_preview_receipt(c, actor_id: str, operation_id: str):
    """Return this audition's safe status, or None for another operation type.

    Called before the generic success receipt lookup so deleted audio is not
    incorrectly advertised as playable.  No provider request is made here.
    """
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prelive_voice_preview_attempts'").fetchone():
        return None
    row = c.execute("SELECT * FROM prelive_voice_preview_attempts WHERE principal_id=? AND operation_id=?", (actor_id, operation_id)).fetchone()
    if not row:
        return None
    base = {"operation_id": operation_id}
    if row["state"] == "succeeded":
        if not _available(row):
            return {**base, "state": "audio_unavailable", "preview_state": "unavailable", "message": "原试听已生成，但音频文件当前不可用。同一操作不会重新调用语音服务。"}
        return {**base, "state": "committed", "preview_state": "succeeded", "receipt": json.loads(row["response_json"])}
    task = _ACTIVE.get((row["attempt_id"], actor_id))
    if row["state"] == "started" and task and not task.done():
        return {**base, "state": "pending", "preview_state": "running", "message": "原试听仍在生成，请稍后核实；不会重复提交。"}
    return {**base, "state": "outcome_unknown", "preview_state": "unknown", "message": _UNKNOWN_MESSAGE}


class VoicePreviewService:
    def __init__(self, store, server):
        self.store, self.server = store, server
        self._claim_lock = asyncio.Lock()
        with store.transaction(write=True) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS prelive_voice_preview_attempts(
                principal_id TEXT NOT NULL,operation_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('started','succeeded','outcome_unknown')),
                response_json TEXT,audio_path TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(principal_id,operation_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS prelive_voice_preview_cache ON prelive_voice_preview_attempts(principal_id,request_hash,state)")

    def _claim(self, actor_id, operation_id, request_hash, run_id=None):
        with self.store.transaction(write=True) as c:
            row = c.execute("SELECT * FROM prelive_voice_preview_attempts WHERE principal_id=? AND operation_id=?", (actor_id, operation_id)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise BusinessError("IDEMPOTENCY_CONFLICT", "操作编号已用于不同的试听内容")
                return None, read_preview_receipt(c, actor_id, operation_id)
            # Operation IDs share a namespace with the rest of the workbench.
            if c.execute("SELECT 1 FROM prelive_operations WHERE principal_id=? AND operation_id=?", (actor_id, operation_id)).fetchone():
                raise BusinessError("IDEMPOTENCY_CONFLICT", "操作编号已用于其他操作")
            # Reuse an available successful audition, including after reload.
            cached = c.execute("SELECT * FROM prelive_voice_preview_attempts WHERE principal_id=? AND request_hash=? AND state='succeeded' ORDER BY updated_at DESC", (actor_id, request_hash)).fetchall()
            cached = next((item for item in cached if _available(item)), None)
            attempt_id, timestamp = "preview_" + uuid4().hex, now()
            if cached:
                result = {**json.loads(cached["response_json"]), "operation_id": operation_id}
                if result.get("command") == "preview_script_audio":
                    data = result.get("data", {})
                    data = {**data, "operation_id": operation_id, "cache_hit": True}
                    if "usage_characters" in data:
                        data["source_usage_characters"] = data.pop("usage_characters")
                    if "trace_id" in data:
                        data["source_trace_id"] = data.pop("trace_id")
                    result["data"] = data
                c.execute("INSERT INTO prelive_voice_preview_attempts VALUES(?,?,?,?,?,?,?,?,?)", (actor_id, operation_id, attempt_id, request_hash, "succeeded", canonical(result), cached["audio_path"], timestamp, timestamp))
                self._save_receipt(c, actor_id, operation_id, request_hash, result)
                return None, {"state": "committed", "receipt": result}
            # The public operation ID may change after tab reload, storage loss,
            # or a second browser tab. It must not bypass an uncertain paid call.
            # SQLite serializes this lookup with claiming the next attempt, so
            # equal content cannot race through different IDs or app instances.
            unsettled = c.execute("SELECT operation_id FROM prelive_voice_preview_attempts WHERE principal_id=? AND request_hash=? AND state IN ('started','outcome_unknown') ORDER BY created_at LIMIT 1", (actor_id, request_hash)).fetchone()
            if unsettled:
                return None, read_preview_receipt(c, actor_id, unsettled["operation_id"])
            c.execute("INSERT INTO prelive_voice_preview_attempts VALUES(?,?,?,?,?,?,?,?,?)", (actor_id, operation_id, attempt_id, request_hash, "started", None, None, timestamp, timestamp))
            self.store.event(c, "voice-preview", "human.voice-preview.started", {"operation_id": operation_id, "preview_state": "running", "run_id": run_id})
            return attempt_id, None

    def _save_receipt(self, c, actor_id, operation_id, request_hash, result):
        c.execute("INSERT INTO prelive_operations VALUES(?,?,?,?,?,?)", (actor_id, operation_id, result.get("command", "human.voice-preview"), request_hash, canonical(result), now()))
        self.store.event(c, "voice-preview", "human.voice-preview.succeeded", {"operation_id": operation_id, "preview_state": "succeeded", "run_id": (result.get("data") or {}).get("run_id")})

    def _finish(self, actor_id, operation_id, attempt_id, request_hash, result, audio_path):
        with self.store.transaction(write=True) as c:
            changed = c.execute("UPDATE prelive_voice_preview_attempts SET state='succeeded',response_json=?,audio_path=?,updated_at=? WHERE principal_id=? AND operation_id=? AND attempt_id=? AND state='started'", (canonical(result), str(audio_path), now(), actor_id, operation_id, attempt_id)).rowcount
            if changed != 1:
                raise BusinessError("PREVIEW_OUTCOME_UNKNOWN", _UNKNOWN_MESSAGE)
            self._save_receipt(c, actor_id, operation_id, request_hash, result)

    def _unknown(self, actor_id, operation_id, attempt_id):
        with self.store.transaction(write=True) as c:
            c.execute("UPDATE prelive_voice_preview_attempts SET state='outcome_unknown',updated_at=? WHERE principal_id=? AND operation_id=? AND attempt_id=? AND state='started'", (now(), actor_id, operation_id, attempt_id))
            self.store.event(c, "voice-preview", "human.voice-preview.outcome_unknown", {"operation_id": operation_id, "preview_state": "unknown"})

    async def _render(self, value, actor_id, attempt_id, request_hash, voice, style, command, result_context):
        try:
            raw = await asyncio.get_running_loop().run_in_executor(self.server.POOL, self.server.render, value.text, self.server.RUNTIME / "audio", voice["voice"], voice["provider"], voice["model"], style)
            # Only safe local audio results are allowed in receipts or logs.
            match = _AUDIO_URL.fullmatch(str(raw.get("url", ""))) if isinstance(raw, dict) else None
            duration = raw.get("duration") if isinstance(raw, dict) else None
            path = self.server.RUNTIME / "audio" / match.group(1) if match else None
            if not match or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0 or not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("Unverifiable audio output")
            result = {key: raw[key] for key in ("audio_id", "url", "duration", "engine", "provider", "voice", "model", "style", "resolved_style", "cache_hit", "usage_characters", "trace_id") if key in raw}
            result.update(operation_id=value.operation_id, preview_state="succeeded")
            if command != "human.voice-preview":
                import hashlib
                import wave
                with wave.open(str(path), "rb") as wav:
                    if wav.getnframes() <= 0 or wav.getframerate() <= 0 or wav.getnchannels() <= 0:
                        raise ValueError("Invalid WAV output")
                    result["duration"] = wav.getnframes() / wav.getframerate()
                result["sha256_bytes"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                result.update(result_context or {})
                if raw.get("resolved_style") is not None or raw.get("style") is not None:
                    result["resolved_style"] = raw.get("resolved_style", raw.get("style"))
                result = {"schema_version": "PRELIVE-2.3", "command": command,
                    "operation_id": value.operation_id, "status": "committed", "data": result,
                    "changed_paths": [], "invalidated_refs": [], "event_cursor": 0}
            await asyncio.to_thread(self._finish, actor_id, value.operation_id, attempt_id, request_hash, result, path.resolve())
            return result
        except BaseException:
            # No exception body is recorded: provider errors may contain secrets.
            await asyncio.to_thread(self._unknown, actor_id, value.operation_id, attempt_id)
            raise BusinessError("PREVIEW_OUTCOME_UNKNOWN", _UNKNOWN_MESSAGE, status=409, details={"operation_id": value.operation_id, "preview_state": "unknown"}) from None
        finally:
            _ACTIVE.pop((attempt_id, actor_id), None)

    async def preview(self, value, actor_id, *, style=None, request_binding=None,
                      command="human.voice-preview", result_context=None):
        voice = value.tts_selection.model_dump()
        self.server.voice_options({"tts_provider": voice["provider"], "tts_voice": voice["voice"], "tts_model": voice["model"]})
        style = self.server.sentence_style({"stage": "identity"}, voice["provider"]) if style is None else style
        # Idempotency binds the public request, not mutable internal style
        # defaults. Replaying the same request after deployment keeps its result.
        request = {"command": command, "voice": voice, "text": value.text}
        if request_binding is not None:
            request.update(binding=request_binding, style=style)
        request_hash = digest(request)
        async with self._claim_lock:
            attempt_id, prior = await asyncio.to_thread(self._claim, actor_id, value.operation_id, request_hash,
                (request_binding or {}).get("run_id"))
            if prior:
                if prior["state"] == "committed":
                    return prior["receipt"]
                code = {"pending": "PREVIEW_IN_PROGRESS", "audio_unavailable": "PREVIEW_AUDIO_UNAVAILABLE"}.get(prior["state"], "PREVIEW_OUTCOME_UNKNOWN")
                original = prior.get("operation_id", value.operation_id)
                raise BusinessError(code, prior["message"], details={"operation_id": original, "original_operation_id": original, "requested_operation_id": value.operation_id, "preview_state": prior["preview_state"]})
            task = asyncio.create_task(self._render(value, actor_id, attempt_id, request_hash, voice, style, command, result_context))
            _ACTIVE[(attempt_id, actor_id)] = task
            # Consume a detached error if the HTTP caller goes away. The durable
            # receipt remains queryable; asyncio must not log exception payloads.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)
