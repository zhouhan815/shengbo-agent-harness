"""Structured, content-free status shared by workflow recovery and its UI."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import StrictModel


_Identifier = Annotated[str, Field(min_length=1, max_length=160)]
_Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class WorkflowRecoveryReceipt(StrictModel):
    workflow: _Identifier
    step_id: _Identifier
    strategy: _Identifier
    status: Literal["selected", "succeeded", "failed", "needs_input"]
    selected_by: Literal["program", "model", "user"]
    model_requests: int = Field(ge=0)
    disposition: Literal[
        "evidence_repair", "structure_repair", "field_repair", "retry", "reconciliation", "missing_input"
    ] | None = None
    input_hash: _Digest | None = None
    issue_codes: list[_Identifier] = Field(default_factory=list, max_length=32)
    affected_units: list[_Identifier] = Field(default_factory=list, max_length=64)


class WorkflowStepProgress(StrictModel):
    workflow: _Identifier
    step_id: _Identifier
    title: str = Field(min_length=1, max_length=240)
    status: Literal[
        "RUNNING", "RECOVERING", "WAITING", "WAITING_USER", "WAITING_JOBS", "SUCCEEDED", "FAILED", "BLOCKED", "SKIPPED"
    ]
    completed_count: int | None = Field(default=None, ge=0)
    total_count: int | None = Field(default=None, ge=0)
    recovery: WorkflowRecoveryReceipt | None = None

    @model_validator(mode="after")
    def check_progress(self):
        if self.completed_count is not None and self.total_count is not None and self.completed_count > self.total_count:
            raise ValueError("completed_count cannot exceed total_count")
        if self.recovery and (self.recovery.workflow, self.recovery.step_id) != (self.workflow, self.step_id):
            raise ValueError("recovery receipt must match the reported workflow and step")
        return self
