"""Isolated v2 business tests: no server import, secrets, provider or live data."""
import asyncio
import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
import wave

from shengbo_contracts.common import Principal, MutationContext, WorkScope
from shengbo_contracts.business import COMMAND_ARGS, QUERY_NAMES, MODULE_KEYS, AnalysisReport
from app.prelive import PreliveService, BusinessError
from app.prelive.service import Product
from app.prelive.playback_compiler import reorder, compile_items
from app.prelive.storage import digest
from shengbo_contracts.common import CommandReceipt, canonical_hash


class FakeAdapter:
    def __init__(self, root):
        self.root = Path(root)
        self.generated = []
        self.rendered = []
        self.starts = []
        self.pause = None

    async def generate_module(self, context, module_key, product, previous):
        self.generated.append(module_key)
        if self.pause:
            await self.pause.wait()
        return [product["name"] + "：" + module_key + "的演示话术。"]

    async def render_audio(self, text, room_id, voice):
        self.rendered.append(text)
        identity = hashlib.sha256((text + str(voice)).encode()).hexdigest()
        path = self.root / "room_audio" / room_id / (identity + ".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\0\0" * 2205)
        return {"path": str(path), "audio_id": identity, "duration": .1, "url": "/test-audio/" + identity + ".wav"}

    async def start_publication(self, room_id, publication, compiled_entries):
        self.starts.append(compiled_entries)
        return {"session_id": "test-session", "room_id": room_id}

    def voice_options(self):
        return {"providers": ["system"], "voices": ["Huihui"]}


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter(self.temp.name)
        self.service = PreliveService(self.temp.name, self.adapter)
        self.human = Principal(kind="human")
        self.controller = Principal(kind="controller", actor_id="controller")
        self.counter = 0

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, command, args, principal=None, scope=None, operation=None, grant=None):
        self.counter += 1
        mutation = MutationContext(operation_id=operation or "op-" + str(self.counter), grant_id=grant["grant_id"] if grant else None, expected_write_generation=grant["write_generation"] if grant else None)
        return await self.service.execute_command(command, args, mutation, principal or self.human, scope=scope)

    async def product(self, name="测试拖鞋"):
        fields = Product(name=name, price=178, selling_points="舒适轻便").model_dump()
        args = {"schema_hash": self.service.schema()["schema_hash"], "patches": [{"path": "/name", "op": "set", "value": name}, {"path": "/price", "op": "set", "value": 178}], "fields": fields}
        return (await self.cmd("save_product", args))["data"]["product_ref"]

    async def prep(self):
        room = (await self.cmd("create_room", {"name": "测试直播间"}))["data"]["room_ref"]
        prep = (await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
        return room, prep

    async def state(self, prep):
        return await self.service.query("read_preparation", {"preparation_id": prep["id"]})

    async def revision_plan(self, prep):
        state = await self.state(prep)
        await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "bundle_ref": state["body"]["bundle_ref"], "items": [{"product_ref": state["body"]["selected_product_refs"][0], "variant_id": "variant-a1", "version_number": 1, "base_script_ref": state["body"]["script_refs"][0], "modules": [{"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate", "instruction": "本次明确采用新表达，验证新请求的恢复边界。"} for key in MODULE_KEYS]}]})
        return await self.state(prep)

    async def prepared_text(self, *, loop_mode="once"):
        product = await self.product()
        room, prep = await self.prep()
        result = await self.cmd("update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [product]})
        prep = result["data"]["preparation_ref"]
        draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "draft": {"theme": "日常介绍", "commerce": {}}})
        prep = draft["data"]["preparation_ref"]
        context = await self.cmd("save_context_bundle", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "product_refs": [product], "draft_ref": draft["data"]["draft_ref"]})
        prep = context["data"]["preparation_ref"]
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "bundle_ref": context["data"]["context_bundle_ref"], "items": [{"product_ref": product, "variant_id": "variant-a1", "modules": [{"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate"} for key in MODULE_KEYS]}]})
        prep = plan["data"]["preparation_ref"]
        scripts = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "plan_ref": plan["data"]["script_plan_ref"], "context_bundle_ref": context["data"]["context_bundle_ref"], "expected_generation_epoch": plan["data"]["generation_epoch"]})
        await self.service.drain_jobs()
        job = await self.service.query("read_job", {"job_id": scripts["data"]["job_id"]})
        self.assertEqual(job["body"]["status"], "succeeded", job)
        prep = await self.state(prep)
        script = prep["body"]["script_refs"][0]
        queue = (await self.service.query("get_workspace_snapshot", {"preparation_id": prep["ref"]["id"]}))["preparation_details"]["playlist"]
        self.assertEqual([item["script_revision_ref"] for item in queue["body"]["items"]], [script])
        playlist = await self.cmd("save_playlist", {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"], "items": [{"occurrence_id": "occurrence-1", "product_id": product["id"], "script_revision_ref": script}], "loop_mode": loop_mode})
        prep = playlist["data"]["preparation_ref"]
        compiled = await self.cmd("compile_playback_plan", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "playlist_ref": playlist["data"]["playlist_ref"]})
        return product, room, compiled["data"]["preparation_ref"], script

    async def test_registry_and_schema_are_strict(self):
        self.assertEqual(len(COMMAND_ARGS), 34)
        self.assertEqual(len(QUERY_NAMES), 32)
        self.assertTrue({"approve_script_audio_preview", "preview_script_audio"} <= set(COMMAND_ARGS))
        self.assertTrue({"read_script_audio_preview", "list_script_audio_previews"} <= set(QUERY_NAMES))
        self.assertIn("read_audio_readiness", QUERY_NAMES)
        self.assertIn("read_start_readiness", QUERY_NAMES)
        self.assertIn("approve_missing_audio_reissue", COMMAND_ARGS)
        self.assertIn("inspect_missing_audio_reissue", QUERY_NAMES)
        with self.assertRaises(Exception):
            await self.cmd("create_room", {"name": "x", "principal": {"kind": "human"}})
        result = await self.cmd("create_room", {"name": "统一合同"})
        CommandReceipt.model_validate(result)
        self.assertEqual(digest({"测试": [1, 2]}), canonical_hash({"测试": [1, 2]}))
        with self.assertRaises(Exception):
            await self.service.query("read_capabilities", {"principal_id": "pretend-controller"})

    async def test_business_schema_exposes_session_draft_fields_and_matches_write_contract(self):
        result = await self.service.query("get_business_schema")
        fields = result["session_draft_schema"]["properties"]
        self.assertTrue({"scheduled_at", "campaign_ends_at", "timezone", "requested_variant_counts", "tts_selection"} <= set(fields))
        self.assertNotIn("ends_at", fields)
        self.assertFalse(result["session_draft_schema"]["additionalProperties"])
        self.assertEqual(result["schema_hash"], digest(result["schema"]))
        room, prep = await self.prep()
        schedule = {"start": "2027-02-14T19:30:00+08:00", "end": "2027-02-14T21:30:00+08:00"}
        saved = await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": prep["revision"],
            "draft": {"theme": "情人节", "scheduled_at": schedule, "timezone": "Asia/Shanghai",
                "campaign_ends_at": "2027-02-14T23:59:00+08:00"}})
        with self.service.store.transaction() as c:
            draft = self.service.store.exact(c, saved["data"]["draft_ref"])["body"]["draft"]
        self.assertEqual(draft["scheduled_at"], schedule)
        self.assertEqual(draft["campaign_ends_at"], "2027-02-14T23:59:00+08:00")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("save_session_draft", {"preparation_id": prep["id"],
                "expected_revision": saved["data"]["preparation_ref"]["revision"], "draft": {"ends_at": schedule["end"]}})
        self.assertEqual(error.exception.code, "UNKNOWN_DRAFT_FIELD")

    async def test_campaign_expiry_is_checked_before_save_and_for_legacy_drafts(self):
        _, prep = await self.prep()
        for invalid in ({}, 0, "2027-02-14T23:59:00"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BusinessError) as error:
                    await self.cmd("save_session_draft", {"preparation_id": prep["id"],
                        "expected_revision": prep["revision"], "draft": {"campaign_ends_at": invalid}})
                self.assertEqual(error.exception.code, "INVALID_CAMPAIGN_EXPIRY")
                self.assertEqual(error.exception.details["field"], "/draft/campaign_ends_at")
        self.assertEqual((await self.state(prep))["ref"], prep)
        with self.service.store.transaction(write=True) as c:
            legacy_ref = self.service.store.artifact(c, "draft",
                {"preparation_id": prep["id"], "draft": {"campaign_ends_at": {"end": "2027-02-14"}}})
            check = self.service._currentness(c, {"id": prep["id"], "selected_product_refs": [], "draft_ref": legacy_ref})
        self.assertEqual(check["blockers"], [{"code": "INVALID_CAMPAIGN_EXPIRY"}])

    async def test_operation_receipt_exposes_original_command_args_and_scope_hash(self):
        args = {"schema_hash": self.service.schema()["schema_hash"], "patches": [],
            "fields": Product(name="原请求哈希测试", price=129).model_dump()}
        saved = await self.cmd("save_product", args, operation="original-request-hash")
        observed = await self.service.query("read_operation_receipt", {"operation_id": saved["operation_id"]})
        normalized = COMMAND_ARGS["save_product"].model_validate(args).model_dump(mode="json", exclude_none=True)
        self.assertEqual(observed["request_hash"], digest({"command": "save_product", "args": normalized, "scope": None}))
        self.assertEqual(observed["receipt"], saved)

    async def test_product_idempotency_and_reused_key(self):
        fields = Product(name="A", price=12).model_dump()
        args = {"schema_hash": self.service.schema()["schema_hash"], "patches": [], "fields": fields}
        first = await self.cmd("save_product", args, operation="same-op")
        second = await self.cmd("save_product", args, operation="same-op")
        self.assertEqual(first, second)
        args["fields"]["price"] = 13
        with self.assertRaises(BusinessError) as error:
            await self.cmd("save_product", args, operation="same-op")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")

    async def test_price_only_preserves_selling_points_and_old_version(self):
        ref = await self.product()
        old = await self.service.query("read_product", {"ref": ref})
        fields = {k: old["body"][k] for k in Product.model_fields}
        fields["price"] = 107
        result = await self.cmd("save_product", {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": self.service.schema()["schema_hash"], "patches": [{"path": "/price", "op": "set", "value": 107}], "fields": fields})
        self.assertEqual(result["data"]["product"]["selling_points"], "舒适轻便")
        self.assertEqual((await self.service.query("read_product", {"ref": ref}))["body"]["price"], 178)
        with self.assertRaises(BusinessError) as error:
            await self.cmd("save_product", {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": self.service.schema()["schema_hash"], "patches": [], "fields": fields})
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    async def test_revoke_before_grant_cannot_resurrect(self):
        await self.cmd("revoke_grant", {"run_id": "run-x"}, self.controller)
        with self.assertRaises(BusinessError) as error:
            await self.cmd("issue_grant", {"run_id": "run-x", "scope": {"kind": "catalog", "account_id": "local", "preparation_id": None}, "capabilities": ["save_product"]}, self.controller)
        self.assertEqual(error.exception.code, "RUN_REVOKED")

    async def test_agent_cannot_approve_or_self_grant(self):
        agent = Principal(kind="agent", run_id="run-a", actor_id="agent-a")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("approve_text_for_audio", {"preparation_id": "missing", "review_hash": "hash"}, agent)
        self.assertEqual(error.exception.code, "HUMAN_REQUIRED")

    async def test_text_audio_candidate_adoption_and_start(self):
        product, room, prep, script = await self.prepared_text()
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertEqual(len(review["texts"]), 4)  # saved one-item queue needs its long opening only
        with self.assertRaises(BusinessError):
            await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "approval_ref": script, "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
        prep = approval["data"]["preparation_ref"]
        job = await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "approval_ref": approval["data"]["approval_ref"], "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        await self.service.drain_jobs()
        self.assertEqual(len(self.adapter.rendered), 4)
        prep = await self.state(prep)
        publication = await self.cmd("publish_preparation", {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"], "approval_ref": approval["data"]["approval_ref"]})
        pub = publication["data"]["publication_ref"]
        self.assertFalse(self.adapter.starts)
        self.assertNotIn("adopted_publication_ref", (await self.service.query("read_room", {"room_id": room["id"]}))["body"])
        pending = await self.service.query("read_start_readiness", {"preparation_id": prep["ref"]["id"]})
        self.assertFalse(pending["ready"])
        self.assertEqual(pending["blockers"], [{"code": "START_ADOPTION_REQUIRED"}])
        check = await self.service.query("create_review", {"ref": pub})
        adoption = await self.cmd("adopt_snapshot", {"publication_ref": pub, "review_hash": check["review_hash"], "expected_room_revision": room["revision"]})
        self.assertFalse(self.adapter.starts)
        ready = await self.service.query("read_start_readiness", {"preparation_id": prep["ref"]["id"]})
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["review_hash"], adoption["data"]["review_hash"])
        self.assertEqual(ready["room_ref"], adoption["data"]["room_ref"])
        start_args = {"publication_ref": pub, "review_hash": adoption["data"]["review_hash"], "expected_room_revision": adoption["data"]["room_ref"]["revision"], "action_id": "start-once"}
        first = await self.cmd("start_prepared_room", start_args, operation="start-op")
        second = await self.cmd("start_prepared_room", start_args, operation="start-op")
        self.assertEqual(first, second)
        self.assertEqual(len(self.adapter.starts), 1)

    async def test_same_name_requires_human_identity_resolution(self):
        await self.product()
        with self.assertRaises(BusinessError) as error:
            await self.product()
        self.assertEqual(error.exception.code, "PRODUCT_IDENTITY_CONFIRMATION_REQUIRED")

    async def test_old_revision_choice_keeps_audio_approval_but_withdrawal_blocks(self):
        product, room, prep, script = await self.prepared_text()
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
        prep = approval["data"]["preparation_ref"]
        old = await self.service.query("read_product", {"ref": product})
        fields = {k: old["body"][k] for k in Product.model_fields}
        fields["price"] = 199
        changed = await self.cmd("save_product", {"product_id": product["id"], "expected_revision": product["revision"], "schema_hash": self.service.schema()["schema_hash"], "patches": [{"path": "/price", "op": "set", "value": 199}], "fields": fields})
        head = changed["data"]["product_ref"]
        diff = (await self.service.query("create_text_review", {"preparation_id": prep["id"]}))["revision_currentness"]["differences"][0]
        before = await self.state(prep)
        choice = await self.cmd("record_product_revision_choice", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "pinned_product_ref": product, "observed_head_ref": head, "diff_hash": diff["diff_hash"], "choice": "keep_pinned"})
        after = await self.state(prep)
        self.assertEqual(before, after)
        approved = await self.service.query("read_text_approval", {"preparation_id": prep["id"]})
        self.assertEqual(approved["status"], "current")
        self.assertFalse(self.adapter.rendered)
        fields["sale_status"] = "unavailable"
        await self.cmd("save_product", {"product_id": product["id"], "expected_revision": head["revision"], "schema_hash": self.service.schema()["schema_hash"], "patches": [{"path": "/sale_status", "op": "set", "value": "unavailable"}], "fields": fields})
        with self.assertRaises(BusinessError) as error:
            await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertEqual(error.exception.code, "CURRENTNESS_BLOCKED")

    async def test_agent_grant_scope_and_late_results_are_fenced(self):
        product, room, prep, script = await self.prepared_text()
        scope = WorkScope(kind="preparation", preparation_id=prep["id"])
        agent = Principal(kind="agent", actor_id="agent-a", run_id="run-scripts")
        grant = (await self.cmd("issue_grant", {"run_id": agent.run_id, "scope": scope.model_dump(), "capabilities": ["submit_script_change"]}, self.controller))["data"]
        state = await self.revision_plan(prep)
        # An explicit revision plan may run, but late completion cannot
        # alter the preparation after controller revocation.
        self.adapter.pause = asyncio.Event()
        job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "plan_ref": state["body"]["plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"], "expected_generation_epoch": state["body"]["generation_epoch"]}, agent, scope, grant=grant)
        await asyncio.sleep(.05)
        await self.cmd("revoke_grant", {"run_id": agent.run_id}, self.controller)
        self.adapter.pause.set()
        await self.service.drain_jobs()
        current = await self.state(prep)
        self.assertEqual(current["body"]["script_refs"], state["body"]["script_refs"])
        job_state = await self.service.query("read_job", {"job_id": job["data"]["job_id"]})
        self.assertEqual(job_state["body"]["status"], "cancelled")

    async def test_reorder_preview_is_read_only_and_noop_preserves_approval(self):
        product, room, prep, script = await self.prepared_text()
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approved = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
        before = await self.state(prep)
        preview = await self.service.query("preview_playlist_reorder", {"preparation_id": prep["id"], "spec": {"mode": "grouped", "product_group_order": [product["id"]]}})
        after_preview = await self.state(prep)
        self.assertEqual(before, after_preview)
        result = await self.cmd("save_playlist", {"preparation_id": prep["id"], "expected_revision": before["ref"]["revision"], "items": preview["proposed_items"], "loop_mode": "once", "reorder_spec": preview["resolved_spec"], "preview_hash": preview["preview_hash"]})
        self.assertTrue(result["data"]["unchanged"])
        self.assertEqual((await self.service.query("read_text_approval", {"preparation_id": prep["id"]}))["status"], "current")

    async def test_generated_versions_extend_saved_queue_and_replay_does_not_append_again(self):
        product, _, prep, script = await self.prepared_text()
        before = await self.state(prep)
        repeated = [{"occurrence_id": key, "product_id": product["id"], "script_revision_ref": script}
                    for key in ["saved-first", "saved-again"]]
        saved = await self.cmd("save_playlist", {"preparation_id": prep["id"], "expected_revision": before["ref"]["revision"],
            "items": repeated, "loop_mode": "infinite"})
        with self.service.store.transaction(write=True) as c:
            current = self.service._prep(c, prep["id"])
            script_body = self.service._artifact(c, script)["body"]
            legacy = self.service.store.artifact(c, "script_revision", {**script_body, "variant_id": "old-unqueued", "version_number": 2})
            self.service._save_prep(c, current, {**current["body"], "script_refs": [script, legacy]}, invalidate=False)
        before = await self.state(prep)
        modules = [{"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate"} for key in MODULE_KEYS]
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": before["ref"]["revision"],
            "bundle_ref": before["body"]["bundle_ref"], "items": [{"product_ref": product, "variant_id": key, "modules": modules}
                for key in ["new-first", "new-next"]]})
        request = {"preparation_id": prep["id"], "expected_revision": plan["data"]["preparation_ref"]["revision"],
                   "plan_ref": plan["data"]["script_plan_ref"], "context_bundle_ref": before["body"]["bundle_ref"],
                   "expected_generation_epoch": plan["data"]["generation_epoch"]}
        first = await self.cmd("submit_script_change", request, operation="auto-append")
        await self.service.drain_jobs()
        job = await self.service.query("read_job", {"job_id": first["data"]["job_id"]})
        self.assertEqual(job["body"]["status"], "succeeded", job)
        result_refs = [item["output"]["script_ref"] for item in job["body"]["items"]]
        after = await self.state(prep)
        self.assertNotEqual(after["body"]["playlist_ref"], saved["data"]["playlist_ref"])
        workspace = await self.service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
        items = workspace["preparation_details"]["playlist"]["body"]["items"]
        self.assertEqual(items[:2], repeated)
        self.assertEqual([item["script_revision_ref"] for item in items[2:]], [legacy, *result_refs])
        self.assertEqual(len({item["occurrence_id"] for item in items}), len(items))
        self.assertEqual(await self.cmd("submit_script_change", request, operation="auto-append"), first)
        await self.service.drain_jobs()
        self.assertEqual(await self.state(prep), after)

    async def test_artifact_cross_preparation_read_is_rejected(self):
        product, room, prep, script = await self.prepared_text()
        other_room, other_prep = await self.prep()
        with self.assertRaises(BusinessError) as error:
            await self.service.query("read_script", {"ref": script}, principal=Principal(kind="agent", run_id="x"), scope=WorkScope(kind="preparation", preparation_id=other_prep["id"]))
        self.assertEqual(error.exception.code, "ARTIFACT_SCOPE_CONFLICT")

    async def test_unknown_provider_result_cannot_be_automatically_retried(self):
        product, room, prep, script = await self.prepared_text()
        async def timeout(*args):
            raise TimeoutError("test timeout; do not copy exception into logs")
        self.adapter.generate_module = timeout
        state = await self.revision_plan(prep)
        job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "plan_ref": state["body"]["plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"], "expected_generation_epoch": state["body"]["generation_epoch"]})
        await self.service.drain_jobs()
        result = await self.service.query("read_job", {"job_id": job["data"]["job_id"]})
        self.assertEqual(result["body"]["items"][0]["call_state"], "timeout_unknown")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["data"]["job_id"]})
        self.assertEqual(error.exception.code, "EXTERNAL_OUTCOME_UNKNOWN")
        self.assertNotIn("do not copy", str(result))

    async def test_audio_cache_reuse_and_corrupt_file_repair(self):
        product, room, prep, script = await self.prepared_text()
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
        for turn in range(3):
            state = await self.state(prep)
            job = await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "approval_ref": approval["data"]["approval_ref"], "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
            await self.service.drain_jobs()
            if turn == 0:
                self.assertEqual(len(self.adapter.rendered), 4)
            elif turn == 1:
                self.assertEqual(len(self.adapter.rendered), 4)
                result = await self.service.query("read_job", {"job_id": job["data"]["job_id"]})
                Path(result["body"]["items"][0]["output"]["asset"]["path"]).write_bytes(b"not a wave")
            else:
                self.assertEqual(len(self.adapter.rendered), 5)

    async def test_human_identity_confirmation_is_not_a_model_tool(self):
        original = await self.product()
        result = self.service.record_identity_decision("测试拖鞋", [original], principal=self.human)
        fields = Product(name="测试拖鞋", price=199).model_dump()
        created = await self.cmd("save_product", {"schema_hash": self.service.schema()["schema_hash"], "fields": fields, "patches": [], "identity_decision_ref": result["identity_decision_ref"]})
        self.assertNotEqual(created["data"]["product_ref"]["id"], original["id"])
        with self.assertRaises(BusinessError):
            self.service.record_identity_decision("测试拖鞋", [original], principal=Principal(kind="agent", run_id="x"))

    async def test_blob_bytes_and_synthetic_recap(self):
        raw = "上次二号商品表现较好，建议放到开头。".encode()
        blob = await self.cmd("import_blob", {"content_base64": base64.b64encode(raw).decode(), "expected_sha256": hashlib.sha256(raw).hexdigest()})
        recap = await self.cmd("import_recap", {"raw_blob_ref": blob["data"]["blob_ref"], "synthetic": True, "document": {"synthetic": True, "findings": [{"finding_id": "RF08", "suggestion": "二号商品先讲"}]}})
        result = await self.service.query("read_recap", {"ref": recap["data"]["recap_ref"]})
        self.assertTrue(result["body"]["synthetic"])
        self.assertEqual(result["raw_text"], raw.decode())

    async def test_private_source_import_retains_original_report_hash_and_rebinds(self):
        product = await self.product()
        raw = "店铺提供的鞋子材质：EVA。".encode()
        blob = await self.cmd("import_blob", {"content_base64": base64.b64encode(raw).decode(), "expected_sha256": hashlib.sha256(raw).hexdigest()})
        manifest = {"original_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "original_size": len(raw), "text": raw.decode(), "trust": "untrusted_uploaded_source"}
        private_ref = {"kind": "source_manifest", "id": "private-material-1", "revision": 1, "content_hash": digest(manifest), "schema_version": "PRELIVE-2.3"}
        report = AnalysisReport(summary="商品材料来自商家上传资料；顾客需求有待访谈验证。", product_ids=[product["id"]], fields={"material": "EVA"}, hypotheses=["可能适合居家场景"], source_refs=[private_ref], dependencies=[product]).model_dump(exclude_none=True)
        prepared_hash = digest(report)
        result = await self.cmd("accept_analysis_report", {"role": "audience_scenes", "report": report, "report_hash": prepared_hash, "product_refs": [product], "readset": [product], "source_manifests": [{"source_ref": private_ref, "manifest": manifest, "original_blob_ref": blob["data"]["blob_ref"]}]})
        accepted = await self.service.query("read_analysis_report", {"ref": result["data"]["analysis_ref"]})
        self.assertEqual(accepted["body"]["lineage"]["prepared_report_hash"], prepared_hash)
        self.assertNotEqual(result["data"]["accepted_report_hash"], prepared_hash)
        voice_source = accepted["body"]["report"]["source_refs"][0]
        self.assertNotEqual(voice_source["id"], private_ref["id"])
        actual_source = await self.service.query("read_blob", {"ref": voice_source})
        self.assertEqual(actual_source["manifest"], manifest)
        research = await self.service.query("find_audience_research", {"product_id": product["id"]})
        self.assertEqual(research["items"][0]["compatibility"], "exact")


class CompilerTests(unittest.TestCase):
    def items(self, groups):
        return [{"occurrence_id": str(i), "product_id": p, "script_revision_ref": {"id": str(i)}} for i, p in enumerate(groups)]

    def test_interleave_and_shuffle_preserve_all_occurrences(self):
        items = self.items("AAABBC")
        for mode in ("grouped", "interleave", "full_shuffle"):
            spec = {"mode": mode, "product_group_order": ["A", "B", "C"], "within_product": "shuffle", "seed": "a" * 32}
            result = reorder(items, spec)
            self.assertEqual({i["occurrence_id"] for i in items}, {i["occurrence_id"] for i in result["proposed_items"]})
            self.assertEqual(result, reorder(items, spec))
        self.assertEqual(reorder(items, {**spec, "mode": "interleave"})["adjacent_same_product_pairs"], 0)

    def test_saved_queue_openings_and_required_only(self):
        items = self.items("ABC")
        script = {"product_id": None, "module_refs": {k: {"id": k} for k in MODULE_KEYS if k not in {"campaign", "transition"}}}
        def scripts(ref):
            return {**script, "product_id": items[int(ref["id"])]["product_id"]}
        result = compile_items(items, "infinite", scripts, lambda ref: {"sentences": [{"id": ref["id"], "text": ref["id"]}]})
        self.assertEqual(result["supercycle_length"], 3)
        self.assertEqual([e["original_position"] for e in result["entries"] if e["opening_mode"] == "opening_long"], [1])
        finite = compile_items(items, "once", scripts, lambda ref: {"sentences": [{"id": ref["id"], "text": ref["id"]}]})
        self.assertEqual(finite["supercycle_length"], 3)


if __name__ == "__main__":
    unittest.main()
