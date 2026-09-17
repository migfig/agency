"""Pre-execution model validation and provisioning.

A single pass that runs before any node executes or process spawns. It merges
the workflow's ``models:`` section with an optional ``--models`` file, resolves
every agent-referenced model to a live endpoint (auto-starting a local
llama.cpp server when the declared endpoint is unreachable and carries a local
path), validates ``agent.fallback`` references, and collapses every failure
into one report.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from agency.core.llamacpp_backend import LlamaCppError, LlamaCppServer
from agency.resource_manager.backend_router import BackendRouter
from agency.resource_manager.vram_tracker import parse_vram_size
from agency.yaml_engine.schema import AgentNode, ModelSpec, Workflow

logger = logging.getLogger(__name__)

_DEFAULT_VRAM_BYTES: int = parse_vram_size("4GB")
_PROBE_TIMEOUT_SECONDS: float = 5.0
_ALLOWED_SPEC_FIELDS = frozenset({"endpoint", "path", "vram_size"})

# Built-in fallback for ``AGENCY_MODELS_DIRS`` when the variable is unset or
# blank: the standard llama.cpp model directory. Element 0 is primary.
_DEFAULT_MODELS_DIRS = ("~/.llama-cpp/models",)


def get_models_dirs() -> list[Path]:
    """Return the ordered model directories to search for model files.

    Reads ``AGENCY_MODELS_DIRS`` — a colon-separated list of directories.
    Element 0 is the primary/default location. Falls back to the built-in
    default (``~/.llama-cpp/models``) when the variable is unset or blank.
    """
    raw = os.environ.get("AGENCY_MODELS_DIRS", "")
    dirs = [Path(os.path.expandvars(p)).expanduser() for p in raw.split(":") if p.strip()]
    return dirs or [Path(d).expanduser() for d in _DEFAULT_MODELS_DIRS]


def resolve_model_path(declared: str) -> Path:
    """Resolve a declared model ``path`` to a concrete filesystem path.

    ``$VAR``/``${VAR}`` and a leading ``~`` are expanded first. An absolute
    path is returned unchanged. A relative path (a model filename, optionally
    with a subdirectory) is resolved against :func:`get_models_dirs` in order:
    the first directory that contains the file wins. When no directory contains
    it, the primary (first) directory is joined and returned — callers can
    detect the miss with ``.is_file()`` and report every searched directory.
    """
    p = Path(os.path.expandvars(os.path.expanduser(declared)))
    if p.is_absolute():
        return p
    dirs = get_models_dirs()
    for d in dirs:
        candidate = d / p
        if candidate.is_file():
            return candidate
    return dirs[0] / p


class ProvisionError(Exception):
    """Raised when pre-execution validation cannot resolve every referenced model.

    Every failure is collected into ``failures`` so a misconfigured run aborts
    before any node runs or process spawns, with a single consolidated report.
    """

    def __init__(self, failures: list[str] | str) -> None:
        if isinstance(failures, str):
            failures = [failures]
        self.failures = failures
        super().__init__(" | ".join(failures))


@dataclass
class ProvisionedRun:
    """The fully-resolved pre-execution state for a run.

    Holds the router (every referenced model registered exactly once) and the
    list of owned servers so the CLI can shut down Agency-started servers.
    """

    workflow: Workflow
    router: BackendRouter
    models: dict[str, ModelSpec]
    vram_sizes: dict[str, int]
    owned_servers: list[LlamaCppServer] = field(default_factory=list)

    @property
    def model_timeouts(self) -> dict[str, float]:
        """Per-model inference timeouts from the merged models section.

        ``ModelSpec.timeout_seconds`` defaults to 60.0, so every declared
        model carries an explicit value; the runner falls back to its own
        built-in default for any model not present here.
        """
        return {
            name: spec.timeout_seconds
            for name, spec in self.models.items()
            if spec.timeout_seconds is not None
        }


def load_cli_models(path: Path) -> dict[str, ModelSpec]:
    """Load a ``--models FILE`` override into a ``name -> ModelSpec`` map.

    The file must be a mapping of model name to a mapping with any of the keys
    ``endpoint``/``path``/``vram_size`` (all string values).

    Raises:
        ProvisionError: If the file is not valid YAML, not a mapping of
            mappings, a value is not a string, or an unknown field is present.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProvisionError(f"--models file '{path}' is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ProvisionError(f"--models file '{path}' could not be read: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProvisionError(
            f"--models file '{path}' must be a mapping of model name to "
            f"endpoint/path/vram_size, got {type(raw).__name__}"
        )

    specs: dict[str, ModelSpec] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ProvisionError(
                f"--models entry '{name}' must be a mapping (endpoint/path/vram_size), "
                f"got {type(entry).__name__}"
            )
        extra = sorted(set(entry) - _ALLOWED_SPEC_FIELDS)
        if extra:
            raise ProvisionError(
                f"--models entry '{name}' has unknown field(s): {', '.join(extra)}"
            )
        for key in ("endpoint", "path", "vram_size"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise ProvisionError(
                    f"--models field '{name}.{key}' must be a string, "
                    f"got {type(value).__name__}"
                )
        specs[str(name)] = ModelSpec(
            endpoint=entry.get("endpoint"),
            path=entry.get("path"),
            vram_size=entry.get("vram_size"),
        )
    return specs


def _host_port(endpoint: str) -> tuple[str, int]:
    """Return (host, port) for an endpoint base URL, applying scheme defaults."""
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


async def _probe_reachable(endpoint: str, timeout: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """Read-only reachability probe.

    Any HTTP response counts as reachable (the server is up, however it reacts
    to the request body). Only connection-level failure (DNS, refused, timeout)
    counts as unreachable. No inference is performed by this probe.
    """
    url = endpoint if endpoint.endswith("/") else endpoint + "/"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        return False
    return True


# One tiny tool offered to the probe endpoint (FR-016). A model that can
# execute tool calls answers with a non-empty ``tool_calls`` array.
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Send a short ping message.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "The ping text."}},
            "required": ["message"],
        },
    },
}


async def _probe_tool_capability(
    endpoint: str, model: str, timeout: float = _PROBE_TIMEOUT_SECONDS
) -> bool:
    """One-shot tool-call capability probe (FR-016).

    POSTs a chat completion with the single ``ping`` tool and
    ``tool_choice="auto"``. Pass = HTTP 200 and a non-empty ``tool_calls``
    array in the response message; anything else (connection failure, HTTP
    error, timeout, non-JSON body, no tool call) counts as
    "cannot execute tool calls".
    """
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Use the ping tool now. Send the message 'probe'."}],
        "tools": [_PROBE_TOOL],
        "tool_choice": "auto",
        "max_tokens": 64,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=body)
            if response.status_code != 200:
                return False
            data = response.json()
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        return False
    try:
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls")
    except (KeyError, IndexError, TypeError):
        return False
    return isinstance(tool_calls, list) and len(tool_calls) > 0


def _resolve_vram_size(name: str, entry: ModelSpec, failures: list[str]) -> int:
    """Return the model's vram_size in bytes, defaulting to 4GB with a log line."""
    if entry.vram_size is None:
        logger.info("model '%s': no vram_size declared; using 4 GB default", name)
        return _DEFAULT_VRAM_BYTES
    try:
        return parse_vram_size(entry.vram_size)
    except ValueError as exc:
        failures.append(f"model '{name}' has invalid vram_size '{entry.vram_size}': {exc}")
        return _DEFAULT_VRAM_BYTES


async def provision(
    workflow: Workflow,
    cli_models: dict[str, ModelSpec] | None,
    router: BackendRouter,
    *,
    capability_probe: Callable[[str, str], Awaitable[bool]] | None = None,
) -> ProvisionedRun:
    """Run the single pre-execution validation/provisioning pass.

    Merges the workflow's ``models:`` section with a ``--models`` override,
    collects every failure (collisions, bad fallback references, unresolvable
    models, failed tool-call capability probes), and — only when there are zero
    failures — starts any owned servers and registers every referenced model
    with the router exactly once.

    Args:
        workflow: The validated workflow (``workflow.models`` is the models section).
        cli_models: The parsed ``--models`` override, or ``None``/empty.
        router: A backend router to register every resolvable model into.
        capability_probe: Async ``(endpoint, model) -> bool`` used to check
            tool-call capability (FR-016). Defaults to the module's real HTTP
            probe (``_probe_tool_capability``).

    Returns:
        A ``ProvisionedRun`` ready for execution (owned servers started
        and every referenced model registered).

    Raises:
        ProvisionError: With a full failure list if validation fails. No models
            are registered on this path; any Agency-started servers are torn
            down before the raise.
    """
    workflow_models: dict[str, ModelSpec] = dict(workflow.models)
    overrides: dict[str, ModelSpec] = dict(cli_models or {})

    # --- merge + collision (no I/O) ---------------------------------------
    merged: dict[str, ModelSpec] = {**workflow_models, **overrides}
    failures: list[str] = [
        f"model '{name}' is declared in both the workflow models: section and "
        f"the --models file; remove it from one source"
        for name in sorted(set(workflow_models) & set(overrides))
    ]

    nodes = workflow.nodes
    agent_nodes = [node for node in nodes.values() if isinstance(node, AgentNode)]
    required = [node.model for node in agent_nodes]
    required_set = set(required)

    # --- fallback reference validation (no I/O) ---------------------------
    for node in agent_nodes:
        ref = node.fallback
        if ref is None:
            continue
        target = nodes.get(ref)
        if not isinstance(target, AgentNode):
            failures.append(
                f"agent '{node.id}' declares fallback '{ref}' which is not an "
                f"existing agent-type node"
            )

    # --- resolve referenced models (read-only reachability probes) --------
    vram_sizes: dict[str, int] = {}
    resolved: dict[str, str] = {}        # referenced model -> live endpoint
    pending_spawn: dict[str, str] = {}   # referenced model -> endpoint to start
    resolved_paths: dict[str, Path] = {}  # referenced model -> resolved model file
    for name in sorted(required_set):
        entry = merged.get(name)
        if entry is None:
            failures.append(
                f"model '{name}' is referenced by an agent node but declared in "
                f"neither the workflow models: section nor the --models file"
            )
            continue

        endpoint = entry.endpoint
        if endpoint is None:
            failures.append(
                f"model '{name}' has a local path but no endpoint base URL; an "
                f"endpoint is required to know which port the model is served on"
            )
            continue

        if await _probe_reachable(endpoint):
            resolved[name] = endpoint
        elif entry.path is not None:
            model_file = resolve_model_path(entry.path)
            declared_absolute = Path(os.path.expandvars(os.path.expanduser(entry.path))).is_absolute()
            # A relative (portable) path that resolves to no file anywhere is a
            # configuration error: fail fast with every searched directory.
            # Absolute paths are trusted as-is (spawn owns their lifecycle).
            if not declared_absolute and not model_file.is_file():
                searched = ", ".join(str(d) for d in get_models_dirs())
                failures.append(
                    f"model '{name}' file '{Path(entry.path).name}' not found in any "
                    f"of: {searched}"
                )
            else:
                pending_spawn[name] = endpoint
                resolved_paths[name] = model_file
        else:
            failures.append(
                f"model '{name}' endpoint '{endpoint}' is unreachable and no local "
                f"model path is declared to auto-start it"
            )

        vram_sizes[name] = _resolve_vram_size(name, entry, failures)

    # --- start owned servers (mutating; skipped when validation already failed) --
    owned_servers: list[LlamaCppServer] = []
    servers_by_hostport: dict[tuple[str, int], LlamaCppServer] = {}
    for name in (sorted(pending_spawn) if not failures else []):
        endpoint = pending_spawn[name]
        host, port = _host_port(endpoint)
        server = servers_by_hostport.get((host, port))
        if server is None:
            model_file = resolved_paths[name]
            startup_timeout = merged[name].startup_timeout
            server = LlamaCppServer(
                port=port, model_path=str(model_file), startup_timeout=startup_timeout
            )
            try:
                await asyncio.to_thread(server.start)
            except (LlamaCppError, OSError) as exc:
                for started in owned_servers:
                    try:
                        started.shutdown()
                    except Exception:
                        logger.warning("teardown of already-started server failed", exc_info=True)
                raise ProvisionError(
                    f"model '{name}' could not be started on port {port} "
                    f"(path={model_file!r}): {exc}"
                ) from exc
            servers_by_hostport[(host, port)] = server
            owned_servers.append(server)
        resolved[name] = endpoint

    # --- FR-016: tool-call capability probe -------------------------------
    # Only models used by tool-enabled agent nodes are probed, once each, after
    # the gate's auto-start (a probe needs a live endpoint). Failures append
    # one line per affected (node, model) pair, alongside any other validation
    # failures already collected.
    probe = capability_probe if capability_probe is not None else _probe_tool_capability
    tool_nodes = [node for node in agent_nodes if node.tools is not None]
    probe_targets = {
        node.model: resolved[node.model]
        for node in tool_nodes
        if node.model in resolved
    }
    for name in sorted(probe_targets):
        if not await probe(probe_targets[name], name):
            for node in tool_nodes:
                if node.model == name:
                    failures.append(
                        f"agent node '{node.id}' enables tool calling but model "
                        f"'{name}' at {probe_targets[name]} failed the "
                        f"tool-call capability probe"
                    )

    # Abort with the consolidated report, tearing down any Agency-started server.
    if failures:
        for server in owned_servers:
            try:
                server.shutdown()
            except Exception:
                logger.warning("teardown of already-started server failed", exc_info=True)
        raise ProvisionError(list(failures))

    # --- register every referenced model exactly once ---------------------
    for name, endpoint in resolved.items():
        router.register(name, endpoint)

    return ProvisionedRun(
        workflow=workflow,
        router=router,
        models=merged,
        vram_sizes=vram_sizes,
        owned_servers=owned_servers,
    )


def teardown(run: ProvisionedRun) -> None:
    """Shut down every Agency-started (owned) server for a ``ProvisionedRun``.

    Externally-owned endpoints are never touched. Safe to call once on any
    completion/abort path that produced a run.
    """
    for server in run.owned_servers:
        try:
            server.shutdown()
        except Exception:
            logger.warning("failed to shut down owned llama.cpp server", exc_info=True)
