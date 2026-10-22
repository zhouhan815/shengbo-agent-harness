"""Both services share a public-only response reader contract."""
import pytest
from pydantic import ValidationError

from shengbo_contracts.business import PublicProviderResponseView


def view():
    return {"content": '{"spoken_text":"按需选择就好。"}', "module_key": "cta", "expected_module_key": "cta",
        "receipt_ref": {"kind": "provider_response", "id": "response-1", "revision": 1, "content_hash": "sha256:exact"},
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 10}, "completion_tokens_details": {"reasoning_tokens": 2}},
        "usage_state": "known", "validation_state": "rejected", "state": "response_received",
        "is_formal_module": False, "reusable": False, "approvable": False}


def test_rejected_public_receipt_roundtrip_does_not_create_formal_authority():
    data = view()
    assert PublicProviderResponseView.model_validate(data).model_dump(mode="json") == data
    data.update(content=None, usage={}, usage_state="unknown", module_key=None)
    assert PublicProviderResponseView.model_validate(data).content is None


@pytest.mark.parametrize("key,value", [("reasoning_content", "private"), ("raw_envelope", {}),
    ("validation_product", {"price": 99}), ("is_formal_module", True), ("reusable", True), ("approvable", True),
    ("state", "outcome_unknown"), ("approvable", 0)])
def test_private_fields_or_invented_commit_and_received_authority_are_rejected(key, value):
    data = {**view(), key: value}
    with pytest.raises(ValidationError):
        PublicProviderResponseView.model_validate(data)


@pytest.mark.parametrize("usage", [{"prompt_tokens": True}, {"prompt_tokens": -1}, {"total_tokens": None},
    {"secret": "private"}, {"completion_tokens_details": {"reasoning_content": "private"}}])
def test_usage_accepts_numbers_without_private_content_or_false_zeroes(usage):
    with pytest.raises(ValidationError):
        PublicProviderResponseView.model_validate({**view(), "usage": usage})
