"""In-memory run registry: live run handles, bus mirroring, pending inputs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agency.api.schemas import NodeStateView, RunHistoryEntry
from agency.core.event_bus import EventBus, get_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_INPUT_REQUESTED,
    EVENT_NODE_INPUT_RESOLVED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeInputRequested,
    NodeInputResolved,
)
from agency.executor.orchestrator import (
    COMPLETED,
    COMPLETED_FALLBACK,
    FAILED,
    SKIPPED,
)
from agency.executor.retry import get_fallback, get_retry_policy, is_final_failure
from agency.executor.run_state import recover_status
from agency.yaml_engine.parser import (
    SchemaValidationError,
    YAMLParseError,
    load_workflow,
)
from agency.yaml_engine.schema import Workflow

logger = logging.getLogger(__name__)

_TERMINAL_NODE_STATUSES = frozenset({COMPLETED, COMPLETED_FALLBACK, FAILED, SKIPPED})


@dataclass
class PendingInput:
    """A node blocked on human input, waiting for an HTTP submission."""

    node_id: str
    prompt: str
    deadline: datetime | None
    future: asyncio.Future[str]


@dataclass
class RunHandle:
    """In-memory state for a run owned by this service process.

    ``workflow`` is ``None`` for recovered runs whose workflow file is no
    longer available on disk.
    """

    run_id: str
    workflow_name: str
    started_at: str
    workflow: Workflow | None
    run_dir: Path
    nodes: dict[str, NodeStateView] = field(default_factory=dict)
    state: str = "running"
    result: Any | None = None
    pending_inputs: dict[str, PendingInput] = field(default_factory=dict)
    replay_source: Any | None = None
    workflow_path: str | None = None
    task: asyncio.Task | None = None
    mirror_tokens: list = field(default_factory=list)

    def register_pending_input(
        self, node_id: str, prompt: str, deadline: datetime | None
    ) -> PendingInput:
        loop = asyncio.get_running_loop()
        entry = PendingInput(
            node_id=node_id, prompt=prompt, deadline=deadline, future=loop.create_future()
        )
        self.pending_inputs[node_id] = entry
        return entry


def _json_status(status: str) -> str:
    """Map executor statuses (hyphenated) onto the JSON vocabulary."""
    return status.replace("-", "_")


def run_dir_metadata(log_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    """Read ``runs/<run_id>/metadata.json``; ``None`` when absent or unreadable."""
    path = Path(log_dir) / run_id / "metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_wal_statuses(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Last committed status per node from the WAL (metrics on terminal lines)."""
    statuses: dict[str, dict[str, Any]] = {}
    wal_path = run_dir / "wal.jsonl"
    try:
        lines = wal_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return statuses
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("mutation") != "node_status":
            continue
        node_id = entry.get("node_id")
        status = entry.get("to")
        if not node_id or not status:
            continue
        state: dict[str, Any] = {"status": status}
        if entry.get("output_ref") is not None or entry.get("tokens_used") is not None:
            state["tokens"] = entry.get("tokens_used")
            state["duration_seconds"] = entry.get("elapsed_seconds")
        statuses[node_id] = state
    return statuses


def node_states_from_run_dir(run_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Per-node state derived from the WAL, overridden by terminal checkpoints."""
    run_dir = Path(run_dir)
    states = _read_wal_statuses(run_dir)
    ckpt_dir = run_dir / "checkpoints"
    if ckpt_dir.is_dir():
        for path in sorted(ckpt_dir.iterdir()):
            if path.suffix != ".json":
                continue
            try:
                ck = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(ck, dict):
                continue
            node_id = ck.get("node_id")
            if node_id not in states:
                continue
            states[node_id] = {
                "status": ck.get("status"),
                "model": ck.get("model"),
                "tokens": ck.get("tokens_used"),
                "duration_seconds": ck.get("elapsed_seconds"),
                "summary": ck.get("summary"),
            }
    return states


def _state_to_view(node_id: str, state: dict[str, Any]) -> NodeStateView:
    status = _json_status(state.get("status", "pending"))
    reason = None
    if status in ("failed", "skipped"):
        reason = state.get("summary")
    return NodeStateView(
        status=status,
        model=state.get("model"),
        tokens=state.get("tokens"),
        duration_seconds=state.get("duration_seconds"),
        attempt=1,
        fallback=None,
        reason=reason,
    )


def derive_state(states: dict[str, NodeStateView]) -> str:
    """Derive a run state from per-node states (no live handle available)."""
    if not states:
        return "interrupted"
    if all(view.status in _TERMINAL_NODE_STATUSES for view in states.values()):
        return "failed" if any(view.status == "failed" for view in states.values()) else "completed"
    return "interrupted"


def _load_workflow_for_recovery(path: Any) -> Workflow | None:
    """Load the workflow recorded in a run's metadata; ``None`` when unusable."""
    if not isinstance(path, str) or not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        return load_workflow(file)
    except (YAMLParseError, SchemaValidationError) as exc:
        logger.warning("could not load workflow '%s' for recovery: %s", path, exc)
        return None


def _same_run(handle: RunHandle, envelope: EventEnvelope) -> bool:
    return getattr(envelope.payload, "run_id", None) == handle.run_id


def _mirror_node_started(handle: RunHandle, envelope: EventEnvelope) -> None:
    if not _same_run(handle, envelope):
        return
    p = envelope.payload
    handle.nodes[p.node_id] = NodeStateView(
        status="running", model=p.model, attempt=p.attempt
    )


def _mirror_node_completed(handle: RunHandle, envelope: EventEnvelope) -> None:
    if not _same_run(handle, envelope):
        return
    p = envelope.payload
    status = "completed_fallback" if p.fallback is not None else "completed"
    handle.nodes[p.node_id] = NodeStateView(
        status=status,
        model=p.model,
        tokens=p.tokens_used,
        duration_seconds=p.duration_seconds,
        attempt=p.attempt,
        fallback=p.fallback,
    )


def _mirror_node_failed(handle: RunHandle, envelope: EventEnvelope) -> None:
    if not _same_run(handle, envelope):
        return
    p = envelope.payload
    policy = get_retry_policy(handle.workflow, p.node_id)
    fallback = get_fallback(handle.workflow, p.node_id)
    final = is_final_failure(
        policy,
        p.attempt,
        fallback_configured=fallback is not None,
        fallback_failed=p.fallback is not None,
    )
    handle.nodes[p.node_id] = NodeStateView(
        status="failed" if final else "awaiting_retry",
        model=p.model,
        attempt=p.attempt,
        reason=p.error,
    )


def _mirror_node_skipped(handle: RunHandle, envelope: EventEnvelope) -> None:
    if not _same_run(handle, envelope):
        return
    p = envelope.payload
    prev = handle.nodes.get(p.node_id)
    handle.nodes[p.node_id] = NodeStateView(
        status="skipped",
        model=prev.model if prev is not None else None,
        attempt=prev.attempt if prev is not None else 1,
        reason=p.reason,
    )


_MIRROR_EVENTS: tuple[tuple[str, Callable[[RunHandle, EventEnvelope], None]], ...] = (
    (EVENT_NODE_STARTED, _mirror_node_started),
    (EVENT_NODE_COMPLETED, _mirror_node_completed),
    (EVENT_NODE_FAILED, _mirror_node_failed),
    (EVENT_NODE_SKIPPED, _mirror_node_skipped),
)


def _make_mirror_handler(
    handle: RunHandle, handler: Callable[[RunHandle, EventEnvelope], None]
) -> Callable[[EventEnvelope], Awaitable[None]]:
    async def _handler(envelope: EventEnvelope) -> None:
        handler(handle, envelope)

    return _handler


class RunRegistry:
    """Owns the live run handles of this service process."""

    def __init__(self) -> None:
        self._handles: dict[str, RunHandle] = {}

    def register(self, handle: RunHandle) -> None:
        self._handles[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    def remove(self, run_id: str) -> None:
        self._handles.pop(run_id, None)

    @property
    def handles(self) -> list[RunHandle]:
        return list(self._handles.values())

    def history(self, log_dir: Path | str) -> list[RunHistoryEntry]:
        """Run history: live handles merged with the durable run dirs on disk.

        Live handles win on run id collision; run dirs without readable
        metadata are skipped. Scanned per call (research R11).
        """
        log_dir = Path(log_dir)
        entries: dict[str, RunHistoryEntry] = {
            run_id: RunHistoryEntry(
                run_id=run_id,
                workflow_name=handle.workflow_name,
                started_at=handle.started_at,
                state=handle.state,
            )
            for run_id, handle in self._handles.items()
        }
        if log_dir.is_dir():
            for run_dir in sorted(log_dir.iterdir()):
                if not run_dir.is_dir() or run_dir.name in entries:
                    continue
                metadata = run_dir_metadata(log_dir, run_dir.name)
                if metadata is None:
                    continue
                views = {
                    node_id: _state_to_view(node_id, state)
                    for node_id, state in node_states_from_run_dir(run_dir).items()
                }
                entries[run_dir.name] = RunHistoryEntry(
                    run_id=run_dir.name,
                    workflow_name=metadata.get("workflow_name", ""),
                    started_at=metadata.get("started_at", ""),
                    state=derive_state(views),
                )
        return sorted(entries.values(), key=lambda e: (e.started_at, e.run_id))

    def recover_interrupted(self, log_dir: Path | str) -> list[str]:
        """Register interrupted runs found on disk so the API can serve them.

        Terminal runs are never registered. When the workflow file still
        exists, per-node states come from ``recover_status`` (all workflow
        nodes, including still-pending ones); otherwise WAL-only states are
        used. A run with zero recorded node statuses gets an empty node map.
        Never resumes a run (FR-018).
        """
        log_dir = Path(log_dir)
        recovered: list[str] = []
        if not log_dir.is_dir():
            return recovered
        for run_dir in sorted(log_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.name in self._handles:
                continue
            metadata = run_dir_metadata(log_dir, run_dir.name)
            if metadata is None:
                continue
            raw_states = node_states_from_run_dir(run_dir)
            views = {
                node_id: _state_to_view(node_id, state)
                for node_id, state in raw_states.items()
            }
            if derive_state(views) != "interrupted":
                continue
            workflow = _load_workflow_for_recovery(metadata.get("workflow_path"))
            if workflow is not None and raw_states:
                statuses = recover_status(log_dir, run_dir.name, workflow)
                nodes = {
                    node_id: _state_to_view(
                        node_id, {**raw_states.get(node_id, {}), "status": status}
                    )
                    for node_id, status in statuses.items()
                }
            else:
                nodes = views
            handle = RunHandle(
                run_id=run_dir.name,
                workflow_name=metadata.get("workflow_name", ""),
                started_at=metadata.get("started_at", ""),
                workflow=workflow,
                run_dir=run_dir,
                nodes=nodes,
                state="interrupted",
                workflow_path=metadata.get("workflow_path")
                if isinstance(metadata.get("workflow_path"), str)
                else None,
            )
            self.register(handle)
            recovered.append(run_dir.name)
            if workflow is None:
                logger.warning(
                    "recovered interrupted run %s with WAL-only node states; "
                    "workflow file unavailable: %s",
                    run_dir.name,
                    metadata.get("workflow_path"),
                )
        return recovered

    async def subscribe_mirror(self, handle: RunHandle, bus: EventBus | None = None) -> None:
        """Subscribe one mirror handler set to the four node lifecycle events.

        Handlers guard on ``payload.run_id`` (research R2) so a global bus
        serving multiple runs never leaks state between handles.
        """
        bus = bus if bus is not None else get_event_bus()
        for event_type, handler in _MIRROR_EVENTS:
            handle.mirror_tokens.append(await bus.subscribe(event_type, _make_mirror_handler(handle, handler)))

    def resolve_input(self, handle: RunHandle, node_id: str, value: str) -> bool:
        """Resolve the pending input for *node_id*. First value wins."""
        entry = handle.pending_inputs.get(node_id)
        if entry is None or entry.future.done():
            return False
        entry.future.set_result(value)
        return True


def api_input_source(run_id: str, registry: RunRegistry) -> Callable[[str, str], Awaitable[str]]:
    """Build an ``InputSource`` that registers pending inputs on the handle.

    The entry is removed on completion (submission, timeout, or error) so a
    node awaiting input can only be resolved once per wait.
    """

    async def _input_source(node_id: str, prompt: str) -> str:
        handle = registry.get(run_id)
        if handle is None:
            raise RuntimeError(f"run {run_id} not found in registry")
        node = handle.workflow.nodes.get(node_id)
        timeout_seconds = getattr(node, "timeout_seconds", None)
        deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            if timeout_seconds
            else None
        )
        bus = get_event_bus()
        entry = handle.register_pending_input(node_id, prompt, deadline)
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_INPUT_REQUESTED,
                bus._next_seq(),
                NodeInputRequested(
                    node_id=node_id, run_id=run_id, prompt=prompt, deadline=deadline
                ),
            )
        )
        try:
            return await entry.future
        finally:
            if handle.pending_inputs.get(node_id) is entry:
                del handle.pending_inputs[node_id]
                await bus.publish(
                    EventEnvelope.create(
                        EVENT_NODE_INPUT_RESOLVED,
                        bus._next_seq(),
                        NodeInputResolved(node_id=node_id, run_id=run_id),
                    )
                )

    return _input_source
