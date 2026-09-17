from __future__ import annotations

import re
import string
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_SUMMARIZE_THRESHOLD = 80
DEFAULT_CONTEXT_WINDOW = 8192
DEFAULT_TOOL_MAX_ROUNDS = 4

_SIZE_PATTERN = re.compile(r"^\s*[\d.]+\s*(GB|MB|KB|B)\s*$")


def _validate_size(value: str | None) -> str | None:
    if value is None:
        return value
    if not _SIZE_PATTERN.match(value):
        raise ValueError(f"Invalid size format: '{value}'. Expected format like '512MB', '1GB', etc.")
    return value


def _builtin_tool_names() -> frozenset[str]:
    # Imported here (not at module top) because ``agency.executor`` pulls in
    # ``node_runners``, which imports this module — a top-level import would be
    # circular (same lazy-import pattern as ``validate_no_cycles``).
    from agency.executor.tool_registry import BUILTIN_TOOL_NAMES

    return BUILTIN_TOOL_NAMES


def _validate_tool_template(value: str) -> str:
    """Reject malformed ``{name}`` tool templates at load time (strict str.format)."""
    try:
        fields = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise ValueError(f"malformed tool template '{value}': {exc}") from exc
    for _, field_name, _, _ in fields:
        # parse() yields field_name=None for literal text parts; an empty
        # field_name is an auto-numbered "{}" field, which is not a named
        # {placeholder} — reject it so only named parameters survive.
        if field_name is not None and not field_name:
            raise ValueError(
                f"tool template '{value}' must use named {{placeholder}} fields, not auto-numbered ones"
            )
    return value


class Phase(BaseModel):
    id: str
    name: str
    node_ids: list[str] = Field(default_factory=list)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(ge=1)
    backoff: Literal["exponential"] = "exponential"
    base_delay_seconds: float = Field(ge=0, default=1.0)
    max_delay_seconds: float | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)


class ShellToolDefinition(BaseModel):
    """Named tool: run a shell command via ``bash -c`` (spec 005)."""

    kind: Literal["shell"]
    command: str
    working_dir: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    description: str = ""
    cpu: float | None = Field(default=None, gt=0)
    memory: str | None = None
    pids_limit: int | None = Field(default=None, gt=0)

    @field_validator("command")
    @classmethod
    def validate_command_template(cls, v: str) -> str:
        return _validate_tool_template(v)

    @field_validator("memory")
    @classmethod
    def validate_memory_size(cls, v: str | None) -> str | None:
        return _validate_size(v)


class ProgramToolDefinition(BaseModel):
    """Named tool: run a script/program file (spec 005)."""

    kind: Literal["program"]
    program: str
    interpreter: str | None = None
    working_dir: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    description: str = ""
    cpu: float | None = Field(default=None, gt=0)
    memory: str | None = None
    pids_limit: int | None = Field(default=None, gt=0)

    @field_validator("program")
    @classmethod
    def validate_program_template(cls, v: str) -> str:
        return _validate_tool_template(v)

    @field_validator("memory")
    @classmethod
    def validate_memory_size(cls, v: str | None) -> str | None:
        return _validate_size(v)


class FileWriteToolDefinition(BaseModel):
    """Named tool: write the ``content`` argument to a file (spec 005)."""

    kind: Literal["file_write"]
    path: str
    description: str = ""
    cpu: float | None = Field(default=None, gt=0)
    memory: str | None = None
    pids_limit: int | None = Field(default=None, gt=0)

    @field_validator("path")
    @classmethod
    def validate_path_template(cls, v: str) -> str:
        return _validate_tool_template(v)

    @field_validator("memory")
    @classmethod
    def validate_memory_size(cls, v: str | None) -> str | None:
        return _validate_size(v)


ToolDefinition = Annotated[
    ShellToolDefinition | ProgramToolDefinition | FileWriteToolDefinition,
    Field(discriminator="kind"),
]


class SandboxConfig(BaseModel):
    """Workflow-level sandbox bound defaults (spec 006, contract §1.2).

    All keys are optional; unset keys take the built-in defaults (contract
    §1.2) at resolution time. ``timeout_seconds`` is the run-level hard
    wall-clock default — an invocation's own ``timeout_seconds`` (the existing
    per-tool setting) still wins for that invocation.
    """

    cpu: float | None = Field(default=None, gt=0)
    memory: str | None = None
    pids_limit: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    output_limit_bytes: int | None = Field(default=None, gt=0)
    scratch_size: str | None = None

    @field_validator("memory", "scratch_size")
    @classmethod
    def validate_size_fields(cls, v: str | None) -> str | None:
        return _validate_size(v)


class AgentTools(BaseModel):
    """Opt-in model-driven tool calling on an agent node (spec 005).

    Absent (``None``) means the node behaves exactly as before the feature.
    ``allow`` restricts the offered tools to the named subset (``None`` offers
    all available tools); ``max_rounds`` bounds the tool round-trips.
    """

    allow: list[str] | None = None
    max_rounds: int = Field(default=DEFAULT_TOOL_MAX_ROUNDS, ge=1)


class AgentNode(BaseModel):
    id: str
    type: Literal["agent"]
    model: str
    prompt_template: str
    system_prompt: str | None = None
    temperature: float | None = None
    retry: RetryPolicy | None = None
    fallback: str | None = Field(default=None, min_length=1)
    tools: AgentTools | None = None


class ConditionalNode(BaseModel):
    id: str
    type: Literal["conditional"]
    condition: str
    branches: dict[str, str]


class MergeNode(BaseModel):
    id: str
    type: Literal["merge"]
    inputs: list[str] = Field(min_length=1)
    strategy: Literal["any", "all"]


class BroadcastNode(BaseModel):
    id: str
    type: Literal["broadcast"]
    targets: list[str] = Field(min_length=1)


class HumanInLoopNode(BaseModel):
    id: str
    type: Literal["human_in_loop"]
    prompt: str
    timeout_seconds: int | None = None


class ToolCallNode(BaseModel):
    id: str
    type: Literal["tool_call"]
    tool_name: str
    arguments_template: str
    retry: RetryPolicy | None = None
    fallback: str | None = Field(default=None, min_length=1)


Node = Annotated[
    AgentNode | ConditionalNode | MergeNode | BroadcastNode | HumanInLoopNode | ToolCallNode,
    Field(discriminator="type"),
]


class Edge(BaseModel):
    from_id: str
    to_id: str
    condition_label: str | None = None


class ModelSpec(BaseModel):
    """Declaration of a model: where it is served and how to bring it up.

    ``endpoint`` is the base URL of a llama.cpp (or compatible) server that
    serves this model. ``path`` is an optional local model file used to
    auto-start a server when the endpoint is unreachable. ``vram_size`` is the
    model's VRAM footprint, used for slot acquisition.
    """

    endpoint: str | None = None
    path: str | None = None
    vram_size: str | None = None
    startup_timeout: float | None = Field(default=60.0, gt=0)
    timeout_seconds: float | None = Field(default=60.0, gt=0)


class Workflow(BaseModel):
    name: str
    nodes: dict[str, Node]
    edges: list[Edge] = Field(default_factory=list)
    entry_point: str
    vram_limit: str | None = None
    phases: list[Phase] = Field(default_factory=list)
    summarize_threshold: int | None = Field(default=None, ge=1, le=100)
    context_window: int | None = Field(default=None, gt=0)
    summarizer_model: str | None = None
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    tools: dict[str, ToolDefinition] = Field(default_factory=dict)
    execution_environment: Literal["sandbox", "local"] | None = None
    sandbox: SandboxConfig | None = None

    @field_validator("vram_limit")
    @classmethod
    def validate_vram_limit(cls, v: str | None) -> str | None:
        if v is None:
            return v
        pattern = r"^\s*[\d.]+\s*(GB|MB|KB|B)\s*$"
        if not re.match(pattern, v, re.IGNORECASE):
            raise ValueError(f"Invalid VRAM size format: '{v}'. Expected format like '14GB', '8000MB', etc.")
        return v

    @field_validator("summarizer_model")
    @classmethod
    def validate_summarizer_model(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("summarizer_model must be a non-blank model name when set")
        return v

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Workflow:
        seen: set[str] = set()
        for node in self.nodes.values():
            if node.id in seen:
                raise ValueError(f"Duplicate node ID: {node.id}")
            seen.add(node.id)
        return self

    @model_validator(mode="after")
    def validate_keys_match_ids(self) -> Workflow:
        for key, node in self.nodes.items():
            if key != node.id:
                raise ValueError(f"Node key '{key}' does not match node ID '{node.id}'")
        return self

    @model_validator(mode="after")
    def validate_edge_references(self) -> Workflow:
        node_ids = set(self.nodes.keys())
        for edge in self.edges:
            if edge.from_id == edge.to_id:
                raise ValueError(f"Edge cannot reference itself: from_id='{edge.from_id}' to_id='{edge.to_id}'")
            if edge.from_id not in node_ids:
                raise ValueError(f"Edge references non-existent node: from_id='{edge.from_id}'")
            if edge.to_id not in node_ids:
                raise ValueError(f"Edge references non-existent node: to_id='{edge.to_id}'")
        return self

    @model_validator(mode="after")
    def validate_branch_targets(self) -> Workflow:
        node_ids = set(self.nodes.keys())
        for node in self.nodes.values():
            if isinstance(node, ConditionalNode):
                for label, target in node.branches.items():
                    if target not in node_ids:
                        raise ValueError(
                            f"Conditional node '{node.id}' branch '{label}' references non-existent node '{target}'"
                        )
        return self

    @model_validator(mode="after")
    def validate_no_cycles(self) -> Workflow:
        from agency.yaml_engine.dag import CycleDetectedError, DAGBuilder

        cycle = DAGBuilder(self).detect_cycle()
        if cycle is not None:
            raise CycleDetectedError(cycle)
        return self

    @model_validator(mode="after")
    def validate_entry_point(self) -> Workflow:
        if self.entry_point not in self.nodes:
            raise ValueError(f"Entry point '{self.entry_point}' not found in nodes")
        return self

    @model_validator(mode="after")
    def validate_phase_node_ids(self) -> Workflow:
        node_ids = set(self.nodes.keys())
        for phase in self.phases:
            for nid in phase.node_ids:
                if nid not in node_ids:
                    raise ValueError(
                        f"Phase '{phase.id}' references non-existent node '{nid}'"
                    )
        return self

    @model_validator(mode="after")
    def validate_tool_names(self) -> Workflow:
        # Tool names are global within a workflow: a collision with a built-in
        # tool is a duplicate (FR-017). Two author tools cannot share a name
        # (YAML mapping keys are unique), so this is the whole of FR-017.
        builtin = _builtin_tool_names()
        for name in self.tools:
            if name in builtin:
                raise ValueError(f"duplicate tool name '{name}': collides with built-in tool '{name}'")
        return self

    @model_validator(mode="after")
    def validate_tool_allow_lists(self) -> Workflow:
        available = _builtin_tool_names() | set(self.tools)
        for node in self.nodes.values():
            if isinstance(node, AgentNode) and node.tools is not None and node.tools.allow is not None:
                for entry in node.tools.allow:
                    if entry not in available:
                        raise ValueError(
                            f"agent node '{node.id}' allow-list references unknown tool '{entry}'"
                        )
        return self
