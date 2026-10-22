"""Isolated business-job recovery with archived fake responses; no remote calls."""
from copy import deepcopy
import inspect
import json
import tempfile
import unittest
from unittest.mock import patch

from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError
from app.prelive import module_generation, whole_script_generation
from shengbo_contracts.common import MutationContext, Principal


KEYS = ("opening_long", "product_intro", "selling_points", "cta")


def module(key):
    return {"module_key": key, "sentences": [{"display_text": "有需要可以了解一下。", "spoken_text": "有需要可以了解一下。",
        "kind": "engagement", "fact_ids": []}]}


class ArchivedOnlyAdapter:
    """Seed a real durable rejected-response boundary without a provider."""
    def __init__(self):
        self.calls = []
        self.reject_at = "cta"
        self.malformed = False
        self.unknown = False

    def script_generation_signature(self):
        return {"model": "offline-fixed"}

    def module_generation_signature(self):
        return {"model": "offline-fixed"}

    async def _observe(self, observer, receipt):
        observed = observer(receipt)
        if inspect.isawaitable(observed):
            await observed

    async def generate_script(self, context, actions, product, *, response_observer):
        self.calls.append("whole_script")
        if self.unknown:
            raise TimeoutError("offline unknown")
        keys = [a["key"] for a in actions if a["action"] == "generate"]
        receipt = {"provider": "offline", "model": "offline-fixed", "protocol": "whole-script-v1",
            "prompt_revision": whole_script_generation.prompt_revision(), "module_keys": keys,
            "content": "broken json" if self.malformed else json.dumps({"modules": [module(k) for k in keys]}, ensure_ascii=False),
            "validation_product": deepcopy(product), "finish_reason": "stop", "unsupported_response": False,
            "usage": {"total_tokens": 123}, "usage_state": "known"}
        await self._observe(response_observer, receipt)
        raise BusinessError("GENERATED_MODULE_NEEDS_REVIEW", "模拟旧校验器拒绝，归档内容仍保留")

    async def generate_module(self, context, key, product, previous, *, response_observer):
        self.calls.append(key)
        receipt = {"provider": "offline", "model": "offline-fixed", "module_key": key,
            "prompt_revision": module_generation.module_prompt_revision(key),
            "content": json.dumps(module(key), ensure_ascii=False), "validation_product": deepcopy(product),
            "finish_reason": "stop", "unsupported_response": False, "usage": {"total_tokens": 42}, "usage_state": "known"}
        await self._observe(response_observer, receipt)
        if key == self.reject_at:
            raise BusinessError("GENERATED_MODULE_NEEDS_REVIEW", "模拟旧校验器拒绝")
        return module_generation.finish_received_module(receipt, product, key)


class ArchivedRevalidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r12-revalidation-")
        self.adapter = ArchivedOnlyAdapter()
        self.service = PreliveService(self.temp.name, self.adapter)
        self.service.wake_worker = lambda: None
        self.counter = 0

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, name, args):
        self.counter += 1
        result = await self.service.execute_command(name, args, MutationContext(operation_id=f"r12-{self.counter}"),
            Principal(kind="human", actor_id="r12-offline-human"))
        return result["data"]

    async def setup_job(self, mode="whole_script"):
        fields = Product(name=f"隔离测试商品{self.counter}", price=99, selling_points="红色。").model_dump()
        product = (await self.cmd("save_product", {"schema_hash": self.service.schema()["schema_hash"],
            "fields": fields, "patches": []}))["product_ref"]
        room = (await self.cmd("create_room", {"name": "本地重验隔离测试"}))["room_ref"]
        prep = (await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
        selected = await self.cmd("update_preparation_products", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "selected_product_refs": [product]})
        draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "原任务主题", "commerce": {}}})
        context = await self.cmd("save_context_bundle", {"preparation_id": prep["id"],
            "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"],
            "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"],
            "items": [{"product_ref": product, "variant_id": f"r12-offline-{self.counter}", "generation_mode": mode,
                "modules": [{"key": key, "action": "generate", "instruction": "保留原任务事实。"} for key in KEYS]}]})
        job = await self.cmd("submit_script_change", {"preparation_id": prep["id"],
            "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
            "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        await self.service.drain_jobs()
        return prep, job

    async def job(self, job):
        return (await self.service.query("read_job", {"job_id": job["job_id"]}))["body"]

    def forbidden_calls(self):
        return (patch.object(self.adapter, "generate_script", side_effect=AssertionError("NO NEW PROVIDER CALL")),
                patch.object(self.adapter, "generate_module", side_effect=AssertionError("NO NEW PROVIDER CALL")))

    async def recover(self, job):
        a, b = self.forbidden_calls()
        with a, b:
            result = await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
            await self.service.drain_jobs()
        return result, await self.job(job)

    async def test_whole_rejected_archive_becomes_formal_script_without_new_call(self):
        prep, job = await self.setup_job()
        before = await self.job(job)
        old_attempt = deepcopy(before["items"][0]["provider_attempts"][0])
        with self.service.store.transaction() as c:
            original_archive = self.service.store.exact(c, old_attempt["provider_response_ref"])
        command, final = await self.recover(job)
        self.assertEqual(command["recovery_mode"], "revalidate_archived")
        self.assertEqual(final["status"], "succeeded", final)
        item = final["items"][0]
        self.assertEqual(item["provider_attempts"], [old_attempt])
        self.assertEqual(old_attempt["validation_state"], "rejected")
        self.assertEqual(self.adapter.calls, ["whole_script"])
        audit = item["archive_revalidations"][-1]
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["script_ref"], item["output"]["script_ref"])
        self.assertTrue(audit["checks"][0]["validator_revision"].startswith("sha256:"))
        self.assertEqual(audit["checks"][0]["status"], "modules_committed")
        self.assertFalse(audit["provider_reissued"])
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        self.assertEqual(state["body"]["script_refs"], [audit["script_ref"]])
        script = await self.service.query("read_script", {"ref": audit["script_ref"]})
        self.assertEqual(set(script["body"]["module_refs"]), set(KEYS))
        with self.service.store.transaction() as c:
            self.assertEqual(self.service.store.exact(c, old_attempt["provider_response_ref"]), original_archive)
            self.assertEqual(len(self.service.store.all(c, "provider_response")), 1)
            commits = c.execute("SELECT payload FROM prelive_events WHERE event_type='script.whole_result_committed'").fetchall()
            self.assertEqual(json.loads(commits[-1]["payload"])["provider_attempt_count"], 0)

    async def test_module_revalidation_keeps_committed_siblings_and_rejected_history(self):
        _, job = await self.setup_job("module")
        before = await self.job(job)
        prior_results = deepcopy(before["items"][0]["module_results"])
        attempts = deepcopy(before["items"][0]["provider_attempts"])
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "succeeded", final)
        item = final["items"][0]
        self.assertEqual(item["provider_attempts"], attempts)
        for key, value in prior_results.items():
            self.assertEqual(item["module_results"][key]["module_ref"], value["module_ref"])
        self.assertEqual(item["archive_revalidations"][-1]["checks"][0]["target"], "cta")
        self.assertEqual(self.adapter.calls, list(KEYS))

    async def test_generator_revision_change_only_explicit_local_revalidation_can_pass(self):
        _, job = await self.setup_job()
        a, b = self.forbidden_calls()
        with a, b, patch.object(whole_script_generation, "prompt_revision", return_value="changed-current-prompt"):
            await self.cmd("retry_failed_items", {"job_id": job["job_id"]})
            await self.service.drain_jobs()
            self.assertEqual((await self.job(job))["items"][0]["error"]["code"], "GENERATOR_REVISION_CHANGED")
            await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
            await self.service.drain_jobs()
        final = await self.job(job)
        self.assertEqual(final["status"], "succeeded", final)
        check = final["items"][0]["archive_revalidations"][-1]["checks"][0]
        self.assertEqual(check["current_generator_revision"], "changed-current-prompt")
        self.assertNotEqual(check["original_generator_revision"], check["current_generator_revision"])

    async def test_still_invalid_archive_records_new_rejection_without_changing_original(self):
        self.adapter.malformed = True
        _, job = await self.setup_job()
        before = await self.job(job)
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["items"][0]["provider_attempts"], before["items"][0]["provider_attempts"])
        audit = final["items"][0]["archive_revalidations"][-1]
        self.assertEqual(audit["status"], "rejected")
        self.assertEqual(audit["checks"][0]["status"], "rejected")
        self.assertEqual(final["items"][0].get("module_results", {}), {})

    async def test_module_prompt_revision_change_revalidates_original_hash_locally(self):
        _, job = await self.setup_job("module")
        with patch.object(module_generation, "module_prompt_revision", return_value="current-module-r12"):
            _, final = await self.recover(job)
        self.assertEqual(final["status"], "succeeded", final)
        check = final["items"][0]["archive_revalidations"][-1]["checks"][0]
        self.assertEqual(check["current_generator_revision"], "current-module-r12")
        self.assertEqual(check["validation_details"]["verified_dependency_hash"], check["dependency_hash"])

    async def test_local_module_commit_failure_is_distinct_from_validation_rejection(self):
        _, job = await self.setup_job()
        with patch.object(self.service, "_commit_whole_script_result", side_effect=BusinessError("INJECTED_MODULE_COMMIT_STOP")):
            _, final = await self.recover(job)
        check = final["items"][0]["archive_revalidations"][-1]["checks"][0]
        self.assertEqual(check["status"], "commit_failed")
        self.assertEqual(check["validation_status"], "accepted")
        self.assertEqual(check["commit_status"], "failed")

    async def test_unknown_and_unreturned_module_never_enter_revalidation(self):
        self.adapter.reject_at = "selling_points"
        _, job = await self.setup_job("module")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
        self.assertEqual(error.exception.code, "ARCHIVED_RESPONSE_INCOMPLETE")
        self.assertEqual(error.exception.details["missing_module_keys"], ["cta"])
        self.adapter.unknown = True
        _, unknown_job = await self.setup_job()
        with self.assertRaises(BusinessError) as unknown:
            await self.cmd("retry_failed_items", {"job_id": unknown_job["job_id"], "recovery_mode": "revalidate_archived"})
        self.assertEqual(unknown.exception.code, "EXTERNAL_OUTCOME_UNKNOWN")

    async def test_changed_fixed_input_is_rejected_by_recomputed_original_dependency_hash(self):
        _, job = await self.setup_job()
        with self.service.store.transaction(write=True) as c:
            saved = self.service.store.get(c, "prelive_job", job["job_id"])
            body = deepcopy(saved["body"])
            body["context"]["r12_changed_input"] = "must not use old response for this input"
            self.service.store.put(c, "prelive_job", job["job_id"], body, expected=saved["ref"]["revision"])
        _, final = await self.recover(job)
        self.assertEqual(final["items"][0]["error"]["code"], "ARCHIVED_RESPONSE_INPUT_MISMATCH")
        self.assertEqual(final["items"][0]["archive_revalidations"][-1]["status"], "rejected")
        self.assertEqual(self.adapter.calls, ["whole_script"])

    async def test_job_epoch_fence_remains_required(self):
        prep, job = await self.setup_job()
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "draft": {"theme": "已经换场", "commerce": {}}})
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
        self.assertEqual(error.exception.code, "GENERATION_EPOCH_CHANGED")

    async def test_response_link_must_belong_to_same_job_item_and_attempt(self):
        _, job = await self.setup_job()
        with self.service.store.transaction(write=True) as c:
            saved = self.service.store.get(c, "prelive_job", job["job_id"])
            body = deepcopy(saved["body"])
            attempt = body["items"][0]["provider_attempts"][0]
            archive = self.service.store.exact(c, attempt["provider_response_ref"])["body"]
            bad_ref = self.service.store.artifact(c, "provider_response", {**archive, "job_id": "another-job"})
            attempt["provider_response_ref"] = bad_ref
            self.service.store.put(c, "prelive_job", job["job_id"], body, expected=saved["ref"]["revision"])
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
        self.assertEqual(error.exception.code, "PROVIDER_RESPONSE_LINK_MISMATCH")

    async def test_local_formal_commit_stop_can_resume_without_duplicate_modules_or_call(self):
        for mode in ("whole_script", "module"):
            with self.subTest(mode=mode):
                _, job = await self.setup_job(mode)
                a, b = self.forbidden_calls()
                with a, b, patch.object(self.service, "_commit_job_item", side_effect=BusinessError("INJECTED_LOCAL_COMMIT_STOP")):
                    await self.cmd("retry_failed_items", {"job_id": job["job_id"], "recovery_mode": "revalidate_archived"})
                    await self.service.drain_jobs()
                failed = await self.job(job)
                self.assertEqual(failed["status"], "failed")
                _, final = await self.recover(job)
                self.assertEqual(final["status"], "succeeded", final)
                self.assertEqual(len(final["items"][0]["archive_revalidations"]), 2)
                self.assertEqual(set(final["items"][0]["module_results"]), set(KEYS))
        with self.service.store.transaction() as c:
            # Identical immutable module content is content-addressed across jobs.
            self.assertEqual(len(self.service.store.all(c, "module")), len(KEYS))
        self.assertEqual(self.adapter.calls, ["whole_script", *KEYS])
