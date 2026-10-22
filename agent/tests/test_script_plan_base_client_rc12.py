"""Closed public diagnostic decoding only; no model or network calls."""
from copy import deepcopy
import json

import httpx
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.client import ShengboClient

CODE = "BASE_MODULE_TARGET_MISMATCH"
SECRET = "fake-private-diagnostic-must-not-be-echoed"


def reference(kind):
    return {"kind": kind, "id": kind + "_requested", "revision": 1, "content_hash": "sha256:" + "a" * 64}


def details():
    return {"command": "save_script_plan", "commit_state": "not_committed",
        "preparation_ref": reference("preparation"), "bundle_ref": reference("context"), "item_index": 0,
        "module_key": "opening_long", "requested_product_ref": reference("product"),
        "requested_variant_id": "commute_v1", "requested_version_number": 1,
        "requested_base_module_ref": reference("module"), "requested_base_script_ref": None,
        "reason": "BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE"}


def response(status=422, body=None, code=CODE):
    return httpx.Response(status, json={"error": {"code": code, "message": SECRET,
        "details": details() if body is None else body}}, request=httpx.Request("POST", "http://isolated/"))


def test_valid_diagnostic_keeps_exact_targets_without_http_attestation_or_raw_message():
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response())
    error = caught.value
    assert error.code == CODE and error.status == 422
    assert error.details == details()
    assert SECRET not in json.dumps(error.as_error().model_dump())
    assert not hasattr(error, "known_operation_id"), "HTTP cannot attest a local committed/failed operation"
    assert not hasattr(error, "known_operation_input_hash")


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
def test_wrong_http_status_keeps_permission_and_unknown_hard_boundaries(status):
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(status))
    error = caught.value
    assert error.code == "VOICE_RESPONSE_INVALID"
    assert error.status == (status if status in {401, 403} or status >= 500 else 502)
    assert error.details == {} and SECRET not in str(error)


def test_legacy_409_target_mismatch_is_known_hard_rejection_not_missing_base_correction():
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(409, body={"input": SECRET}))
    error = caught.value
    assert error.code == CODE and error.status == 409
    assert error.details == {} and SECRET not in str(error)
    assert not hasattr(error, "known_operation_id")
    assert not hasattr(error, "known_operation_input_hash")


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(raw_input=SECRET),
    lambda d: d.update(command="submit_script_change"),
    lambda d: d.update(commit_state="unknown"),
    lambda d: d.update(reason="BASE_MODULE_WRONG_PRODUCT"),
    lambda d: d.update(item_index=True),
    lambda d: d.update(item_index=-1),
    lambda d: d.update(item_index="0"),
    lambda d: d.update(module_key=SECRET),
    lambda d: d.update(requested_version_number=True),
    lambda d: d.update(requested_version_number=0),
    lambda d: d.update(requested_variant_id=123),
    lambda d: d.update(requested_base_script_ref=reference("script_revision")),
    lambda d: d.pop("requested_base_script_ref"),
    lambda d: d["requested_product_ref"].update(kind="draft"),
    lambda d: d["bundle_ref"].update(content_hash=SECRET),
    lambda d: d["preparation_ref"].update(revision=True),
    lambda d: d["requested_base_module_ref"].update(schema_version="PRELIVE-OLD"),
    lambda d: d["requested_base_module_ref"].update(input=SECRET),
])
def test_invalid_closed_diagnostic_has_no_untrusted_raw_details(mutate):
    raw = details(); mutate(raw)
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(body=raw))
    error = caught.value
    assert error.code == "VOICE_RESPONSE_INVALID" and error.status == 502
    assert error.details == {} and SECRET not in str(error)


@pytest.mark.parametrize("code,status", [("REVISION_CONFLICT", 409), ("SCOPE_CONFLICT", 403), ("BASE_MODULE_KEY_MISMATCH", 409)])
def test_other_errors_keep_existing_semantics_and_are_not_relabelled(code, status):
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(status, body={}, code=code))
    assert caught.value.code == code and caught.value.status == status


def test_response_object_and_requested_ref_are_not_mutated():
    raw = details(); original = deepcopy(raw); value = response(body=raw)
    with pytest.raises(ContractError):
        ShengboClient._result(value)
    assert raw == original and value.json()["error"]["details"] == original
