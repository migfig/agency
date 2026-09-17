from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# --- Event type constants ---

EVENT_VRAM_THRESHOLD_EXCEEDED = "vram_threshold_exceeded"
EVENT_VRAM_FREED = "vram_freed"
EVENT_MODEL_OFFLOADED = "model_offloaded"
EVENT_MODEL_RELOADED = "model_reloaded"
EVENT_AGENT_QUEUED = "agent_queued"
EVENT_AGENT_DEQUEUED = "agent_dequeued"
EVENT_PHASE_STARTED = "phase_started"
EVENT_PHASE_COMPLETED = "phase_completed"
EVENT_SUMMARIZATION_TRIGGERED = "summarization_triggered"
EVENT_NODE_QUEUED = "node_queued"
EVENT_NODE_STARTED = "node_started"
EVENT_NODE_COMPLETED = "node_completed"
EVENT_NODE_FAILED = "node_failed"
EVENT_NODE_RETRYING = "node_retrying"
EVENT_NODE_SKIPPED = "node_skipped"
EVENT_FALLBACK_ACTIVATED = "fallback_activated"
EVENT_RUN_CANCEL = "run_cancel"
EVENT_NODE_INPUT_REQUESTED = "node_input_requested"
EVENT_NODE_INPUT_RESOLVED = "node_input_resolved"


# --- Immutable event payloads ---

@dataclass(frozen=True)
class VRAMThresholdExceeded:
    """Published when VRAM usage crosses the configured safety threshold."""

    used_bytes: int
    limit_bytes: int
    gpu_index: int


@dataclass(frozen=True)
class VRAMFreed:
    """Published when a model is unloaded / offloaded from VRAM."""

    model_name: str
    freed_bytes: int
    remaining_bytes: int


@dataclass(frozen=True)
class ModelOffloaded:
    """Published when a model is offloaded from VRAM to CPU RAM."""

    model_name: str
    endpoint: str
    latency_ms: float


@dataclass(frozen=True)
class ModelReloaded:
    """Published when a model is reloaded from CPU RAM back to VRAM."""

    model_name: str
    endpoint: str
    latency_ms: float


@dataclass(frozen=True)
class AgentQueued:
    """Published when an agent is queued due to insufficient VRAM capacity."""

    node_id: str
    model_name: str
    queue_position: int
    estimated_bytes: int


@dataclass(frozen=True)
class AgentDequeued:
    """Published when a queued agent is dequeued and capacity becomes available."""

    node_id: str
    model_name: str
    wait_seconds: float
    queue_position_was: int


@dataclass(frozen=True)
class PhaseStarted:
    """Published when a workflow phase begins execution."""

    phase_id: str
    phase_name: str
    run_id: str


@dataclass(frozen=True)
class PhaseCompleted:
    """Published when a workflow phase finishes execution."""

    phase_id: str
    phase_name: str
    run_id: str
    completed_at: datetime


@dataclass(frozen=True)
class SummarizationTriggered:
    """Published when a phase's shared context was summarized to fit the context window."""

    phase_id: str
    tokens_before: int
    tokens_after: int


@dataclass(frozen=True)
class NodeQueued:
    """Published when a node's upstreams are satisfied and it is queued to start."""

    node_id: str
    run_id: str
    queue_position: int | None = None
    wait_seconds: float | None = None


@dataclass(frozen=True)
class NodeStarted:
    """Published when a node enters the running state.

    Carries no timestamp — the envelope timestamp is the start time.
    """

    node_id: str
    run_id: str
    model: str | None
    input: str | None = None
    attempt: int = 1


@dataclass(frozen=True)
class NodeCompleted:
    """Published when a node finishes successfully."""

    node_id: str
    run_id: str
    model: str | None
    tokens_used: int
    duration_seconds: float
    attempt: int = 1
    output: str | None = None
    fallback: str | None = None


@dataclass(frozen=True)
class NodeFailed:
    """Published when a node fails."""

    node_id: str
    run_id: str
    model: str | None
    error: str
    stack_trace: str | None = None
    attempt: int = 1
    fallback: str | None = None


@dataclass(frozen=True)
class FallbackActivated:
    """Published when a failed node's fallback agent begins a replacement attempt.

    Carries the primary failure the fallback reacts to: the terminal attempt
    number, the configured fallback agent, and the primary's error message.
    """

    node_id: str
    run_id: str
    model: str | None
    fallback: str
    error: str
    attempt: int


@dataclass(frozen=True)
class NodeRetrying:
    """Published when a failing node is about to be retried."""

    node_id: str
    run_id: str
    model: str | None
    error: str
    attempt: int
    next_attempt: int
    delay_seconds: float


@dataclass(frozen=True)
class NodeSkipped:
    """Published when a node is skipped without being started."""

    node_id: str
    run_id: str
    reason: str
    skipped_bindings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunCancelled:
    """Published when the user cancels a run (Esc in TUI or SIGINT)."""

    run_id: str


@dataclass(frozen=True)
class NodeInputRequested:
    """Published when a node begins waiting for human input.

    Carries the question text and, when the node declares a timeout, the
    UTC deadline by which the input must arrive.
    """

    node_id: str
    run_id: str
    prompt: str
    deadline: datetime | None = None


@dataclass(frozen=True)
class NodeInputResolved:
    """Published when a node's pending human input is resolved or discarded
    (submitted, timed out, or the wait otherwise ended)."""

    node_id: str
    run_id: str


# --- Event envelope ---

@dataclass(frozen=True)
class EventEnvelope:
    """Immutable wrapper around any event payload with metadata."""

    event_type: str
    seq: int
    timestamp: datetime
    payload: Any

    @classmethod
    def create(cls, event_type: str, seq: int, payload: Any) -> EventEnvelope:
        return cls(
            event_type=event_type,
            seq=seq,
            timestamp=datetime.now(timezone.utc),
            payload=payload,
        )
