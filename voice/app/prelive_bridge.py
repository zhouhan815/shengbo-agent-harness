"""Explicit same-origin browser bridge and existing-engine prelive adapters.

Importing this module does not read OAuth, run providers, start workers or play
audio. `install` mounts routes; only the application lifespan wakes job recovery.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
import wave

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from shengbo_contracts.common import Principal, WorkScope, canonical_json
from shengbo_prelive.security import service_tokens, token_bytes
from .prelive.routes import create_router
from .prelive.service import PreliveService
from .prelive.storage import BusinessError
from .prelive.module_generation import MODULE_KEYS, generate_one_module
from .voice_catalog import voice_catalog
from .speech import SpeechRenderError, tts_diagnostics

COOKIE = "shengbo_prelive_session"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_AUDIO_ID = re.compile(r"^[a-f0-9]{64}$")


class _BorrowedProxyTransport(httpx.AsyncBaseTransport):
    """A request client may close its lease, never the app-owned transport.

    Injected transports are shared by concurrent polling and streaming clients.
    Closing one request must not abort another request's in-flight response.
    """

    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        pass


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or ".." in value:
        raise BusinessError("INVALID_IDENTIFIER", "标识格式无效", status=422)
    return value


def _local_origin(request, *, required=False):
    if request.url.hostname not in {"127.0.0.1", "localhost"}:
        raise BusinessError("LOCAL_ONLY", "仅允许本机访问", status=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise BusinessError("CROSS_SITE_REJECTED", "请求来源不匹配", status=403)
    origin = request.headers.get("origin")
    if origin is None and not required:
        return
    try:
        parsed = urlsplit(origin or "")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected = request.url.port or (443 if request.url.scheme == "https" else 80)
        valid = (parsed.scheme == request.url.scheme and parsed.hostname in {"127.0.0.1", "localhost"}
                 and port == expected and not parsed.username and not parsed.password and parsed.path in {"", "/"})
    except ValueError:
        valid = False
    if not valid:
        raise BusinessError("ORIGIN_MISMATCH", "请求来源不匹配", status=403)


_VERIFIED_AUDIO = {}


def _audio_asset(server, room_id, asset):
    _id(room_id)
    identity = asset.get("audio_id")
    if not isinstance(identity, str) or not _AUDIO_ID.fullmatch(identity):
        raise BusinessError("INVALID_PRECOMPILED_AUDIO", "预生成语音标识无效")
    expected = (server.RUNTIME / "room_audio" / room_id / (identity + ".wav")).resolve()
    actual = Path(asset.get("path", "")).resolve()
    if actual != expected or not actual.is_relative_to(server.RUNTIME.resolve()) or not actual.is_file():
        raise BusinessError("INVALID_PRECOMPILED_AUDIO", "预生成语音文件不存在或不属于本直播间")
    stat = actual.stat()
    file_key = (str(actual), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, asset.get("sha256_bytes"))
    if file_key not in _VERIFIED_AUDIO:
        if asset.get("sha256_bytes") and "sha256:" + hashlib.sha256(actual.read_bytes()).hexdigest() != asset["sha256_bytes"]:
            raise BusinessError("PRECOMPILED_AUDIO_CHANGED", "预生成语音文件发生变化")
    try:
        with wave.open(str(actual), "rb") as source:
            if source.getnframes() < 100 or source.getframerate() <= 0:
                raise ValueError()
            duration = source.getnframes() / source.getframerate()
    except Exception:
        raise BusinessError("INVALID_PRECOMPILED_AUDIO", "预生成语音不是有效WAV文件") from None
    _VERIFIED_AUDIO[file_key] = True
    if len(_VERIFIED_AUDIO) > 512:
        _VERIFIED_AUDIO.pop(next(iter(_VERIFIED_AUDIO)))
    timing = asset.get("continuous_track")
    if timing:
        import math
        begin, end = timing.get("start"), timing.get("end")
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (begin, end))
                or not 0 <= begin < end <= duration + .001):
            raise BusinessError("CONTINUOUS_RANGE_INVALID")
        duration = end - begin
    return {**deepcopy(asset), "path": str(actual), "url": f"/room-audio/{room_id}/{identity}.wav", "duration": duration}


async def prepare_precompiled(room, epoch, item, server_module):
    """Called explicitly by Room.prepare; no legacy script lookup or live TTS."""
    server = server_module
    try:
        head = server.get("product", item["product_id"])
        pinned = item.get("pinned_product_ref", {})
        if (head.get("archived") or head.get("sale_status", "available") != "available" or head.get("withdrawn")
                or pinned.get("revision") in head.get("withdrawn_revisions", [])):
            raise BusinessError("PRODUCT_WITHDRAWN", "商品已停售或撤回，已停止继续播报")
        audio = await asyncio.to_thread(_audio_asset, server, room.state["room_id"], item["prelive_audio"])
        async with room.lock:
            if epoch != room.state["epoch"]:
                return None
            room.state["audio"] = audio
            if room.state["status"] not in {"paused", "blocked"}:
                room.state["status"] = "ready"
            room.log("audio_ready", audio["audio_id"])
            await room.broadcast()
    except Exception as error:
        async with room.lock:
            if epoch == room.state["epoch"]:
                message = error.message if isinstance(error, BusinessError) else "预生成语音校验失败，已停止当前句"
                room.state.update(status="error", error=message)
                room.log("prelive_audio_rejected", {"sentence_id": item.get("id"), "code": error.code if isinstance(error, BusinessError) else "AUDIO_CHECK_FAILED"})
                await room.broadcast()
    return None


class ExistingVoiceAdapter:
    def __init__(self, server_module, *, module_transport=None):
        self.server = server_module
        self.service = None
        # Tests may inject an HTTP transport object, never an endpoint override.
        self.module_transport = module_transport

    def module_generation_signature(self):
        """Public configuration only, used to invalidate incompatible reuse."""
        return {"provider": "deepseek", "model": self.server.os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")}

    def script_generation_signature(self):
        """Whole responses never share a cache identity with legacy modules."""
        from .prelive.whole_script_generation import PROTOCOL, whole_script_flash_options
        if options := whole_script_flash_options():
            return {"provider": "deepseek", "model": options["model"], "thinking": options["thinking"], "protocol": PROTOCOL}
        return {**self.module_generation_signature(), "protocol": PROTOCOL}

    async def generate_script(self, context, actions, product, *, response_observer=None):
        from .prelive.whole_script_generation import generate_one_script
        if not isinstance(context, dict):
            raise BusinessError("INVALID_MODULE_CONTEXT", status=422)
        records = context.get("records", [])
        records = list(records.values()) if isinstance(records, dict) else records
        if not isinstance(records, list):
            raise BusinessError("INVALID_MODULE_CONTEXT", status=422)
        candidates = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if context.get("draft_ref") and record.get("ref") != context["draft_ref"]:
                continue
            body = record.get("body", record)
            if isinstance(body, dict) and isinstance(body.get("draft"), dict):
                candidates.append(body["draft"])
        if len(candidates) != 1:
            raise BusinessError("MODULE_DRAFT_SNAPSHOT_MISSING", "本场配置快照缺失或不明确，未调用供应商，请重建上下文")
        commerce = self.server.commerce.parse_room(candidates[0].get("commerce", {}))
        effective = self.server.commerce.effective_product(product, {"commerce": commerce})
        return await generate_one_script(effective, context, actions,
            api_key=self.server.os.environ.get("DEEPSEEK_API_KEY", ""),
            model=self.server.os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            transport=self.module_transport, response_observer=response_observer)

    async def generate_module(self, context, module_key, product, previous, *, response_observer=None):
        if module_key not in MODULE_KEYS:
            raise BusinessError("UNKNOWN_MODULE_KEY")
        server = self.server
        # Select the exact immutable draft named by the bundle, never a room head
        # or whichever draft happened to occur last in the supplied records.
        if not isinstance(context, dict):
            raise BusinessError("INVALID_MODULE_CONTEXT", status=422)
        records = context.get("records", [])
        records = list(records.values()) if isinstance(records, dict) else records
        if not isinstance(records, list):
            raise BusinessError("INVALID_MODULE_CONTEXT", status=422)
        candidates = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if context.get("draft_ref") and record.get("ref") != context["draft_ref"]:
                continue
            body = record.get("body", record)
            if isinstance(body, dict) and isinstance(body.get("draft"), dict):
                candidates.append(body["draft"])
        if len(candidates) != 1:
            raise BusinessError("MODULE_DRAFT_SNAPSHOT_MISSING", "本场配置快照缺失或不明确，未调用供应商，请重建上下文")
        draft = candidates[0]
        commerce = server.commerce.parse_room(draft.get("commerce", {}))
        effective = server.commerce.effective_product(product, {"commerce": commerce})
        return await generate_one_module(effective, context, module_key, previous,
                                         api_key=server.os.environ.get("DEEPSEEK_API_KEY", ""),
                                         model=server.os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                                         transport=self.module_transport, response_observer=response_observer)

    async def render_audio(self, text, room_id, voice, *, style=None, response_observer=None):
        server = self.server
        _id(room_id)
        server.settings(room_id)
        selected = server.voice_options({"tts_provider": voice.get("provider", "system"), "tts_voice": voice.get("voice", "Huihui"),
                                         "tts_model": voice.get("model", "speech-2.8-turbo")})
        directory = server.RUNTIME / "room_audio" / room_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            # The caller owns the exact provider attempt and durable ledger.
            # Forward only its callback; never infer an attempt from text or
            # write a second ledger in this adapter. Legacy no-style calls
            # retain their original frozen synthesis semantics.
            render_kwargs = {"style": style} if style is not None else {}
            if response_observer is not None and selected["provider"] == "minimax":
                render_kwargs["response_observer"] = response_observer
            asset = await asyncio.to_thread(server.render, text, directory, selected["voice"],
                selected["provider"], selected["model"], **render_kwargs)
            identity = asset.get("audio_id", "")
            candidate = {**asset, "path": str(directory / (identity + ".wav")), "voice": selected}
            result = await asyncio.to_thread(_audio_asset, server, room_id, candidate)
            result["sha256_bytes"] = "sha256:" + hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()
            return result
        except BusinessError:
            raise
        except SpeechRenderError as error:
            details = {"tts_diagnostics": deepcopy(error.diagnostics)}
            if error.diagnostics.get("outcome") == "known_rejected":
                raise BusinessError("TTS_PROVIDER_REJECTED", "语音供应商已明确拒绝本次请求，请查看安全诊断信息", details=details) from None
            if error.diagnostics.get("outcome") == "not_sent":
                raise BusinessError("TTS_RENDER_FAILED", "语音请求尚未发出，请检查本地配置", details=details) from None
            raise BusinessError("TTS_OUTCOME_UNKNOWN", "语音请求结果未知，请先核实，不能自动重复付费", details=details) from None
        except Exception as error:
            if selected["provider"] == "minimax":
                # An untyped third-party failure cannot prove a paid request
                # was rejected. Never classify its prose, which may be secret.
                safe = tts_diagnostics("adapter", "outcome_unknown", exception=error)
                raise BusinessError("TTS_OUTCOME_UNKNOWN", "语音请求结果未知，请先核实，不能自动重复付费",
                    details={"tts_diagnostics": safe}) from None
            raise BusinessError("TTS_RENDER_FAILED", "语音生成失败或WAV校验未通过，未标记为可播出") from None

    async def start_publication(self, room_id, publication, compiled_entries):
        server = self.server
        _id(room_id)
        if not compiled_entries or self.service is None:
            raise BusinessError("EMPTY_COMPILED_PUBLICATION")
        products, scripts = {}, {}
        with self.service.store.transaction() as connection:
            # Execution settings belong to the reviewed immutable publication.
            # Room defaults may change after adoption and are never consulted here.
            draft_record = self.service.store.exact(connection, publication["draft_ref"])
            playlist_record = self.service.store.exact(connection, publication["playlist_ref"])
            if (draft_record["body"].get("preparation_id") != publication["preparation_id"]
                    or playlist_record["body"].get("preparation_id") != publication["preparation_id"]):
                raise BusinessError("PUBLICATION_SETTINGS_MISMATCH")
            voice = draft_record["body"]["draft"]["tts_selection"]
            selected = server.voice_options({"tts_provider": voice["provider"], "tts_voice": voice["voice"],
                                             "tts_model": voice.get("model", "speech-2.8-turbo")})
            for ref in publication["product_refs"]:
                products[ref["id"]] = {"ref": ref, "body": self.service.store.exact(connection, ref)["body"]}
            for entry in compiled_entries:
                ref = entry["script_revision_ref"]
                scripts[canonical_json(ref)] = self.service.store.exact(connection, ref)["body"]
            from .continuous_audio import overlay
            compiled_entries = [overlay(self.service, connection, publication["publication_ref"], entry) for entry in compiled_entries]
        queue = []
        for entry in compiled_entries:
            product = products.get(entry["product_id"])
            if not product:
                raise BusinessError("COMPILED_PRODUCT_MISMATCH")
            script = scripts[canonical_json(entry["script_revision_ref"])]
            from .welcome import build_product_templates, validate_templates
            welcome_templates = (validate_templates(script["welcome_templates"]) if script.get("welcome_templates")
                                 else build_product_templates(product["body"]))
            version_number = script["version_number"]
            module_order = list(dict.fromkeys(sentence.get("module_key", "product_intro") for sentence in entry["sentences"]))
            for sentence_index, sentence in enumerate(entry["sentences"], 1):
                asset = await asyncio.to_thread(_audio_asset, server, room_id, sentence["audio"])
                source_sentence_id = sentence["id"]
                playback_sentence_id = "compiled_sentence_" + hashlib.sha256(canonical_json({"entry": entry["compiled_entry_id"], "sentence": source_sentence_id, "offset": sentence_index}).encode()).hexdigest()[:32]
                module_key = sentence.get("module_key", "product_intro")
                queue.append({**deepcopy(sentence), "id": playback_sentence_id, "source_sentence_id": source_sentence_id,
                              "stage": module_key, "block_id": sentence.get("module_ref", {}).get("id", module_key),
                              "block_ordinal": module_order.index(module_key) + 1,
                              "queue_id": server.uid("queued"), "product_id": entry["product_id"],
                              "product_name": product["body"]["name"], "image": product["body"].get("image", ""),
                              "script_id": entry["script_revision_ref"]["id"], "version": f"第{version_number}版",
                              "revision": script["script_revision"], "version_number": version_number,
                              "variant_id": script["variant_id"], "script_revision_ref": deepcopy(entry["script_revision_ref"]),
                              "welcome_templates": deepcopy(welcome_templates),
                              "product_number": product["body"].get("display_number"), "ordinal": sentence_index,
                              "total_sentences": len(entry["sentences"]), "source": "prelive-publication",
                              "prelive_audio": asset, "pinned_product_ref": product["ref"], "publication_ref": publication["publication_ref"],
                              **{k: deepcopy(entry[k]) for k in ("compiled_entry_id", "original_position", "occurrence_id", "opening_mode")}})
        if not queue:
            raise BusinessError("EMPTY_COMPILED_PUBLICATION")
        async with server.start_lock:
            target = server.room_instance(room_id)
            async with target.lock:
                starts = getattr(target, "_prelive_starts", {})
                action = publication["action_id"]
                if action in starts:
                    old = starts[action]
                    if old["publication_ref"] != publication["publication_ref"]:
                        raise BusinessError("PLAYBACK_ACTION_REUSED")
                    return deepcopy(old["receipt"])
                active = [r for r in server.rooms.values() if r.state["status"] not in {"stopped", "completed"}]
                if active:
                    raise BusinessError("LIVE_SLOT_BUSY", "已有直播间正在播出，请先结束当前场次")
                target.state.update(session_id=server.uid("session"), queue=queue, index=0, played=[], insertion=None,
                                    loop=True, tts=selected, publication_ref=deepcopy(publication["publication_ref"]),
                                    recovery_required=False)
                target.resume_item = None
                target.set_current(queue[0])
                receipt = {"session_id": target.state["session_id"], "room_id": room_id,
                           "queue_sentences": len(queue), "compiled_entries": len(compiled_entries),
                           "playback_url": f"/live/{room_id}"}
                starts[action] = {"publication_ref": deepcopy(publication["publication_ref"]), "receipt": deepcopy(receipt)}
                target._prelive_starts = starts
                target.log("prelive_publication_started", {"publication_ref": publication["publication_ref"], "queue_sentences": len(queue)})
                await target.broadcast()
                return receipt

    def voice_options(self):
        import os
        configured = bool(os.environ.get("MINIMAX_API_KEY"))
        return {**voice_catalog(),
                "provider_status": {"system": {"available": os.name == "nt", "reason": "本机 Windows 普通话音色；请试听确认已安装。" if os.name == "nt" else "本机语音需要 Windows 普通话音色。"},
                                    "minimax": {"available": configured, "reason": "已配置云端语音，试听和生成会使用该服务。" if configured else "尚未配置云端语音，不能试听或生成；已有音色选择会保留。"}}}


def install(app, server_module):
    if hasattr(app.state, "prelive_service"):
        return app.state.prelive_service
    tokens = service_tokens()
    adapter = ExistingVoiceAdapter(server_module)
    service = PreliveService(server_module.RUNTIME, adapter=adapter)
    adapter.service = service
    sessions = {}
    app.state.prelive_service = service
    # Test-only injection is a transport object, never a production URL override.
    app.state.prelive_proxy_transport = None

    def browser_identity(request, *, write=False):
        _local_origin(request, required=write)
        cookie = request.cookies.get(COOKIE)
        value = sessions.get(cookie)
        if not value or value["expires"] < time.time():
            raise BusinessError("LOCAL_SESSION_REQUIRED", "请刷新本机页面后重试", status=401)
        if write:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied.encode(), value["csrf"].encode()):
                raise BusinessError("CSRF_REJECTED", "请求校验未通过，请刷新页面", status=403)
        return Principal(kind="human", actor_id="local-user", account_id="local"), None

    def identity(request):
        supplied = request.headers.get("x-shengbo-service-token")
        if supplied is None:
            return browser_identity(request, write=request.method not in {"GET", "HEAD", "OPTIONS"})
        if request.url.hostname not in {"127.0.0.1", "localhost"}:
            raise BusinessError("LOCAL_ONLY", status=403)
        raw = supplied.encode()
        if hmac.compare_digest(raw, token_bytes(tokens["agent_to_voice"])):
            kind, actor = "agent", "agent-runtime"
        elif hmac.compare_digest(raw, token_bytes(tokens["controller_to_voice"])):
            kind, actor = "controller", "controller-runtime"
        else:
            raise BusinessError("SERVICE_AUTH_REJECTED", status=403)
        run_id = request.headers.get("x-shengbo-run-id")
        if kind == "agent" and not run_id:
            raise BusinessError("RUN_ID_REQUIRED", status=403)
        if run_id:
            _id(run_id)
        scope = None
        encoded = request.headers.get("x-shengbo-scope")
        if encoded:
            try:
                if len(encoded) > 4096:
                    raise ValueError()
                scope = WorkScope.model_validate(json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True)))
                if scope.account_id != "local":
                    raise ValueError()
            except Exception:
                raise BusinessError("SERVICE_SCOPE_INVALID", status=403) from None
        return Principal(kind=kind, actor_id=actor, account_id="local", run_id=run_id), scope

    app.include_router(create_router(service, identity))
    from .prelive.human_workbench import install_human_workbench
    install_human_workbench(app, server_module, service, browser_identity)
    from .continuous_audio import install as install_continuous_audio
    install_continuous_audio(app, server_module, service, browser_identity)

    @app.get("/api/assistant/v2/session")
    async def session(request: Request):
        try:
            _local_origin(request)
            now = time.time()
            for key in list(sessions):
                if sessions[key]["expires"] < now:
                    sessions.pop(key, None)
            cookie = request.cookies.get(COOKIE)
            if cookie not in sessions:
                cookie = secrets.token_urlsafe(32)
                sessions[cookie] = {"csrf": secrets.token_urlsafe(32), "expires": now + 12 * 3600}
            response = JSONResponse({"csrf_token": sessions[cookie]["csrf"]})
            response.set_cookie(COOKIE, cookie, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/api", max_age=12 * 3600)
            response.headers["Cache-Control"] = "no-store"
            return response
        except BusinessError as error:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": error.payload()}, status_code=error.status)

    from .model_key_setup import install_model_key_setup
    install_model_key_setup(app, server_module, browser_identity)

    @app.api_route("/api/assistant/v2/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        client = response = None
        try:
            browser_identity(request, write=request.method != "GET")
            if not path or any(part in {"", ".", ".."} for part in path.split("/")) or "\\" in path or "%" in path:
                raise BusinessError("INVALID_PROXY_PATH", status=422)
            shared_transport = app.state.prelive_proxy_transport
            client = httpx.AsyncClient(base_url="http://127.0.0.1:8791",
                                       transport=_BorrowedProxyTransport(shared_transport) if shared_transport is not None else None,
                                       timeout=httpx.Timeout(90, connect=5), follow_redirects=False, trust_env=False)
            headers = {"X-Shengbo-Service-Token": tokens["voice_to_agent"]}
            if request.headers.get("content-type"):
                headers["Content-Type"] = request.headers["content-type"]
            headers["Accept"] = request.headers.get("accept", "application/json")
            # No browser Authorization/Cookie/actor/scope/run ID is forwarded.
            body_limit = (130 if path == "materials" else 12) * 1024 * 1024
            if int(request.headers.get("content-length", "0")) > body_limit:
                raise BusinessError("PROXY_BODY_TOO_LARGE", status=413)
            chunks, received = [], 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > body_limit:
                    raise BusinessError("PROXY_BODY_TOO_LARGE", status=413)
                chunks.append(chunk)
            body_content = b"".join(chunks)
            upstream = client.build_request(request.method, "/api/assistant/v2/" + path,
                                            params=request.query_params, headers=headers, content=body_content)
            response = await client.send(upstream, stream=True)
            if "text/event-stream" in response.headers.get("content-type", ""):
                async def body():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    finally:
                        await response.aclose()
                        await client.aclose()
                # Closing this subscriber closes only its HTTP stream, never the
                # separately owned Agent run. No cancel endpoint is called.
                return StreamingResponse(body(), status_code=response.status_code, media_type="text/event-stream",
                                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
            content = await response.aread()
            await response.aclose()
            await client.aclose()
            return __import__("starlette.responses", fromlist=["Response"]).Response(content, status_code=response.status_code,
                       media_type=response.headers.get("content-type", "application/json"), headers={"Cache-Control": "no-store"})
        except BusinessError as error:
            if response is not None:
                await response.aclose()
            if client is not None:
                await client.aclose()
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": error.payload()}, status_code=error.status)
        except Exception:
            if response is not None:
                await response.aclose()
            if client is not None:
                await client.aclose()
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": {"code": "AGENT_UNAVAILABLE", "message": "播前助手暂未连接，请检查本机启动状态", "details": {}, "retryable": True}}, status_code=503)

    previous_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            service.wake_worker()
            try:
                yield
            finally:
                try:
                    await service.close()
                finally:
                    transport = application.state.prelive_proxy_transport
                    if transport is not None:
                        await transport.aclose()
    app.router.lifespan_context = lifespan
    return service
