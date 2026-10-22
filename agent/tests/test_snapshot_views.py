"""Snapshot selectors stay bounded, exact, navigable and free of authority."""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.snapshot_views import project_snapshot


@pytest.mark.parametrize("document", [None, True, 42, 2.5, "中文🙂", {}, [],
    {"status": "failed", "output": None, "formal_reuse": False}, [1, {"a": 2}]])
def test_small_values_are_exact_and_not_modified(document):
    original = deepcopy(document)
    result = project_snapshot(document)
    assert result["document"] == original
    assert result["selection"]["mode"] == "value"
    assert result["selection"]["content_hash"] == canonical_hash(original)
    assert result["selection"]["next_cursor"] is None
    assert document == original
    assert json.loads(canonical_json(result)) == result


@pytest.mark.parametrize(("path", "expected"), [
    ("", {"": "empty", "a/b": {"~": ["zero", {"x": "target"}]}, "~1": "literal", "01": "object key"}),
    ("/", "empty"), ("/a~1b/~0/0", "zero"), ("/a~1b/~0/1/x", "target"),
    ("/~01", "literal"), ("/01", "object key"),
])
def test_rfc6901_paths_select_exact_values(path, expected):
    document = {"": "empty", "a/b": {"~": ["zero", {"x": "target"}]}, "~1": "literal", "01": "object key"}
    result = project_snapshot(document, path=path)
    assert result["document"] == expected
    assert result["selection"]["path"] == path


@pytest.mark.parametrize("path", ["job", 1, False, "/missing", "/a/~", "/a/~2", "/a/-", "/a/-1",
                                      "/a/01", "/a/2", "/a/١", "/a/0/x"])
def test_invalid_paths_are_recoverable_contract_errors(path):
    with pytest.raises(ContractError) as caught:
        project_snapshot({"a": [None, 1]}, path=path)
    assert caught.value.code == "HISTORY_SNAPSHOT_PATH_INVALID"
    assert caught.value.status == 422
    assert "available_paths" in caught.value.details


def test_sections_are_actual_snapshot_top_level_keys_and_never_ignored():
    document = {"kind": "job", "job": {"id": "job_1"}, "evidence_only": True}
    result = project_snapshot(document, sections=["job", "job"])
    assert result["document"] == {"job": {"id": "job_1"}}
    assert result["selection"]["sections"] == ["job"]
    assert project_snapshot(document, sections=[])["document"] == {}
    for sections in (["operations", "artifacts"], "job", [1]):
        with pytest.raises(ContractError) as caught:
            project_snapshot(document, sections=sections)
        assert caught.value.code == "HISTORY_SNAPSHOT_SECTION_INVALID"
        assert caught.value.details["available_sections"] == list(document)
        assert caught.value.details["available_paths"][1]["path"] == "/job"
    with pytest.raises(ContractError) as caught:
        project_snapshot([], sections=["job"])
    assert caught.value.code == "HISTORY_SNAPSHOT_SECTION_INVALID"


@pytest.mark.parametrize("options", [
    {"path": "", "sections": []}, {"full": True, "path": ""}, {"full": True, "sections": []},
    {"full": True, "cursor": 1}, {"cursor": -1}, {"cursor": True}, {"cursor": 1.2},
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.2},
    {"max_chars": 0}, {"max_chars": True}, {"max_chars": 1.2}, {"full": "true"},
])
def test_invalid_input_is_action_boundary_correctable(options):
    with pytest.raises(ContractError) as caught:
        project_snapshot({}, **options)
    assert caught.value.code == "HISTORY_SNAPSHOT_INPUT_INVALID"
    assert caught.value.status == 422
    assert caught.value.as_error().retryable is False


@pytest.mark.parametrize("container", [{f"key_{i}": {"value": i} for i in range(7)},
                                       [{"value": i} for i in range(7)]])
def test_container_pages_reconstruct_every_value_once(container):
    cursor, pages, selections = 0, [], []
    while True:
        result = project_snapshot(container, cursor=cursor, limit=2)
        pages.append(result["document"])
        selections.append(result["selection"])
        assert result["selection"]["units"] == ("keys" if isinstance(container, dict) else "items")
        assert len(result["document"]) <= 2
        if result["selection"]["next_cursor"] is None:
            break
        assert result["selection"]["next_cursor"] > cursor
        cursor = result["selection"]["next_cursor"]
    if isinstance(container, dict):
        assert {key: value for page in pages for key, value in page.items()} == container
    else:
        assert [item for page in pages for item in page] == container
    assert [page["cursor"] for page in selections] == [0, 2, 4, 6]
    exhausted = project_snapshot(container, cursor=100, limit=2)
    assert exhausted["document"] == ({} if isinstance(container, dict) else [])
    assert exhausted["selection"]["next_cursor"] is None


def test_large_job_can_navigate_directly_to_provider_output_without_context():
    output = {"sentences": [{"text": "仅供复核的文本，尚未正式保存。", "refs": []}], "formal_reuse": False}
    attempt = {"attempt_id": "attempt_1", "state": "result_received", "output": output,
               "input": "原输入" * 20000, "output_hash": canonical_hash(output)}
    document = {"kind": "job", "job": {"id": "job_1", "status": "cancelled", "context": "旧上下文" * 50000,
        "items": [{"item_id": "item_1", "status": "failed", "output": None, "provider_attempts": [attempt]}]}}
    original = deepcopy(document)
    root = project_snapshot(document, max_chars=1000)
    index = root["document"]["job"]
    assert index["index_only"] is True
    assert index["path"] == "/job"
    assert index["content_hash"] == canonical_hash(document["job"])
    assert root["selection"]["mode"] == "page"
    assert root["selection"]["indexed_paths"] == [{"path": "/job"}]
    assert len(canonical_json(root["document"])) <= 1000
    path = "/job/items/0/provider_attempts/0/output"
    result = project_snapshot(document, path=path, max_chars=1000)
    assert result["document"] == output
    assert result["selection"]["mode"] == "value"
    assert result["selection"]["content_hash"] == attempt["output_hash"]
    assert "原输入" not in canonical_json(result)
    assert "旧上下文" not in canonical_json(result)
    assert result["document"]["formal_reuse"] is False
    assert document == original


def test_list_indexes_preserve_exact_paths_and_do_not_look_like_successful_outputs():
    document = [{"output": "大输出" * 10000, "state": "timeout_unknown"},
                {"output": None, "state": "known_failed"}]
    result = project_snapshot(document, limit=1, max_chars=1000)
    index = result["document"][0]
    assert index["index_only"] is True and index["path"] == "/0"
    assert index["content_hash"] == canonical_hash(document[0])
    assert "output" not in index and "state" not in index
    assert result["selection"]["next_cursor"] == 1
    assert project_snapshot(document, path="/1")["document"] == document[1]


def test_budgeted_pages_advance_and_never_return_broken_json():
    document = {str(i): '"\\\n中文🙂' * 1000 for i in range(12)}
    cursor, seen = 0, []
    while True:
        result = project_snapshot(document, cursor=cursor, limit=100, max_chars=500)
        assert len(canonical_json(result["document"])) <= 500
        assert json.loads(canonical_json(result)) == result
        seen.extend(result["document"])
        next_cursor = result["selection"]["next_cursor"]
        if next_cursor is None:
            break
        assert next_cursor > cursor
        cursor = next_cursor
    assert seen == list(document)


@pytest.mark.parametrize("text", ['"\\\n\t中文🙂' * 100, "普通文案" * 100])
def test_string_pages_reconstruct_exact_source_with_character_cursors(text):
    cursor, chunks = 0, []
    while True:
        result = project_snapshot({"body": text}, path="/body", cursor=cursor, max_chars=100)
        selection = result["selection"]
        assert selection["mode"] == "text" and selection["units"] == "characters"
        assert selection["total_chars"] == len(text)
        assert selection["content_hash"] == canonical_hash(text)
        assert len(canonical_json(result["document"])) <= 100
        chunks.append(result["document"]["text"])
        assert selection["end_cursor"] - cursor == len(chunks[-1])
        if selection["next_cursor"] is None:
            break
        assert selection["next_cursor"] == selection["end_cursor"] > cursor
        cursor = selection["next_cursor"]
    assert "".join(chunks) == text


def test_tiny_budget_still_makes_finite_progress_with_bounded_index_overhead():
    result = project_snapshot(["x" * 10000], max_chars=1)
    assert result["selection"]["end_cursor"] == 1
    assert result["selection"]["next_cursor"] is None
    assert result["document"][0]["index_only"] is True
    assert len(canonical_json(result)) < 1000
    result = project_snapshot("x" * 10, max_chars=1)
    assert result["document"] == {"text": "x"}
    assert result["selection"]["next_cursor"] == 1


def test_untrusted_key_names_do_not_become_unbounded_navigation_metadata():
    document = {(str(i) + "大key" * 10000): "large" * 10000 for i in range(120)}
    result = project_snapshot(document, limit=100, max_chars=1000)
    assert len(canonical_json(result)) <= 1000 + 6000
    assert len(result["available_paths"]) <= 100
    assert result["selection"]["next_cursor"] > 0
    assert result["selection"]["omitted_keys"][0]["path_omitted"] is True
    assert result["document"] == {}
    with pytest.raises(ContractError) as caught:
        project_snapshot(document, sections=["unknown"])
    assert len(canonical_json(caught.value.details)) < 6000
    assert caught.value.details["available_sections_total"] == 120


def test_full_requires_explicit_opt_in_and_is_exact_even_over_budget():
    document = {"job": {"context": "上下文" * 20000, "items": list(range(120))}}
    bounded = project_snapshot(document, limit=1, max_chars=200)
    assert bounded["document"] != document
    result = project_snapshot(document, full=True, limit=1, max_chars=1)
    assert result["document"] == document
    assert result["selection"]["mode"] == "full"
    assert result["selection"]["units"] == "document"
    assert result["selection"]["paginated"] is False
    assert result["selection"]["next_cursor"] is None
    assert result["selection"]["content_hash"] == canonical_hash(document)


def test_scalar_cursors_cannot_silently_repeat_the_same_value():
    with pytest.raises(ContractError) as caught:
        project_snapshot(3, cursor=1)
    assert caught.value.code == "HISTORY_SNAPSHOT_INPUT_INVALID"
