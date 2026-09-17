"""Durable run state: sync-fsynced WAL + atomic checkpoints (Story 4.2).

Second, independently durable subscriber beside :class:`RunLogger`:
``execution.jsonl`` is the human audit trail (4.1) while ``wal.jsonl`` and the
per-node checkpoint files are the crash-recovery substrate replayed by
4.3. Every node-status transition is made durable first — raw output file,
``flush()``+``fsync()`` WAL append, atomic checkpoint write — and only then is
the in-memory status map updated, so memory never leads disk.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EventEnvelope,
)
from agency.executor.retry import get_fallback, get_retry_policy, is_final_failure
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import Workflow

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
COMPLETED_FALLBACK = "completed-fallback"
FAILED = "failed"
SKIPPED = "skipped"

_STATUSES: tuple[str, ...] = (
    PENDING,
    RUNNING,
    COMPLETED,
    COMPLETED_FALLBACK,
    FAILED,
    SKIPPED,
)
_TERMINAL = frozenset({COMPLETED, COMPLETED_FALLBACK, FAILED, SKIPPED})
_MUTATION = "node_status"


def _fsync(fd: int) -> None:
    """Process-level fsync seam so tests can inject a failure."""
    os.fsync(fd)


@dataclass
class _StartRecord:
    """Snapshot captured at a node's first ``NodeStarted`` event."""

    model: str | None
    started_at: datetime


class RunStateStore:
    """Pure event-bus subscriber that makes every node-status transition durable.

    Same containment standard as :class:`RunLogger`: no imports/calls of TUI or
    executor code, no emitted events, and nothing escapes to the bus or the
    TUI. Per transition the durable steps run in strict order — raw output
    file, WAL append, atomic checkpoint — and the in-memory map is updated
    only after they succeed; a failure at any step logs a warning and drops
    the remaining steps and the mutation.
    """

    def __init__(
        self,
        log_dir: Path | str,
        run_id: str,
        workflow: Workflow,
        event_bus: EventBus | None = None,
        *,
        vram_limit_bytes: int | None = None,
        replay_source: dict[str, str] | None = None,
        workflow_path: str | Path | None = None,
        execution_environment: str | None = None,
    ) -> None:
        self._run_id = run_id
        self._run_dir = Path(log_dir) / run_id
        self._workflow = workflow
        self._bus = event_bus if event_bus is not None else get_event_bus()
        self._vram_limit_bytes = vram_limit_bytes
        self._replay_source = replay_source
        self._workflow_path = workflow_path
        self._execution_environment = execution_environment
        self._dependents: dict[str, list[str]] = DAGBuilder(workflow).build_dag()
        self._status: dict[str, str] = {node_id: PENDING for node_id in workflow.nodes}
        self._started: dict[str, _StartRecord] = {}
        self._tokens: list[UnsubscribeToken] = []
        self._next_restore_seq: int = 1

    async def start(self) -> None:
        """Create the run dir, write ``metadata.json``, seed WAL seq 0, and
        subscribe to the four node lifecycle events.

        ``metadata.json`` carries the ``replay_source`` block (source run id
        plus the checkpoint node) only for replay runs, and the additive
        ``execution_environment`` field (``"sandbox"`` or ``"local"``) only when
        the run site resolved and supplied an environment."""
        wal_path = self._run_dir / "wal.jsonl"
        if wal_path.is_file():
            return
        started_at = datetime.now(timezone.utc)
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create run dir '%s': %s", self._run_dir, exc)
        metadata = {
            "run_id": self._run_id,
            "workflow_name": self._workflow.name,
            "started_at": started_at.isoformat(),
            "node_count": len(self._workflow.nodes),
            "vram_limit_bytes": self._vram_limit_bytes,
        }
        if self._replay_source is not None:
            metadata["replay_source"] = self._replay_source
        if self._workflow_path is not None:
            metadata["workflow_path"] = str(self._workflow_path)
        if self._execution_environment is not None:
            metadata["execution_environment"] = self._execution_environment
        self._atomic_write(
            self._run_dir / "metadata.json",
            (json.dumps(metadata, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        self._wal_append(
            {
                "seq": 0,
                "ts": started_at.isoformat(),
                "mutation": "run_started",
                "run_id": self._run_id,
            }
        )
        for event_type, handler in (
            (EVENT_NODE_STARTED, self._on_node_started),
            (EVENT_NODE_COMPLETED, self._on_node_completed),
            (EVENT_NODE_FAILED, self._on_node_failed),
            (EVENT_NODE_SKIPPED, self._on_node_skipped),
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
            node_id = payload.node_id
            if node_id not in self._status:
                logger.warning(
                    "Ignoring %s event for unknown node '%s'", EVENT_NODE_STARTED, node_id
                )
                return
            if self._status[node_id] != PENDING:
                logger.debug(
                    "ignoring %s for non-pending node '%s'", EVENT_NODE_STARTED, node_id
                )
                return
            if self._persist_transition(envelope, node_id, RUNNING):
                self._started[node_id] = _StartRecord(
                    model=payload.model,
                    started_at=envelope.timestamp,
                )
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run state could not persist node_started: %s", exc)

    async def _on_node_completed(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            node_id = payload.node_id
            if node_id not in self._status:
                logger.warning(
                    "Ignoring %s event for unknown node '%s'", EVENT_NODE_COMPLETED, node_id
                )
                return
            if self._status[node_id] in _TERMINAL:
                logger.debug(
                    "ignoring second terminal event for node '%s'", node_id
                )
                return
            record = self._started.get(node_id)
            output = payload.output
            fallback = payload.fallback
            if self._persist_transition(
                envelope,
                node_id,
                COMPLETED_FALLBACK if fallback is not None else COMPLETED,
                output_text=output,
                output_ref=f"outputs/{node_id}.txt" if output is not None else None,
                summary=output[:200] if output is not None else None,
                tokens_used=payload.tokens_used,
                model=(
                    payload.model
                    if fallback is not None
                    else record.model if record is not None else payload.model
                ),
                elapsed_seconds=payload.duration_seconds,
            ):
                self._started.pop(node_id, None)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run state could not persist node_completed: %s", exc)

    async def _on_node_failed(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            node_id = payload.node_id
            if node_id not in self._status:
                logger.warning(
                    "Ignoring %s event for unknown node '%s'", EVENT_NODE_FAILED, node_id
                )
                return
            if self._status[node_id] in _TERMINAL:
                logger.debug(
                    "ignoring second terminal event for node '%s'", node_id
                )
                return
            policy = get_retry_policy(self._workflow, node_id)
            attempt = getattr(payload, "attempt", 1)
            if not is_final_failure(
                policy,
                attempt,
                fallback_configured=get_fallback(self._workflow, node_id) is not None,
                fallback_failed=payload.fallback is not None,
            ):
                logger.debug(
                    "non-final failure for node '%s' (attempt %d), keeping running",
                    node_id,
                    attempt,
                )
                return
            record = self._started.get(node_id)
            elapsed_seconds = (
                (envelope.timestamp - record.started_at).total_seconds()
                if record is not None
                else None
            )
            if self._persist_transition(
                envelope,
                node_id,
                FAILED,
                output_ref=None,
                summary=payload.error,
                tokens_used=None,
                model=(
                    payload.model
                    if payload.fallback is not None
                    else record.model if record is not None else payload.model
                ),
                elapsed_seconds=elapsed_seconds,
            ):
                self._started.pop(node_id, None)
                self._skip_dependents(node_id, envelope)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run state could not persist node_failed: %s", exc)

    async def _on_node_skipped(self, envelope: EventEnvelope) -> None:
        try:
            payload = envelope.payload
            if getattr(payload, "run_id", None) != self._run_id:
                return
            node_id = payload.node_id
            if node_id not in self._status:
                logger.warning(
                    "Ignoring %s event for unknown node '%s'",
                    EVENT_NODE_SKIPPED,
                    node_id,
                )
                return
            if self._status[node_id] in _TERMINAL:
                logger.debug(
                    "ignoring second terminal event for node '%s'", node_id
                )
                return
            record = self._started.get(node_id)
            elapsed_seconds = (
                (envelope.timestamp - record.started_at).total_seconds()
                if record is not None
                else None
            )
            if self._persist_transition(
                envelope,
                node_id,
                SKIPPED,
                output_ref=None,
                summary=getattr(payload, "reason", None),
                tokens_used=None,
                model=record.model if record is not None else None,
                elapsed_seconds=elapsed_seconds,
            ):
                self._started.pop(node_id, None)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("run state could not persist node_skipped: %s", exc)

    # -- restore seam -------------------------------------------------------

    def record_restored(
        self,
        node_id: str,
        *,
        output_text: str | None = None,
        output_ref: str | None = None,
        summary: str | None = None,
        tokens_used: int | None = None,
        elapsed_seconds: float | None = None,
        model: str | None = None,
    ) -> bool:
        """Persist a restored node without publishing any bus event.

        Used by replay to checkpoint prefix nodes that were copied from a
        prior run. Returns False on durability failure (memory never leads).
        """
        if node_id not in self._status:
            logger.warning("record_restored: unknown node '%s'", node_id)
            return False
        if self._status[node_id] in _TERMINAL:
            logger.debug("record_restored: node '%s' already terminal", node_id)
            return False

        seq = self._next_restore_seq
        self._next_restore_seq += 1
        now = datetime.now(timezone.utc)

        if output_text is not None and not self._atomic_write(
            self._run_dir / "outputs" / f"{node_id}.txt", output_text.encode("utf-8")
        ):
            return False

        entry: dict[str, Any] = {
            "seq": seq,
            "ts": now.isoformat(),
            "mutation": _MUTATION,
            "node_id": node_id,
            "from": self._status[node_id],
            "to": COMPLETED,
        }
        entry["output_ref"] = output_ref
        entry["tokens_used"] = tokens_used
        entry["elapsed_seconds"] = elapsed_seconds

        if not self._wal_append(entry):
            return False

        if not self._atomic_write(
            self._run_dir / "checkpoints" / f"{node_id}.json",
            (
                json.dumps(
                    {
                        "node_id": node_id,
                        "workflow_id": self._workflow.name,
                        "run_id": self._run_id,
                        "status": COMPLETED,
                        "output_ref": output_ref,
                        "summary": summary,
                        "tokens_used": tokens_used,
                        "model": model,
                        "elapsed_seconds": elapsed_seconds,
                        "timestamp": now.isoformat(),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        ):
            return False

        self._status[node_id] = COMPLETED
        return True

    # -- internals ----------------------------------------------------------

    def _skip_dependents(self, failed_id: str, envelope: EventEnvelope) -> None:
        """Transitively persist ``skipped`` for pending dependents.

        Mirrors ``RunLogger._skip_dependents``: a BFS over the adjacency list
        that persists pending dependents in place, and continues through
        pending and already-skipped nodes but never through running or
        terminal ones.
        """
        queue: deque[str] = deque(self._dependents.get(failed_id, ()))
        while queue:
            current = queue.popleft()
            status = self._status.get(current)
            if status is None or status in (RUNNING, COMPLETED, COMPLETED_FALLBACK, FAILED):
                continue
            if status == PENDING:
                self._persist_transition(envelope, current, SKIPPED)
            queue.extend(self._dependents.get(current, ()))

    def _persist_transition(
        self,
        envelope: EventEnvelope,
        node_id: str,
        to: str,
        *,
        output_text: str | None = None,
        output_ref: str | None = None,
        summary: str | None = None,
        tokens_used: int | None = None,
        model: str | None = None,
        elapsed_seconds: float | None = None,
    ) -> bool:
        """Run the durable steps for one transition; update the map only after
        they all succeed; return ``False`` (leaving the map untouched) on any
        failure so memory never leads disk.
        """
        if output_text is not None and not self._atomic_write(
            self._run_dir / "outputs" / f"{node_id}.txt", output_text.encode("utf-8")
        ):
            return False
        entry: dict[str, Any] = {
            "seq": envelope.seq,
            "ts": envelope.timestamp.isoformat(),
            "mutation": _MUTATION,
            "node_id": node_id,
            "from": self._status[node_id],
            "to": to,
        }
        if to in _TERMINAL:
            entry["output_ref"] = output_ref
            entry["tokens_used"] = tokens_used
            entry["elapsed_seconds"] = elapsed_seconds
        if not self._wal_append(entry):
            return False
        if to in _TERMINAL and not self._atomic_write(
            self._run_dir / "checkpoints" / f"{node_id}.json",
            (
                json.dumps(
                    {
                        "node_id": node_id,
                        "workflow_id": self._workflow.name,
                        "run_id": self._run_id,
                        "status": to,
                        "output_ref": output_ref,
                        "summary": summary,
                        "tokens_used": tokens_used,
                        "model": model,
                        "elapsed_seconds": elapsed_seconds,
                        "timestamp": envelope.timestamp.isoformat(),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        ):
            return False
        self._status[node_id] = to
        return True

    def _wal_append(self, entry: dict[str, Any]) -> bool:
        """Per-open append with ``flush()``+``fsync()`` so the line is durable
        before the in-memory transition is allowed to happen."""
        try:
            self._wal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._wal_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
                handle.flush()
                _fsync(handle.fileno())
            return True
        except OSError as exc:
            logger.warning("could not append WAL line to '%s': %s", self._wal_path, exc)
            return False

    def _atomic_write(self, path: Path, data: bytes) -> bool:
        """Temp file in the target dir, ``flush()``+``fsync()``, ``os.replace()``,
        best-effort dir fsync. On failure the temp file is removed so nothing
        partial is ever visible."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                _fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_dir(path.parent)
        except OSError as exc:
            logger.warning("could not atomically write '%s': %s", path, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        return True

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            _fsync(fd)
        except OSError:
            logger.warning("could not fsync directory '%s'", path)
        finally:
            os.close(fd)

    @property
    def _wal_path(self) -> Path:
        return self._run_dir / "wal.jsonl"


def recover_status(log_dir: Path | str, run_id: str, workflow: Workflow) -> dict[str, str]:
    """Rebuild the node-status map by replaying the run's WAL.

    All workflow nodes start seeded ``pending``; only committed, well-formed,
    strictly-increasing ``node_status`` lines advance a node. Unparseable,
    non-object, seq-less, duplicate/out-of-order, unknown-node, or unknown-
    status lines are dropped with a warning.
    """
    status: dict[str, str] = {node_id: PENDING for node_id in workflow.nodes}
    wal_path = Path(log_dir) / run_id / "wal.jsonl"
    try:
        lines = wal_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("could not read WAL '%s': %s", wal_path, exc)
        return status
    last_seq = -1
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            logger.warning("WAL line is unparseable, skipping: %.120s", line)
            continue
        if not isinstance(entry, dict):
            logger.warning("WAL line is not a JSON object, skipping: %.120s", line)
            continue
        seq = entry.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            logger.warning("WAL line without integer seq, skipping: %.120s", line)
            continue
        if seq <= last_seq:
            logger.warning("duplicate or out-of-order WAL seq %s, skipping", seq)
            continue
        last_seq = seq
        if entry.get("mutation") != _MUTATION:
            continue
        node_id = entry.get("node_id")
        if node_id not in status:
            logger.warning("WAL line for unknown node '%s', skipping", node_id)
            continue
        new_status = entry.get("to")
        if new_status not in _STATUSES:
            logger.warning("WAL line with unknown status '%s', skipping", new_status)
            continue
        status[node_id] = new_status
    return status
