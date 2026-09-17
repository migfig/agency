"""Tests for the shell/program/file-write executors (spec 005, T003).

Real local subprocesses only — deterministic commands (echo, false, sleep, a
tiny generated .py file), no network, no model.
"""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

from agency.executor.exec_tools import (
    ToolExecutionError,
    ToolExecutionResult,
    execute_file_write,
    execute_program,
    execute_shell,
    make_named_tool,
)
from agency.executor.execution_env import ToolCaps
from agency.yaml_engine.schema import (
    FileWriteToolDefinition,
    ProgramToolDefinition,
    ShellToolDefinition,
)

# -- execute_shell ----------------------------------------------------------


async def test_shell_success_captures_output():
    result = await execute_shell("echo hi")
    assert isinstance(result, ToolExecutionResult)
    assert result.status == "success"
    assert result.exit_code == 0
    assert "hi" in result.output
    assert result.error is None
    assert result.command == "echo hi"


async def test_shell_failure_captures_exit_and_stderr():
    result = await execute_shell("echo oops >&2; false")
    assert result.status == "failed"
    assert result.exit_code == 1
    assert "oops" in result.stderr
    assert result.error is not None


async def test_shell_timeout_kills_without_hang():
    start = time.monotonic()
    result = await execute_shell("sleep 5", timeout_seconds=1)
    elapsed = time.monotonic() - start
    assert result.status == "timed_out"
    assert result.exit_code is None
    assert "exceeded 1s timeout" in (result.error or "")
    assert elapsed < 4.0


# -- execute_program --------------------------------------------------------


async def test_program_python_by_extension(tmp_path):
    script = tmp_path / "tiny.py"
    script.write_text("print('py-out')\n", encoding="utf-8")
    result = await execute_program(str(script))
    assert result.status == "success"
    assert result.exit_code == 0
    assert "py-out" in result.output


async def test_program_runtime_error_reported_not_raised(tmp_path):
    # US3: a runtime error in the saved program is reported by the execute
    # step (failed result), never raised through the executor.
    script = tmp_path / "broken.py"
    script.write_text("raise ValueError('boom-runtime')\n", encoding="utf-8")
    result = await execute_program(str(script))
    assert result.status == "failed"
    assert result.exit_code == 1
    assert "ValueError" in result.stderr
    assert result.error is not None


# -- execute_file_write -------------------------------------------------------


async def test_file_write_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.txt"
    result = await execute_file_write(str(target), "hello")
    assert result.status == "success"
    assert result.error is None
    assert target.read_text(encoding="utf-8") == "hello"
    # the success result carries the written path (the node output becomes it)
    assert result.output == str(target)


async def test_file_write_unwritable_location_is_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        result = await execute_file_write(str(locked / "out.txt"), "x")
    finally:
        os.chmod(locked, 0o755)
    assert result.status == "error"
    assert "could not write" in (result.error or "")
    assert str(locked / "out.txt") in (result.error or "")


async def test_file_write_empty_content_writes_empty_file(tmp_path):
    target = tmp_path / "empty.txt"
    result = await execute_file_write(str(target), "")
    assert result.status == "success"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == ""


async def test_file_write_path_is_directory_is_error(tmp_path):
    directory = tmp_path / "adir"
    directory.mkdir()
    result = await execute_file_write(str(directory), "x")
    assert result.status == "error"
    assert "could not write" in (result.error or "")


# -- make_named_tool: strict placeholder binding (contract §6) ---------------


async def test_named_tool_missing_argument_is_error():
    tool = make_named_tool(ShellToolDefinition(kind="shell", command="echo hello {name}"))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool()
    assert exc_info.value.result.status == "error"
    assert "missing required argument 'name'" in (exc_info.value.result.error or "")


async def test_named_tool_unexpected_argument_is_error():
    tool = make_named_tool(ShellToolDefinition(kind="shell", command="echo hello {name}"))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool(name="bob", typo="x")
    assert exc_info.value.result.status == "error"
    assert "unexpected argument 'typo'" in (exc_info.value.result.error or "")


# -- make_named_tool: success returns result, non-success raises -------------


async def test_named_tool_success_returns_result():
    tool = make_named_tool(ShellToolDefinition(kind="shell", command="echo hi {name}"))
    result = await tool(name="bob")
    assert isinstance(result, ToolExecutionResult)
    assert result.status == "success"
    assert "hi bob" in result.output
    assert result.as_dict()["command"] == "echo hi bob"


async def test_named_tool_non_success_raises_tool_execution_error():
    tool = make_named_tool(ShellToolDefinition(kind="shell", command="false {name}"))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool(name="x")
    assert exc_info.value.result.status == "failed"
    assert exc_info.value.result.exit_code == 1


async def test_named_file_write_has_implicit_content_parameter(tmp_path):
    tool = make_named_tool(FileWriteToolDefinition(kind="file_write", path=str(tmp_path / "{name}.txt")))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool(name="a")
    assert exc_info.value.result.status == "error"
    assert "missing required argument 'content'" in (exc_info.value.result.error or "")


async def test_named_file_write_success_writes_file(tmp_path):
    tool = make_named_tool(FileWriteToolDefinition(kind="file_write", path=str(tmp_path / "{name}.txt")))
    result = await tool(name="a", content="hello")
    assert result.status == "success"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"


async def test_named_file_write_creates_parents_and_carries_path(tmp_path):
    tool = make_named_tool(FileWriteToolDefinition(kind="file_write", path=str(tmp_path / "out" / "{name}.txt")))
    result = await tool(name="a", content="hello")
    assert result.status == "success"
    # the success result carries the RESOLVED path, not the template
    assert result.output == str(tmp_path / "out" / "a.txt")
    assert (tmp_path / "out" / "a.txt").read_text(encoding="utf-8") == "hello"


async def test_named_file_write_empty_content_is_success(tmp_path):
    tool = make_named_tool(FileWriteToolDefinition(kind="file_write", path=str(tmp_path / "{name}.txt")))
    result = await tool(name="a", content="")
    assert result.status == "success"
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == ""


async def test_named_file_write_failure_raises_with_error_status(tmp_path):
    directory = tmp_path / "adir"
    directory.mkdir()
    tool = make_named_tool(FileWriteToolDefinition(kind="file_write", path=str(directory)))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool(content="x")
    assert exc_info.value.result.status == "error"
    assert "could not write" in (exc_info.value.result.error or "")


# -- US5 (T023): working_dir, strict timeout bound, could-not-run ------------


async def test_shell_working_dir_honored(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = await execute_shell("pwd", working_dir=str(workdir))
    assert result.status == "success"
    assert result.output.strip() == str(workdir)


async def test_program_working_dir_honored(tmp_path):
    script = tmp_path / "cwd.py"
    script.write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = await execute_program(str(script), working_dir=str(workdir))
    assert result.status == "success"
    assert result.output.strip() == str(workdir)


async def test_named_shell_working_dir_and_timeout_from_definition(tmp_path):
    # FR-006: per-tool working_dir + timeout_seconds configured on the
    # definition are both honored and enforced.
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tool = make_named_tool(
        ShellToolDefinition(kind="shell", command="pwd", working_dir=str(workdir), timeout_seconds=1)
    )
    result = await tool()
    assert result.status == "success"
    assert result.output.strip() == str(workdir)


async def test_shell_timeout_strictly_within_configured_bound():
    # SC-004: no unbounded hang — the run is killed and reported within the
    # configured bound (1 s here, small epsilon for kill + reap).
    start = time.monotonic()
    result = await execute_shell("sleep 5", timeout_seconds=1)
    elapsed = time.monotonic() - start
    assert result.status == "timed_out"
    assert result.exit_code is None
    assert "exceeded 1s timeout" in (result.error or "")
    assert result.duration_seconds < 2.0
    assert elapsed < 2.0


async def test_program_timeout_strictly_within_configured_bound(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    start = time.monotonic()
    result = await execute_program(str(script), timeout_seconds=1)
    elapsed = time.monotonic() - start
    assert result.status == "timed_out"
    assert result.exit_code is None
    assert "exceeded 1s timeout" in (result.error or "")
    assert elapsed < 2.0


async def test_shell_could_not_run_is_error_not_crash(tmp_path):
    # Spawn fails (configured working_dir does not exist) -> status "error"
    # with a clear reason, never an exception.
    missing = tmp_path / "does-not-exist"
    result = await execute_shell("echo hi", working_dir=str(missing))
    assert result.status == "error"
    assert result.exit_code is None
    assert "could not run" in (result.error or "")
    assert result.command == "echo hi"


async def test_program_file_not_found_is_error_not_crash(tmp_path):
    missing = str(tmp_path / "missing.py")
    result = await execute_program(missing)
    assert result.status == "error"
    assert result.exit_code is None
    assert "program file not found" in (result.error or "")
    assert missing in (result.error or "")


async def test_program_not_executable_is_error_not_crash(tmp_path):
    script = tmp_path / "noext"
    script.write_text("echo hi\n", encoding="utf-8")
    os.chmod(script, 0o644)
    result = await execute_program(str(script))
    assert result.status == "error"
    assert result.exit_code is None
    assert "not executable" in (result.error or "")


async def test_program_missing_interpreter_is_error_not_crash(tmp_path):
    script = tmp_path / "tiny.py"
    script.write_text("print(1)\n", encoding="utf-8")
    result = await execute_program(str(script), interpreter=str(tmp_path / "no-such-interpreter"))
    assert result.status == "error"
    assert result.exit_code is None
    assert "could not run" in (result.error or "")


async def test_named_program_file_not_found_is_error(tmp_path):
    missing = str(tmp_path / "missing.py")
    tool = make_named_tool(ProgramToolDefinition(kind="program", program=missing))
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool()
    assert exc_info.value.result.status == "error"
    assert "program file not found" in (exc_info.value.result.error or "")


# -- spec 006 (T003): additive environment field + backend routing -----------


def test_result_environment_defaults_to_local_and_appears_in_as_dict():
    result = ToolExecutionResult(
        status="success",
        exit_code=0,
        output="",
        stderr="",
        error=None,
        duration_seconds=0.0,
        command="c",
        kind="shell",
    )
    assert result.environment == "local"
    assert result.as_dict()["environment"] == "local"
    # explicit override is carried through
    sandboxed = dataclasses.replace(result, environment="sandbox")
    assert sandboxed.as_dict()["environment"] == "sandbox"


async def test_executors_default_environment_to_local():
    result = await execute_shell("echo hi")
    assert result.environment == "local"
    assert result.as_dict()["environment"] == "local"


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


async def test_named_tool_routes_through_injected_backend():
    caps = ToolCaps(cpu=4.0, memory_bytes=1024**3, pids_limit=512)
    backend = _RecordingBackend()
    tool = make_named_tool(
        ShellToolDefinition(kind="shell", command="echo hello {name}", working_dir="/wd", timeout_seconds=9),
        backend=backend,
        caps=caps,
    )
    result = await tool(name="bob")
    assert backend.calls == [("shell", "echo hello bob", "/wd", 9, caps)]
    assert result.environment == "sandbox"
    assert result.output == "fake-shell:echo hello bob"


async def test_named_program_routes_through_injected_backend():
    backend = _RecordingBackend()
    tool = make_named_tool(
        ProgramToolDefinition(kind="program", program="scripts/{name}.py", interpreter="python3"),
        backend=backend,
    )
    result = await tool(name="a")
    assert backend.calls == [("program", "scripts/a.py", "python3", None, None, None)]
    assert result.environment == "sandbox"


async def test_named_file_write_routes_through_injected_backend(tmp_path):
    backend = _RecordingBackend()
    tool = make_named_tool(
        FileWriteToolDefinition(kind="file_write", path=str(tmp_path / "{name}.txt")),
        backend=backend,
    )
    result = await tool(name="a", content="hello")
    assert backend.calls == [("file_write", str(tmp_path / "a.txt"), "hello", None)]
    assert result.environment == "sandbox"
    assert result.output == str(tmp_path / "a.txt")
    # the backend is authoritative: the recording fake never wrote to disk
    assert not (tmp_path / "a.txt").exists()


async def test_named_tool_backend_keeps_strict_binding():
    # Strict {placeholder} binding still applies before any execution (contract §6).
    backend = _RecordingBackend()
    tool = make_named_tool(
        ShellToolDefinition(kind="shell", command="echo {name}"),
        backend=backend,
    )
    with pytest.raises(ToolExecutionError) as exc_info:
        await tool()
    assert "missing required argument 'name'" in (exc_info.value.result.error or "")
    assert backend.calls == []
