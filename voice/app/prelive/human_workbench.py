"""Human-only workbench helpers. No new model tool or implicit paid action."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from typing import Literal

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, field_validator
from shengbo_contracts.business import DTO, VoiceSelection
from shengbo_contracts.common import RecordRef
from .. import commerce_config as commerce
from .storage import BusinessError, canonical, digest, now
from .voice_preview import VoicePreviewService


class RoomSettingsInput(DTO):
    room_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    tts_selection: VoiceSelection
    version_count: int = Field(ge=1, le=6)
    loop_mode: Literal["infinite", "once"] = "infinite"

    @field_validator("loop_mode")
    @classmethod
    def always_loop(cls, value):
        # Older saved clients may still send once; the current product rule is
        # always repeat the ordered queue. Never persist the legacy stop mode.
        return "infinite"


class ArchiveInput(DTO):
    product_ref: RecordRef
    operation_id: str = Field(min_length=1, max_length=200)


class PriceInput(DTO):
    preparation_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    commerce: commerce.RoomCommerce


class VoicePreviewInput(DTO):
    operation_id: str = Field(min_length=1, max_length=200)
    tts_selection: VoiceSelection
    text: str = Field(default="欢迎来到声播直播间。这是一段普通话音色试听，商品资料请以实际内容为准。", min_length=1, max_length=500)


def install_human_workbench(app, server, service, browser_identity):
    """Mount same-origin CSRF guarded manual UI operations only."""
    previews = VoicePreviewService(service.store, server)
    def mutate(value, command, actor_id):
        data = value.model_dump(mode="json")
        request_hash = digest({"command": command, "input": data})
        with service.store.transaction(write=True) as c:
            prior = c.execute("SELECT request_hash,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?", (actor_id, value.operation_id)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise BusinessError("IDEMPOTENCY_CONFLICT", "操作编号已用于不同内容")
                return json.loads(prior["response_json"])
            if c.execute("SELECT 1 FROM prelive_voice_preview_attempts WHERE principal_id=? AND operation_id=?", (actor_id, value.operation_id)).fetchone():
                raise BusinessError("IDEMPOTENCY_CONFLICT", "操作编号已用于试听请求")
            if command == "human.room-settings":
                room = service.store.get(c, "config", value.room_id)
                body = deepcopy(room["body"])
                voice = value.tts_selection.model_dump()
                server.voice_options({"tts_provider": voice["provider"], "tts_voice": voice["voice"], "tts_model": voice["model"]})
                name = value.name.strip()
                if not name:
                    raise BusinessError("INVALID_NAME", "请输入直播间名称", status=422)
                body.update(name=name, tts_provider=voice["provider"], tts_voice=voice["voice"], tts_model=voice["model"], version_count=value.version_count, loop=True, updated_at=now())
                result = {"room_ref": service.store.put(c, "config", value.room_id, body, expected=value.expected_revision)}
                stream = value.room_id
            else:
                if value.product_ref.kind != "product":
                    raise BusinessError("REF_KIND_MISMATCH", "归档目标必须是商品", status=422)
                old = service.store.exact(c, value.product_ref)
                body = deepcopy(old["body"])
                body.update(archived=True, updated_at=now())
                result = {"product_ref": service.store.put(c, "product", old["ref"]["id"], body, expected=value.product_ref.revision)}
                stream = "catalog"
            service.store.event(c, stream, command, result)
            c.execute("INSERT INTO prelive_operations VALUES(?,?,?,?,?,?)", (actor_id, value.operation_id, command, request_hash, canonical(result), now()))
            return result

    def price_preview(value):
        with service.store.transaction() as c:
            prep = service._prep(c, value.preparation_id, value.expected_revision)
            products = [service.store.exact(c, ref) for ref in prep["body"]["selected_product_refs"]]
            business = commerce.validate_room(value.commerce.model_dump(), {p["ref"]["id"]: p["body"] for p in products})
            return {"items": [{"product_ref": p["ref"], "name": p["body"]["name"], "quote": commerce.quote(p["body"], {"commerce": business})} for p in products], "blockers": service._currentness(c, prep["body"])["blockers"]}

    async def handle(request, model, operation):
        try:
            principal, _ = browser_identity(request, write=True)
            if principal.kind != "human":
                raise BusinessError("HUMAN_REQUIRED", status=403)
            value = model.model_validate(await request.json())
            if operation == "price-preview":
                result = await asyncio.to_thread(price_preview, value)
            elif operation == "voice-preview":
                result = await previews.preview(value, principal.actor_id)
            else:
                result = await asyncio.to_thread(mutate, value, "human." + operation, principal.actor_id)
            return JSONResponse(result)
        except BusinessError as error:
            return JSONResponse({"error": error.payload()}, status_code=error.status)
        except ValidationError as error:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "配置字段不符合要求", "details": {"fields": [{"path": list(e["loc"]), "type": e["type"]} for e in error.errors()]}}}, status_code=422)
        except HTTPException as error:
            return JSONResponse({"error": {"code": "INVALID_CONFIGURATION", "message": str(error.detail)}}, status_code=error.status_code)
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "请求格式不正确"}}, status_code=422)
        except Exception:
            preview = operation == "voice-preview"
            return JSONResponse({"error": {"code": "PREVIEW_FAILED" if preview else "REMOTE_OUTCOME_UNKNOWN", "message": "试听未完成，请检查语音服务配置。不会自动重试。" if preview else "操作结果尚未确定，请先核实原操作编号和当前版本。"}}, status_code=502)

    @app.post("/api/prelive/v2/human/room-settings")
    async def room_settings(request: Request):
        return await handle(request, RoomSettingsInput, "room-settings")

    @app.post("/api/prelive/v2/human/product-archive")
    async def archive(request: Request):
        return await handle(request, ArchiveInput, "product-archive")

    @app.post("/api/prelive/v2/human/price-preview")
    async def price(request: Request):
        return await handle(request, PriceInput, "price-preview")

    @app.post("/api/prelive/v2/human/voice-preview")
    async def voice(request: Request):
        return await handle(request, VoicePreviewInput, "voice-preview")
