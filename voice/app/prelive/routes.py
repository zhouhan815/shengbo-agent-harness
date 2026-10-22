"""Explicit v2 wire boundary; identity comes from a trusted resolver."""
from __future__ import annotations
import inspect
from typing import Any, Literal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError, Field
from shengbo_contracts.business import DTO, QueryArgs
from shengbo_contracts.common import MutationContext, ArtifactRef
from .storage import BusinessError


class CommandEnvelope(DTO):
    contract_version: Literal["PRELIVE-2.3"] = "PRELIVE-2.3"
    command: str
    args: dict[str, Any]
    mutation: MutationContext


class QueryEnvelope(DTO):
    contract_version: Literal["PRELIVE-2.3"] = "PRELIVE-2.3"
    query: str
    args: dict[str, Any]


class IdentityDecisionInput(DTO):
    target_name: str
    candidate_refs: list[dict[str, Any]]


class ProductControlInput(DTO):
    product_id: str
    expected_product_revision: int = Field(ge=1)
    expected_control_revision: int | None = Field(default=None, ge=1)
    sale_blocked: bool = False
    withdrawn_revisions: list[int] = Field(default_factory=list)
    withdrawn_paths: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)


class IdentityDecisionReadInput(DTO):
    ref: ArtifactRef


def create_router(service, principal_resolver):
    router = APIRouter(prefix="/api/prelive/v2", tags=["prelive"])

    async def identity(request):
        resolved = principal_resolver(request)
        resolved = await resolved if inspect.isawaitable(resolved) else resolved
        if isinstance(resolved, tuple):
            return resolved
        if isinstance(resolved, dict) and "principal" in resolved:
            return resolved["principal"], resolved.get("scope")
        return resolved, None

    async def invoke(request, query=False):
        try:
            data = await request.json()
            principal, scope = await identity(request)
            if query:
                envelope = QueryEnvelope.model_validate(data)
                result = await service.query(envelope.query, envelope.args, principal=principal, scope=scope)
            else:
                envelope = CommandEnvelope.model_validate(data)
                result = await service.execute_command(envelope.command, envelope.args, envelope.mutation, principal, scope=scope)
            return JSONResponse(result)
        except BusinessError as e:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": e.payload()}, status_code=e.status)
        except ValidationError as e:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": {"code": "INVALID_REQUEST", "message": "请求字段或类型不符合合同", "details": {"fields": [{"path": list(err["loc"]), "type": err["type"]} for err in e.errors()]}, "retryable": False}}, status_code=422)
        except (ValueError, KeyError):
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": {"code": "INVALID_REQUEST", "message": "请求格式不符合合同", "details": {}, "retryable": False}}, status_code=400)

    @router.post("/commands")
    async def command(request: Request):
        return await invoke(request)

    @router.post("/queries")
    async def query(request: Request):
        return await invoke(request, query=True)

    @router.post("/human/product-identity")
    async def product_identity(request: Request):
        try:
            principal, _ = await identity(request)
            value = IdentityDecisionInput.model_validate(await request.json())
            from functools import partial
            import asyncio
            result = await asyncio.to_thread(partial(service.record_identity_decision, value.target_name, value.candidate_refs, principal=principal))
            return JSONResponse(result)
        except BusinessError as e:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": e.payload()}, status_code=e.status)
        except ValidationError:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "商品身份确认格式错误"}}, status_code=422)

    @router.post("/human/product-control")
    async def product_control(request: Request):
        try:
            principal, _ = await identity(request)
            value = ProductControlInput.model_validate(await request.json())
            from functools import partial
            import asyncio
            result = await asyncio.to_thread(partial(service.record_product_control, value.model_dump(), principal=principal))
            return JSONResponse(result)
        except BusinessError as e:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": e.payload()}, status_code=e.status)
        except ValidationError:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "商品控制信息格式错误"}}, status_code=422)

    @router.post("/controller/product-identity/read")
    async def read_product_identity(request: Request):
        try:
            principal, scope = await identity(request)
            value = IdentityDecisionReadInput.model_validate(await request.json())
            from functools import partial
            import asyncio
            result = await asyncio.to_thread(partial(service.read_identity_decision, value.ref, principal=principal, scope=scope))
            return JSONResponse(result)
        except BusinessError as error:
            return JSONResponse({"schema_version": "PRELIVE-2.3", "error": error.payload()}, status_code=error.status)
        except ValidationError:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "商品身份回执引用格式错误"}}, status_code=422)

    return router
