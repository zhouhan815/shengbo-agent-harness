"""Required query inputs fail at the typed tool boundary, never over HTTP."""
import pytest
from pydantic import TypeAdapter, ValidationError

from shengbo_contracts.business import QUERY_ARGS, parse_query_args


REQUIRED_FIELDS = [(query, name) for query, model in QUERY_ARGS.items()
                   for name, field in model.model_fields.items() if field.is_required()]


@pytest.mark.parametrize("query,field", REQUIRED_FIELDS)
def test_required_query_fields_are_present_and_non_nullable_in_schema_and_validation(query, field):
    model = QUERY_ARGS[query]
    schema = model.model_json_schema()
    assert field in schema["required"]
    node = schema["properties"][field]
    assert node.get("type") != "null"
    assert not any(option.get("type") == "null" for option in node.get("anyOf", []))
    with pytest.raises(ValidationError):
        TypeAdapter(model.model_fields[field].annotation).validate_python(None)


def test_optional_query_ref_stays_optional_and_exact_required_ref_stays_exact():
    assert QUERY_ARGS["read_product"].model_validate({"ref": None}).ref is None
    assert parse_query_args("read_product", {}).ref is None
    with pytest.raises(ValidationError) as invalid:
        QUERY_ARGS["read_context"].model_validate({"ref": None, "sections": ["session_draft_schema"]})
    assert any(item["loc"] == ("ref", "RecordRef") or item["loc"][0] == "ref" for item in invalid.value.errors())
    ref = {"kind": "context", "id": "context1", "revision": 1, "content_hash": "sha256:exact"}
    request = QUERY_ARGS["read_context"].model_validate({"ref": ref, "sections": ["draft"]})
    assert request.model_dump(mode="json", exclude_none=True)["ref"] == ref


def test_preparation_plan_reader_is_optional_and_does_not_open_other_sections():
    assert parse_query_args("read_preparation", {"preparation_id": "p"}).sections == []
    assert parse_query_args("read_preparation", {"preparation_id": "p", "sections": ["script_plan"]}).sections == ["script_plan"]
    for sections in (["arbitrary_internal_table"], ["script_plan", "script_plan"]):
        with pytest.raises(ValidationError):
            parse_query_args("read_preparation", {"preparation_id": "p", "sections": sections})
    with pytest.raises(ValidationError):
        parse_query_args("read_product", {"product_id": "p", "sections": ["script_plan"]})
