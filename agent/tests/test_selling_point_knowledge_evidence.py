"""Consumer knowledge is citable; identity metadata is not a capability."""
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.selling_point_evidence import build_evidence_catalog
from shengbo_prelive.selling_points import _field


@pytest.fixture
def product():
    return {
        "knowledge": "可用于单人回弹练习。",
        "target_audience": "初学者，男女通用，儿童可使用。",
        "knowledge_documents": [{"id": "scene", "title": "适合所有人", "content": "可用于课外自主练习。"}],
        "core_selling_points": ["想练就练"],
        "hidden_optional_fields": [],
    }


@pytest.mark.parametrize("pointer, expected", [
    ("/knowledge", "可用于单人回弹练习。"),
    ("/target_audience", "初学者，男女通用，儿童可使用。"),
    ("/knowledge_documents/0/content", "可用于课外自主练习。"),
])
def test_current_consumer_facts_are_directly_citable(product, pointer, expected):
    assert _field(product, pointer) == expected


@pytest.mark.parametrize("pointer", [
    "/knowledge_documents", "/knowledge_documents/0", "/knowledge_documents/0/title",
    "/knowledge_documents/0/id", "/knowledge_documents/1/content", "/core_selling_points/0",
])
def test_titles_identity_missing_content_and_marketing_are_not_evidence(product, pointer):
    with pytest.raises(ContractError) as error:
        _field(product, pointer)
    assert error.value.code == "SELLING_POINTS_EVIDENCE_INVALID"


def test_hidden_or_empty_knowledge_does_not_become_an_asserted_fact(product):
    product["hidden_optional_fields"] = ["knowledge"]
    with pytest.raises(ContractError):
        _field(product, "/knowledge")
    product["hidden_optional_fields"] = []
    product["knowledge_documents"][0]["content"] = ""
    with pytest.raises(ContractError):
        _field(product, "/knowledge_documents/0/content")


def test_evidence_catalog_resolves_exact_values_on_the_selected_revision(product):
    product["specifications"] = [{"name": "电池容量", "value": "2400 mAh"},
                                 {"name": "电源兼容", "value": "仅适用 5 V 电源"}]
    product["hidden_optional_fields"] = ["target_audience"]
    ref = {"id": "camp-lamp", "revision": 7, "content_hash": "rev-7"}
    catalog = build_evidence_catalog(ref, product)
    evidence = {item["product_field"]: item for item in catalog.values()}
    assert evidence["/specifications/0/value"] == {
        "product_field": "/specifications/0/value", "label": "电池容量", "fact": "2400 mAh",
    }
    assert evidence["/specifications/1/value"]["fact"] == "仅适用 5 V 电源"
    assert "/target_audience" not in evidence
    assert "/knowledge_documents/0/title" not in evidence
    assert all(item["fact"] == _field(product, path) for path, item in evidence.items())
    assert catalog == build_evidence_catalog(ref, product)
    assert set(catalog).isdisjoint(build_evidence_catalog({**ref, "revision": 8}, product))
