"""A stricter metadata repair replays archived text without another provider.

All adapters and stores are isolated, and no real model or credentials are used.
"""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

import pytest
import httpx

from app.prelive import module_generation as local, whole_script_generation as whole
from app.prelive.storage import BusinessError, digest
import test_archived_revalidation_r12 as replay
import test_whole_script_generation_v1 as business


PRODUCT = {"name": "测试套装", "price": 99, "currency": "CNY"}
ACTIONS = [{"key": "product_intro", "action": "generate"}]


def row(*, kind="engagement", refs=None):
    return {"text": "这就是测试套装！", "kind": kind, "fact_ids": ["NAME"] if refs is None else refs}


def receipt(sentence, *, mode="whole", root_extra=None):
    sentence = deepcopy(sentence)
    if mode == "local":
        sentence["display_text"] = sentence["spoken_text"] = sentence.pop("text")
    module = {"module_key": "product_intro", "sentences": [sentence], **(root_extra or {})}
    value = {"modules": [module]} if mode == "whole" else module
    result = {"content": json.dumps(value, ensure_ascii=False), "finish_reason": "stop",
        "unsupported_response": False, "usage": {"total_tokens": 123}, "usage_state": "known"}
    if mode == "whole":
        result.update(protocol=whole.PROTOCOL, text_contract=whole.TEXT_CONTRACT)
    return result


def finish(value, mode):
    return (whole.finish_received_script(value, PRODUCT, ACTIONS) if mode == "whole"
            else local.finish_received_module(value, PRODUCT, "product_intro"))


def details(result, mode):
    return result["modules"]["product_intro"]["sentence_details"] if mode == "whole" else result["sentence_details"]


@pytest.mark.parametrize("mode", ["whole", "local"])
def test_referenced_engagement_is_stricter_claim_with_exact_text_refs_and_audit(mode):
    source = receipt(row(), mode=mode)
    before = deepcopy(source)
    result = finish(source, mode)
    assert source == before
    assert details(result, mode) == [{"display_text": "这就是测试套装！", "spoken_text": "这就是测试套装！",
        "kind": "product_claim", "fact_ids": ["NAME"]}]
    audit = result["normalization"]
    prefix = "/modules/0" if mode == "whole" else ""
    assert audit["metadata_changes"] == [{"rule_id": "referenced_engagement_to_product_claim_v1",
        "path": prefix + "/sentences/0/kind", "from": "engagement", "to": "product_claim", "fact_ids_preserved": True}]
    assert audit["raw_content_hash"] == "sha256:" + hashlib.sha256(source["content"].encode()).hexdigest()
    derived = {"module_key": "product_intro", "sentences": details(result, mode)}
    assert audit["derived_content_hash"] == digest({"modules": [derived]} if mode == "whole" else derived)
    assert audit["text_unchanged"] and not audit["content_approved"] and not audit["semantic_changes"]
    assert result["usage"] == source["usage"]


@pytest.mark.parametrize("mode", ["whole", "local"])
@pytest.mark.parametrize("kind,refs", [("engagement", ["UNKNOWN"]), ("engagement", ["NAME", "UNKNOWN"]),
    ("product_claim", [])])
def test_metadata_repair_never_removes_invalid_or_adds_missing_references(mode, kind, refs):
    source = receipt(row(kind=kind, refs=refs), mode=mode)
    original = deepcopy(source)
    with pytest.raises(BusinessError) as caught:
        finish(source, mode)
    assert caught.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert "事实引用无效" in caught.value.details["issues"]
    assert source == original
    normalization = (whole.received_whole_script_normalization_audit(source) if mode == "whole"
                     else local.received_module_normalization_audit(source))
    if kind == "engagement":
        assert normalization["metadata_changes"][0]["to"] == "product_claim"


@pytest.mark.parametrize("mode", ["whole", "local"])
@pytest.mark.parametrize("kind,refs", [("product_claim", ["NAME"]), ("engagement", [])])
def test_correct_annotations_are_not_reclassified(mode, kind, refs):
    source = receipt(row(kind=kind, refs=refs), mode=mode)
    result = finish(source, mode)
    assert details(result, mode)[0]["kind"] == kind
    assert details(result, mode)[0]["fact_ids"] == refs
    assert "metadata_changes" not in result.get("normalization", {})
    if mode == "local":
        assert "normalization" not in result


@pytest.mark.parametrize("mode", ["whole", "local"])
def test_other_content_checks_still_run_after_kind_is_strengthened(mode):
    source = receipt({**row(), "text": "这款绝对不会受伤。"}, mode=mode)
    with pytest.raises(BusinessError) as caught:
        finish(source, mode)
    assert caught.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert "存在未核实的保证、体验或促销承诺" in caught.value.details["issues"]


def test_root_annotation_cleanup_composes_with_metadata_audit_without_editing_raw():
    source = receipt(row(), mode="local", root_extra={"fact_ids": []})
    result = finish(source, "local")
    assert result["normalization"]["removed_paths"] == ["/fact_ids"]
    assert result["normalization"]["metadata_changes"][0]["to"] == "product_claim"
    assert json.loads(source["content"])["fact_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_fact", [False, True])
async def test_current_job_archives_raw_usage_and_kind_audit_before_content_validation(invalid_fact):
    harness = business.WholeServiceTests("test_incomplete_whole_response_archives_once_and_commits_no_partial_modules")
    await harness.asyncSetUp()
    raw_contents = []
    try:
        def handler(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            harness.sent.append(task)
            reply = business.envelope(task["requested_module_keys"])
            raw = json.loads(reply["choices"][0]["message"]["content"])
            raw["modules"][0]["sentences"][0] = {"text": "一起来了解这款商品！", "kind": "engagement",
                "fact_ids": ["UNKNOWN"] if invalid_fact else ["NAME"]}
            content = json.dumps(raw, ensure_ascii=False)
            raw_contents.append(content)
            reply["choices"][0]["message"]["content"] = content
            return httpx.Response(200, json=reply)

        harness.service.adapter.module_transport = httpx.MockTransport(handler)
        _, job = await harness.setup_job()
        original_validate = whole.validate_output

        def observe_before_validation(*args, **kwargs):
            with harness.service.store.transaction() as connection:
                current = harness.service.store.get(connection, "prelive_job", job["job_id"])["body"]
                attempt = current["items"][0]["provider_attempts"][0]
                assert attempt["normalization"]["metadata_changes"][0]["to"] == "product_claim"
                archived = harness.service.store.exact(connection, attempt["provider_response_ref"])["body"]["response"]
                assert archived["content"] == raw_contents[0]
                assert archived["usage"]["total_tokens"] == attempt["usage"]["total_tokens"] == 300
            return original_validate(*args, **kwargs)

        with patch.object(whole, "validate_output", side_effect=observe_before_validation):
            await harness.service.drain_jobs()
        current = await harness.read_job(job)
        assert current["status"] == ("failed" if invalid_fact else "succeeded"), current
        assert len(harness.sent) == 1
        if invalid_fact:
            with harness.service.store.transaction() as connection:
                assert harness.service.store.all(connection, "module") == []
    finally:
        await harness.asyncTearDown()


class OldValidatorAdapter(replay.ArchivedOnlyAdapter):
    async def _observe(self, observer, value):
        raw = json.loads(value["content"])
        if value.get("protocol") == whole.PROTOCOL:
            for module in raw["modules"]:
                module["sentences"] = [{"text": "这就是测试套装！", "kind": "engagement", "fact_ids": ["NAME"]}]
            value["text_contract"] = whole.TEXT_CONTRACT
        elif value.get("module_key") == self.reject_at:
            raw["sentences"] = [{"display_text": "这就是测试套装！", "spoken_text": "这就是测试套装！",
                "kind": "engagement", "fact_ids": ["NAME"]}]
        value["content"] = json.dumps(raw, ensure_ascii=False)
        await super()._observe(observer, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["whole_script", "module"])
async def test_explicit_archived_revalidation_preserves_old_audit_usage_and_never_reissues(mode):
    harness = replay.ArchivedRevalidationTests()
    await harness.asyncSetUp()
    try:
        harness.adapter = OldValidatorAdapter()
        harness.adapter.reject_at = "cta"
        harness.service.adapter = harness.adapter
        # Seed the historical failure under the old annotation validator, with
        # its old immutable projection audit, just as the real rejected run.
        old = lambda sentence, **kwargs: (sentence, None)
        with patch.object(local, "_normalize_referenced_engagement", side_effect=old), \
                patch.object(whole, "_normalize_referenced_engagement", side_effect=old):
            _, job = await harness.setup_job(mode)
        before = await harness.job(job)
        old_attempts = deepcopy(before["items"][0]["provider_attempts"])
        assert before["status"] == "failed"
        with harness.service.store.transaction() as connection:
            archives = [harness.service.store.exact(connection, attempt["provider_response_ref"])
                        for attempt in old_attempts]
        _, final = await harness.recover(job)
        assert final["status"] == "succeeded", final
        assert final["items"][0]["provider_attempts"] == old_attempts
        check = final["items"][0]["archive_revalidations"][-1]["checks"][0]
        assert check["status"] == "modules_committed" and check["provider_reissued"] is False
        assert check["validation_details"]["normalization"]["metadata_changes"][0]["to"] == "product_claim"
        with harness.service.store.transaction() as connection:
            for original, attempt in zip(archives, old_attempts):
                assert harness.service.store.exact(connection, attempt["provider_response_ref"]) == original
                assert attempt["usage"] == original["body"]["response"]["usage"]
    finally:
        await harness.asyncTearDown()
