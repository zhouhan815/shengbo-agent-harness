import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.execution import InstanceLock, ExecutionConflict
from shengbo_prelive.models import AnalysisReport, RunScope
from shengbo_prelive.runtime import RuntimeEngine, CURRENT_SCOPE, CURRENT_ATTEMPT


class FakeClient:
    def __init__(self):
        self.commands, self.queries, self.closed = [], [], False
        self.receipt_state = {"state": "not_found", "does_not_prove_no_inflight_request": True}
        self.observation = {}
    async def close(self):
        self.closed = True
    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, args, kwargs))
        if key == "read_operation_receipt":
            return self.receipt_state
        if key == "read_completion_observation":
            return self.observation
        return {}
    async def command(self, key, args, mutation, **kwargs):
        operation = mutation.operation_id if hasattr(mutation, "operation_id") else mutation["operation_id"]
        self.commands.append((key, args, operation, kwargs))
        data = {"grant_id": "grant1", "write_generation": 1} if key == "issue_grant" else {"run_id": args.get("run_id"), "revoked": True}
        if key == "import_blob":
            data = {"blob_ref": {"kind": "blob", "id": "blob1", "revision": 1, "content_hash": args["expected_sha256"]}}
        if key == "accept_analysis_report":
            data = {"analysis_ref": {"kind": "analysis", "id": "analysis1", "revision": 1, "content_hash": canonical_hash(args["report"])},
                    "accepted_report_hash": canonical_hash(args["report"]), "source_ref_replacements": []}
        return {"command": key, "operation_id": operation, "status": "committed", "data": data}


class RuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = FakeClient()
        self.engine = RuntimeEngine(self.root, self.client, model_factory=lambda **kwargs: None)
    async def asyncTearDown(self):
        await self.engine.stop()
        self.temporary.cleanup()
    def saved_run(self, identity="run1", **changes):
        source = {"text": "分析商品资料", "material_refs": []}
        scope = {"kind": "catalog", "account_id": "local", "preparation_id": None}
        goal = {"objective": "facts_analysis", "summary": "分析商品资料", "allowed_effects": ["import_blob", "accept_analysis_report"],
                "target_product_ids": [], "scope": scope, "original_input_hash": canonical_hash(source)}
        return self.engine.store.put("run", {"id": identity, "conversation_id": "conversation1", "scope": scope,
            "scope_key": "scope1", "input": source, "status": "RUNNING", "phase": "execute", "dispatch_frozen": False,
            "goal": goal, "allowed_effects": goal["allowed_effects"], "target_product_ids": [], "receipts": [],
            "grant_id": "grant1", "write_generation": 1, **changes})
    async def test_constructor_excludes_second_writer_before_migration_and_stop_without_start_releases(self):
        with self.assertRaises(ExecutionConflict):
            RuntimeEngine(self.root, FakeClient())
        await self.engine.stop()
        self.assertTrue(self.client.closed)
        with InstanceLock(self.root):
            pass
    async def test_start_failure_closes_saver_and_client_releases_lock(self):
        def fail(**kwargs):
            raise RuntimeError("mock construction failure")
        self.engine._model_factory = fail
        with self.assertRaises(RuntimeError):
            await self.engine.start()
        self.assertTrue(self.client.closed)
        with InstanceLock(self.root):
            pass
    async def test_authorizing_goal_recovery_reuses_original_scope_and_operation_id(self):
        run = self.saved_run(status="AUTHORIZING", phase="intent", grant_id=None, write_generation=None)
        goal = run["goal"]
        await self.engine._resume_authorization(run)
        updated = self.engine.store.get("run", "run1")
        self.assertEqual(updated["goal"], goal)
        self.assertEqual(updated["phase"], "execute")
        self.assertEqual(updated["grant_id"], "grant1")
        self.assertEqual(self.client.commands[0][2], "run1_grant")
        await self.engine._resume_authorization(updated)
        self.assertEqual(len(self.client.commands), 1)
    async def test_unknown_operation_positive_receipt_reconciles_without_replay(self):
        run = self.saved_run(pending_unknown_operation="op1")
        self.engine.store.put("operation", {"id": "op1", "run_id": "run1", "key": "save_product", "args": {}, "status": "unknown"})
        self.client.receipt_state = {"state": "committed", "receipt": {"operation_id": "op1", "command": "save_product", "data": {"product_ref": {"id": "new_product"}}}}
        self.assertTrue(await self.engine._reconcile_pending_operation(run))
        updated = self.engine.store.get("run", "run1")
        self.assertIsNone(updated["pending_unknown_operation"])
        self.assertEqual(updated["created_product_ids"], ["new_product"])
        self.assertEqual(len(updated["receipts"]), 1)
        self.assertEqual(self.client.commands, [])
    async def test_definite_business_rejection_is_known_failed_and_does_not_trap_finalizer(self):
        self.saved_run()
        async def rejected(*args, **kwargs):
            raise ContractError("REVISION_CONFLICT", "冲突", status=409)
        original = self.client.command
        self.client.command = rejected
        with self.assertRaises(ContractError):
            await self.engine.business_command(RunScope("run1", "invoke1", "execute"), "import_blob", {
                "content_base64": base64.b64encode(b"source").decode(), "expected_sha256": "sha256:" + "1" * 64}, "call_rejected")
        operations = self.engine.store.list("operation")
        self.assertEqual(operations[0]["status"], "known_failed")
        self.assertFalse(self.engine.store.get("run", "run1").get("pending_unknown_operation"))
        self.client.command = original
        await self.engine._finalize("run1", "FAILED", "known_rejection")
        self.assertEqual(self.engine.store.get("run", "run1")["status"], "FAILED")
    async def test_cancelled_business_request_stays_unknown(self):
        self.saved_run()
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        self.client.command = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.engine.business_command(RunScope("run1", "invoke1", "execute"), "import_blob", {
                "content_base64": base64.b64encode(b"source").decode(), "expected_sha256": "sha256:" + "1" * 64}, "call_cancelled")
        operation = self.engine.store.list("operation")[0]
        self.assertEqual(operation["status"], "unknown")
        self.assertEqual(self.engine.store.get("run", "run1")["pending_unknown_operation"], operation["id"])
    async def test_negative_read_does_not_prove_nonexecution_or_release_slot(self):
        run = self.saved_run(pending_unknown_operation="op1")
        self.engine.store.acquire_scope(run)
        self.engine.store.put("operation", {"id": "op1", "run_id": "run1", "key": "save_product", "args": {}, "status": "sent"})
        await self.engine._finalize("run1", "FAILED", "unknown_http")
        current = self.engine.store.get("run", "run1")
        self.assertEqual(current["status"], "FINALIZING")
        self.assertEqual(current["finalization_phase"], "business_operation_unknown")
        self.assertFalse(self.engine.store.acquire_scope({"id": "run2", "scope_key": "scope1"}))
        self.assertEqual([item[0] for item in self.client.commands], ["revoke_grant"])
    async def test_model_observer_distinguishes_pre_send_known_failed_and_completed(self):
        self.saved_run()
        scope_token = CURRENT_SCOPE.set(RunScope("run1", "invoke1", "execute"))
        attempt_token = None
        try:
            ticket = self.engine.store.reserve_attempt("run1", "invoke1", "main")
            attempt_token = CURRENT_ATTEMPT.set(ticket["id"])
            self.engine._model_event({"type": "model.failed", "attempt_id": "wire1", "code": "catalog_error", "outcome_unknown": True})
            with self.engine.store.tx() as connection:
                value = json.loads(connection.execute("SELECT body FROM attempts WHERE id=?", (ticket["id"],)).fetchone()[0])
            self.assertEqual(value["state"], "aborted_before_send")
            ticket2 = self.engine.store.reserve_attempt("run1", "invoke1", "main")
            CURRENT_ATTEMPT.set(ticket2["id"])
            self.engine._model_event({"type": "model.sent", "attempt_id": "wire2"})
            self.engine._model_event({"type": "model.protocol", "shape": {}})
            self.engine._model_event({"type": "model.failed", "attempt_id": "wire2", "outcome_unknown": False, "http_status": 401})
            with self.engine.store.tx() as connection:
                value = json.loads(connection.execute("SELECT body FROM attempts WHERE id=?", (ticket2["id"],)).fetchone()[0])
            self.assertEqual(value["state"], "known_failed")
        finally:
            if attempt_token:
                CURRENT_ATTEMPT.reset(attempt_token)
            CURRENT_SCOPE.reset(scope_token)
    async def test_global_cursor_has_no_run_or_200_event_truncation_and_is_durable(self):
        for number in range(230):
            self.engine.trace.emit("old_run", "step.progress", {"number": number})
        page = self.engine.all_events()
        self.assertEqual(len(page["events"]), 200)
        next_page = self.engine.all_events(page["next_cursor"])
        self.assertEqual(len(next_page["events"]), 30)
        cursor = next_page["next_cursor"]
        self.engine.trace.emit("old_run", "step.done", {})
        self.assertEqual(len(self.engine.all_events(cursor)["events"]), 1)
        await self.engine.stop()
        replacement = RuntimeEngine(self.root, FakeClient())
        try:
            self.assertEqual(len(replacement.all_events(cursor)["events"]), 1)
        finally:
            await replacement.stop()
    async def test_material_import_rebound_from_trusted_manifest_and_bytes(self):
        accepted = self.engine.materials.accept("conversation1", "product.txt", "text/plain", b"original product facts")
        source_ref = accepted["material_ref"]
        source = {"text": "分析商品资料", "material_refs": [source_ref]}
        run = self.saved_run(input=source)
        run = self.engine.store.update("run", run["id"], goal={**run["goal"], "original_input_hash": canonical_hash(source)})
        report = AnalysisReport(summary="资料分析", source_refs=[source_ref]).model_dump(mode="json")
        self.engine.store.put("branch", {"id": "branch1", "run_id": "run1", "role": "product_facts", "status": "PREPARED",
            "task": {"product_ids": [], "source_refs": [source_ref], "input_refs": []}, "report": report, "report_hash": canonical_hash(report)})
        result = await self.engine.accept_reports(RunScope("run1", "invoke1", "execute"), ["branch1"], "call1")
        self.assertEqual(len(result["accepted_refs"]), 1)
        imported, accepted_call = self.client.commands
        self.assertEqual(imported[0], "import_blob")
        self.assertEqual(base64.b64decode(imported[1]["content_base64"]), b"original product facts")
        self.assertEqual(accepted_call[1]["source_manifests"][0]["source_ref"], source_ref)
        self.assertEqual(accepted_call[1]["source_manifests"][0]["original_blob_ref"]["kind"], "blob")
        self.assertEqual(accepted_call[1]["prepared_report_ref"]["content_hash"], canonical_hash(report))

    async def test_interleaved_runs_global_sse_and_restart_use_global_cursor(self):
        from shengbo_prelive.api import create_app
        self.engine.trace.emit("runA", "step.progress", {"step": "a1"})
        self.engine.trace.emit("runB", "step.progress", {"step": "b1"})
        self.engine.trace.emit("runA", "step.progress", {"step": "a2"})
        page = self.engine.all_events()
        self.assertEqual([item["stream_sequence"] for item in page["events"]], [1, 2, 3])
        self.assertEqual([item["event"]["sequence"] for item in page["events"]], [1, 1, 2])
        await self.engine.stop()
        reopened = RuntimeEngine(self.root, FakeClient())
        try:
            reopened.trace.emit("runB", "step.progress", {"step": "b2"})
            app = create_app(reopened, "a-safe-service-test-credential-long-enough")
            endpoint = next(route.endpoint for route in app.routes if route.path == "/api/assistant/v2/events")
            class Request:
                checks = 0
                async def is_disconnected(self):
                    self.checks += 1
                    return self.checks > 1
            response = await endpoint(Request(), run_id=None, after=3, agent_after=None, last_event_id=None)
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(len(chunks), 1)
            self.assertIn("id: 4\n", chunks[0])
            data_line = next(line[6:] for line in chunks[0].splitlines() if line.startswith("data: "))
            event = json.loads(data_line)
            self.assertEqual(event["run_id"], "runB")
            self.assertEqual(event["sequence"], 2)
            self.assertEqual(event["data"]["step"], "b2")
        finally:
            await reopened.stop()


if __name__ == "__main__":
    unittest.main()
