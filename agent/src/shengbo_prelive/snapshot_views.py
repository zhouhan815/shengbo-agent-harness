"""Bounded JSON views of snapshots whose scope/hash checks happen in Runtime.

This module only selects immutable JSON values.  It neither reads storage nor
grants access, records acceptance, or turns provider output into a formal result.
"""
from __future__ import annotations

from copy import deepcopy
import re

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json


_MAX_PATH_CHARS = 512
_MAX_METADATA_CHARS = 2400


def _type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _pointer(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _path_fields(path):
    if len(path) <= _MAX_PATH_CHARS:
        return {"path": path}
    return {"path": None, "path_chars": len(path), "path_hash": canonical_hash(path),
            "path_omitted": True}


def _index(value, path):
    return {**_path_fields(path), "type": _type(value),
            "length": len(value) if isinstance(value, (dict, list, str)) else len(canonical_json(value)),
            "content_hash": canonical_hash(value), "index_only": True}


def _bounded(items, budget=_MAX_METADATA_CHARS):
    result = []
    for item in items:
        if len(result) >= 100 or len(canonical_json(result + [item])) > budget:
            break
        result.append(item)
    return result


def _available(value, path, *, cursor=0, limit=20):
    """A bounded list of immediate readable paths, never arbitrary key text."""
    if isinstance(value, dict):
        children = ((key, item) for key, item in value.items())
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return [_index(value, path)]
    result = []
    for position, (key, child) in enumerate(children):
        if position < cursor:
            continue
        entry = _index(child, _pointer(path, key))
        if len(result) >= min(limit, 100) or len(canonical_json(result + [entry])) > _MAX_METADATA_CHARS:
            break
        result.append(entry)
    return result


def _fail(code, message, document, *, path="", **details):
    raise ContractError(code, message, status=422,
        details={**details, "available_paths": _available(document, path)})


def _resolve(document, path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        _fail("HISTORY_SNAPSHOT_PATH_INVALID", "快照路径须为以/开头的JSON pointer或空根路径", document)
    value, resolved = document, ""
    for token in path.split("/")[1:] if path else []:
        if re.search(r"~(?![01])", token):
            _fail("HISTORY_SNAPSHOT_PATH_INVALID", "JSON pointer转义仅支持~0和~1", value, path=resolved)
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and len(key) < 20 and int(key) < len(value):
            value = value[int(key)]
        else:
            _fail("HISTORY_SNAPSHOT_PATH_INVALID", "快照路径不存在；请使用available_paths中的精确路径", value, path=resolved)
        resolved = _pointer(resolved, key)
    return value


def project_snapshot(document, *, path=None, sections=None, cursor=0, limit=20,
                     max_chars=12000, full=False) -> dict:
    """Select a JSON value, container page, or string page after Runtime ACL.

    Small values are returned exactly. Container pages retain their dict/list
    shape; an oversized child becomes an explicit index with an exact pointer.
    ``selection.indexed_paths`` distinguishes those indexes from original data.
    String cursor units are source characters; container units are keys/items.
    ``max_chars`` bounds serialized selected data (plus at most one small index
    needed to advance an otherwise empty page). Navigation metadata is bounded.
    Explicit ``full=True`` returns the original document without pagination.
    """
    if (not isinstance(full, bool) or not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100
            or not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1):
        _fail("HISTORY_SNAPSHOT_INPUT_INVALID", "快照分页须cursor>=0、1<=limit<=100、max_chars>=1", document)
    if path is not None and sections is not None:
        _fail("HISTORY_SNAPSHOT_INPUT_INVALID", "path与sections不能同时使用", document)
    if full and (path is not None or sections is not None or cursor != 0):
        _fail("HISTORY_SNAPSHOT_INPUT_INVALID", "full全文读取不能同时选择path、sections或非零cursor", document)

    if full:
        return {"document": deepcopy(document), "selection": {"mode": "full", "path": "",
            "units": "document", "paginated": False, "cursor": 0, "next_cursor": None,
            "content_hash": canonical_hash(document)}, "available_paths": _available(document, "", limit=limit)}

    selected_path = path if path is not None else ""
    value = _resolve(document, selected_path)
    if sections is not None:
        available_sections = _bounded(key if len(key) <= 96 else {"key_preview": key[:96], "key_chars": len(key),
            "key_hash": canonical_hash(key)} for key in document) if isinstance(document, dict) else []
        if (not isinstance(document, dict) or not isinstance(sections, list)
                or any(not isinstance(key, str) or key not in document for key in sections)):
            _fail("HISTORY_SNAPSHOT_SECTION_INVALID", "sections只能选择当前快照实际存在的顶层key", document,
                  available_sections=available_sections,
                  available_sections_total=len(document) if isinstance(document, dict) else 0)
        value = {key: document[key] for key in dict.fromkeys(sections)}

    kind = _type(value)
    units = "keys" if isinstance(value, dict) else "items" if isinstance(value, list) else "characters" if isinstance(value, str) else "value"
    total = len(value) if isinstance(value, (dict, list, str)) else 1
    selection = {"mode": "value", **_path_fields(selected_path), "type": kind, "units": units,
        "cursor": cursor, "next_cursor": None, "total": total, "content_hash": canonical_hash(value),
        "paginated": False}
    if sections is not None:
        selection["sections"] = _bounded(key if len(key) <= 96 else {"key_hash": canonical_hash(key), "key_chars": len(key)}
                                        for key in dict.fromkeys(sections))
        selection["sections_total"] = len(value)
    navigation = _available(value, selected_path, cursor=cursor if isinstance(value, (dict, list)) else 0, limit=limit)

    if cursor == 0 and (not isinstance(value, (dict, list)) or total <= limit) and len(canonical_json(value)) <= max_chars:
        return {"document": deepcopy(value), "selection": selection, "available_paths": navigation}

    if isinstance(value, str):
        # Cursor tracks source characters. JSON escaping may consume several
        # characters, so choose a valid independently serialized text fragment.
        end = min(total, cursor + max_chars)
        low, high = min(cursor, total), end
        while low < high:
            middle = (low + high + 1) // 2
            if len(canonical_json({"text": value[cursor:middle]})) <= max_chars:
                low = middle
            else:
                high = middle - 1
        end = low if low > cursor or cursor >= total else cursor + 1
        selection.update(mode="text", paginated=True, next_cursor=end if end < total else None,
                         total_chars=total, end_cursor=end)
        return {"document": {"text": value[cursor:end]}, "selection": selection, "available_paths": navigation}

    if not isinstance(value, (dict, list)):
        if cursor:
            _fail("HISTORY_SNAPSHOT_INPUT_INVALID", "标量快照不支持非零cursor", document)
        selection.update(mode="index", indexed_paths=[_path_fields(selected_path)])
        return {"document": _index(value, selected_path), "selection": selection, "available_paths": navigation}

    page = {} if isinstance(value, dict) else []
    indexed, omitted_keys = [], []
    entries = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
    position = min(cursor, total)
    while position < min(total, cursor + limit):
        key, child = entries[position]
        child_path = _pointer(selected_path, key)
        index = _index(child, child_path)
        if isinstance(value, dict) and len(key) > 256:
            # Do not echo unbounded user-controlled key names as metadata.
            omitted = {"key_preview": key[:64], "key_chars": len(key), "key_hash": canonical_hash(key), **index}
            if omitted_keys and len(canonical_json(omitted_keys + [omitted])) > min(max_chars, _MAX_METADATA_CHARS):
                break
            omitted_keys.append(omitted)
            indexed.append(_path_fields(child_path))
            position += 1
            if len(canonical_json(omitted_keys)) >= max_chars:
                break
            continue
        candidate = {**page, key: child} if isinstance(value, dict) else [*page, child]
        chosen = child
        if len(canonical_json(candidate)) > max_chars:
            chosen = index
            candidate = {**page, key: chosen} if isinstance(value, dict) else [*page, chosen]
        if len(canonical_json(candidate)) > max_chars and page:
            break
        page = candidate
        if chosen is index:
            indexed.append(_path_fields(child_path))
        position += 1
    bounded_indexes = _bounded(indexed)
    selection.update(mode="page", paginated=True, next_cursor=position if position < total else None,
                     end_cursor=position, indexed_paths=bounded_indexes, indexed_count=len(indexed),
                     indexed_paths_truncated=len(bounded_indexes) != len(indexed))
    if omitted_keys:
        selection["omitted_keys"] = omitted_keys
    return {"document": deepcopy(page), "selection": selection, "available_paths": navigation}
