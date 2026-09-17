# AGENTS.md — Agency

CLI + API platform for orchestrating AI agent workflows on local llama.cpp hardware, with a Flutter client app (`agency_app/`).

## Package manager: uv

- **Install deps**: `uv sync` (add `--extra dev` for test tools, `--extra gpu` for pynvml)
- **Run CLI**: `uv run agency <workflow.yaml>` (still works — bare first arg falls through to the `run` subcommand)
- **Serve HTTP API**: `uv run agency serve [--host 127.0.0.1] [--port 8000] [--log-dir runs]` (defaults `127.0.0.1:8000`; `--log-dir` defaults to `runs`)
- **Run tests**: `uv run pytest` or `uv run pytest tests/test_dag.py -v` for a single file

## Project layout (src layout)

```
src/agency/
  api/app.py                  # create_app(): FastAPI factory; lifespan startup recovery +
                              # graceful shutdown; routes; AgencyAPIError/422 handlers
  api/config.py               # ServiceConfig: host, port, log_dir
  api/registry.py             # RunRegistry/RunHandle/PendingInput: per-run bus mirror,
                              # pending HIL inputs, api_input_source() factory
  api/schemas.py              # Pydantic request/response models + AgencyAPIError
  api/service.py              # RunService: start_run/replay_run/run_status/run_result/
                              # submit_input/list_runs/health; allocate_run_id()
  api/stream.py               # EventBroadcaster: per-client bounded queues, consumer
                              # tasks, shared frame serialization, overflow/shutdown
  cli/commands.py             # Click group: run + replay + serve subcommands; main() entrypoint
  core/event_bus.py           # EventBus singleton, get_event_bus() / reset_event_bus()
  core/events.py              # EVENT_* constants, frozen payload dataclasses, EventEnvelope
  core/types.py               # NodeType enum
  core/llamacpp_backend.py    # LlamaCppServer / ServerInfo: spawn+manage local llama.cpp server
  executor/orchestrator.py    # DagOrchestrator: sole Node* event publisher; returns DagRunResult
  executor/agent_runner.py    # AgentRunner: agent inference via BackendRouter + ModelLoadManager;
                              # optional tool registry -> bounded model-driven tool loop;
                              # per-model inference timeout (models: timeout_seconds)
  executor/node_runners.py    # NonAgentRunner: conditional/merge/broadcast/human_in_loop/tool_call
  executor/binding_resolver.py # VariableResolver: resolves {{ nodes.<id>.output }} bindings
  executor/context_store.py   # ContextStore: per-phase context, persists phase_<id>_context.jsonl
  executor/summarizer.py      # ContextSummarizer: compresses phase context at token threshold
  executor/exec_tools.py      # Executable tool engine (stdlib only): ToolExecutionResult /
                              # ToolExecutionError, async execute_shell / execute_program /
                              # execute_file_write (asyncio subprocess, cwd, timeout),
                              # make_named_tool (strict {placeholder} binding)
  executor/execution_env.py   # ToolExecutionBackend protocol (shell/program/file_write +
                               # optional ToolCaps), LocalToolBackend (host path, delegates to
                               # exec_tools), ToolCaps (per-named-tool cap subset),
                               # resolve_environment() (CLI > workflow > sandbox default)
  executor/tool_registry.py   # ToolRegistry + ToolSpec/ToolParam metadata, DEFAULT_REGISTRY
                               # (string utilities), BUILTIN_TOOL_NAMES,
                               # build_workflow_tool_registry() (per-workflow registry)
  executor/retry.py           # RetrySupervisor: retry policies, exponential backoff, fallback lookup
  executor/fallback.py        # FallbackSupervisor: fallback-model run after terminal failure
  executor/run_state.py       # RunStateStore: fsynced WAL + atomic node checkpoints
  executor/run_log.py         # RunLogger: execution.jsonl audit trail
  executor/replay.py          # replay_checkpoint(): checkpoint-prefix restore from a prior run
  executor/contracts.py       # RunResult (runner return contract)
  resource_manager/backend_router.py     # BackendRouter (httpx): model->endpoint routing, inference.log
  resource_manager/model_load_manager.py # ModelLoadManager: VRAM-gated load acquisition queue
  resource_manager/model_offload_manager.py # ModelOffloadManager
  resource_manager/model_registry.py   # ModelRegistry: model name -> endpoint map
  resource_manager/sandbox.py          # DockerSandboxBackend / SandboxPolicy / ToolCaps layering:
                                       # one-shot container per tool exec (keep-alive main + exec),
                                       # read-only rootfs + tmpfs /scratch, network none, bounds,
                                       # stdout/stderr drain, artifact copy-out, OOM/timeout mapping,
                                       # provision_sandbox() + default_image_root() (gate build)
  resource_manager/vram_tracker.py     # resolve_vram_limit / parse_vram_size / detect_gpu_vram
  resource_manager/vram_monitor.py     # VRAMMonitor: pynvml polling, threshold/freed events
  resource_manager/provisioning.py     # provision(): pre-run model validation + server auto-start
                                       # + tool-call capability probe for tool-enabled agent models;
                                       # get_models_dirs() / resolve_model_path() (AGENCY_MODELS_DIRS)
  tui/app.py                  # AgencyApp (Textual): event consumer, live metrics, one-shot summary
  tui/dag_view.py             # DAGView widget, NodeRow, status vocabulary constants
  tui/metrics_panel.py        # MetricsPanel + MetricsCollector, VRAM/elapsed formatting
  tui/simulator.py            # Deterministic demo event producer (--demo)
  tui/summary.py              # RunSummary + render_summary_table + append_summary_to_log
  yaml_engine/schema.py       # Pydantic Workflow: node models, ModelSpec, RetryPolicy, Edge,
                              # ToolDefinition (shell/program/file_write), AgentTools
  yaml_engine/parser.py       # load_workflow()
  yaml_engine/dag.py          # DAGBuilder: topo sort, cycle detection
sandbox/               # Docker sandbox image (spec 006): Dockerfile -> agency-sandbox:v1
 agency_app/            # Flutter (Dart) client (specs 003/004/007): REST + /events WebSocket consumer
  lib/main.dart             # entrypoint: boots AppState, WorkflowCatalog, AgencyClient, AgencyEvents
  lib/api/agency_client.dart # AgencyClient: REST client (health, start/list runs, status, HIL input, result)
  lib/api/agency_events.dart # AgencyEvents: /events WebSocket frame stream, auto-reconnect 1 s -> 30 s
  lib/api/models.dart       # wire-faithful DTOs: EventFrame + decoders, run/node/status views, error codes
  lib/app/app_state.dart    # AppState: persisted settings (server address, theme, canvas mode), run stores
  lib/app/home_page.dart    # HomePage: top bar, collapsible sidebar, canvas + inspector + HIL card
  lib/app/sidebar/          # WorkflowList (start buttons), HistoryList (past runs, newest first)
  lib/canvas/canvas_view.dart # CanvasView: "abyss" canvas — pan/zoom/pinch, tap-to-inspect, stale badge
  lib/canvas/canvas_scene.dart # CanvasScene: pure buildScene(state, definition) snapshot
  lib/canvas/canvas_keyboard.dart # handleViewportKeys: shared pan/zoom keyboard gestures (both modes)
  lib/canvas/scene_layout.dart # computeLayout: Kahn longest-path column grid -> NodePosition
  lib/canvas/scene_painter.dart # ScenePainter: sea gradient, glowing strands, light-shape nodes, motes
  lib/canvas/state_encodings.dart # kStateEncodings: 7-state shape + motion table, color-independent
  lib/canvas/inspector.dart # Inspector: node type, model, state, attempt, tokens, output, error
  lib/canvas/summary_bar.dart # SummaryBar: run name, status, cancelled badge, elapsed, total tokens
  lib/canvas/hil_prompt.dart # HilPromptCard: HIL prompt, deadline countdown, text field, submit
  lib/canvas/zoom_controls.dart # ZoomControls: -/%/+/fit bar, shared by both canvas modes
  lib/canvas/classic/       # "classic" canvas: ClassicCanvasView/Scene/Layout/Painter, kTypeMarkers,
                            # kClassicStateEncodings, ClassicPalette, ClassicLegend
  lib/catalog/workflow_catalog.dart # WorkflowCatalog: live scan of workflows dir (watch + poll)
  lib/catalog/workflow_def.dart # parseWorkflowDefinition: lenient YAML -> WorkflowDefinition
  lib/catalog/catalog_entry.dart # CatalogEntry: sidebar row, holds parse error when unreadable
  lib/monitor/system_metrics.dart # SystemMetrics sample model + empty() neutral sample (spec 007)
  lib/monitor/system_service.dart # 1 Hz sampler: /proc/stat, /proc/meminfo, /proc/net/dev,
                                  # nvidia-smi (injectable fetch/tick seam, per-metric neutral guards)
  lib/monitor/metric_meta.dart # MetricKey enum + per-key identity (title, accent, unit, formatters)
  lib/monitor/resource_monitor.dart # ResourceMonitor (ChangeNotifier): current sample, 60-sample
                                    # histories, per-metric visibility prefs, per-session collapsed flag
  lib/monitor/monitor_card.dart # theme-aware fl_chart trend card (value, unit, sub-value, ~60-pt trend)
  lib/monitor/metrics_dialog.dart # MetricsDialog: checkbox per MetricKey driving setMetricVisible
  lib/monitor/resource_monitor_panel.dart # panel: header (title, gear, collapse) + scrollable body,
                                          # one card per enabled metric (topbar-toggleable)
  lib/run/run_store.dart    # RunStore: per-run ChangeNotifier; seeds from snapshot, reduces frames
  lib/run/run_state.dart    # immutable RunState/SNodeState + pure reducers (run-id guard, terminal lock)
  lib/theme/                # AbyssPalette dark/light colors, themeDataFor Material 3 theme
  test/                     # flutter_test tests (one file per module)
  test/monitor/             # monitor module tests (one per lib/monitor/ file)
tests/               # pytest tests (one file per module)
runs/<run_id>/       # run artifacts: metadata.json, execution.jsonl, wal.jsonl,
                     # checkpoints/<node>.json, phase_<id>_context.jsonl, outputs, inference.log,
                     # sandbox/<execution-uuid>/ (sandboxed tool artifact copy-out)
```

## Key facts

- **Python >=3.10** required. All source files use `from __future__ import annotations`.
- **Pytest**: `asyncio_mode = "auto"` in `pyproject.toml` — async test functions need no decorator.
- **Workflow schema**: YAML workflows are validated by Pydantic (`agency.yaml_engine.schema.Workflow`). Node types are discriminated union via `Field(discriminator="type")`: agent, conditional, merge, broadcast, human_in_loop, tool_call. Workflow also carries `models:` (name -> `ModelSpec` with endpoint/path/vram_size/startup_timeout/timeout_seconds — `timeout_seconds` is the per-model inference timeout used by `AgentRunner`), `tools:` (name -> `ToolDefinition`, discriminated by `kind`: shell/program/file_write), `vram_limit`, `summarizer_model`, `context_window`, `summarize_threshold`. Cycle detection runs during validation via `DAGBuilder`; tool names are validated at load time (built-in collision -> `duplicate tool name '<n>'`; agent allow-list reference -> `agent node '<id>' allow-list references unknown tool '<n>'`).
- **Event bus**: Singleton `EventBus` in `agency.core.event_bus`. Events are defined in `agency.core.events` (frozen payload dataclasses + `EVENT_*` string constants, wrapped in `EventEnvelope`). Use `reset_event_bus()` in tests to avoid state leaks between test runs.
- **Orchestrator**: `DagOrchestrator` (executor/orchestrator.py) is the *sole* publisher of `Node*` lifecycle events and the sole skip-marker. Injected runners (AgentRunner / NonAgentRunner) receive per-node context and return a `RunResult` without emitting events. Terminal node statuses: `completed`, `completed-fallback`, `failed`, `skipped`.
- **Provisioning gate**: `provision()` (resource_manager/provisioning.py) runs before any node executes. It merges the workflow's `models:` section with an optional `--models FILE` (a model present in both aborts), resolves every referenced model to a live endpoint, auto-starting a local llama.cpp server (`LlamaCppServer`) when the declared endpoint is unreachable and the model has a local path. After auto-start, every *distinct* model used by a tool-enabled agent node is checked with a one-shot tool-call capability probe (a tiny completion offering a single `ping` tool with `tool_choice: "auto"`; pass = HTTP 200 with a non-empty `tool_calls` array). Any failure collapses into one `ProvisionError` report and aborts the run before spawns; Agency-owned servers are torn down after the run (and before the abort on probe failure). In sandbox mode (the default) the gate additionally provisions the Docker runtime: it connects to the daemon and builds `agency-sandbox:v1` from `sandbox/Dockerfile` only when the image is absent (cached — later runs never rebuild); a runtime/build failure collapses into the same consolidated abort (see **Sandbox execution**).
 - **Model path resolution** (portable workflows): workflow `models:` `path:` values are resolved by `resolve_model_path()` (resource_manager/provisioning.py). `$VAR`/`${VAR}` and a leading `~` are expanded first; an absolute path is used as-is. A relative path (a model filename, optionally with a subdirectory) is searched against `get_models_dirs()` — the `AGENCY_MODELS_DIRS` colon-separated list (element 0 primary; default `~/.llama-cpp/models`) — and the first directory containing the file wins. A relative path found in no directory is a consolidated pre-run failure listing every searched dir; absolute paths are trusted (spawn owns their lifecycle). Model files referenced by shipped workflows are bare filenames so the repo is clone-and-run: put the `.gguf` in any listed dir (or set `AGENCY_MODELS_DIRS`). Settings come from a local `.env` (gitignored) loaded once at CLI entry (`load_dotenv()` in cli/commands.py); real env vars always win. `LLAMA_CPP_SERVER_PATH` names the llama.cpp server binary used for auto-start (default `llama-server`).
 - **VRAM resolution priority**: CLI flag > YAML config > auto-detect (pynvml, requires `--extra gpu`) > no limit (`resolve_vram_limit` in resource_manager/vram_tracker.py). `VRAMMonitor` polls GPU 0 via pynvml and publishes `VRAMThresholdExceeded` / `VRAMFreed` with a 512 MB safety margin.
- **BackendRouter**: Routes inference to registered llama.cpp HTTP endpoints (`/v1/chat/completions`). Logs JSON-line records to `runs/<run_id>/inference.log`. Fed by provisioning; `ModelRegistry` maps model names to endpoints.
- **Retry & fallback**: Per-node `RetryPolicy` (max attempts, exponential backoff) is enforced by `RetrySupervisor`; when a node failure is terminal, `FallbackSupervisor` runs the node's `fallback:` model once (no retries, no chained fallbacks) — success records status `completed-fallback`.
- **Run persistence**: `RunLogger` appends `execution.jsonl`; `RunStateStore` writes `metadata.json`, an fsynced `wal.jsonl`, and atomic `checkpoints/<node>.json` — disk is always updated before in-memory state so memory never leads. `recover_status()` replays the WAL for crash recovery.
- **Replay**: `uv run agency replay <workflow.yaml> --checkpoint NODE [--run RUN_ID]` restores the checkpoint node plus its transitive ancestors byte-for-byte (checkpoint + raw output files) from a source run, and re-executes the remaining nodes into a fresh run dir. Source run defaults to the most recent eligible run for the workflow.
- **API service**: `agency serve` runs uvicorn + FastAPI in-process (`agency/api/` package). Per-run bus subscribers carry a run-id guard (`if getattr(payload, "run_id", None) != self._run_id: return`) so concurrent runs on the singleton bus never cross-contaminate — applied in `RunStateStore`, `RunLogger`, `RetrySupervisor`, `FallbackSupervisor`, and the orchestrator cancel handler, plus the API's own status mirror. `InputSource` is `(node_id, prompt) -> text`; the API serves HIL nodes through the registry's `api_input_source` future (pending input resolves once, duplicates → 409).
- **Flutter client** (`agency_app/`, specs 003/004): standalone Flutter Material 3 app — `cd agency_app && flutter run` to run, `flutter test` to test. Talks to `agency serve` over REST (`GET /health`, `GET /runs`, `POST /runs` with `{workflow: <absolute path>}`, `GET /runs/{id}`, `POST /runs/{id}/nodes/{node}/input`, `GET /runs/{id}/result`) plus a read-only `/events` WebSocket (auto-reconnect 1 s→30 s; a 1008 slow-consumer close triggers a snapshot resync). Server address (default `127.0.0.1:8000`), theme, canvas mode, and workflows dir are persisted in-app. `WorkflowCatalog` scans a *local* workflows directory (Directory.watch + 5 s poll) and "Start" POSTs the workflow file's absolute path, so the path must be visible to the server process. Per-run immutable `RunState` is reduced from event frames (run-id guard; terminal nodes never re-transition; resync after HIL submit). Two interchangeable canvas renderings of the same run state — "abyss" (glowing light shapes, default) and "classic" (DAG cards) — share the layout, pan/zoom gestures, and keyboard controls. The app can start runs and answer HIL prompts but cannot cancel (the API has no cancel route).
- **Binding syntax**: Templates use `{{ nodes.<node_id>.output }}` placeholders resolved by `VariableResolver`. If a referenced binding has no upstream output when the node executes, the node is skipped with reason `unresolved_binding` (agent and non-agent nodes alike). Malformed `{{ ... }}` expressions are left untouched with a warning; `BindingResolutionError` is only raised for a non-dict context.
- **Auto-summarization**: If `summarizer_model` is set, `ContextSummarizer` replaces the phase's shared context with a summary from that dedicated model when the serialized context reaches `summarize_threshold`% (default 80%) of `context_window` tokens. Summarization never borrows a workflow agent's model.
- **Tools**: `tool_call` nodes resolve `arguments_template` to JSON whose keys become kwargs; `DEFAULT_REGISTRY` ships only pure string utilities (echo, upper, lower, trim, length, replace). Every run (CLI, TUI, replay, API) also gets the per-workflow registry from `build_workflow_tool_registry(workflow)`: the string utilities, the always-present generic execution tools (`shell` — `bash -c` with optional `working_dir`/`timeout_seconds`; `program` — run a script file, interpreter explicit > by extension (`.py`/`.sh`) > executable bit; `write_file` — write content to a path, parents created), and the workflow's named tools from the top-level `tools:` section (kind shell/program/file_write; parameters are the template's `{name}` placeholders plus an implicit `content` for file_write; strict binding — missing/unexpected kwargs fail the node with the contract reason). Executable outcomes: shell/program success -> node output is the structured result as a JSON string (status/exit_code/output/stderr/error/duration_seconds/command; status ∈ success|failed|timed_out|error); file_write success -> the written path as a plain string; non-success -> failed node with the full structured report embedded in `error` so `execution.jsonl` captures output even on failure. Callers may build custom registries.
- **Sandbox execution** (spec 006): tool execution has an environment resolved as CLI `--environment [sandbox|local]` > workflow `execution_environment:` > default `sandbox`. `sandbox` runs every tool call in a one-shot Docker container (create → exec → drain → artifact copy-out → remove; keep-alive `sleep infinity` main, per-invocation `exec`; containers labeled `agency-sandbox=<run_id>` and removed on every path — success, failure, timeout, OOM). Posture: `agency-sandbox:v1` image (debian bookworm-slim: bash/coreutils/python3, uid 1000), read-only rootfs, tmpfs `/scratch` (default 64MB) + `/tmp`, `network_mode="none"`, `cap_drop: ALL`, `no-new-privileges`, pids/CPU/memory limits. Bound layering (later wins): built-in defaults (cpu 2, memory 512MB, pids 256, timeout 300s, output 1MB, scratch 64MB) ← workflow `sandbox:` (`SandboxConfig`: cpu/memory/pids_limit/timeout_seconds/output_limit_bytes/scratch_size) ← per-run CLI caps (`--sandbox-cpu` / `--sandbox-memory` / `--sandbox-pids` / `--sandbox-timeout`) ← per-named-tool caps (`cpu` / `memory` / `pids_limit` fields on `tools:` entries). Failure mapping (inside the structured result): OOM → `exit_code 137` + `terminated by sandbox memory limit (OOMKilled)`; bound kill → status `timed_out` + `exceeded <n>s timeout` (the run never hangs); any other exit → `failed` + `exited with code <n>`. Result differences: structured results gain an `environment` field (`"sandbox"` | `"local"`); a sandboxed `write_file`'s node output is the plain host path `runs/<run_id>/sandbox/<execution-uuid>/<basename>` (staged into the container read-only, the written file copied back out); shell/program files written under `/scratch` are copied out to the same per-execution dir. Gate: an unreachable daemon or failed image build collapses into the single pre-run error (CLI: printed, exit 1; API: 409 `sandbox_unavailable`) — never a silent fallback to local. `local` mode delegates to the same `exec_tools` executors as before the feature (host paths, host network, host filesystem). `tests/test_sandbox_integration.py` drives real containers (auto-skips when no daemon); all other sandbox tests are hermetic via the fake docker client in `tests/test_sandbox_backend.py`. Demo: `workflows/sandbox_smoke.yaml` (tool-only, no model server needed).
- **Agent tool calling**: an agent node's optional `tools:` block (`allow: [names]` — omitted offers all available tools; `max_rounds: int = 4, ge=1`) opts into the bounded model-driven tool loop in `AgentRunner`. Each request offers the tools as OpenAI `tools` with `tool_choice: "auto"`; a response with `tool_calls` executes each call against the per-workflow registry and feeds the result back as that call's `role: "tool"` message (unknown tool / invalid argument JSON feed back error text — per-call problems never fail the step); a response without `tool_calls` completes the node with its content. If the `max_rounds`-th round-trip still requests tools, the node fails with `tool-loop bound exceeded after <N> round-trips` (retry/fallback then apply as for any failure). One VRAM slot covers the whole attempt; token usage sums all turns; a node without `tools:` stays byte-for-byte the single-shot path.
- **TUI**: `--tui` launches `AgencyApp` (Textual) — a pure event consumer that renders the DAG live; real nodes are never executed through it. `--demo` (requires `--tui`) simulates node lifecycle over the real bus, deterministically by topo index; `--demo-fail` / `--demo-fallback-fail` (repeatable) script failures. `--tui` requires a TTY; `q` quits. The run summary table is printed on exit and appended to the run log exactly once.
- **Exit codes**: headless runs exit 0 on `completed`, 1 on run failure or `ProvisionError`.

## Pitfalls

- `[project.scripts]` points at `agency.cli.commands:main`, which builds the Click group `agency_cli`. Any bare first argument that is not a subcommand name falls through to `run` with the full arg vector (legacy `agency <workflow.yaml>` behavior) — see `AgencyGroup.resolve_command`.
- `executor/` must not import `resource_manager/` at runtime — `ContextSummarizer`'s router is `TYPE_CHECKING`-typed (dependency rule).
- Docker bind-mount `Source`/`Target` must be absolute: `_ro_bind()` resolves its argument and the per-execution artifact dir is resolved at construction (the default `run_root` is the relative `Path("runs")`). Re-introducing a raw relative path into `containers.create` kwargs is a daemon 400 that the fake-client unit tests cannot catch (real-container runs only).
- `State.OOMKilled` for an OOM-killed *exec'd* process lags the kill (the daemon's bookkeeping catches up while the container stays `running`) — the backend reads it only after the exec-settle window. Don't "simplify" that into reading container state before the settle wait.
- The `docker` SDK is a hard dependency (`uv sync` installs it); a live Docker daemon is required only for sandbox-mode runs and `tests/test_sandbox_integration.py` (which auto-skips without one).
- `.agents/`, `.opencode/`, `__pycache__/`, and `*.egg-info/` are gitignored — do not commit them.
