"""Structured execution log: JSON-lines subscriber for a single run (Story 4.1)."""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agency.core.event_bus import EventBus, get_event_bus
from agency.core.events import (
    EVENT_FALLBACK_ACTIVATED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_STARTED,
    EventEnvelope,
    FallbackActivated,
)
from agency.executor.retry import get_fallback, get_retry_policy, is_final_failure
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import Workflow

logger = logging.getLogger(__name__)


@dataclass
class _StartRecord:
    """Liveness snapshot captured at a node's first ``NodeStarted`` event."""

    input: str | None
    model: str | None
    started_at: datetime


class RunLogger:
    """Pure event-bus subscriber appending ``runs/{run_id}/execution.jsonl``.

    Subscribes to the four node lifecycle/recovery events — never imports or
    calls TUI or executor code. Every line is one compact JSON object: a
    ``node_started`` liveness snapshot (unknown fields ``null``), a once-per-run
    ``node_fallback_activated`` line per fallback activation, and a
    once-per-node terminal line that is the canonical per-node record
    (``node_failed`` lines also carry the error and stack trace). Node
    failures additionally walk the DAG adjacency over not-yet-terminal nodes
    and emit one ``node_skipped`` line per dependent. All I/O is guarded: a
    write failure only logs a warning and drops the line; nothing escapes to
    the bus or the TUI.
    """

    def __init__(
        self,
        log_dir: Path | str,
        run_id: str,
        workflow: Workflow,
        event_bus: EventBus | None = None,
    ) -> None:
        self._run_id = run_id
        self._log_path = Path(log_dir) / run_id / "execution.jsonl"
        self._workflow = workflow
        self._bus = event_bus if event_bus is not None else get_event_bus()
        self._dependents: dict[str, list[str]] = DAGBuilder(workflow).build_dag()
        self._started: dict[str, _StartRecord] = {}
        self._terminal: dict[str, str] = {}
        self._tokens = []

    async def start(self) -> None:
        """Create the run log (writing the immediate ``run_started`` line) and
        subscribe to the node lifecycle/recovery events."""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "could not create run log directory '%s': %s", self._log_path.parent, exc
            )
        self._append(
            {
                "type": "run_started",
                "run_id": self._run_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        for event_type, handler in (
            (EVENT_NODE_STARTED, self._on_node_started),
            (EVENT_NODE_COMPLETED, self._on_node_completed),
            (EVENT_NODE_FAILED, self._on_node_failed),
            (EVENT_NODE_RETRYING, self._on_node_retrying),
            (EVENT_FALLBACK_ACTIVATED, self._on_node_fallback_activated),
        ):
            self._tokens.append(await self._bus.subscribe(event_type, handler))

    async def close(self) -> None:
        """Unsubscribe from the bus; no file handle is held per append."""
        for token in self._tokens:
            token.cancel()
        self._tokens.clear()

    # -- event handlers ---------------------------------------------------

    async def _on_node_started(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            if self._reject_unknown(EVENT_NODE_STARTED, payload.node_id):
                return
            if payload.node_id in self._terminal:
                logger.debug(
                    "ignoring late %s for terminal node '%s'", EVENT_NODE_STARTED, payload.node_id
                )
                return
            if payload.node_id in self._started:
                logger.debug(
                    "ignoring duplicate %s for node '%s'", EVENT_NODE_STARTED, payload.node_id
                )
                return
            self._started[payload.node_id] = _StartRecord(
                input=payload.input,
                model=payload.model,
                started_at=envelope.timestamp,
            )
            self._append(
                {
                    "type": "node_started",
                    "run_id": self._run_id,
                    "seq": envelope.seq,
                    "node_id": payload.node_id,
                    "status": "running",
                    "model": payload.model,
                    "input": payload.input,
                    "started_at": envelope.timestamp.isoformat(),
                    "output": None,
                    "ended_at": None,
                    "tokens_used": None,
                    "attempt": getattr(payload, "attempt", 1),
                }
            )
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run log could not write node_started: %s", exc)

    async def _on_node_completed(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            if self._reject_unknown(EVENT_NODE_COMPLETED, payload.node_id):
                return
            if payload.node_id in self._terminal:
                logger.debug(
                    "ignoring second terminal event for node '%s'", payload.node_id
                )
                return
            record = self._started.pop(payload.node_id, None)
            self._terminal[payload.node_id] = "completed"
            fallback = payload.fallback
            entry: dict[str, Any] = {
                "type": "node_completed",
                "run_id": self._run_id,
                "seq": envelope.seq,
                "node_id": payload.node_id,
                "status": "completed-fallback" if fallback is not None else "completed",
                "model": payload.model if fallback is not None else self._model_of(record, payload.model),
                "input": record.input if record is not None else None,
                "output": payload.output,
                "started_at": record.started_at.isoformat() if record is not None else None,
                "ended_at": envelope.timestamp.isoformat(),
                "duration_seconds": payload.duration_seconds,
                "tokens_used": payload.tokens_used,
            }
            if fallback is not None:
                entry["fallback"] = fallback
            self._append(entry)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run log could not write node_completed: %s", exc)

    async def _on_node_failed(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            if self._reject_unknown(EVENT_NODE_FAILED, payload.node_id):
                return
            if payload.node_id in self._terminal:
                logger.debug(
                    "ignoring second terminal event for node '%s'", payload.node_id
                )
                return
            policy = get_retry_policy(self._workflow, payload.node_id)
            attempt = getattr(payload, "attempt", 1)
            if not is_final_failure(
                policy,
                attempt,
                fallback_configured=(
                    get_fallback(self._workflow, payload.node_id) is not None
                ),
                fallback_failed=payload.fallback is not None,
            ):
                logger.debug(
                    "non-final failure for node '%s' (attempt %d), skipping terminal log",
                    payload.node_id,
                    attempt,
                )
                return
            record = self._started.pop(payload.node_id, None)
            self._terminal[payload.node_id] = "failed"
            duration_seconds = (
                (envelope.timestamp - record.started_at).total_seconds()
                if record is not None
                else None
            )
            fallback = payload.fallback
            entry: dict[str, Any] = {
                "type": "node_failed",
                "run_id": self._run_id,
                "seq": envelope.seq,
                "node_id": payload.node_id,
                "status": "failed",
                "model": payload.model if fallback is not None else self._model_of(record, payload.model),
                "input": record.input if record is not None else None,
                "output": None,
                "started_at": record.started_at.isoformat() if record is not None else None,
                "ended_at": envelope.timestamp.isoformat(),
                "duration_seconds": duration_seconds,
                "tokens_used": None,
                "error": payload.error,
                "stack_trace": payload.stack_trace,
                "attempt": attempt,
            }
            if fallback is not None:
                entry["fallback"] = fallback
            self._append(entry)
            self._skip_dependents(payload.node_id, envelope)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run log could not write node_failed: %s", exc)

    async def _on_node_retrying(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            if self._reject_unknown(EVENT_NODE_RETRYING, payload.node_id):
                return
            self._append(
                {
                    "type": "node_retrying",
                    "run_id": self._run_id,
                    "seq": envelope.seq,
                    "node_id": payload.node_id,
                    "attempt": payload.attempt,
                    "next_attempt": payload.next_attempt,
                    "delay_seconds": payload.delay_seconds,
                    "error": payload.error,
                }
            )
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run log could not write node_retrying: %s", exc)

    async def _on_node_fallback_activated(self, envelope: EventEnvelope) -> None:
        try:
            payload: FallbackActivated = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            if self._reject_unknown(EVENT_FALLBACK_ACTIVATED, payload.node_id):
                return
            self._append(
                {
                    "type": "node_fallback_activated",
                    "run_id": self._run_id,
                    "seq": envelope.seq,
                    "node_id": payload.node_id,
                    "model": payload.model,
                    "fallback": payload.fallback,
                    "attempt": payload.attempt,
                    "error": payload.error,
                }
            )
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning(
                "run log could not write node_fallback_activated: %s", exc
            )

    # -- internals ----------------------------------------------------------

    def _skip_dependents(self, failed_id: str, envelope: EventEnvelope) -> None:
        """Transitively emit ``node_skipped`` lines for pending dependents.

        Mirrors ``DAGView.mark_skipped_dependents``: a BFS over the adjacency
        list that skips pending rows (writing a line) and continues through
        them and through already-skipped ones, but never through running or
        completed/failed nodes.
        """
        queue: deque[str] = deque(self._dependents.get(failed_id, ()))
        while queue:
            current = queue.popleft()
            status = self._terminal.get(current)
            if status is None and current in self._started:
                continue
            if status is None:
                self._terminal[current] = "skipped"
                self._append(
                    {
                        "type": "node_skipped",
                        "run_id": self._run_id,
                        "seq": envelope.seq,
                        "node_id": current,
                        "status": "skipped",
                        "model": None,
                        "input": None,
                        "output": None,
                        "started_at": None,
                        "ended_at": envelope.timestamp.isoformat(),
                        "duration_seconds": None,
                        "tokens_used": None,
                    }
                )
            if status is None or status == "skipped":
                queue.extend(self._dependents.get(current, ()))

    def _reject_unknown(self, event_type: str, node_id: str) -> bool:
        if node_id not in self._workflow.nodes:
            logger.warning(
                "Ignoring %s event for unknown node '%s'", event_type, node_id
            )
            return True
        return False

    @staticmethod
    def _model_of(record: _StartRecord | None, fallback: str | None) -> str | None:
        return record.model if record is not None else fallback

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning("could not append to '%s': %s", self._log_path, exc)
