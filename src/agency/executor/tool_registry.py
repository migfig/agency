"""In-code tool registry backing ``tool_call`` nodes (Story 5.3) and carrying
the model-facing tool metadata for spec 005 (executable tools & tool calling).

Tools are plain callables registered by name. A ``tool_call`` node resolves its
``arguments_template`` to a JSON object whose keys become the callable's keyword
arguments. The callable may be sync or async; its return value becomes the node
output. Every registered tool also carries a :class:`ToolSpec` (name,
description, parameters) used to offer tools to model-driven agent steps.

Only pure, side-effect-free string utilities are registered into
``DEFAULT_REGISTRY``. Callers may build their own :class:`ToolRegistry` to extend
or restrict the available tools (e.g. injecting tools with controlled I/O).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agency.executor.exec_tools import (
    ToolExecutionError,
    make_named_tool,
    named_tool_parameters,
)
from agency.executor.execution_env import (
    LocalToolBackend,
    ToolCaps,
    ToolExecutionBackend,
    parse_size,
)

if TYPE_CHECKING:
    from agency.yaml_engine.schema import ToolDefinition, Workflow

ToolFn = Callable[..., Any]

__all__ = [
    "BUILTIN_TOOL_NAMES",
    "DEFAULT_REGISTRY",
    "ToolFn",
    "ToolParam",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "build_workflow_tool_registry",
]


@dataclass(frozen=True)
class ToolParam:
    """One parameter of a model-facing tool spec."""

    type: str  # "string" | "number"
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """Model-facing metadata for one registered tool."""

    name: str
    description: str = ""
    parameters: dict[str, ToolParam] = field(default_factory=dict)

    def to_openai_tool(self) -> dict[str, Any]:
        """Render the OpenAI function-calling JSON object for this spec."""
        properties = {
            name: {"type": p.type, "description": p.description} for name, p in self.parameters.items()
        }
        required = [name for name, p in self.parameters.items() if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """Name-keyed registry of tool callables with per-tool metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}
        self._specs: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        fn: ToolFn,
        *,
        description: str = "",
        parameters: dict[str, ToolParam] | None = None,
    ) -> None:
        """Register *fn* under *name*, replacing any existing entry."""
        self._tools[name] = fn
        self._specs[name] = ToolSpec(name=name, description=description, parameters=dict(parameters or {}))

    def get(self, name: str) -> ToolFn | None:
        """Return the callable registered as *name*, or ``None``."""
        return self._tools.get(name)

    def spec(self, name: str) -> ToolSpec:
        """Return the model-facing metadata for *name*."""
        return self._specs[name]

    def specs(self) -> list[ToolSpec]:
        """Return all registered tool specs, sorted by name."""
        return [self._specs[name] for name in self.names()]

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        """Return sorted registered tool names."""
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


def _echo(text: str = "", **_: Any) -> str:
    """Return *text* unchanged."""
    return text


def _upper(text: str = "", **_: Any) -> str:
    return text.upper()


def _lower(text: str = "", **_: Any) -> str:
    return text.lower()


def _trim(text: str = "", **_: Any) -> str:
    return text.strip()


def _length(text: str = "", **_: Any) -> str:
    return str(len(text))


def _replace(text: str = "", old: str = "", new: str = "", **_: Any) -> str:
    return text.replace(old, new)


def _text(description: str) -> ToolParam:
    return ToolParam(type="string", required=True, description=description)


def _register_string_tools(registry: ToolRegistry) -> None:
    registry.register("echo", _echo, description="Return the text unchanged.", parameters={"text": _text("The input text.")})
    registry.register("upper", _upper, description="Return the text upper-cased.", parameters={"text": _text("The input text.")})
    registry.register("lower", _lower, description="Return the text lower-cased.", parameters={"text": _text("The input text.")})
    registry.register("trim", _trim, description="Return the text with surrounding whitespace removed.", parameters={"text": _text("The input text.")})
    registry.register("length", _length, description="Return the character count of the text.", parameters={"text": _text("The input text.")})
    registry.register(
        "replace",
        _replace,
        description="Return the text with every occurrence of 'old' replaced by 'new'.",
        parameters={
            "text": _text("The input text."),
            "old": _text("The substring to find."),
            "new": _text("The replacement substring."),
        },
    )


def _register_generic_tools(registry: ToolRegistry, backend: ToolExecutionBackend) -> None:
    """Register the always-present ``shell``/``program``/``write_file`` tools,
    routing each execution through *backend* (spec 006)."""

    async def shell_tool(
        command: str,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        result = await backend.shell(command, working_dir=working_dir, timeout_seconds=timeout_seconds)
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    async def program_tool(
        program: str,
        interpreter: str | None = None,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        result = await backend.program(
            program, interpreter=interpreter, working_dir=working_dir, timeout_seconds=timeout_seconds
        )
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    async def write_file_tool(path: str, content: str) -> Any:
        result = await backend.file_write(path, content)
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    working_dir = ToolParam(type="string", required=False, description="Working directory for the run.")
    timeout = ToolParam(
        type="number",
        required=False,
        description="Kill the run if it exceeds this many seconds (omitted = no timeout).",
    )
    registry.register(
        "shell",
        shell_tool,
        description="Run a shell command via bash and capture its output.",
        parameters={
            "command": ToolParam(type="string", required=True, description="The command line to run."),
            "working_dir": working_dir,
            "timeout_seconds": timeout,
        },
    )
    registry.register(
        "program",
        program_tool,
        description="Run a script or program file and capture its output.",
        parameters={
            "program": ToolParam(type="string", required=True, description="Path to the file to execute."),
            "interpreter": ToolParam(
                type="string",
                required=False,
                description="Interpreter to run it with (defaults by extension: .py/.sh).",
            ),
            "working_dir": working_dir,
            "timeout_seconds": timeout,
        },
    )
    registry.register(
        "write_file",
        write_file_tool,
        description="Write content to a file, creating parent directories.",
        parameters={
            "path": ToolParam(type="string", required=True, description="Target file path."),
            "content": ToolParam(type="string", required=True, description="Content to write."),
        },
    )


# The single source of truth for FR-017 duplicate detection: the six string
# utilities plus the always-present generic execution tools.
BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {"echo", "upper", "lower", "trim", "length", "replace", "shell", "program", "write_file"}
)


def build_default_registry() -> ToolRegistry:
    """Return a fresh registry populated with the pure built-in tools."""
    registry = ToolRegistry()
    _register_string_tools(registry)
    return registry


DEFAULT_REGISTRY: ToolRegistry = build_default_registry()


def _named_tool_caps(definition: ToolDefinition) -> ToolCaps | None:
    """The named tool's declared per-tool caps (FR-005), or ``None`` if it declares none."""
    memory_bytes = parse_size(definition.memory) if definition.memory is not None else None
    caps = ToolCaps(cpu=definition.cpu, memory_bytes=memory_bytes, pids_limit=definition.pids_limit)
    if caps.cpu is None and caps.memory_bytes is None and caps.pids_limit is None:
        return None
    return caps


def build_workflow_tool_registry(
    workflow: Workflow,
    *,
    execution_backend: ToolExecutionBackend | None = None,
) -> ToolRegistry:
    """Return the per-workflow registry (spec 005).

    Composed in order: the built-in string utilities, the always-present
    generic execution tools (``shell``/``program``/``write_file``), and the
    workflow's named tools from its ``tools:`` section (parameters derived from
    the templates' ``{name}`` placeholders, plus the implicit ``content`` for
    file_write).

    All executions route through *execution_backend* (spec 006) — the local
    backend by default (today's behavior, byte-for-byte) or an injected
    sandbox backend. Named tools pass their declared per-tool caps.
    """
    backend = execution_backend if execution_backend is not None else LocalToolBackend()
    registry = ToolRegistry()
    _register_string_tools(registry)
    _register_generic_tools(registry, backend)
    for name, definition in workflow.tools.items():
        parameters = {
            p: ToolParam(type="string", required=True, description=f"Tool argument '{p}'.")
            for p in named_tool_parameters(definition)
        }
        registry.register(
            name,
            make_named_tool(definition, backend=backend, caps=_named_tool_caps(definition)),
            description=definition.description or f"Named tool '{name}'.",
            parameters=parameters,
        )
    return registry
