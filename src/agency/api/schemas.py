from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agency.yaml_engine.schema import ModelSpec

# --- Error codes (contracts/http-api.md error table) -----------------------

CODE_UNKNOWN_WORKFLOW = "unknown_workflow"
CODE_INVALID_WORKFLOW = "invalid_workflow"
CODE_MODEL_VALIDATION_FAILED = "model_validation_failed"
CODE_NO_REPLAY_SOURCE = "no_replay_source"
CODE_UNKNOWN_RUN = "unknown_run"
CODE_UNKNOWN_NODE = "unknown_node"
CODE_INPUT_NOT_AWAITING = "input_not_awaiting"
CODE_RUN_NOT_FINISHED = "run_not_finished"
CODE_INVALID_REQUEST = "invalid_request"
CODE_SANDBOX_UNAVAILABLE = "sandbox_unavailable"

_SIZE_PATTERN = re.compile(r"^\s*[\d.]+\s*(GB|MB|KB|B)\s*$")


class AgencyAPIError(Exception):
    """A structured API failure mapped to a specific HTTP status and code.

    ``code``/``http_status``/``message``/``details`` mirror the contracts error
    table; the transport layer renders ``{"error": {code, message, details}}``.
    """

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details: dict[str, Any] = details if details is not None else {}
        super().__init__(message)

    def error_body(self) -> ErrorBody:
        return ErrorBody(code=self.code, message=self.message, details=self.details)


# --- Request models --------------------------------------------------------


class SandboxRequest(BaseModel):
    """Additive per-run sandbox caps (spec 006, contract §3).

    Mirrors the workflow's ``sandbox:`` mapping; every field is optional —
    an absent field inherits the workflow ``sandbox:`` value, else the
    built-in default (contract §4). Only ``cpu``/``memory``/``pids_limit``/
    ``timeout_seconds`` are exposed per-run; ``output_limit_bytes`` and
    ``scratch_size`` ride along for completeness and layer the same way.
    """

    cpu: float | None = Field(default=None, gt=0)
    memory: str | None = None
    pids_limit: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    output_limit_bytes: int | None = Field(default=None, gt=0)
    scratch_size: str | None = None

    @field_validator("memory", "scratch_size")
    @classmethod
    def validate_size_fields(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _SIZE_PATTERN.match(v):
            raise ValueError(f"Invalid size format: '{v}'. Expected format like '512MB', '1GB'.")
        return v


class StartRunRequest(BaseModel):
    workflow: str
    vram_limit: str | None = None
    models: dict[str, ModelSpec] | None = None
    environment: Literal["sandbox", "local"] | None = None
    sandbox: SandboxRequest | None = None


class ReplayRequest(BaseModel):
    workflow: str
    checkpoint: str
    source_run: str | None = None
    vram_limit: str | None = None
    models: dict[str, ModelSpec] | None = None
    environment: Literal["sandbox", "local"] | None = None
    sandbox: SandboxRequest | None = None


class NodeInputRequest(BaseModel):
    value: str


# --- Response models -------------------------------------------------------


class RunAccepted(BaseModel):
    run_id: str
    state: str
    workflow: str


class ReplayAccepted(BaseModel):
    run_id: str
    state: str
    source_run: str
    checkpoint: str


class NodeStateView(BaseModel):
    status: str
    model: str | None = None
    tokens: int | None = None
    duration_seconds: float | None = None
    attempt: int = 1
    fallback: str | None = None
    reason: str | None = None


class PendingInputView(BaseModel):
    node_id: str
    prompt: str
    deadline: str | None = None


class ReplaySource(BaseModel):
    source_run_id: str
    checkpoint: str


class RunStatusView(BaseModel):
    run_id: str
    workflow_name: str
    state: str
    started_at: str
    replay_source: ReplaySource | None = None
    nodes: dict[str, NodeStateView]
    pending_inputs: list[PendingInputView] = Field(default_factory=list)


class NodeResult(BaseModel):
    node_id: str
    status: str
    model: str | None = None
    tokens: int | None = None
    duration_seconds: float | None = None
    attempt: int = 1
    fallback: str | None = None
    reason: str | None = None
    output: str | None = None


class RunResultView(BaseModel):
    run_id: str
    status: str
    total_tokens: int
    duration_seconds: float
    nodes: list[NodeResult]


class RunHistoryEntry(BaseModel):
    run_id: str
    workflow_name: str
    started_at: str
    state: str


class HealthResponse(BaseModel):
    status: str
    log_dir: str


# --- Error body -----------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
