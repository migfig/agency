"""Textual app shell: live DAG view + metrics panel + end-of-run summary.

Stories 3.3/3.4/5.6. Renders node lifecycle events from the bus, feeds the
metrics collector, and builds the run summary when all nodes are terminal.
Supports demo (simulator) and live orchestrator modes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import App
from textual.binding import Binding

from agency.core.event_bus import EventBus, get_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EVENT_RUN_CANCEL,
    EventEnvelope,
    RunCancelled,
)
from agency.executor.fallback import FallbackSupervisor
from agency.executor.retry import (
    RetrySupervisor,
    get_fallback,
    get_retry_policy,
    is_final_failure,
)
from agency.executor.run_log import RunLogger
from agency.executor.run_state import RunStateStore
from agency.resource_manager.vram_monitor import VRAMMonitor
from agency.tui.dag_view import (
    STATUS_COMPLETED_FALLBACK,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    DAGView,
)
from agency.tui.metrics_panel import MetricsCollector, MetricsPanel
from agency.tui.simulator import (
    simulate_fallback_attempt,
    simulate_node_attempt,
    simulate_run,
)
from agency.tui.summary import RunSummary, append_summary_to_log
from agency.yaml_engine.schema import Workflow

if TYPE_CHECKING:
    from agency.executor.orchestrator import DagOrchestrator

logger = logging.getLogger(__name__)


class AgencyApp(App[None]):
    """Dashboard for a run: DAG rows, metrics panel, then a one-shot summary."""

    TITLE = "Agency"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        workflow: Workflow,
        run_id: str,
        event_bus: EventBus | None = None,
        demo: bool = False,
        log_dir: Path | str = Path("runs"),
        vram_limit: int | None = None,
        vram_monitor: VRAMMonitor | None = None,
        run_logger: RunLogger | None = None,
        run_state: RunStateStore | None = None,
        demo_fail_schedule: list[str] | None = None,
        demo_fallback_fail_schedule: list[str] | None = None,
        orchestrator: DagOrchestrator | None = None,
        execution_environment: str | None = None,
    ) -> None:
        super().__init__()
        self.workflow = workflow
        self.run_id = run_id
        self._bus = event_bus if event_bus is not None else get_event_bus()
        self._demo = demo
        self._demo_fail_schedule = set(demo_fail_schedule or [])
        self._demo_fallback_fail_schedule = set(demo_fallback_fail_schedule or [])
        self.log_dir = Path(log_dir)
        self.vram_monitor = (
            vram_monitor
            if vram_monitor is not None
            else VRAMMonitor(vram_limit_bytes=vram_limit)
        )
        self.run_logger = (
            run_logger
            if run_logger is not None
            else RunLogger(self.log_dir, run_id, workflow, self._bus)
        )
        self.run_state = (
            run_state
            if run_state is not None
            else RunStateStore(
                self.log_dir,
                run_id,
                workflow,
                self._bus,
                vram_limit_bytes=vram_limit,
                execution_environment=execution_environment,
            )
        )
        self.collector = MetricsCollector()
        self.view: DAGView | None = None
        self.panel: MetricsPanel | None = None
        self.summary: RunSummary | None = None
        self._unsubscribers = []
        self._completed = False
        self._retry_supervisor: RetrySupervisor | None = None
        self._fallback_supervisor: FallbackSupervisor | None = None
        self._orchestrator = orchestrator

    async def on_mount(self) -> None:
        self.view = DAGView(self.workflow)
        self.mount(self.view)
        self.panel = MetricsPanel()
        self.mount(self.panel)
        for event_type, handler in (
            (EVENT_NODE_STARTED, self._on_node_started),
            (EVENT_NODE_COMPLETED, self._on_node_completed),
            (EVENT_NODE_FAILED, self._on_node_failed),
            (EVENT_NODE_SKIPPED, self._on_node_skipped),
            (EVENT_RUN_CANCEL, self._on_cancel),
        ):
            self._unsubscribers.append(await self._bus.subscribe(event_type, handler))
        self.set_interval(1.0, self._tick)
        await self.vram_monitor.start()
        await self.run_logger.start()
        await self.run_state.start()

        if self._demo:
            fail_schedule = self._demo_fail_schedule
            self._retry_supervisor = RetrySupervisor(
                self.workflow,
                self.run_id,
                bus=self._bus,
                execute=lambda nid, att: simulate_node_attempt(
                    self.workflow, self._bus, self.run_id, nid, att, fail_schedule
                ),
            )
            await self._retry_supervisor.start()
            fb_fail_schedule = self._demo_fallback_fail_schedule
            self._fallback_supervisor = FallbackSupervisor(
                self.workflow,
                self.run_id,
                bus=self._bus,
                execute=lambda nid, fb, att: simulate_fallback_attempt(
                    self.workflow, self._bus, self.run_id, nid, fb, att,
                    fallback_fail_schedule=fb_fail_schedule,
                ),
            )
            await self._fallback_supervisor.start()
            self.run_worker(
                simulate_run(
                    self.workflow, self._bus, self.run_id,
                    fail_schedule=fail_schedule,
                ),
                group="demo",
                exit_on_error=False,
            )
        elif self._orchestrator is not None:
            self.run_worker(
                self._run_live(),
                group="live",
                exit_on_error=False,
            )

    async def _run_live(self) -> None:
        if self._orchestrator is not None:
            await self._orchestrator.run()

    async def on_unmount(self) -> None:
        for token in self._unsubscribers:
            token.cancel()
        self._unsubscribers.clear()
        if self._retry_supervisor is not None:
            await self._retry_supervisor.close()
        if self._fallback_supervisor is not None:
            await self._fallback_supervisor.close()
        await self.vram_monitor.stop()
        await self.run_logger.close()
        await self.run_state.close()

    def _tick(self) -> None:
        if self.view is not None and self.view.has_running:
            self.view.refresh()
        if self.panel is not None:
            self.collector.note_vram_sample(self.vram_monitor.usage_bytes)
            self.panel.update(self.collector.snapshot())

    async def _on_node_started(self, envelope: EventEnvelope) -> None:
        if self.view is None:
            return
        node_id = envelope.payload.node_id
        self.view.update_node(
            node_id,
            STATUS_RUNNING,
            started_at=envelope.timestamp,
        )
        if self.view.get_row(node_id) is not None:
            self.collector.note_started(node_id, envelope.timestamp)

    async def _on_node_completed(self, envelope: EventEnvelope) -> None:
        if self.view is None:
            return
        payload = envelope.payload
        self.view.update_node(
            payload.node_id,
            STATUS_COMPLETED_FALLBACK if payload.fallback is not None else STATUS_DONE,
            tokens=payload.tokens_used,
            duration_seconds=payload.duration_seconds,
        )
        if self.view.get_row(payload.node_id) is not None:
            self.collector.note_completed(payload.node_id, payload.tokens_used)
        self._maybe_complete()

    async def _on_node_failed(self, envelope: EventEnvelope) -> None:
        if self.view is None:
            return
        payload = envelope.payload
        node_id = payload.node_id
        policy = get_retry_policy(self.workflow, node_id)
        attempt = getattr(payload, "attempt", 1)
        if not is_final_failure(
            policy,
            attempt,
            fallback_configured=get_fallback(self.workflow, node_id) is not None,
            fallback_failed=payload.fallback is not None,
        ):
            return
        self.view.update_node(node_id, STATUS_FAILED)
        if self.view.get_row(node_id) is not None:
            self.collector.note_failed(node_id)
            self.view.mark_skipped_dependents(node_id)
        self._maybe_complete()

    async def _on_node_skipped(self, envelope: EventEnvelope) -> None:
        if self.view is None:
            return
        node_id = envelope.payload.node_id
        self.view.update_node(node_id, STATUS_SKIPPED)
        if self.view.get_row(node_id) is not None:
            self.collector.note_skipped(node_id)
        self._maybe_complete()

    async def _on_cancel(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload
        if isinstance(payload, RunCancelled) and payload.run_id == self.run_id:
            pass

    def action_cancel(self) -> None:
        asyncio.create_task(
            self._bus.publish(
                EventEnvelope.create(
                    EVENT_RUN_CANCEL,
                    self._bus._next_seq(),
                    RunCancelled(run_id=self.run_id),
                )
            )
        )

    def _maybe_complete(self) -> None:
        """Build the one-shot run summary once every node is terminal."""
        if self._completed or self.view is None or not self.view.is_complete:
            return
        self._completed = True
        now = datetime.now(timezone.utc)
        snapshot = self.collector.snapshot(now=now)
        summary = RunSummary(
            run_id=self.run_id,
            completed_at=now,
            total_tokens=snapshot.total_tokens,
            vram_peak_bytes=snapshot.vram_peak_bytes,
            duration_seconds=snapshot.elapsed_seconds,
            nodes=self.view.summary_rows(),
        )
        try:
            append_summary_to_log(self.log_dir, self.run_id, summary)
        except OSError as exc:
            logger.warning("could not write run summary log: %s", exc)
        self.summary = summary
        self.exit()
