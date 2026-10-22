"""Offline public-response normalization, strict rejection and durable audit."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app import commerce_config
from app.prelive.module_generation import (
    ModuleNormalizationAudit, ModuleOutput, ModuleStructureDetails,
    finish_received_module, generate_one_module, received_module_normalization_audit,
    validate_output,
)
from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError, digest
from app.prelive_bridge import ExistingVoiceAdapter
from shengbo_contracts.common import MutationContext, Principal


KEY = "offline-rc11-test-key"
PRIVATE = "private-envelope-must-not-be-recorded"
FIXTURE = Path(__file__).parent / "fixtures/rc10_empty_root_fact_ids.json"


def line(text="请按自己的实际需要选择。", *, kind="engagement", fact_ids=None):
    return {"display_text": text, "spoken_text": text, "kind": kind,
            "fact_ids": [] if fact_ids is None else fact_ids}


def raw_module(key="selling_points", **extra):
    return {"module_key": key, "sentences": [line()], **extra}


def receipt(content):
    return {"provider": "deepseek", "model": "offline-model", "module_key": "selling_points",
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            "finish_reason": "stop", "unsupported_response": False,
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "usage_state": "known"}


def provider_response(content):
    return {"id": "offline-rc11-response", "usage": receipt("")["usage"],
            "choices": [{"finish_reason": "stop", "message": {
                "content": content, "reasoning_content": PRIVATE}}]}


def test_exact_rc10_public_response_replays_without_changing_original_or_schema():
    sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
    original = deepcopy(sample["receipt"])
    raw = json.loads(original["content"])
    assert set(raw) - {"module_key", "sentences"} == {"fact_ids"}
    assert raw["fact_ids"] == []
    assert hashlib.sha256(original["content"].encode()).hexdigest() == sample["public_content_sha256"]
    with pytest.raises(ValidationError):
        ModuleOutput.model_validate(raw)  # Original strict DTO was not loosened.
    result = finish_received_module(sample["receipt"], sample["product"], "selling_points")
    assert result["sentences"] == [s["spoken_text"] for s in raw["sentences"]]
    assert result["sentence_details"] == raw["sentences"]
    assert sample["receipt"] == original
    audit = ModuleNormalizationAudit.model_validate(result["normalization"])
    assert audit.rule_id == "empty_root_fact_ids_v1"
    assert audit.raw_content_hash == "sha256:" + sample["public_content_sha256"]
    assert audit.derived_content_hash == digest({k: v for k, v in raw.items() if k != "fact_ids"})
    assert audit.removed_paths == ["/fact_ids"]
    assert audit.content_approved is audit.semantic_changes is False
    assert result["usage"] == original["usage"]


@pytest.mark.parametrize("extra", [None, ["SELLING"], False, {}, "", 0])
def test_nonempty_null_and_wrong_typed_root_fact_ids_are_never_normalized(extra):
    value = receipt(raw_module(fact_ids=extra))
    assert received_module_normalization_audit(value) is None
    with pytest.raises(BusinessError) as caught:
        finish_received_module(value, {"name": "测试商品"}, "selling_points")
    assert caught.value.code == "MODULE_RESPONSE_INVALID"
    assert {"path": "/fact_ids", "type": "extra_forbidden"} in caught.value.details["issues"]


def test_empty_root_annotation_does_not_excuse_any_other_extra_field():
    value = receipt(raw_module(fact_ids=[], arbitrary_secret_key="never-echo-value"))
    assert received_module_normalization_audit(value) is None
    with pytest.raises(BusinessError) as caught:
        finish_received_module(value, {"name": "测试商品"}, "selling_points")
    assert caught.value.code == "MODULE_RESPONSE_INVALID"
    encoded = json.dumps(caught.value.payload())
    assert "arbitrary_secret_key" not in encoded and "never-echo-value" not in encoded
    assert {"path": "/unknown_field", "type": "extra_forbidden"} in caught.value.details["issues"]


@pytest.mark.parametrize("raw", [
    {"module_key": "selling_points", "fact_ids": []},
    {"module_key": "selling_points", "sentences": "not-a-list", "fact_ids": []},
])
def test_empty_annotation_never_fills_missing_or_wrong_typed_required_fields(raw):
    value = receipt(raw)
    assert received_module_normalization_audit(value)["content_approved"] is False
    with pytest.raises(BusinessError) as caught:
        finish_received_module(value, {"name": "测试商品"}, "selling_points")
    assert caught.value.code == "MODULE_RESPONSE_INVALID"
    assert caught.value.details["validation_stage"] == "module_dto"


def test_clean_response_does_not_add_normalization_or_change_its_sentences():
    value = receipt(raw_module())
    original = deepcopy(value)
    result = finish_received_module(value, {"name": "测试商品"}, "selling_points")
    assert "normalization" not in result and received_module_normalization_audit(value) is None
    assert result["sentence_details"] == json.loads(original["content"])["sentences"]
    assert value == original


@pytest.mark.parametrize("content", [
    '{"module_key":"selling_points","sentences":[],"fact_ids":["SELLING"],"fact_ids":[]}',
    '{"module_key":"selling_points","sentences":[{"kind":"engagement","kind":"product_claim"}],"fact_ids":[]}',
    '{"module_key":"selling_points","module_key":"cta","sentences":[],"fact_ids":[]}',
    r'{"module_key":"selling_points","sentences":[],"fact_ids":["SELLING"],"fact_\u0069ds":[]}',
])
def test_duplicate_keys_at_any_level_remain_strictly_rejected(content):
    value = receipt(content)
    assert received_module_normalization_audit(value) is None
    with pytest.raises(BusinessError) as caught:
        finish_received_module(value, {"name": "测试商品"}, "selling_points")
    assert caught.value.details == {"validation_stage": "module_json", "issues": [
        {"path": "/", "type": "json_duplicate_key"}]}


def test_structure_error_is_closed_paths_types_without_pydantic_input_message_or_context():
    raw = raw_module()
    raw["sentences"][0]["spoken_text"] = "sensitive-value\nnever-echo"
    raw["sentences"][0]["sensitive-key"] = {"private": "sensitive-input"}
    with pytest.raises(BusinessError) as caught:
        validate_output(raw, {"name": "测试商品"}, "selling_points")
    details = caught.value.details
    assert set(details) == {"validation_stage", "issues"}
    assert ModuleStructureDetails.model_validate(details).validation_stage == "module_dto"
    assert {"path": "/sentences/0/spoken_text", "type": "value_error"} in details["issues"]
    assert {"path": "/sentences/0/unknown_field", "type": "extra_forbidden"} in details["issues"]
    assert all(set(issue) == {"path", "type"} for issue in details["issues"])
    encoded = json.dumps(caught.value.payload())
    for forbidden in ["sensitive-value", "never-echo", "sensitive-key", "sensitive-input", '"input"', '"msg"', '"ctx"']:
        assert forbidden not in encoded


def test_normalization_does_not_accept_invalid_facts_wrong_module_or_incomplete_offer():
    invalid_fact = {**raw_module(fact_ids=[]), "sentences": [line("这是商品。", kind="product_claim", fact_ids=["UNKNOWN"])]}
    incomplete_offer = {**raw_module("campaign", fact_ids=[]), "sentences": [
        line("现在有八折优惠。", kind="product_claim", fact_ids=["PROMOTION"])]}
    for raw, product, key, code in [
        (invalid_fact, {"name": "测试商品"}, "selling_points", "GENERATED_MODULE_NEEDS_REVIEW"),
        (raw_module("cta", fact_ids=[]), {"name": "测试商品"}, "selling_points", "MODULE_RESPONSE_MISMATCH"),
        (incomplete_offer, {"name": "测试商品", "promotion": "八折优惠，不与其他优惠叠加。"}, "campaign", "GENERATED_MODULE_NEEDS_REVIEW"),
    ]:
        value = receipt(raw)
        assert received_module_normalization_audit(value)
        with pytest.raises(BusinessError) as caught:
            finish_received_module(value, product, key)
        assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("content, expected_type", [
    ('{"module_key":"selling_points","module_key":"cta","sentences":[]}', "json_duplicate_key"),
    ('not-json', "json_invalid"),
    ('{"module_key":"selling_points","sentences":[],"fact_ids":NaN}', "json_non_finite"),
])
async def test_response_observer_receives_original_and_usage_before_every_parse_rejection(content, expected_type):
    observed, requests = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=provider_response(content))

    with pytest.raises(BusinessError) as caught:
        await generate_one_module({"name": "测试商品"}, {}, "selling_points", [], api_key=KEY,
            transport=httpx.MockTransport(handler), response_observer=observed.append)
    assert caught.value.code == "MODULE_RESPONSE_INVALID"
    assert caught.value.details["issues"][0]["type"] == expected_type
    assert len(requests) == len(observed) == 1
    assert observed[0]["content"] == content
    assert observed[0]["usage"]["total_tokens"] == 18
    assert PRIVATE not in json.dumps(observed) and KEY not in json.dumps(caught.value.payload())


@pytest_asyncio.fixture
async def service_harness(tmp_path):
    class Harness:
        def __init__(self):
            self.counter = 0
            self.calls = []
            self.fail_fact = False
            self.duplicate = False

        async def cmd(self, key, args):
            self.counter += 1
            return (await self.service.execute_command(key, args,
                MutationContext(operation_id="rc11-" + str(self.counter)), Principal(kind="human")))["data"]

        async def setup(self):
            product = (await self.cmd("save_product", {"schema_hash": self.service.schema()["schema_hash"],
                "fields": Product(name="离线规范化测试口红", price=129).model_dump(), "patches": []}))["product_ref"]
            room = (await self.cmd("create_room", {"name": "RC11离线"}))["room_ref"]
            prep = (await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
            selected = await self.cmd("update_preparation_products", {"preparation_id": prep["id"],
                "expected_revision": prep["revision"], "selected_product_refs": [product]})
            draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
                "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "测试", "commerce": {}}})
            bundle = await self.cmd("save_context_bundle", {"preparation_id": prep["id"],
                "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
            plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"],
                "expected_revision": bundle["preparation_ref"]["revision"], "bundle_ref": bundle["context_bundle_ref"],
                "items": [{"product_ref": product, "variant_id": "one", "generation_mode": "module", "modules": [
                    {"key": key, "action": "generate"} for key in ["opening_long", "product_intro", "selling_points", "cta"]]}]})
            return await self.cmd("submit_script_change", {"preparation_id": prep["id"],
                "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
                "context_bundle_ref": bundle["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})

    h = Harness()

    def handler(request):
        task = json.loads(json.loads(request.content)["messages"][1]["content"])
        key = task["requested_module_key"]
        h.calls.append(key)
        raw = raw_module(key)
        if key == "selling_points":
            raw["fact_ids"] = []
            if h.fail_fact:
                raw["sentences"] = [line("这是商品。", kind="product_claim", fact_ids=["UNKNOWN"])]
        content = json.dumps(raw, ensure_ascii=False)
        if h.duplicate:
            content = content[:-1] + ',"module_key":"cta"}'
        return httpx.Response(200, json=provider_response(content))

    server = SimpleNamespace(commerce=commerce_config, os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": KEY}))
    h.service = PreliveService(tmp_path / "voice", ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(handler)))
    h.service.wake_worker = lambda: None
    yield h
    await h.service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_fact", [False, True])
async def test_audit_is_durable_before_semantic_validation_even_when_rejected(service_harness, fail_fact):
    h = service_harness
    h.fail_fact = fail_fact
    created = await h.setup()
    await h.service.drain_jobs()
    job = await h.service.query("read_job", {"job_id": created["job_id"]})
    item = job["body"]["items"][0]
    attempt = next(a for a in item["provider_attempts"] if a["purpose"] == "copywriting.selling_points")
    audit = attempt["normalization"]
    assert ModuleNormalizationAudit.model_validate(audit).content_approved is False
    assert attempt["validation_state"] == ("rejected" if fail_fact else "accepted")
    assert attempt["usage"]["total_tokens"] == 18
    assert attempt["state"] in {"response_received", "result_received"}
    assert item["status"] == ("failed" if fail_fact else "succeeded")
    with h.service.store.transaction() as connection:
        archived = h.service.store.exact(connection, attempt["provider_response_ref"])
        assert archived["body"]["response"]["usage"]["total_tokens"] == 18
        original = archived["body"]["response"]["content"]
        assert json.loads(original)["fact_ids"] == []
        assert audit["raw_content_hash"] == "sha256:" + hashlib.sha256(original.encode()).hexdigest()
        assert "normalization" not in archived["body"]["response"]
        assert PRIVATE not in json.dumps(archived) and KEY not in json.dumps(archived)
        events_before = connection.execute("SELECT COUNT(*) FROM prelive_events").fetchone()[0]
    before = deepcopy(job["ref"])
    h.service._record_module_normalization(created["job_id"], item["item_id"], attempt["attempt_id"])
    assert (await h.service.query("read_job", {"job_id": created["job_id"]}))["ref"] == before
    with h.service.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM prelive_events").fetchone()[0] == events_before
        assert h.service.store.exact(connection, attempt["provider_response_ref"]) == archived
    assert h.calls == ["opening_long", "product_intro", "selling_points"] + ([] if fail_fact else ["cta"])
    if fail_fact:
        assert item["error"]["code"] == "GENERATED_MODULE_NEEDS_REVIEW"
        assert set(item["module_results"]) == {"opening_long", "product_intro"}
    else:
        assert set(item["module_results"]) == {"opening_long", "product_intro", "selling_points", "cta"}


@pytest.mark.asyncio
async def test_duplicate_rejection_keeps_durable_usage_and_never_becomes_transport_unknown(service_harness):
    h = service_harness
    h.duplicate = True
    created = await h.setup()
    await h.service.drain_jobs()
    job = (await h.service.query("read_job", {"job_id": created["job_id"]}))["body"]
    item = job["items"][0]
    attempt = item["provider_attempts"][0]
    assert h.calls == ["opening_long"]
    assert item["error"]["code"] == "MODULE_RESPONSE_INVALID"
    assert item["error"]["details"]["issues"][0]["type"] == "json_duplicate_key"
    assert attempt["state"] == "response_received" and attempt["validation_state"] == "rejected"
    assert attempt["usage"]["total_tokens"] == 18
    assert "normalization" not in attempt
    with h.service.store.transaction() as connection:
        original = h.service.store.exact(connection, attempt["provider_response_ref"])["body"]["response"]
        assert original["content"].count('"module_key"') == 2
        assert original["usage"]["total_tokens"] == 18
        state = h.service.store.get(connection, "prelive_external_request", attempt["request_key"])["body"]["state"]
        assert state == "response_received"
