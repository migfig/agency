"""DAG executor core (Story 5.4): end-to-end execution of a validated workflow DAG.

``DagOrchestrator`` is the sole publisher of ``Node*`` lifecycle events and the
sole skip-marker: injected runners receive a per-node context and return a
:class:`~agency.executor.contracts.RunResult` without emitting anything, and
the orchestrator translates readiness, execution, and termination into bus
events, per-phase context transitions, and per-node records. It hosts its
collaborators (starting/stopping them) and returns a :class:`DagRunResult`
that the CLI turns into the summary table and exit code.

All collaborators are injected structurally — the orchestrator never
``isinstance``-checks a concrete collaborator class.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from agency.core.event_bus import EventBus, get_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_QUEUED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EVENT_RUN_CANCEL,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeQueued,
    NodeSkipped,
    NodeStarted,
)
from agency.executor.contracts import RunResult
from agency.executor.fallback import FallbackSupervisor
from agency.executor.retry import (
    RetrySupervisor,
    get_fallback,
    get_retry_policy,
    is_final_failure,
)
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import AgentNode, ToolCallNode, Workflow

logger = logging.getLogger(__name__)

# Skip reasons (stable strings, shared with log readers and run state).
SKIP_DEPENDENCY_FAILED = "dependency_failed"
SKIP_BRANCH_NOT_TAKEN = "branch_not_taken"
SKIP_UNRESOLVED_BINDING = "unresolved_binding"

# Per-node lifecycle status; terminal values mirror the run_state/dag_view
# vocabulary so the CLI can render rows without translation.
PENDING = "pending"
RUNNING = "running"
AWAITING_RETRY = "awaiting_retry"
COMPLETED = "completed"
COMPLETED_FALLBACK = "completed-fallback"
FAILED = "failed"
SKIPPED = "skipped"

_TERMINAL = frozenset({COMPLETED, COMPLETED_FALLBACK, FAILED, SKIPPED})

RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class NodeRecord:
    """Terminal per-node outcome row (field order is stable for the CLI)."""

    node_id: str
    status: str
    model: str | None = None
    output: str | None = None
    tokens: int | None = None
    duration_seconds: float | None = None
    attempt: int = 0
    fallback: str | None = None
    reason: str | None = None
    started_at: datetime | None = None


@dataclass(frozen=True)
class DagRunResult:
    """Run-level outcome: aggregate status plus one record per workflow node."""

    run_id: str
    status: str
    duration_seconds: float
    total_tokens: int
    nodes: tuple[NodeRecord, ...]


def _model_of(node: Any) -> str | None:
    return node.model if isinstance(node, AgentNode) else None


class DagOrchestrator:
    """Drive a workflow DAG to completion over one event bus.

    The orchestrator owns node scheduling: a node starts only after every edge
    into it has reached a ready-terminal state (completed, skipped, or final
    failure). Independent branches execute concurrently as asyncio tasks.
    """

    def __init__(
        self,
        workflow: Workflow,
        run_id: str,
        *,
        agent_runner: Any,
        non_agent_runner: Any,
        context_store: Any,
        run_state: Any,
        run_logger: Any,
        load_manager: Any,
        bus: EventBus | None = None,
    ) -> None:
        self._workflow = workflow
        self._run_id = run_id
        self._nodes = workflow.nodes
        self._agent_runner = agent_runner
        self._non_agent_runner = non_agent_runner
        self._context_store = context_store
        self._run_state = run_state
        self._run_logger = run_logger
        self._load_manager = load_manager
        self._bus = bus if bus is not None else get_event_bus()

        self._dependents: dict[str, list[str]] = DAGBuilder(workflow).build_dag()
        self._upstreams: dict[str, list[str]] = {nid: [] for nid in self._nodes}
        for edge in workflow.edges:
            self._upstreams[edge.to_id].append(edge.from_id)
        self._remaining: dict[str, int] = {nid: 0 for nid in self._nodes}
        for deps in self._dependents.values():
            for dep in deps:
                self._remaining[dep] += 1

        fb_refs: set[str] = set()
        for node in self._nodes.values():
            if isinstance(node, (AgentNode, ToolCallNode)) and node.fallback is not None:
                fb_refs.add(node.fallback)
        self._standby: set[str] = {
            ref
            for ref in fb_refs
            if ref in self._nodes
            and not self._upstreams[ref]
            and not self._dependents.get(ref)
            and ref != self._workflow.entry_point
        }

        self._phase_of: dict[str, str] = {}
        self._phase_names: dict[str, str] = {}
        self._phase_nodes: dict[str, list[str]] = {}
        for phase in workflow.phases:
            self._phase_names[phase.id] = phase.name
            self._phase_nodes[phase.id] = list(phase.node_ids)
            for nid in phase.node_ids:
                self._phase_of[nid] = phase.id
        self._phase_open: set[str] = set()
        self._phase_closed: set[str] = set()

        self._status: dict[str, str] = {nid: PENDING for nid in self._nodes}
        self._records: dict[str, NodeRecord] = {
            nid: NodeRecord(node_id=nid, status=PENDING, model=_model_of(node))
            for nid, node in self._nodes.items()
        }
        self._outputs: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._failure_skips: list[str] = []
        self._done = asyncio.Event()
        self._started_monotonic: float | None = None
        self._logged_first_start = False
        self._cancelled = False

    # -- public API -------------------------------------------------------

    async def run(
        self,
        *,
        seed_nodes: dict[str, str] | None = None,
    ) -> DagRunResult:
        """Execute the whole DAG and return the run-level result.

        Hosts the collaborators (load manager, run state, run logger),
        schedules every node, and always closes what it started before
        returning — success or exception.

        When *seed_nodes* is provided (replay tail execution), those nodes
        are treated as already-completed: their outputs injected, remaining
        counters decremented, and only the un-seeded suffix executes live.
        """
        started = time.monotonic()
        self._started_monotonic = started
        closers: list = []
        try:
            await self._load_manager.start()
            closers.append(self._load_manager.stop)
            await self._run_state.start()
            closers.append(self._run_state.close)
            await self._run_logger.start()
            closers.append(self._run_logger.close)
            cancel_token = await self._bus.subscribe(
                EVENT_RUN_CANCEL, self._on_cancel
            )

            async def _cancel() -> None:
                cancel_token.cancel()

            closers.append(_cancel)
            retry_supervisor = RetrySupervisor(
                self._workflow, self._run_id, bus=self._bus, execute=self._retry_execute
            )
            await retry_supervisor.start()
            closers.append(retry_supervisor.close)
            fallback_supervisor = FallbackSupervisor(
                self._workflow, self._run_id, bus=self._bus, execute=self._fallback_execute
            )
            await fallback_supervisor.start()
            closers.append(fallback_supervisor.close)
            if seed_nodes:
                self._seed_completed(seed_nodes)
            self._launch_ready()
            if self._all_terminal():
                self._done.set()
            await self._done.wait()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # Drain: wait for any pending retry/fallback callbacks to fire and complete.
            while any(self._status[nid] == AWAITING_RETRY for nid in self._nodes):
                await asyncio.sleep(0)
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
        finally:
            for close in reversed(closers):
                try:
                    await close()
                except Exception:
                    logger.warning("collaborator shutdown failed", exc_info=True)
        duration = time.monotonic() - started
        nodes = tuple(self._records[nid] for nid in self._nodes)
        total_tokens = sum(r.tokens or 0 for r in nodes)
        status = (
            RUN_STATUS_FAILED
            if any(r.status == FAILED for r in nodes)
            else RUN_STATUS_COMPLETED
        )
        return DagRunResult(
            run_id=self._run_id,
            status=status,
            duration_seconds=duration,
            total_tokens=total_tokens,
            nodes=nodes,
        )

    # -- scheduling -------------------------------------------------------

    def _launch_ready(self) -> None:
        for nid in self._nodes:
            if (
                self._remaining[nid] == 0
                and self._status[nid] == PENDING
                and nid not in self._standby
                and not self._cancelled
            ):
                self._launch(nid)

    def _launch(self, node_id: str) -> None:
        task = asyncio.create_task(
            self._drive(node_id), name=f"drive-{self._run_id}-{node_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- supervisor re-entry seam ------------------------------------------

    async def _retry_execute(self, node_id: str, attempt: int) -> None:
        await self.execute_node(node_id, attempt=attempt)

    async def _fallback_execute(
        self, node_id: str, fallback: str, attempt: int
    ) -> None:
        await self.execute_node(node_id, attempt=attempt, fallback=fallback)

    async def execute_node(
        self,
        node_id: str,
        attempt: int = 1,
        fallback: str | None = None,
    ) -> None:
        """Re-entry seam for retry and fallback supervisors.

        A terminal-state guard makes late-arriving supervisor callbacks safe
        no-ops (the node may have already been resolved by another path).
        Crashes are delegated to ``_resolve_failed`` so that a crashing
        fallback attempt is correctly recognised as terminal.
        """
        if node_id not in self._nodes:
            raise ValueError(f"unknown node '{node_id}'")
        if self._status.get(node_id) in _TERMINAL:
            logger.debug(
                "execute_node('%s', attempt=%d, fallback=%r) ignored: already %s",
                node_id,
                attempt,
                fallback,
                self._status[node_id],
            )
            return
        try:
            await self._execute(node_id, attempt=attempt, fallback=fallback)
        except Exception as exc:
            logger.exception("node '%s' drive failed unexpectedly", node_id)
            crash = RunResult(
                node_id=node_id,
                outcome="failed",
                error=f"internal error: {exc}",
                stack_trace=traceback.format_exc(),
                attempt=attempt,
                fallback=fallback,
            )
            try:
                await self._resolve_failed(node_id, self._nodes[node_id], crash)
            except Exception:
                logger.exception(
                    "node '%s' crash resolution failed; forcing terminal", node_id
                )
                self._status[node_id] = FAILED
                self._update_record(node_id, status=FAILED, reason="internal_error")
                self._mark_dependents_failed(node_id)
                self._commit_terminal(node_id)

    async def _drive(self, node_id: str) -> None:
        if self._status[node_id] != PENDING:
            return
        await self.execute_node(node_id)

    # -- execution --------------------------------------------------------

    async def _execute(
        self, node_id: str, attempt: int, fallback: str | None = None
    ) -> None:
        node = self._nodes[node_id]
        fallback_node = self._fallback_node(fallback)
        started_model = fallback_node.model if fallback_node is not None else _model_of(node)
        self._status[node_id] = RUNNING
        self._update_record(node_id, status=RUNNING)
        if attempt == 1 and fallback is None:
            await self._emit(
                EVENT_NODE_QUEUED, NodeQueued(node_id=node_id, run_id=self._run_id)
            )
        phase_id = self._phase_of.get(node_id)
        if phase_id is not None and phase_id not in self._phase_open:
            self._phase_open.add(phase_id)
            self._context_store.start_phase(phase_id, self._phase_names[phase_id])
            await self._context_store.flush_events()
        await self._emit(
            EVENT_NODE_STARTED,
            NodeStarted(
                node_id=node_id,
                run_id=self._run_id,
                model=started_model,
                attempt=attempt,
            ),
        )
        if not self._logged_first_start and self._started_monotonic is not None:
            self._logged_first_start = True
            logger.info(
                "time to first NodeStarted: %.3fs",
                time.monotonic() - self._started_monotonic,
            )
        started_at = datetime.now(timezone.utc)
        self._update_record(node_id, started_at=started_at)
        context = {f"nodes.{nid}.output": v for nid, v in self._outputs.items()}
        result = await self._dispatch(
            node, context, phase_id, attempt, node_id, fallback_node=fallback_node
        )
        await self._resolve(node_id, node, result)

    async def _dispatch(
        self,
        node: Any,
        context: dict[str, str],
        phase_id: str | None,
        attempt: int,
        node_id: str,
        fallback_node: AgentNode | None = None,
    ) -> RunResult:
        if fallback_node is not None:
            return await self._agent_runner.run(
                fallback_node,
                context,
                phase_id=phase_id,
                attempt=attempt,
                fallback=fallback_node.id,
                node_id=node_id,
            )
        if isinstance(node, AgentNode):
            return await self._agent_runner.run(
                node, context, phase_id=phase_id, attempt=attempt, fallback=None
            )
        return await self._non_agent_runner.run(
            node,
            context,
            phase_id=phase_id,
            deps=tuple(self._upstreams.get(node_id, ())),
        )

    def _fallback_node(self, fallback: str | None) -> AgentNode | None:
        if fallback is None:
            return None
        node = self._nodes.get(fallback)
        return node if isinstance(node, AgentNode) else None

    # -- outcome resolution (sole event publisher) -------------------------

    async def _resolve(self, node_id: str, node: Any, result: RunResult) -> None:
        if result.outcome == "completed":
            await self._resolve_completed(node_id, node, result)
        elif result.outcome == "failed":
            await self._resolve_failed(node_id, node, result)
        else:
            await self._resolve_skipped(node_id, node, result)

    async def _resolve_completed(
        self, node_id: str, node: Any, result: RunResult
    ) -> None:
        if result.output is not None:
            self._outputs[node_id] = result.output
        status = COMPLETED_FALLBACK if result.fallback is not None else COMPLETED
        self._status[node_id] = status
        self._update_record(
            node_id,
            status=status,
            model=result.model if result.model is not None else _model_of(node),
            output=result.output,
            tokens=result.tokens_used or None,
            duration_seconds=result.duration_seconds or None,
            attempt=result.attempt,
            fallback=result.fallback,
        )
        await self._emit(EVENT_NODE_COMPLETED, self._completed_event(node_id, result))
        skipped_now: list[str] = []
        for target in result.skipped_targets:
            if target in self._nodes and self._status[target] == PENDING:
                self._mark_skip_sync(target, SKIP_BRANCH_NOT_TAKEN)
                skipped_now.append(target)
        for target in skipped_now:
            await self._emit(
                EVENT_NODE_SKIPPED,
                NodeSkipped(
                    node_id=target,
                    run_id=self._run_id,
                    reason=SKIP_BRANCH_NOT_TAKEN,
                ),
            )
        await self._maybe_close_phase(node_id)
        for target in skipped_now:
            await self._maybe_close_phase(target)
        self._commit_terminal(node_id)
        for target in skipped_now:
            self._commit_terminal(target)

    async def _resolve_failed(
        self, node_id: str, node: Any, result: RunResult
    ) -> None:
        policy = get_retry_policy(self._workflow, node_id)
        final = is_final_failure(
            policy,
            result.attempt,
            fallback_configured=(get_fallback(self._workflow, node_id) is not None),
            fallback_failed=result.fallback is not None,
        )
        if final:
            self._status[node_id] = FAILED
            self._failure_skips = self._mark_dependents_failed(node_id)
        else:
            self._status[node_id] = AWAITING_RETRY
        self._update_record(
            node_id,
            status=self._status[node_id],
            model=result.model if result.model is not None else _model_of(node),
            tokens=result.tokens_used or None,
            duration_seconds=result.duration_seconds or None,
            attempt=result.attempt,
            fallback=result.fallback,
        )
        await self._emit(EVENT_NODE_FAILED, self._failed_event(node_id, result))
        if not final:
            logger.warning(
                "node '%s' failed attempt %d (non-final); waiting for supervisor",
                node_id,
                result.attempt,
            )
            return
        for target in self._failure_skips:
            await self._emit(
                EVENT_NODE_SKIPPED,
                NodeSkipped(
                    node_id=target,
                    run_id=self._run_id,
                    reason=SKIP_DEPENDENCY_FAILED,
                ),
            )
        await self._maybe_close_phase(node_id)
        for target in self._failure_skips:
            await self._maybe_close_phase(target)
        self._commit_terminal(node_id)
        for target in self._failure_skips:
            self._commit_terminal(target)
        self._failure_skips = []

    async def _resolve_skipped(
        self, node_id: str, node: Any, result: RunResult
    ) -> None:
        self._status[node_id] = SKIPPED
        self._update_record(
            node_id,
            status=SKIPPED,
            reason=SKIP_UNRESOLVED_BINDING,
            attempt=result.attempt,
        )
        logger.warning(
            "node '%s' skipped: unresolved bindings %s",
            node_id,
            list(result.skipped_bindings),
        )
        await self._emit(
            EVENT_NODE_SKIPPED,
            NodeSkipped(
                node_id=node_id,
                run_id=self._run_id,
                reason=SKIP_UNRESOLVED_BINDING,
                skipped_bindings=result.skipped_bindings,
            ),
        )
        await self._maybe_close_phase(node_id)
        self._commit_terminal(node_id)

    # -- payload builders ---------------------------------------------------

    def _completed_event(self, node_id: str, result: RunResult) -> NodeCompleted:
        return NodeCompleted(
            node_id=node_id,
            run_id=self._run_id,
            model=result.model,
            tokens_used=result.tokens_used,
            duration_seconds=result.duration_seconds,
            attempt=result.attempt,
            output=result.output,
            fallback=result.fallback,
        )

    def _failed_event(self, node_id: str, result: RunResult) -> NodeFailed:
        return NodeFailed(
            node_id=node_id,
            run_id=self._run_id,
            model=result.model,
            error=result.error or "",
            stack_trace=result.stack_trace,
            attempt=result.attempt,
            fallback=result.fallback,
        )

    # -- terminal-commit helpers -------------------------------------------

    def _mark_skip_sync(self, node_id: str, reason: str) -> None:
        self._status[node_id] = SKIPPED
        self._update_record(node_id, status=SKIPPED, reason=reason)

    def _mark_dependents_failed(self, node_id: str) -> list[str]:
        """Transitively mark every reachable still-pending dependent skipped.

        Returns the skip list in BFS order; unrelated running branches are
        untouched.
        """
        skips: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque(self._dependents.get(node_id, ()))
        while queue:
            dep = queue.popleft()
            if dep in seen:
                continue
            seen.add(dep)
            status = self._status.get(dep)
            if status == PENDING:
                self._mark_skip_sync(dep, SKIP_DEPENDENCY_FAILED)
                skips.append(dep)
                queue.extend(self._dependents.get(dep, ()))
            elif status == SKIPPED:
                queue.extend(self._dependents.get(dep, ()))
        return skips

    async def _maybe_close_phase(self, node_id: str) -> None:
        phase_id = self._phase_of.get(node_id)
        if phase_id is None or phase_id in self._phase_closed:
            return
        if all(
            self._status[nid] in _TERMINAL or nid in self._standby
            for nid in self._phase_nodes[phase_id]
        ):
            self._phase_closed.add(phase_id)
            self._context_store.complete_phase(phase_id)
            await self._context_store.flush_events()

    def _commit_terminal(self, node_id: str) -> None:
        self._advance_dependents(node_id)
        if self._all_terminal():
            self._done.set()

    def _advance_dependents(self, node_id: str) -> None:
        for dep in self._dependents.get(node_id, ()):
            if self._status[dep] != PENDING:
                continue
            self._remaining[dep] -= 1
            if self._remaining[dep] == 0 and not self._cancelled:
                self._launch(dep)

    def _on_cancel(self, envelope: EventEnvelope) -> None:
        """Handle run cancellation: mark remaining PENDING nodes as SKIPPED."""
        if getattr(envelope.payload, "run_id", None) != self._run_id:
            return
        self._cancelled = True
        for nid in self._nodes:
            if self._status[nid] == PENDING:
                self._mark_skip_sync(nid, "cancelled")
                self._commit_terminal(nid)

    def _seed_completed(self, seed_nodes: dict[str, str]) -> None:
        """Mark pre-restored nodes as completed for replay tail execution."""
        for nid, output in seed_nodes.items():
            if nid not in self._nodes:
                continue
            node = self._nodes[nid]
            self._status[nid] = COMPLETED
            self._outputs[nid] = output
            self._update_record(
                nid,
                status=COMPLETED,
                model=_model_of(node),
                output=output,
            )
            for dep in self._dependents.get(nid, ()):
                if self._status[dep] == PENDING:
                    self._remaining[dep] -= 1

    def _all_terminal(self) -> bool:
        return all(
            status in _TERMINAL or nid in self._standby
            for nid, status in self._status.items()
        )

    def _update_record(self, node_id: str, **fields: Any) -> None:
        self._records[node_id] = replace(self._records[node_id], **fields)

    async def _emit(self, event_type: str, payload: Any) -> EventEnvelope:
        envelope = EventEnvelope.create(event_type, self._bus._next_seq(), payload)
        await self._bus.publish(envelope)
        return envelope
