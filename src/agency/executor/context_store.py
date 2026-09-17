from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agency.core.event_bus import EventBus
from agency.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_STARTED,
    EventEnvelope,
    PhaseCompleted,
    PhaseStarted,
)


class ContextStore:
    """Per-phase context store with event bus integration and JSON-lines persistence.

    Maintains isolated dictionaries per phase.  On phase completion the context
    is persisted to ``runs/<run_id>/phase_<id>_context.jsonl`` then cleared
    from memory.

    Lifecycle events are accumulated synchronously as raw (event_type, payload)
    tuples and flushed via ``flush_events()`` (async) so the store itself
    remains non-async.
    """

    def __init__(
        self,
        run_id: str,
        log_dir: Path = Path("runs"),
        event_bus: EventBus | None = None,
    ) -> None:
        self._run_id = run_id
        self._log_dir = log_dir
        self._event_bus = event_bus
        self._contexts: dict[str, dict[str, Any]] = {}
        self._active_phases: list[str] = []
        self._phase_names: dict[str, str] = {}
        self._pending_events: list[tuple[str, Any]] = []
        self._seq: int = 0

    # -- lifecycle --------------------------------------------------------

    def start_phase(self, phase_id: str, phase_name: str) -> None:
        """Initialise an empty context for *phase_id*."""
        if phase_id in self._contexts:
            raise ValueError(f"Phase '{phase_id}' already started")
        self._contexts[phase_id] = {}
        self._active_phases.append(phase_id)
        self._phase_names[phase_id] = phase_name
        self._pending_events.append(
            (EVENT_PHASE_STARTED, PhaseStarted(phase_id=phase_id, phase_name=phase_name, run_id=self._run_id))
        )

    def record_output(self, phase_id: str, node_id: str, value: Any) -> None:
        """Store a node's output inside *phase_id*'s context."""
        ctx = self._contexts.get(phase_id)
        if ctx is None:
            raise KeyError(f"Phase '{phase_id}' has not been started")
        ctx[node_id] = value

    def complete_phase(self, phase_id: str) -> None:
        """Persist the phase context to disk then clear it from memory."""
        ctx = self._contexts.get(phase_id)
        if ctx is None:
            raise KeyError(f"Phase '{phase_id}' has not been started")
        self._persist_phase_context(phase_id, ctx)

        phase_name = self._phase_names.get(phase_id, phase_id)
        self._pending_events.append(
            (
                EVENT_PHASE_COMPLETED,
                PhaseCompleted(
                    phase_id=phase_id,
                    phase_name=phase_name,
                    run_id=self._run_id,
                    completed_at=datetime.now(timezone.utc),
                ),
            )
        )

        del self._contexts[phase_id]
        self._active_phases.remove(phase_id)

    # -- query ------------------------------------------------------------

    def get_context(self, phase_id: str) -> dict[str, Any]:
        """Return a flat dict of all node outputs for *phase_id*."""
        ctx = self._contexts.get(phase_id)
        if ctx is None:
            return {}
        return dict(ctx)

    def set_context(self, phase_id: str, context: dict[str, Any]) -> None:
        """Replace the in-memory context for *phase_id* (e.g. with a summary)."""
        if phase_id not in self._contexts:
            raise KeyError(f"Phase '{phase_id}' has not been started")
        self._contexts[phase_id] = context

    # -- events -----------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def flush_events(self) -> None:
        """Publish accumulated lifecycle events to the event bus."""
        if not self._event_bus or not self._pending_events:
            return
        for event_type, payload in self._pending_events:
            envelope = EventEnvelope.create(event_type, self._next_seq(), payload)
            await self._event_bus.publish(envelope)
        self._pending_events.clear()

    def get_pending_events(self) -> list[tuple[str, Any]]:
        """Return a copy of accumulated (unpublished) events."""
        return list(self._pending_events)

    # -- persistence ------------------------------------------------------

    def _persist_phase_context(self, phase_id: str, context: dict[str, Any]) -> None:
        """Append a single JSON line containing the full phase context."""
        log_path = self._log_dir / self._run_id / f"phase_{phase_id}_context.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "phase_id": phase_id,
            "run_id": self._run_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "context": context,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
