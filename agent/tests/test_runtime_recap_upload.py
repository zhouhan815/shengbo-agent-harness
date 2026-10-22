"""Uploaded recap uses trusted original bytes, not model-authored base64.

Runs the real voice service and shared DTOs against temporary databases only.
No provider, TTS, live room or user catalog is accessed.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import sys

import pytest

VOICE_ROOT = Path("D:/Codex project/ecommerce-voice-agent")
if str(VOICE_ROOT) not in sys.path:
    sys.path.insert(0, str(VOICE_ROOT))
from app.prelive import PreliveService
from app.prelive.storage import BusinessError
from shengbo_contracts.common import ContractError, MutationContext, Principal, WorkScope, canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import RuntimeEngine


class IsolatedBusinessClient:
    def __init__(self, service):
        self.service = service
        self.commands = []

    async def command(self, key, args, mutation, *, scope=None, run_id=None, controller=False):
        self.commands.append((key, deepcopy(args)))
        principal = Principal(kind="controller" if controller else "agent",
                              actor_id="controller-runtime" if controller else "agent-runtime", run_id=run_id)
        try:
            return await self.service.execute_command(key, args, mutation, principal, scope=scope)
        except BusinessError as error:
            raise ContractError(error.code, error.message, status=error.status, details=error.details) from None

    async def close(self):
        await self.service.close()


async def human(service, key, args):
    return await service.execute_command(key, args,
        MutationContext(operation_id="human-" + key + "-" + canonical_hash(args)[7:23]),
        Principal(kind="human"))


async def setup_run(engine, service, source_ref):
    room = (await human(service, "create_room", {"name": "复盘隔离测试"}))["data"]["room_ref"]
    prep = (await human(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    scope = WorkScope(kind="preparation", preparation_id=prep["id"]).model_dump(mode="json")
    effects = ["import_blob", "import_recap", "link_recap_to_preparation"]
    source = {"text": "导入这份模拟复盘并关联本场，暂不改话术", "material_refs": [source_ref]}
    grant = await service.execute_command("issue_grant", {
        "run_id": "recap-run", "scope": scope, "capabilities": effects, "target_constraints": []},
        MutationContext(operation_id="recap-run-grant"),
        Principal(kind="controller", actor_id="controller-runtime", run_id="recap-run"))
    engine.store.put("run", {"id": "recap-run", "conversation_id": "conversation1", "scope": scope,
        "scope_key": canonical_hash(scope), "input": source, "status": "RUNNING", "phase": "execute",
        "dispatch_frozen": False, "allowed_effects": effects, "receipts": [], "target_product_ids": [],
        "goal": {"objective": "prepare_session", "allowed_effects": effects, "scope": scope,
                 "original_input_hash": canonical_hash(source)},
        "grant_id": grant["data"]["grant_id"], "write_generation": grant["data"]["write_generation"]})
    return prep


@pytest.mark.asyncio
async def test_uploaded_recap_import_and_link_use_exact_original_bytes_and_lineage(tmp_path):
    service = PreliveService(tmp_path / "voice")
    client = IsolatedBusinessClient(service)
    engine = RuntimeEngine(tmp_path / "agent", client)
    try:
        quote = "建议将二号商品放到开头讲解；这是模拟复盘，尚未采纳。"
        raw = ("# 测试复盘\n" + quote + "\n").encode("utf-8")
        source_ref = engine.materials.accept("conversation1", "模拟复盘.md", "text/markdown", raw)["material_ref"]
        prep = await setup_run(engine, service, source_ref)
        scope = RunScope("recap-run", "recap-invocation", "execute")
        document = {"synthetic": True, "origin": {"synthetic": True},
                    "findings": [{"finding_id": "finding1", "suggestion": quote, "adoption": "pending"}],
                    "evidence": [{"evidence_id": "evidence1", "synthetic": True, "quote": quote,
                                  "content_hash": "sha256:" + hashlib.sha256(quote.encode()).hexdigest()}],
                    # Forged lineage must be overwritten with trusted material metadata.
                    "trusted_upload_lineage": {"source_material_ref": "forged"}}
        args = {"raw_blob_ref": source_ref, "document": document, "synthetic": True}
        imported = await engine.business_command(scope, "import_recap", args, "import-recap-once")
        same = await engine.business_command(scope, "import_recap", args, "import-recap-once")
        assert same == imported
        recap_ref = imported["data"]["recap_ref"]
        with service.store.transaction() as connection:
            recap = service.store.exact(connection, recap_ref)["body"]
            blob = service.store.exact(connection, recap["raw_blob_ref"])["body"]
        assert recap["raw_blob_ref"]["kind"] == "blob"
        assert (service.runtime / blob["relative_path"]).read_bytes() == raw
        lineage = recap["document"]["trusted_upload_lineage"]
        assert lineage["source_material_ref"] == source_ref
        assert lineage["original_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert lineage["source_manifest_hash"] == source_ref["content_hash"]
        assert [key for key, _ in client.commands].count("import_blob") == 1
        assert [key for key, _ in client.commands].count("import_recap") == 1
        linked = await engine.business_command(scope, "link_recap_to_preparation", {
            "preparation_id": prep["id"], "expected_revision": prep["revision"], "recap_refs": [recap_ref]}, "link-recap-once")
        current = await service.query("read_preparation", {"preparation_id": prep["id"]})
        assert current["body"]["recap_refs"] == [recap_ref]
        assert linked["data"]["preparation_ref"] == current["ref"]
        events = engine.trace.events("recap-run")["events"]
        assert [event["type"] for event in events if event["type"].startswith("toolflow.")] == ["toolflow.started", "toolflow.completed"]
        assert events[-1]["data"]["source_material_ref"] == source_ref
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_recap_bridge_rejects_unbound_material_and_intent_before_import(tmp_path):
    service = PreliveService(tmp_path / "voice")
    client = IsolatedBusinessClient(service)
    engine = RuntimeEngine(tmp_path / "agent", client)
    try:
        owned = engine.materials.accept("conversation1", "owned.txt", "text/plain", b"owned")["material_ref"]
        foreign = engine.materials.accept("conversation2", "foreign.txt", "text/plain", b"foreign")["material_ref"]
        await setup_run(engine, service, owned)
        args = {"raw_blob_ref": foreign, "document": {"findings": [{"id": "f"}]}}
        with pytest.raises(ContractError, match="SOURCE_SCOPE_DENIED"):
            await engine.business_command(RunScope("recap-run", "execute1", "execute"), "import_recap", args, "forbidden-upload")
        assert client.commands == []
        engine.store.update("run", "recap-run", phase="intent", status="ASSESSING_INTENT")
        with pytest.raises(ContractError, match="GOAL_EFFECT_DENIED"):
            await engine.business_command(RunScope("recap-run", "intent1", "intent"), "import_recap", {**args, "raw_blob_ref": owned}, "intent-import")
        assert client.commands == []
    finally:
        await engine.stop()
