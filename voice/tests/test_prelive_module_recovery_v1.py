"""Offline paid-response accounting and per-module recovery contracts."""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import httpx

from app import commerce_config
from app.prelive.module_generation import generate_one_module
from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError
from app.prelive_bridge import ExistingVoiceAdapter
from shengbo_contracts.common import MutationContext, Principal, WorkScope


KEY = "offline-test-secret-do-not-persist"
REASONING = "private-analysis-never-persist"


def response(module_key, *, valid=True, duplicate=False):
    sentence = {"display_text": "这款商品适合了解一下。", "spoken_text": "这款商品适合了解一下。",
                "kind": "engagement", "fact_ids": []}
    content = {"module_key": module_key, "sentences": [sentence] * (2 if duplicate else 1)} if valid else {"bad": True}
    return {"id": "receipt-" + module_key, "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(content, ensure_ascii=False), "reasoning_content": REASONING}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                  "private": KEY}}


class ModuleRecoveryV1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prelive-recovery-v1-")
        self.calls = []
        self.fail_module = None
        self.fail_count = 0
        self.duplicate = False
        self.network_failure = False

        def handler(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            key = task["requested_module_key"]
            self.calls.append(key)
            if self.network_failure and key == "selling_points":
                raise httpx.ReadTimeout("offline timeout", request=request)
            valid = not (key == self.fail_module and self.fail_count == 0)
            if not valid:
                self.fail_count += 1
            return httpx.Response(200, json=response(key, valid=valid, duplicate=self.duplicate))

        server = SimpleNamespace(commerce=commerce_config, os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": KEY}))
        self.adapter = ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(handler))
        self.service = PreliveService(self.temp.name, self.adapter)
        # Tests control the worker explicitly to exercise crash boundaries.
        self.service.wake_worker = lambda: None
        self.counter = 0

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, name, args):
        self.counter += 1
        return (await self.service.execute_command(name, args, MutationContext(operation_id="r-" + str(self.counter)),
            Principal(kind="human")))["data"]

    async def setup_plan(self):
        fields = Product(name="测试口红", price=99, selling_points="红色，方便携带。").model_dump()
        product = (await self.cmd("save_product", {"schema_hash": self.service.schema()["schema_hash"], "fields": fields, "patches": []}))["product_ref"]
        room = (await self.cmd("create_room", {"name": "离线恢复测试"}))["room_ref"]
        prep = (await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
        selected = await self.cmd("update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [product]})
        draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "情人节", "commerce": {}}})
        context = await self.cmd("save_context_bundle", {"preparation_id": prep["id"], "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"], "items": [{"product_ref": product, "variant_id": "recovery-v1", "version_number": 1, "generation_mode": "module",
            "modules": [{"key": key, "action": "generate", "instruction": "只承担本模块职责。"} for key in ("opening_long", "product_intro", "selling_points", "cta")]}]})
        job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"], "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        return prep, job

    async def job(self, job):
        return (await self.service.query("read_job", {"job_id": job["job_id"]}))["body"]

    async def grant(self, run_id, scope):
        self.counter += 1
        return (await self.service.execute_command("issue_grant", {"run_id": run_id, "scope": scope.model_dump(),
            "capabilities": ["submit_script_change", "retry_failed_items"]}, MutationContext(operation_id="grant-" + str(self.counter)),
            Principal(kind="controller", actor_id="offline-controller")))["data"]

    async def agent_command(self, name, args, agent, grant, scope, operation=None):
        self.counter += 1
        return await self.service.execute_command(name, args, MutationContext(operation_id=operation or "agent-" + str(self.counter),
            grant_id=grant["grant_id"], expected_write_generation=grant["write_generation"]), agent, scope=scope)

    async def setup_agent_failed_job(self, *, unknown=False):
        self.fail_module = "selling_points" if not unknown else None
        self.network_failure = unknown
        prep, seeded = await self.setup_plan()
        await self.cmd("cancel_job", {"job_id": seeded["job_id"]})
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        scope = WorkScope(kind="preparation", preparation_id=prep["id"])
        agent = Principal(kind="agent", actor_id="offline-agent-a", run_id="offline-run-a")
        grant = await self.grant(agent.run_id, scope)
        job = (await self.agent_command("submit_script_change", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "plan_ref": state["body"]["plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"],
            "expected_generation_epoch": state["body"]["generation_epoch"]}, agent, grant, scope))["data"]
        await self.service.drain_jobs()
        return prep, job, agent, grant, scope

    async def test_rejected_response_usage_saved_and_retry_keeps_valid_siblings(self):
        self.fail_module = "selling_points"
        prep, job = await self.setup_plan()
        await self.service.drain_jobs()
        first = await self.job(job)
        self.assertEqual(first["status"], "failed")
        item = first["items"][0]
        self.assertEqual(set(item["module_results"]), {"opening_long", "product_intro"})
        self.assertEqual(len(item["provider_attempts"]), 3)
        rejected = item["provider_attempts"][-1]
        self.assertEqual(rejected["usage"]["total_tokens"], 120)
        self.assertEqual(rejected["usage_state"], "known")
        self.assertEqual(rejected["validation_state"], "rejected")
        self.assertTrue(rejected["provider_response_ref"])
        self.assertEqual((await self.service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["script_refs"], [])
        original = deepcopy(item["module_results"])
        await self.cmd("retry_failed_items", {"job_id": job["job_id"]})
        await self.service.drain_jobs()
        final = await self.job(job)
        self.assertEqual(final["status"], "succeeded", final)
        self.assertEqual(self.calls, ["opening_long", "product_intro", "selling_points", "selling_points", "cta"])
        for key in original:
            self.assertEqual(original[key]["module_ref"], final["items"][0]["module_results"][key]["module_ref"])
        self.assertEqual(sum(a["usage"]["total_tokens"] for a in final["items"][0]["provider_attempts"]), 600)
        serialized = json.dumps(final)
        self.assertNotIn(KEY, serialized)
        self.assertNotIn(REASONING, serialized)
        with self.service.store.transaction() as c:
            archived = self.service.store.all(c, "provider_response")
            self.assertEqual(len(archived), 5)
            self.assertNotIn(KEY, json.dumps(archived))
            self.assertNotIn(REASONING, json.dumps(archived))

    async def test_restart_after_received_response_does_not_pay_again(self):
        prep, job = await self.setup_plan()
        actual_commit = self.service._commit_module_result
        failures = 0

        def stop_before_commit(*args, **kwargs):
            nonlocal failures
            if failures == 0:
                failures += 1
                raise BusinessError("INJECTED_PROCESS_STOP")
            return actual_commit(*args, **kwargs)

        with patch.object(self.service, "_commit_module_result", side_effect=stop_before_commit):
            await self.service.drain_jobs()
        first = await self.job(job)
        self.assertEqual(self.calls, ["opening_long"])
        self.assertEqual(first["items"][0]["call_state"], "result_received")
        # Simulate the persisted running state observed by a new process.
        with self.service.store.transaction(write=True) as c:
            record = self.service.store.get(c, "prelive_job", job["job_id"])
            body = deepcopy(record["body"])
            body["status"] = "running"
            body["items"][0]["status"] = "running"
            self.service.store.put(c, "prelive_job", job["job_id"], body, expected=record["ref"]["revision"])
        await self.service.drain_jobs()
        final = await self.job(job)
        self.assertEqual(final["status"], "succeeded", final)
        self.assertEqual(self.calls, ["opening_long", "product_intro", "selling_points", "cta"])
        self.assertEqual(len(final["items"][0]["provider_attempts"]), 4)

    async def test_restart_with_sent_unknown_never_reissues(self):
        _, job = await self.setup_plan()
        with self.service.store.transaction(write=True) as c:
            record = self.service.store.get(c, "prelive_job", job["job_id"])
            body = deepcopy(record["body"])
            body["status"] = "running"
            body["items"][0].update(status="running", call_state="sent")
            self.service.store.put(c, "prelive_job", job["job_id"], body, expected=record["ref"]["revision"])
        await self.service.drain_jobs()
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.job(job))["items"][0]["call_state"], "timeout_unknown")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["job_id"]})
        self.assertEqual(error.exception.code, "EXTERNAL_OUTCOME_UNKNOWN")

    async def test_invalid_json_still_reports_observed_usage_before_validation(self):
        observed = []
        reply = response("opening_long")
        reply["choices"][0]["message"]["content"] = "invalid-json"
        with self.assertRaises(BusinessError):
            await generate_one_module({"name": "测试口红", "price": 99}, {}, "opening_long", [], api_key=KEY,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply)), response_observer=observed.append)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["usage"]["total_tokens"], 120)
        self.assertNotIn(KEY, json.dumps(observed))
        self.assertNotIn(REASONING, json.dumps(observed))

    async def test_exact_duplicate_sentence_rejected_without_losing_usage(self):
        self.duplicate = True
        _, job = await self.setup_plan()
        await self.service.drain_jobs()
        item = (await self.job(job))["items"][0]
        self.assertEqual(item["error"]["code"], "GENERATED_MODULE_NEEDS_REVIEW")
        self.assertEqual(item["provider_attempts"][0]["usage"]["total_tokens"], 120)
        self.assertFalse(item.get("module_results"))

    async def test_unknown_request_cannot_be_reissued_with_new_job_or_operation_id(self):
        self.network_failure = True
        prep, job = await self.setup_plan()
        await self.service.drain_jobs()
        self.assertEqual(self.calls, ["opening_long", "product_intro", "selling_points"])
        first = await self.job(job)
        self.assertEqual(first["items"][0]["call_state"], "timeout_unknown")
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        another = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "plan_ref": state["body"]["plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"],
            "expected_generation_epoch": state["body"]["generation_epoch"]})
        await self.service.drain_jobs()
        final = await self.job(another)
        self.assertEqual(final["items"][0]["error"]["code"], "EXTERNAL_OUTCOME_UNKNOWN")
        self.assertEqual(self.calls, ["opening_long", "product_intro", "selling_points"])
        self.assertEqual(set(final["items"][0]["module_results"]), {"opening_long", "product_intro"})

    async def test_response_archive_failure_stays_unknown_and_never_leaks_error(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=response("opening_long"))
        def fail_observer(_receipt):
            raise OSError(KEY)
        with self.assertRaises(BusinessError) as error:
            await generate_one_module({"name": "测试口红", "price": 99}, {}, "opening_long", [], api_key=KEY,
                transport=httpx.MockTransport(handler), response_observer=fail_observer)
        self.assertEqual(error.exception.code, "MODULE_PROVIDER_OUTCOME_UNKNOWN")
        self.assertEqual(len(calls), 1)
        self.assertNotIn(KEY, json.dumps(error.exception.payload()))

    async def test_received_wav_resumes_without_second_tts_and_review_stays_current(self):
        prep, job = await self.setup_plan()
        await self.service.drain_jobs()
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        script = state["body"]["script_refs"][0]
        playlist = await self.cmd("save_playlist", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "items": [{"occurrence_id": "first", "product_id": state["body"]["selected_product_refs"][0]["id"], "script_revision_ref": script}]})
        compiled = await self.cmd("compile_playback_plan", {"preparation_id": prep["id"], "expected_revision": playlist["preparation_ref"]["revision"], "playlist_ref": playlist["playlist_ref"]})
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": compiled["preparation_ref"]["revision"], "review_hash": review["review_hash"]})
        render_calls = []
        async def render(text, room_id, voice):
            render_calls.append(text)
            path = Path(self.temp.name) / "received.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(22050)
                audio.writeframes(b"\0\0" * 2205)
            return {"path": str(path), "audio_id": "received", "duration": .1, "url": "/offline/received.wav"}
        self.adapter.render_audio = render
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        audio_job = await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "approval_ref": approval["approval_ref"], "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        actual_commit = self.service._commit_job_item
        failed = False
        def stop_before_asset_commit(job_id, item_id, output):
            nonlocal failed
            if output["kind"] == "audio" and not failed:
                failed = True
                raise BusinessError("INJECTED_LOCAL_STOP")
            return actual_commit(job_id, item_id, output)
        with patch.object(self.service, "_commit_job_item", side_effect=stop_before_asset_commit):
            await self.service.drain_jobs()
        self.assertEqual((await self.job(audio_job))["status"], "failed")
        self.assertEqual(len(render_calls), 1)
        await self.cmd("retry_failed_items", {"job_id": audio_job["job_id"]})
        await self.service.drain_jobs()
        self.assertEqual((await self.job(audio_job))["status"], "succeeded")
        self.assertEqual(len(render_calls), 1)
        self.assertEqual((await self.service.query("create_text_review", {"preparation_id": prep["id"]}))["review_hash"], review["review_hash"])
        ready = await self.service.query("read_audio_readiness", {"preparation_id": prep["id"]})
        self.assertTrue(ready["ready_for_publish"], ready)
        self.assertEqual(ready["playback_statistics"]["audio_duration_seconds"], .4)

    async def test_agent_retry_same_original_run_reuses_modules_and_operation_receipt(self):
        _, job, agent, grant, scope = await self.setup_agent_failed_job()
        first = await self.job(job)
        committed = deepcopy(first["items"][0]["module_results"])
        receipt = await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, scope, "same-retry-operation")
        replay = await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, scope, "same-retry-operation")
        self.assertEqual(receipt, replay)
        self.assertEqual(receipt["data"]["retry_item_count"], 1)
        await self.service.drain_jobs()
        final = await self.job(job)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(len(self.calls), 5)
        for key in committed:
            self.assertEqual(committed[key]["module_ref"], final["items"][0]["module_results"][key]["module_ref"])

    async def test_agent_retry_rejects_other_run_and_other_scope_without_calling_provider(self):
        _, job, agent, grant, scope = await self.setup_agent_failed_job()
        calls = len(self.calls)
        other_agent = Principal(kind="agent", actor_id="offline-agent-b", run_id="offline-run-b")
        other_grant = await self.grant(other_agent.run_id, scope)
        with self.assertRaises(BusinessError) as error:
            await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, other_agent, other_grant, scope)
        self.assertEqual(error.exception.code, "JOB_RUN_MISMATCH")
        with self.assertRaises(BusinessError) as error:
            await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, WorkScope(kind="catalog"))
        self.assertEqual(error.exception.code, "SCOPE_CONFLICT")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((await self.job(job))["status"], "failed")

    async def test_agent_retry_rejects_unknown_result_even_with_retry_capability(self):
        _, job, agent, grant, scope = await self.setup_agent_failed_job(unknown=True)
        calls = len(self.calls)
        with self.assertRaises(BusinessError) as error:
            await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, scope)
        self.assertEqual(error.exception.code, "EXTERNAL_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((await self.job(job))["items"][0]["call_state"], "timeout_unknown")

    async def test_agent_retry_rejects_changed_generation_before_requeue(self):
        prep, job, agent, grant, scope = await self.setup_agent_failed_job()
        calls = len(self.calls)
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "draft": {"theme": "另一次活动", "commerce": {}}})
        with self.assertRaises(BusinessError) as error:
            await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, scope)
        self.assertEqual(error.exception.code, "GENERATION_EPOCH_CHANGED")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((await self.job(job))["status"], "failed")

    async def test_agent_retry_rejects_revoked_original_grant(self):
        _, job, agent, grant, scope = await self.setup_agent_failed_job()
        calls = len(self.calls)
        self.counter += 1
        await self.service.execute_command("revoke_grant", {"run_id": agent.run_id}, MutationContext(operation_id="revoke-" + str(self.counter)),
            Principal(kind="controller", actor_id="offline-controller"))
        with self.assertRaises(BusinessError) as error:
            await self.agent_command("retry_failed_items", {"job_id": job["job_id"]}, agent, grant, scope)
        self.assertEqual(error.exception.code, "GRANT_REVOKED")
        self.assertEqual(len(self.calls), calls)


if __name__ == "__main__":
    unittest.main()
