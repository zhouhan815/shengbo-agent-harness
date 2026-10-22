"""Offline import_blob transport/archive tests; no provider or live service."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path

import pytest

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.client import RemoteOutcomeUnknown
from shengbo_prelive.models import RunScope
from shengbo_prelive.operation_payloads import (
    STORAGE, import_blob_request_hash, persist_import_blob_args, load_wire_args, validate_import_blob_projection,
)
from shengbo_prelive.runtime import RuntimeEngine


SCOPE = WorkScope(kind="catalog").model_dump(mode="json")


def args_for(data=b"source"):
    return {"content_base64": base64.b64encode(data).decode(), "filename": "产品资料.xlsx", "mime": "application/octet-stream",
            "expected_sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


@pytest.mark.parametrize("value", ["small", ('a"b\\c\n中文' * 150000)], ids=["small", "chunk-boundary-unicode-escaping"])
def test_stream_hash_and_archive_are_exact_canonical_args(tmp_path, value):
    args = {**args_for(), "content_base64": value}
    digest = canonical_hash(args)
    assert import_blob_request_hash(args) == digest
    operation, metrics = persist_import_blob_args(tmp_path, "op_" + "a" * 32, "run", args, SCOPE)
    assert operation["request_hash"] == operation["wire_payload_ref"]["content_hash"] == digest
    normalized = WorkScope.model_validate(SCOPE).model_dump(mode="json", exclude_none=True)
    assert operation["remote_request_hash"] == canonical_hash({"command": "import_blob", "args": args, "scope": normalized})
    assert "content_base64" not in operation["args"]
    assert load_wire_args(tmp_path, operation) == args
    assert metrics["wire_payload_bytes"] == len(canonical_json(args).encode())


def test_metadata_forgery_and_changed_payload_are_rejected(tmp_path):
    operation, _ = persist_import_blob_args(tmp_path, "op_" + "b" * 32, "run", args_for(), SCOPE)
    for patch in [{"request_hash": "sha256:" + "0" * 64}, {"args": {"filename": "wrong"}},
                  {"wire_payload_ref": {**operation["wire_payload_ref"], "id": "../../escape"}}]:
        with pytest.raises(ContractError):
            validate_import_blob_projection(tmp_path, {**operation, **patch})
    wire = next((tmp_path / "operation_wire_payloads").glob("*.json"))
    raw = wire.read_bytes()
    wire.write_bytes(raw.replace(b"source", b"change") if b"source" in raw else b"X" + raw[1:])
    with pytest.raises(ContractError, match="OPERATION_PAYLOAD_HASH_MISMATCH"):
        load_wire_args(tmp_path, operation)


class WireClient:
    def __init__(self, unknown=False):
        self.commands = []
        self.queries = []
        self.unknown = unknown
        self.last_args = self.receipt = self.remote_hash = None
    async def close(self):
        pass
    async def command(self, key, args, mutation, *, scope=None, **kwargs):
        self.commands.append(key)
        self.last_args = deepcopy(args)
        self.receipt = {"command": key, "operation_id": mutation.operation_id, "status": "committed",
                        "data": {"blob_ref": {"kind": "blob", "id": "blob-test", "revision": 1, "content_hash": args["expected_sha256"]}}}
        self.remote_hash = canonical_hash({"command": key, "args": args,
                                          "scope": WorkScope.model_validate(scope).model_dump(mode="json", exclude_none=True)})
        if self.unknown:
            raise RemoteOutcomeUnknown(mutation.operation_id)
        return self.receipt
    async def query(self, key, args=None, **kwargs):
        self.queries.append(key)
        if key == "read_operation_receipt":
            return {"state": "committed", "receipt": self.receipt, "request_hash": self.remote_hash}
        return {}


def save_run(engine, identity="wire-run", materials=None):
    source = {"text": "分析商品资料", "material_refs": materials or []}
    return engine.store.put("run", {"id": identity, "conversation_id": "wire-conversation", "scope": SCOPE,
        "scope_key": "wire-scope", "input": source, "status": "RUNNING", "phase": "execute", "dispatch_frozen": False,
        "goal": {"objective": "facts_analysis", "scope": SCOPE}, "allowed_effects": ["import_blob"],
        "target_product_ids": [], "receipts": [], "grant_id": "fixture-grant", "write_generation": 1})


@pytest.mark.asyncio
async def test_transport_keeps_exact_args_while_operation_is_small_and_idempotent(tmp_path):
    client = WireClient()
    engine = RuntimeEngine(tmp_path, client, model_factory=lambda **kwargs: None)
    try:
        save_run(engine)
        args = args_for(b"merchant" * 160000)
        scope = RunScope("wire-run", "wire-invocation", "execute")
        receipt = await engine.business_command(scope, "import_blob", args, "same-call")
        same = await engine.business_command(scope, "import_blob", args, "same-call")
        assert same == receipt and client.commands == ["import_blob"]
        assert client.last_args == args
        operation = engine.store.list("operation")[0]
        assert operation["args_storage"] == STORAGE
        assert operation["request_hash"] == canonical_hash(args)
        assert len(canonical_json(operation)) < 4000
        assert load_wire_args(engine.root, operation) == args
        with pytest.raises(ContractError, match="OPERATION_CHANGED"):
            await engine.business_command(scope, "import_blob", {**args, "filename": "different.xlsx"}, "same-call")
        events = engine.trace.events("wire-run")["events"]
        timing = next(e["data"] for e in events if e["type"] == "material.blob.transport")
        assert {"wire_persist_ms", "operation_persist_ms", "send_ms", "pre_send_ms"} <= set(timing)
        assert not any("content_base64" in canonical_json(e["data"]) and args["content_base64"] in canonical_json(e["data"]) for e in events)
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_unknown_import_reconciles_original_receipt_without_replay_or_hydration(tmp_path, monkeypatch):
    client = WireClient(unknown=True)
    engine = RuntimeEngine(tmp_path, client, model_factory=lambda **kwargs: None)
    try:
        save_run(engine)
        with pytest.raises(RemoteOutcomeUnknown):
            await engine.business_command(RunScope("wire-run", "invoke", "execute"), "import_blob", args_for(), "unknown-call")
        operation = engine.store.list("operation")[0]
        assert operation["status"] == "unknown" and operation["args_storage"] == STORAGE
        monkeypatch.setattr("shengbo_prelive.operation_payloads._file_hash", lambda *args: pytest.fail("reconciliation must not hydrate or hash entire wire file"))
        assert await engine._reconcile_pending_operation(engine.store.get("run", "wire-run"))
        assert client.commands == ["import_blob"] and client.queries == ["read_operation_receipt"]
        assert engine.store.get("operation", operation["id"])["status"] == "committed"
        assert engine.store.get("run", "wire-run")["pending_unknown_operation"] is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_hash',[None,'sha256:'+'0'*64],ids=['missing','wrong'])
async def test_committed_import_receipt_without_exact_wire_hash_stays_unknown(tmp_path,remote_hash):
    client=WireClient(unknown=True)
    engine=RuntimeEngine(tmp_path,client,model_factory=lambda **kwargs:None)
    try:
        save_run(engine)
        with pytest.raises(RemoteOutcomeUnknown):
            await engine.business_command(RunScope('wire-run','invoke','execute'),'import_blob',args_for(),'unknown-hash')
        operation=engine.store.list('operation')[0]
        client.remote_hash=remote_hash
        with pytest.raises(ContractError,match='OPERATION_RECEIPT_MISMATCH'):
            await engine._reconcile_pending_operation(engine.store.get('run','wire-run'))
        assert engine.store.get('operation',operation['id'])['status']=='unknown'
        assert engine.store.get('run','wire-run')['pending_unknown_operation']==operation['id']
        assert client.commands==['import_blob']
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_ancestor_index_keeps_verified_external_payload_reference_without_loading(tmp_path, monkeypatch):
    client = WireClient()
    engine = RuntimeEngine(tmp_path, client, model_factory=lambda **kwargs: None)
    try:
        owner = save_run(engine, "owner")
        await engine.business_command(RunScope("owner", "invoke", "execute"), "import_blob", args_for(), "owned-import")
        owner = engine.store.get("run", "owner")
        current = save_run(engine, "child")
        monkeypatch.setattr(engine, "_source_binding_owner_runs", lambda run: {"owner": owner, "child": current})
        monkeypatch.setattr(engine, "_related_evidence_owner_runs", lambda run: {"owner": owner, "child": current})
        monkeypatch.setattr("shengbo_prelive.operation_payloads._file_hash", lambda *args: pytest.fail("hot index must not read payload"))
        index = engine._related_evidence_index(current)
        assert index["operations"]["total"] == 1
        full_ref = index["operations"]["items"][0]["operation_ref"]
        snapshot = engine.store.get("context_snapshot", full_ref["id"])["body"]
        assert snapshot["document"]["operation"]["args_storage"] == STORAGE
        assert snapshot["document"]["operation"]["wire_payload_ref"]["kind"] == "import_blob_wire_payload"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_source_bridge_records_read_encode_transport_and_persist_timing(tmp_path):
    client = WireClient()
    engine = RuntimeEngine(tmp_path, client, model_factory=lambda **kwargs: None)
    try:
        ref = engine.materials.accept("wire-conversation", "资料.txt", "text/plain", "商品原资料".encode())["material_ref"]
        save_run(engine, materials=[ref])
        scope = RunScope("wire-run", "invoke", "execute")
        result = await engine._import_uploaded_source(scope, ref)
        reused = await engine._import_uploaded_source(scope, ref)
        assert result["blob_ref"] == reused["blob_ref"] and client.commands == ["import_blob"]
        events = engine.trace.events("wire-run")["events"]
        timing = next(e["data"] for e in events if e["type"] == "material.source_import.timing")
        assert {"manifest_read_ms", "blob_read_verify_ms", "base64_encode_ms", "business_command_ms", "total_ms"} <= set(timing)
        assert any(e["type"] == "material.blob.persisted" for e in events)
    finally:
        await engine.stop()
