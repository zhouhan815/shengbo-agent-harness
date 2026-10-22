"""Fixed-host provider contract tests; no real credentials or live product store."""
import asyncio
from copy import deepcopy
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app import commerce_config
from app.copywriting_quality import content_issues
from app.prelive.module_generation import (ENDPOINT, SYSTEM, build_request, generate_one_module,
                                           validate_output)
from app.prelive.storage import BusinessError
from app.prelive.service import PreliveService, Product
from app.prelive_bridge import ExistingVoiceAdapter
from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import MutationContext, Principal


TEST_KEY = "unit-test-provider-secret"


def product():
    return {"id": "p-1", "name": "足弓支撑凉拖鞋", "price": 178, "currency": "TWD",
            "promo_price": None, "selling_points": "EVA材质、一体成型、足弓支撑、厚底、静音、防滑、轻便、耐磨、快干、防臭。",
            "promotion": "", "knowledge": "湿地面请慢行。", "specifications": [], "sale_status": "available"}


def sentence(text="接下来看看这双足弓支撑凉拖鞋。", kind="product_claim", fact_ids=None):
    return {"display_text": text, "spoken_text": text, "kind": kind,
            "fact_ids": ["NAME"] if fact_ids is None else fact_ids}


def output(key="opening_short", sentences=None):
    return {"module_key": key, "sentences": [sentence()] if sentences is None else sentences}


def provider(raw=None, **overrides):
    return {"id": "chatcmpl-unit-1", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(output() if raw is None else raw, ensure_ascii=False),
        "reasoning_content": "must-not-be-recorded"}}],
        "usage": {"prompt_tokens": 240, "completion_tokens": 40, "total_tokens": 280,
                  "prompt_tokens_details": {"cached_tokens": 20, "private": TEST_KEY}, "unexpected": TEST_KEY}, **overrides}


def context():
    draft_ref = {"kind": "draft", "id": "draft-original", "revision": 1}
    return {"preparation_id": "prep-1", "draft_ref": draft_ref,
            "module_key": "opening_short", "generation_instruction": "从夜间居家场景切入，不重写其他模块。",
            "user_decisions": [{"opening_mode": "short"}], "records": [
                {"ref": draft_ref, "body": {"draft": {"theme": "居家好物", "objective": "转化",
                    "commerce": commerce_config.parse_room({"scene": "夜间居家", "product_overrides": {
                        "p-1": {"mode": "price", "price": 107, "promotion": "限时活动价，不与其他优惠叠加。"}}})}}},
                {"ref": {"kind": "analysis", "id": "audience"}, "body": {"summary": "关注夜间室内行走的顾客", "hypotheses": ["不作为事实"]}},
                {"ref": {"kind": "recap", "id": "recap-last"}, "body": {"suggestions": ["上一场活动说明太靠后"]}},
                {"ref": {"kind": "product", "id": "p-1"}, "body": product()}]}


class ModuleGenerationTests(unittest.IsolatedAsyncioTestCase):
    def test_short_transition_needs_no_coupon_recital_but_discount_claim_stays_guarded(self):
        item = {**product(), "promotion": "本场99元，先领满180减20券，不与其他券或折扣叠加。"}
        neutral = output("transition", [sentence("接下来换个角度，看看约会时的搭配思路。", "engagement", [])])
        self.assertEqual(validate_output(neutral, item, "transition"),
                         ["接下来换个角度，看看约会时的搭配思路。"])
        unsafe = output("transition", [sentence("这次优惠别错过。", fact_ids=["PROMOTION"])])
        with self.assertRaises(BusinessError) as error:
            validate_output(unsafe, item, "transition")
        self.assertEqual(error.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")
        self.assertTrue(any("叠加" in issue for issue in error.exception.details["issues"]))

    async def call(self, raw=None, *, key="opening_short", context_value=None, status=200, response=None):
        seen = []
        async def handler(request):
            seen.append(request)
            return httpx.Response(status, json=provider(raw) if response is None else response)
        value = await generate_one_module(product(), context_value or context(), key, [],
                                           api_key=TEST_KEY, transport=httpx.MockTransport(handler))
        return value, seen

    async def test_actual_http_contract_is_one_module_and_preserves_every_context_record(self):
        original = context()
        previous = [sentence("旧版引入。", "engagement", [])]
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=provider())
        result = await generate_one_module(product(), original, "opening_short", previous,
                                           api_key=TEST_KEY, transport=httpx.MockTransport(handler))
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(str(request.url), ENDPOINT)
        self.assertEqual(request.headers["Authorization"], "Bearer " + TEST_KEY)
        body = json.loads(request.content)
        self.assertEqual(body["messages"][0]["content"], SYSTEM)
        task = json.loads(body["messages"][1]["content"])
        self.assertEqual(task["context_bundle"], original)
        self.assertEqual(task["previous_module"], previous)
        self.assertEqual(task["effective_product_snapshot"], product())
        self.assertEqual(task["generation_instruction"], original["generation_instruction"])
        self.assertEqual(task["requested_module_key"], "opening_short")
        self.assertNotIn("must_mention_each_version", task)
        self.assertNotIn("version_count", task)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn(TEST_KEY, request.content.decode())
        self.assertEqual(result["usage"], {"prompt_tokens": 240, "completion_tokens": 40, "total_tokens": 280,
                                          "prompt_tokens_details": {"cached_tokens": 20}})
        self.assertEqual(result["sentence_details"][0]["fact_ids"], ["NAME"])
        self.assertNotIn("reasoning_content", json.dumps(result))
        self.assertNotIn(TEST_KEY, json.dumps(result))

    async def test_short_module_does_not_require_full_feature_coverage_or_700_characters(self):
        value, _ = await self.call()
        self.assertEqual(value["sentences"], ["接下来看看这双足弓支撑凉拖鞋。"])
        self.assertTrue(content_issues(product(), [{"sentences": [{"text": value["sentences"][0]}]}]))
        self.assertEqual(content_issues(product(), [{"sentences": [{"text": value["sentences"][0]}]}], require_coverage=False), [])

    async def test_transition_has_own_module_identity_and_no_welcome_stage_extraction(self):
        c = context(); c["module_key"] = "transition"
        value, _ = await self.call(output("transition", [sentence("接下来我们换个角度继续看。", "engagement", [])]), key="transition", context_value=c)
        self.assertEqual(value["module_key"], "transition")
        self.assertEqual(value["sentences"], ["接下来我们换个角度继续看。"])

    async def test_wrong_module_is_rejected_instead_of_silently_extracting(self):
        with self.assertRaises(BusinessError) as error:
            await self.call(output("campaign"))
        self.assertEqual(error.exception.code, "MODULE_RESPONSE_MISMATCH")

    async def test_legacy_versions_or_stage_or_unknown_fields_are_rejected(self):
        for raw in ({"versions": [{"blocks": []}]}, {**output(), "blocks": []}, output(sentences=[{**sentence(), "stage": "welcome"}])):
            with self.subTest(raw=raw), self.assertRaises(BusinessError) as error:
                await self.call(raw)
            self.assertEqual(error.exception.code, "MODULE_RESPONSE_INVALID")

    async def test_no_extra_long_short_module_or_merged_sentences(self):
        for raw in (output(sentences=[sentence(), sentence(), sentence()]),
                    output(sentences=[sentence("先看这双鞋。然后看颜色。")]), output(sentences=[])):
            with self.subTest(raw=raw), self.assertRaises(BusinessError):
                await self.call(raw)

    async def test_fact_reference_failures_are_not_swallowed(self):
        for line in (sentence(fact_ids=[]), sentence(fact_ids=["UNKNOWN"]), sentence(kind="engagement"),
                     sentence(fact_ids=["PROMOTION"])):
            with self.subTest(line=line), self.assertRaises(BusinessError) as error:
                await self.call(output(sentences=[line]))
            self.assertEqual(error.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")

    async def test_disallowed_claims_checked_in_both_display_and_spoken_text(self):
        line = sentence("这双鞋有足弓支撑。", fact_ids=["SELLING"])
        line["spoken_text"] = "站多久都不会累。"
        with self.assertRaises(BusinessError) as error:
            await self.call(output(sentences=[line]))
        self.assertEqual(error.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")

    async def test_existing_performance_checks_remain_without_all_feature_rule(self):
        with self.assertRaises(BusinessError) as error:
            await self.call(output(sentences=[sentence("穿着透气不闷脚。", fact_ids=["SELLING"])]))
        self.assertEqual(error.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")

    def test_nonstackable_rule_required_when_offer_mentioned_but_not_plain_opening(self):
        p = product(); p["promotion"] = "八折优惠，不与其他优惠叠加。"
        self.assertEqual(validate_output(output(), p, "opening_short"), ["接下来看看这双足弓支撑凉拖鞋。"])
        with self.assertRaises(BusinessError):
            validate_output(output("campaign", [sentence("现在有八折优惠。", fact_ids=["PROMOTION"])]), p, "campaign")
        self.assertEqual(validate_output(output("campaign", [sentence("现在有八折优惠，不与其他优惠叠加。", fact_ids=["PROMOTION"])]), p, "campaign"), ["现在有八折优惠，不与其他优惠叠加。"])

    async def test_truncated_refusal_tool_or_non_json_never_marked_valid(self):
        for change in ({"finish_reason": "length"}, {"message": {"content": "not-json"}},
                       {"message": {"content": "{}", "refusal": "no"}},
                       {"message": {"content": "{}", "tool_calls": [{"id": "x"}]}}):
            reply = provider(); reply["choices"][0].update(change)
            with self.subTest(change=change), self.assertRaises(BusinessError) as error:
                await self.call(response=reply)
            self.assertEqual(error.exception.code, "MODULE_RESPONSE_INVALID")

    async def test_http_errors_never_retry_or_return_body_or_key(self):
        for status, code in ((401, "MODULE_PROVIDER_REJECTED"), (402, "MODULE_PROVIDER_REJECTED"),
                             (429, "MODULE_PROVIDER_REJECTED"), (503, "MODULE_PROVIDER_OUTCOME_UNKNOWN"),
                             (302, "MODULE_PROVIDER_REJECTED")):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(status, json={"error": TEST_KEY}, headers={"location": "https://evil.invalid"})
            with self.subTest(status=status), self.assertRaises(BusinessError) as error:
                await generate_one_module(product(), context(), "opening_short", [], api_key=TEST_KEY, transport=httpx.MockTransport(handler))
            self.assertEqual(error.exception.code, code)
            self.assertEqual(len(seen), 1)
            self.assertNotIn(TEST_KEY, json.dumps(error.exception.payload()))

    async def test_transport_timeout_unknown_once_no_secret_error_details(self):
        seen = []
        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout(TEST_KEY, request=request)
        with self.assertRaises(BusinessError) as error:
            await generate_one_module(product(), context(), "opening_short", [], api_key=TEST_KEY, transport=httpx.MockTransport(handler))
        self.assertEqual(error.exception.code, "MODULE_PROVIDER_OUTCOME_UNKNOWN")
        self.assertEqual(len(seen), 1)
        self.assertNotIn(TEST_KEY, json.dumps(error.exception.payload()))

    async def test_missing_key_or_oversize_context_never_sends(self):
        def forbidden(_):
            raise AssertionError("must not send")
        for key, c in (("", context()), (TEST_KEY, {**context(), "too_large": "a" * (8 * 1024 * 1024)})):
            with self.subTest(key=bool(key)), self.assertRaises(BusinessError):
                await generate_one_module(product(), c, "opening_short", [], api_key=key, transport=httpx.MockTransport(forbidden))

    async def test_cancellation_not_swallowed_or_retried(self):
        def handler(_):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await generate_one_module(product(), context(), "opening_short", [], api_key=TEST_KEY, transport=httpx.MockTransport(handler))

    async def test_adapter_uses_exact_draft_not_current_room_or_last_record(self):
        c = context(); c["records"].append({"ref": {"kind": "draft", "id": "different"}, "body": {"draft": {"commerce": commerce_config.parse_room({"scene": "错误场景"})}}})
        seen = []
        def handler(request):
            seen.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
            return httpx.Response(200, json=provider())
        server = SimpleNamespace(commerce=commerce_config, os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": TEST_KEY}),
                                 _generate_remote_once=lambda *args: self.fail("must not use whole-script generator"),
                                 settings=lambda *args: self.fail("must not read mutable room settings"))
        adapter = ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(handler))
        result = await adapter.generate_module(c, "opening_short", product(), [])
        self.assertEqual(result["module_key"], "opening_short")
        self.assertEqual(seen[0]["effective_product_snapshot"]["promo_price"], 107)
        self.assertEqual(seen[0]["effective_product_snapshot"]["_room_scene"], "夜间居家")
        self.assertEqual(seen[0]["context_bundle"], c)
        self.assertIn("不与其他优惠叠加", seen[0]["effective_product_snapshot"]["promotion"])

    async def test_missing_ambiguous_or_wrong_ref_draft_fails_before_provider(self):
        adapter = ExistingVoiceAdapter(SimpleNamespace(commerce=commerce_config))
        for c in ({"records": []}, {**context(), "draft_ref": {"id": "wrong"}},
                  {**context(), "records": context()["records"] * 2}):
            with self.subTest(c=c), self.assertRaises(BusinessError) as error:
                await adapter.generate_module(c, "opening_short", product(), [])
            self.assertEqual(error.exception.code, "MODULE_DRAFT_SNAPSHOT_MISSING")

    def test_serialization_and_plan_module_mismatch_fail_before_http(self):
        for c in ({**context(), "module_key": "selling_points"}, {**context(), "generation_instruction": []},
                  {**context(), "bad": float("nan")}):
            with self.subTest(c=c), self.assertRaises(BusinessError):
                build_request(product(), c, "opening_short", [], model="deepseek-flash")

    async def test_real_isolated_job_persists_dict_module_and_provider_usage_receipts(self):
        calls = []
        def handler(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            key = task["requested_module_key"]
            calls.append(task)
            return httpx.Response(200, json=provider(output(key, [sentence("这款商品有足弓支撑。", fact_ids=["SELLING"])])))
        with tempfile.TemporaryDirectory(prefix="module-job-isolated-") as root:
            server = SimpleNamespace(commerce=commerce_config, os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": TEST_KEY}))
            adapter = ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(handler))
            service = PreliveService(root, adapter)
            counter = 0
            async def command(name, args):
                nonlocal counter
                counter += 1
                return (await service.execute_command(name, args, MutationContext(operation_id="module-job-" + str(counter)), Principal(kind="human")))["data"]
            try:
                fields = Product(name="足弓支撑凉拖鞋", price=178, currency="TWD", selling_points="足弓支撑，厚底软弹。").model_dump()
                p = (await command("save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [{"op": "set", "path": "/name", "value": fields["name"]}], "fields": fields}))["product_ref"]
                room = (await command("create_room", {"name": "模块测试临时房间"}))["room_ref"]
                prep = (await command("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
                prep = (await command("update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [p]}))["preparation_ref"]
                draft = await command("save_session_draft", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "draft": {"theme": "居家好物", "commerce": {}}})
                prep = draft["preparation_ref"]
                bundle = await command("save_context_bundle", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "product_refs": [p], "draft_ref": draft["draft_ref"]})
                prep = bundle["preparation_ref"]
                plan = await command("save_script_plan", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "bundle_ref": bundle["context_bundle_ref"], "items": [{"product_ref": p, "variant_id": "module-test-version", "generation_mode": "module", "modules": [{"key": k, "action": "disable" if k in {"campaign", "transition"} else "generate", "instruction": "强调已有的足弓支撑特点，不添加无据性能。"} for k in MODULE_KEYS]}]})
                prep = plan["preparation_ref"]
                job = await command("submit_script_change", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "plan_ref": plan["script_plan_ref"], "context_bundle_ref": bundle["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
                await service.drain_jobs()
                state = (await service.query("read_job", {"job_id": job["job_id"]}))["body"]
                self.assertEqual(state["status"], "succeeded", state)
                attempts = state["items"][0]["provider_attempts"]
                self.assertEqual(len(attempts), 5)
                self.assertEqual(len(calls), 5)
                self.assertEqual(sum(a["output"]["sentences"]["usage"]["total_tokens"] for a in attempts), 1400)
                for attempt in attempts:
                    receipt = attempt["output"]["sentences"]
                    self.assertEqual(attempt["state"], "result_received")
                    self.assertEqual(receipt["provider"], "deepseek")
                    self.assertEqual(receipt["sentence_details"][0]["fact_ids"], ["SELLING"])
                    self.assertEqual(len(receipt["prompt_revision"]), 16)
                self.assertNotIn(TEST_KEY, json.dumps(state))
                self.assertNotIn("must-not-be-recorded", json.dumps(state))
                prepared = await service.query("read_preparation", {"preparation_id": prep["id"]})
                script = await service.query("read_script", {"ref": prepared["body"]["script_refs"][0]})
                self.assertEqual(script["modules"]["opening_short"]["sentences"][0]["text"], "这款商品有足弓支撑。")
            finally:
                await service.close()


if __name__ == "__main__":
    unittest.main()
