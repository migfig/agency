"""Tests for tool metadata and the per-workflow registry (spec 005, T004).

Covers ToolParam/ToolSpec shapes, to_openai_tool() JSON shape,
BUILTIN_TOOL_NAMES, build_workflow_tool_registry(workflow), and the
back-compatible register()/get() behavior.
"""

from __future__ import annotations

import dataclasses

import pytest

from agency.executor.exec_tools import ToolExecutionResult
from agency.executor.execution_env import ToolCaps
from agency.executor.tool_registry import (
    BUILTIN_TOOL_NAMES,
    DEFAULT_REGISTRY,
    ToolParam,
    ToolRegistry,
    ToolSpec,
    build_workflow_tool_registry,
)
from agency.yaml_engine.schema import Workflow

# -- ToolParam / ToolSpec shapes --------------------------------------------


def test_tool_param_shape():
    param = ToolParam(type="string", required=True, description="d")
    assert param.type == "string"
    assert param.required is True
    assert param.description == "d"


def test_tool_param_is_frozen():
    param = ToolParam(type="string", required=True, description="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        param.type = "number"  # type: ignore[misc]


def test_tool_spec_shape():
    spec = ToolSpec(
        name="lint",
        description="Lint a file",
        parameters={"file": ToolParam(type="string", required=True, description="the file")},
    )
    assert spec.name == "lint"
    assert spec.description == "Lint a file"
    assert set(spec.parameters) == {"file"}


def test_to_openai_tool_json_shape():
    spec = ToolSpec(
        name="lint",
        description="Lint a file",
        parameters={
            "file": ToolParam(type="string", required=True, description="the file"),
            "retries": ToolParam(type="number", required=False, description="tries"),
        },
    )
    tool = spec.to_openai_tool()
    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "lint"
    assert function["description"] == "Lint a file"
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert parameters["properties"]["file"] == {"type": "string", "description": "the file"}
    assert parameters["properties"]["retries"]["type"] == "number"
    assert parameters["required"] == ["file"]


# -- BUILTIN_TOOL_NAMES ------------------------------------------------------


def test_builtin_tool_names_exact():
    assert BUILTIN_TOOL_NAMES == frozenset(
        {
            "echo",
            "upper",
            "lower",
            "trim",
            "length",
            "replace",
            "shell",
            "program",
            "write_file",
        }
    )


# -- build_workflow_tool_registry --------------------------------------------


def _workflow_with_tools() -> Workflow:
    return Workflow.model_validate(
        {
            "name": "w",
            "entry_point": "call",
            "nodes": {
                "call": {
                    "id": "call",
                    "type": "tool_call",
                    "tool_name": "echo",
                    "arguments_template": "{}",
                },
            },
            "tools": {
                "lint_python": {
                    "kind": "shell",
                    "command": "ruff check {file}",
                    "description": "Lint a Python file",
                },
                "save_program": {"kind": "file_write", "path": "programs/{name}.py"},
                "run_python": {"kind": "program", "program": "{file}", "interpreter": "python"},
            },
        }
    )


def test_registry_has_builtins_generics_and_named():
    reg = build_workflow_tool_registry(_workflow_with_tools())
    names = set(reg.names())
    assert BUILTIN_TOOL_NAMES <= names
    assert {"lint_python", "save_program", "run_python"} <= names


def test_named_tool_parameters_derived_from_placeholders():
    reg = build_workflow_tool_registry(_workflow_with_tools())
    assert list(reg.spec("lint_python").parameters) == ["file"]
    assert list(reg.spec("run_python").parameters) == ["file"]
    # file_write carries the implicit required content parameter
    assert list(reg.spec("save_program").parameters) == ["name", "content"]
    assert reg.spec("save_program").parameters["content"].required is True
    assert reg.spec("save_program").parameters["name"].type == "string"


def test_generic_tool_parameter_sets():
    reg = build_workflow_tool_registry(_workflow_with_tools())
    assert list(reg.spec("shell").parameters) == ["command", "working_dir", "timeout_seconds"]
    assert reg.spec("shell").parameters["command"].required is True
    assert reg.spec("shell").parameters["timeout_seconds"].type == "number"
    assert reg.spec("shell").parameters["working_dir"].required is False
    assert list(reg.spec("program").parameters) == [
        "program",
        "interpreter",
        "working_dir",
        "timeout_seconds",
    ]
    assert list(reg.spec("write_file").parameters) == ["path", "content"]


def test_registry_spec_and_specs_listing():
    reg = build_workflow_tool_registry(_workflow_with_tools())
    assert reg.spec("lint_python").name == "lint_python"
    specs = reg.specs()
    assert all(isinstance(s, ToolSpec) for s in specs)
    assert [s.name for s in specs] == reg.names()


# -- Back-compatibility (SC-008) ---------------------------------------------


def test_register_get_back_compatible():
    reg = ToolRegistry()

    def boom(**_):
        return "x"

    reg.register("boom", boom)
    assert reg.get("boom") is boom
    assert reg.is_registered("boom")
    assert reg.names() == ["boom"]
    assert len(reg) == 1
    assert reg.get("missing") is None


def test_default_registry_keeps_six_string_tools():
    assert DEFAULT_REGISTRY.names() == ["echo", "length", "lower", "replace", "trim", "upper"]
    assert DEFAULT_REGISTRY.get("upper")("x") == "X"
    assert DEFAULT_REGISTRY.get("replace")("a b", old="b", new="c") == "a c"


# -- spec 006 (T004): backend injection + per-tool caps -----------------------


class _RecordingBackend:
    """Records protocol calls and returns canned sandbox results (no I/O)."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def shell(self, command, working_dir=None, timeout_seconds=None, caps=None):
        self.calls.append(("shell", command, working_dir, timeout_seconds, caps))
        return ToolExecutionResult(
            status="success",
            exit_code=0,
            output=f"fake-shell:{command}",
            stderr="",
            error=None,
            duration_seconds=0.0,
            command=command,
            kind="shell",
            environment="sandbox",
        )

    async def program(self, program, interpreter=None, working_dir=None, timeout_seconds=None, caps=None):
        self.calls.append(("program", program, interpreter, working_dir, timeout_seconds, caps))
        return ToolExecutionResult(
            status="success",
            exit_code=0,
            output=f"fake-program:{program}",
            stderr="",
            error=None,
            duration_seconds=0.0,
            command=program,
            kind="program",
            environment="sandbox",
        )

    async def file_write(self, path, content, caps=None):
        self.calls.append(("file_write", path, content, caps))
        return ToolExecutionResult(
            status="success",
            exit_code=None,
            output=path,
            stderr="",
            error=None,
            duration_seconds=0.0,
            command=f"write to '{path}'",
            kind="file_write",
            environment="sandbox",
        )


async def test_builtins_route_through_injected_backend():
    backend = _RecordingBackend()
    reg = build_workflow_tool_registry(_workflow_with_tools(), execution_backend=backend)
    result = await reg.get("shell")(command="echo routed", timeout_seconds=3.0)
    assert backend.calls == [("shell", "echo routed", None, 3.0, None)]
    assert result.environment == "sandbox"
    assert result.as_dict()["environment"] == "sandbox"
    result = await reg.get("program")(program="/x.py", interpreter="python3")
    assert backend.calls[-1] == ("program", "/x.py", "python3", None, None, None)
    assert result.environment == "sandbox"
    result = await reg.get("write_file")(path="/out.txt", content="c")
    assert backend.calls[-1] == ("file_write", "/out.txt", "c", None)
    assert result.environment == "sandbox"


async def test_registry_defaults_to_local_backend_when_none():
    reg = build_workflow_tool_registry(_workflow_with_tools())
    result = await reg.get("shell")(command="echo default-local")
    assert result.status == "success"
    assert "default-local" in result.output
    assert result.environment == "local"
    assert result.as_dict()["environment"] == "local"


async def test_named_tools_pass_declared_caps_to_backend():
    workflow = Workflow.model_validate(
        {
            "name": "w",
            "entry_point": "call",
            "nodes": {
                "call": {
                    "id": "call",
                    "type": "tool_call",
                    "tool_name": "echo",
                    "arguments_template": "{}",
                },
            },
            "tools": {
                "heavy": {
                    "kind": "shell",
                    "command": "python3 heavy.py {input}",
                    "cpu": 4,
                    "memory": "1GB",
                    "pids_limit": 512,
                },
                "plain": {"kind": "shell", "command": "echo {input}"},
            },
        }
    )
    backend = _RecordingBackend()
    reg = build_workflow_tool_registry(workflow, execution_backend=backend)
    await reg.get("heavy")(input="x")
    assert backend.calls == [
        ("shell", "python3 heavy.py x", None, None, ToolCaps(cpu=4.0, memory_bytes=1024**3, pids_limit=512))
    ]
    await reg.get("plain")(input="y")
    assert backend.calls[-1] == ("shell", "echo y", None, None, None)
