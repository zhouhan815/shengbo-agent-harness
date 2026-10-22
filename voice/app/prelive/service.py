"""Voice-owned, versioned pre-live transactions and persistent generation jobs.

No service constructor starts playback, calls a provider, or reads credentials.
Adapters perform remote work outside SQLite transactions; every result is fenced
again before it can become a selected preparation output.
"""
from __future__ import annotations
import asyncio
import base64
from copy import deepcopy
import hashlib
import inspect
import os
from pathlib import Path
import re
from time import perf_counter_ns
import wave
from typing import Any

from pydantic import Field, ValidationError
from shengbo_contracts.common import Principal, MutationContext, WorkScope
from shengbo_contracts.business import COMMAND_ARGS, QUERY_NAMES, DTO, QueryArgs, MODULE_KEYS, ACTIVE_MODULE_KEYS, ProductAssemblyInvalidDetails, PublicProviderResponseView, ScriptPlanModuleBaseInvalidDetails, VoiceSelection, parse_args, parse_query_args
from .. import commerce_config as commerce
from ..consumer_catalog import assert_consumer_product, policy_metadata as consumer_catalog_policy
from ..platform_rules import platform_policy_catalog
from ..prosody import sentence_style
from ..speech import resolved_style
from .storage import BusinessError, Store, canonical, digest, now, uid
from .playback_compiler import reorder, compile_items, playback_statistics


class Product(DTO, commerce.ProductFields):
    name: str = Field(min_length=1, max_length=60)
    currency: str = Field(default="CNY", pattern="^(CNY|TWD|USD|AUD)$")
    price: float = Field(ge=0, le=10000000)
    promo_price: float | None = Field(default=None, ge=0, le=10000000)
    selling_points: str = Field(default="", max_length=15000)
    promotion: str = Field(default="", max_length=2000)
    knowledge: str = Field(default="", max_length=15000)
    knowledge_documents: list[commerce.Document] = Field(default_factory=list, max_length=20)
    image: str = Field(default="", max_length=4500000)


class UnavailableAdapter:
    async def generate_module(self, context, module_key, product, previous):
        raise BusinessError("COPYWRITING_NOT_CONFIGURED", "话术模型未配置")

    async def render_audio(self, text, room_id, voice, *, style=None):
        raise BusinessError("TTS_NOT_CONFIGURED", "语音引擎未配置")

    async def start_publication(self, room_id, publication, compiled_entries):
        raise BusinessError("PLAYBACK_NOT_CONFIGURED", "播放器接入未配置")

    def voice_options(self):
        return {"providers": ["system", "minimax"], "preview_requires_approval": True}


HUMAN_ONLY = {"approve_text_for_audio", "approve_script_audio_preview", "approve_missing_audio_reissue", "revoke_text_approval", "adopt_snapshot", "start_prepared_room", "record_product_revision_choice"}
CONTROLLER_ONLY = {"issue_grant", "revoke_grant"}
SESSION_DRAFT_FIELDS = frozenset({"theme", "objective", "scheduled_at", "timezone", "commerce", "language",
    "tts_selection", "requested_variant_counts", "loop_mode", "inheritance_decisions", "unknowns", "campaign_ends_at"})
ARTIFACT_KINDS = {"analysis", "draft", "context", "script_plan", "module", "script_revision", "playlist", "compiled_plan", "requirements", "approval", "audio_manifest", "publication", "recap", "asset_checklist", "blob", "revision_choice", "adoption"}


def wire(value):
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else deepcopy(value)


class PreliveService:
    def __init__(self, runtime_path, adapter=None, *, minimax_audio_concurrency=None):
        configured = (os.environ.get("PRELIVE_MINIMAX_AUDIO_CONCURRENCY", "3")
                      if minimax_audio_concurrency is None else minimax_audio_concurrency)
        if isinstance(configured, bool) or str(configured).strip() not in {"1", "2", "3"}:
            raise ValueError("PRELIVE_MINIMAX_AUDIO_CONCURRENCY must be an integer from 1 to 3")
        self.minimax_audio_concurrency = int(configured)
        self.store = Store(runtime_path)
        self.runtime = Path(runtime_path)
        self.adapter = adapter or UnavailableAdapter()
        self._worker_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._background: asyncio.Task | None = None
        self.playback_owner_id: str | None = None
        self.command_handlers = {k: getattr(self, "_cmd_" + k) for k in COMMAND_ARGS}
        self.query_handlers = {k: getattr(self, "_q_" + k) for k in QUERY_NAMES}

    def schema(self):
        schema = {"product": Product.model_json_schema(), "commerce": commerce.RoomCommerce.model_json_schema(), "module_keys": list(ACTIVE_MODULE_KEYS)}
        descriptions = {
            "theme": "本场主题。", "objective": "本场目标。",
            "scheduled_at": "场次时间。建议ISO 8601开始时间字符串；需要同时保存起止时使用{start: ISO时间, end: ISO时间}。后端原样保留，时区单列。不要填写顶层starts_at/ends_at。",
            "timezone": "明确场次时区，例如Asia/Shanghai。", "commerce": "本场优惠配置，严格遵循本响应schema.commerce。",
            "language": "口播语言，例如zh-CN。", "tts_selection": "真实音色选择，生成语音仍须另行人工整包批准。",
            "requested_variant_counts": "建议使用商品ID到版本数量的映射。", "loop_mode": "保存后固定为infinite，直到人工停止。",
            "inheritance_decisions": "历史配置采用或拒绝的决定与依据。", "unknowns": "未确认项，不能作为已知承诺口播。",
            "campaign_ends_at": "活动优惠截止时间，使用含年份和时区的ISO 8601字符串；与场次结束分开保存。"}
        draft_properties = {key: {"description": descriptions[key]} for key in sorted(SESSION_DRAFT_FIELDS)}
        draft_properties["tts_selection"].update(VoiceSelection.model_json_schema())
        draft_properties["loop_mode"]["const"] = "infinite"
        # Additive query metadata does not invalidate an existing product
        # assembly's schema_hash. The accepted field set is shared with writes.
        return {"schema": schema, "schema_hash": digest(schema), "contract_version": "PRELIVE-2.3",
            "consumer_catalog_policy": consumer_catalog_policy(),
            "platform_policies": platform_policy_catalog(),
            "session_draft_schema": {"type": "object", "additionalProperties": False, "properties": draft_properties,
                "allowed_fields": sorted(SESSION_DRAFT_FIELDS),
                "backend_validation": "顶层字段严格；commerce与tts_selection按对应schema验证；其余值原样保存。"}}

    def _assert_scope(self, c, principal, scope, preparation_id=None):
        if principal.account_id != "local" or (scope and scope.account_id != principal.account_id):
            raise BusinessError("FORBIDDEN", status=403)
        if scope and scope.kind == "preparation":
            if preparation_id is not None and scope.preparation_id != preparation_id:
                raise BusinessError("SCOPE_CONFLICT", "对象不属于任务范围", status=403)
        elif scope and preparation_id is not None:
            raise BusinessError("SCOPE_CONFLICT", "商品库任务不能修改直播准备", status=403)

    def _grant(self, c, command, mutation, principal, scope, args=None):
        if command in HUMAN_ONLY and principal.kind != "human":
            raise BusinessError("HUMAN_REQUIRED", "此动作必须由用户确认", status=403)
        if command in CONTROLLER_ONLY and principal.kind != "controller":
            raise BusinessError("CONTROLLER_REQUIRED", status=403)
        if principal.kind != "agent":
            return
        if not mutation.grant_id or not principal.run_id:
            raise BusinessError("GRANT_REQUIRED", status=403)
        grant = c.execute("SELECT * FROM prelive_grants WHERE grant_id=?", (mutation.grant_id,)).fetchone()
        if not grant or grant["run_id"] != principal.run_id or grant["status"] != "active" or grant["write_generation"] != mutation.expected_write_generation:
            raise BusinessError("GRANT_REVOKED", "任务写入权限已失效", status=403)
        if c.execute("SELECT 1 FROM prelive_revocations WHERE run_id=?", (principal.run_id,)).fetchone():
            raise BusinessError("GRANT_REVOKED", status=403)
        import json
        grant_scope = json.loads(grant["scope_json"])
        if scope is None or grant_scope != wire(scope):
            raise BusinessError("SCOPE_CONFLICT", "授权范围不匹配", status=403)
        if command not in json.loads(grant["capabilities_json"]):
            raise BusinessError("CAPABILITY_DENIED", details={"command": command}, status=403)
        constraints = json.loads(grant["constraints_json"])
        if args is not None and constraints:
            from .target_authorization import guard_targets
            guard_targets(self, c, command, args, constraints, run_id=principal.run_id)
            allowed_paths = {r["field_path"] for r in constraints if r.get("field_path")}
            if command == "save_product" and allowed_paths:
                requested = {p.path for p in args.patches if p.op != "preserve"}
                if requested - allowed_paths:
                    raise BusinessError("SCOPE_CONFLICT", "商品字段超出授权范围", status=403)
            allowed_modules = {r["module_key"] for r in constraints if r.get("module_key")}
            if allowed_modules and command == "patch_script" and args.module_key not in allowed_modules:
                raise BusinessError("SCOPE_CONFLICT", "话术模块超出授权范围", status=403)
            if command == "preview_script_audio":
                preview = self._artifact(c, args.preview_approval_ref, "script_audio_preview_approval")["body"]["binding"]
                if allowed_modules and set(preview["module_keys"]) - allowed_modules:
                    raise BusinessError("SCOPE_CONFLICT", "试听模块超出任务授权范围", status=403)
            from .target_authorization import advance_sentence_constraints
            sentence_constraints = [row for row in advance_sentence_constraints(c, constraints, principal.run_id)
                                    if row.get("sentence_ids")]
            if sentence_constraints and command in {"save_script_plan", "submit_script_change"}:
                raise BusinessError("SCOPE_CONFLICT", "句子范围授权不能执行整模块生成", status=403)
            if sentence_constraints and command == "patch_script":
                if not any(row["sentence_ids"] == args.sentence_ids and row["module_key"] == args.module_key
                        and all(row["script_ref"].get(key) == getattr(args.script_ref, key)
                                for key in ("id", "revision", "content_hash"))
                        and row["selected_text_hash"] == args.selected_text_hash for row in sentence_constraints):
                    raise BusinessError("SCOPE_CONFLICT", "句子改动超出精确批准范围", status=403)
            if allowed_modules and command == "save_script_plan":
                requested = set()
                for item in args.items:
                    base = self._artifact(c, item.base_script_ref, "script_revision", args.preparation_id)["body"] if item.base_script_ref else None
                    for action in item.modules:
                        if action.action in {"generate", "patch"} or (
                            action.action == "disable" and (base is None or base.get("module_refs", {}).get(action.key))
                        ):
                            requested.add(action.key)
                if requested - allowed_modules:
                    raise BusinessError("SCOPE_CONFLICT", "计划含未授权改动模块", status=403)

    async def execute_command(self, command, args, mutation, principal, *, scope=None):
        if command not in self.command_handlers:
            raise BusinessError("UNKNOWN_COMMAND", status=400)
        parsed = parse_args(command, args) if isinstance(args, dict) else args
        mutation = MutationContext.model_validate(mutation) if isinstance(mutation, dict) else mutation
        principal = Principal.model_validate(principal) if isinstance(principal, dict) else principal
        scope = WorkScope.model_validate(scope) if isinstance(scope, dict) else scope
        if command == "start_prepared_room":
            return await self._start(parsed, mutation, principal, scope)
        if command == "preview_script_audio":
            from .script_audio_preview import preview
            return await preview(self, parsed, mutation, principal, scope)
        if command == "import_blob":
            # Byte decoding, hashing, and atomic disk I/O precede the short
            # metadata transaction. Unreferenced blobs are safe orphan files.
            with self.store.transaction() as c:
                self._assert_scope(c, principal, scope)
                self._grant(c, command, mutation, principal, scope, parsed)
            await asyncio.to_thread(self._prepare_blob_file, parsed)
        receipt = await asyncio.to_thread(self._mutate, command, parsed, mutation, principal, scope)
        if command in {"submit_script_change", "prepare_required_audio", "preview_audio", "retry_failed_items", "approve_missing_audio_reissue"}:
            self.wake_worker()
        return receipt

    def _mutate(self, command, args, mutation, principal, scope):
        import json
        from shengbo_contracts.business import command_wire_candidates
        request_hash = digest({"command": command, "args": wire(args), "scope": wire(scope)})
        hashes = {digest({"command": command, "args": candidate, "scope": wire(scope)})
                  for candidate in command_wire_candidates(command, wire(args))}
        if mutation.request_hash and mutation.request_hash not in hashes:
            raise BusinessError("REQUEST_HASH_MISMATCH")
        with self.store.transaction(write=True) as c:
            prior = c.execute("SELECT * FROM prelive_operations WHERE principal_id=? AND operation_id=?", (principal.actor_id, mutation.operation_id)).fetchone()
            if prior:
                if prior["request_hash"] not in hashes or prior["command"] != command:
                    raise BusinessError("IDEMPOTENCY_KEY_REUSED")
                return json.loads(prior["response_json"])
            from .voice_preview import read_preview_receipt
            if read_preview_receipt(c, principal.actor_id, mutation.operation_id) is not None:
                raise BusinessError("IDEMPOTENCY_KEY_REUSED", "操作编号已用于试听，请使用本操作的独立编号")
            self._assert_scope(c, principal, scope, getattr(args, "preparation_id", None))
            self._grant(c, command, mutation, principal, scope, args)
            for ref in mutation.expected_versions:
                current = self.store.get(c, ref.kind, ref.id)
                if any(current["ref"].get(k) != getattr(ref, k) for k in ("kind", "id", "revision", "content_hash")):
                    raise BusinessError("REVISION_CONFLICT")
            if getattr(args, "preparation_id", None) and mutation.expected_generation_epoch is not None:
                prep = self.store.get(c, "preparation", args.preparation_id)
                if prep["body"]["generation_epoch"] != mutation.expected_generation_epoch:
                    raise BusinessError("GENERATION_EPOCH_CHANGED")
            data = self.command_handlers[command](c, args, mutation, principal, scope)
            cursor = self.store.event(c, getattr(args, "preparation_id", None) or "catalog", "business." + command, {"command": command, "operation_id": mutation.operation_id, "run_id": principal.run_id, "result": data})
            receipt = {"schema_version": "PRELIVE-2.3", "command": command, "operation_id": mutation.operation_id, "status": "accepted" if command in {"submit_script_change", "prepare_required_audio", "preview_audio", "retry_failed_items", "approve_missing_audio_reissue"} else "committed", "data": data, "changed_paths": data.get("changed_paths", []), "invalidated_refs": [], "event_cursor": cursor}
            c.execute("INSERT INTO prelive_operations VALUES(?,?,?,?,?,?)", (principal.actor_id, mutation.operation_id, command, request_hash, canonical(receipt), now()))
            return receipt

    async def query(self, key, args=None, *, principal=None, scope=None):
        if key not in self.query_handlers:
            raise BusinessError("UNKNOWN_QUERY", status=400)
        args = parse_query_args(key, args or {}) if isinstance(args, (dict, type(None))) else args
        principal = Principal.model_validate(principal or {"kind": "human"}) if isinstance(principal, (dict, type(None))) else principal
        scope = WorkScope.model_validate(scope) if isinstance(scope, dict) else scope
        return await asyncio.to_thread(self._query, key, args, principal, scope)

    def _query(self, key, args, principal, scope):
        with self.store.transaction() as c:
            self._assert_scope(c, principal, scope, args.preparation_id)
            if args.ref and scope:
                target = self.store.exact(c, args.ref)
                target_preparation = target["body"].get("preparation_id")
                if target_preparation and (scope.kind != "preparation" or scope.preparation_id != target_preparation):
                    raise BusinessError("ARTIFACT_SCOPE_CONFLICT", status=403)
            return self.query_handlers[key](c, args, principal, scope)

    def _prep(self, c, identity, expected=None):
        if not identity:
            raise BusinessError("PREPARATION_REQUIRED")
        result = self.store.get(c, "preparation", identity)
        if expected is not None and result["ref"]["revision"] != expected:
            raise BusinessError("REVISION_CONFLICT", details={"current_revision": result["ref"]["revision"]})
        return result

    def _save_prep(self, c, prep, body, *, invalidate=True):
        body = deepcopy(body)
        if invalidate:
            body["generation_epoch"] += 1
            body["approval_ref"] = None
            body["candidate_publication_ref"] = None
            body["status"] = "draft"
        ref = self.store.put(c, "preparation", prep["ref"]["id"], body, expected=prep["ref"]["revision"])
        return {"preparation_ref": ref, "generation_epoch": body["generation_epoch"]}

    def _artifact(self, c, ref, kind=None, preparation_id=None):
        result = self.store.exact(c, ref)
        if kind and result["ref"]["kind"] != kind:
            raise BusinessError("WRONG_ARTIFACT_KIND")
        if preparation_id and result["body"].get("preparation_id") not in {None, preparation_id}:
            raise BusinessError("ARTIFACT_SCOPE_CONFLICT", status=403)
        return result

    def _cmd_issue_grant(self, c, a, m, p, s):
        import json
        if c.execute("SELECT 1 FROM prelive_revocations WHERE run_id=?", (a.run_id,)).fetchone():
            raise BusinessError("RUN_REVOKED", status=403)
        if set(a.capabilities) - (set(COMMAND_ARGS) - HUMAN_ONLY - CONTROLLER_ONLY):
            raise BusinessError("INVALID_GRANT_CAPABILITY", status=403)
        valid_scope = wire(WorkScope.model_validate(a.scope))
        prior = c.execute("SELECT * FROM prelive_grants WHERE run_id=?", (a.run_id,)).fetchone()
        if prior:
            if json.loads(prior["scope_json"]) != valid_scope or json.loads(prior["capabilities_json"]) != a.capabilities or json.loads(prior["constraints_json"]) != a.target_constraints:
                raise BusinessError("GRANT_CONFLICT")
            return {"grant_id": prior["grant_id"], "run_id": a.run_id, "write_generation": prior["write_generation"], "status": prior["status"], "expires_at": None}
        gid = uid("grant")
        c.execute("INSERT INTO prelive_grants VALUES(?,?,?,?,?,?,?,?)", (gid, a.run_id, 1, "active", canonical(valid_scope), canonical(a.capabilities), canonical(a.target_constraints), now()))
        return {"grant_id": gid, "run_id": a.run_id, "write_generation": 1, "status": "active", "expires_at": None}

    def _cmd_revoke_grant(self, c, a, m, p, s):
        c.execute("INSERT OR IGNORE INTO prelive_revocations VALUES(?,?,?,?)", (a.run_id, m.operation_id, a.reason, now()))
        c.execute("UPDATE prelive_grants SET status='revoked',write_generation=write_generation+1 WHERE run_id=? AND status='active'", (a.run_id,))
        for job in self.store.all(c, "prelive_job"):
            if job["body"].get("run_id") == a.run_id and job["body"]["status"] in {"queued", "running", "partially_failed"}:
                body = job["body"]
                body.update(cancel_requested=True, status="cancelled")
                self.store.put(c, "prelive_job", job["ref"]["id"], body, expected=job["ref"]["revision"])
        return {"run_id": a.run_id, "revoked": True, "prior_commits_preserved": True}

    def _cmd_create_room(self, c, a, m, p, s):
        if set(a.initial_config) - {"tts_provider", "tts_voice", "tts_model", "loop", "commerce", "version_count"}:
            raise BusinessError("UNKNOWN_ROOM_CONFIG_FIELD")
        identity = uid("room")
        config = {"id": identity, "name": a.name, "product_ids": [], "script_order": [], "selected_script_ids": [], "plan_mode": "all", "loop": True, "tts_provider": "system", "tts_voice": "Huihui", "tts_model": "speech-2.8-turbo", "commerce": commerce.RoomCommerce().model_dump(), **a.initial_config}
        if config.get("product_ids") or config.get("script_order"):
            raise BusinessError("INITIAL_ROOM_REFERENCES_NOT_ALLOWED")
        config["id"] = identity
        config["commerce"] = commerce.parse_room(config["commerce"])
        return {"room_ref": self.store.put(c, "config", identity, config, create=True)}

    def _cmd_create_preparation(self, c, a, m, p, s):
        room = self.store.get(c, "config", a.room_id)
        if room["ref"]["revision"] != a.room_revision:
            raise BusinessError("REVISION_CONFLICT")
        identity = uid("preparation")
        if s and s.kind == "preparation" and s.preparation_id != identity:
            # A controller can create a preparation before issuing the new run grant.
            if p.kind == "agent":
                raise BusinessError("PREPARATION_CREATE_REQUIRES_CONTROLLER", status=403)
        for ref in a.source_refs:
            self._artifact(c, ref)
        body = {"id": identity, "room_id": a.room_id, "base_room_revision": a.room_revision, "goal": a.goal, "generation_epoch": 1, "status": "draft", "selected_product_refs": [], "source_refs": [wire(r) for r in a.source_refs], "analysis_refs": [], "recap_refs": [], "script_refs": [], "draft_ref": None, "bundle_ref": None, "plan_ref": None, "playlist_ref": None, "compiled_plan_ref": None, "requirements_ref": None, "approval_ref": None, "audio_manifest_ref": None, "candidate_publication_ref": None, "asset_checklist_ref": None}
        return {"preparation_ref": self.store.put(c, "preparation", identity, body, create=True), "generation_epoch": 1}

    def _validate_product_assembly(self, value, *, validation_stage, context, product_ref, schema_hash):
        """Expose only correctable field coordinates, never rejected values.

        Both call sites precede any product, sequence, audit or event write.
        Only Pydantic product-shape validation is translated here: scope,
        permissions, CAS and external outcome fences retain their own errors.
        """
        try:
            return Product.model_validate(value, context=context).model_dump()
        except ValidationError as error:
            schema = Product.model_json_schema()

            def public_path(location):
                node, result = schema, []
                for part in location:
                    if "$ref" in node:
                        node = schema.get("$defs", {}).get(node["$ref"].removeprefix("#/$defs/"), {})
                    if type(part) is int and part >= 0:
                        result.append(part)
                        node = node.get("items", {})
                    elif isinstance(part, str) and part in node.get("properties", {}):
                        result.append(part)
                        node = node["properties"][part]
                    else:
                        # Extra keys are user input too. Report a stable
                        # coordinate without copying a potentially private key.
                        result.append("unknown_field")
                        node = {}
                return result

            details = ProductAssemblyInvalidDetails.model_validate({
                "validation_stage": validation_stage,
                "commit_state": "not_committed",
                "issues": [{"path": public_path(issue["loc"]), "type": issue["type"]}
                    for issue in error.errors(include_url=False, include_input=False, include_context=False)],
                "product_ref": product_ref,
                "schema_hash": schema_hash,
                "required_fields": schema.get("required", []),
            }, strict=True).model_dump(mode="json")
            raise BusinessError("PRODUCT_ASSEMBLY_INVALID", "商品完整字段不符合当前格式，请按字段位置补齐或修正后再保存",
                status=422, details=details) from None

    def _cmd_save_product(self, c, a, m, p, s):
        schema_hash = self.schema()["schema_hash"]
        if a.schema_hash != schema_hash:
            raise BusinessError("SCHEMA_CHANGED", details={"schema_hash": schema_hash})
        old = self.store.get(c, "product", a.product_id) if a.product_id else None
        if old and old["ref"]["revision"] != a.expected_revision:
            raise BusinessError("REVISION_CONFLICT")
        names = set(Product.model_fields)
        previous = {k: deepcopy(v) for k, v in old["body"].items() if k in names} if old else {}
        value = deepcopy(previous)
        paths, changed = set(), []
        for patch in a.patches:
            if not re.fullmatch(r"/[A-Za-z_]+", patch.path) or patch.path[1:] not in names or patch.path in paths:
                raise BusinessError("INVALID_FIELD_PATH", details={"path": patch.path})
            paths.add(patch.path)
            key = patch.path[1:]
            if patch.op == "preserve":
                if patch.value is not None:
                    raise BusinessError("PRESERVE_HAS_VALUE")
                continue
            if patch.op == "clear":
                if key in {"name", "price", "currency"} or patch.value is not None:
                    raise BusinessError("FIELD_CANNOT_CLEAR")
                default = Product.model_fields[key].get_default(call_default_factory=True)
                value[key] = default
            else:
                value[key] = patch.value
            changed.append(patch.path)
        if not old:
            value = {**a.fields, **value}
        canonical_points = "/core_selling_points" in changed
        prose_points = "/selling_points" in changed
        if canonical_points:
            derived = "\n".join(value.get("core_selling_points", []))
            if prose_points and value.get("selling_points") != derived:
                raise BusinessError("CANONICAL_FIELD_CONFLICT")
            value["selling_points"] = derived
        elif prose_points:
            value["core_selling_points"] = []
        # Do not migrate legacy inactive values as a side effect of a name-only
        # or empty patch. Only a newly disabled, explicitly patched group can
        # authorize clearing its dependent fields.
        validation_context = {"preserve_hidden_optional_values": old is not None}
        product_ref = old["ref"] if old else None
        product = self._validate_product_assembly(value, validation_stage="assembled",
            context=validation_context, product_ref=product_ref, schema_hash=schema_hash)
        supplied = self._validate_product_assembly(a.fields, validation_stage="supplied",
            context=validation_context, product_ref=product_ref, schema_hash=schema_hash)
        newly_hidden = (set(product["hidden_optional_fields"]) - set(previous.get("hidden_optional_fields", []))
                        if old and "/hidden_optional_fields" in changed else set())
        derived_clear_fields = {key for group in newly_hidden for key in commerce.OPTIONAL_PRODUCT_FIELDS[group]}
        if derived_clear_fields:
            active = commerce.active_product_fields(product, consumer_safe=False)
            active_supplied = commerce.active_product_fields(supplied, consumer_safe=False)
            for key in derived_clear_fields:
                product[key], supplied[key] = active[key], active_supplied[key]
        if product["promo_price"] is not None and product["promo_price"] > product["price"]:
            raise BusinessError("PROMO_PRICE_EXCEEDS_DAILY")
        if product != supplied:
            raise BusinessError("ASSEMBLY_PATCH_MISMATCH", "完整商品与字段操作不一致")
        # Same shared policy as the direct/manual product save. The exact
        # assembled payload is either accepted or rejected, never rewritten.
        assert_consumer_product({**(old["body"] if old else {}), **product})
        if old:
            allowed = {x[1:] for x in changed}
            if canonical_points or prose_points:
                allowed.update({"core_selling_points", "selling_points"})
            allowed.update(derived_clear_fields)
            for key in derived_clear_fields:
                if previous.get(key) != product[key] and "/" + key not in changed:
                    changed.append("/" + key)
            for key in names - allowed:
                if key in previous and product[key] != previous[key]:
                    raise BusinessError("SCOPE_CONFLICT", "未授权字段不能被格式化或覆盖", details={"path": "/" + key})
        if a.field_dispositions and {d.path for d in a.field_dispositions} != {"/" + k for k in names}:
            required_paths = {"/" + k for k in names}
            provided_paths = {d.path for d in a.field_dispositions}
            raise BusinessError("INCOMPLETE_FIELD_DISPOSITIONS", details={
                "required_paths": sorted(required_paths),
                "missing_paths": sorted(required_paths - provided_paths),
                "unexpected_paths": sorted(provided_paths - required_paths),
                "commit_state": "not_committed",
                "hint": "自定义field_dispositions非空时必须完整覆盖商品字段；保留已有处置（包括not_applicable）并补齐缺项，不要清除其语义。无需自定义处置时可全部留空，由既有机械规则生成。",
            })
        for ref in a.source_manifest_refs:
            self._artifact(c, ref)
        if product["image"] and not re.match(r"^data:image/(png|jpeg|webp);base64,", product["image"]):
            raise BusinessError("INVALID_PRODUCT_IMAGE")
        if len({d["id"] for d in product["knowledge_documents"]}) != len(product["knowledge_documents"]):
            raise BusinessError("DUPLICATE_KNOWLEDGE_ID")
        identity = a.product_id or uid("product")
        if not old:
            similar = [r for r in self.store.all(c, "product") if not r["body"].get("archived") and r["body"].get("name") == product["name"]]
            if similar:
                proof = self._artifact(c, a.identity_decision_ref, "human_decision")["body"] if a.identity_decision_ref else None
                if not proof or proof.get("decision") != "create_distinct_product" or proof.get("target_name") != product["name"] or proof.get("candidate_refs") != [r["ref"] for r in similar]:
                    raise BusinessError("PRODUCT_IDENTITY_CONFIRMATION_REQUIRED", "已有同名商品，请确认新增还是更新", details={"candidate_refs": [r["ref"] for r in similar]})
            c.execute("INSERT OR IGNORE INTO prelive_sequences VALUES('product_display_number',1)")
            highest = max((r["body"].get("display_number", 0) for r in self.store.all(c, "product")), default=0)
            sequence = c.execute("SELECT next_value FROM prelive_sequences WHERE name='product_display_number'").fetchone()[0]
            number = max(sequence, highest + 1)
            c.execute("UPDATE prelive_sequences SET next_value=? WHERE name='product_display_number'", (number + 1,))
        else:
            number = old["body"].get("display_number", 1)
        sku = product["sku_id"] or identity
        if any(r["ref"]["id"] != identity and (r["body"].get("sku_id") or r["ref"]["id"]) == sku for r in self.store.all(c, "product")):
            raise BusinessError("SKU_CONFLICT")
        conflict = c.execute("SELECT product_id FROM prelive_skus WHERE sku_id=?", (sku,)).fetchone()
        if conflict and conflict[0] != identity:
            raise BusinessError("SKU_CONFLICT")
        c.execute("DELETE FROM prelive_skus WHERE product_id=?", (identity,))
        c.execute("INSERT INTO prelive_skus VALUES(?,?)", (sku, identity))
        body = {**(old["body"] if old else {}), **product, "id": identity, "sku_id": sku, "display_number": number, "archived": old["body"].get("archived", False) if old else False, "updated_at": now()}
        ref = self.store.put(c, "product", identity, body, expected=a.expected_revision, create=not old)
        dispositions = [wire(d) for d in a.field_dispositions]
        inactive = {key for group in product["hidden_optional_fields"] for key in commerce.OPTIONAL_PRODUCT_FIELDS[group]}
        patch_actions = {patch.path: patch.op for patch in a.patches}
        for disposition in dispositions:
            if disposition["path"][1:] in inactive:
                key = disposition["path"][1:]
                disposition.update(knowledge="not_applicable", reason="停用字段不作为商品事实；未授权的历史存储值保持原样")
                if key in derived_clear_fields or not old:
                    disposition.update(action="clear", reason="本次明确停用并清空依赖字段，不作为商品事实")
                else:
                    disposition["action"] = patch_actions.get(disposition["path"], "preserve")
        if not dispositions:
            dispositions = [{"path": "/" + key, "action": "clear" if key in derived_clear_fields or (not old and key in inactive) else patch_actions.get("/" + key, "preserve"), "knowledge": "not_applicable" if key in inactive else "unknown" if product[key] in (None, "", []) else "known", "source_refs": [wire(r) for r in a.source_manifest_refs], "reason": "停用字段不作为商品事实；仅本次明确停用才清空，其余历史值保持原样" if key in inactive else "服务依据明确字段操作生成的完整处理清单"} for key in sorted(names)]
        audit = self.store.artifact(c, "product_assembly", {"product_ref": ref, "field_dispositions": dispositions, "sources": [wire(r) for r in a.source_manifest_refs], "changed_paths": changed})
        change = c.execute("SELECT id FROM prelive_changes WHERE kind='product' AND object_id=? AND after_ref=? ORDER BY created_at DESC LIMIT 1", (identity, canonical(ref))).fetchone()
        return {"product_ref": ref, "product": body, "assembly_ref": audit, "change_id": change[0] if change else None, "changed_paths": changed, "scripts_automatically_changed": False, "created": old is None}

    def record_identity_decision(self, target_name, candidate_refs, *, principal):
        """Trusted human UI callback; deliberately absent from model tools."""
        principal = Principal.model_validate(principal) if isinstance(principal, dict) else principal
        if principal.kind != "human" or principal.account_id != "local":
            raise BusinessError("HUMAN_REQUIRED", status=403)
        with self.store.transaction(write=True) as c:
            actual = [r["ref"] for r in self.store.all(c, "product") if not r["body"].get("archived") and r["body"].get("name") == target_name]
            if actual != candidate_refs:
                raise BusinessError("IDENTITY_CANDIDATES_CHANGED")
            proof = {"decision": "create_distinct_product", "target_name": target_name, "candidate_refs": actual, "human_actor": principal.actor_id}
            ref = self.store.artifact(c, "human_decision", proof)
            self.store.event(c, "catalog", "human.identity_decided", {"decision_ref": ref})
            return {"identity_decision_ref": ref}

    def read_identity_decision(self, ref, *, principal, scope=None):
        principal = Principal.model_validate(principal) if isinstance(principal, dict) else principal
        scope = WorkScope.model_validate(scope) if isinstance(scope, dict) else scope
        if principal.kind != "controller" or principal.account_id != "local" or (scope and scope.account_id != "local"):
            raise BusinessError("CONTROLLER_REQUIRED", status=403)
        with self.store.transaction() as c:
            decision = self._artifact(c, ref, "human_decision")
            if decision["body"].get("decision") != "create_distinct_product" or not decision["body"].get("human_actor"):
                raise BusinessError("WRONG_DECISION_KIND")
            actual = [r["ref"] for r in self.store.all(c, "product") if not r["body"].get("archived") and r["body"].get("name") == decision["body"]["target_name"]]
            if actual != decision["body"]["candidate_refs"]:
                raise BusinessError("IDENTITY_CANDIDATES_CHANGED")
            return decision

    def legacy_script_projection(self, identity):
        """Read-only shape for the existing voice UI, never a second draft."""
        with self.store.transaction() as c:
            script = self.store.get(c, "script_revision", identity)
            body = script["body"]
            product = self.store.exact(c, body["product_ref"])["body"]
            sentences, blocks = [], []
            for key in MODULE_KEYS:
                ref = body["module_refs"].get(key)
                if not ref:
                    continue
                module = self._artifact(c, ref)["body"]
                block_id = ref["id"]
                blocks.append({"id": block_id, "ordinal": len(blocks) + 1, "stage": key})
                for sentence in module["sentences"]:
                    sentences.append({**sentence, "ordinal": len(sentences) + 1, "block_id": block_id, "block_ordinal": len(blocks), "stage": key, "fact_ids": []})
            from ..welcome import build_product_templates
            return {"id": identity, "product_id": body["product_id"], "product_snapshot": product, "room_id": self._prep(c, body["preparation_id"])["body"]["room_id"], "version_number": body["version_number"], "revision": body["script_revision"], "variant_id": body["variant_id"], "module_refs": body["module_refs"], "angle": "本场策划话术", "label": "本场策划话术", "source": "prelive-agent", "facts": [], "status": "draft", "prelive_managed": True, "sentences": sentences, "blocks": blocks, "created_at": body["created_at"], "issues": [], "welcome_templates": deepcopy(body.get("welcome_templates") or build_product_templates(product))}

    def _cmd_update_preparation_products(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        refs = [wire(r) for r in a.selected_product_refs]
        if len({r["id"] for r in refs}) != len(refs):
            raise BusinessError("DUPLICATE_PRODUCT_SELECTION")
        if p.kind == "agent":
            import json
            pinned = {ref["id"]: ref for ref in prep["body"]["selected_product_refs"]}
            requested = {ref["id"]: ref for ref in refs}
            if set(pinned) - set(requested):
                raise BusinessError("PRODUCT_SELECTION_REMOVAL_DENIED", "新增选品不能移除原已选商品", status=403)
            if any(requested[identity] != ref for identity, ref in pinned.items()):
                raise BusinessError("HUMAN_REVISION_CHOICE_REQUIRED", "已选商品版本变化需要人工选择保留或刷新", status=403)
            grant = c.execute("SELECT constraints_json FROM prelive_grants WHERE grant_id=?", (m.grant_id,)).fetchone()
            constraints = json.loads(grant["constraints_json"])
            allowed = {item.get("id") or item.get("product_id") or item.get("object_ref", {}).get("id") for item in constraints}
            # Only server-owned creation receipts from this exact run can extend
            # the frozen targets; a model-supplied ID is never creation evidence.
            for row in c.execute("SELECT payload FROM prelive_events WHERE event_type='business.save_product'"):
                event = json.loads(row["payload"])
                result = event.get("result", {})
                if event.get("run_id") == p.run_id and result.get("created") is True:
                    allowed.add(result["product_ref"]["id"])
            if set(requested) - set(pinned) - allowed:
                raise BusinessError("TARGET_DENIED", "新增选品不属于本次目标或本次新建商品", status=403)
        for ref in refs:
            product = self._artifact(c, ref, "product")["body"]
            if product.get("sale_status") == "unavailable" or product.get("archived"):
                raise BusinessError("PRODUCT_UNAVAILABLE")
        body = deepcopy(prep["body"])
        if body["selected_product_refs"] == refs:
            return {"preparation_ref": prep["ref"], "generation_epoch": body["generation_epoch"], "unchanged": True}
        body["selected_product_refs"] = refs
        for key in ("bundle_ref", "plan_ref", "playlist_ref", "compiled_plan_ref", "requirements_ref", "audio_manifest_ref"):
            body[key] = None
        return self._save_prep(c, prep, body)

    def _refresh(self, c, prep, old_ref, new_ref):
        old_ref, new_ref = wire(old_ref), wire(new_ref)
        if old_ref["id"] != new_ref["id"] or old_ref["kind"] != "product" or new_ref["kind"] != "product":
            raise BusinessError("PRODUCT_IDENTITY_MISMATCH")
        self.store.exact(c, old_ref)
        self.store.exact(c, new_ref)
        body = deepcopy(prep["body"])
        if old_ref not in body["selected_product_refs"]:
            raise BusinessError("PINNED_REFERENCE_CHANGED")
        body["selected_product_refs"] = [new_ref if r == old_ref else r for r in body["selected_product_refs"]]
        for key in ("bundle_ref", "plan_ref", "compiled_plan_ref", "requirements_ref", "audio_manifest_ref"):
            body[key] = None
        return self._save_prep(c, prep, body)

    def _cmd_refresh_product_reference(self, c, a, m, p, s):
        if p.kind == "agent":
            raise BusinessError("HUMAN_REVISION_CHOICE_REQUIRED", status=403)
        return self._refresh(c, self._prep(c, a.preparation_id, a.expected_revision), a.expected_old_product_ref, a.new_product_ref)

    def _differences(self, old, new):
        keys = set(old) | set(new)
        return [{"path": "/" + k, "old": old.get(k), "new": new.get(k)} for k in sorted(keys) if old.get(k) != new.get(k) and k not in {"updated_at"}]

    def _currentness(self, c, prep, *, choices_required=False):
        import json
        diffs, blockers, choices = [], [], []
        for pinned in prep["selected_product_refs"]:
            old = self.store.exact(c, pinned)
            head = self.store.get(c, "product", pinned["id"])
            control = head["body"]
            try:
                explicit_control = self.store.get(c, "product_control", pinned["id"])
            except BusinessError as e:
                if e.code != "NOT_FOUND":
                    raise
                explicit_control = None
            if control.get("archived") or control.get("sale_status") == "unavailable" or control.get("withdrawn") or pinned["revision"] in control.get("withdrawn_revisions", []):
                blockers.append({"code": "PRODUCT_UNAVAILABLE_OR_WITHDRAWN", "product_ref": pinned})
            if explicit_control and (explicit_control["body"]["sale_blocked"] or pinned["revision"] in explicit_control["body"]["withdrawn_revisions"] or explicit_control["body"]["withdrawn_paths"]):
                blockers.append({"code": "PRODUCT_EXPLICITLY_WITHDRAWN", "product_ref": pinned, "control_ref": explicit_control["ref"], "withdrawn_paths": explicit_control["body"]["withdrawn_paths"], "reason": explicit_control["body"]["reason"]})
            if pinned != head["ref"]:
                changes = self._differences(old["body"], head["body"])
                relevant = [d for d in changes if d["path"] not in {"/image", "/display_number", "/hidden_optional_fields"}]
                diff_hash = digest(changes)
                rows = c.execute("SELECT receipt_json FROM prelive_revision_choices WHERE preparation_id=? AND pinned_hash=? AND head_hash=? AND diff_hash=? AND choice='keep_pinned'", (prep["id"], pinned["content_hash"], head["ref"]["content_hash"], diff_hash)).fetchall()
                decision = json.loads(rows[-1][0]) if rows else None
                diffs.append({"pinned_ref": pinned, "observed_head_ref": head["ref"], "diff_hash": diff_hash, "differences": changes, "requires_choice": bool(relevant), "choice_receipt": decision})
                if decision:
                    choices.append(decision["revision_choice_ref"])
                elif relevant and choices_required:
                    blockers.append({"code": "PRODUCT_REVISION_CHOICE_REQUIRED", "product_id": pinned["id"], "diff_hash": diff_hash})
        if prep.get("draft_ref"):
            draft = self._artifact(c, prep["draft_ref"])["body"]["draft"]
            end = draft.get("campaign_ends_at")
            if end:
                from datetime import datetime, timezone
                try:
                    expiry = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                        blockers.append({"code": "CAMPAIGN_EXPIRED"})
                except ValueError:
                    blockers.append({"code": "INVALID_CAMPAIGN_EXPIRY"})
        return {"differences": diffs, "blockers": blockers, "revision_choice_refs": choices, "current": not blockers}

    def record_product_control(self, args, *, principal):
        """Trusted human control signal, separate from immutable product facts."""
        principal = Principal.model_validate(principal) if isinstance(principal, dict) else principal
        if principal.kind != "human" or principal.account_id != "local":
            raise BusinessError("HUMAN_REQUIRED", status=403)
        with self.store.transaction(write=True) as c:
            product = self.store.get(c, "product", args["product_id"])
            if product["ref"]["revision"] != args["expected_product_revision"]:
                raise BusinessError("REVISION_CONFLICT")
            try:
                old = self.store.get(c, "product_control", args["product_id"])
            except BusinessError as e:
                if e.code != "NOT_FOUND":
                    raise
                old = None
            if old and old["ref"]["revision"] != args.get("expected_control_revision"):
                raise BusinessError("CONTROL_REVISION_CONFLICT")
            for revision in args.get("withdrawn_revisions", []):
                self.store.get(c, "product", args["product_id"], revision=revision)
            if any(not path.startswith("/") for path in args.get("withdrawn_paths", [])):
                raise BusinessError("INVALID_FIELD_PATH")
            body = {"product_id": args["product_id"], "sale_blocked": args.get("sale_blocked", False), "withdrawn_revisions": args.get("withdrawn_revisions", []), "withdrawn_paths": args.get("withdrawn_paths", []), "reason": args["reason"], "human_actor": principal.actor_id}
            ref = self.store.put(c, "product_control", args["product_id"], body, expected=old["ref"]["revision"] if old else None, create=old is None)
            self.store.event(c, "catalog", "product.control_changed", {"product_ref": product["ref"], "control_ref": ref})
            return {"control_ref": ref}

    def _cmd_record_product_revision_choice(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        pinned, head = wire(a.pinned_product_ref), wire(a.observed_head_ref)
        if pinned not in prep["body"]["selected_product_refs"] or pinned["id"] != head["id"]:
            raise BusinessError("PINNED_REFERENCE_CHANGED")
        actual_head = self.store.get(c, "product", head["id"])
        if actual_head["ref"] != head:
            raise BusinessError("OBSERVED_HEAD_CHANGED")
        old = self.store.exact(c, pinned)
        if digest(self._differences(old["body"], actual_head["body"])) != a.diff_hash:
            raise BusinessError("DIFF_CHANGED")
        check = self._currentness(c, prep["body"])
        if check["blockers"]:
            raise BusinessError("PRODUCT_UNAVAILABLE_OR_WITHDRAWN", details=check)
        payload = {"preparation_id": a.preparation_id, "pinned_product_ref": pinned, "observed_head_ref": head, "diff_hash": a.diff_hash, "choice": a.choice, "human_actor": p.actor_id, "operation_id": m.operation_id}
        ref = self.store.artifact(c, "revision_choice", payload)
        receipt = {"revision_choice_ref": ref, **payload}
        c.execute("INSERT OR IGNORE INTO prelive_revision_choices VALUES(?,?,?,?,?,?,?)", (ref["id"], a.preparation_id, pinned["content_hash"], head["content_hash"], a.diff_hash, a.choice, canonical(receipt)))
        if a.choice == "refresh":
            receipt.update(self._refresh(c, prep, pinned, head))
        return receipt

    def _cmd_save_session_draft(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        allowed = SESSION_DRAFT_FIELDS
        if set(a.draft) - allowed:
            raise BusinessError("UNKNOWN_DRAFT_FIELD", details={"fields": sorted(set(a.draft) - allowed)})
        draft = deepcopy(a.draft)
        # The first release always repeats the arranged queue until manual stop.
        # Accept old clients' `once` value but persist one authoritative policy.
        draft["loop_mode"] = "infinite"
        products = {r["id"]: self.store.exact(c, r)["body"] for r in prep["body"]["selected_product_refs"]}
        configured = commerce.parse_room(draft.get("commerce", {}))
        references = (set(configured["product_overrides"]) | set(configured["coupon"]["product_ids"])
                      | set(configured["promotion_product_ids"]))
        missing = sorted(references - set(products))
        if missing:
            # A known, side-effect-free dependency failure. Keep selection an
            # explicit main-Agent/user action; never strip offers or add goods.
            raise BusinessError("PREPARATION_PRODUCTS_NOT_SELECTED", "本场活动引用了尚未选入本场的商品，请核对并明确选入后再保存", details={
                "preparation_ref": prep["ref"],
                "current_selected_product_refs": deepcopy(prep["body"]["selected_product_refs"]),
                "missing_product_ids": missing,
                "requested_product_ids": sorted(references), "commit_state": "not_committed",
                "refresh_query": {"query": "read_preparation", "args": {"preparation_id": a.preparation_id}},
                "required_action": {"command": "update_preparation_products", "tool": "wf_product_materials",
                                    "action": "select", "requires_explicit_selection": True}})
        draft["commerce"] = commerce.validate_room(configured, products)
        draft.setdefault("language", "zh-CN")
        draft.setdefault("tts_selection", {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"})
        draft["tts_selection"] = VoiceSelection.model_validate(draft["tts_selection"]).model_dump()
        if draft["tts_selection"]["provider"] == "system" and draft["tts_selection"]["voice"] != "Huihui":
            raise BusinessError("LOCAL_VOICE_NOT_AVAILABLE")
        ref = self.store.artifact(c, "draft", {"preparation_id": a.preparation_id, "draft": draft})
        body = deepcopy(prep["body"])
        if body["draft_ref"] == ref:
            return {"draft_ref": ref, "preparation_ref": prep["ref"], "unchanged": True}
        body.update(draft_ref=ref, bundle_ref=None, plan_ref=None, compiled_plan_ref=None, requirements_ref=None, audio_manifest_ref=None)
        result = self._save_prep(c, prep, body)
        return {**result, "draft_ref": ref, "blocking_gaps": draft.get("unknowns", [])}

    def _cmd_accept_analysis_report(self, c, a, m, p, s):
        report = wire(a.report)
        if not a.report.summary.strip():
            raise BusinessError("EMPTY_ANALYSIS_REPORT")
        if digest(report) != a.report_hash:
            raise BusinessError("REPORT_HASH_MISMATCH")
        replacements = []
        mapping = {}
        ref_fields = {"kind", "id", "revision", "content_hash"}
        def source_identity(value):
            # RecordRef and ArtifactRef share the same four-field identity.
            # Only exact reference-shaped objects may be rebound: free-form
            # dictionaries with extra business fields must remain intact.
            if (not isinstance(value, dict) or not ref_fields <= value.keys()
                    or value.keys() - (ref_fields | {"schema_version"})
                    or value.get("kind") != "source_manifest"
                    or type(value.get("revision")) is not int
                    or not isinstance(value.get("id"), str)
                    or not isinstance(value.get("content_hash"), str)):
                return None
            return tuple(value[key] for key in ("kind", "id", "revision", "content_hash"))
        for source in a.source_manifests:
            if source.source_ref.kind != "source_manifest" or digest(source.manifest) != source.source_ref.content_hash:
                raise BusinessError("SOURCE_MANIFEST_HASH_MISMATCH")
            blob = self._artifact(c, source.original_blob_ref, "blob")
            if source.manifest.get("original_sha256") != blob["body"]["sha256_bytes"] or source.manifest.get("original_size") != blob["body"]["size"]:
                raise BusinessError("SOURCE_ORIGINAL_BLOB_MISMATCH")
            voice_ref = self.store.artifact(c, "source_manifest", {"manifest": source.manifest, "original_blob_ref": wire(source.original_blob_ref), "origin_source_ref": wire(source.source_ref)})
            trusted_ref = wire(source.source_ref)
            mapping[source_identity(trusted_ref)] = {"old_ref": trusted_ref, "new_ref": voice_ref}
            replacements.append({"old_ref": wire(source.source_ref), "new_ref": voice_ref})
        def rebind(value):
            if isinstance(value, dict):
                mapped = mapping.get(source_identity(value))
                if mapped and ("schema_version" not in value
                        or value["schema_version"] == mapped["old_ref"].get("schema_version")):
                    return deepcopy(mapped["new_ref"])
                return {k: rebind(v) for k, v in value.items()}
            return [rebind(v) for v in value] if isinstance(value, list) else value
        report = rebind(report)
        product_refs = [wire(r) for r in a.product_refs]
        readset = rebind([wire(r) for r in a.readset])
        if set(a.report.product_ids) - {r.id for r in a.product_refs}:
            raise BusinessError("REPORT_PRODUCT_SCOPE_MISMATCH")
        for ref in [*product_refs, *readset, *report["dependencies"], *report["source_refs"]]:
            record = self.store.exact(c, ref)
            record_preparation = record["body"].get("preparation_id")
            if record_preparation and record_preparation != a.preparation_id:
                raise BusinessError("REPORT_DEPENDENCY_SCOPE_MISMATCH", status=403)
        accepted_report_hash = digest(report)
        lineage = {"prepared_report_ref": wire(a.prepared_report_ref), "prepared_report_hash": a.report_hash, "accepted_report_hash": accepted_report_hash, "transformation": "verified_source_ref_rebinding" if replacements else "none", "original_prepared_report": wire(a.report) if replacements else None}
        ref = self.store.artifact(c, "analysis", {"role": a.role, "preparation_id": a.preparation_id, "report": report, "product_refs": product_refs, "readset": readset, "source_ref_replacements": replacements, "lineage": lineage, "accepted": True})
        if a.role == "audience_scenes":
            for product in a.product_refs:
                c.execute("INSERT OR IGNORE INTO prelive_research VALUES(?,?,?)", (product.id, ref["id"], digest(report.get("fields", {}).get("context", {}))))
        result = {"analysis_ref": ref, "state": "accepted", "prepared_report_hash": a.report_hash, "accepted_report_hash": accepted_report_hash, "source_ref_replacements": replacements}
        if a.preparation_id:
            prep = self._prep(c, a.preparation_id, a.expected_revision)
            body = deepcopy(prep["body"])
            if ref not in body["analysis_refs"]:
                body["analysis_refs"].append(ref)
                body.update(bundle_ref=None, plan_ref=None)
                result.update(self._save_prep(c, prep, body))
        return result

    def _cmd_save_context_bundle(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        # Ref permits both RecordRef and ArtifactRef on the wire. Resolve exact
        # identities before comparing so optional schema metadata is canonical;
        # wrong hashes/revisions/schema and foreign preparations still fail.
        fields = {key: [self._artifact(c, r, preparation_id=a.preparation_id) for r in getattr(a, key)]
                  for key in ("product_refs", "analysis_refs", "recap_refs", "source_refs", "prior_script_refs")}
        draft = self._artifact(c, a.draft_ref, "draft", a.preparation_id)
        normalized = {key: [r["ref"] for r in values] for key, values in fields.items()}
        changed = [key for key, expected in (("product_refs", prep["body"]["selected_product_refs"]),
                    ("draft_ref", prep["body"]["draft_ref"]))
                   if (draft["ref"] if key == "draft_ref" else normalized[key]) != expected]
        if changed:
            raise BusinessError("CONTEXT_INPUTS_CHANGED", "本场上下文输入已变化，请读取当前本场引用后重建",
                details={"changed_fields": changed, "preparation_ref": prep["ref"],
                         "refresh_query": {"query": "read_preparation", "args": {"preparation_id": a.preparation_id}},
                         "input_section": "context_inputs"})
        if {canonical(r) for r in normalized["recap_refs"]} != {canonical(r) for r in prep["body"]["recap_refs"]}:
            raise BusinessError("CONTEXT_RECAP_MISSING", details={"changed_fields": ["recap_refs"],
                "refresh_query": {"query": "read_preparation", "args": {"preparation_id": a.preparation_id}},
                "input_section": "context_inputs"})
        records = [*fields["product_refs"], *fields["analysis_refs"], draft,
                   *fields["recap_refs"], *fields["source_refs"], *fields["prior_script_refs"]]
        body = {"preparation_id": a.preparation_id, **normalized, "draft_ref": draft["ref"],
                "user_decisions": a.user_decisions, "records": records}
        ref = self.store.artifact(c, "context", body)
        prep_body = deepcopy(prep["body"])
        prep_body.update(bundle_ref=ref, plan_ref=None)
        return {**self._save_prep(c, prep, prep_body), "context_bundle_ref": ref}

    def _cmd_save_script_plan(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        bundle_record = self._artifact(c, a.bundle_ref, "context", a.preparation_id)
        if bundle_record["ref"] != prep["body"]["bundle_ref"]:
            raise BusinessError("CONTEXT_BUNDLE_CHANGED")
        bundle = bundle_record["body"]
        if not a.items:
            raise BusinessError("EMPTY_SCRIPT_PLAN")
        normalized_items = []
        if len({item.variant_id for item in a.items}) != len(a.items):
            raise BusinessError("DUPLICATE_VARIANT_ID", "一份计划中每套话术需要独立的方案ID", status=422)
        # A display version identifies a variant line; revision identifies an
        # edit on that line. Never relabel historical script/approval records.
        used_numbers = {}
        for script_ref in prep["body"]["script_refs"]:
            current = self._artifact(c, script_ref, "script_revision", a.preparation_id)["body"]
            used_numbers.setdefault(current["product_id"], set()).add(current["version_number"])
        for item_index, item in enumerate(a.items):
            product_ref = self._artifact(c, item.product_ref, "product")["ref"]
            if product_ref not in prep["body"]["selected_product_refs"]:
                raise BusinessError("PRODUCT_NOT_SELECTED")
            if len({x.key for x in item.modules}) != len(item.modules):
                raise BusinessError("DUPLICATE_MODULE_ACTION")
            keys = {x.key for x in item.modules}
            if not {"product_intro", "selling_points", "cta"} <= keys:
                raise BusinessError("INCOMPLETE_MODULE_PLAN")
            if not any(x.key in {"opening_long", "opening_short"} and x.action != "disable" for x in item.modules):
                raise BusinessError("OPENING_MODULE_REQUIRED", "至少准备一种本场需要的开场；另一种可在重排后按缺项补充")
            base_record = self._artifact(c, item.base_script_ref, "script_revision", a.preparation_id) if item.base_script_ref else None
            base = base_record["body"] if base_record else None
            if base and (base_record["ref"] not in prep["body"]["script_refs"] or base["product_id"] != item.product_ref.id):
                raise BusinessError("BASE_SCRIPT_TARGET_MISMATCH")
            normalized_item = {**wire(item), "product_ref": product_ref}
            if item.original_script_ref:
                original = self._artifact(c, item.original_script_ref, "script_revision", a.preparation_id)
                if (item.base_script_ref or original["body"]["product_id"] != product_ref["id"]
                        or original["ref"] not in prep["body"]["script_refs"]
                        or item.variant_id == original["body"]["variant_id"]):
                    raise BusinessError("ORIGINAL_SCRIPT_TARGET_MISMATCH", details={"commit_state": "not_committed"})
                normalized_item["original_script_ref"] = original["ref"]
            if base_record:
                normalized_item["base_script_ref"] = base_record["ref"]
            normalized_actions = []
            for action in item.modules:
                if action.action == "disable" and action.key in {"product_intro", "selling_points", "cta"}:
                    raise BusinessError("REQUIRED_MODULE_DISABLED")
                if action.key == "transition" and action.action in {"generate", "patch"}:
                    raise BusinessError("TRANSITION_GENERATION_DISABLED", "新稿直接按模块顺序播放，无须单独生成衔接话术")
                if action.action in {"reuse", "patch"} and not action.base_module_ref:
                    raise BusinessError("BASE_MODULE_REQUIRED")
                if action.base_module_ref:
                    module_record = self._artifact(c, action.base_module_ref, "module")
                    module = module_record["body"]
                    if module["module_key"] != action.key:
                        raise BusinessError("BASE_MODULE_KEY_MISMATCH")
                    if not base or base["module_refs"].get(action.key) != module_record["ref"]:
                        if base_record is None:
                            details = ScriptPlanModuleBaseInvalidDetails(
                                command="save_script_plan", commit_state="not_committed",
                                preparation_ref=prep["ref"], bundle_ref=bundle_record["ref"], item_index=item_index,
                                module_key=action.key, requested_product_ref=product_ref,
                                requested_variant_id=item.variant_id, requested_version_number=item.version_number,
                                requested_base_module_ref=module_record["ref"], requested_base_script_ref=None,
                                reason="BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE").model_dump(mode="json")
                            raise BusinessError("BASE_MODULE_TARGET_MISMATCH",
                                "显式复用需要当前完整基准话术；部分成果可由主Agent选择既有同job受控重试，或保持未变生成输入按既有缓存规则复用，以真实回执为准。不会自动改引用或重试。",
                                status=422, details=details)
                        raise BusinessError("BASE_MODULE_TARGET_MISMATCH")
                normalized_action = wire(action)
                if action.base_module_ref:
                    normalized_action["base_module_ref"] = module_record["ref"]
                normalized_actions.append(normalized_action)
            normalized_item["modules"] = normalized_actions
            if base:
                if item.variant_id != base["variant_id"]:
                    raise BusinessError("BASE_VARIANT_TARGET_MISMATCH", "修改现有话术时必须保持其方案ID", status=422)
                normalized_item["version_number"] = base["version_number"]
            else:
                numbers = used_numbers.setdefault(item.product_ref.id, set())
                requested_number = item.version_number
                if requested_number in numbers:
                    requested_number = max(numbers, default=0) + 1
                normalized_item["version_number"] = requested_number
                numbers.add(requested_number)
            requested_mode = getattr(item, "generation_mode", "auto")
            all_generated = any(x["action"] == "generate" for x in normalized_actions) and all(
                x["action"] in {"generate", "disable"} for x in normalized_actions)
            if requested_mode == "whole_script" and not all_generated:
                raise BusinessError("INVALID_WHOLE_SCRIPT_PLAN", "整版生成不能包含局部修改或复用；请保留模块模式", status=422)
            use_whole = requested_mode == "whole_script" or (
                requested_mode == "auto" and all_generated and callable(getattr(self.adapter, "generate_script", None)))
            if use_whole and not callable(getattr(self.adapter, "generate_script", None)):
                raise BusinessError("WHOLE_SCRIPT_GENERATOR_NOT_CONFIGURED", status=503)
            normalized_item["generation_mode"] = "whole_script" if use_whole else "module"
            if use_whole:
                from .whole_script_generation import prompt_revision
                normalized_item["generator_revision"] = prompt_revision()
                normalized_item["version_context_contract"] = "existing-script-text-v3"
            normalized_items.append(normalized_item)
        finding_ids = set()
        for ref in bundle["recap_refs"]:
            recap = self._artifact(c, ref)["body"]
            finding_ids.update(f.get("finding_id") or f.get("id") for f in recap["document"].get("findings", []))
        if finding_ids - {d.get("finding_id") for d in a.recap_dispositions}:
            raise BusinessError("RECAP_DISPOSITION_MISSING")
        ref = self.store.artifact(c, "script_plan", {"preparation_id": a.preparation_id, "bundle_ref": bundle_record["ref"], "items": normalized_items, "recap_dispositions": a.recap_dispositions})
        body = deepcopy(prep["body"])
        body["plan_ref"] = ref
        return {**self._save_prep(c, prep, body), "script_plan_ref": ref}

    def _enqueue(self, c, kind, prep, items, m, p, s, extra=None):
        identity = uid("prelive_job")
        body = {"id": identity, "kind": kind, "preparation_id": prep["body"]["id"], "room_id": prep["body"]["room_id"], "generation_epoch": prep["body"]["generation_epoch"], "run_id": p.run_id, "principal": wire(p), "mutation": wire(m), "scope": wire(s), "status": "queued", "cancel_requested": False, "created_at": now(), "items": [{"item_id": uid("item"), "status": "queued", "attempt": 0, "call_state": "not_sent", "input": x, "output": None, "error": None} for x in items], **(extra or {})}
        ref = self.store.put(c, "prelive_job", identity, body, create=True)
        return {"job_id": identity, "job_ref": ref, "status": "queued", "item_count": len(items)}

    def _cmd_submit_script_change(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        plan_record = self._artifact(c, a.plan_ref, "script_plan", a.preparation_id)
        context_record = self._artifact(c, a.context_bundle_ref, "context", a.preparation_id)
        if prep["body"]["generation_epoch"] != a.expected_generation_epoch or prep["body"]["plan_ref"] != plan_record["ref"] or prep["body"]["bundle_ref"] != context_record["ref"]:
            raise BusinessError("GENERATION_INPUTS_CHANGED")
        plan, context = plan_record["body"], context_record["body"]
        existing = self.store.all(c, "script_revision")
        for item in plan["items"]:
            if not item.get("base_script_ref") and any(r["body"]["variant_id"] == item["variant_id"] for r in existing):
                raise BusinessError("VARIANT_ALREADY_EXISTS", "该版本线已存在，请指定基准修订或创建新的版本ID")
        return self._enqueue(c, "scripts", prep, plan["items"], m, p, s, {"plan_ref": plan_record["ref"], "context_ref": context_record["ref"], "context": context})

    def _cmd_import_script(self, c, a, m, p, s):
        from .script_imports import import_script
        return import_script(self, c, a)

    def _cmd_patch_script(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        script = self._artifact(c, a.script_ref, "script_revision", a.preparation_id)
        if script["ref"] not in prep["body"]["script_refs"]:
            raise BusinessError("SCRIPT_NOT_IN_PREPARATION")
        old_ref = script["body"]["module_refs"].get(a.module_key)
        if not old_ref:
            raise BusinessError("MODULE_NOT_FOUND")
        old = self._artifact(c, old_ref, "module")["body"]
        from .script_patch_scope import selected_sentence_objects
        selected, indexes = selected_sentence_objects(old["sentences"], a.sentence_ids)
        if digest(selected) != a.selected_text_hash:
            raise BusinessError("TEXT_SELECTION_STALE")
        module = self._module_body(a.module_key, a.sentences, old_ref)
        from .script_patch_scope import guard_patch_scope
        guard_patch_scope(a, script, old, module)
        replacement_ids = [row["id"] for row in module["sentences"]]
        replacement_hash = digest(module["sentences"])
        if a.sentence_ids:
            module["sentences"] = (deepcopy(old["sentences"][:indexes[0]]) + module["sentences"]
                                   + deepcopy(old["sentences"][indexes[-1] + 1:]))
        module_ref = self.store.artifact(c, "module", module)
        body = deepcopy(script["body"])
        body["module_refs"][a.module_key] = module_ref
        if body.get("origin") == "imported":
            body.setdefault("original_raw_text", body.get("raw_text", ""))
            body["raw_text"] = "".join(row["text"] for row in module["sentences"])
            body["edited_after_import"] = True
        body["content_diagnostics"] = self._script_content_diagnostics(c, body["module_refs"])
        body["parent_ref"] = script["ref"]
        body["script_revision"] += 1
        body["created_at"] = now()
        new_ref = self.store.artifact(c, "script_revision", body)
        prep_body = deepcopy(prep["body"])
        prep_body["script_refs"] = [new_ref if r == script["ref"] else r for r in prep_body["script_refs"]]
        # Preserve occurrence identities and positions; only replace this exact line.
        if prep_body["playlist_ref"]:
            playlist = deepcopy(self._artifact(c, prep_body["playlist_ref"])["body"])
            for item in playlist["items"]:
                if item["script_revision_ref"] == script["ref"]:
                    item["script_revision_ref"] = new_ref
            prep_body["playlist_ref"] = self.store.artifact(c, "playlist", playlist)
        prep_body.update(compiled_plan_ref=None, requirements_ref=None, audio_manifest_ref=None)
        return {**self._save_prep(c, prep, prep_body), "script_ref": new_ref, "replacement": {
            "old_ref": script["ref"], "new_ref": new_ref, "module_key": a.module_key,
            "old_sentence_ids": a.sentence_ids, "old_selected_text_hash": a.selected_text_hash,
            "sentence_ids": replacement_ids, "selected_text_hash": replacement_hash}}

    def _cmd_save_playlist(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        items = [wire(i) for i in a.items]
        if len({i["occurrence_id"] for i in items}) != len(items):
            raise BusinessError("DUPLICATE_OCCURRENCE_ID")
        selected = {r["id"] for r in prep["body"]["selected_product_refs"]}
        for item in items:
            script_record = self._artifact(c, item["script_revision_ref"], "script_revision", a.preparation_id)
            script = script_record["body"]
            item["script_revision_ref"] = script_record["ref"]
            if item["product_id"] not in selected or script["product_id"] != item["product_id"] or item["script_revision_ref"] not in prep["body"]["script_refs"]:
                raise BusinessError("PLAYLIST_TARGET_NOT_SELECTED")
        if a.reorder_spec:
            if not prep["body"]["playlist_ref"]:
                raise BusinessError("PLAYLIST_REQUIRED")
            old = self._artifact(c, prep["body"]["playlist_ref"])["body"]
            preview = reorder(old["items"], wire(a.reorder_spec))
            expected_hash = digest({"base_ref": prep["body"]["playlist_ref"], **preview})
            if a.preview_hash != expected_hash or preview["proposed_items"] != items:
                raise BusinessError("REORDER_PREVIEW_STALE")
        body = {"preparation_id": a.preparation_id, "items": items, "loop_mode": "infinite", "product_group_order": a.product_group_order or list(dict.fromkeys(i["product_id"] for i in items)), "reorder_provenance": wire(a.reorder_spec)}
        if prep["body"]["playlist_ref"]:
            old = self._artifact(c, prep["body"]["playlist_ref"])["body"]
            if old["items"] == items and old["loop_mode"] == body["loop_mode"] and old["product_group_order"] == body["product_group_order"]:
                return {"playlist_ref": prep["body"]["playlist_ref"], "preparation_ref": prep["ref"], "unchanged": True}
        ref = self.store.artifact(c, "playlist", body)
        prep_body = deepcopy(prep["body"])
        prep_body.update(playlist_ref=ref, compiled_plan_ref=None, requirements_ref=None, audio_manifest_ref=None)
        return {**self._save_prep(c, prep, prep_body), "playlist_ref": ref}

    def _cmd_compile_playback_plan(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        playlist_record = self._artifact(c, a.playlist_ref, "playlist", a.preparation_id)
        if prep["body"]["playlist_ref"] != playlist_record["ref"]:
            raise BusinessError("PLAYLIST_CHANGED")
        playlist = playlist_record["body"]
        for occurrence in playlist["items"]:
            script = self._artifact(c, occurrence["script_revision_ref"], "script_revision")["body"]
            if script["product_ref"] not in prep["body"]["selected_product_refs"]:
                raise BusinessError("SCRIPT_FACTS_STALE", "商品引用已刷新，请先处理受影响话术")
        if not prep["body"]["draft_ref"]:
            raise BusinessError("SESSION_DRAFT_REQUIRED")
        draft = self._artifact(c, prep["body"]["draft_ref"])["body"]["draft"]
        result = compile_items(playlist["items"], playlist["loop_mode"], lambda r: self._artifact(c, r)["body"], lambda r: self._artifact(c, r)["body"])
        voice = draft["tts_selection"]
        requirements = []
        original_requirements = result.pop("requirements")
        if voice["provider"] == "minimax":
            # The text-only compiler may merge equal text across modules. Bind
            # from each selected sentence, so different module styles remain
            # separate synthesis work and immutable human-review inputs.
            by_id = {}
            for entry in result["entries"]:
                for sentence in entry["sentences"]:
                    spoken = sentence.get("spoken_text") or sentence["text"]
                    style = resolved_style(sentence_style({"stage": sentence["module_key"]}, "minimax"),
                        "minimax", voice["model"])
                    rid = digest({"spoken_text": spoken, "voice": voice, "style": style})
                    sentence.update(requirement_id=rid, style=deepcopy(style))
                    requirement = by_id.setdefault(rid, {"requirement_id": rid, "spoken_text": spoken,
                        "sentence_refs": [], "voice": voice, "style": style})
                    if sentence["module_ref"] not in requirement["sentence_refs"]:
                        requirement["sentence_refs"].append(deepcopy(sentence["module_ref"]))
            requirements = list(by_id.values())
            result["playback_statistics"] = playback_statistics(result["entries"])
        else:
            # Keep local/system identity and legacy three-argument adapters.
            remap = {}
            for req in original_requirements:
                rid = digest({"spoken_text": req["spoken_text"], "voice": voice})
                remap[req["requirement_id"]] = rid
                requirements.append({**req, "requirement_id": rid, "voice": voice})
            for entry in result["entries"]:
                for sentence in entry["sentences"]:
                    sentence["requirement_id"] = remap[sentence["requirement_id"]]
        plan_ref = self.store.artifact(c, "compiled_plan", {"preparation_id": a.preparation_id, "playlist_ref": playlist_record["ref"], "draft_ref": prep["body"]["draft_ref"], **result})
        req_ref = self.store.artifact(c, "requirements", {"preparation_id": a.preparation_id, "compiled_plan_ref": plan_ref, "voice": voice, "items": requirements})
        prep_body = deepcopy(prep["body"])
        changed = prep_body["compiled_plan_ref"] != plan_ref or prep_body["requirements_ref"] != req_ref
        prep_body.update(compiled_plan_ref=plan_ref, requirements_ref=req_ref)
        return {**self._save_prep(c, prep, prep_body, invalidate=changed), "compiled_plan_ref": plan_ref, "requirements_ref": req_ref, "required_sentence_count": len(requirements), "supercycle_length": result["supercycle_length"]}

    def _text_review(self, c, prep):
        for key in ("compiled_plan_ref", "requirements_ref", "draft_ref"):
            if not prep.get(key):
                raise BusinessError("DEPENDENCY_NOT_READY", details={"missing": key})
        currentness = self._currentness(c, prep)
        if currentness["blockers"]:
            raise BusinessError("CURRENTNESS_BLOCKED", details=currentness)
        plan = self._artifact(c, prep["compiled_plan_ref"])["body"]
        if plan["draft_ref"] != prep["draft_ref"] or plan["playlist_ref"] != prep["playlist_ref"]:
            raise BusinessError("COMPILED_PLAN_STALE")
        requirements = self._artifact(c, prep["requirements_ref"])["body"]
        if not requirements["items"]:
            raise BusinessError("EMPTY_AUDIO_REQUIREMENTS")
        if any(not r["spoken_text"].strip() for r in requirements["items"]):
            raise BusinessError("EMPTY_SCRIPT_TEXT")
        payload = {"preparation_id": prep["id"], "generation_epoch": prep["generation_epoch"], "product_refs": prep["selected_product_refs"], "draft_ref": prep["draft_ref"], "compiled_plan_ref": prep["compiled_plan_ref"], "requirements_ref": prep["requirements_ref"], "playlist_ref": prep["playlist_ref"], "voice": requirements["voice"], "texts": requirements["items"], "plan_summary": [{"original_position": e["original_position"], "occurrence_id": e["occurrence_id"], "opening_mode": e["opening_mode"]} for e in plan["entries"]], "cost_scope": {"provider": requirements["voice"]["provider"], "characters": sum(len(r["spoken_text"]) for r in requirements["items"]), "currency_estimate": None, "notice": "按供应商实际计费；未伪造价格"}}
        if plan.get("playback_statistics") is not None:
            # New plans expose canonical version labels and text statistics.
            # Keep legacy review payloads unchanged and never add live audio
            # duration here: rendering must not invalidate a text approval.
            payload["playback_statistics"] = deepcopy(plan["playback_statistics"])
            payload["script_versions"] = []
            seen = set()
            for entry in plan["entries"]:
                ref = entry["script_revision_ref"]
                if ref["id"] in seen:
                    continue
                seen.add(ref["id"])
                script = self._artifact(c, ref, "script_revision", prep["id"])["body"]
                payload["script_versions"].append({"script_ref": ref, "product_id": script["product_id"],
                    "variant_id": script["variant_id"], "version_number": script["version_number"],
                    "script_revision": script["script_revision"]})
        return {"review_hash": digest(payload), "request_id": "review_" + digest(payload).split(":")[-1], **payload, "revision_currentness": currentness}

    def _approval(self, c, prep, ref, review_hash=None):
        approval = self._artifact(c, ref, "approval", prep["id"])["body"]
        if approval.get("revoked"):
            raise BusinessError("TEXT_APPROVAL_REVOKED")
        current = self._text_review(c, prep)
        if approval["review_hash"] != current["review_hash"] or (review_hash and review_hash != current["review_hash"]):
            raise BusinessError("TEXT_APPROVAL_STALE")
        # Revocations are separate mutable controls; approved artifact is immutable.
        try:
            control = self.store.get(c, "approval_control", ref["id"])["body"]
        except BusinessError as e:
            if e.code != "NOT_FOUND":
                raise
        else:
            if control["status"] == "revoked":
                raise BusinessError("TEXT_APPROVAL_REVOKED")
        return approval

    def _cmd_approve_text_for_audio(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        review = self._text_review(c, prep["body"])
        if a.review_hash != review["review_hash"]:
            raise BusinessError("TEXT_REVIEW_STALE")
        ref = self.store.artifact(c, "approval", {"preparation_id": a.preparation_id, "review_hash": a.review_hash, "bound_review": {k: v for k, v in review.items() if k != "revision_currentness"}, "actor_id": p.actor_id, "approved_at": now(), "operation_id": m.operation_id})
        body = deepcopy(prep["body"])
        body["approval_ref"] = ref
        return {**self._save_prep(c, prep, body, invalidate=False), "approval_ref": ref, "review_hash": a.review_hash}

    def _cmd_revoke_text_approval(self, c, a, m, p, s):
        approval = self._artifact(c, a.approval_ref, "approval")
        try:
            old = self.store.get(c, "approval_control", a.approval_ref.id)
        except BusinessError:
            old = None
        ref = self.store.put(c, "approval_control", a.approval_ref.id, {"status": "revoked", "reason": a.reason, "actor_id": p.actor_id}, expected=old["ref"]["revision"] if old else None, create=not old)
        return {"approval_ref": wire(a.approval_ref), "control_ref": ref, "revoked": True, "already_generated_assets_preserved": True}

    def _cmd_prepare_required_audio(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        if prep["body"]["generation_epoch"] != a.expected_generation_epoch:
            raise BusinessError("GENERATION_EPOCH_CHANGED")
        self._approval(c, prep["body"], wire(a.approval_ref), a.review_hash)
        self._assert_current_job_outcomes_known(c, prep["body"])
        requirements = self._artifact(c, prep["body"]["requirements_ref"])["body"]
        return self._enqueue(c, "audio", prep, requirements["items"], m, p, s, {"approval_ref": wire(a.approval_ref), "review_hash": a.review_hash, "requirements_ref": prep["body"]["requirements_ref"]})

    def _cmd_preview_audio(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        self._approval(c, prep["body"], wire(a.approval_ref), a.review_hash)
        requirements = self._artifact(c, prep["body"]["requirements_ref"])["body"]
        items = [r for r in requirements["items"] if r["requirement_id"] == a.requirement_id]
        if len(items) != 1:
            raise BusinessError("PREVIEW_REQUIREMENT_NOT_FOUND")
        return self._enqueue(c, "preview", prep, items, m, p, s, {"approval_ref": wire(a.approval_ref), "review_hash": a.review_hash, "requirements_ref": prep["body"]["requirements_ref"]})

    def _q_inspect_missing_audio_reissue(self, c, a, p, s):
        from .audio_reissue import inspect as inspect_reissue
        return inspect_reissue(self, c, a)

    def _cmd_approve_missing_audio_reissue(self, c, a, m, p, s):
        from .audio_reissue import approve
        return approve(self, c, a, m, p, s)

    def _cmd_approve_script_audio_preview(self, c, a, m, p, s):
        from .script_audio_preview import approve
        return approve(self, c, a, m, p, s)

    def _cmd_preview_script_audio(self, c, a, m, p, s):
        raise BusinessError("ASYNC_COMMAND_REQUIRED")

    def _q_read_script_audio_preview(self, c, a, p, s):
        from .script_audio_preview import read
        return read(self, c, a, p, s)

    def _q_list_script_audio_previews(self, c, a, p, s):
        from .script_audio_preview import list_previews
        return list_previews(self, c, a, p, s)

    def _cmd_retry_failed_items(self, c, a, m, p, s):
        import json
        job = self.store.get(c, "prelive_job", a.job_id)
        body = deepcopy(job["body"])
        self._assert_scope(c, p, s, body["preparation_id"])
        if body.get("audio_reissue_approval_ref"):
            raise BusinessError("AUDIO_REISSUE_ONCE_ONLY", "一次补合成不进入普通自动重试；新未知需要另一次真实人工决定")
        if body["run_id"] != p.run_id and p.kind == "agent":
            raise BusinessError("JOB_RUN_MISMATCH")
        if body["run_id"] != p.run_id and p.kind == "controller":
            # The controller verified explicit conversation ancestry and the
            # remote submit receipt. Independently require the NEW live grant;
            # never revive the closed owner's grant or allow a paid retry.
            if a.recovery_mode != "revalidate_archived" or body["kind"] != "scripts" or not p.run_id:
                raise BusinessError("RETRY_SCOPE_DENIED", status=403)
            current = Principal(kind="agent", actor_id="agent-runtime", account_id=p.account_id, run_id=p.run_id)
            self._grant(c, "retry_failed_items", m, current, s, a)
            prep = self._prep(c, body["preparation_id"])
            original_args = parse_args("submit_script_change", {"preparation_id": body["preparation_id"],
                "expected_revision": prep["ref"]["revision"], "plan_ref": body["plan_ref"],
                "context_bundle_ref": body["context_ref"], "expected_generation_epoch": body["generation_epoch"]})
            self._grant(c, "submit_script_change", m, current, s, original_args)
            original_principal = Principal.model_validate(body["principal"])
            original_mutation = MutationContext.model_validate(body["mutation"])
            stored = c.execute("SELECT command,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?",
                (original_principal.actor_id, original_mutation.operation_id)).fetchone()
            original_receipt = json.loads(stored["response_json"]) if stored else {}
            if (original_principal.run_id != body["run_id"] or original_principal.account_id != p.account_id
                    or not stored or stored["command"] != "submit_script_change"
                    or original_receipt.get("data", {}).get("job_id") != a.job_id
                    or original_receipt.get("data", {}).get("job_ref", {}).get("id") != a.job_id
                    or wire(s) != body.get("scope")):
                raise BusinessError("JOB_SOURCE_PROOF_INVALID", status=403)
            grant = c.execute("SELECT constraints_json FROM prelive_grants WHERE grant_id=?", (m.grant_id,)).fetchone()
            constraints = json.loads(grant["constraints_json"])
            allowed_modules = {row["module_key"] for row in constraints if row.get("module_key")}
            if allowed_modules:
                for item in body["items"]:
                    if item["status"] not in {"failed", "interrupted"}:
                        continue
                    spec = item["input"]
                    base = self._artifact(c, spec["base_script_ref"], "script_revision", body["preparation_id"])["body"] if spec.get("base_script_ref") else {}
                    if any(action["key"] not in allowed_modules and (action["action"] not in {"disable", "reuse"}
                            or action["action"] == "disable" and base.get("module_refs", {}).get(action["key"]))
                            for action in spec.get("modules", [])):
                        raise BusinessError("SCOPE_CONFLICT", "归档恢复模块超出本次授权", status=403)
            body["archive_revalidation_execution"] = {"original_run_id": body["run_id"],
                "original_submit_operation_id": original_mutation.operation_id,
                "requesting_run_id": p.run_id, "operation_id": m.operation_id,
                "principal": wire(current), "mutation": wire(m), "scope": wire(s)}
        self._job_fence(c, body)
        mode = a.recovery_mode
        if mode == "revalidate_archived" and body["kind"] != "scripts":
            raise BusinessError("ARCHIVED_REVALIDATION_NOT_APPLICABLE", "仅支持原话术任务的已拒绝归档响应本地重验")
        count = 0
        for item in body["items"]:
            if item["status"] in {"failed", "interrupted"} and item["call_state"] != "timeout_unknown":
                if mode == "revalidate_archived":
                    spec = item["input"]
                    whole = spec.get("generation_mode") == "whole_script"
                    targets = ["whole_script"] if whole else [x["key"] for x in spec["modules"]
                        if x["action"] not in {"disable", "reuse"} and x["key"] not in item.get("module_results", {})]
                    prior_revalidation = next((x for x in item.get("archive_revalidations", [])
                        if x["revalidation_id"] == item.get("active_archive_revalidation_id")), None)
                    if not targets and item.get("recovery_mode") == mode and prior_revalidation:
                        # All local modules may have committed before the final
                        # script transaction stopped. Revalidate the same bound
                        # original response, then reuse those exact modules.
                        targets = list(prior_revalidation["targets"])
                    bound, missing = {}, []
                    for target in targets:
                        attempt = next((x for x in reversed(item.get("provider_attempts", []))
                            if x.get("validation_state") == "rejected" and x.get("provider_response_ref")
                            and x.get("purpose") == ("copywriting.whole_script" if whole else "copywriting." + target)), None)
                        if attempt is None:
                            missing.append(target)
                            continue
                        archived = self.store.exact(c, attempt["provider_response_ref"])
                        value = archived["body"]
                        if (archived["ref"]["kind"] != "provider_response" or value.get("job_id") != a.job_id
                                or value.get("item_id") != item["item_id"] or value.get("attempt_id") != attempt["attempt_id"]):
                            raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
                        bound[target] = {"attempt_id": attempt["attempt_id"], "response_ref": archived["ref"],
                            "dependency_hash": attempt.get("input", {}).get("dependency_hash")}
                    if missing:
                        raise BusinessError("ARCHIVED_RESPONSE_INCOMPLETE", "原归档不足以完成本地恢复；不会补发供应商请求",
                            details={"item_id": item["item_id"], "missing_module_keys": missing, "provider_reissued": False})
                    if not bound:
                        continue
                    request = {"revalidation_id": uid("archive_revalidation"), "recovery_mode": mode,
                        "operation_id": m.operation_id, "actor_id": p.actor_id, "requested_at": now(),
                        "prior_error": deepcopy(item.get("error")), "targets": bound, "checks": [], "status": "queued",
                        "provider_reissued": False}
                    if body.get("archive_revalidation_execution"):
                        request["requesting_run_id"] = body["archive_revalidation_execution"]["requesting_run_id"]
                    if prior_revalidation:
                        request["prior_revalidation_id"] = prior_revalidation["revalidation_id"]
                    item.setdefault("archive_revalidations", []).append(request)
                    item["active_archive_revalidation_id"] = request["revalidation_id"]
                    item["recovery_mode"] = mode
                else:
                    item.pop("active_archive_revalidation_id", None)
                    item.pop("recovery_mode", None)
                item.update(status="queued", error=None, call_state="not_sent")
                count += 1
        if not count:
            if any(i["call_state"] == "timeout_unknown" for i in body["items"]):
                raise BusinessError("EXTERNAL_OUTCOME_UNKNOWN", "请求结果未知，不能自动重复计费；已完成模块仍保留")
            raise BusinessError("NO_RETRYABLE_ITEMS")
        body["status"] = "queued"
        body["cancel_requested"] = False
        ref = self.store.put(c, "prelive_job", a.job_id, body, expected=job["ref"]["revision"])
        return {"job_id": a.job_id, "job_ref": ref, "retry_item_count": count, "recovery_mode": mode,
            "provider_reissued": False if mode == "revalidate_archived" else None}

    def _cmd_cancel_job(self, c, a, m, p, s):
        job = self.store.get(c, "prelive_job", a.job_id)
        self._assert_scope(c, p, s, job["body"]["preparation_id"])
        if p.kind == "agent" and job["body"]["run_id"] != p.run_id:
            raise BusinessError("JOB_RUN_MISMATCH")
        body = deepcopy(job["body"])
        body.update(cancel_requested=True, status="cancelled")
        for item in body["items"]:
            if item["status"] == "queued":
                item["status"] = "cancelled"
        return {"job_id": a.job_id, "job_ref": self.store.put(c, "prelive_job", a.job_id, body, expected=job["ref"]["revision"]), "status": "cancelled"}

    def _audio_valid(self, asset):
        path = asset.get("path")
        if not path:
            return False
        try:
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(self.runtime.resolve()):
                return False
            if asset.get("sha256_bytes") and "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest() != asset["sha256_bytes"]:
                return False
            with wave.open(str(resolved), "rb") as audio:
                return audio.getnframes() >= 100 and audio.getframerate() > 0
        except (OSError, ValueError, EOFError, wave.Error):
            return False

    def _manifest(self, c, prep):
        if not prep.get("audio_manifest_ref"):
            raise BusinessError("AUDIO_NOT_READY")
        manifest = self._artifact(c, prep["audio_manifest_ref"], "audio_manifest")["body"]
        requirements = self._artifact(c, prep["requirements_ref"])["body"]
        actual = {a["requirement_id"]: a for a in manifest["assets"]}
        missing = [r["requirement_id"] for r in requirements["items"] if r["requirement_id"] not in actual
            or not self._audio_valid(actual[r["requirement_id"]])
            or not self._audio_style_matches(actual[r["requirement_id"]], r)]
        if missing or not requirements["items"] or manifest["requirements_ref"] != prep["requirements_ref"]:
            raise BusinessError("AUDIO_NOT_READY", details={"missing_requirement_ids": missing})
        return manifest

    def _cmd_publish_preparation(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        self._approval(c, prep["body"], wire(a.approval_ref))
        self._manifest(c, prep["body"])
        self._assert_current_job_outcomes_known(c, prep["body"])
        current = self._currentness(c, prep["body"])
        if current["blockers"]:
            raise BusinessError("CURRENTNESS_BLOCKED", details=current)
        body = {"preparation_id": a.preparation_id, "room_id": prep["body"]["room_id"], "base_room_revision": prep["body"]["base_room_revision"], "product_refs": prep["body"]["selected_product_refs"], "draft_ref": prep["body"]["draft_ref"], "compiled_plan_ref": prep["body"]["compiled_plan_ref"], "requirements_ref": prep["body"]["requirements_ref"], "audio_manifest_ref": prep["body"]["audio_manifest_ref"], "approval_ref": wire(a.approval_ref), "playlist_ref": prep["body"]["playlist_ref"], "generation_epoch": prep["body"]["generation_epoch"]}
        ref = self.store.artifact(c, "publication", body)
        adoption_currentness = self._currentness(c, prep["body"], choices_required=True)
        ready_status = "candidate_requires_choice" if adoption_currentness["blockers"] else "ready_to_adopt"
        prep_body = deepcopy(prep["body"])
        prep_body.update(candidate_publication_ref=ref, status=ready_status)
        return {**self._save_prep(c, prep, prep_body, invalidate=False), "publication_ref": ref, "status": ready_status, "currentness": adoption_currentness, "adopted": False, "started": False}

    def _publication_review(self, c, ref):
        publication = self._artifact(c, ref, "publication")
        body = publication["body"]
        prep = self._prep(c, body["preparation_id"])
        if prep["body"].get("candidate_publication_ref") != publication["ref"]:
            raise BusinessError("PUBLICATION_SUPERSEDED")
        for key in ("draft_ref", "compiled_plan_ref", "requirements_ref", "audio_manifest_ref", "playlist_ref"):
            if body[key] != prep["body"][key]:
                raise BusinessError("PUBLICATION_STALE")
        self._approval(c, prep["body"], body["approval_ref"])
        self._manifest(c, prep["body"])
        self._assert_current_job_outcomes_known(c, prep["body"])
        current = self._currentness(c, prep["body"], choices_required=True)
        room = self.store.get(c, "config", body["room_id"])
        result = {"publication_ref": publication["ref"], "preparation_ref": prep["ref"], "room_ref": room["ref"], "publication": body, "currentness": current, "adopted": room["body"].get("adopted_publication_ref") == publication["ref"]}
        # prep bookkeeping revisions do not change the immutable reviewed package.
        result["review_hash"] = digest({"publication_ref": publication["ref"], "room_ref": room["ref"], "currentness": current})
        return result

    def _cmd_adopt_snapshot(self, c, a, m, p, s):
        review = self._publication_review(c, a.publication_ref)
        if review["currentness"]["blockers"]:
            raise BusinessError("CURRENTNESS_BLOCKED", details=review["currentness"])
        if review["review_hash"] != a.review_hash:
            raise BusinessError("REVIEW_STALE")
        publication = review["publication"]
        room = self.store.get(c, "config", publication["room_id"])
        if room["ref"]["revision"] != a.expected_room_revision:
            raise BusinessError("REVISION_CONFLICT")
        if c.execute("SELECT 1 FROM prelive_live_slot WHERE slot=1 AND state IN ('starting','live','unknown')").fetchone():
            raise BusinessError("ROOM_IS_LIVE", "请先结束当前直播再采用新配置")
        draft = self._artifact(c, publication["draft_ref"])["body"]["draft"]
        playlist = self._artifact(c, publication["playlist_ref"])["body"]
        body = deepcopy(room["body"])
        body.update(product_ids=[r["id"] for r in publication["product_refs"]], commerce=draft["commerce"], adopted_publication_ref=wire(a.publication_ref), adopted_preparation_id=publication["preparation_id"], plan_mode="all", loop=True, script_order=[i["script_revision_ref"]["id"] for i in playlist["items"]], selected_script_ids=[i["script_revision_ref"]["id"] for i in playlist["items"]], tts_provider=draft["tts_selection"]["provider"], tts_voice=draft["tts_selection"]["voice"], tts_model=draft["tts_selection"].get("model", "speech-2.8-turbo"))
        room_ref = self.store.put(c, "config", room["ref"]["id"], body, expected=a.expected_room_revision)
        adoption_ref = self.store.artifact(c, "adoption", {"publication_ref": wire(a.publication_ref), "room_ref": room_ref, "human_actor": p.actor_id, "review_hash": a.review_hash, "revision_choice_refs": review["currentness"]["revision_choice_refs"], "operation_id": m.operation_id})
        new_review = self._publication_review(c, a.publication_ref)
        return {"adoption_ref": adoption_ref, "room_ref": room_ref, "publication_ref": wire(a.publication_ref), "review_hash": new_review["review_hash"], "started": False}

    def _cmd_start_prepared_room(self, c, a, m, p, s):
        raise BusinessError("ASYNC_START_REQUIRED")

    async def _start(self, a, m, p, s):
        if p.kind != "human":
            raise BusinessError("HUMAN_REQUIRED", status=403)
        import json
        request_hash = digest({"command": "start_prepared_room", "args": wire(a), "scope": wire(s)})
        async with self._start_lock:
            with self.store.transaction(write=True) as c:
                previous = c.execute("SELECT request_hash,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?", (p.actor_id, m.operation_id)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise BusinessError("IDEMPOTENCY_KEY_REUSED")
                    return json.loads(previous["response_json"])
                from .voice_preview import read_preview_receipt
                if read_preview_receipt(c, p.actor_id, m.operation_id) is not None:
                    raise BusinessError("IDEMPOTENCY_KEY_REUSED", "操作编号已用于试听，请使用本操作的独立编号")
                self._assert_scope(c, p, s)
                review = self._publication_review(c, a.publication_ref)
                if review["currentness"]["blockers"] or not review["adopted"]:
                    raise BusinessError("ADOPTED_VALID_PUBLICATION_REQUIRED", details=review["currentness"])
                if a.review_hash != review["review_hash"] or a.expected_room_revision != review["room_ref"]["revision"]:
                    raise BusinessError("REVIEW_STALE")
                slot = c.execute("SELECT * FROM prelive_live_slot WHERE slot=1").fetchone()
                if slot and slot["state"] in {"starting", "live", "unknown"}:
                    raise BusinessError("LIVE_SLOT_BUSY", "直播启动或运行中，不能重复开播")
                publication = review["publication"]
                plan = self._artifact(c, publication["compiled_plan_ref"])["body"]
                manifest = self._artifact(c, publication["audio_manifest_ref"])["body"]
                assets = {x["requirement_id"]: x for x in manifest["assets"]}
                entries = deepcopy(plan["entries"])
                for entry in entries:
                    for sentence in entry["sentences"]:
                        sentence["audio"] = assets[sentence["requirement_id"]]
                c.execute("INSERT OR REPLACE INTO prelive_live_slot VALUES(1,?,?,?,?)", (publication["room_id"], a.publication_ref.id, "starting", m.operation_id))
                prior_start = c.execute("SELECT revision FROM records WHERE kind='playback_start' AND id=?", (m.operation_id,)).fetchone()
                self.store.put(c, "playback_start", m.operation_id,
                               {"operation_id": m.operation_id, "owner_id": self.playback_owner_id,
                                "room_id": publication["room_id"], "publication_id": a.publication_ref.id,
                                "created_at": now()}, expected=prior_start["revision"] if prior_start else None,
                               create=prior_start is None)
            try:
                result = await self.adapter.start_publication(publication["room_id"], {**publication, "publication_ref": wire(a.publication_ref), "action_id": a.action_id}, entries)
                if not isinstance(result, dict) or not result.get("session_id"):
                    raise BusinessError("INVALID_PLAYBACK_RECEIPT")
            except BaseException as e:
                with self.store.transaction(write=True) as c:
                    known_not_started = isinstance(e, BusinessError) and e.code in {"PLAYBACK_NOT_CONFIGURED", "LIVE_SLOT_BUSY", "EMPTY_COMPILED_PUBLICATION", "COMPILED_PRODUCT_MISMATCH", "INVALID_PRECOMPILED_AUDIO", "PRECOMPILED_AUDIO_CHANGED", "INVALID_IDENTIFIER", "PLAYBACK_ACTION_REUSED"}
                    c.execute("UPDATE prelive_live_slot SET state=? WHERE slot=1 AND start_operation_id=?", ("stopped" if known_not_started else "unknown", m.operation_id))
                    self.store.event(c, publication["preparation_id"], "playback.start_unknown", {"operation_id": m.operation_id, "code": e.code if isinstance(e, BusinessError) else "START_OUTCOME_UNKNOWN"})
                if isinstance(e, (asyncio.CancelledError, BusinessError)):
                    raise
                raise BusinessError("START_OUTCOME_UNKNOWN", "开播结果未知，请检查播放器状态后再操作") from None
            with self.store.transaction(write=True) as c:
                c.execute("UPDATE prelive_live_slot SET state='live' WHERE slot=1 AND start_operation_id=?", (m.operation_id,))
                session_ref = self.store.put(c, "prelive_session", result["session_id"], {"id": result["session_id"], "room_id": publication["room_id"], "publication_ref": wire(a.publication_ref), "compiled_plan_ref": publication["compiled_plan_ref"], "state": "live", "started_at": now(), "start_operation_id": m.operation_id, "revision_choice_refs": review["currentness"]["revision_choice_refs"]}, create=True)
                cursor = self.store.event(c, publication["preparation_id"], "playback.started", result)
                receipt = {"schema_version": "PRELIVE-2.3", "command": "start_prepared_room", "operation_id": m.operation_id, "status": "committed", "data": {**result, "session_ref": session_ref, "publication_ref": wire(a.publication_ref), "revision_choice_refs": review["currentness"]["revision_choice_refs"]}, "changed_paths": [], "invalidated_refs": [], "event_cursor": cursor}
                c.execute("INSERT INTO prelive_operations VALUES(?,?,?,?,?,?)", (p.actor_id, m.operation_id, "start_prepared_room", request_hash, canonical(receipt), now()))
                return receipt

    def begin_playback_owner(self, owner_id, *, exclusive_lease=False):
        """Reconcile only starts proven to belong to the exited exclusive owner.

        A legacy/foreign/unknown slot without matching owner evidence remains
        blocked and is returned for explicit human stop; it is never blanket reset.
        This method is called only after the server holds its lifetime OS lock.
        """
        if not exclusive_lease or not owner_id:
            raise BusinessError("PLAYBACK_OWNER_LEASE_REQUIRED")
        with self.store.transaction(write=True) as c:
            previous = c.execute("SELECT 1 FROM records WHERE kind='playback_owner' AND id='local'").fetchone()
            old = self.store.get(c, "playback_owner", "local") if previous else None
            slot = c.execute("SELECT * FROM prelive_live_slot WHERE slot=1").fetchone()
            recovery = None
            if slot and slot["state"] in {"starting", "live", "unknown"}:
                start_row = c.execute("SELECT 1 FROM records WHERE kind='playback_start' AND id=?", (slot["start_operation_id"],)).fetchone()
                start = self.store.get(c, "playback_start", slot["start_operation_id"])["body"] if start_row else None
                proven = (old and old["body"].get("owner_id") and old["body"]["owner_id"] != owner_id and start
                          and start.get("owner_id") == old["body"].get("owner_id")
                          and start.get("room_id") == slot["room_id"]
                          and start.get("publication_id") == slot["publication_id"])
                if proven:
                    c.execute("UPDATE prelive_live_slot SET state='stopped' WHERE slot=1 AND start_operation_id=?", (slot["start_operation_id"],))
                    for session in self.store.all(c, "prelive_session"):
                        if session["body"].get("start_operation_id") == slot["start_operation_id"] and session["body"].get("state") == "live":
                            body = {**session["body"], "state": "interrupted", "stopped_at": now(), "stop_reason": "playback_owner_exited"}
                            self.store.put(c, "prelive_session", session["ref"]["id"], body, expected=session["ref"]["revision"])
                    self.store.event(c, slot["room_id"], "playback.owner_recovered", {"room_id": slot["room_id"], "start_operation_id": slot["start_operation_id"], "reason": "exclusive_owner_exited"})
                else:
                    recovery = {"room_id": slot["room_id"], "state": slot["state"], "start_operation_id": slot["start_operation_id"]}
            self.store.put(c, "playback_owner", "local", {"owner_id": owner_id, "started_at": now()},
                           expected=old["ref"]["revision"] if old else None, create=old is None)
        self.playback_owner_id = owner_id
        return recovery

    def release_live_slot(self, room_id):
        """Trusted player callback, after it has actually stopped playback."""
        with self.store.transaction(write=True) as c:
            c.execute("UPDATE prelive_live_slot SET state='stopped' WHERE slot=1 AND room_id=?", (room_id,))
            for session in self.store.all(c, "prelive_session"):
                if session["body"]["room_id"] == room_id and session["body"]["state"] == "live":
                    body = deepcopy(session["body"])
                    body.update(state="stopped", stopped_at=now())
                    self.store.put(c, "prelive_session", session["ref"]["id"], body, expected=session["ref"]["revision"])

    def assert_live_product_control(self, product_id, pinned_ref=None):
        """Trusted player checks only hard controls, never ordinary head edits."""
        with self.store.transaction() as c:
            head = self.store.get(c, "product", product_id)["body"]
            if head.get("archived") or head.get("sale_status", "available") != "available" or head.get("withdrawn") or (pinned_ref and pinned_ref["revision"] in head.get("withdrawn_revisions", [])):
                raise BusinessError("PRODUCT_WITHDRAWN", "商品已停售或撤回，已停止继续播报")
            try:
                control = self.store.get(c, "product_control", product_id)["body"]
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
                return
            if control["sale_blocked"] or control["withdrawn_paths"] or (pinned_ref and pinned_ref["revision"] in control["withdrawn_revisions"]):
                raise BusinessError("PRODUCT_EXPLICITLY_WITHDRAWN", "商品资料已明确撤回，已停止继续播报")

    def _cmd_undo_mutation(self, c, a, m, p, s):
        import json
        row = c.execute("SELECT * FROM prelive_changes WHERE id=?", (a.change_id,)).fetchone()
        if not row:
            raise BusinessError("CHANGE_NOT_FOUND", status=404)
        if row["kind"] != "product":
            raise BusinessError("UNDO_KIND_NOT_SUPPORTED", "首期仅允许安全补偿商品编辑")
        old = json.loads(row["before_json"])
        after = json.loads(row["after_ref"])
        current = self.store.get(c, row["kind"], row["object_id"])
        if current["ref"] != after or a.expected_revision != after["revision"]:
            raise BusinessError("UNDO_PRECONDITION_CHANGED")
        sku = old.get("sku_id") or row["object_id"]
        collision = c.execute("SELECT product_id FROM prelive_skus WHERE sku_id=?", (sku,)).fetchone()
        if collision and collision[0] != row["object_id"]:
            raise BusinessError("UNDO_SKU_CONFLICT")
        if any(r["ref"]["id"] != row["object_id"] and (r["body"].get("sku_id") or r["ref"]["id"]) == sku for r in self.store.all(c, "product")):
            raise BusinessError("UNDO_SKU_CONFLICT")
        c.execute("DELETE FROM prelive_skus WHERE product_id=?", (row["object_id"],))
        c.execute("INSERT INTO prelive_skus VALUES(?,?)", (sku, row["object_id"]))
        ref = self.store.put(c, row["kind"], row["object_id"], old, expected=a.expected_revision)
        return {"compensation_ref": ref, "source_change_id": a.change_id}

    def _prepare_blob_file(self, a):
        started = perf_counter_ns()
        spreadsheet_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        allowed = {"text/plain", "text/markdown", "text/csv", "application/json", "application/pdf",
            "image/png", "image/jpeg", "image/webp", "application/msword", "application/rtf", "text/rtf",
            spreadsheet_mime, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        if a.mime not in allowed:
            raise BusinessError("BLOB_MIME_NOT_ALLOWED")
        limit = (128 if a.mime == spreadsheet_mime else 20) * 1024 * 1024
        # Reject before allocating decoded bytes, and retain the exact decoded
        # length check for padding/invalid input at the boundary.
        if len(a.content_base64) > 4 * ((limit + 2) // 3):
            raise BusinessError("BLOB_TOO_LARGE", status=413, details={"limit_bytes": limit})
        try:
            raw = base64.b64decode(a.content_base64, validate=True)
        except ValueError:
            raise BusinessError("INVALID_BASE64", status=400) from None
        decoded = perf_counter_ns()
        if len(raw) > limit:
            raise BusinessError("BLOB_TOO_LARGE", status=413)
        actual = hashlib.sha256(raw).hexdigest()
        hashed = perf_counter_ns()
        if a.expected_sha256.removeprefix("sha256:") != actual:
            raise BusinessError("BLOB_HASH_MISMATCH")
        # Content-addressed bytes; no arbitrary path or URL is accepted.
        directory = self.runtime / "prelive_blobs"
        directory.mkdir(exist_ok=True)
        target = directory / actual
        reused = target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == actual
        if not reused:
            pending = directory / (actual + "." + uid("tmp"))
            pending.write_bytes(raw)
            pending.replace(target)
        finished = perf_counter_ns()
        with self.store.transaction(write=True) as c:
            self.store.event(c, "catalog", "material.blob_staged", {"sha256_bytes": "sha256:" + actual,
                "mime": a.mime, "size_bytes": len(raw), "reused": reused,
                "base64_decode_ms": round((decoded - started) / 1_000_000, 3),
                "sha256_ms": round((hashed - decoded) / 1_000_000, 3),
                "verify_and_write_ms": round((finished - hashed) / 1_000_000, 3),
                "elapsed_ms": round((finished - started) / 1_000_000, 3)})
        return actual

    def _cmd_import_blob(self, c, a, m, p, s):
        actual = a.expected_sha256.removeprefix("sha256:")
        if not re.fullmatch("[a-f0-9]{64}", actual):
            raise BusinessError("BLOB_HASH_MISMATCH")
        target = self.runtime / "prelive_blobs" / actual
        if not target.is_file():
            raise BusinessError("BLOB_NOT_STAGED")
        body = {"sha256_bytes": "sha256:" + actual, "relative_path": "prelive_blobs/" + actual, "size": target.stat().st_size, "mime": a.mime, "filename": Path(a.filename).name}
        return {"blob_ref": self.store.artifact(c, "blob", body)}

    def _cmd_import_recap(self, c, a, m, p, s):
        blob = self._artifact(c, a.raw_blob_ref, "blob")
        origin = a.document.get("origin", {})
        if a.document.get("synthetic", a.synthetic) != a.synthetic or origin.get("synthetic", a.synthetic) != a.synthetic:
            raise BusinessError("RECAP_SYNTHETIC_MISMATCH")
        findings = a.document.get("findings", [])
        ids = [f.get("finding_id") or f.get("id") for f in findings]
        if not findings or any(not x for x in ids) or len(ids) != len(set(ids)):
            raise BusinessError("INVALID_RECAP_FINDINGS")
        if a.synthetic and (a.document.get("live_session_id") or origin.get("previous_live_session_id") or origin.get("previous_publication_ref")):
            raise BusinessError("SYNTHETIC_RECAP_HAS_REAL_SESSION")
        def contains_fixture(value):
            if isinstance(value, dict):
                return "fixture_ref" in value or any(contains_fixture(v) for v in value.values())
            return isinstance(value, list) and any(contains_fixture(v) for v in value)
        if contains_fixture(a.document):
            raise BusinessError("UNBOUND_FIXTURE_REF", "模拟资料必须先绑定隔离测试对象的精确引用")
        raw = (self.runtime / blob["body"]["relative_path"]).read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != blob["body"]["sha256_bytes"]:
            raise BusinessError("BLOB_HASH_MISMATCH")
        raw_text = raw.decode("utf-8") if blob["body"]["mime"] in {"text/plain", "text/markdown", "application/json"} else None
        evidence_ids = set()
        for evidence in a.document.get("evidence", []):
            evidence_id = evidence.get("evidence_id")
            if not evidence_id or evidence_id in evidence_ids:
                raise BusinessError("INVALID_RECAP_EVIDENCE")
            evidence_ids.add(evidence_id)
            quote = evidence.get("quote")
            expected_hash = evidence.get("content_hash")
            if quote and expected_hash and expected_hash.removeprefix("sha256:") != hashlib.sha256(quote.encode()).hexdigest():
                raise BusinessError("RECAP_QUOTE_HASH_MISMATCH")
            if quote and raw_text is not None and quote not in raw_text:
                raise BusinessError("RECAP_QUOTE_NOT_IN_SOURCE")
            if a.synthetic and evidence.get("synthetic", True) is False:
                raise BusinessError("RECAP_SYNTHETIC_MISMATCH")
        body = {"document": a.document, "raw_blob_ref": wire(a.raw_blob_ref), "synthetic": a.synthetic, "raw_hash": blob["body"]["sha256_bytes"]}
        return {"recap_ref": self.store.artifact(c, "recap", body), "synthetic": a.synthetic}

    def _cmd_link_recap_to_preparation(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        for ref in a.recap_refs:
            self._artifact(c, ref, "recap")
        body = deepcopy(prep["body"])
        body.update(recap_refs=[wire(r) for r in a.recap_refs], bundle_ref=None, plan_ref=None)
        return self._save_prep(c, prep, body)

    def _cmd_save_asset_checklist(self, c, a, m, p, s):
        prep = self._prep(c, a.preparation_id, a.expected_revision)
        body = wire(a.checklist)
        if len({i["item_id"] for i in body["items"]}) != len(body["items"]):
            raise BusinessError("DUPLICATE_ASSET_ITEM")
        for item in body.get("items", []):
            if item.get("product_ref") and item["product_ref"] not in prep["body"]["selected_product_refs"]:
                raise BusinessError("ASSET_PRODUCT_NOT_SELECTED")
            for source_ref in item.get("source_refs", []):
                self._artifact(c, source_ref, preparation_id=a.preparation_id)
            ref = item.get("blob_ref")
            if item.get("status") == "available":
                if not ref:
                    raise BusinessError("ASSET_FILE_REQUIRED")
                blob = self._artifact(c, ref, "blob")["body"]
                path = self.runtime / blob["relative_path"]
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != blob["sha256_bytes"].split(":")[-1]:
                    raise BusinessError("ASSET_FILE_MISSING_OR_CORRUPT")
            elif ref:
                self._artifact(c, ref, "blob")
        ref = self.store.artifact(c, "asset_checklist", {"preparation_id": a.preparation_id, "checklist": body, "visual_assets_required_for_sound": False})
        prep_body = deepcopy(prep["body"])
        prep_body["asset_checklist_ref"] = ref
        return {**self._save_prep(c, prep, prep_body, invalidate=False), "asset_checklist_ref": ref}

    def _job_fence(self, c, job):
        prep = self._prep(c, job["preparation_id"])
        if job.get("cancel_requested"):
            raise BusinessError("JOB_CANCELLED")
        if prep["body"]["generation_epoch"] != job["generation_epoch"]:
            raise BusinessError("GENERATION_EPOCH_CHANGED")
        execution = job.get("archive_revalidation_execution")
        if execution:
            if (job["kind"] != "scripts" or execution.get("original_run_id") != job["run_id"]
                    or execution.get("scope") != job["scope"]
                    or any(item.get("recovery_mode") != "revalidate_archived" for item in job["items"]
                        if item["status"] in {"queued", "running"})):
                raise BusinessError("RETRY_SCOPE_DENIED", status=403)
            principal = Principal.model_validate(execution["principal"])
            mutation = MutationContext.model_validate(execution["mutation"])
            scope = WorkScope.model_validate(execution["scope"])
            if principal.run_id != execution.get("requesting_run_id"):
                raise BusinessError("RETRY_SCOPE_DENIED", status=403)
            self._grant(c, "retry_failed_items", mutation, principal, scope)
        else:
            principal = Principal.model_validate(job["principal"])
            mutation = MutationContext.model_validate(job["mutation"])
            scope = WorkScope.model_validate(job["scope"]) if job["scope"] else None
        command = "submit_script_change" if job["kind"] == "scripts" else "preview_audio" if job["kind"] == "preview" else "prepare_required_audio"
        self._grant(c, command, mutation, principal, scope)
        if job["kind"] in {"audio", "preview"}:
            self._approval(c, prep["body"], job["approval_ref"], job["review_hash"])
            if job.get("audio_reissue_approval_ref"):
                from .audio_reissue import validate_job
                validate_job(self, c, prep["body"], job)
        elif job["plan_ref"] != prep["body"]["plan_ref"] or job["context_ref"] != prep["body"]["bundle_ref"]:
            raise BusinessError("GENERATION_INPUTS_CHANGED")
        return prep

    def wake_worker(self):
        if self._background is None or self._background.done():
            self._background = asyncio.create_task(self.drain_jobs(), name="prelive-business-worker")

    async def close(self):
        if self._background and not self._background.done():
            self._background.cancel()
            try:
                await self._background
            except asyncio.CancelledError:
                pass

    async def drain_jobs(self):
        async with self._worker_lock:
            while True:
                recovered = False
                with self.store.transaction(write=True) as c:
                    jobs = [j for j in self.store.all(c, "prelive_job") if j["body"]["status"] in {"queued", "running"}]
                    claim = None
                    for job in jobs:
                        body = deepcopy(job["body"])
                        # Only a sent request lacking a durable response is
                        # unknown. Received responses and committed modules can
                        # resume local validation/assembly without another call.
                        running = [i for i in body["items"] if i["status"] == "running"]
                        if running:
                            for item in running:
                                if item.get("call_state") == "sent":
                                    item.update(status="interrupted", call_state="timeout_unknown", error={"code": "EXTERNAL_OUTCOME_UNKNOWN"})
                                    if item.get("provider_attempts"):
                                        item["provider_attempts"][-1]["state"] = "timeout_unknown"
                                else:
                                    item.update(status="queued", error=None)
                            body["status"] = "queued" if any(i["status"] == "queued" for i in body["items"]) else "interrupted"
                            self.store.put(c, "prelive_job", job["ref"]["id"], body, expected=job["ref"]["revision"])
                            recovered = body["status"] == "queued" or recovered
                            continue
                        queued_items = [i for i in body["items"] if i["status"] == "queued"]
                        if not queued_items:
                            continue
                        try:
                            self._job_fence(c, body)
                        except BusinessError as e:
                            body["status"] = "superseded" if e.code != "JOB_CANCELLED" else "cancelled"
                            for item in body["items"]:
                                if item["status"] == "queued":
                                    item.update(status=body["status"], error=e.payload())
                            self.store.put(c, "prelive_job", job["ref"]["id"], body, expected=job["ref"]["revision"])
                            continue
                        # One coordinator owns a bounded batch. Never scan for
                        # restart recovery while any of its children are live:
                        # that scan deliberately treats persisted running work
                        # as abandoned. Scripts, previews and local COM stay
                        # serial; only formal MiniMax sentence audio overlaps.
                        if (body["kind"] == "audio"
                                and not body.get("audio_reissue_approval_ref")
                                and queued_items[0]["input"].get("voice", {}).get("provider") == "minimax"):
                            queued_items = [i for i in queued_items
                                if i["input"].get("voice", {}).get("provider") == "minimax"][:self.minimax_audio_concurrency]
                        else:
                            queued_items = queued_items[:1]
                        for queued in queued_items:
                            # Keep local validation out of generation budgets.
                            generation_attempts = int(queued.get("generation_attempts", queued["attempt"]))
                            local_attempts = int(queued.get("local_revalidation_attempts", 0))
                            local_only = queued.get("recovery_mode") == "revalidate_archived"
                            queued.update(status="running", attempt=queued["attempt"] + 1, call_state="not_sent",
                                generation_attempts=generation_attempts + (0 if local_only else 1),
                                local_revalidation_attempts=local_attempts + (1 if local_only else 0))
                        body["status"] = "running"
                        self.store.put(c, "prelive_job", job["ref"]["id"], body, expected=job["ref"]["revision"])
                        self.store.event(c, body["preparation_id"], "job.batch_claimed", {
                            "job_id": job["ref"]["id"], "kind": body["kind"],
                            "item_ids": [i["item_id"] for i in queued_items],
                            "concurrency": len(queued_items)})
                        claim = (body, deepcopy(queued_items))
                        break
                if claim is None:
                    if recovered:
                        continue
                    break
                job, items = claim
                children = [asyncio.create_task(self._run_claimed_job_item(job, item),
                    name="prelive-item-" + item["item_id"]) for item in items]
                try:
                    await asyncio.gather(*children)
                except BaseException:
                    # Await every child's durable failure/unknown write before
                    # releasing the sole coordinator or returning from close().
                    for child in children:
                        # Cancelling the gather already cancels its children.
                        # A second cancel here could interrupt their awaited
                        # durable _fail_job_item write during cancellation.
                        if not child.done() and not child.cancelling():
                            child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                    raise

    async def _run_claimed_job_item(self, job, item):
        output = None
        try:
            if job["kind"] == "scripts":
                output = await self._produce_script(job, item)
            else:
                output = await self._produce_audio(job, item)
            await asyncio.to_thread(self._commit_job_item, job["id"], item["item_id"], output)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._fail_job_item, job["id"], item["item_id"], BusinessError("JOB_INTERRUPTED"), True)
            raise
        except BaseException as e:
            error = e if isinstance(e, BusinessError) else BusinessError("PROVIDER_OPERATION_FAILED", "供应商调用失败，已完成内容保留")
            unknown = isinstance(e, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)) or (isinstance(e, BusinessError) and any(k in e.code for k in ("TIMEOUT", "NETWORK", "CONNECTION", "OUTCOME_UNKNOWN")))
            await asyncio.to_thread(self._fail_job_item, job["id"], item["item_id"], error, unknown, output)

    def _before_provider(self, job_id, item_id, purpose="provider", public_input=None):
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            prep = self._job_fence(c, job["body"])
            supplied = public_input or {}
            request_identity = supplied.get("dependency_hash") or (supplied.get("requirement_id") if purpose == "tts" else None)
            request_key = digest({"purpose": purpose, "identity": request_identity}) if request_identity else None
            indexed = None
            reissue = None
            if job["body"].get("audio_reissue_approval_ref"):
                if purpose != "tts":
                    raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
                from .audio_reissue import provider_slot
                reissue = provider_slot(self, c, prep["body"], job["body"], item_id, supplied)
                request_key = reissue["request_key"]
            elif request_key:
                try:
                    indexed = self.store.get(c, "prelive_external_request", request_key)
                except BusinessError as error:
                    if error.code != "NOT_FOUND":
                        raise
                if indexed and indexed["body"]["state"] in {"sent", "timeout_unknown", "outcome_unknown"}:
                    raise BusinessError("EXTERNAL_OUTCOME_UNKNOWN", "相同输入已有未核实的供应商请求，禁止换操作号重复计费",
                        details={"prior_job_id": indexed["body"]["job_id"], "prior_item_id": indexed["body"]["item_id"],
                                 "prior_attempt_id": indexed["body"]["attempt_id"]})
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            if item["status"] != "running":
                raise BusinessError("JOB_LEASE_LOST")
            item["call_state"] = "sent"
            item["sent_at"] = now()
            attempt_id = uid("provider_attempt")
            attempt = {"attempt_id": attempt_id, "purpose": purpose, "state": "sent", "sent_at": item["sent_at"],
                "input": supplied, "input_hash": digest(supplied), "usage": {}, "usage_state": "unknown", "request_key": request_key}
            if reissue:
                from .audio_reissue import consume_slot
                consume_slot(self, c, reissue, attempt_id)
                attempt.update({key: value for key, value in reissue.items() if key != "slot_ref"})
            if request_key:
                self.store.put(c, "prelive_external_request", request_key, {"job_id": job_id, "item_id": item_id,
                    "attempt_id": attempt_id, "state": "sent", "sent_at": item["sent_at"]},
                    expected=indexed["ref"]["revision"] if indexed else None, create=indexed is None)
            item.setdefault("provider_attempts", []).append(attempt)
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "provider.call_started", {"job_id": job_id, "item_id": item_id, **attempt})
            return attempt_id

    def _record_request_outcome(self, c, attempt, state):
        if not attempt.get("request_key"):
            return
        record = self.store.get(c, "prelive_external_request", attempt["request_key"])
        if record["body"]["attempt_id"] != attempt["attempt_id"]:
            raise BusinessError("PROVIDER_ATTEMPT_CONFLICT")
        self.store.put(c, "prelive_external_request", attempt["request_key"],
            {**record["body"], "state": state, "updated_at": now()}, expected=record["ref"]["revision"])

    def _apply_tts_observation(self, c, job, item, attempt, observation):
        """A scalar response observation is usage evidence, never audio success."""
        if not isinstance(observation, dict) or observation.get("provider") != "minimax" or attempt.get("purpose") != "tts":
            return False
        from ..speech import tts_diagnostics
        safe = tts_diagnostics(observation.get("stage"), observation.get("outcome"),
            response_received=observation.get("response_received") is True,
            http_status=observation.get("http_status"), provider_code=observation.get("provider_code"), metadata=observation)
        name = observation.get("exception_type")
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
            safe["exception_type"] = name
        if safe.get("trace_id", "").lower().startswith(("sk-", "bearer")):
            safe.pop("trace_id", None)
        if attempt.get("response_diagnostics") == safe:
            return False
        attempt["response_diagnostics"] = safe
        attempt.setdefault("response_observations", []).append({"observed_at": now(), **safe})
        if "usage_characters" in safe:
            attempt["usage"] = {**attempt.get("usage", {}), "usage_characters": safe["usage_characters"]}
            attempt["usage_state"] = "known"
        self.store.event(c, job["preparation_id"], "provider.audio_observed", {"job_id": job["id"],
            "item_id": item["item_id"], "attempt_id": attempt["attempt_id"], "diagnostics": safe,
            "usage": attempt.get("usage", {}), "usage_state": attempt.get("usage_state", "unknown")})
        return True

    def _record_tts_observation(self, job_id, item_id, attempt_id, observation):
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(row for row in body["items"] if row["item_id"] == item_id)
            attempt = next(row for row in item.get("provider_attempts", []) if row["attempt_id"] == attempt_id)
            if self._apply_tts_observation(c, body, item, attempt, observation):
                self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])

    def _after_provider(self, job_id, item_id, attempt_id, public_output):
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            attempt = next(a for a in item["provider_attempts"] if a["attempt_id"] == attempt_id)
            attempt.update(state="result_received", received_at=now(), output=public_output, output_hash=digest(public_output))
            self._record_request_outcome(c, attempt, "result_received")
            item["call_state"] = "result_received"
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "provider.call_result", {"job_id": job_id, "item_id": item_id, "attempt_id": attempt_id, "purpose": attempt["purpose"], "output": public_output, "output_hash": attempt["output_hash"]})

    def _archive_provider_response(self, job_id, item_id, attempt_id, receipt):
        """First durable boundary: received/charged is independent of valid copy."""
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            attempt = next(a for a in item["provider_attempts"] if a["attempt_id"] == attempt_id)
            response_ref = self.store.artifact(c, "provider_response", {
                "job_id": job_id, "item_id": item_id, "attempt_id": attempt_id,
                "received_at": now(), "response": receipt})
            attempt.update(state="response_received", received_at=now(), provider_response_ref=response_ref,
                usage=receipt.get("usage", {}), usage_state=receipt.get("usage_state", "unknown"), validation_state="pending")
            self._record_request_outcome(c, attempt, "response_received")
            item["call_state"] = "response_received"
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "provider.response_received", {
                "job_id": job_id, "item_id": item_id, "attempt_id": attempt_id,
                "purpose": attempt["purpose"], "response_ref": response_ref,
                "usage": attempt["usage"], "usage_state": attempt["usage_state"]})
        # The original public content and usage are durably committed first.
        # Audit a derived validation object separately, including when the
        # following fact/offer validation will reject it. Never rewrite archive.
        self._record_module_normalization(job_id, item_id, attempt_id)

    def _record_module_normalization(self, job_id, item_id, attempt_id):
        from .module_generation import received_module_normalization_audit
        from .whole_script_generation import received_whole_script_normalization_audit
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            attempt = next(a for a in item["provider_attempts"] if a["attempt_id"] == attempt_id)
            if (attempt.get("state") not in {"response_received", "result_received"}
                    or not attempt.get("provider_response_ref")):
                return
            archived = self.store.exact(c, attempt["provider_response_ref"])
            value = archived["body"]
            if (archived["ref"]["kind"] != "provider_response" or value.get("job_id") != job_id
                    or value.get("item_id") != item_id or value.get("attempt_id") != attempt_id):
                raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
            if value.get("response", {}).get("protocol") == "whole-script-v1":
                audit = received_whole_script_normalization_audit(value.get("response"))
            else:
                audit = received_module_normalization_audit(value.get("response"))
            if audit is None or attempt.get("normalization") == audit:
                return
            if attempt.get("normalization") is not None:
                raise BusinessError("MODULE_NORMALIZATION_CONFLICT")
            attempt["normalization"] = audit
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "provider.content_normalized", {
                "job_id": job_id, "item_id": item_id, "attempt_id": attempt_id,
                "response_ref": archived["ref"], "normalization": audit})

    def _module_checkpoint(self, job_id, item_id, dependency_hash, *, module_key=None):
        with self.store.transaction() as c:
            job = self.store.get(c, "prelive_job", job_id)["body"]
            item = next(i for i in job["items"] if i["item_id"] == item_id)
            if item.get("recovery_mode") == "revalidate_archived":
                request = next(x for x in item.get("archive_revalidations", [])
                    if x["revalidation_id"] == item["active_archive_revalidation_id"])
                key = module_key
                if key in item.get("module_results", {}) and key not in request["targets"]:
                    record = item["module_results"][key]
                    module = self.store.exact(c, record["module_ref"])
                    return {"module_ref": module["ref"], "dependency_hash": record["dependency_hash"], "reused": True}
                bound = request["targets"].get(key)
                if bound is None:
                    raise BusinessError("ARCHIVED_RESPONSE_INCOMPLETE", details={"module_key": key, "provider_reissued": False})
                attempt = next(x for x in item.get("provider_attempts", []) if x["attempt_id"] == bound["attempt_id"])
                archived = self.store.exact(c, bound["response_ref"])
                value = archived["body"]
                if (attempt.get("validation_state") != "rejected" or attempt.get("provider_response_ref") != archived["ref"]
                        or archived["ref"]["kind"] != "provider_response" or value.get("job_id") != job_id
                        or value.get("item_id") != item_id or value.get("attempt_id") != attempt["attempt_id"]
                        or attempt.get("purpose") != "copywriting." + key
                        or attempt.get("input", {}).get("dependency_hash") != bound["dependency_hash"]):
                    raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
                return {"response": value["response"], "attempt_id": attempt["attempt_id"],
                    "dependency_hash": bound["dependency_hash"], "revalidate_archived": True,
                    "committed_module_ref": item.get("module_results", {}).get(key, {}).get("module_ref")}
            try:
                record = self.store.get(c, "prelive_module_result", dependency_hash)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
            else:
                module = self.store.exact(c, record["body"]["module_ref"])
                return {"module_ref": module["ref"], "reused": True}
            for attempt in reversed(item.get("provider_attempts", [])):
                if (attempt.get("input", {}).get("dependency_hash") == dependency_hash
                        and attempt.get("provider_response_ref")
                        and attempt.get("validation_state") != "rejected"):
                    archived = self.store.exact(c, attempt["provider_response_ref"])
                    value = archived["body"]
                    if (archived["ref"]["kind"] != "provider_response" or value.get("job_id") != job_id
                            or value.get("item_id") != item_id or value.get("attempt_id") != attempt["attempt_id"]):
                        raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
                    response = value["response"]
                    return {"response": response, "attempt_id": attempt["attempt_id"], "resumed": True}
        return None

    def _record_archive_revalidation(self, job_id, item_id, target, checkpoint, *, status="validating", check_id=None,
                                     current_generator_revision=None, error=None, committed_refs=None, validation_details=None):
        """Audit derived local validation; the original rejected attempt stays immutable."""
        from . import module_generation, whole_script_generation
        from .. import copywriting_quality
        validator_revision = digest({"schema": "archive-validator-v1", "files": {
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (module_generation, whole_script_generation, copywriting_quality)}})
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            request = next(x for x in item["archive_revalidations"]
                if x["revalidation_id"] == item["active_archive_revalidation_id"])
            bound = request["targets"][target]
            if checkpoint["attempt_id"] != bound["attempt_id"] or checkpoint["dependency_hash"] != bound["dependency_hash"]:
                raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
            if check_id is None:
                self._job_fence(c, body)
                check = {"check_id": uid("archive_check"), "target": target, **deepcopy(bound),
                    "original_generator_revision": checkpoint["response"].get("prompt_revision"),
                    "current_generator_revision": current_generator_revision, "validator_revision": validator_revision,
                    "started_at": now(), "provider_reissued": False}
                request["checks"].append(check)
            else:
                check = next(x for x in request["checks"] if x["check_id"] == check_id)
            check["status"] = status
            check["validation_status"] = "pending" if status == "validating" else "rejected" if status == "rejected" else "accepted"
            check["commit_status"] = "committed" if status == "modules_committed" else "failed" if status == "commit_failed" else "not_committed"
            if status != "validating":
                check["completed_at"] = now()
            if error is not None:
                check["error"] = error.payload() if isinstance(error, BusinessError) else {"code": "LOCAL_REVALIDATION_FAILED"}
            if committed_refs is not None:
                check["committed_refs"] = deepcopy(committed_refs)
            if validation_details is not None:
                check["validation_details"] = deepcopy(validation_details)
            request["status"] = "failed" if status == "commit_failed" else "rejected" if status == "rejected" else "modules_committed" if status == "modules_committed" else "running"
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "provider.archive_revalidation", {
                "job_id": job_id, "item_id": item_id, "revalidation_id": request["revalidation_id"], **deepcopy(check)})
            return check["check_id"]

    def _commit_module_result(self, job_id, item_id, key, dependency_hash, module, attempt_id=None, reused_ref=None):
        """Commit valid siblings even when another module later fails.

        This does not select a partial script in a preparation, approve it or
        generate audio. Whole-script assembly remains an atomic later step.
        """
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            self._job_fence(c, body)
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            if item["status"] != "running":
                raise BusinessError("JOB_LEASE_LOST")
            module_ref = self.store.exact(c, reused_ref)["ref"] if reused_ref else self.store.artifact(c, "module", module)
            record = {"module_key": key, "dependency_hash": dependency_hash, "module_ref": module_ref,
                "status": "committed", "job_id": job_id, "item_id": item_id, "committed_at": now()}
            try:
                saved = self.store.get(c, "prelive_module_result", dependency_hash)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
                self.store.put(c, "prelive_module_result", dependency_hash, record, create=True)
            else:
                if saved["body"]["module_ref"] != module_ref:
                    raise BusinessError("MODULE_RESULT_CONFLICT")
            item.setdefault("module_results", {})[key] = record
            item["call_state"] = "result_received"
            if attempt_id:
                attempt = next(a for a in item["provider_attempts"] if a["attempt_id"] == attempt_id)
                attempt.update(validation_state="accepted", committed_module_ref=module_ref)
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "script.module_committed", {
                "job_id": job_id, "item_id": item_id, "module_key": key,
                "module_ref": module_ref, "dependency_hash": dependency_hash, "reused": bool(reused_ref)})
            return module_ref

    def _module_body(self, key, sentences, parent=None, details=None):
        if not isinstance(sentences, list) or not sentences or any(not isinstance(t, str) or not t.strip() or len(t) > 2500 for t in sentences):
            raise BusinessError("INVALID_MODULE_TEXT", "模型未返回有效逐句话术")
        if len(sentences) != len(set(sentences)):
            raise BusinessError("DUPLICATE_MODULE_SENTENCE", "同一模块出现完全重复句子，请局部修改")
        rows = [{"id": "sentence_" + digest({"key": key, "index": i, "text": t.strip()}).split(":")[-1][:24], "text": t.strip(), "spoken_text": t.strip()} for i, t in enumerate(sentences)]
        if details is not None:
            if not isinstance(details, list) or len(details) != len(rows):
                raise BusinessError("MODULE_DETAILS_MISMATCH")
            for row, detail in zip(rows, details):
                if not isinstance(detail, dict) or detail.get("spoken_text") != row["spoken_text"]:
                    raise BusinessError("MODULE_DETAILS_MISMATCH")
                row.update(text=detail["display_text"], kind=detail["kind"], fact_ids=deepcopy(detail["fact_ids"]))
        return {"module_key": key, "parent_ref": parent, "sentences": rows}

    def _script_content_diagnostics(self, c, module_refs):
        """Observable editing hints, not a length gate or another model pass."""
        from .script_duration import length_goal, script_duration_metrics
        by_opening = {}
        for opening in ("opening_long", "opening_short"):
            if not module_refs.get(opening):
                continue
            seen, duplicate_groups, neighbour_references = {}, [], []
            characters, sentences, selected_modules = 0, 0, {}
            for key in (opening, "product_intro", "selling_points", "campaign", "cta"):
                if not module_refs.get(key):
                    continue
                body = self._artifact(c, module_refs[key], "module")["body"]
                selected_modules[key] = body
                for sentence in body["sentences"]:
                    text = sentence.get("spoken_text") or sentence["text"]
                    characters += len(text)
                    sentences += 1
                    seen.setdefault(text.strip(), []).append(key)
                    if re.search(r"上一款|下一款|刚才(的|那款)|下一个商品", text):
                        neighbour_references.append({"module_key": key, "sentence_id": sentence["id"], "excerpt": text[:100]})
            for text, keys in seen.items():
                if len(keys) > 1:
                    duplicate_groups.append({"module_keys": list(dict.fromkeys(keys)), "occurrences": len(keys), "excerpt": text[:100]})
            duration = script_duration_metrics(selected_modules)
            by_opening[opening] = {"spoken_characters": duration["spoken_characters"], "raw_text_characters": characters,
                "sentence_count": sentences, "duration_estimate": duration,
                "exact_duplicate_group_count": len(duplicate_groups), "exact_duplicate_groups": duplicate_groups[:20],
                "neighbour_reference_count": len(neighbour_references), "neighbour_references": neighbour_references[:20],
                "soft_length_observation": duration["duration_goal_status"]}
        return {"status": "advisory_only", "basis": "one_selected_opening_plus_body", "by_opening": by_opening,
            "length_goal": length_goal([]),
            "notice": "按每分钟240个有效口播字估算8—10分钟，仅供文字规划；实际时长以生成音频为准。每次只计所选一种开场及正文，重复或邻项依赖由主Agent判断是否局改。"}

    def _record_generation_metric(self, job_id, item_id, stage, elapsed_ms, **counts):
        """Local timings are facts, not another model or a validation pass."""
        with self.store.transaction(write=True) as c:
            saved = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(saved["body"])
            item = next(row for row in body["items"] if row["item_id"] == item_id)
            metric = {"stage": stage, "elapsed_ms": round(elapsed_ms, 3), "recorded_at": now(), **counts}
            item.setdefault("generation_metrics", []).append(metric)
            self.store.put(c, "prelive_job", job_id, body, expected=saved["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "script.generation_stage", {
                "job_id": job_id, "item_id": item_id, **metric})

    def _whole_script_checkpoint(self, job_id, item_id, dependency_hash):
        with self.store.transaction() as c:
            job = self.store.get(c, "prelive_job", job_id)["body"]
            item = next(i for i in job["items"] if i["item_id"] == item_id)
            if item.get("recovery_mode") == "revalidate_archived":
                request = next(x for x in item["archive_revalidations"]
                    if x["revalidation_id"] == item["active_archive_revalidation_id"])
                bound = request["targets"].get("whole_script")
                if bound is None:
                    raise BusinessError("ARCHIVED_RESPONSE_INCOMPLETE", details={"provider_reissued": False})
                attempt = next(x for x in item.get("provider_attempts", []) if x["attempt_id"] == bound["attempt_id"])
                archived = self.store.exact(c, bound["response_ref"])
                value = archived["body"]
                if (attempt.get("validation_state") != "rejected" or attempt.get("provider_response_ref") != archived["ref"]
                        or archived["ref"]["kind"] != "provider_response" or value.get("job_id") != job_id
                        or value.get("item_id") != item_id or value.get("attempt_id") != attempt["attempt_id"]
                        or attempt.get("purpose") != "copywriting.whole_script"
                        or attempt.get("input", {}).get("dependency_hash") != bound["dependency_hash"]):
                    raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
                return {"response": value["response"], "attempt_id": attempt["attempt_id"],
                    "dependency_hash": bound["dependency_hash"], "revalidate_archived": True,
                    "committed_module_refs": item.get("whole_script_result", {}).get("module_refs")}
            try:
                record = self.store.get(c, "prelive_whole_script_result", dependency_hash)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
            else:
                for ref in record["body"]["module_refs"].values():
                    self.store.exact(c, ref)
                return {"module_refs": record["body"]["module_refs"], "reused": True}
            for attempt in reversed(item.get("provider_attempts", [])):
                if (attempt.get("input", {}).get("dependency_hash") == dependency_hash
                        and attempt.get("provider_response_ref")
                        and attempt.get("validation_state") != "rejected"):
                    archive = self.store.exact(c, attempt["provider_response_ref"])["body"]
                    if (archive.get("job_id") != job_id or archive.get("item_id") != item_id
                            or archive.get("attempt_id") != attempt["attempt_id"]):
                        raise BusinessError("PROVIDER_RESPONSE_LINK_MISMATCH")
                    return {"response": archive["response"], "attempt_id": attempt["attempt_id"], "resumed": True}
        return None

    def _commit_whole_script_result(self, job_id, item_id, dependency_hash, modules, attempt_id=None, reused_refs=None):
        """All selected modules have validated before this one transaction.

        The cache contains the complete valid set or nothing. Selection of the
        assembled script remains in _commit_job_item with its original fence.
        """
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            self._job_fence(c, body)
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            if item["status"] != "running":
                raise BusinessError("JOB_LEASE_LOST")
            refs = {key: self.store.exact(c, ref)["ref"] for key, ref in reused_refs.items()} if reused_refs is not None else {
                key: self.store.artifact(c, "module", value) for key, value in modules.items()}
            expected = {a["key"] for a in item["input"]["modules"] if a["action"] == "generate"}
            if set(refs) != expected:
                raise BusinessError("WHOLE_SCRIPT_RESULT_MISMATCH")
            record = {"protocol": "whole-script-v1", "dependency_hash": dependency_hash,
                "module_refs": refs, "status": "committed", "job_id": job_id, "item_id": item_id, "committed_at": now()}
            try:
                saved = self.store.get(c, "prelive_whole_script_result", dependency_hash)
            except BusinessError as error:
                if error.code != "NOT_FOUND":
                    raise
                self.store.put(c, "prelive_whole_script_result", dependency_hash, record, create=True)
            else:
                if saved["body"]["module_refs"] != refs:
                    raise BusinessError("WHOLE_SCRIPT_RESULT_CONFLICT")
            item["whole_script_result"] = record
            item.setdefault("module_results", {}).update({key: {
                "module_key": key, "module_ref": ref, "dependency_hash": dependency_hash,
                "protocol": "whole-script-v1", "status": "committed", "job_id": job_id,
                "item_id": item_id, "committed_at": record["committed_at"]} for key, ref in refs.items()})
            item["call_state"] = "result_received"
            if attempt_id:
                attempt = next(a for a in item["provider_attempts"] if a["attempt_id"] == attempt_id)
                attempt.update(validation_state="accepted", committed_module_refs=refs)
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "script.whole_result_committed", {
                "job_id": job_id, "item_id": item_id, "module_refs": refs,
                "dependency_hash": dependency_hash, "reused": reused_refs is not None,
                "provider_attempt_count": 0 if reused_refs is not None or attempt_id is None else 1})
            return refs

    async def _produce_whole_script(self, job, item):
        start = perf_counter_ns()
        outcome = "succeeded"
        try:
            return await self._produce_whole_script_impl(job, item)
        except BaseException as error:
            outcome = error.code if isinstance(error, BusinessError) else type(error).__name__
            raise
        finally:
            await asyncio.to_thread(self._record_generation_metric, job["id"], item["item_id"],
                "whole_attempt_total", (perf_counter_ns() - start) / 1_000_000, outcome=outcome)

    async def _produce_whole_script_impl(self, job, item):
        from .whole_script_generation import prompt_revision, finish_received_script
        start = perf_counter_ns()
        spec = item["input"]
        local_only = item.get("recovery_mode") == "revalidate_archived"
        if spec.get("generator_revision") != prompt_revision() and not local_only:
            raise BusinessError("GENERATOR_REVISION_CHANGED", "整版生成规则已更新，请重新保存生成计划；已有返回和稿件保留，未再次调用供应商",
                details={"commit_state": "not_committed", "provider_reissued": False})
        with self.store.transaction() as c:
            product = commerce.active_product_fields(self.store.exact(c, spec["product_ref"])["body"])
            base = self._artifact(c, spec["base_script_ref"])["body"] if spec.get("base_script_ref") else None
        context = commerce.active_context_products({**job["context"],
            "variant": {"variant_id": spec["variant_id"], "version_number": spec["version_number"]}})
        from .script_imports import original_script_context
        from .script_version_context import script_version_context
        with self.store.transaction() as c:
            context.update(original_script_context(self, c, spec, job["preparation_id"]))
            context.update(script_version_context(self, c, job, spec))
        from .writing_input_projection import project_writing_context
        context = project_writing_context(context, spec)
        signature = self.adapter.script_generation_signature() if hasattr(self.adapter, "script_generation_signature") else {}
        dependency_input = {"protocol": "whole-script-v1", "variant_id": spec["variant_id"],
            "version_number": spec["version_number"], "product_ref": spec["product_ref"],
            "context": context, "actions": spec["modules"], "base_script_ref": spec.get("base_script_ref"),
            "generator": type(self.adapter).__module__ + "." + type(self.adapter).__qualname__,
            "provider_configuration": signature, "prompt_revision": prompt_revision()}
        dependency_hash = digest(dependency_input)
        checkpoint = await asyncio.to_thread(self._whole_script_checkpoint, job["id"], item["item_id"], dependency_hash)
        await asyncio.to_thread(self._record_generation_metric, job["id"], item["item_id"], "whole_context_and_checkpoint",
            (perf_counter_ns() - start) / 1_000_000, cache_hit=bool(checkpoint and checkpoint.get("module_refs")),
            archived_response=bool(checkpoint and checkpoint.get("response")))
        if checkpoint and checkpoint.get("module_refs"):
            refs = await asyncio.to_thread(self._commit_whole_script_result, job["id"], item["item_id"],
                dependency_hash, None, reused_refs=checkpoint["module_refs"])
        else:
            attempt = checkpoint.get("attempt_id") if checkpoint else None
            if local_only:
                if not checkpoint or not checkpoint.get("revalidate_archived"):
                    raise BusinessError("ARCHIVED_RESPONSE_INCOMPLETE", details={"provider_reissued": False})
                check_id = await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"],
                    "whole_script", checkpoint, current_generator_revision=prompt_revision())
                validated = False
                try:
                    receipt = checkpoint["response"]
                    expected_hash = digest({**dependency_input, "prompt_revision": receipt.get("prompt_revision")})
                    from .archive_validation_product import verified_archived_validation_product
                    same_input = (receipt.get("protocol") == "whole-script-v1" and bool(receipt.get("prompt_revision"))
                            and receipt.get("prompt_revision") == spec.get("generator_revision")
                            and expected_hash == checkpoint["dependency_hash"])
                    validation = verified_archived_validation_product(product, context, receipt.get("validation_product")) if same_input else None
                    if validation is None:
                        raise BusinessError("ARCHIVED_RESPONSE_INPUT_MISMATCH", "原归档与当前固定任务输入不一致，未重新请求供应商",
                            details={"provider_reissued": False})
                    validation_product = validation["product"]
                    texts = finish_received_script(receipt, validation_product, spec["modules"])
                    modules = {key: self._module_body(key, value.get("sentences"),
                        next(a.get("base_module_ref") for a in spec["modules"] if a["key"] == key), value.get("sentence_details"))
                        for key, value in texts["modules"].items()}
                    await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], "whole_script",
                        checkpoint, status="validated", check_id=check_id, validation_details={
                            "verified_dependency_hash": expected_hash, "validation_product_hash": digest(validation_product),
                            "validation_product_contract": validation["contract"],
                            "normalization": texts.get("normalization"), "validation_metrics": texts.get("validation_metrics")})
                    validated = True
                    reused_refs = checkpoint.get("committed_module_refs")
                    if reused_refs:
                        with self.store.transaction() as c:
                            if set(reused_refs) != set(modules) or any(self.store.exact(c, ref)["body"] != modules[key]
                                    for key, ref in reused_refs.items()):
                                raise BusinessError("WHOLE_SCRIPT_RESULT_CONFLICT")
                    refs = await asyncio.to_thread(self._commit_whole_script_result, job["id"], item["item_id"],
                        checkpoint["dependency_hash"], modules, None, reused_refs=reused_refs)
                    await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], "whole_script",
                        checkpoint, status="modules_committed", check_id=check_id, committed_refs=refs)
                except BaseException as error:
                    await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], "whole_script",
                        checkpoint, status="commit_failed" if validated else "rejected", check_id=check_id, error=error)
                    raise
                modules = {action["key"]: None if action["action"] == "disable" else {"reuse_ref": refs[action["key"]]}
                    for action in spec["modules"]}
                return {"kind": "script", "spec": spec, "modules": modules, "base": base}
            if checkpoint and checkpoint.get("response"):
                receipt = checkpoint["response"]
                await asyncio.to_thread(self._record_module_normalization, job["id"], item["item_id"], attempt)
                texts = finish_received_script(receipt, receipt["validation_product"], spec["modules"])
            else:
                if not callable(getattr(self.adapter, "generate_script", None)):
                    raise BusinessError("WHOLE_SCRIPT_GENERATOR_NOT_CONFIGURED", status=503)
                attempt = await asyncio.to_thread(self._before_provider, job["id"], item["item_id"], "copywriting.whole_script",
                    {"generation_mode": "whole_script", "module_keys": [a["key"] for a in spec["modules"] if a["action"] == "generate"],
                     "product_ref": spec["product_ref"], "context_ref": job["context_ref"], "dependency_hash": dependency_hash,
                     "generator_revision": prompt_revision()})
                kwargs = {}
                if "response_observer" in inspect.signature(self.adapter.generate_script).parameters:
                    async def observe(receipt):
                        await asyncio.to_thread(self._archive_provider_response, job["id"], item["item_id"], attempt, receipt)
                    kwargs["response_observer"] = observe
                texts = await self.adapter.generate_script(context, spec["modules"], product, **kwargs)
            if not isinstance(texts, dict) or not isinstance(texts.get("modules"), dict):
                raise BusinessError("WHOLE_SCRIPT_RESPONSE_INVALID", "整版生成器未返回完整模块对象")
            expected = {a["key"] for a in spec["modules"] if a["action"] == "generate"}
            if set(texts["modules"]) != expected:
                raise BusinessError("WHOLE_SCRIPT_RESULT_MISMATCH")
            await asyncio.to_thread(self._after_provider, job["id"], item["item_id"], attempt, texts)
            modules = {key: self._module_body(key, value.get("sentences"),
                next(a.get("base_module_ref") for a in spec["modules"] if a["key"] == key), value.get("sentence_details"))
                for key, value in texts["modules"].items()}
            tick = perf_counter_ns()
            refs = await asyncio.to_thread(self._commit_whole_script_result, job["id"], item["item_id"],
                dependency_hash, modules, attempt)
            await asyncio.to_thread(self._record_generation_metric, job["id"], item["item_id"], "whole_module_commit",
                (perf_counter_ns() - tick) / 1_000_000,
                module_count=len(refs), validation_metrics=texts.get("validation_metrics", {}),
                provider_timing=texts.get("timing", {}), context_compaction=texts.get("context_compaction", {}))
        modules = {action["key"]: None if action["action"] == "disable" else {"reuse_ref": refs[action["key"]]}
                   for action in spec["modules"]}
        await asyncio.to_thread(self._record_generation_metric, job["id"], item["item_id"], "whole_item_ready",
            (perf_counter_ns() - start) / 1_000_000, module_count=len(refs))
        return {"kind": "script", "spec": spec, "modules": modules, "base": base}

    async def _produce_script(self, job, item):
        spec = item["input"]
        # Already persisted jobs without this marker keep their old module
        # protocol, so a deployment never reinterprets received responses.
        if spec.get("generation_mode") == "whole_script":
            return await self._produce_whole_script(job, item)
        with self.store.transaction() as c:
            product = commerce.active_product_fields(self.store.exact(c, spec["product_ref"])["body"])
            base = self._artifact(c, spec["base_script_ref"])["body"] if spec.get("base_script_ref") else None
        modules = {}
        for action in spec["modules"]:
            key = action["key"]
            if action["action"] == "disable":
                modules[key] = None
            elif action["action"] == "reuse":
                modules[key] = {"reuse_ref": action["base_module_ref"]}
            else:
                with self.store.transaction() as c:
                    prior = self._artifact(c, action["base_module_ref"])["body"]["sentences"] if action.get("base_module_ref") else []
                context = commerce.active_context_products({**job["context"], "generation_instruction": action["instruction"], "module_key": key})
                from .script_imports import original_script_context
                with self.store.transaction() as c:
                    context.update(original_script_context(self, c, spec, job["preparation_id"]))
                from .module_generation import module_prompt_revision, finish_received_module
                dependency_input = {"version": "module-result-v1", "variant_id": spec["variant_id"],
                    "version_number": spec["version_number"], "product_ref": spec["product_ref"],
                    "context": context, "previous": prior, "action": action,
                    "generator": type(self.adapter).__module__ + "." + type(self.adapter).__qualname__,
                    "provider_configuration": self.adapter.module_generation_signature() if hasattr(self.adapter, "module_generation_signature") else {},
                    "prompt_revision": module_prompt_revision(key)}
                dependency_hash = digest(dependency_input)
                checkpoint = await asyncio.to_thread(self._module_checkpoint, job["id"], item["item_id"], dependency_hash, module_key=key)
                if checkpoint and checkpoint.get("module_ref"):
                    module_ref = await asyncio.to_thread(self._commit_module_result, job["id"], item["item_id"], key,
                        checkpoint.get("dependency_hash", dependency_hash), None, None, checkpoint["module_ref"])
                    modules[key] = {"reuse_ref": module_ref}
                    continue
                if item.get("recovery_mode") == "revalidate_archived":
                    if not checkpoint or not checkpoint.get("revalidate_archived"):
                        raise BusinessError("ARCHIVED_RESPONSE_INCOMPLETE", details={"module_key": key, "provider_reissued": False})
                    check_id = await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"],
                        key, checkpoint, current_generator_revision=module_prompt_revision(key))
                    validated = False
                    try:
                        response = checkpoint["response"]
                        expected_hash = digest({**dependency_input, "prompt_revision": response.get("prompt_revision")})
                        from .archive_validation_product import verified_archived_validation_product
                        same_input = (response.get("protocol") != "whole-script-v1" and response.get("module_key") == key
                                and bool(response.get("prompt_revision")) and expected_hash == checkpoint["dependency_hash"])
                        validation = verified_archived_validation_product(product, context, response.get("validation_product")) if same_input else None
                        if validation is None:
                            raise BusinessError("ARCHIVED_RESPONSE_INPUT_MISMATCH", "原归档与当前固定任务输入不一致，未重新请求供应商",
                                details={"module_key": key, "provider_reissued": False})
                        validation_product = validation["product"]
                        texts = finish_received_module(response, validation_product, key)
                        module = self._module_body(key, texts.get("sentences"), action.get("base_module_ref"), texts.get("sentence_details"))
                        await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], key,
                            checkpoint, status="validated", check_id=check_id, validation_details={
                                "verified_dependency_hash": expected_hash, "validation_product_hash": digest(validation_product),
                                "validation_product_contract": validation["contract"],
                                "normalization": texts.get("normalization")})
                        validated = True
                        reused_ref = checkpoint.get("committed_module_ref")
                        if reused_ref:
                            with self.store.transaction() as c:
                                if self.store.exact(c, reused_ref)["body"] != module:
                                    raise BusinessError("MODULE_RESULT_CONFLICT")
                        module_ref = await asyncio.to_thread(self._commit_module_result, job["id"], item["item_id"], key,
                            checkpoint["dependency_hash"], module, None, reused_ref=reused_ref)
                        await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], key,
                            checkpoint, status="modules_committed", check_id=check_id, committed_refs={key: module_ref})
                    except BaseException as error:
                        await asyncio.to_thread(self._record_archive_revalidation, job["id"], item["item_id"], key,
                            checkpoint, status="commit_failed" if validated else "rejected", check_id=check_id, error=error)
                        raise
                    modules[key] = {"reuse_ref": module_ref}
                    continue
                if checkpoint and checkpoint.get("response"):
                    attempt = checkpoint["attempt_id"]
                    response = checkpoint["response"]
                    await asyncio.to_thread(self._record_module_normalization, job["id"], item["item_id"], attempt)
                    texts = finish_received_module(response, response["validation_product"], key)
                else:
                    attempt = await asyncio.to_thread(self._before_provider, job["id"], item["item_id"], "copywriting." + key,
                        {"module_key": key, "instruction": action["instruction"], "product_ref": spec["product_ref"],
                         "context_ref": job["context_ref"], "previous_sentences": prior, "dependency_hash": dependency_hash})
                    kwargs = {}
                    if "response_observer" in inspect.signature(self.adapter.generate_module).parameters:
                        async def observe(receipt):
                            await asyncio.to_thread(self._archive_provider_response, job["id"], item["item_id"], attempt, receipt)
                        kwargs["response_observer"] = observe
                    texts = await self.adapter.generate_module(context, key, product, prior, **kwargs)
                await asyncio.to_thread(self._after_provider, job["id"], item["item_id"], attempt, {"sentences": texts})
                details = texts.get("sentence_details") if isinstance(texts, dict) else None
                lines = texts.get("sentences") if isinstance(texts, dict) else texts
                module = self._module_body(key, lines, action.get("base_module_ref"), details)
                module_ref = await asyncio.to_thread(self._commit_module_result, job["id"], item["item_id"], key,
                    dependency_hash, module, attempt)
                modules[key] = {"reuse_ref": module_ref}
        return {"kind": "script", "spec": spec, "modules": modules, "base": base}

    @staticmethod
    def _audio_style_matches(asset, requirement):
        # Absence is the old frozen protocol, not an invitation to resolve the
        # latest module defaults during approval/recovery/cache reuse.
        return "style" not in requirement or asset.get("style") == requirement["style"]

    async def _produce_audio(self, job, item):
        requirement = item["input"]
        with self.store.transaction() as c:
            try:
                cached = self.store.get(c, "prelive_audio_asset", requirement["requirement_id"])["body"]
            except BusinessError:
                cached = None
        if cached and self._audio_valid(cached) and self._audio_style_matches(cached, requirement):
            return {"kind": "audio", "asset": {**cached, "reused": True}}
        # A process may stop after the returned WAV was archived but before its
        # asset commit. Resume that local result rather than synthesizing twice.
        with self.store.transaction() as c:
            persisted = self.store.get(c, "prelive_job", job["id"])["body"]
            saved_item = next(i for i in persisted["items"] if i["item_id"] == item["item_id"])
            received = next((a.get("output") for a in reversed(saved_item.get("provider_attempts", []))
                if a.get("purpose") == "tts" and a.get("state") == "result_received"
                and a.get("input", {}).get("requirement_id") == requirement["requirement_id"]
                and ("style" not in requirement or a.get("input", {}).get("style") == requirement["style"])
                and isinstance(a.get("output"), dict) and self._audio_valid(a["output"])), None)
        style_fields = {"style": deepcopy(requirement["style"])} if "style" in requirement else {}
        if received:
            asset = {**received, "requirement_id": requirement["requirement_id"],
                "spoken_text_hash": digest(requirement["spoken_text"]), "voice": requirement["voice"],
                "sha256_bytes": "sha256:" + hashlib.sha256(Path(received["path"]).read_bytes()).hexdigest(),
                **style_fields, "reused": True}
            return {"kind": "audio", "asset": asset}
        attempt = await asyncio.to_thread(self._before_provider, job["id"], item["item_id"], "tts", {"spoken_text": requirement["spoken_text"], "voice": requirement["voice"], "requirement_id": requirement["requirement_id"], **style_fields})
        kwargs = {"style": deepcopy(requirement["style"])} if "style" in requirement else {}
        if "response_observer" in inspect.signature(self.adapter.render_audio).parameters:
            kwargs["response_observer"] = lambda observation: self._record_tts_observation(job["id"], item["item_id"], attempt, observation)
        asset = await self.adapter.render_audio(requirement["spoken_text"], job["room_id"], requirement["voice"], **kwargs)
        await asyncio.to_thread(self._after_provider, job["id"], item["item_id"], attempt, asset)
        if not isinstance(asset, dict) or not self._audio_valid(asset):
            raise BusinessError("INVALID_AUDIO_OUTPUT", "合成结果不是有效本地WAV，未标记完成")
        asset = {**asset, "requirement_id": requirement["requirement_id"], "spoken_text_hash": digest(requirement["spoken_text"]), "voice": requirement["voice"], **style_fields, "sha256_bytes": "sha256:" + hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest()}
        return {"kind": "audio", "asset": asset}

    def _commit_job_item(self, job_id, item_id, output):
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            if item["status"] != "running":
                raise BusinessError("JOB_LEASE_LOST")
            prep = self._job_fence(c, body)
            if output["kind"] == "script":
                spec = output["spec"]
                if not spec.get("base_script_ref") and any(r["body"]["variant_id"] == spec["variant_id"] for r in self.store.all(c, "script_revision")):
                    raise BusinessError("VARIANT_ALREADY_EXISTS")
                if spec.get("base_script_ref") and spec["base_script_ref"] not in prep["body"]["script_refs"]:
                    raise BusinessError("BASE_SCRIPT_TARGET_MISMATCH")
                module_refs = {}
                for key, module in output["modules"].items():
                    module_refs[key] = None if module is None else module["reuse_ref"] if "reuse_ref" in module else self.store.artifact(c, "module", module)
                spec, base = output["spec"], output["base"]
                script_body = {"preparation_id": body["preparation_id"], "product_id": spec["product_ref"]["id"], "product_ref": spec["product_ref"], "variant_id": spec["variant_id"], "version_number": spec["version_number"], "script_revision": base["script_revision"] + 1 if base else 1, "parent_ref": spec.get("base_script_ref"), "module_refs": module_refs, "input_hash": digest({"plan_ref": body["plan_ref"], "spec": spec}), "created_at": now(), "content_diagnostics": self._script_content_diagnostics(c, module_refs)}
                from ..welcome import build_product_templates
                script_body["welcome_templates"] = build_product_templates(self.store.exact(c, spec["product_ref"])["body"])
                script_body["generation_provenance"] = {"plan_ref": body["plan_ref"],
                    "context_ref": body["context_ref"], "product_ref": spec["product_ref"],
                    "generation_mode": spec.get("generation_mode", "module"),
                    "generator_revision": spec.get("generator_revision"), "job_id": job_id,
                    "item_id": item_id, "parent_script_ref": spec.get("base_script_ref")}
                result_ref = self.store.artifact(c, "script_revision", script_body)
                prep_body = deepcopy(prep["body"])
                if spec.get("base_script_ref"):
                    prep_body["script_refs"] = [result_ref if r == spec["base_script_ref"] else r for r in prep_body["script_refs"]]
                    if prep_body["playlist_ref"]:
                        playlist = deepcopy(self._artifact(c, prep_body["playlist_ref"])["body"])
                        for occurrence in playlist["items"]:
                            if occurrence["script_revision_ref"] == spec["base_script_ref"]:
                                occurrence["script_revision_ref"] = result_ref
                        prep_body["playlist_ref"] = self.store.artifact(c, "playlist", playlist)
                elif result_ref not in prep_body["script_refs"]:
                    prep_body["script_refs"].append(result_ref)
                prep_body.update(compiled_plan_ref=None, requirements_ref=None, approval_ref=None, audio_manifest_ref=None, candidate_publication_ref=None)
                self._save_prep(c, prep, prep_body, invalidate=False)
                item["output"] = {"script_ref": result_ref, "content_diagnostics": script_body["content_diagnostics"]}
                if item.get("recovery_mode") == "revalidate_archived":
                    revalidation = next(x for x in item["archive_revalidations"]
                        if x["revalidation_id"] == item["active_archive_revalidation_id"])
                    revalidation.update(status="completed", script_ref=result_ref, completed_at=now())
            else:
                asset = output["asset"]
                try:
                    existing = self.store.get(c, "prelive_audio_asset", asset["requirement_id"])
                except BusinessError:
                    existing = None
                asset_ref = self.store.put(c, "prelive_audio_asset", asset["requirement_id"], asset, expected=existing["ref"]["revision"] if existing else None, create=existing is None)
                item["output"] = {"asset_ref": asset_ref, "asset": asset}
                if body.get("audio_reissue_approval_ref"):
                    from .audio_reissue import record_resolution
                    record_resolution(self, c, prep["body"], body, item, asset_ref)
            item.update(status="succeeded", call_state="result_received", completed_at=now())
            unfinished = any(i["status"] in {"queued", "running"} for i in body["items"])
            if not unfinished:
                successes = sum(i["status"] == "succeeded" for i in body["items"])
                body["status"] = "succeeded" if successes == len(body["items"]) else "partially_failed" if successes else "failed"
                if body["kind"] == "audio" and body["status"] == "succeeded":
                    latest = self._prep(c, body["preparation_id"])
                    if body.get("audio_reissue_approval_ref"):
                        from .audio_reissue import complete_manifest
                        manifest_ref = complete_manifest(self, c, latest["body"], body)
                    else:
                        manifest_ref = self.store.artifact(c, "audio_manifest", {"preparation_id": body["preparation_id"], "requirements_ref": body["requirements_ref"], "approval_ref": body["approval_ref"], "assets": [i["output"]["asset"] for i in body["items"]], "all_ready": True})
                    if manifest_ref:
                        prep_body = deepcopy(latest["body"])
                        prep_body["audio_manifest_ref"] = manifest_ref
                        self._save_prep(c, latest, prep_body, invalidate=False)
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "job.item_succeeded", {"job_id": job_id, "item_id": item_id, "output": item["output"], "status": body["status"]})

    def _fail_job_item(self, job_id, item_id, error, unknown=False, candidate=None):
        with self.store.transaction(write=True) as c:
            job = self.store.get(c, "prelive_job", job_id)
            body = deepcopy(job["body"])
            item = next(i for i in body["items"] if i["item_id"] == item_id)
            # Cancellation can arrive while to_thread is returning from an
            # already committed result. Its durable success wins that race.
            if item["status"] == "succeeded":
                return
            if item.get("provider_attempts") and isinstance(error.details.get("tts_diagnostics"), dict):
                self._apply_tts_observation(c, body, item, item["provider_attempts"][-1], error.details["tts_diagnostics"])
            unknown = bool(unknown and item.get("call_state") == "sent")
            status = "superseded" if error.code in {"GENERATION_EPOCH_CHANGED", "GENERATION_INPUTS_CHANGED", "GRANT_REVOKED", "TEXT_APPROVAL_STALE"} else "cancelled" if body.get("cancel_requested") else "interrupted" if unknown else "failed"
            item.update(status=status, call_state="timeout_unknown" if unknown else "result_received", error=error.payload())
            if item.get("provider_attempts") and item["provider_attempts"][-1]["state"] == "sent":
                item["provider_attempts"][-1].update(state="timeout_unknown" if unknown else "known_failed", error=error.payload())
                self._record_request_outcome(c, item["provider_attempts"][-1], "timeout_unknown" if unknown else "known_failed")
            elif (item.get("provider_attempts") and item["provider_attempts"][-1].get("validation_state") == "pending"
                    and error.code in {"MODULE_RESPONSE_INVALID", "MODULE_RESPONSE_MISMATCH", "SHORT_MODULE_TOO_LONG",
                    "GENERATED_MODULE_NEEDS_REVIEW", "INVALID_MODULE_TEXT", "DUPLICATE_MODULE_SENTENCE", "MODULE_DETAILS_MISMATCH",
                    "WHOLE_SCRIPT_RESPONSE_INVALID", "WHOLE_SCRIPT_RESULT_MISMATCH"}):
                item["provider_attempts"][-1].update(validation_state="rejected", validation_error=error.payload())
            if candidate is not None:
                # Audit-only quarantine: never changes preparation/room refs.
                item["candidate_ref"] = self.store.artifact(c, "quarantined_candidate", {"job_id": job_id, "item_id": item_id, "reason": error.code, "candidate": candidate})
            if not any(i["status"] in {"queued", "running"} for i in body["items"]):
                body["status"] = "partially_failed" if any(i["status"] == "succeeded" for i in body["items"]) else status
            self.store.put(c, "prelive_job", job_id, body, expected=job["ref"]["revision"])
            self.store.event(c, body["preparation_id"], "job.item_failed", {"job_id": job_id, "item_id": item_id, "status": status, "error": error.payload()})

    def _q_read_capabilities(self, c, a, p, s):
        return {"contract_version": "PRELIVE-2.3", "commands": list(COMMAND_ARGS), "queries": list(QUERY_NAMES), "adapter_configured": not isinstance(self.adapter, UnavailableAdapter), "human_only": sorted(HUMAN_ONLY), "controller_only": sorted(CONTROLLER_ONLY), "dynamic_generation": True, "live_agent_control": False}

    def _q_get_business_schema(self, c, a, p, s):
        return self.schema()

    def _q_search_products(self, c, a, p, s):
        products = self.store.all(c, "product")
        matches = [r for r in products if not r["body"].get("archived") and a.q.casefold() in (r["body"].get("name", "") + " " + r["body"].get("sku_id", "")).casefold()]
        page = matches[a.cursor:a.cursor + a.limit]
        return {"items": [{"ref": r["ref"], "name": r["body"]["name"], "sku_id": r["body"].get("sku_id"), "display_number": r["body"].get("display_number"), "price": r["body"]["price"], "currency": r["body"]["currency"], "sale_status": r["body"].get("sale_status", "available")} for r in page], "next_cursor": a.cursor + a.limit if a.cursor + a.limit < len(matches) else None}

    def _q_read_product(self, c, a, p, s):
        if a.ref:
            return self._artifact(c, a.ref, "product")
        if not a.product_id:
            raise BusinessError("PRODUCT_REQUIRED", status=400)
        return self.store.get(c, "product", a.product_id)

    def _q_read_preparation(self, c, a, p, s):
        prep = self._prep(c, a.preparation_id)
        body = prep["body"]
        # A discoverable, read-only input projection, not an instruction to
        # invent decisions or silently retry a write with substituted inputs.
        inputs = {"preparation_id": a.preparation_id, "expected_revision": prep["ref"]["revision"],
                  "draft_ref": body["draft_ref"]}
        for key, source in (("product_refs", "selected_product_refs"), ("analysis_refs", "analysis_refs"),
                            ("recap_refs", "recap_refs"), ("source_refs", "source_refs"),
                            ("prior_script_refs", "script_refs")):
            inputs[key] = deepcopy(body[source])
        plan_projection = {}
        if "script_plan" in a.sections and body.get("plan_ref"):
            plan_projection["script_plan"] = self._artifact(c, body["plan_ref"], "script_plan", a.preparation_id)
        return {**prep, **plan_projection, "context_inputs": inputs,
                "context_readers": {"draft_ref": {"query": "read_context", "ref_kind": "draft", "sections": ["draft"]},
                                    "bundle_ref": {"query": "read_context", "ref_kind": "context"}},
                "audio_reader": {"query": "read_audio_readiness", "args": {"preparation_id": a.preparation_id},
                    "ref_fields": ["approval_ref", "requirements_ref", "audio_manifest_ref"],
                    "note": "这些引用不能用read_context读取；使用只读音频就绪查询取得当前批准与覆盖校验。"},
                "context_input_contract": {"command": "save_context_bundle", "exact_ref_fields": ["kind", "id", "revision", "content_hash"],
                    "schema_version": "可省略；若显式提供必须匹配真实制品。服务保存规范引用。",
                    "user_decisions": "由Agent依据本次用户授权填写；本查询不代做决定。"}}

    def _q_read_room(self, c, a, p, s):
        if not a.room_id:
            raise BusinessError("ROOM_REQUIRED", status=400)
        if s and s.kind == "preparation" and self._prep(c, s.preparation_id)["body"]["room_id"] != a.room_id:
            raise BusinessError("SCOPE_CONFLICT", status=403)
        return self.store.get(c, "config", a.room_id)

    def _read_ref_query(self, c, a, s, kind):
        if not a.ref:
            raise BusinessError("EXACT_REF_REQUIRED", status=400)
        result = self._artifact(c, a.ref, kind)
        prepid = result["body"].get("preparation_id")
        if prepid and s and (s.kind != "preparation" or s.preparation_id != prepid):
            raise BusinessError("ARTIFACT_SCOPE_CONFLICT", status=403)
        return result

    def _q_read_analysis_report(self, c, a, p, s):
        return self._read_ref_query(c, a, s, "analysis")

    def _q_preview_playlist_reorder(self, c, a, p, s):
        prep = self._prep(c, a.preparation_id)
        if not a.spec or not prep["body"]["playlist_ref"]:
            raise BusinessError("PLAYLIST_AND_SPEC_REQUIRED", status=400)
        if a.ref and self._artifact(c, a.ref, "playlist", a.preparation_id)["ref"] != prep["body"]["playlist_ref"]:
            raise BusinessError("PLAYLIST_CHANGED")
        old = self._artifact(c, prep["body"]["playlist_ref"])["body"]
        result = reorder(old["items"], wire(a.spec))
        return {**result, "base_playlist_ref": prep["body"]["playlist_ref"], "preview_hash": digest({"base_ref": prep["body"]["playlist_ref"], **result})}

    def _q_find_audience_research(self, c, a, p, s):
        product = self._q_read_product(c, a, p, s)
        # The index returns report bodies, so its scope must match the exact
        # report reader. Filter before pagination: inaccessible preparations
        # cannot consume visible rows or leak through a discovery endpoint.
        sql = "SELECT r.artifact_id FROM prelive_research r JOIN records a ON a.kind='analysis' AND a.id=r.artifact_id WHERE r.product_id=?"
        params = [product["ref"]["id"]]
        if s:
            sql += " AND (json_extract(a.body,'$.preparation_id') IS NULL OR json_extract(a.body,'$.preparation_id')=?)"
            params.append(s.preparation_id if s.kind == "preparation" else None)
        # Fetch at most one page plus its continuation sentinel. Scope
        # filtering need not deserialize every historical report.
        sql += " ORDER BY r.rowid DESC LIMIT ? OFFSET ?"
        params.extend([a.limit + 1, a.cursor])
        rows = c.execute(sql, params).fetchall()
        items = []
        for row in rows[:a.limit]:
            report = self.store.get(c, "analysis", row[0])
            matching = any(r == product["ref"] for r in report["body"]["product_refs"])
            report_context = report["body"]["report"].get("fields", {}).get("context", {})
            context_matching = not a.context or report_context == a.context
            items.append({"research_ref": report["ref"], "compatibility": "exact" if matching and context_matching else "requires_review", "context_match": context_matching, "stored_context": report_context, "product_refs": report["body"]["product_refs"], "report": report["body"]["report"]})
        return {"items": items, "next_cursor": a.cursor + a.limit if len(rows) > a.limit else None}

    def _q_read_session_history(self, c, a, p, s):
        room_id = a.room_id or self._prep(c, a.preparation_id)["body"]["room_id"]
        if s and s.kind == "preparation" and self._prep(c, s.preparation_id)["body"]["room_id"] != room_id:
            raise BusinessError("SCOPE_CONFLICT", status=403)
        room = self.store.get(c, "config", room_id)
        rows = c.execute("SELECT revision FROM record_revisions WHERE kind='config' AND id=? ORDER BY revision DESC LIMIT ? OFFSET ?", (room_id, a.limit, a.cursor)).fetchall()
        return {"room_ref": room["ref"], "configurations": [self.store.get(c, "config", room_id, revision=r[0]) for r in rows], "preparations": [r for r in self.store.all(c, "preparation") if r["body"]["room_id"] == room_id], "live_sessions": [r for r in self.store.all(c, "prelive_session") if r["body"]["room_id"] == room_id], "legacy_playback_logs": [r for r in self.store.all(c, "room_log") if r["body"]["room_id"] == room_id][-a.limit:], "live_history_status": "recorded_starts_and_actual_player_logs"}

    def _q_inspect_change_impact(self, c, a, p, s):
        if not a.product_id and not a.ref:
            raise BusinessError("TARGET_REQUIRED", status=400)
        product_id = a.product_id or a.ref.id
        scripts = [r["ref"] for r in self.store.all(c, "script_revision") if r["body"]["product_id"] == product_id]
        preparations = [r["ref"] for r in self.store.all(c, "preparation") if any(x["id"] == product_id for x in r["body"]["selected_product_refs"])]
        import json
        changes = c.execute("SELECT id,after_ref,created_at FROM prelive_changes WHERE kind='product' AND object_id=? ORDER BY created_at DESC LIMIT 20", (product_id,)).fetchall()
        return {"target_product_id": product_id, "potentially_affected_script_refs": scripts, "preparation_refs": preparations, "recent_changes": [{"change_id": r["id"], "after_ref": json.loads(r["after_ref"]), "created_at": r["created_at"]} for r in changes], "automatic_script_changes": False, "impact_certainty": "conservative_product_dependency"}

    def _q_read_asset_checklist(self, c, a, p, s):
        if a.ref:
            return self._read_ref_query(c, a, s, "asset_checklist")
        prep = self._prep(c, a.preparation_id)
        if not prep["body"]["asset_checklist_ref"]:
            return {"status": "not_prepared", "items": []}
        return self._artifact(c, prep["body"]["asset_checklist_ref"], "asset_checklist")

    def _q_read_recap(self, c, a, p, s):
        result = self._read_ref_query(c, a, s, "recap")
        blob = self._artifact(c, result["body"]["raw_blob_ref"], "blob")["body"]
        if blob["mime"] in {"text/plain", "text/markdown", "application/json"}:
            raw = (self.runtime / blob["relative_path"]).read_bytes()
            if "sha256:" + hashlib.sha256(raw).hexdigest() != blob["sha256_bytes"]:
                raise BusinessError("BLOB_HASH_MISMATCH")
            result["raw_text"] = raw.decode("utf-8")
        return result

    def _q_create_text_review(self, c, a, p, s):
        return self._text_review(c, self._prep(c, a.preparation_id)["body"])

    def _q_read_text_approval(self, c, a, p, s):
        prep = self._prep(c, a.preparation_id)
        ref = wire(a.ref) if a.ref else prep["body"]["approval_ref"]
        if not ref:
            return {"status": "not_reviewed"}
        try:
            approval = self._approval(c, prep["body"], ref)
        except BusinessError as e:
            return {"approval_ref": ref, "status": "revoked" if e.code == "TEXT_APPROVAL_REVOKED" else "stale", "blocker": e.payload()}
        return {"approval_ref": ref, "status": "current", "approval": approval}

    def _assert_current_job_outcomes_known(self, c, prep):
        from .job_outcome_dependencies import observe_job_outcomes
        observation = observe_job_outcomes(self, c, prep)
        if observation["unknown_items"]:
            raise BusinessError("EXTERNAL_OUTCOME_UNKNOWN", "当前成果依赖仍有未知供应商结果，请先核对原任务；未重复生成或发布",
                details={"unknown_items": observation["unknown_items"]})
        if observation["pending_job_ids"]:
            raise BusinessError("BUSINESS_JOB_PENDING", "当前场次仍有执行中的任务，尚未重新生成或发布",
                details={"pending_job_ids": observation["pending_job_ids"]})
        return observation

    def _q_read_audio_readiness(self, c, a, p, s):
        """Observe current approved audio without approving, rendering or publishing.

        Aggregate every requirement/file before applying the detail-page limit.
        References remain the current immutable business references. This query
        is deliberately separate from the context/draft-only context reader.
        """
        prep_record = self._prep(c, a.preparation_id)
        prep = prep_record["body"]
        currentness = self._currentness(c, prep)
        blockers = deepcopy(currentness["blockers"])
        approval = {"status": "not_reviewed", "approval_ref": prep.get("approval_ref")}
        if prep.get("approval_ref"):
            try:
                body = self._approval(c, prep, prep["approval_ref"])
                approval.update(status="current", review_hash=body["review_hash"],
                    approved_at=body.get("approved_at"), operation_id=body.get("operation_id"))
            except BusinessError as error:
                approval.update(status="revoked" if error.code == "TEXT_APPROVAL_REVOKED" else "stale", blocker=error.payload())
        if approval["status"] != "current":
            blockers.append({"code": "TEXT_APPROVAL_NOT_CURRENT", "status": approval["status"]})

        requirements, manifest = None, None
        for key, kind in (("requirements_ref", "requirements"), ("audio_manifest_ref", "audio_manifest")):
            if not prep.get(key):
                blockers.append({"code": "AUDIO_REQUIREMENTS_MISSING" if kind == "requirements" else "AUDIO_MANIFEST_MISSING"})
                continue
            record = self._artifact(c, prep[key], kind, prep["id"])
            if record["body"].get("preparation_id") != prep["id"]:
                raise BusinessError("ARTIFACT_SCOPE_CONFLICT", status=403)
            if kind == "requirements":
                requirements = record["body"]
            else:
                manifest = record["body"]
        required = (requirements or {}).get("items", [])
        assets = (manifest or {}).get("assets", [])
        required_ids = [item["requirement_id"] for item in required]
        actual = {}
        for asset in assets:
            actual.setdefault(asset.get("requirement_id"), []).append(asset)
        rows = []
        for item in required:
            matches = actual.get(item["requirement_id"], [])
            asset = matches[0] if len(matches) == 1 else None
            recorded_hash = asset.get("sha256_bytes") if asset else None
            hash_recorded = isinstance(recorded_hash, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", recorded_hash) is not None
            rows.append({"requirement_id": item["requirement_id"], "covered": len(matches) == 1,
                "hash_present": hash_recorded, "file_valid": bool(asset and hash_recorded and self._audio_valid(asset)),
                "voice_matches": bool(asset and asset.get("voice") == requirements.get("voice")),
                "style_matches": bool(asset and self._audio_style_matches(asset, item)),
                "spoken_text_matches": bool(asset and asset.get("spoken_text_hash") == digest(item["spoken_text"]))})
        counts = {"required": len(required), "manifest_assets": len(assets),
            "covered": sum(row["covered"] for row in rows), "file_valid": sum(row["file_valid"] for row in rows),
            "hash_present": sum(row["hash_present"] for row in rows),
            "voice_matches": sum(row["voice_matches"] for row in rows),
            "style_matches": sum(row["style_matches"] for row in rows),
            "spoken_text_matches": sum(row["spoken_text_matches"] for row in rows),
            "duplicate_requirement_ids": len(required_ids) - len(set(required_ids)),
            "duplicate_asset_ids": sum(max(0, len(matches) - 1) for matches in actual.values()),
            "unexpected_assets": sum(len(matches) for key, matches in actual.items() if key not in set(required_ids))}
        if manifest and (manifest.get("requirements_ref") != prep.get("requirements_ref")
                or manifest.get("approval_ref") != prep.get("approval_ref")):
            blockers.append({"code": "AUDIO_MANIFEST_BINDING_MISMATCH"})
        if not required or any(not all(row[key] for key in ("covered", "file_valid", "voice_matches", "style_matches", "spoken_text_matches")) for row in rows) or any(counts[key] for key in ("duplicate_requirement_ids", "duplicate_asset_ids", "unexpected_assets")):
            blockers.append({"code": "AUDIO_COVERAGE_OR_FILE_INVALID"})
        # Classify against exact current dependencies, not the whole historical
        # ledger. Replaced writing remains unknown for billing and retry safety.
        from .job_outcome_dependencies import observe_job_outcomes
        job_outcomes = observe_job_outcomes(self, c, prep)
        unknown_count = len(job_outcomes["unknown_items"])
        counts["unknown_job_items"] = unknown_count
        counts["pending_jobs"] = len(job_outcomes["pending_job_ids"])
        if unknown_count:
            blockers.append({"code": "BUSINESS_JOB_OUTCOME_UNKNOWN", "item_count": unknown_count})
        if counts["pending_jobs"]:
            blockers.append({"code": "BUSINESS_JOB_PENDING", "job_count": counts["pending_jobs"]})
        limit = min(a.limit, 20)
        return {"preparation_ref": prep_record["ref"], "preparation_id": prep["id"],
            "generation_epoch": prep["generation_epoch"], "approval": approval,
            "approval_ref": prep.get("approval_ref"), "requirements_ref": prep.get("requirements_ref"),
            "audio_manifest_ref": prep.get("audio_manifest_ref"), "voice": (requirements or {}).get("voice"),
            "job_outcomes": job_outcomes,
            "counts": counts, "currentness": currentness, "blockers": blockers, "ready_for_publish": not blockers,
            "playback_statistics": playback_statistics(self._artifact(c, prep["compiled_plan_ref"])["body"]["entries"],
                [asset for asset in assets if asset.get("requirement_id") in {row["requirement_id"] for row in rows
                    if all(row[key] for key in ("covered", "hash_present", "file_valid", "voice_matches", "spoken_text_matches"))}]) if prep.get("compiled_plan_ref") else None,
            "items": rows[a.cursor:a.cursor + limit], "total": len(rows),
            "next_cursor": a.cursor + limit if a.cursor + limit < len(rows) else None,
            "reader_contract": "只读当前本场批准与音频就绪证据；未批准、未重试、未生成、未发布。发布时仍由业务后端重新校验。"}

    def _q_read_context(self, c, a, p, s):
        # A formal session draft is context, but is not a runtime history
        # snapshot. Both readers use the same exact-ref and preparation fence.
        result = self._read_ref_query(c, a, s, None)
        if result["ref"]["kind"] not in {"context", "draft"}:
            raise BusinessError("WRONG_ARTIFACT_KIND", details={"allowed_kinds": ["context", "draft"]})
        available = list(result["body"])
        if a.sections and set(a.sections) - set(available):
            raise BusinessError("CONTEXT_SECTION_INVALID", status=422,
                                details={"available_sections": available,
                                         "ref_kind": result["ref"]["kind"], "reader": "read_context"})
        if a.sections:
            result = {"ref": result["ref"], "body": {k: result["body"][k] for k in a.sections}}
        return {**result, "available_sections": available,
                "reader_contract": "正式context或draft精确引用；runtime_context快照使用read_run_history。"}

    def _q_read_script(self, c, a, p, s):
        result = self._read_ref_query(c, a, s, "script_revision")
        module_refs = result["body"]["module_refs"]
        if a.module_key is not None or a.sentence_ids is not None:
            if (not a.module_key or not a.sentence_ids
                    or a.sections not in ([], [a.module_key])):
                raise BusinessError("TEXT_SELECTION_QUERY_INVALID",
                    "句级读取须同时指定module_key和非空sentence_ids，不混用其他章节", status=422)
            prep = self._prep(c, result["body"]["preparation_id"])
            if result["ref"] not in prep["body"]["script_refs"]:
                raise BusinessError("SCRIPT_NOT_IN_PREPARATION")
            module_ref = module_refs.get(a.module_key)
            if not module_ref:
                raise BusinessError("MODULE_NOT_FOUND")
            module = self._artifact(c, module_ref, "module")["body"]
            from .script_patch_scope import selected_sentence_objects
            selected, _ = selected_sentence_objects(module["sentences"], a.sentence_ids)
            return {"ref": result["ref"], "body": deepcopy(result["body"]),
                "projection": "exact_sentence_selection_v1", "coverage": "selected_sentences",
                "selection": {"script_ref": deepcopy(result["ref"]), "module_key": a.module_key,
                    "module_ref": deepcopy(module_ref), "sentence_ids": [row["id"] for row in selected],
                    "selected_text_hash": digest(selected), "sentences": deepcopy(selected)},
                "notice": "selection仅为原句对象和原patch前置坐标，不是人工批准或写权限；未选句及其他模块不在修改范围。"}
        def with_edit_targets(view):
            # Public read metadata, not an edit grant. Hash the exact sentence
            # objects checked by patch_script, before any text projection.
            if "modules" in view:
                view["module_edit_targets"] = {key: {
                    "script_ref": deepcopy(result["ref"]), "module_key": key,
                    "module_ref": deepcopy(module_refs[key]), "sentence_ids": [],
                    "selected_text_hash": digest(module["sentences"])}
                    for key, module in view["modules"].items() if module is not None}
            return view
        if not a.sections:
            # Preserve artifacts and module bodies; edit coordinates are an
            # additive read-only field outside the hashed script body.
            result["modules"] = {k: self._artifact(c, r)["body"] if r else None for k, r in module_refs.items()}
            return with_edit_targets(result)
        # Validate only after the exact artifact and preparation fence above.
        # Never echo an arbitrary rejected section into public diagnostics.
        available = ["body", "module_refs", "modules", "spoken_text", "content_diagnostics", *MODULE_KEYS]
        sections = list(dict.fromkeys(a.sections))
        if any(key not in available for key in sections):
            raise BusinessError("CONTEXT_SECTION_INVALID", "请按话术支持的章节或模块名读取", status=422,
                                details={"available_sections": available, "ref_kind": "script_revision",
                                         "reader": "read_script"})
        if "spoken_text" in sections:
            selected = [key for key in sections if key in MODULE_KEYS] or list(module_refs)
            modules = {key: self._artifact(c, module_refs[key])["body"]
                       if module_refs.get(key) else None for key in selected}
            # Read projection only: source ref/hash still identifies the full
            # immutable record, never this smaller body. No model summary.
            view = {"ref": result["ref"], "body": {key: deepcopy(result["body"][key])
                    for key in ("preparation_id", "product_id", "product_ref", "variant_id", "version_number", "script_revision", "module_refs")
                    if key in result["body"]}, "projection": "exact_spoken_text_v1",
                "coverage": "full" if set(selected) == set(module_refs) else "selected_modules",
                "selected_sections": sections, "available_sections": available,
                "module_order": [key for key in MODULE_KEYS if key in selected and modules.get(key) is not None],
                "playback_note": "module_order是正式稿阅读顺序；长短开场互斥，实际按编排只选一种，历史transition不参与播报。",
                "module_texts": {key: {"source_module_ref": deepcopy(module_refs.get(key)),
                    "enabled": module is not None,
                    "spoken_text": "".join(row.get("spoken_text") or row["text"] for row in module["sentences"]) if module else ""}
                    for key, module in modules.items()},
                "module_edit_targets": {key: {"script_ref": deepcopy(result["ref"]), "module_key": key,
                    "module_ref": deepcopy(module_refs[key]), "sentence_ids": [],
                    "selected_text_hash": digest(module["sentences"])} for key, module in modules.items() if module},
                "reader": {"tool": "read_script", "args": {"ref": deepcopy(result["ref"]), "sections": ["modules"]}},
                "notice": "口播原文按模块完整拼接，未摘要或删句；本body是身份投影，ref仍指完整正式稿。局改哈希仍基于原句对象。"}
            if "content_diagnostics" in sections:
                view["content_diagnostics"] = deepcopy(result["body"].get("content_diagnostics", {}))
            return view
        if sections == ["content_diagnostics"]:
            return {"ref": result["ref"], "content_diagnostics": deepcopy(result["body"].get("content_diagnostics", {})),
                    "coverage": "diagnostics_only", "available_sections": available}
        selected = [key for key in sections if key in MODULE_KEYS]
        if not selected and "modules" in sections:
            selected = list(module_refs)
        # Explicit module keys narrow an accompanying 'modules' marker rather
        # than silently expanding every module. Missing optional keys are null.
        view = {**result, "coverage": "selected_modules" if selected else "metadata_only",
                "selected_sections": sections, "available_sections": available}
        if "module_refs" in sections and "body" not in sections:
            view["body"] = {"module_refs": deepcopy(module_refs)}
        if ("modules" in sections and not any(key in MODULE_KEYS for key in sections)
                and ("module_refs" not in sections or "body" in sections)):
            view["coverage"] = "full"
        if selected or "modules" in sections:
            view["modules"] = {key: self._artifact(c, module_refs[key])["body"]
                               if module_refs.get(key) else None for key in selected}
        return with_edit_targets(view)

    def _q_read_script_usage(self, c, a, p, s):
        if not a.ref:
            raise BusinessError("EXACT_REF_REQUIRED")
        script_ref = self._artifact(c, a.ref, "script_revision")["ref"]
        scheduled = []
        for session in self.store.all(c, "prelive_session"):
            plan = self._artifact(c, session["body"]["compiled_plan_ref"])["body"]
            if any(e["script_revision_ref"] == script_ref for e in plan["entries"]):
                scheduled.append(session["ref"])
        return {"script_ref": script_ref, "usage": "unknown", "last_played_at": None, "scheduled_session_refs": scheduled, "notice": "实际开播与排入队列不等于这版已播完；缺少逐句ACK关联时，不按创建或开播时间推断使用年龄"}

    def _q_voice_options(self, c, a, p, s):
        options = self.adapter.voice_options()
        if inspect.isawaitable(options):
            raise BusinessError("VOICE_OPTIONS_ADAPTER_MUST_BE_SYNC")
        return options

    def _q_validate_product_assembly(self, c, a, p, s):
        if a.schema_hash != self.schema()["schema_hash"]:
            raise BusinessError("SCHEMA_CHANGED", details={"schema_hash": self.schema()["schema_hash"]})
        try:
            value = Product.model_validate(a.fields).model_dump()
            return {"valid": True, "normalized_fields": value, "errors": []}
        except ValidationError as e:
            return {"valid": False, "errors": [{"path": "/" + "/".join(map(str, err["loc"])), "type": err["type"]} for err in e.errors()]}

    def _q_get_workspace_snapshot(self, c, a, p, s):
        cursor = c.execute("SELECT COALESCE(MAX(seq),0) FROM prelive_events").fetchone()[0]
        snapshot = {"voice_event_cursor": cursor, "products": self._q_search_products(c, a, p, s), "rooms": self.store.all(c, "config")}
        if s and s.kind == "preparation":
            snapshot["preparation"] = self._prep(c, s.preparation_id)
            snapshot["rooms"] = [r for r in snapshot["rooms"] if r["ref"]["id"] == snapshot["preparation"]["body"]["room_id"]]
        elif a.preparation_id:
            snapshot["preparation"] = self._prep(c, a.preparation_id)
        if snapshot.get("preparation"):
            prep = snapshot["preparation"]["body"]
            details = {}
            for key, output in (("playlist_ref", "playlist"), ("draft_ref", "draft"), ("asset_checklist_ref", "asset_checklist"), ("compiled_plan_ref", "compiled_plan"), ("requirements_ref", "requirements")):
                details[output] = self._artifact(c, prep[key], preparation_id=prep["id"]) if prep.get(key) else None
            details["scripts"] = []
            for ref in prep["script_refs"]:
                script = self._artifact(c, ref, "script_revision", prep["id"])
                script["modules"] = {key: self._artifact(c, value, "module")["body"] if value else None for key, value in script["body"]["module_refs"].items()}
                details["scripts"].append(script)
            snapshot["preparation_details"] = details
        return snapshot

    def _q_read_completion_observation(self, c, a, p, s):
        jobs = [r for r in self.store.all(c, "prelive_job") if not a.preparation_id or r["body"]["preparation_id"] == a.preparation_id]
        if p.kind == "agent":
            jobs = [r for r in jobs if r["body"].get("run_id") == p.run_id]
        for job in jobs:
            job["body"] = {k: v for k, v in job["body"].items()
                           if k not in {"principal", "mutation", "scope", "archive_revalidation_execution"}}
        result = {"jobs": jobs, "pending_job_ids": [r["ref"]["id"] for r in jobs if r["body"]["status"] in {"queued", "running"}], "unknown_job_ids": [r["ref"]["id"] for r in jobs if any(i["call_state"] == "timeout_unknown" for i in r["body"]["items"])], "candidate_ready": False}
        if a.preparation_id:
            prep = self._prep(c, a.preparation_id)
            result["preparation"] = prep
            if prep["body"]["candidate_publication_ref"]:
                try:
                    review = self._publication_review(c, prep["body"]["candidate_publication_ref"])
                    result.update(candidate_exists=True, candidate_ready=not review["currentness"]["blockers"], publication_ref=prep["body"]["candidate_publication_ref"], review=review)
                except BusinessError as e:
                    result["blocker"] = e.payload()
        return result

    def _q_read_job(self, c, a, p, s):
        if not a.job_id:
            raise BusinessError("JOB_REQUIRED", status=400)
        result = self.store.get(c, "prelive_job", a.job_id)
        self._assert_scope(c, p, s, result["body"]["preparation_id"])
        if p.kind == "agent" and result["body"]["run_id"] != p.run_id:
            raise BusinessError("JOB_RUN_MISMATCH", status=403)
        # Stored trusted principal/grant values are not part of user/model view.
        result["body"] = {k: v for k, v in result["body"].items()
                          if k not in {"principal", "mutation", "scope", "archive_revalidation_execution"}}
        # This is a query-only projection, never a ModuleRef or a replacement
        # for the immutable job/response body named by the returned references.
        # Resolve archives only through this already authorized job's attempts;
        # a supplied foreign receipt must not become a general response reader.
        from .module_generation import _safe_usage
        for item in result["body"].get("items", []):
            for attempt in item.get("provider_attempts", []):
                ref = attempt.get("provider_response_ref")
                if not ref or attempt.get("state") not in {"response_received", "result_received"}:
                    continue
                try:
                    archived = self.store.exact(c, ref)
                except BusinessError:
                    attempt["public_response_error"] = {"code": "PROVIDER_RESPONSE_REFERENCE_INVALID"}
                    continue
                body = archived["body"]
                if (archived["ref"]["kind"] != "provider_response" or not isinstance(body, dict)
                        or body.get("job_id") != a.job_id
                        or body.get("item_id") != item.get("item_id")
                        or body.get("attempt_id") != attempt.get("attempt_id")
                        or not isinstance(body.get("response"), dict)):
                    attempt["public_response_error"] = {"code": "PROVIDER_RESPONSE_LINK_MISMATCH"}
                    continue
                receipt = body["response"]
                expected = attempt.get("input", {}).get("module_key")
                if not isinstance(expected, str) or expected not in MODULE_KEYS:
                    expected = str(attempt.get("purpose", "")).removeprefix("copywriting.")
                module_key = receipt.get("module_key")
                validation = attempt.get("validation_state", "unknown")
                attempt["public_response"] = PublicProviderResponseView.model_validate({
                    "content": receipt.get("content") if isinstance(receipt.get("content"), str) else None,
                    "module_key": module_key if isinstance(module_key, str) and module_key in MODULE_KEYS else None,
                    "expected_module_key": expected if expected in MODULE_KEYS else None,
                    "receipt_ref": deepcopy(archived["ref"]),
                    "usage": _safe_usage(receipt.get("usage")),
                    "usage_state": receipt.get("usage_state") if isinstance(receipt.get("usage_state"), str) and receipt["usage_state"] in {"known", "partial", "pending", "unknown"} else "unknown",
                    "validation_state": validation if isinstance(validation, str) and validation in {"pending", "accepted", "rejected"} else "unknown",
                    "state": "response_received", "is_formal_module": False,
                    "reusable": False, "approvable": False,
                }).model_dump(mode="json")
                if receipt.get("protocol") == "whole-script-v1":
                    attempt["generation_response"] = {"protocol": "whole-script-v1",
                        "expected_module_keys": [key for key in attempt.get("input", {}).get("module_keys", []) if key in ACTIVE_MODULE_KEYS],
                        "response_module_keys": [key for key in receipt.get("module_keys", []) if key in ACTIVE_MODULE_KEYS],
                        "is_formal_script": False, "reusable": False, "approvable": False}
                    from .whole_script_generation import TEXT_CONTRACT
                    if receipt.get("text_contract") == TEXT_CONTRACT:
                        attempt["generation_response"]["text_contract"] = TEXT_CONTRACT
        result["body"]["public_response_contract"] = {
            "projection_only": True, "stored_reference_unchanged": True,
            "is_formal_module": False, "reusable": False, "approvable": False,
            "notice": "公开返回正文仅供核查和定向修改；不是正式模块，不能作为复用或批准依据。",
        }
        return result

    def _q_create_review(self, c, a, p, s):
        ref = a.ref or self._prep(c, a.preparation_id)["body"]["candidate_publication_ref"]
        if not ref:
            raise BusinessError("PUBLICATION_REQUIRED")
        return self._publication_review(c, ref)

    def _q_read_operation_receipt(self, c, a, p, s):
        import json
        if not a.operation_id:
            raise BusinessError("OPERATION_ID_REQUIRED")
        actor = a.principal_id or p.actor_id
        if actor != p.actor_id:
            raise BusinessError("FORBIDDEN", status=403)
        if p.kind in {"human", "agent"}:
            from .voice_preview import read_preview_receipt
            preview = read_preview_receipt(c, actor, a.operation_id)
            if preview is not None:
                if p.kind == "agent":
                    event = c.execute("SELECT payload FROM prelive_events WHERE json_extract(payload,'$.operation_id')=? ORDER BY seq LIMIT 1", (a.operation_id,)).fetchone()
                    if not event or json.loads(event[0]).get("run_id") != p.run_id:
                        raise BusinessError("OPERATION_RUN_MISMATCH", status=403)
                return preview
        row = c.execute("SELECT response_json,request_hash FROM prelive_operations WHERE principal_id=? AND operation_id=?", (actor, a.operation_id)).fetchone()
        if row and p.kind == "agent":
            event = c.execute("SELECT payload FROM prelive_events WHERE json_extract(payload,'$.operation_id')=? ORDER BY seq LIMIT 1", (a.operation_id,)).fetchone()
            if not event or json.loads(event[0]).get("run_id") != p.run_id:
                raise BusinessError("OPERATION_RUN_MISMATCH", status=403)
        return {"state": "committed", "receipt": json.loads(row[0]), "request_hash": row[1]} if row else {"state": "not_found", "does_not_prove_no_inflight_request": True}

    def _q_read_events(self, c, a, p, s):
        import json
        clauses, values = [], []
        if s and s.kind == "preparation":
            if a.scope_id and a.scope_id != s.preparation_id:
                raise BusinessError("SCOPE_CONFLICT", status=403)
            clauses.append("stream_id=?")
            values.append(s.preparation_id)
        elif s and s.kind == "catalog":
            if a.scope_id and a.scope_id != "catalog":
                raise BusinessError("SCOPE_CONFLICT", status=403)
            clauses.append("stream_id='catalog'")
        elif a.scope_id:
            if a.include_catalog and a.scope_id != "catalog":
                clauses.append("stream_id IN (?, 'catalog')")
            else:
                clauses.append("stream_id=?")
            values.append(a.scope_id)
        # Ascending cursor semantics remain unchanged for event consumers.
        # Descending cursor 0 means latest, subsequent pages move to older seqs.
        if a.order == "asc" or a.cursor:
            clauses.append("seq>?" if a.order == "asc" else "seq<?")
            values.append(a.cursor)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if a.order == "desc" else "ASC"
        rows = c.execute("SELECT * FROM prelive_events" + where + " ORDER BY seq " + direction + " LIMIT ?", (*values, a.limit + 1)).fetchall()
        more, rows = len(rows) > a.limit, rows[:a.limit]
        return {"items": [{"seq": r["seq"], "event_id": r["event_id"], "stream_id": r["stream_id"], "event_type": r["event_type"], "payload": json.loads(r["payload"]), "occurred_at": r["created_at"]} for r in rows], "next_cursor": rows[-1]["seq"] if rows else a.cursor, "has_more": more, "order": a.order}

    def _q_read_blob(self, c, a, p, s):
        if a.ref and a.ref.kind == "source_manifest":
            source = self._read_ref_query(c, a, s, "source_manifest")
            return {"ref": source["ref"], "manifest": source["body"]["manifest"], "original_blob_ref": source["body"]["original_blob_ref"], "origin_source_ref": source["body"]["origin_source_ref"]}
        blob = self._read_ref_query(c, a, s, "blob")
        path = self.runtime / blob["body"]["relative_path"]
        raw = path.read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != blob["body"]["sha256_bytes"]:
            raise BusinessError("BLOB_HASH_MISMATCH")
        end = min(a.byte_end if a.byte_end is not None else len(raw), a.byte_start + 256 * 1024)
        value = raw[a.byte_start:end]
        result = {"ref": blob["ref"], "mime": blob["body"]["mime"], "size": len(raw), "start": a.byte_start, "end": end, "content_base64": base64.b64encode(value).decode("ascii")}
        if blob["body"]["mime"] in {"text/plain", "text/markdown", "text/csv", "application/json"}:
            result["text"] = value.decode("utf-8", errors="replace")
        return result
