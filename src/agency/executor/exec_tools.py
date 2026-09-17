"""Executable tools: shell, program, and file-write executors (spec 005).

Stdlib only (Constitution V). Every executable run produces a frozen
:class:`ToolExecutionResult` capturing what ran, its status, exit code, and
captured stdout/stderr. ``make_named_tool`` wraps a workflow ``ToolDefinition``
into a tool callable: it returns the :class:`ToolExecutionResult` on success and
raises :class:`ToolExecutionError` (carrying the full result) on non-success so
the runner — not the tool — decides the node outcome.
"""

from __future__ import annotations

import asyncio
import os
import string
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agency.executor.execution_env import ToolCaps, ToolExecutionBackend
    from agency.yaml_engine.schema import ToolDefinition

__all__ = [
    "ToolExecutionError",
    "ToolExecutionResult",
    "execute_file_write",
    "execute_program",
    "execute_shell",
    "make_named_tool",
    "named_tool_parameters",
    "named_tool_template",
    "placeholder_names",
]

ToolStatus = Literal["success", "failed", "timed_out", "error"]
ToolKind = Literal["shell", "program", "file_write"]

# Explicit interpreter > extension map > executable-bit check (spec 005).
_EXTENSION_INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable],
    ".sh": ["bash"],
}


@dataclass(frozen=True)
class ToolExecutionResult:
    """Captured outcome of one executable tool run.

    ``kind`` records which executor produced the result so the runner can map
    the outcome to the contract's node-output shape (plain path for
    ``file_write`` vs the JSON report for ``shell``/``program``). It is internal
    and deliberately excluded from :meth:`as_dict` to keep the JSON contract.

    ``environment`` (spec 006, additive) records where the execution ran —
    ``"local"`` or ``"sandbox"`` — and IS included in :meth:`as_dict` (contract §6).
    """

    status: ToolStatus
    exit_code: int | None
    output: str
    stderr: str
    error: str | None
    duration_seconds: float
    command: str
    kind: ToolKind
    environment: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "stderr": self.stderr,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "command": self.command,
            "environment": self.environment,
        }


class ToolExecutionError(Exception):
    """Raised by a tool callable when an executable run is non-success."""

    def __init__(self, result: ToolExecutionResult) -> None:
        super().__init__(f"tool execution {result.status}: {result.error or result.command}")
        self.result = result


def _fmt_seconds(value: float) -> str:
    return f"{value:g}"


def _error_result(command: str, reason: str, duration_seconds: float, kind: ToolKind) -> ToolExecutionResult:
    return ToolExecutionResult(
        status="error",
        exit_code=None,
        output="",
        stderr="",
        error=reason,
        duration_seconds=duration_seconds,
        command=command,
        kind=kind,
    )


async def _run_bounded(
    argv: list[str],
    *,
    command: str,
    working_dir: str | None,
    timeout_seconds: float | None,
    kind: ToolKind,
) -> ToolExecutionResult:
    """Run *argv* with captured stdout/stderr, bounded by *timeout_seconds*."""
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _error_result(command, f"could not run: {exc}", time.monotonic() - start, kind)
    stdout_b = b""
    stderr_b = b""
    try:
        if timeout_seconds is None:
            stdout_b, stderr_b = await proc.communicate()
        else:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError, OSError):
            stdout_b, stderr_b = b"", b""
        await proc.wait()
        return ToolExecutionResult(
            status="timed_out",
            exit_code=None,
            output=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            error=f"exceeded {_fmt_seconds(timeout_seconds)}s timeout",
            duration_seconds=time.monotonic() - start,
            command=command,
            kind=kind,
        )
    exit_code = proc.returncode
    output = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if exit_code == 0:
        return ToolExecutionResult(
            status="success",
            exit_code=0,
            output=output,
            stderr=stderr,
            error=None,
            duration_seconds=time.monotonic() - start,
            command=command,
            kind=kind,
        )
    return ToolExecutionResult(
        status="failed",
        exit_code=exit_code,
        output=output,
        stderr=stderr,
        error=f"exited with code {exit_code}",
        duration_seconds=time.monotonic() - start,
        command=command,
        kind=kind,
    )


async def execute_shell(
    command: str,
    *,
    working_dir: str | None = None,
    timeout_seconds: float | None = None,
) -> ToolExecutionResult:
    """Run *command* via ``bash -c`` and capture the result."""
    return await _run_bounded(
        ["bash", "-c", command],
        command=command,
        working_dir=working_dir,
        timeout_seconds=timeout_seconds,
        kind="shell",
    )


async def execute_program(
    program: str,
    *,
    interpreter: str | None = None,
    working_dir: str | None = None,
    timeout_seconds: float | None = None,
) -> ToolExecutionResult:
    """Run the script/program file *program* and capture the result."""
    start = time.monotonic()
    if not os.path.isfile(program):
        return _error_result(program, f"program file not found: '{program}'", time.monotonic() - start, "program")
    if interpreter:
        argv = [interpreter, program]
    else:
        suffix = os.path.splitext(program)[1].lower()
        prefix = _EXTENSION_INTERPRETERS.get(suffix)
        if prefix is not None:
            argv = [*prefix, program]
        elif os.access(program, os.X_OK):
            argv = [program]
        else:
            return _error_result(
                program,
                f"no interpreter for '{program}' and the file is not executable",
                time.monotonic() - start,
                "program",
            )
    return await _run_bounded(
        argv, command=program, working_dir=working_dir, timeout_seconds=timeout_seconds, kind="program"
    )


async def execute_file_write(path: str, content: str) -> ToolExecutionResult:
    """Write *content* to *path*, creating parent directories.

    Empty content writes an empty file; any I/O failure (unwritable location,
    path is a directory, ...) is a ``status="error"`` result, not a crash. On
    success ``output`` carries the written path — the plain string that becomes
    the node output (contract §3).
    """
    start = time.monotonic()
    command = f"write to '{path}'"

    def _write() -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        return _error_result(command, f"could not write '{path}': {exc}", time.monotonic() - start, "file_write")
    return ToolExecutionResult(
        status="success",
        exit_code=None,
        output=path,
        stderr="",
        error=None,
        duration_seconds=time.monotonic() - start,
        command=command,
        kind="file_write",
    )


def placeholder_names(template: str) -> tuple[str, ...]:
    """Ordered ``{name}`` placeholder names in *template* (strict str.format)."""
    names: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name:  # None = literal part, "" = auto-numbered field
            names.append(field_name)
    return tuple(names)


def named_tool_template(definition: ToolDefinition) -> str:
    """The template field of a named tool definition, by kind."""
    kind = definition.kind
    if kind == "shell":
        return definition.command
    if kind == "program":
        return definition.program
    if kind == "file_write":
        return definition.path
    raise ValueError(f"unknown tool kind: {kind!r}")


def named_tool_parameters(definition: ToolDefinition) -> tuple[str, ...]:
    """Ordered parameter names of a named tool: placeholders (+ content)."""
    names = list(placeholder_names(named_tool_template(definition)))
    if definition.kind == "file_write":
        names.append("content")
    return tuple(names)


def make_named_tool(
    definition: ToolDefinition,
    backend: ToolExecutionBackend | None = None,
    caps: ToolCaps | None = None,
) -> Callable[..., Any]:
    """Compile a workflow ``ToolDefinition`` into an async tool callable.

    Arguments are bound strictly from the template's ``{name}`` placeholders
    (plus the implicit ``content`` for file_write): a missing or unexpected
    argument fails the run with the contract reason string instead of
    silently producing a wrong command/path.

    When *backend* is supplied the execution is routed through it (spec 006),
    passing *caps* (the tool's declared per-tool overrides) on every call;
    when ``None`` the executors run directly on the host (today's behavior).
    """
    kind = definition.kind
    template = named_tool_template(definition)
    params = named_tool_parameters(definition)

    async def tool(**kwargs: Any) -> ToolExecutionResult:
        missing = next((p for p in params if p not in kwargs), None)
        if missing is not None:
            raise ToolExecutionError(
                _error_result(template, f"missing required argument '{missing}'", 0.0, kind)
            )
        unexpected = next((k for k in kwargs if k not in params), None)
        if unexpected is not None:
            raise ToolExecutionError(
                _error_result(template, f"unexpected argument '{unexpected}'", 0.0, kind)
            )
        if kind == "shell":
            command = template.format(**kwargs)
            if backend is None:
                result = await execute_shell(
                    command,
                    working_dir=definition.working_dir,
                    timeout_seconds=definition.timeout_seconds,
                )
            else:
                result = await backend.shell(
                    command,
                    working_dir=definition.working_dir,
                    timeout_seconds=definition.timeout_seconds,
                    caps=caps,
                )
        elif kind == "program":
            program = template.format(**kwargs)
            if backend is None:
                result = await execute_program(
                    program,
                    interpreter=definition.interpreter,
                    working_dir=definition.working_dir,
                    timeout_seconds=definition.timeout_seconds,
                )
            else:
                result = await backend.program(
                    program,
                    interpreter=definition.interpreter,
                    working_dir=definition.working_dir,
                    timeout_seconds=definition.timeout_seconds,
                    caps=caps,
                )
        else:  # file_write
            path = template.format(**{k: v for k, v in kwargs.items() if k != "content"})
            if backend is None:
                result = await execute_file_write(path, kwargs["content"])
            else:
                result = await backend.file_write(path, kwargs["content"], caps=caps)
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    tool.__name__ = f"named_tool_{kind}"
    return tool
