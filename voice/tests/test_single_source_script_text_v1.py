import json
from copy import deepcopy
import httpx
import pytest
from app.prelive.whole_script_generation import (TEXT_CONTRACT, build_request, finish_received_script,
    generate_one_script, received_whole_script_normalization_audit)
from app.prelive.storage import BusinessError


PRODUCT = {"name": "训练套件", "price": 23.58, "currency": "CNY"}
ACTIONS = [{"key": "product_intro", "action": "generate"}]


def receipt(row):
    return {"protocol": "whole-script-v1", "finish_reason": "stop", "content": json.dumps({
        "modules": [{"module_key": "product_intro", "sentences": [row]}]}, ensure_ascii=False)}


def test_new_request_asks_for_one_text_not_two_independently_written_fields():
    body, _ = build_request(PRODUCT, {}, ACTIONS, model="test")
    task = json.loads(body["messages"][1]["content"])
    props = task["response_schema"]["$defs"]["ScriptUtterance"]["properties"]
    assert set(props) == {"text", "kind", "fact_ids"}
    assert props["fact_ids"]["items"]["enum"] == task["allowed_fact_ids"]
    assert task["text_contract"] == "single-source-utterance-v1"


def test_single_text_projection_preserves_all_words_and_punctuation():
    source = receipt({"text": "看看训练套件。一起了解一下吧！", "kind": "product_claim", "fact_ids": ["NAME"]})
    original = source["content"]
    result = finish_received_script(source, PRODUCT, ACTIONS)
    rows = result["modules"]["product_intro"]["sentence_details"]
    assert "".join(s["spoken_text"] for s in rows) == "看看训练套件。一起了解一下吧！"
    assert all(s["display_text"] == s["spoken_text"] and s["fact_ids"] == ["NAME"] for s in rows)
    audit = result["normalization"]
    assert audit["text_unchanged"] and not audit["semantic_changes"] and not audit["content_approved"]
    assert audit["single_source_text_projections"] and source["content"] == original


def test_legacy_mismatched_two_texts_are_not_silently_changed():
    source = receipt({"display_text": "再说户外场景。一起了解一下吧。", "spoken_text": "一起了解一下吧。", "kind": "engagement", "fact_ids": []})
    original = source["content"]
    with pytest.raises(BusinessError):
        finish_received_script(source, PRODUCT, ACTIONS)
    assert source["content"] == original


@pytest.mark.parametrize("row", [
    {"text": "看看训练套件。", "kind": "product_claim", "fact_ids": ["material"]},
    {"text": "绝对不会受伤。", "kind": "product_claim", "fact_ids": ["NAME"]},
    {"text": "看看训练套件。", "display_text": "看看训练套件。", "kind": "product_claim", "fact_ids": ["NAME"]},
])
def test_projection_does_not_bypass_existing_rejection(row):
    with pytest.raises(BusinessError):
        finish_received_script(receipt(row), PRODUCT, ACTIONS)


@pytest.mark.parametrize("row", [
    {"display_text": "欢迎来到直播间。", "spoken_text": "欢迎新来的朋友。", "kind": "engagement", "fact_ids": []},
    {"display_text": "欢迎来到直播间。", "spoken_text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []},
    {"text": "欢迎来到直播间。", "spoken_text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []},
    {"text": "欢迎来到直播间。", "display_text": "欢迎来到直播间。", "spoken_text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []},
])
def test_new_marked_receipt_cannot_fall_back_to_dual_or_mixed_fields(row):
    source = {**receipt(row), "text_contract": TEXT_CONTRACT}
    original = deepcopy(source)
    with pytest.raises(BusinessError) as caught:
        finish_received_script(source, PRODUCT, ACTIONS)
    assert caught.value.code == "WHOLE_SCRIPT_RESPONSE_INVALID"
    assert caught.value.details["validation_stage"] == "single_source_dto"
    assert received_whole_script_normalization_audit(source) is None
    assert source == original


@pytest.mark.parametrize("marker", [None, "", "single-source-utterance-v999"])
def test_explicit_unknown_contract_is_not_treated_as_legacy_absence(marker):
    source = {**receipt({"text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []}), "text_contract": marker}
    with pytest.raises(BusinessError) as caught:
        finish_received_script(source, PRODUCT, ACTIONS)
    assert caught.value.details["validation_stage"] == "text_contract"


def test_legacy_absent_marker_keeps_distinct_valid_display_and_spoken_without_reinterpretation():
    source = receipt({"display_text": "欢迎来到直播间。", "spoken_text": "欢迎新来的朋友。", "kind": "engagement", "fact_ids": []})
    original = deepcopy(source)
    result = finish_received_script(source, PRODUCT, ACTIONS)
    detail = result["modules"]["product_intro"]["sentence_details"][0]
    assert detail["display_text"] == "欢迎来到直播间。"
    assert detail["spoken_text"] == "欢迎新来的朋友。"
    assert "text_contract" not in result and source == original


@pytest.mark.asyncio
@pytest.mark.parametrize("row, accepted", [
    ({"text": "看看训练套件。一起了解一下吧！", "kind": "product_claim", "fact_ids": ["NAME"]}, True),
    ({"display_text": "欢迎来到直播间。", "spoken_text": "欢迎新来的朋友。", "kind": "engagement", "fact_ids": []}, False),
    ({"text": "欢迎来到直播间。", "spoken_text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []}, False),
])
async def test_actual_new_transport_receipt_marks_contract_before_observer_and_validation(row, accepted):
    observed, sent = [], []
    content = receipt(row)["content"]

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})

    kwargs = {"api_key": "isolated-source-contract-only", "transport": httpx.MockTransport(handler),
              "response_observer": lambda value: observed.append(deepcopy(value))}
    if accepted:
        result = await generate_one_script(PRODUCT, {}, ACTIONS, **kwargs)
        assert result["text_contract"] == TEXT_CONTRACT
        details = result["modules"]["product_intro"]["sentence_details"]
        assert "".join(item["spoken_text"] for item in details) == row["text"]
        assert all(item["display_text"] == item["spoken_text"] for item in details)
    else:
        with pytest.raises(BusinessError) as caught:
            await generate_one_script(PRODUCT, {}, ACTIONS, **kwargs)
        assert caught.value.details["validation_stage"] == "single_source_dto"
    assert len(sent) == len(observed) == 1
    assert observed[0]["text_contract"] == TEXT_CONTRACT
    assert observed[0]["content"] == content and observed[0]["usage"]["total_tokens"] == 30


def test_new_marker_rejects_a_mixture_of_single_and_legacy_sentence_entries():
    source = receipt({"text": "欢迎来到直播间。", "kind": "engagement", "fact_ids": []})
    value = json.loads(source["content"])
    value["modules"][0]["sentences"].append({"display_text": "一起了解商品。", "spoken_text": "一起看看商品。",
        "kind": "engagement", "fact_ids": []})
    source.update(text_contract=TEXT_CONTRACT, content=json.dumps(value, ensure_ascii=False))
    with pytest.raises(BusinessError) as caught:
        finish_received_script(source, PRODUCT, ACTIONS)
    assert caught.value.details["validation_stage"] == "single_source_dto"


def test_text_contract_change_versions_prompt_identity_without_rewriting_system(monkeypatch):
    from app.prelive import whole_script_generation as whole
    before, system = whole.prompt_revision(), whole.SYSTEM
    monkeypatch.setattr(whole, "TEXT_CONTRACT", "isolated-next-contract")
    assert whole.prompt_revision() != before
    assert whole.SYSTEM == system


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_real_service_persists_marker_before_rejection_and_returns_public_generation_metadata(accepted):
    import test_whole_script_generation_v1 as base
    harness = base.WholeServiceTests()
    await harness.asyncSetUp()
    try:
        seen = []

        def respond(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            seen.append(task)
            if accepted:
                value = base.envelope(task["requested_module_keys"])
            else:
                value = base.envelope(raw=json.dumps({"modules": [base.module(key)
                    for key in task["requested_module_keys"]]}, ensure_ascii=False))
            return httpx.Response(200, json=value)

        harness.service.adapter.module_transport = httpx.MockTransport(respond)
        prep, job = await harness.setup_job()
        await harness.service.drain_jobs()
        current = await harness.read_job(job)
        assert current["status"] == ("succeeded" if accepted else "failed")
        attempt = current["items"][0]["provider_attempts"][0]
        assert attempt["generation_response"]["text_contract"] == TEXT_CONTRACT
        assert attempt["generation_response"]["approvable"] is False
        if accepted:
            assert attempt["output"]["text_contract"] == TEXT_CONTRACT
        with harness.service.store.transaction() as connection:
            archive = harness.service.store.exact(connection, attempt["provider_response_ref"])
            response = archive["body"]["response"]
            assert response["text_contract"] == TEXT_CONTRACT
            assert response["usage"]["total_tokens"] == 300
            assert len(harness.service.store.all(connection, "provider_response")) == 1
            if not accepted:
                assert harness.service.store.all(connection, "module") == []
        assert len(seen) == 1
        saved = await harness.service.query("read_preparation", {"preparation_id": prep["id"]})
        assert bool(saved["body"]["script_refs"]) is accepted
    finally:
        await harness.asyncTearDown()
