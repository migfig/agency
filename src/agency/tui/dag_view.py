"""Terminal projection of the workflow DAG (Stories 3.3/3.4)."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.text import Text
from textual.widget import Widget

from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import AgentNode, Workflow

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_COMPLETED_FALLBACK = "completed-fallback"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_TERMINAL: frozenset[str] = frozenset(
    {STATUS_DONE, STATUS_COMPLETED_FALLBACK, STATUS_FAILED, STATUS_SKIPPED}
)

# Statuses are monotonic: a terminal state (done/completed-fallback/
# failed/skipped) is absorbing, and a late out-of-order event can never
# regress a row that has already moved on. ``skipped`` is only ever applied
# to rows that are still pending (when an upstream node fails), so it can
# never regress live work either.
_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (STATUS_PENDING, STATUS_RUNNING),
        (STATUS_PENDING, STATUS_DONE),
        (STATUS_PENDING, STATUS_COMPLETED_FALLBACK),
        (STATUS_PENDING, STATUS_FAILED),
        (STATUS_PENDING, STATUS_SKIPPED),
        (STATUS_RUNNING, STATUS_DONE),
        (STATUS_RUNNING, STATUS_COMPLETED_FALLBACK),
        (STATUS_RUNNING, STATUS_FAILED),
    }
)


@dataclass
class NodeRow:
    """Mutable per-node state backing one rendered row."""

    node_id: str
    node_type: str
    model: str
    status: str = STATUS_PENDING
    tokens: int | None = None
    duration_seconds: float | None = None
    started_at: datetime | None = None


class DAGView(Widget):
    """Renders the workflow as topologically-ordered node rows plus an edge list.

    State changes only through :meth:`update_node`, which is idempotent and
    monotonic: events for unknown nodes are ignored with a warning, and a
    finished row (``done``/``failed``) never regresses.
    """

    def __init__(self, workflow: Workflow) -> None:
        super().__init__(name="dag-view")
        self._rows: list[NodeRow] = []
        self._by_id: dict[str, NodeRow] = {}
        topo = DAGBuilder(workflow).topological_sort()
        for node_id in topo:
            node = workflow.nodes[node_id]
            model = node.model if isinstance(node, AgentNode) else None
            row = NodeRow(node_id=node_id, node_type=node.type, model=model or "-")
            self._rows.append(row)
            self._by_id[node_id] = row
        self._edge_labels = [f"{edge.from_id} -> {edge.to_id}" for edge in workflow.edges]
        self._dependents: dict[str, list[str]] = {node_id: [] for node_id in topo}
        for edge in workflow.edges:
            self._dependents.setdefault(edge.from_id, []).append(edge.to_id)

    @property
    def has_running(self) -> bool:
        """True while at least one row is in the running state."""
        return any(row.status == STATUS_RUNNING for row in self._rows)

    @property
    def is_complete(self) -> bool:
        """True when every row is terminal — none left pending or running."""
        return all(row.status in _TERMINAL for row in self._rows)

    def get_row(self, node_id: str) -> NodeRow | None:
        """Return the row state for *node_id*, or None for unknown nodes."""
        return self._by_id.get(node_id)

    def summary_rows(self) -> tuple[NodeRow, ...]:
        """All node rows in topological order, for the end-of-run summary."""
        return tuple(self._rows)

    def mark_skipped_dependents(self, node_id: str) -> None:
        """Transitively mark the pending dependents of *node_id* as skipped.

        A BFS over the edge graph: a pending dependent is moved to the terminal
        ``skipped`` state and the search continues through it. Rows that are
        already running/done/failed are left untouched and not traversed, so
        terminal work is never regressed.
        """
        queue: deque[str] = deque(self._dependents.get(node_id, ()))
        while queue:
            current = queue.popleft()
            row = self._by_id.get(current)
            if row is None:
                continue
            if row.status == STATUS_PENDING:
                row.status = STATUS_SKIPPED
                queue.extend(self._dependents.get(current, ()))
            elif row.status == STATUS_SKIPPED:
                queue.extend(self._dependents.get(current, ()))

    def update_node(
        self,
        node_id: str,
        status: str,
        *,
        tokens: int | None = None,
        duration_seconds: float | None = None,
        started_at: datetime | None = None,
    ) -> None:
        """Apply a node lifecycle transition and repaint.

        Unknown node ids are ignored (with a warning). Rejected transitions
        (status regression) are a no-op. ``tokens``/``duration_seconds`` from
        the event win over wall-clock derivation; the running row's elapsed
        time is measured from ``started_at`` (the event's envelope timestamp,
        i.e. the node's actual start time) to now.
        """
        row = self._by_id.get(node_id)
        if row is None:
            logger.warning("Ignoring %s event for unknown node '%s'", status, node_id)
            return
        if status != row.status:
            if (row.status, status) not in _ALLOWED_TRANSITIONS:
                return
            if status == STATUS_RUNNING:
                row.started_at = started_at or datetime.now(timezone.utc)
            elif status in _TERMINAL and row.started_at is not None:
                row.duration_seconds = (datetime.now(timezone.utc) - row.started_at).total_seconds()
            row.status = status
        if tokens is not None:
            row.tokens = tokens
        if duration_seconds is not None:
            row.duration_seconds = duration_seconds
        self.refresh()

    def render(self) -> Text:
        counts = {
            STATUS_DONE: 0,
            STATUS_RUNNING: 0,
            STATUS_COMPLETED_FALLBACK: 0,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
        }
        for row in self._rows:
            if row.status in counts:
                counts[row.status] += 1
        header = (
            f"nodes: {len(self._rows)}"
            f"   done: {counts[STATUS_DONE]}"
            f"   running: {counts[STATUS_RUNNING]}"
        )
        if counts[STATUS_COMPLETED_FALLBACK] > 0:
            header += f"   completed-fallback: {counts[STATUS_COMPLETED_FALLBACK]}"
        header += (
            f"   failed: {counts[STATUS_FAILED]}"
            f"   skipped: {counts[STATUS_SKIPPED]}"
        )
        lines = [
            header,
            *[self._render_row(row) for row in self._rows],
            "edges: " + (", ".join(self._edge_labels) if self._edge_labels else "-"),
        ]
        return Text("\n".join(lines))

    def _render_row(self, row: NodeRow) -> str:
        parts = [row.node_id, row.node_type, row.model, row.status]
        if row.status == STATUS_RUNNING and row.started_at is not None:
            elapsed = (datetime.now(timezone.utc) - row.started_at).total_seconds()
            parts.append(f"{elapsed:.1f}s")
        if row.status in (STATUS_DONE, STATUS_COMPLETED_FALLBACK):
            if row.tokens is not None:
                parts.append(f"{row.tokens} tok")
            if row.duration_seconds is not None:
                parts.append(f"{row.duration_seconds:.1f}s")
        return "  ".join(parts)
