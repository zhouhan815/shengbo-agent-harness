from __future__ import annotations

import base64
import re
from typing import Any

import httpx
from pydantic import ValidationError
from shengbo_contracts.business import ProductAssemblyInvalidDetails, ProductConsumerContentInvalidDetails, ScriptPlanModuleBaseInvalidDetails
from shengbo_contracts.common import ArtifactRef, ContractError, MutationContext, RecordRef, WorkScope, canonical_json
from .observability import sanitize


def _selection_dependency_details(value):
    """Narrow public correction contract, never arbitrary HTTP exception text."""
    value = value if isinstance(value, dict) else {}

    def exact_ref(raw, kind):
        if not isinstance(raw, dict):
            return None
        try:
            ref = (ArtifactRef if "schema_version" in raw else RecordRef).model_validate(raw)
        except ValidationError:
            return None
        return ref.model_dump(mode="json") if ref.kind == kind else None

    result = {"current_selected_product_refs": [], "missing_product_ids": [], "requested_product_ids": [],
        "commit_state": "not_committed",
        "required_action": {"command": "update_preparation_products", "tool": "wf_product_materials",
                            "action": "select", "requires_explicit_selection": True}}
    prep = exact_ref(value.get("preparation_ref"), "preparation")
    if prep:
        result.update(preparation_ref=prep,
            refresh_query={"query": "read_preparation", "args": {"preparation_id": prep["id"]}})
    selected = value.get("current_selected_product_refs", [])
    for raw in selected[:1000] if isinstance(selected, list) else []:
        ref = exact_ref(raw, "product")
        if ref:
            result["current_selected_product_refs"].append(ref)
    for key in ("missing_product_ids", "requested_product_ids"):
        identities = value.get(key, [])
        result[key] = [identity for identity in identities[:1000]
            if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", identity)] if isinstance(identities, list) else []
    return sanitize(result)


class RemoteOutcomeUnknown(ContractError):
    def __init__(self, operation_id, *, original_operation_id=None, preview_state=None):
        super().__init__("REMOTE_OUTCOME_UNKNOWN", "业务请求结果尚未确定，需按原操作编号核实", status=503,
                         details={"operation_id": operation_id,
                             **({"original_operation_id": original_operation_id} if original_operation_id else {}),
                             **({"preview_state": preview_state} if preview_state else {})})


class ShengboClient:
    def __init__(self, base_url, agent_token, controller_token, *, transport=None):
        self._agent_token, self._controller_token = agent_token, controller_token
        self.http = httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=30, trust_env=False, transport=transport)

    def _headers(self, scope=None, run_id=None, controller=False):
        result = {"X-Shengbo-Service-Token": self._controller_token if controller else self._agent_token}
        if run_id:
            result["X-Shengbo-Run-Id"] = run_id
        if scope:
            value = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope
            result["X-Shengbo-Scope"] = base64.urlsafe_b64encode(canonical_json(value).encode()).decode()
        return result

    @staticmethod
    def _result(response):
        try:
            value = response.json()
        except ValueError:
            raise ContractError("VOICE_RESPONSE_INVALID", "声播返回了无法解析的响应", status=502) from None
        if response.is_error:
            if not isinstance(value, dict):
                raise ContractError("VOICE_RESPONSE_INVALID", "声播返回了不符合合同的拒绝响应", status=502)
            error = value.get("error", value.get("detail", {}))
            if not isinstance(error, dict):
                error = {"message": "声播业务请求被拒绝"}
            if error.get("code") == "PRODUCT_CONSUMER_CONTENT_INVALID":
                if response.status_code != 422:
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播消费者商品内容拒绝状态与合同不符",
                        status=response.status_code if response.status_code in {401, 403} or response.status_code >= 500 else 502) from None
                try:
                    details = ProductConsumerContentInvalidDetails.model_validate(error.get("details"), strict=True)
                except ValidationError:
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播消费者商品内容拒绝格式与合同不符", status=502) from None
                raise ContractError("PRODUCT_CONSUMER_CONTENT_INVALID", "商品含不应进入消费者商品库的内容，本次保存未提交",
                    status=422, details=details.model_dump(mode="json")) from None
            if error.get("code") == "PRODUCT_ASSEMBLY_INVALID":
                if response.status_code != 422:
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播商品校验拒绝状态与合同不符",
                        status=response.status_code if response.status_code in {401, 403} or response.status_code >= 500 else 502) from None
                try:
                    details = ProductAssemblyInvalidDetails.model_validate(error.get("details"), strict=True)
                except ValidationError:
                    # No arbitrary response details, Pydantic input or remote
                    # exception message may enter model context or logs.
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播商品校验拒绝内容与合同不符", status=502) from None
                raise ContractError("PRODUCT_ASSEMBLY_INVALID", "商品完整字段校验未通过，本次保存未提交",
                    status=422, details=details.model_dump(mode="json")) from None
            if error.get("code") == "BASE_MODULE_TARGET_MISMATCH":
                if response.status_code == 409:
                    # An existing complete baseline with a mismatched module is
                    # a definite legacy rejection, not the typed missing-base
                    # correction contract and not an unknown remote outcome.
                    raise ContractError("BASE_MODULE_TARGET_MISMATCH", "模块与完整基稿目标不匹配，本次计划被拒绝",
                        status=409) from None
                if response.status_code != 422:
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播模块基稿拒绝状态与合同不符",
                        status=response.status_code if response.status_code in {401, 403} or response.status_code >= 500 else 502) from None
                try:
                    details = ScriptPlanModuleBaseInvalidDetails.model_validate(error.get("details"), strict=True)
                except ValidationError:
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播模块基稿拒绝内容与合同不符", status=502) from None
                raise ContractError("BASE_MODULE_TARGET_MISMATCH", "新计划显式复用模块缺少同目标完整基稿，本次计划未提交",
                    status=422, details=details.model_dump(mode="json")) from None
            if error.get("code") == "PREPARATION_PRODUCTS_NOT_SELECTED":
                if response.status_code != 409:
                    # Never turn a contradictory 5xx/permission response into
                    # a correctable dependency merely because its label says so.
                    raise ContractError("VOICE_RESPONSE_INVALID", "声播拒绝状态与依赖校验合同不符",
                                        status=response.status_code if response.status_code in {401, 403} or response.status_code >= 500 else 502)
                raise ContractError("PREPARATION_PRODUCTS_NOT_SELECTED", "本场活动引用了尚未选入本场的商品，请核对并明确选入后再保存",
                                    status=409, details=_selection_dependency_details(error.get("details")))
            raise ContractError(error.get("code", "VOICE_REJECTED"), error.get("message", "声播业务请求被拒绝"),
                                status=response.status_code, details=error.get("details", {}))
        return value

    async def query(self, key: str, args=None, *, scope=None, run_id=None, controller=False):
        try:
            response = await self.http.post('/api/prelive/v2/queries', json={"query": key, "args": args or {}},
                                            headers=self._headers(scope, run_id, controller))
        except httpx.HTTPError:
            raise ContractError("VOICE_UNAVAILABLE", "声播业务服务暂不可用", status=503, retryable=True) from None
        return self._result(response)

    async def command(self, key: str, args: dict[str, Any], mutation: MutationContext | dict,
                      *, scope=None, run_id=None, controller=False):
        mutation = mutation.model_dump(mode="json") if hasattr(mutation, "model_dump") else mutation
        try:
            response = await self.http.post('/api/prelive/v2/commands',
                json={"contract_version": "PRELIVE-2.3", "command": key, "args": args, "mutation": mutation},
                headers=self._headers(scope, run_id, controller))
        except httpx.HTTPError:
            raise RemoteOutcomeUnknown(mutation["operation_id"]) from None
        try:
            return self._result(response)
        except ContractError as error:
            if key == "preview_script_audio" and error.status == 409 and error.code in {"PREVIEW_OUTCOME_UNKNOWN", "PREVIEW_IN_PROGRESS"}:
                original = error.details.get("original_operation_id") or error.details.get("operation_id")
                original = original if isinstance(original, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", original) else mutation["operation_id"]
                raise RemoteOutcomeUnknown(mutation["operation_id"], original_operation_id=original,
                    preview_state="running" if error.code == "PREVIEW_IN_PROGRESS" else "unknown") from None
            raise

    async def close(self):
        await self.http.aclose()

    async def read_identity_decision(self, ref, *, run_id=None):
        """Fixed controller-only readback; never an arbitrary URL or Agent tool."""
        value = ref.model_dump(mode="json") if hasattr(ref, "model_dump") else ref
        try:
            response = await self.http.post('/api/prelive/v2/controller/product-identity/read',
                                            json={"ref": value}, headers=self._headers(run_id=run_id, controller=True))
        except httpx.HTTPError:
            raise ContractError("VOICE_UNAVAILABLE", "商品身份确认暂时无法独立核实", status=503, retryable=True) from None
        return self._result(response)

