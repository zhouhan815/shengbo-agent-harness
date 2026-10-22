from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

SCHEMA_VERSION = "PRELIVE-2.3"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "obj") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    def convert(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("value is not a supported JSON contract value")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=convert)


def canonical_hash(value) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class WorkScope(StrictModel):
    kind: Literal["catalog", "preparation"] = "catalog"
    account_id: str = "local"
    preparation_id: str | None = None

    @model_validator(mode="after")
    def consistent_kind(self):
        if (self.kind == "preparation") != bool(self.preparation_id):
            raise ValueError("preparation scope requires preparation_id; catalog scope forbids it")
        return self


class RecordRef(StrictModel):
    kind: str
    id: str
    revision: int = Field(ge=1)
    content_hash: str


class ArtifactRef(RecordRef):
    schema_version: str = SCHEMA_VERSION


def same_artifact_ref(left, right) -> bool:
    """Compare legal wire representations without rewriting their originals.

    The declared default schema may be omitted. All other fields, strict types,
    unexpected fields and an explicitly unsupported schema remain significant.
    Equality never proves ownership or content support; callers keep those gates.
    """
    try:
        refs = [ArtifactRef.model_validate(value, strict=True) for value in (left, right)]
    except (ValidationError, TypeError, ValueError):
        return False
    return all(ref.schema_version == SCHEMA_VERSION for ref in refs) and refs[0] == refs[1]


class Principal(StrictModel):
    kind: Literal["human", "agent", "controller"]
    actor_id: str = "local-user"
    account_id: str = "local"
    run_id: str | None = None


class MutationContext(StrictModel):
    operation_id: str
    grant_id: str | None = None
    expected_write_generation: int | None = None
    expected_generation_epoch: int | None = None
    expected_versions: list[RecordRef] = Field(default_factory=list)
    request_hash: str = ""


class WireError(StrictModel):
    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    retryable: bool = False


class ContractError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, details: dict | None = None,
                 retryable: bool = False):
        self.code, self.message, self.status = code, message, status
        self.details, self.retryable = details or {}, retryable
        super().__init__(f"{code}: {message}")

    def as_error(self) -> WireError:
        # Pydantic diagnostic loc coordinates are tuples. JSON's lossless
        # sequence projection must precede JsonValue validation; no repr/default
        # stringification of exceptions or arbitrary objects is permitted.
        details = json.loads(canonical_json(self.details))
        return WireError(code=self.code, message=self.message, details=details, retryable=self.retryable)


class CommandReceipt(StrictModel):
    schema_version: str = SCHEMA_VERSION
    command: str
    operation_id: str
    status: Literal["committed", "accepted", "replayed"] = "committed"
    data: dict[str, JsonValue] = Field(default_factory=dict)
    changed_paths: list[str] = Field(default_factory=list)
    invalidated_refs: list[dict[str, JsonValue]] = Field(default_factory=list)
    event_cursor: int = 0


class EventEnvelope(StrictModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str
    sequence: int = Field(ge=1)
    run_id: str
    invocation_id: str | None = None
    branch_task_id: str | None = None
    parent_event_id: str | None = None
    type: str
    timestamp: str
    data: dict[str, JsonValue] = Field(default_factory=dict)

