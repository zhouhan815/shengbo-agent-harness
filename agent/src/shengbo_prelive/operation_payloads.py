"""Immutable local wire arguments for import_blob, never a model-facing tool.

Only large base64 transport arguments are externalized. Their original request
hash and exact wire contract remain unchanged; summaries cannot be sent/replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json

STORAGE = "external_import_blob_wire.v1"
KIND = "import_blob_wire_payload"
MAX_WIRE_BYTES = 192 * 1024 * 1024
_HASH = re.compile(r"sha256:[a-f0-9]{64}")
_OP = re.compile(r"op_[a-f0-9]{32}")


def _fail(code="OPERATION_PAYLOAD_INVALID"):
    raise ContractError(code, "原业务请求的本地归档或引用校验失败，未重新发送", status=409)


def _chunks(args):
    """Exact canonical_json(args), streamed without a huge escaped copy."""
    if not isinstance(args, dict) or set(args) != {"content_base64", "mime", "expected_sha256", "filename"}:
        _fail()
    if any(not isinstance(value, str) for value in args.values()):
        _fail()
    yield b"{"
    for index, key in enumerate(sorted(args)):
        if index:
            yield b","
        yield canonical_json(key).encode("utf-8") + b":"
        if key == "content_base64":
            yield b'"'
            value = args[key]
            for offset in range(0, len(value), 1024 * 1024):
                # Base64 is ASCII in valid requests. Proper JSON escaping also
                # preserves exact hashes for malformed inputs rejected remotely.
                yield canonical_json(value[offset:offset + 1024 * 1024])[1:-1].encode("utf-8")
            yield b'"'
        else:
            yield canonical_json(args[key]).encode("utf-8")
    yield b"}"


def import_blob_request_hash(args):
    digest = hashlib.sha256()
    total = 0
    for chunk in _chunks(args):
        total += len(chunk)
        if total > MAX_WIRE_BYTES:
            _fail("OPERATION_PAYLOAD_TOO_LARGE")
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _binding(operation):
    return canonical_hash({key: operation[key] for key in
        ("id", "run_id", "key", "request_hash", "args_summary_hash", "wire_payload_ref", "remote_request_hash", "scope_hash")})


def _root(root):
    return Path(root).resolve() / "operation_wire_payloads"


def _path(root, reference):
    if not isinstance(reference, dict) or set(reference) != {"kind", "id", "content_hash", "size_bytes"}:
        _fail()
    if reference["kind"] != KIND or not _OP.fullmatch(str(reference["id"])) or not _HASH.fullmatch(str(reference["content_hash"])):
        _fail()
    size = reference["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_WIRE_BYTES:
        _fail()
    directory = _root(root)
    path = directory / (reference["id"] + "-" + reference["content_hash"][7:] + ".json")
    if path.is_symlink() or path.resolve().parent != directory:
        _fail()
    return path


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def persist_import_blob_args(root, operation_id, run_id, args, scope):
    """Archive exact args before send; return a small operation projection."""
    if not _OP.fullmatch(operation_id) or not isinstance(run_id, str) or not run_id:
        _fail()
    started = time.perf_counter()
    directory = _root(root)
    directory.mkdir(parents=True, exist_ok=True)
    pending = directory / (operation_id + ".pending-" + uuid4().hex)
    digest, remote = hashlib.sha256(), hashlib.sha256()
    normalized_scope = WorkScope.model_validate(scope).model_dump(mode="json", exclude_none=True)
    remote.update(b'{"args":')
    size = 0
    try:
        with pending.open("xb") as handle:
            for chunk in _chunks(args):
                size += len(chunk)
                if size > MAX_WIRE_BYTES:
                    _fail("OPERATION_PAYLOAD_TOO_LARGE")
                digest.update(chunk)
                remote.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        remote.update(b',"command":"import_blob","scope":' + canonical_json(normalized_scope).encode("utf-8") + b"}")
        request_hash = "sha256:" + digest.hexdigest()
        reference = {"kind": KIND, "id": operation_id, "content_hash": request_hash, "size_bytes": size}
        target = _path(root, reference)
        reused = False
        try:
            # Publish without overwriting an existing immutable archive. The
            # exclusive runtime instance prevents concurrent service writers.
            os.link(pending, target)
        except FileExistsError:
            if target.stat().st_size != size or _file_hash(target) != request_hash:
                _fail("OPERATION_PAYLOAD_HASH_MISMATCH")
            reused = True
        summary = {"mime": args["mime"], "filename": args["filename"], "expected_sha256": args["expected_sha256"],
                   "content_base64_chars": len(args["content_base64"]),
                   "content_base64_storage": "local_immutable_wire_payload_not_inline"}
        operation = {"id": operation_id, "run_id": run_id, "key": "import_blob", "args": summary,
                     "args_storage": STORAGE, "args_summary_hash": canonical_hash(summary),
                     "request_hash": request_hash, "wire_payload_ref": reference,
                     "scope_hash": canonical_hash(normalized_scope), "remote_request_hash": "sha256:" + remote.hexdigest()}
        operation["wire_binding_hash"] = _binding(operation)
        return operation, {"wire_payload_bytes": size, "base64_chars": len(args["content_base64"]),
                           "wire_persist_ms": round((time.perf_counter() - started) * 1000, 3), "wire_reused": reused}
    finally:
        if pending.exists():
            pending.unlink()


def validate_import_blob_projection(root, operation):
    """Validate bounded metadata; never load the payload in list/context paths.

    It establishes a content-addressed reference, not a fresh full-file hash.
    Exact forensic loading separately verifies the bytes through load_wire_args.
    """
    if operation.get("key") != "import_blob" or operation.get("args_storage") != STORAGE:
        _fail()
    try:
        reference = operation["wire_payload_ref"]
        path = _path(root, reference)
        if (operation["id"] != reference["id"] or operation["request_hash"] != reference["content_hash"]
                or canonical_hash(operation["args"]) != operation["args_summary_hash"]
                or _binding(operation) != operation["wire_binding_hash"]
                or not _HASH.fullmatch(operation["remote_request_hash"])
                or not _HASH.fullmatch(operation["scope_hash"])):
            _fail()
        summary = operation["args"]
        if (set(summary) != {"mime", "filename", "expected_sha256", "content_base64_chars", "content_base64_storage"}
                or summary["content_base64_storage"] != "local_immutable_wire_payload_not_inline"
                or any(not isinstance(summary[name], str) for name in ("mime", "filename", "expected_sha256"))
                or isinstance(summary["content_base64_chars"], bool) or not isinstance(summary["content_base64_chars"], int)
                or not 0 <= summary["content_base64_chars"] <= MAX_WIRE_BYTES):
            _fail()
        if not path.is_file() or path.stat().st_size != reference["size_bytes"]:
            _fail("OPERATION_PAYLOAD_MISSING")
    except (KeyError, TypeError, ValueError):
        _fail()
    return {"metadata_verified": True, "payload_bytes_verified": False,
            "wire_payload_ref": reference, "request_hash": operation["request_hash"]}


def load_wire_args(root, operation):
    """Explicit local forensic read, not automatic replay or a model tool."""
    validate_import_blob_projection(root, operation)
    reference = operation["wire_payload_ref"]
    path = _path(root, reference)
    if _file_hash(path) != reference["content_hash"]:
        _fail("OPERATION_PAYLOAD_HASH_MISMATCH")
    try:
        args = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        _fail()
    if import_blob_request_hash(args) != operation["request_hash"]:
        _fail("OPERATION_PAYLOAD_HASH_MISMATCH")
    return args
