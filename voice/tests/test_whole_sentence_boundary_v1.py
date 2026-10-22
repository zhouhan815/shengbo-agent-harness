"""Offline boundary fixes from the first merchant-input live attempt.

The original provider text remains immutable. Deterministic sentence expansion
does not approve claims or relax fact/offer/DTO validation.
"""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from app.copywriting_quality import content_issues
from app.prelive.storage import BusinessError
from app.prelive.whole_script_generation import (PROTOCOL, finish_received_script,
    received_whole_script_normalization_audit)
import test_whole_script_generation_v1 as base


def receipt(sentence, *, key="opening_short"):
    return {"protocol": PROTOCOL, "finish_reason": "stop", "unsupported_response": False,
        "content": json.dumps({"modules": [{"module_key": key, "sentences": [sentence]}]}, ensure_ascii=False),
        "usage": {"total_tokens": 300}, "usage_state": "known"}


def line(text, *, spoken=None, kind="engagement", refs=None):
    return {"display_text": text, "spoken_text": text if spoken is None else spoken,
        "kind": kind, "fact_ids": [] if refs is None else refs}


@pytest.mark.parametrize("text, assertion", [
    ("家里的小摆件、公仔是不是常常散在桌面，想找地方集中又不占地方？", False),
    ("想要一个不占地方的展示位吗？", False),
    ("这款架子不占地方。", True),
    ("这款不占地方，你想要吗？", True),
    ("想要这款不占地方的架子吗？", True),
    ("这么不占地方，难道不方便吗？", True),
    ("想要不占地方的展示位吗？这款架子不占地方。", True),
])
def test_customer_wish_question_is_not_a_product_performance_assertion(text, assertion):
    issues = content_issues({"name": "测试收纳架", "selling_points": "轻便。"},
        [{"sentences": [{"text": text}]}], require_coverage=False)
    assert any("不占地方" in issue for issue in issues) is assertion


def test_space_claim_still_requires_its_own_support_not_an_unrelated_wear_feature():
    text = "这款轻便，而且不占地方。"
    assert content_issues({"name": "测试商品", "selling_points": "轻便，耐磨处理。"},
        [{"sentences": [{"text": text}]}], require_coverage=False)
    assert not content_issues({"name": "测试商品", "selling_points": "轻便，不占地方。"},
        [{"sentences": [{"text": text}]}], require_coverage=False)


def test_equal_sentence_counts_split_losslessly_with_same_kind_refs_and_no_raw_edit():
    source = line("展示价129元。请先了解款式。", spoken="展示价一百二十九元。请先了解款式。",
        kind="product_claim", refs=["PRICE", "NAME"])
    original = receipt(source)
    before = deepcopy(original)
    result = finish_received_script(original, {"name": "测试商品", "price": 129, "currency": "CNY"},
        base.actions(["opening_short"]))
    details = result["modules"]["opening_short"]["sentence_details"]
    assert len(details) == 2
    for key in ("display_text", "spoken_text"):
        assert "".join(item[key] for item in details) == source[key]
    assert all(item["kind"] == source["kind"] and item["fact_ids"] == source["fact_ids"] for item in details)
    assert original == before
    audit = result["normalization"]
    assert audit == received_whole_script_normalization_audit(original)
    assert audit["expanded_entries"] == [{"path": "/modules/0/sentences/0", "sentence_count": 2}]
    assert audit["raw_content_hash"] == "sha256:" + hashlib.sha256(original["content"].encode()).hexdigest()
    assert audit["text_unchanged"] is True
    assert audit["content_approved"] is audit["semantic_changes"] is False
    assert result["usage"] == before["usage"]


def test_different_sentence_counts_are_not_guessed_and_error_has_exact_path():
    value = receipt(line("想有一个展示位？角落也能利用起来。", spoken="请看看展示位。"))
    assert received_whole_script_normalization_audit(value) is None
    with pytest.raises(BusinessError) as caught:
        finish_received_script(value, {"name": "商品"}, base.actions(["opening_short"]))
    assert caught.value.code == "WHOLE_SCRIPT_RESPONSE_INVALID"
    assert {"path": "/modules/0/sentences/0/display_text", "type": "value_error"} in caught.value.details["issues"]


def test_extra_fields_are_never_removed_and_paths_never_echo_provider_field_names():
    value = receipt({**line("第一句。第二句。"), "sensitive-key": "never-echo-value"})
    assert received_whole_script_normalization_audit(value) is None
    with pytest.raises(BusinessError) as caught:
        finish_received_script(value, {"name": "商品"}, base.actions(["opening_short"]))
    assert {"path": "/modules/0/sentences/0/unknown_field", "type": "extra_forbidden"} in caught.value.details["issues"]
    assert "sensitive-key" not in json.dumps(caught.value.payload())
    assert "never-echo-value" not in json.dumps(caught.value.payload())


def test_split_cannot_make_an_invalid_fact_reference_acceptable():
    value = receipt(line("这是一款商品。请了解商品。", kind="product_claim", refs=["UNKNOWN"]))
    assert received_whole_script_normalization_audit(value)["content_approved"] is False
    with pytest.raises(BusinessError) as caught:
        finish_received_script(value, {"name": "商品"}, base.actions(["opening_short"]))
    assert caught.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert "事实引用无效" in caught.value.details["issues"]


def test_normalization_does_not_drop_extra_punctuation_to_force_sentence_alignment():
    value = receipt(line("第一句？！第二句。"))
    assert received_whole_script_normalization_audit(value) is None
    with pytest.raises(BusinessError):
        finish_received_script(value, {"name": "商品"}, base.actions(["opening_short"]))


def test_normalized_module_still_obeys_the_original_short_module_sentence_limit():
    value = receipt(line("第一句。第二句。第三句。"))
    assert received_whole_script_normalization_audit(value)
    with pytest.raises(BusinessError) as caught:
        finish_received_script(value, {"name": "商品"}, base.actions(["opening_short"]))
    assert caught.value.code == "SHORT_MODULE_TOO_LONG"


@pytest_asyncio.fixture
async def service_harness():
    h = base.WholeServiceTests("test_incomplete_whole_response_archives_once_and_commits_no_partial_modules")
    await h.asyncSetUp()
    try:
        yield h
    finally:
        await h.asyncTearDown()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_fact", [False, True])
async def test_raw_usage_and_normalization_audit_are_durable_before_content_validation(service_harness, invalid_fact):
    h = service_harness
    raw_contents = []

    def handler(request):
        task = json.loads(json.loads(request.content)["messages"][1]["content"])
        h.sent.append(task)
        reply = base.envelope(task["requested_module_keys"])
        raw = json.loads(reply["choices"][0]["message"]["content"])
        raw["modules"][0]["sentences"] = [{"text": "想给公仔留个展示位？角落也能利用起来。", "kind": "engagement", "fact_ids": []}]
        if invalid_fact:
            raw["modules"][1]["sentences"][0]["fact_ids"] = ["UNKNOWN"]
        content = json.dumps(raw, ensure_ascii=False)
        raw_contents.append(content)
        reply["choices"][0]["message"]["content"] = content
        return httpx.Response(200, json=reply)

    h.service.adapter.module_transport = httpx.MockTransport(handler)
    prep, job = await h.setup_job()
    from app.prelive.whole_script_generation import validate_output

    def verify_before_validation(*args, **kwargs):
        with h.service.store.transaction() as c:
            body = h.service.store.get(c, "prelive_job", job["job_id"])["body"]
            attempt = body["items"][0]["provider_attempts"][0]
            assert attempt["normalization"]["content_approved"] is False
            archived = h.service.store.exact(c, attempt["provider_response_ref"])["body"]["response"]
            assert archived["content"] == raw_contents[0]
            assert archived["usage"]["total_tokens"] == 300
        return validate_output(*args, **kwargs)

    with patch("app.prelive.whole_script_generation.validate_output", side_effect=verify_before_validation):
        await h.service.drain_jobs()
    result = await h.read_job(job)
    assert result["status"] == ("failed" if invalid_fact else "succeeded"), result
    assert len(h.sent) == 1
    attempt = result["items"][0]["provider_attempts"][0]
    assert attempt["usage"]["total_tokens"] == 300
    with h.service.store.transaction() as c:
        saved = h.service.store.get(c, "prelive_job", job["job_id"])["body"]["items"][0]["provider_attempts"][0]
        assert saved["normalization"]["expanded_entries"][0]["sentence_count"] == 2
        assert len(h.service.store.all(c, "provider_response")) == 1
        if invalid_fact:
            assert not h.service.store.all(c, "module")
    if invalid_fact:
        assert not (await h.service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["script_refs"]
