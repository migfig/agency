"""Live execution metrics panel (Story 3.4).

A pure-Python :class:`MetricsCollector` — no I/O, no clock, no UI — is fed by
the app from node lifecycle events plus one VRAM sample per UI tick, and
rendered as a fixed four-line :class:`MetricsPanel`::

    total tokens: 1024
    vram: 1.54 GB
    elapsed: 1:30
    active agents: 2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rich.text import Text
from textual.widget import Widget

_GB = 1e9


@dataclass(frozen=True)
class MetricsSnapshot:
    """Point-in-time read of the run's execution metrics."""

    total_tokens: int
    active_agents: int
    vram_bytes: int | None
    vram_peak_bytes: int | None
    elapsed_seconds: float


class MetricsCollector:
    """Aggregates node lifecycle events and VRAM samples into snapshots.

    Pure in-memory state: no I/O, no clock, no knowledge of workflows. Callers
    gate which events are fed (e.g. only rows that exist in the view); the
    collector additionally never re-counts a node already in a terminal state,
    so duplicate or out-of-order events cannot corrupt the totals.
    """

    def __init__(self) -> None:
        self._first_start: datetime | None = None
        self._total_tokens = 0
        self._active: set[str] = set()
        self._terminal: set[str] = set()
        self._vram_bytes: int | None = None
        self._vram_peak: int | None = None

    def note_started(self, node_id: str, when: datetime) -> None:
        """A node has started; it joins the active-agents set."""
        if node_id in self._terminal:
            return
        if self._first_start is None:
            self._first_start = when
        self._active.add(node_id)

    def note_completed(self, node_id: str, tokens: int) -> None:
        """A node has completed; its tokens are added to the run total once."""
        if node_id in self._terminal:
            return
        self._terminal.add(node_id)
        self._total_tokens += tokens
        self._active.discard(node_id)

    def note_failed(self, node_id: str) -> None:
        """A node has failed; it leaves the active-agents set."""
        if node_id in self._terminal:
            return
        self._terminal.add(node_id)
        self._active.discard(node_id)

    def note_skipped(self, node_id: str) -> None:
        """A node was skipped; it leaves the active-agents set."""
        if node_id in self._terminal:
            return
        self._terminal.add(node_id)
        self._active.discard(node_id)

    def note_vram_sample(self, used_bytes: int | None) -> None:
        """Record a device-0 usage sample; ``None`` (no sample) is a no-op."""
        if used_bytes is None:
            return
        self._vram_bytes = used_bytes
        if self._vram_peak is None or used_bytes > self._vram_peak:
            self._vram_peak = used_bytes

    def snapshot(self, now: datetime | None = None) -> MetricsSnapshot:
        """Current metrics as a value object (deterministic when *now* is given)."""
        if now is None:
            now = datetime.now(timezone.utc)
        return MetricsSnapshot(
            total_tokens=self._total_tokens,
            active_agents=len(self._active),
            vram_bytes=self._vram_bytes,
            vram_peak_bytes=self._vram_peak,
            elapsed_seconds=self._elapsed(now),
        )

    def _elapsed(self, now: datetime | None) -> float:
        if now is None or self._first_start is None:
            return 0.0
        return max(0.0, (now - self._first_start).total_seconds())


def format_vram(used_bytes: int | None) -> str:
    """Format a byte count as GB (1e9, 2 dp), or ``n/a`` when no sample."""
    if used_bytes is None:
        return "n/a"
    return f"{used_bytes / _GB:.2f} GB"


def format_elapsed(seconds: float) -> str:
    """Format a duration as ``M:SS`` (whole seconds, no hours for run lengths)."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


class MetricsPanel(Widget):
    """Fixed-height panel rendering the latest :class:`MetricsSnapshot`."""

    def __init__(self, snapshot: MetricsSnapshot | None = None) -> None:
        super().__init__(name="metrics-panel")
        self._snapshot = snapshot or MetricsSnapshot(0, 0, None, None, 0.0)

    @property
    def snapshot(self) -> MetricsSnapshot:
        """The snapshot currently displayed."""
        return self._snapshot

    def update(self, snapshot: MetricsSnapshot) -> None:
        """Replace the displayed snapshot and re-render."""
        self._snapshot = snapshot
        self.refresh()

    def render(self) -> Text:
        s = self.snapshot
        return Text(
            "\n".join(
                [
                    f"total tokens: {s.total_tokens}",
                    f"vram: {format_vram(s.vram_bytes)}",
                    f"elapsed: {format_elapsed(s.elapsed_seconds)}",
                    f"active agents: {s.active_agents}",
                ]
            )
        )
