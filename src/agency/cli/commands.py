from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

from agency.executor.replay import (
    ReplayError,
    find_source_run,
    get_folder_id,
    replay_checkpoint,
)
from agency.resource_manager.vram_tracker import resolve_vram_limit
from agency.yaml_engine.parser import load_workflow
from agency.yaml_engine.schema import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_SUMMARIZE_THRESHOLD,
    Workflow,
)

logger = logging.getLogger(__name__)


def _build_execution_backend(
    workflow: Workflow,
    environment: str | None,
    sandbox_caps: tuple[float | None, str | None, int | None, float | None],
    run_id: str,
    run_root: Path,
    run_result: object,
) -> tuple[object, str]:
    """Resolve the execution environment and return ``(backend, resolved)``.

    ``sandbox`` runs the Docker gate — on a ``ProvisionError`` the model
    gate's ``ProvisionedRun`` is torn down and the process exits 1 with no
    fallback to local (contract §2/§4, Constitution IV). ``local`` returns the
    process-local backend without touching the gate. ``resolved`` is the
    environment that was actually applied (for run-record observability).
    """
    from agency.executor.execution_env import LocalToolBackend, resolve_environment
    from agency.resource_manager.provisioning import ProvisionError, teardown
    from agency.resource_manager.sandbox import (
        build_sandbox_policy,
        default_image_root,
        provision_sandbox,
    )

    sandbox_cpu, sandbox_memory, sandbox_pids, sandbox_timeout = sandbox_caps
    resolved = resolve_environment(environment, workflow.execution_environment)
    if resolved != "sandbox":
        return LocalToolBackend(), resolved
    policy = build_sandbox_policy(
        workflow,
        cpu=sandbox_cpu,
        memory=sandbox_memory,
        pids_limit=sandbox_pids,
        timeout_seconds=sandbox_timeout,
    )
    try:
        return (
            asyncio.run(
                provision_sandbox(policy, default_image_root(), run_id, run_root=run_root)
            ),
            resolved,
        )
    except ProvisionError as exc:
        for line in exc.failures:
            print(f"sandbox unavailable: {line}", file=sys.stderr)
        teardown(run_result)
        raise SystemExit(1)


@click.command()
@click.argument("workflow_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--vram-limit",
    "vram_limit_cli",
    default=None,
    help="VRAM limit in human-readable format (e.g. '14GB', '8000MB'). Overrides YAML value.",
)
@click.option(
    "--models",
    "models_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Optional path to a YAML file of model name -> {endpoint, path, vram_size}. "
        "Merged over the workflow's models: section and used to pre-resolve every "
        "referenced model. A model present in both sources aborts before validation."
    ),
)
@click.option(
    "--tui",
    is_flag=True,
    default=False,
    help="Launch the live terminal DAG view, updating from node lifecycle events.",
)
@click.option(
    "--demo",
    is_flag=True,
    default=False,
    help="Simulate node events over the real bus (requires --tui).",
)
@click.option(
    "--demo-fail",
    "demo_fail_nodes",
    multiple=True,
    help="Node ID that should fail on every attempt (repeatable; requires --demo).",
)
@click.option(
    "--demo-fallback-fail",
    "demo_fallback_fail_nodes",
    multiple=True,
    help=(
        "Node ID whose fallback attempt should also fail (repeatable; "
        "requires --demo)."
    ),
)
@click.option(
    "--environment",
    "environment",
    type=click.Choice(["sandbox", "local"]),
    default=None,
    help=(
        "Execution environment for tool execution: 'sandbox' (Docker) or "
        "'local' (host). Overrides the workflow's execution_environment."
    ),
)
@click.option(
    "--sandbox-cpu",
    "sandbox_cpu",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Per-run sandbox CPU cap in cores. Overrides the workflow sandbox.cpu.",
)
@click.option(
    "--sandbox-memory",
    "sandbox_memory",
    type=str,
    default=None,
    help="Per-run sandbox memory cap (e.g. '512MB'). Overrides the workflow sandbox.memory.",
)
@click.option(
    "--sandbox-pids",
    "sandbox_pids",
    type=click.IntRange(min=1),
    default=None,
    help="Per-run sandbox PID limit. Overrides the workflow sandbox.pids_limit.",
)
@click.option(
    "--sandbox-timeout",
    "sandbox_timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Per-run sandbox per-tool timeout in seconds. Overrides the workflow sandbox.timeout_seconds.",
)
def run(
    workflow_path: Path,
    vram_limit_cli: str | None,
    models_path: Path | None,
    tui: bool,
    demo: bool,
    demo_fail_nodes: tuple[str, ...],
    demo_fallback_fail_nodes: tuple[str, ...],
    environment: str | None,
    sandbox_cpu: float | None,
    sandbox_memory: str | None,
    sandbox_pids: int | None,
    sandbox_timeout: float | None,
) -> None:
    if demo and not tui:
        raise click.UsageError("--demo requires --tui")
    if demo_fail_nodes and not demo:
        raise click.UsageError("--demo-fail requires --demo")
    if demo_fallback_fail_nodes and not demo:
        raise click.UsageError("--demo-fallback-fail requires --demo")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    workflow = load_workflow(workflow_path)
    result = resolve_vram_limit(
        cli_value=vram_limit_cli,
        yaml_value=workflow.vram_limit,
    )

    if result.limit_bytes is not None:
        logger.info("Effective VRAM limit: %s bytes (%s)", result.limit_bytes, result.source)
    else:
        logger.warning("No VRAM limit in effect — running without cap")

    logger.info("Loaded workflow '%s' with %d nodes", workflow.name, len(workflow.nodes))

    if workflow.summarizer_model is None:
        logger.info("Auto-summarization: disabled (no summarizer_model configured)")
    else:
        threshold_pct = workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
        window = workflow.context_window or DEFAULT_CONTEXT_WINDOW
        logger.info(
            "Auto-summarization: enabled at %d%% of %d-token window (%d tokens) with model '%s'",
            threshold_pct,
            window,
            window * threshold_pct // 100,
            workflow.summarizer_model,
        )

    sandbox_caps = (sandbox_cpu, sandbox_memory, sandbox_pids, sandbox_timeout)
    if tui:
        _launch_tui(
            workflow,
            demo=demo,
            models_path=models_path,
            vram_limit=result.limit_bytes,
            demo_fail_nodes=set(demo_fail_nodes),
            demo_fallback_fail_nodes=set(demo_fallback_fail_nodes),
            environment=environment,
            sandbox_caps=sandbox_caps,
        )
    else:
        _run_headless(
            workflow,
            models_path,
            result.limit_bytes,
            environment=environment,
            sandbox_caps=sandbox_caps,
        )


def _launch_tui(
    workflow: Workflow,
    *,
    demo: bool,
    models_path: Path | None = None,
    vram_limit: int | None = None,
    demo_fail_nodes: set[str] | None = None,
    demo_fallback_fail_nodes: set[str] | None = None,
    environment: str | None = None,
    sandbox_caps: tuple[float | None, str | None, int | None, float | None] = (None, None, None, None),
) -> None:
    """Open the live Textual view. With --demo simulates node lifecycle;
    otherwise runs a real orchestrator under the TUI with shared provisioning."""
    if not sys.stdout.isatty():
        raise click.UsageError("--tui requires an interactive terminal")

    from agency.core.event_bus import get_event_bus
    from agency.resource_manager import BackendRouter, RoutingError
    from agency.resource_manager.model_load_manager import ModelLoadManager
    from agency.resource_manager.provisioning import (
        ProvisionError,
        load_cli_models,
        provision,
        teardown,
    )
    from agency.tui.app import AgencyApp
    from agency.yaml_engine.dag import DAGBuilder

    DAGBuilder(workflow).build_dag()
    run_id = get_folder_id()

    router = BackendRouter()
    cli_models = load_cli_models(models_path) if models_path is not None else None

    try:
        prov_run = asyncio.run(provision(workflow, cli_models, router))
    except ProvisionError as exc:
        for line in exc.failures:
            print(f"model validation failed: {line}", file=sys.stderr)
        raise SystemExit(1)

    bus = get_event_bus()
    orchestrator = None
    resolved_env = None
    if not demo:
        from agency.executor.agent_runner import AgentRunner
        from agency.executor.context_store import ContextStore
        from agency.executor.node_runners import NonAgentRunner
        from agency.executor.orchestrator import DagOrchestrator
        from agency.executor.run_log import RunLogger
        from agency.executor.run_state import RunStateStore
        from agency.executor.summarizer import ContextSummarizer
        from agency.executor.tool_registry import build_workflow_tool_registry

        backend, resolved_env = _build_execution_backend(
            workflow, environment, sandbox_caps, run_id, Path("runs"), prov_run
        )
        context_store = ContextStore(run_id, Path("runs"), event_bus=bus)
        context_summarizer = None
        if workflow.summarizer_model is not None:
            context_summarizer = ContextSummarizer(
                run_id=run_id,
                store=context_store,
                router=router,
                model=workflow.summarizer_model,
                context_window=workflow.context_window or DEFAULT_CONTEXT_WINDOW,
                threshold_percent=(
                    workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
                ),
            )
        load_manager = ModelLoadManager(vram_limit_bytes=vram_limit)
        tool_registry = build_workflow_tool_registry(workflow, execution_backend=backend)
        agent_runner = AgentRunner(
            run_id=run_id,
            router=router,
            load_manager=load_manager,
            vram_sizes=prov_run.vram_sizes,
            model_timeouts=prov_run.model_timeouts,
            context_store=context_store,
            summarizer=context_summarizer,
            routing_error=RoutingError,
            tool_registry=tool_registry,
        )
        orchestrator = DagOrchestrator(
            workflow,
            run_id,
            agent_runner=agent_runner,
            non_agent_runner=NonAgentRunner(context_store=context_store, tool_registry=tool_registry),
            context_store=context_store,
            run_state=RunStateStore(
                Path("runs"),
                run_id,
                workflow,
                bus,
                vram_limit_bytes=vram_limit,
                execution_environment=resolved_env,
            ),
            run_logger=RunLogger(Path("runs"), run_id, workflow, bus),
            load_manager=load_manager,
            bus=bus,
        )

    app = AgencyApp(
        workflow,
        run_id,
        event_bus=bus,
        demo=demo,
        vram_limit=vram_limit,
        demo_fail_schedule=list(demo_fail_nodes) if demo_fail_nodes else None,
        demo_fallback_fail_schedule=(
            list(demo_fallback_fail_nodes) if demo_fallback_fail_nodes else None
        ),
        orchestrator=orchestrator,
        execution_environment=resolved_env,
    )
    try:
        app.run()
    finally:
        if not demo:
            teardown(prov_run)

    if app.summary is not None:
        from agency.tui.summary import render_summary_table

        print(render_summary_table(app.summary))


def _run_headless(
    workflow: Workflow,
    models_path: Path | None,
    vram_limit: int | None,
    environment: str | None = None,
    sandbox_caps: tuple[float | None, str | None, int | None, float | None] = (None, None, None, None),
) -> None:
    """Run the provisioning gate, then execute the DAG headlessly.

    Local imports so the provision module and the executor load only when the
    flagless path runs. On ``ProvisionError`` the full failure report is
    printed to stderr and the process exits non-zero (no nodes run, no
    servers spawned on that path). Otherwise the DAG is driven by the orchestrator,
    owned servers are torn down once the run finishes, the summary table is
    printed and appended to the run log, and the exit code reflects the run
    outcome (0 completed, 1 failed).
    """
    from agency.core.event_bus import get_event_bus
    from agency.executor.agent_runner import AgentRunner
    from agency.executor.context_store import ContextStore
    from agency.executor.node_runners import NonAgentRunner
    from agency.executor.orchestrator import (
        AWAITING_RETRY,
        COMPLETED,
        COMPLETED_FALLBACK,
        FAILED,
        PENDING,
        RUN_STATUS_FAILED,
        RUNNING,
        SKIPPED,
        DagOrchestrator,
    )
    from agency.executor.run_log import RunLogger
    from agency.executor.run_state import RunStateStore
    from agency.executor.summarizer import ContextSummarizer
    from agency.executor.tool_registry import build_workflow_tool_registry
    from agency.resource_manager import BackendRouter, RoutingError
    from agency.resource_manager.model_load_manager import ModelLoadManager
    from agency.resource_manager.provisioning import (
        ProvisionError,
        load_cli_models,
        provision,
        teardown,
    )
    from agency.tui.dag_view import (
        STATUS_COMPLETED_FALLBACK,
        STATUS_DONE,
        STATUS_FAILED,
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_SKIPPED,
        NodeRow,
    )
    from agency.tui.summary import (
        RunSummary,
        append_summary_to_log,
        render_summary_table,
    )

    router = BackendRouter()
    cli_models = load_cli_models(models_path) if models_path is not None else None

    try:
        run = asyncio.run(provision(workflow, cli_models, router))
    except ProvisionError as exc:
        for line in exc.failures:
            print(f"model validation failed: {line}", file=sys.stderr)
        raise SystemExit(1)

    logger.info(
        "Pre-execution validation complete: %d referenced model(s) resolved, "
        "%d owned llama.cpp server(s) started",
        len(run.vram_sizes),
        len(run.owned_servers),
    )

    log_dir = Path("runs")
    run_id = get_folder_id()
    backend, resolved_env = _build_execution_backend(
        workflow, environment, sandbox_caps, run_id, log_dir, run
    )
    bus = get_event_bus()
    context_store = ContextStore(run_id, log_dir, event_bus=bus)
    context_summarizer = None
    if workflow.summarizer_model is not None:
        context_summarizer = ContextSummarizer(
            run_id=run_id,
            store=context_store,
            router=router,
            model=workflow.summarizer_model,
            context_window=workflow.context_window or DEFAULT_CONTEXT_WINDOW,
            threshold_percent=(
                workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
            ),
        )
    load_manager = ModelLoadManager(vram_limit_bytes=vram_limit)
    tool_registry = build_workflow_tool_registry(workflow, execution_backend=backend)
    agent_runner = AgentRunner(
        run_id=run_id,
        router=router,
        load_manager=load_manager,
        vram_sizes=run.vram_sizes,
        model_timeouts=run.model_timeouts,
        context_store=context_store,
        summarizer=context_summarizer,
        routing_error=RoutingError,
        tool_registry=tool_registry,
    )
    orchestrator = DagOrchestrator(
        workflow,
        run_id,
        agent_runner=agent_runner,
        non_agent_runner=NonAgentRunner(context_store=context_store, tool_registry=tool_registry),
        context_store=context_store,
        run_state=RunStateStore(
            log_dir,
            run_id,
            workflow,
            bus,
            vram_limit_bytes=vram_limit,
            execution_environment=resolved_env,
        ),
        run_logger=RunLogger(log_dir, run_id, workflow, bus),
        load_manager=load_manager,
    )
    try:
        result = asyncio.run(orchestrator.run())
    finally:
        teardown(run)

    node_types: dict[str, str] = {
        nid: node.type for nid, node in workflow.nodes.items()
    }
    status_to_row: dict[str, str] = {
        COMPLETED: STATUS_DONE,
        COMPLETED_FALLBACK: STATUS_COMPLETED_FALLBACK,
        FAILED: STATUS_FAILED,
        SKIPPED: STATUS_SKIPPED,
        RUNNING: STATUS_RUNNING,
        AWAITING_RETRY: STATUS_RUNNING,
        PENDING: STATUS_PENDING,
    }
    rows = tuple(
        NodeRow(
            node_id=record.node_id,
            node_type=node_types[record.node_id],
            model=record.model or "-",
            status=status_to_row[record.status],
            tokens=record.tokens,
            duration_seconds=record.duration_seconds,
            started_at=record.started_at,
        )
        for record in result.nodes
    )
    summary = RunSummary(
        run_id=run_id,
        completed_at=datetime.now(timezone.utc),
        total_tokens=result.total_tokens,
        vram_peak_bytes=None,
        duration_seconds=result.duration_seconds,
        nodes=rows,
    )
    print(render_summary_table(summary))
    append_summary_to_log(log_dir, run_id, summary)
    raise SystemExit(1 if result.status == RUN_STATUS_FAILED else 0)


@click.command(name="replay")
@click.argument("workflow_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--checkpoint",
    required=True,
    help=(
        "Node ID to restore; that node and its transitive ancestors are "
        "replayed byte-for-byte from the source run, the rest is re-executed."
    ),
)
@click.option(
    "--run",
    "source_run",
    default=None,
    metavar="RUN_ID",
    help=(
        "Source run ID under --log-dir holding the completed checkpoint. "
        "Defaults to the most recent run (by started_at) for this workflow "
        "with a completed checkpoint for --checkpoint."
    ),
)
@click.option(
    "--log-dir",
    "log_dir",
    default="runs",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Directory holding previous runs; the fresh run is created here.",
)
@click.option(
    "--vram-limit",
    "vram_limit_cli",
    default=None,
    help="VRAM limit in human-readable format. Overrides YAML value.",
)
@click.option(
    "--models",
    "models_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional YAML file of model overrides for tail execution.",
)
@click.option(
    "--environment",
    "environment",
    type=click.Choice(["sandbox", "local"]),
    default=None,
    help=(
        "Execution environment for tool execution: 'sandbox' (Docker) or "
        "'local' (host). Overrides the workflow's execution_environment."
    ),
)
@click.option(
    "--sandbox-cpu",
    "sandbox_cpu",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Per-run sandbox CPU cap in cores. Overrides the workflow sandbox.cpu.",
)
@click.option(
    "--sandbox-memory",
    "sandbox_memory",
    type=str,
    default=None,
    help="Per-run sandbox memory cap (e.g. '512MB'). Overrides the workflow sandbox.memory.",
)
@click.option(
    "--sandbox-pids",
    "sandbox_pids",
    type=click.IntRange(min=1),
    default=None,
    help="Per-run sandbox PID limit. Overrides the workflow sandbox.pids_limit.",
)
@click.option(
    "--sandbox-timeout",
    "sandbox_timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Per-run sandbox per-tool timeout in seconds. Overrides the workflow sandbox.timeout_seconds.",
)
def replay(
    workflow_path: Path,
    checkpoint: str,
    source_run: str | None,
    log_dir: Path,
    vram_limit_cli: str | None,
    models_path: Path | None,
    environment: str | None,
    sandbox_cpu: float | None,
    sandbox_memory: str | None,
    sandbox_pids: int | None,
    sandbox_timeout: float | None,
) -> None:
    """Restore a completed checkpoint prefix from a previous run,
    then re-execute the remaining nodes into a fresh run dir."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    workflow = load_workflow(workflow_path)
    vram = resolve_vram_limit(cli_value=vram_limit_cli, yaml_value=workflow.vram_limit)

    from agency.core.event_bus import get_event_bus
    from agency.executor.agent_runner import AgentRunner
    from agency.executor.context_store import ContextStore
    from agency.executor.node_runners import NonAgentRunner
    from agency.executor.summarizer import ContextSummarizer
    from agency.executor.tool_registry import build_workflow_tool_registry
    from agency.resource_manager import BackendRouter, RoutingError
    from agency.resource_manager.model_load_manager import ModelLoadManager
    from agency.resource_manager.provisioning import (
        ProvisionError,
        load_cli_models,
        provision,
        teardown,
    )

    router = BackendRouter()
    cli_models = load_cli_models(models_path) if models_path is not None else None

    try:
        run_result = asyncio.run(provision(workflow, cli_models, router))
    except ProvisionError as exc:
        for line in exc.failures:
            print(f"model validation failed: {line}", file=sys.stderr)
        raise SystemExit(1)

    # Source-run lookup first so a missing/invalid source run takes precedence
    # over sandbox provisioning (and no run dir is created either way).
    try:
        source_run_id = find_source_run(
            log_dir, source_run, workflow.name, checkpoint
        )
    except ReplayError as exc:
        raise click.ClickException(str(exc)) from exc
    new_run_id = get_folder_id()
    sandbox_caps = (sandbox_cpu, sandbox_memory, sandbox_pids, sandbox_timeout)
    backend, resolved_env = _build_execution_backend(
        workflow, environment, sandbox_caps, new_run_id, log_dir, run_result
    )

    bus = get_event_bus()
    context_store = ContextStore("replay", log_dir, event_bus=bus)
    context_summarizer = None
    if workflow.summarizer_model is not None:
        context_summarizer = ContextSummarizer(
            run_id="replay",
            store=context_store,
            router=router,
            model=workflow.summarizer_model,
            context_window=workflow.context_window or DEFAULT_CONTEXT_WINDOW,
            threshold_percent=(
                workflow.summarize_threshold or DEFAULT_SUMMARIZE_THRESHOLD
            ),
        )
    load_manager = ModelLoadManager(vram_limit_bytes=vram.limit_bytes)
    tool_registry = build_workflow_tool_registry(workflow, execution_backend=backend)
    agent_runner = AgentRunner(
        run_id="replay",
        router=router,
        load_manager=load_manager,
        vram_sizes=run_result.vram_sizes,
        model_timeouts=run_result.model_timeouts,
        context_store=context_store,
        summarizer=context_summarizer,
        routing_error=RoutingError,
        tool_registry=tool_registry,
    )

    try:
        result = asyncio.run(
            replay_checkpoint(
                log_dir,
                workflow,
                checkpoint,
                source_run_id=source_run_id,
                new_run_id=new_run_id,
                event_bus=bus,
                agent_runner=agent_runner,
                non_agent_runner=NonAgentRunner(
                    context_store=context_store, tool_registry=tool_registry
                ),
                context_store=context_store,
                load_manager=load_manager,
                execution_environment=resolved_env,
            )
        )
    except ReplayError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        teardown(run_result)

    print(
        f"Replaying '{workflow.name}' from checkpoint '{checkpoint}' "
        f"(source run: {result.source_run_id})"
    )
    print(f"New run id: {result.new_run_id}")
    for node_id in result.restored:
        print(f"  restored: {node_id}")
    for node_id in result.executed:
        print(f"  executed: {node_id}")
    print(f"New run dir: {log_dir / result.new_run_id}")


@click.command(name="serve")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address for the HTTP service.",
)
@click.option(
    "--port",
    default=8000,
    show_default=True,
    type=int,
    help="Port for the HTTP service.",
)
@click.option(
    "--log-dir",
    "log_dir",
    default="runs",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Run history root shared with the CLI; new runs are created here.",
)
def serve(host: str, port: int, log_dir: Path) -> None:
    """Start the Agency HTTP service (uvicorn + FastAPI)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import uvicorn

    from agency.api.app import create_app
    from agency.api.config import ServiceConfig

    app = create_app(ServiceConfig(host=host, port=port, log_dir=log_dir))
    uvicorn.run(app, host=host, port=port, log_level="info")


class AgencyGroup(click.Group):
    """Entry group for ``agency``.

    Subcommands ``run`` and ``replay`` are looked up as usual; any bare
    invocation whose first token is not a known subcommand name (e.g. legacy
    ``agency <workflow.yaml> ...``) falls through to ``run`` with the full
    argument vector, so the original entry point keeps working verbatim.
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and self.get_command(ctx, args[0]) is None:
            return "run", run, args
        return super().resolve_command(ctx, args)


agency_cli = AgencyGroup(
    name="agency",
    invoke_without_command=True,
    no_args_is_help=True,
)
agency_cli.add_command(run)
agency_cli.add_command(replay)
agency_cli.add_command(serve)


def main() -> None:
    # Load a local .env (if present) so model-dir / server-binary settings are
    # picked up. Real environment variables always take precedence (override=False).
    load_dotenv()
    agency_cli.main()
