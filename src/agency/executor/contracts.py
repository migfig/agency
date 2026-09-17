"""Shared runner contract for every node type (Stories 5.2/5.3).

The single source of truth for the ``RunResult`` every runner returns. Runners
perform their side effects and return one of these; the DAG orchestrator is the
sole authority for publishing ``Node*`` lifecycle events and marking skips.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunOutcome = Literal["completed", "failed", "skipped"]


@dataclass(frozen=True)
class RunResult:
    """Outcome of executing one node. Carries everything the orchestrator
    needs to publish lifecycle events; the runner emits none of them itself."""

    node_id: str
    outcome: RunOutcome
    model: str | None = None
    output: str | None = None
    tokens_used: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    stack_trace: str | None = None
    attempt: int = 1
    fallback: str | None = None
    skipped_bindings: tuple[str, ...] = ()
    # Non-agent (5.3) flow-control data. Defaults keep the agent-runner
    # construction sites (agent_runner.py) and its tests working unchanged.
    selected_label: str | None = None
    branch_target: str | None = None
    skipped_targets: tuple[str, ...] = ()
