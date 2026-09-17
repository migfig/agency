"""Execution-environment seam (spec 006): where tool executions run.

The executor-side half of the sandbox feature (Constitution V: the Docker
backend lives in ``resource_manager/sandbox.py`` and is injected here, never
imported). This module holds:

- :class:`ToolCaps` — the per-named-tool overridable cap subset (FR-005);
- :class:`ToolExecutionBackend` — the structural protocol the generic
  execution tools call into (three async ops mirroring ``shell``/``program``/
  ``write_file``, each accepting optional :class:`ToolCaps`);
- :func:`resolve_environment` — the pure environment precedence (contract §4:
  run supply > workflow declaration > built-in ``"sandbox"``);
- :class:`LocalToolBackend` — today's behavior: delegates to the existing
  ``exec_tools`` executors byte-for-byte (SC-010), ``environment="local"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from agency.executor.exec_tools import (
    ToolExecutionResult,
    execute_file_write,
    execute_program,
    execute_shell,
)

__all__ = [
    "ExecutionEnvironment",
    "LocalToolBackend",
    "ToolCaps",
    "ToolExecutionBackend",
    "parse_size",
    "resolve_environment",
]

ExecutionEnvironment = Literal["sandbox", "local"]

_SIZE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(b|kb|mb|gb)\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
}


@dataclass(frozen=True)
class ToolCaps:
    """The subset of resource bounds a *named tool* may override (FR-005).

    All fields optional: ``None`` means "inherit the run-level value" (the
    layering built-in defaults ← workflow ``sandbox:`` ← run caps ←
    named-tool caps is applied by the backend, contract §4).
    """

    cpu: float | None = None
    memory_bytes: int | None = None
    pids_limit: int | None = None


class ToolExecutionBackend(Protocol):
    """Where tool executions run. Implemented by the local and Docker backends.

    The three ops mirror the model-facing generic tools (same parameters)
    plus the optional per-invocation :class:`ToolCaps`. Implementations return
    :class:`ToolExecutionResult` with ``environment`` set accordingly; the
    local backend delegates to the existing ``exec_tools`` executors.
    """

    async def shell(
        self,
        command: str,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        """Run *command* via ``bash -c`` and capture the result."""
        ...

    async def program(
        self,
        program: str,
        interpreter: str | None = None,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        """Run the script/program file *program* and capture the result."""
        ...

    async def file_write(
        self,
        path: str,
        content: str,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        """Write *content* to *path* and capture the result."""
        ...


def resolve_environment(run_value: ExecutionEnvironment | None, workflow_value: ExecutionEnvironment | None) -> str:
    """Resolve the run's execution environment (contract §4, normative).

    ``run_value`` (CLI flag / API field) wins, else the workflow's
    ``execution_environment``, else the built-in default ``"sandbox"``.
    """
    return run_value if run_value is not None else (workflow_value or "sandbox")


def parse_size(value: str) -> int:
    """Parse a ``512MB``-style size string to bytes.

    The executor-side equivalent of ``vram_tracker.parse_vram_size`` — the
    executor must not import ``resource_manager`` at runtime (Constitution V),
    so the per-tool ``memory`` cap is parsed here instead.
    """
    match = _SIZE_PATTERN.match(value)
    if match is None:
        raise ValueError(f"Invalid size format: '{value}' (expected e.g. '512MB', '1GB')")
    amount = float(match.group(1))
    return int(amount * _SIZE_MULTIPLIERS[match.group(2).lower()])


class LocalToolBackend:
    """The local backend: today's host execution, byte-for-byte (SC-010).

    Delegates to the existing ``exec_tools`` executors with the same
    arguments; the caps are accepted but ignored (local execution is
    unbounded by design — contract §1.3: bounds apply to sandbox mode).
    """

    async def shell(
        self,
        command: str,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        result = await execute_shell(command, working_dir=working_dir, timeout_seconds=timeout_seconds)
        return replace(result, environment="local")

    async def program(
        self,
        program: str,
        interpreter: str | None = None,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        result = await execute_program(
            program, interpreter=interpreter, working_dir=working_dir, timeout_seconds=timeout_seconds
        )
        return replace(result, environment="local")

    async def file_write(
        self,
        path: str,
        content: str,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        result = await execute_file_write(path, content)
        return replace(result, environment="local")
