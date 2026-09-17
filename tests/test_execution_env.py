"""Tests for the execution-environment seam (spec 006, T002).

Covers ``resolve_environment`` precedence (contract §4: run supply > workflow
declaration > built-in default ``sandbox``) and the ``LocalToolBackend``
passthrough — each of ``shell``/``program``/``file_write`` delegates to the
existing ``exec_tools`` executors with the same arguments and returns a result
whose ``environment`` is ``"local"`` (SC-010 regression: local execution is
byte-for-byte today's behavior).
"""

from __future__ import annotations

import pytest

from agency.executor.exec_tools import ToolExecutionResult
from agency.executor.execution_env import (
    LocalToolBackend,
    ToolCaps,
    resolve_environment,
)

# -- resolve_environment (contract §4) ---------------------------------------


@pytest.mark.parametrize(
    ("run_value", "workflow_value", "expected"),
    [
        ("local", "sandbox", "local"),  # run supply wins
        ("sandbox", "local", "sandbox"),
        ("local", None, "local"),
        (None, "local", "local"),
        (None, "sandbox", "sandbox"),
        (None, None, "sandbox"),  # built-in default when absent everywhere
    ],
)
def test_resolve_environment_precedence(run_value, workflow_value, expected):
    assert resolve_environment(run_value, workflow_value) == expected


# -- LocalToolBackend: delegation to the exec_tools executors -----------------


def _stub_result(kind: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        status="success",
        exit_code=0,
        output="stubbed",
        stderr="",
        error=None,
        duration_seconds=0.0,
        command="stub",
        kind=kind,
        environment="local",
    )


@pytest.mark.parametrize("caps", [None, ToolCaps(cpu=1.0, memory_bytes=512 * 2**20)])
async def test_local_shell_delegates_with_same_args(monkeypatch, caps):
    calls: list[tuple] = []

    async def fake_shell(command, *, working_dir=None, timeout_seconds=None):
        calls.append((command, working_dir, timeout_seconds))
        return _stub_result("shell")

    monkeypatch.setattr("agency.executor.execution_env.execute_shell", fake_shell)
    result = await LocalToolBackend().shell("echo hi", working_dir="/wd", timeout_seconds=5.0, caps=caps)
    assert calls == [("echo hi", "/wd", 5.0)]
    assert result.environment == "local"
    assert result.output == "stubbed"


@pytest.mark.parametrize("caps", [None, ToolCaps(pids_limit=128)])
async def test_local_program_delegates_with_same_args(monkeypatch, caps):
    calls: list[tuple] = []

    async def fake_program(program, *, interpreter=None, working_dir=None, timeout_seconds=None):
        calls.append((program, interpreter, working_dir, timeout_seconds))
        return _stub_result("program")

    monkeypatch.setattr("agency.executor.execution_env.execute_program", fake_program)
    result = await LocalToolBackend().program(
        "prog.py", interpreter=None, working_dir="/wd", timeout_seconds=2.0, caps=caps
    )
    assert calls == [("prog.py", None, "/wd", 2.0)]
    assert result.environment == "local"


async def test_local_file_write_delegates_with_same_args(monkeypatch):
    calls: list[tuple] = []

    async def fake_file_write(path, content):
        calls.append((path, content))
        return _stub_result("file_write")

    monkeypatch.setattr("agency.executor.execution_env.execute_file_write", fake_file_write)
    result = await LocalToolBackend().file_write("/tmp/out.txt", "hello", caps=None)
    assert calls == [("/tmp/out.txt", "hello")]
    assert result.environment == "local"


# -- LocalToolBackend: SC-010 real-execution regression -----------------------


async def test_local_shell_real_execution_captures_output():
    result = await LocalToolBackend().shell("echo local-out")
    assert result.status == "success"
    assert result.exit_code == 0
    assert "local-out" in result.output
    assert result.environment == "local"
    assert result.as_dict()["environment"] == "local"


async def test_local_program_real_execution(tmp_path):
    script = tmp_path / "p.py"
    script.write_text("print('prog-out')\n", encoding="utf-8")
    result = await LocalToolBackend().program(str(script))
    assert result.status == "success"
    assert "prog-out" in result.output
    assert result.environment == "local"


async def test_local_file_write_real_execution(tmp_path):
    target = tmp_path / "out.txt"
    result = await LocalToolBackend().file_write(str(target), "content")
    assert result.status == "success"
    assert target.read_text(encoding="utf-8") == "content"
    assert result.output == str(target)
    assert result.environment == "local"
