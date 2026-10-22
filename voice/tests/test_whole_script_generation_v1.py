"""Whole-version protocol, exact-context reuse, atomic split and paid-call count.

All HTTP responses are simulated. No credentials, live store, or TTS required.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app import commerce_config
from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError
from app.prelive.whole_script_generation import (PROTOCOL, build_request, compact_context,
    finish_received_script, generate_one_script)
from app.prelive_bridge import ExistingVoiceAdapter
from shengbo_contracts.common import MutationContext, Principal

KEY = "whole-offline-secret-never-persist"
PRIVATE = "whole-private-reasoning-never-persist"
KEYS = ("opening_long", "product_intro", "selling_points", "cta")


def actions(keys=KEYS):
    return [{"key": key, "action": "generate", "instruction": "保留有据事实，按模块职责自然表达。"} for key in keys]


def module(key):
    texts = {"opening_long": "欢迎了解这款商品。", "opening_short": "一起看看这款商品。",
             "product_intro": "这是一款测试口红。", "selling_points": "这款口红采用红色。",
             "campaign": "请按页面确认本场活动。", "cta": "有需要可以了解一下。"}
    claim = key in {"product_intro", "selling_points"}
    return {"module_key": key, "sentences": [{"display_text": texts[key], "spoken_text": texts[key],
        "kind": "product_claim" if claim else "engagement",
        "fact_ids": ["NAME" if key == "product_intro" else "SELLING"] if claim else []}]}


def envelope(keys=KEYS, raw=None):
    modules = [module(key) for key in keys]
    for item in modules:
        item["sentences"] = [{"text": row["spoken_text"], "kind": row["kind"], "fact_ids": row["fact_ids"]}
                             for row in item["sentences"]]
    return {"id": "whole-test-receipt", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"modules": modules}, ensure_ascii=False) if raw is None else raw,
        "reasoning_content": PRIVATE}}], "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300, "private": KEY}}


class WholeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def product(self):
        return {"id": "p-test", "name": "测试口红", "price": 99, "currency": "CNY", "selling_points": "红色。"}

    async def test_one_request_all_selected_modules_and_public_receipt_before_validation(self):
        sent, observed = [], []
        def handler(request):
            sent.append(request)
            return httpx.Response(200, json=envelope())
        result = await generate_one_script(self.product(), {"research": {"priority": "通勤"}}, actions(),
            api_key=KEY, transport=httpx.MockTransport(handler), response_observer=observed.append)
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(result["modules"]), set(KEYS))
        task = json.loads(json.loads(sent[0].content)["messages"][1]["content"])
        self.assertEqual(task["requested_module_keys"], list(KEYS))
        self.assertNotIn("opening_short", task["module_specs"])
        self.assertNotIn("transition", task["module_specs"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["usage"], {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
        self.assertEqual(len(result["validation_metrics"]["modules"]), len(KEYS))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_missing_duplicate_extra_and_truncated_responses_keep_single_usage_receipt(self):
        invalid = [envelope(KEYS[:-1]), envelope((*KEYS, "cta")), envelope((*KEYS, "opening_short")),
            envelope(raw='{"modules": [], "modules": []}')]
        truncated = envelope(); truncated["choices"][0]["finish_reason"] = "length"; invalid.append(truncated)
        for reply in invalid:
            observed, sent = [], []
            def handler(request):
                sent.append(request)
                return httpx.Response(200, json=reply)
            with self.subTest(reply=reply), self.assertRaises(BusinessError):
                await generate_one_script(self.product(), {}, actions(), api_key=KEY,
                    transport=httpx.MockTransport(handler), response_observer=observed.append)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["usage"]["total_tokens"], 300)

    async def test_timeout_and_archive_failure_never_retry_or_leak(self):
        for failure in ("timeout", "archive"):
            sent = []
            def handler(request):
                sent.append(request)
                if failure == "timeout":
                    raise httpx.ReadTimeout(KEY, request=request)
                return httpx.Response(200, json=envelope())
            def observer(_):
                raise OSError(KEY)
            with self.subTest(failure=failure), self.assertRaises(BusinessError) as error:
                await generate_one_script(self.product(), {}, actions(), api_key=KEY,
                    transport=httpx.MockTransport(handler), response_observer=observer)
            self.assertEqual(error.exception.code, "MODULE_PROVIDER_OUTCOME_UNKNOWN")
            self.assertEqual(len(sent), 1)
            self.assertNotIn(KEY, json.dumps(error.exception.payload()))

    def test_exact_subtree_deduplication_is_reversible_and_unique_facts_stay_verbatim(self):
        shared = {"facts": ["不应删除的完整说明。" * 30], "other": {"region": "上海"}}
        original = {"a": shared, "b": deepcopy(shared), "different": {**shared, "unique": "本SKU仅此新增事实"}}
        compacted, audit = compact_context(original)
        def resolve(value):
            if isinstance(value, dict) and set(value) == {"$context_ref"}:
                target = compacted
                for token in value["$context_ref"].split("/")[2:]:
                    token = token.replace("~1", "/").replace("~0", "~")
                    target = target[int(token)] if isinstance(target, list) else target[token]
                return resolve(target)
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value
        self.assertEqual(resolve(compacted), original)
        self.assertLess(audit["after_bytes"], audit["before_bytes"])
        self.assertIn("本SKU仅此新增事实", json.dumps(compacted, ensure_ascii=False))

    def test_mixed_reuse_never_enters_whole_request(self):
        with self.assertRaises(BusinessError):
            build_request(self.product(), {}, [*actions(), {"key": "campaign", "action": "reuse"}], model="deepseek-flash")


class WholeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="whole-script-v1-")
        self.sent, self.invalid, self.network_failure = [], False, False
        def handler(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            self.sent.append(task)
            if self.network_failure:
                raise httpx.ReadTimeout("offline", request=request)
            keys = task.get("requested_module_keys")
            if keys is None:
                return httpx.Response(200, json=envelope(raw=json.dumps(module(task["requested_module_key"]), ensure_ascii=False)))
            return httpx.Response(200, json=envelope(keys[:-1] if self.invalid else keys))
        server = SimpleNamespace(commerce=commerce_config, os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": KEY}))
        self.service = PreliveService(self.temp.name, ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(handler)))
        self.service.wake_worker = lambda: None
        self.count = 0

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, command, args):
        self.count += 1
        return (await self.service.execute_command(command, args, MutationContext(operation_id="whole-" + str(self.count)), Principal(kind="human")))["data"]

    async def setup_job(self, variants=1):
        fields = Product(name="测试口红", price=99, selling_points="红色。").model_dump()
        product = (await self.cmd("save_product", {"schema_hash": self.service.schema()["schema_hash"], "fields": fields, "patches": []}))["product_ref"]
        room = (await self.cmd("create_room", {"name": "整版离线测试"}))["room_ref"]
        prep = (await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
        selected = await self.cmd("update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [product]})
        draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "情人节", "commerce": {}}})
        context = await self.cmd("save_context_bundle", {"preparation_id": prep["id"], "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"],
            "items": [{"product_ref": product, "variant_id": "whole-v" + str(n + 1), "modules": actions()} for n in range(variants)]})
        job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"], "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        return prep, job

    async def read_job(self, job):
        return (await self.service.query("read_job", {"job_id": job["job_id"]}))["body"]

    async def test_two_variants_are_two_requests_distinct_version_numbers_and_separate_revisions(self):
        prep, job = await self.setup_job(2)
        await self.service.drain_jobs()
        result = await self.read_job(job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual([row["input"]["version_number"] for row in result["items"]], [1, 2])
        self.assertTrue(all(row["input"]["generation_mode"] == "whole_script" for row in result["items"]))
        self.assertTrue(all(len(row["provider_attempts"]) == 1 for row in result["items"]))
        self.assertEqual(sum(a["usage"]["total_tokens"] for row in result["items"] for a in row["provider_attempts"]), 600)
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        scripts = [(await self.service.query("read_script", {"ref": ref}))["body"] for ref in state["body"]["script_refs"]]
        self.assertEqual([row["version_number"] for row in scripts], [1, 2])
        self.assertEqual([row["script_revision"] for row in scripts], [1, 1])
        self.assertTrue(all(set(row["module_refs"]) == set(KEYS) for row in scripts))
        self.assertTrue(all(row["generation_metrics"] for row in result["items"]))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_incomplete_whole_response_archives_once_and_commits_no_partial_modules(self):
        self.invalid = True
        prep, job = await self.setup_job()
        await self.service.drain_jobs()
        result = await self.read_job(job)
        self.assertEqual(result["status"], "failed", result)
        item = result["items"][0]
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(item["provider_attempts"][0]["usage"]["total_tokens"], 300)
        self.assertEqual(item["provider_attempts"][0]["validation_state"], "rejected")
        self.assertFalse(item.get("module_results"))
        with self.service.store.transaction() as c:
            self.assertEqual(self.service.store.all(c, "module"), [])
        self.assertEqual((await self.service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["script_refs"], [])

    async def test_whole_initial_then_one_module_patch_reuses_other_modules_and_keeps_variant_number(self):
        prep, initial_job = await self.setup_job()
        await self.service.drain_jobs()
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        old_ref = state["body"]["script_refs"][0]
        old = await self.service.query("read_script", {"ref": old_ref})
        changed = [{"key": key, "action": "patch" if key == "selling_points" else "reuse",
            "instruction": "只调整这一模块。", "base_module_ref": old["body"]["module_refs"][key]} for key in KEYS]
        plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "bundle_ref": state["body"]["bundle_ref"], "items": [{"product_ref": state["body"]["selected_product_refs"][0],
                "variant_id": old["body"]["variant_id"], "version_number": 99, "base_script_ref": old_ref, "modules": changed}]})
        next_job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": plan["preparation_ref"]["revision"],
            "plan_ref": plan["script_plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        await self.service.drain_jobs()
        result = await self.read_job(next_job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1]["requested_module_key"], "selling_points")
        self.assertEqual(result["items"][0]["input"]["generation_mode"], "module")
        latest = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        new = await self.service.query("read_script", {"ref": latest["body"]["script_refs"][0]})
        self.assertEqual(new["body"]["version_number"], 1)
        self.assertEqual(new["body"]["script_revision"], 2)
        for key in set(KEYS) - {"selling_points"}:
            self.assertEqual(old["body"]["module_refs"][key], new["body"]["module_refs"][key])
        self.assertEqual((await self.service.query("read_script", {"ref": old_ref})), old)
        self.assertEqual(new["body"]["generation_provenance"]["context_ref"], state["body"]["bundle_ref"])

    async def test_local_commit_failure_resumes_archived_complete_response_without_second_call(self):
        _, job = await self.setup_job()
        original = self.service._commit_whole_script_result
        failures = 0
        def stop_once(*args, **kwargs):
            nonlocal failures
            if failures == 0:
                failures += 1
                raise BusinessError("INJECTED_LOCAL_STOP")
            return original(*args, **kwargs)
        with patch.object(self.service, "_commit_whole_script_result", side_effect=stop_once):
            await self.service.drain_jobs()
        self.assertEqual((await self.read_job(job))["status"], "failed")
        self.assertEqual(len(self.sent), 1)
        await self.cmd("retry_failed_items", {"job_id": job["job_id"]})
        await self.service.drain_jobs()
        self.assertEqual((await self.read_job(job))["status"], "succeeded")
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_whole_response_is_not_blindly_retried(self):
        self.network_failure = True
        _, job = await self.setup_job()
        await self.service.drain_jobs()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual((await self.read_job(job))["items"][0]["call_state"], "timeout_unknown")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("retry_failed_items", {"job_id": job["job_id"]})
        self.assertEqual(error.exception.code, "EXTERNAL_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.sent), 1)

    async def test_persisted_legacy_job_without_mode_still_uses_old_module_protocol(self):
        _, job = await self.setup_job()
        with self.service.store.transaction(write=True) as c:
            saved = self.service.store.get(c, "prelive_job", job["job_id"])
            body = deepcopy(saved["body"])
            body["items"][0]["input"].pop("generation_mode", None)
            body["items"][0]["input"].pop("generator_revision", None)
            self.service.store.put(c, "prelive_job", job["job_id"], body, expected=saved["ref"]["revision"])
        await self.service.drain_jobs()
        result = await self.read_job(job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.sent), len(KEYS))
        self.assertTrue(all("requested_module_key" in task for task in self.sent))

    async def test_new_blob_mimes_and_base64_preflight_leave_source_bytes_unchanged(self):
        import base64
        for mime in ("application/msword", "application/rtf", "text/rtf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            raw = ("原始资料-" + mime).encode()
            hashed = hashlib.sha256(raw).hexdigest()
            result = await self.cmd("import_blob", {"mime": mime, "expected_sha256": "sha256:" + hashed,
                "filename": "source.bin", "content_base64": base64.b64encode(raw).decode()})
            self.assertEqual((Path(self.temp.name) / "prelive_blobs" / hashed).read_bytes(), raw)
            self.assertEqual(result["blob_ref"]["kind"], "blob")
        class Oversized(str):
            def __len__(self):
                return 4 * (((20 * 1024 * 1024) + 2) // 3) + 1
        value = SimpleNamespace(mime="text/plain", content_base64=Oversized("not-decoded"), expected_sha256="unused")
        with patch("app.prelive.service.base64.b64decode", side_effect=AssertionError("must not decode")):
            with self.assertRaises(BusinessError) as error:
                self.service._prepare_blob_file(value)
        self.assertEqual(error.exception.code, "BLOB_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
