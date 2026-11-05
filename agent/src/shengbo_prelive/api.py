"""Trusted loopback HTTP boundary for the prelive Agent.

The voice service owns browser cookies, Origin and CSRF checks. This service
accepts only its independent service token, never a user-supplied principal.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import inspect
import json
import re
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field, ValidationError

from shengbo_contracts.common import ContractError, StrictModel, WorkScope
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.observability import sanitize

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


class ConversationRequest(StrictModel):
    client_request_id: str | None = None
    scope: WorkScope | None = None
    room_id: str | None = Field(default=None, max_length=200)
    preparation_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=160)


class CancelRequest(StrictModel):
    client_request_id: str | None = None
    operation_id: str | None = None
    expected_run_version: int | None = Field(default=None, ge=0)


def _require_id(value: str) -> str:
    if not _OPAQUE_ID.fullmatch(value) or ".." in value:
        raise ContractError("invalid_identifier", "标识格式无效", status=422)
    return value


def _require_local_scope(scope: WorkScope | None) -> None:
    if scope is not None and scope.account_id != "local":
        raise ContractError("account_scope_forbidden", "当前本机服务不允许指定其他账户", status=403)


async def _call(function, *args, **kwargs):
    value = function(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _response(value, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=_public_value(value), status_code=status_code)


def _public_value(value):
    safe = sanitize(value)
    def scrub(item):
        if isinstance(item, list):
            return [scrub(part) for part in item]
        if isinstance(item, dict):
            return {key: "[REDACTED]" if re.sub(r"[^a-z0-9]", "", key.lower()) in
                    {"servicetoken", "csrftoken", "token"} else scrub(part) for key, part in item.items()}
        return item
    return scrub(safe)


def create_app(engine, service_token: str) -> FastAPI:
    if not isinstance(service_token, str) or not service_token:
        raise ValueError("An independent service credential is required")
    token_bytes = service_token.encode("utf-8")

    @asynccontextmanager
    async def lifespan(app):
        owned_start = False
        if hasattr(engine, "start") and not getattr(engine, "started", False):
            await _call(engine.start)
            owned_start = True
        try:
            yield
        finally:
            if owned_start and hasattr(engine, "stop"):
                await _call(engine.stop)

    app = FastAPI(title="声播播前 Agent", version="PRELIVE-2.3", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_private_exception_output(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            # Do not let an exception containing credentials reach an ASGI
            # access/error logger. The engine already journals safe failures.
            return _response({"error": {"code": "internal_error", "message": "执行服务内部错误，请查看本机安全运行记录", "details": {}, "retryable": False}}, 500)

    async def trusted_proxy(x_shengbo_service_token: Annotated[str | None, Header()] = None):
        provided = (x_shengbo_service_token or "").encode("utf-8")
        if not hmac.compare_digest(provided, token_bytes):
            raise ContractError("service_auth_required", "服务身份验证失败", status=401)

    @app.exception_handler(ContractError)
    async def contract_error(request: Request, error: ContractError):
        return _response({"error": error.as_error().model_dump(mode="json")}, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Pydantic's default response includes the rejected input. It may contain
        # accidental credentials, so report only locations and error codes.
        issues = [{"path": list(item["loc"]), "code": item["type"]} for item in error.errors()]
        return _response({"error": {"code": "invalid_request", "message": "请求格式不符合当前接口合同", "details": {"issues": issues}, "retryable": False}}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        return _response({"error": {"code": "internal_error", "message": "执行服务内部错误，请查看本机安全运行记录", "details": {}, "retryable": False}}, 500)

    @app.get("/health")
    async def health():
        value = await _call(engine.health)
        from .document_readers import reader_tool_capabilities
        return _response({**value, "document_readers": reader_tool_capabilities()})

    protected = [Depends(trusted_proxy)]
    base = "/api/assistant/v2"

    @app.get(base + "/editor-locks", dependencies=protected)
    async def editor_locks():
        return _response(await _call(engine.editor_locks))

    @app.get(base + "/conversations", dependencies=protected)
    async def list_conversations(room_id: str | None = None, preparation_id: str | None = None):
        if room_id:
            _require_id(room_id)
        if preparation_id:
            _require_id(preparation_id)
        return _response(await _call(engine.list_conversations, room_id=room_id, preparation_id=preparation_id))

    @app.post(base + "/conversations", dependencies=protected)
    async def create_conversation(body: ConversationRequest):
        _require_local_scope(body.scope)
        for value in (body.room_id, body.preparation_id):
            if value:
                _require_id(value)
        if body.scope and body.scope.preparation_id and body.preparation_id and body.scope.preparation_id != body.preparation_id:
            raise ContractError("scope_mismatch", "本场标识与对话范围不一致", status=409)
        return _response(await _call(engine.create_conversation, body.model_dump(mode="json")), 201)

    @app.get(base + "/conversations/{conversation_id}", dependencies=protected)
    async def get_conversation(conversation_id: str):
        return _response(await _call(engine.get_conversation, _require_id(conversation_id)))

    @app.delete(base + "/conversations/{conversation_id}", dependencies=protected)
    async def delete_conversation(conversation_id: str):
        return _response(await _call(engine.delete_conversation, _require_id(conversation_id)))

    @app.post(base + "/conversations/{conversation_id}/messages", dependencies=protected)
    async def submit_message(conversation_id: str, body: MessageRequest):
        _require_local_scope(body.scope)
        if not body.text.strip() and not body.material_refs:
            raise ContractError("empty_message", "请提供任务要求或资料", status=422)
        if not body.client_request_id and not body.client_message_id:
            raise ContractError("request_id_required", "任务必须包含稳定请求编号", status=422)
        return _response(await _call(engine.submit_message, _require_id(conversation_id), body), 202)

    @app.post(base + "/materials", dependencies=protected)
    async def accept_material(file: Annotated[UploadFile, File()],
                              conversation_id: Annotated[str, Form()],
                              scope: Annotated[str | None, Form()] = None,
                              client_request_id: Annotated[str | None, Form()] = None):
        _require_id(conversation_id)
        if scope is not None:
            try:
                parsed_scope = WorkScope.model_validate_json(scope)
            except (ValidationError, ValueError):
                raise ContractError("invalid_scope", "资料范围格式无效", status=422) from None
            _require_local_scope(parsed_scope)
        name = file.filename or "material"
        if len(name) > 240 or any(char in name for char in ("/", "\\", "\x00", ":")) or name in {".", ".."}:
            await file.close()
            raise ContractError("invalid_filename", "文件名包含不允许的路径字符", status=422)
        try:
            # Read only one byte beyond the limit; never load an unbounded body.
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise ContractError("material_too_large", "单个资料不能超过 128 MiB；各格式还受独立解析限制", status=413)
            if not data:
                raise ContractError("empty_material", "资料文件为空", status=422)
            # Native document parsing/legacy Word conversion can be slow. Keep
            # chat polling and cancellation responsive during source intake.
            if inspect.iscoroutinefunction(engine.accept_material):
                result = await engine.accept_material(conversation_id, name, file.content_type or "application/octet-stream", data)
            else:
                result = await asyncio.to_thread(engine.accept_material, conversation_id, name,
                                               file.content_type or "application/octet-stream", data)
            return _response(result, 201)
        finally:
            await file.close()

    @app.get(base + "/runs/{run_id}", dependencies=protected)
    async def get_run(run_id: str):
        value = await _call(engine.get_run, _require_id(run_id))
        if value.get("status") == "COMPLETED" and value.get("catalog_intake_result"):
            from .catalog_intake_report import refresh_catalog_intake_requirements
            report = await refresh_catalog_intake_requirements(engine.client, value["catalog_intake_result"])
            value = {**value, "catalog_intake_result": report}
        return _response(value)

    @app.post(base + "/runs/{run_id}/cancel", dependencies=protected)
    async def cancel_run(run_id: str, body: CancelRequest | None = None):
        return _response(await _call(engine.cancel_run, _require_id(run_id)), 202)

    @app.post(base + "/decisions/{decision_id}/answers", dependencies=protected)
    async def answer_decision(decision_id: str, body: AnswerRequest):
        _require_id(decision_id)
        if body.decision_id and body.decision_id != decision_id:
            raise ContractError("decision_mismatch", "答案与确认项不一致", status=409)
        return _response(await _call(engine.answer, decision_id, body))

    @app.post(base + "/runs/{run_id}/answers", dependencies=protected)
    async def answer_run(run_id: str, body: AnswerRequest):
        _require_id(run_id)
        if not body.decision_id:
            raise ContractError("decision_id_required", "答案需要指定确认项", status=422)
        await _call(engine.get_run, run_id)
        if not await _call(engine.decision_belongs_to_run, body.decision_id, run_id):
            raise ContractError("decision_not_in_run", "该确认项不属于此任务", status=403)
        return _response(await _call(engine.answer, _require_id(body.decision_id), body))

    @app.get(base + "/runs/{run_id}/events", dependencies=protected)
    async def events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        return _response(await _call(engine.events, _require_id(run_id), after, limit=limit))

    @app.get(base + "/runs/{run_id}/payloads/{payload_ref}", dependencies=protected)
    async def payload(run_id: str, payload_ref: str):
        # The engine resolves an opaque, run-owned ID; this API never opens paths.
        return _response(await _call(engine.read_payload, _require_id(run_id), _require_id(payload_ref)))

    @app.get(base + "/workspace", dependencies=protected)
    async def workspace(conversation_id: str | None = None, scope: str | None = Query(None, max_length=10000)):
        parsed = None
        if conversation_id:
            _require_id(conversation_id)
        if scope is not None:
            try:
                parsed = WorkScope.model_validate_json(scope)
            except (ValidationError, ValueError):
                raise ContractError("invalid_scope", "工作区范围格式无效", status=422) from None
            _require_local_scope(parsed)
        return _response(await _call(engine.workspace, conversation_id=conversation_id,
                                     scope=parsed.model_dump(mode="json") if parsed else None))

    @app.get(base + "/events", dependencies=protected)
    async def stream_events(request: Request, run_id: str | None = None,
                            after: int = Query(0, ge=0), agent_after: int | None = Query(None, ge=0),
                            last_event_id: Annotated[str | None, Header()] = None):
        if run_id:
            _require_id(run_id)
        cursor = max(after, agent_after or 0)
        if last_event_id:
            if not last_event_id.isdecimal():
                raise ContractError("invalid_cursor", "事件游标格式无效", status=422)
            cursor = max(cursor, int(last_event_id))

        async def generate():
            nonlocal cursor
            idle = 0
            try:
                while not await request.is_disconnected():
                    page = await _call(engine.events, run_id, cursor, limit=200) if run_id else await _call(engine.all_events, cursor)
                    records = page.get("events", page.get("items", [])) if isinstance(page, dict) else page
                    for record in records:
                        # A run has its own EventEnvelope.sequence. Global SSE
                        # uses an independent persistent stream position and must
                        # never overwrite or compare different runs' sequences.
                        if run_id:
                            event = record
                            position = int(event.get("sequence", 0))
                        else:
                            event = record["event"]
                            position = int(record["stream_sequence"])
                        safe = _public_value(event)
                        if position <= cursor:
                            continue
                        cursor = position
                        event_type = safe.get("type", "execution.event")
                        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(event_type)):
                            event_type = "execution.event"
                        yield f"id: {cursor}\nevent: {event_type}\ndata: {json.dumps(safe, ensure_ascii=False, allow_nan=False)}\n\n"
                    idle = 0 if records else idle + 1
                    if idle >= 30:
                        idle = 0
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                # Closing a browser stream stops only the projection, not the run.
                return
            except Exception:
                # Streaming starts after response headers; still avoid exposing an
                # exception string or letting it reach a default ASGI error logger.
                yield 'event: stream.error\ndata: {"error":{"code":"event_stream_unavailable","message":"过程连接暂时不可用，请重新连接"}}\n\n'
                return

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
