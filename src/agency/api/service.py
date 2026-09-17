"""RunService: start/status/result/input for the HTTP API (US1)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agency.api.config import ServiceConfig
from agency.api.registry import (
    RunHandle,
    RunRegistry,
    _state_to_view,
    api_input_source,
    derive_state,
    node_states_from_run_dir,
    run_dir_metadata,
)
from agency.api.schemas import (
    CODE_INPUT_NOT_AWAITING,
    CODE_INVALID_REQUEST,
    CODE_INVALID_WORKFLOW,
    CODE_MODEL_VALIDATION_FAILED,
    CODE_NO_REPLAY_SOURCE,
    CODE_RUN_NOT_FINISHED,
    CODE_SANDBOX_UNAVAILABLE,
    CODE_UNKNOWN_NODE,
    CODE_UNKNOWN_RUN,
    CODE_UNKNOWN_WORKFLOW,
    AgencyAPIError,
    HealthResponse,
    NodeResult,
    NodeStateView,
    PendingInputView,
    ReplayAccepted,
    ReplaySource,
    RunAccepted,
    RunHistoryEntry,
    RunResultView,
    RunStatusView,
    SandboxRequest,
)
from agency.core.event_bus import get_event_bus
from agency.executor.agent_runner import AgentRunner
from agency.executor.context_store import ContextStore
from agency.executor.execution_env import LocalToolBackend, resolve_environment
from agency.executor.node_runners import NonAgentRunner
from agency.executor.orchestrator import (
    RUN_STATUS_FAILED,
    DagOrchestrator,
    NodeRecord,
)
from agency.executor.replay import (
    ReplayError,
    find_source_run,
    get_folder_id,
    replay_checkpoint,
    restore_set_for,
)
from agency.executor.run_log import RunLogger
from agency.executor.run_state import RunStateStore
from agency.executor.summarizer import ContextSummarizer
from agency.executor.tool_registry import build_workflow_tool_registry
from agency.resource_manager import BackendRouter, RoutingError
from agency.resource_manager.model_load_manager import ModelLoadManager
from agency.resource_manager.provisioning import ProvisionError, provision, teardown
from agency.resource_manager.sandbox import (
    build_sandbox_policy,
    default_image_root,
    provision_sandbox,
)
from agency.resource_manager.vram_tracker import resolve_vram_limit
from agency.yaml_engine.parser import (
    SchemaValidationError,
    YAMLParseError,
    load_workflow,
)
from agency.yaml_engine.schema import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_SUMMARIZE_THRESHOLD,
    ModelSpec,
    Workflow,
)

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATES = frozenset({"completed", "failed"})
_VALID_ENVIRONMENTS = frozenset({"sandbox", "local"})


def _json_status(status: str) -> str:
    """Map executor statuses onto the JSON vocabulary (unhyphenated)."""
    return status.replace("-", "_")


def allocate_run_id(log_dir: Path | str) -> str:
    """Allocate a fresh run folder id, suffixing on collision (research R6)."""
    log_dir = Path(log_dir)
    base = get_folder_id()
    candidate = base
    while (log_dir / candidate).exists():
        candidate = f"{base}_{secrets.token_hex(2)}"
    return candidate


def _view_from_record(record: NodeRecord) -> NodeStateView:
    return NodeStateView(
        status=_json_status(record.status),
        model=record.model,
        tokens=record.tokens,
        duration_seconds=record.duration_seconds,
        attempt=record.attempt,
        fallback=record.fallback,
        reason=record.reason,
    )


def _result_from_record(record: NodeRecord) -> NodeResult:
    return NodeResult(
        node_id=record.node_id,
        status=_json_status(record.status),
        model=record.model,
        tokens=record.tokens,
        duration_seconds=record.duration_seconds,
        attempt=record.attempt,
        fallback=record.fallback,
        reason=record.reason,
        output=record.output,
    )


def status_view_from_run_dir(log_dir: Path | str, run_id: str) -> RunStatusView | None:
    """Reconstruct a status view for a run not owned by this process."""
    metadata = run_dir_metadata(log_dir, run_id)
    if metadata is None:
        return None
    states = {
        node_id: _state_to_view(node_id, state)
        for node_id, state in node_states_from_run_dir(Path(log_dir) / run_id).items()
    }
    replay = metadata.get("replay_source")
    return RunStatusView(
        run_id=run_id,
        workflow_name=metadata.get("workflow_name", ""),
        state=derive_state(states),
        started_at=metadata.get("started_at", ""),
        replay_source=(
            ReplaySource(
                source_run_id=replay.get("source_run_id"),
                checkpoint=replay.get("checkpoint_node"),
            )
            if isinstance(replay, dict)
            else None
        ),
        nodes=states,
        pending_inputs=[],
    )


def result_view_from_run_dir(log_dir: Path | str, run_id: str) -> RunResultView | None:
    """Reconstruct a result view for a terminal run not owned by this process.

    Returns ``None`` when the run dir has no metadata or the derived state is
    not terminal.
    """
    metadata = run_dir_metadata(log_dir, run_id)
    if metadata is None:
        return None
    run_dir = Path(log_dir) / run_id
    raw_states = node_states_from_run_dir(run_dir)
    states = {
        node_id: _state_to_view(node_id, state)
        for node_id, state in raw_states.items()
    }
    state = derive_state(states)
    if state not in _TERMINAL_RUN_STATES:
        return None
    outputs_dir = run_dir / "outputs"
    nodes = [
        NodeResult(
            node_id=node_id,
            status=view.status,
            model=view.model,
            tokens=view.tokens,
            duration_seconds=view.duration_seconds,
            attempt=view.attempt,
            fallback=view.fallback,
            reason=view.reason,
            output=(outputs_dir / f"{node_id}.txt").read_text(encoding="utf-8")
            if (outputs_dir / f"{node_id}.txt").is_file()
            else None,
        )
        for node_id, view in sorted(states.items())
    ]
    return RunResultView(
        run_id=run_id,
        status=state,
        total_tokens=sum(view.tokens or 0 for view in states.values()),
        duration_seconds=sum(view.duration_seconds or 0.0 for view in states.values()),
        nodes=nodes,
    )


class RunService:
    """Owns the run lifecycle for the HTTP API: start, status, result, input."""

    def __init__(
        self,
        config: ServiceConfig,
        registry: RunRegistry | None = None,
        *,
        provision: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._registry = registry if registry is not None else RunRegistry()
        self._provision = provision

    @property
    def log_dir(self) -> Path:
        return Path(self._config.log_dir)

    @property
    def registry(self) -> RunRegistry:
        return self._registry

    # -- start ------------------------------------------------------------

    async def start_run(
        self,
        workflow_path: str,
        vram_limit: str | None = None,
        models: dict[str, ModelSpec] | None = None,
        environment: str | None = None,
        sandbox: SandboxRequest | dict[str, Any] | None = None,
    ) -> RunAccepted:
        """Validate, provision, allocate a run id, register a handle, and spawn
        the run task. Raises structured :class:`AgencyAPIError` on gate failure."""
        path = Path(workflow_path)
        if not path.is_file():
            raise AgencyAPIError(
                CODE_UNKNOWN_WORKFLOW,
                404,
                f"workflow file not found: {workflow_path}",
                {"workflow": workflow_path},
            )
        try:
            workflow = load_workflow(path)
        except YAMLParseError as exc:
            raise AgencyAPIError(
                CODE_INVALID_WORKFLOW,
                422,
                f"workflow file failed validation: {workflow_path}",
                {"workflow": workflow_path, "problems": [str(exc)]},
            ) from exc
        except SchemaValidationError as exc:
            raise AgencyAPIError(
                CODE_INVALID_WORKFLOW,
                422,
                f"workflow file failed validation: {workflow_path}",
                {"workflow": workflow_path, "problems": list(exc.errors)},
            ) from exc

        try:
            vram = resolve_vram_limit(
                cli_value=vram_limit, yaml_value=workflow.vram_limit
            ).limit_bytes
        except ValueError as exc:
            raise AgencyAPIError(
                CODE_INVALID_REQUEST,
                422,
                f"invalid vram_limit: {exc}",
                {"problems": [{"field": "vram_limit", "message": str(exc)}]},
            ) from exc

        self._check_environment(environment)

        router = BackendRouter()
        provision_fn = self._provision if self._provision is not None else provision
        try:
            prov_run = await provision_fn(workflow, models, router)
        except ProvisionError as exc:
            raise AgencyAPIError(
                CODE_MODEL_VALIDATION_FAILED,
                409,
                "model validation failed",
                {"failures": list(exc.failures)},
            ) from exc

        run_id = allocate_run_id(self.log_dir)
        backend, resolved_env = await self._resolve_backend(
            workflow, environment, sandbox, run_id, prov_run
        )
        handle = RunHandle(
            run_id=run_id,
            workflow_name=workflow.name,
            started_at=datetime.now(timezone.utc).isoformat(),
            workflow=workflow,
            run_dir=self.log_dir / run_id,
            nodes={nid: NodeStateView(status="pending") for nid in workflow.nodes},
            workflow_path=workflow_path,
        )
        self._registry.register(handle)
        await self._registry.subscribe_mirror(handle)
        handle.task = asyncio.create_task(
            self._run_task(handle, workflow, vram, prov_run, backend, resolved_env),
            name=f"agency-run-{run_id}",
        )
        return RunAccepted(run_id=run_id, state="running", workflow=workflow.name)

    # -- replay -----------------------------------------------------------

    async def replay_run(
        self,
        workflow_path: str,
        checkpoint: str,
        source_run: str | None = None,
        vram_limit: str | None = None,
        models: dict[str, ModelSpec] | None = None,
        environment: str | None = None,
        sandbox: SandboxRequest | dict[str, Any] | None = None,
    ) -> ReplayAccepted:
        """Validate, provision, locate a source run, register a replay handle, and
        spawn the replay task. Raises structured :class:`AgencyAPIError` on gate
        failure; no run dir is created when a gate fails."""
        path = Path(workflow_path)
        if not path.is_file():
            raise AgencyAPIError(
                CODE_UNKNOWN_WORKFLOW,
                404,
                f"workflow file not found: {workflow_path}",
                {"workflow": workflow_path},
            )
        try:
            workflow = load_workflow(path)
        except YAMLParseError as exc:
            raise AgencyAPIError(
                CODE_INVALID_WORKFLOW,
                422,
                f"workflow file failed validation: {workflow_path}",
                {"workflow": workflow_path, "problems": [str(exc)]},
            ) from exc
        except SchemaValidationError as exc:
            raise AgencyAPIError(
                CODE_INVALID_WORKFLOW,
                422,
                f"workflow file failed validation: {workflow_path}",
                {"workflow": workflow_path, "problems": list(exc.errors)},
            ) from exc

        try:
            vram = resolve_vram_limit(
                cli_value=vram_limit, yaml_value=workflow.vram_limit
            ).limit_bytes
        except ValueError as exc:
            raise AgencyAPIError(
                CODE_INVALID_REQUEST,
                422,
                f"invalid vram_limit: {exc}",
                {"problems": [{"field": "vram_limit", "message": str(exc)}]},
            ) from exc

        self._check_environment(environment)

        router = BackendRouter()
        provision_fn = self._provision if self._provision is not None else provision
        try:
            prov_run = await provision_fn(workflow, models, router)
        except ProvisionError as exc:
            raise AgencyAPIError(
                CODE_MODEL_VALIDATION_FAILED,
                409,
                "model validation failed",
                {"failures": list(exc.failures)},
            ) from exc

        try:
            restore_set = restore_set_for(workflow, checkpoint)
            source_run_id = find_source_run(
                self.log_dir, source_run, workflow.name, checkpoint
            )
        except ReplayError as exc:
            raise AgencyAPIError(
                CODE_NO_REPLAY_SOURCE,
                404,
                f"no eligible source run for node '{checkpoint}': {exc}",
                {"checkpoint": checkpoint, "source_run": source_run},
            ) from exc

        run_id = allocate_run_id(self.log_dir)
        backend, resolved_env = await self._resolve_backend(
            workflow, environment, sandbox, run_id, prov_run
        )
        handle = RunHandle(
            run_id=run_id,
            workflow_name=workflow.name,
            started_at=datetime.now(timezone.utc).isoformat(),
            workflow=workflow,
            run_dir=self.log_dir / run_id,
            nodes={nid: NodeStateView(status="pending") for nid in workflow.nodes},
            replay_source=ReplaySource(
                source_run_id=source_run_id, checkpoint=checkpoint
            ),
            workflow_path=workflow_path,
        )
        for nid in restore_set:
            handle.nodes[nid] = NodeStateView(status="completed")
        self._registry.register(handle)
        await self._registry.subscribe_mirror(handle)
        handle.task = asyncio.create_task(
            self._replay_task(
                handle,
                workflow,
                vram,
                prov_run,
                checkpoint,
                source_run_id,
                backend,
                resolved_env,
            ),
            name=f"agency-replay-{run_id}",
        )
        return ReplayAccepted(
            run_id=run_id,
            state="running",
            source_run=source_run_id,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _check_environment(environment: str | None) -> None:
        if environment is not None and environment not in _VALID_ENVIRONMENTS:
            raise AgencyAPIError(
                CODE_INVALID_REQUEST,
                422,
                f"invalid environment: {environment!r} (expected 'sandbox' or 'local')",
                {"problems": [{"field": "environment", "message": str(environment)}]},
            )

    async def _resolve_backend(
        self,
        workflow: Workflow,
        environment: str | None,
        sandbox: SandboxRequest | dict[str, Any] | None,
        run_id: str,
        prov_run: Any,
    ) -> tuple[Any, str]:
        """Resolve the execution environment (contract §4) and return
        ``(backend, resolved)``.

        ``sandbox`` provisions the Docker gate (tearing down *prov_run* and
        raising :class:`AgencyAPIError` ``sandbox_unavailable`` on failure);
        ``local`` — the only other possible value — returns the process-local
        backend without touching the gate. ``resolved`` is the environment that
        was actually applied (for run-record observability)."""
        caps = (
            SandboxRequest.model_validate(sandbox)
            if isinstance(sandbox, dict)
            else sandbox
        )
        resolved = resolve_environment(environment, workflow.execution_environment)
        if resolved != "sandbox":
            return LocalToolBackend(), resolved
        policy = build_sandbox_policy(
            workflow,
            cpu=caps.cpu if caps is not None else None,
            memory=caps.memory if caps is not None else None,
            pids_limit=caps.pids_limit if caps is not None else None,
            timeout_seconds=caps.timeout_seconds if caps is not None else None,
            output_limit_bytes=caps.output_limit_bytes if caps is not None else None,
            scratch_size=caps.scratch_size if caps is not None else None,
        )
        try:
            return (
                await provision_sandbox(
                    policy, default_image_root(), run_id, run_root=self.log_dir
                ),
                resolved,
            )
        except ProvisionError as exc:
            teardown(prov_run)
            raise AgencyAPIError(
                CODE_SANDBOX_UNAVAILABLE,
                409,
                "sandbox unavailable",
                {"failures": list(exc.failures)},
            ) from exc

    async def _run_task(
        self,
        handle: RunHandle,
        workflow: Workflow,
        vram_limit: int | None,
        prov_run: Any,
        backend: Any,
        execution_environment: str | None = None,
    ) -> None:
        """Execute the DAG with the same wiring as the CLI headless path."""
        bus = get_event_bus()
        context_store = ContextStore(handle.run_id, self.log_dir, event_bus=bus)
        context_summarizer = None
        if workflow.summarizer_model is not None:
            context_summarizer = ContextSummarizer(
                run_id=handle.run_id,
                store=context_store,
                router=prov_run.router,
                model=workflow.summarizer_model,
                context_window=workflow.context_window or DEFAULT_CONTEXT_WINDOW,
                threshold_percent=(
                    workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
                ),
            )
        load_manager = ModelLoadManager(vram_limit_bytes=vram_limit)
        tool_registry = build_workflow_tool_registry(workflow, execution_backend=backend)
        agent_runner = AgentRunner(
            run_id=handle.run_id,
            router=prov_run.router,
            load_manager=load_manager,
            vram_sizes=prov_run.vram_sizes,
            model_timeouts=prov_run.model_timeouts,
            context_store=context_store,
            summarizer=context_summarizer,
            routing_error=RoutingError,
            tool_registry=tool_registry,
        )
        non_agent_runner = NonAgentRunner(
            context_store=context_store,
            input_source=api_input_source(handle.run_id, self._registry),
            tool_registry=tool_registry,
        )
        orchestrator = DagOrchestrator(
            workflow,
            handle.run_id,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            run_state=RunStateStore(
                self.log_dir,
                handle.run_id,
                workflow,
                bus,
                vram_limit_bytes=vram_limit,
                workflow_path=handle.workflow_path,
                execution_environment=execution_environment,
            ),
            run_logger=RunLogger(self.log_dir, handle.run_id, workflow, bus),
            load_manager=load_manager,
        )
        result: Any = None
        try:
            result = await orchestrator.run()
        except Exception:
            logger.exception("run %s crashed", handle.run_id)
        finally:
            teardown(prov_run)
            for token in handle.mirror_tokens:
                token.cancel()
        if result is None:
            handle.state = "failed"
            return
        handle.result = result
        handle.state = "failed" if result.status == RUN_STATUS_FAILED else "completed"
        for record in result.nodes:
            handle.nodes[record.node_id] = _view_from_record(record)

    async def _replay_task(
        self,
        handle: RunHandle,
        workflow: Workflow,
        vram_limit: int | None,
        prov_run: Any,
        checkpoint: str,
        source_run_id: str,
        backend: Any,
        execution_environment: str | None = None,
    ) -> None:
        """Restore the checkpoint prefix from the source run and tail-execute live,
        into the pre-generated run id."""
        bus = get_event_bus()
        context_store = ContextStore(handle.run_id, self.log_dir, event_bus=bus)
        context_summarizer = None
        if workflow.summarizer_model is not None:
            context_summarizer = ContextSummarizer(
                run_id=handle.run_id,
                store=context_store,
                router=prov_run.router,
                model=workflow.summarizer_model,
                context_window=workflow.context_window or DEFAULT_CONTEXT_WINDOW,
                threshold_percent=(
                    workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
                ),
            )
        load_manager = ModelLoadManager(vram_limit_bytes=vram_limit)
        tool_registry = build_workflow_tool_registry(workflow, execution_backend=backend)
        agent_runner = AgentRunner(
            run_id=handle.run_id,
            router=prov_run.router,
            load_manager=load_manager,
            vram_sizes=prov_run.vram_sizes,
            model_timeouts=prov_run.model_timeouts,
            context_store=context_store,
            summarizer=context_summarizer,
            routing_error=RoutingError,
            tool_registry=tool_registry,
        )
        non_agent_runner = NonAgentRunner(
            context_store=context_store,
            input_source=api_input_source(handle.run_id, self._registry),
            tool_registry=tool_registry,
        )
        crashed = False
        try:
            await replay_checkpoint(
                self.log_dir,
                workflow,
                checkpoint,
                source_run_id=source_run_id,
                new_run_id=handle.run_id,
                event_bus=bus,
                agent_runner=agent_runner,
                non_agent_runner=non_agent_runner,
                context_store=context_store,
                load_manager=load_manager,
                execution_environment=execution_environment,
            )
        except Exception:
            logger.exception("replay %s crashed", handle.run_id)
            crashed = True
        finally:
            teardown(prov_run)
            for token in handle.mirror_tokens:
                token.cancel()
        if crashed:
            handle.state = "failed"
        else:
            # Terminal: retire the handle so status/result serve the durable
            # run-dir artifacts (restored nodes byte-for-byte from the source).
            self._registry.remove(handle.run_id)

    # -- history / health ---------------------------------------------------

    def list_runs(self, workflow_name: str | None = None) -> list[RunHistoryEntry]:
        entries = self._registry.history(self.log_dir)
        if workflow_name is not None:
            entries = [e for e in entries if e.workflow_name == workflow_name]
        return entries

    def health(self) -> HealthResponse:
        return HealthResponse(status="ok", log_dir=str(self.log_dir))

    # -- status / result / input ------------------------------------------

    def run_status(self, run_id: str) -> RunStatusView:
        handle = self._registry.get(run_id)
        if handle is not None:
            return RunStatusView(
                run_id=run_id,
                workflow_name=handle.workflow_name,
                state=handle.state,
                started_at=handle.started_at,
                replay_source=handle.replay_source,
                nodes=dict(handle.nodes),
                pending_inputs=[
                    PendingInputView(
                        node_id=entry.node_id,
                        prompt=entry.prompt,
                        deadline=entry.deadline.isoformat() if entry.deadline is not None else None,
                    )
                    for entry in handle.pending_inputs.values()
                ],
            )
        view = status_view_from_run_dir(self.log_dir, run_id)
        if view is None:
            raise AgencyAPIError(
                CODE_UNKNOWN_RUN, 404, f"unknown run: {run_id}", {"run_id": run_id}
            )
        return view

    def run_result(self, run_id: str) -> RunResultView:
        handle = self._registry.get(run_id)
        if handle is not None:
            if handle.state == "running" or handle.result is None:
                raise AgencyAPIError(
                    CODE_RUN_NOT_FINISHED,
                    409,
                    f"run {run_id} is not finished",
                    {"run_id": run_id, "state": handle.state},
                )
            result = handle.result
            return RunResultView(
                run_id=run_id,
                status=result.status,
                total_tokens=result.total_tokens,
                duration_seconds=result.duration_seconds,
                nodes=[_result_from_record(record) for record in result.nodes],
            )
        if run_dir_metadata(self.log_dir, run_id) is None:
            raise AgencyAPIError(
                CODE_UNKNOWN_RUN, 404, f"unknown run: {run_id}", {"run_id": run_id}
            )
        view = result_view_from_run_dir(self.log_dir, run_id)
        if view is None:
            states = {
                node_id: _state_to_view(node_id, state)
                for node_id, state in node_states_from_run_dir(
                    self.log_dir / run_id
                ).items()
            }
            raise AgencyAPIError(
                CODE_RUN_NOT_FINISHED,
                409,
                f"run {run_id} is not finished",
                {"run_id": run_id, "state": derive_state(states)},
            )
        return view

    def submit_input(self, run_id: str, node_id: str, value: str) -> None:
        handle = self._registry.get(run_id)
        if handle is None:
            raise AgencyAPIError(
                CODE_UNKNOWN_RUN, 404, f"unknown run: {run_id}", {"run_id": run_id}
            )
        if handle.workflow is not None and node_id not in handle.workflow.nodes:
            raise AgencyAPIError(
                CODE_UNKNOWN_NODE,
                404,
                f"unknown node: {node_id}",
                {"run_id": run_id, "node_id": node_id},
            )
        if not self._registry.resolve_input(handle, node_id, value):
            raise AgencyAPIError(
                CODE_INPUT_NOT_AWAITING,
                409,
                f"node '{node_id}' is not awaiting input",
                {"run_id": run_id, "node_id": node_id},
            )
