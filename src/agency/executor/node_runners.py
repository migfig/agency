"""Non-agent node runners and their collaborators (Story 5.3).

``NonAgentRunner`` executes ``conditional``, ``merge``, ``broadcast``,
``human_in_loop`` and ``tool_call`` nodes under the SAME contract as
:class:`agency.executor.agent_runner.AgentRunner`: it performs its side effects
(recording phase outputs) and returns a :class:`RunResult`, emitting no
``Node*`` lifecycle events. The DAG orchestrator (Story 5.4) is the sole
authority for events, node state, and skip-marking; this runner only returns
the flow data (selected branch, candidate skipped targets) for it to act on.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from agency.executor.binding_resolver import VariableResolver
from agency.executor.contracts import RunResult
from agency.executor.exec_tools import ToolExecutionError, ToolExecutionResult
from agency.executor.tool_registry import DEFAULT_REGISTRY, ToolRegistry
from agency.yaml_engine.schema import (
    BroadcastNode,
    ConditionalNode,
    HumanInLoopNode,
    MergeNode,
    ToolCallNode,
)

logger = logging.getLogger(__name__)


class _ContextStore(Protocol):
    def record_output(self, phase_id: str, node_id: str, value: Any) -> None:
        ...


InputSource = Callable[[str, str], Awaitable[str]]


class ConditionError(ValueError):
    """Raised when a resolved condition expression is not a valid v1 expression."""


# v1 grammar: ``<lhs> <op> <rhs>`` where op is one of the supported operators.
# Alternatives are ordered most-specific-first so ``not contains`` wins over
# ``contains``. The left-hand side is left as-is (a resolved upstream output);
# the right-hand side is unwrapped of one layer of surrounding quotes.
_CONDITION_RE = re.compile(
    r"^(?P<lhs>.*?)\s+(?P<op>not contains|contains|!=|==)\s+(?P<rhs>.+)$",
    re.DOTALL,
)


def _unquote(value: str) -> str:
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_condition(expr: str) -> bool:
    """Evaluate a resolved v1 condition expression to a boolean.

    Supported operators (case-sensitive): ``==``, ``!=``, ``contains``,
    ``not contains``. ``contains``/``not contains`` test whether the resolved
    right-hand literal is a substring of the resolved left-hand value.

    Raises:
        ConditionError: if no supported operator is present.
    """
    match = _CONDITION_RE.match(expr)
    if match is None:
        raise ConditionError(f"invalid condition expression: {expr!r}")
    lhs = match.group("lhs").strip()
    op = match.group("op")
    rhs = _unquote(match.group("rhs"))
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "contains":
        return rhs in lhs
    if op == "not contains":
        return rhs not in lhs
    raise ConditionError(f"unsupported operator in condition: {op!r}")


def _tool_error_report(node: ToolCallNode, result: ToolExecutionResult) -> str:
    """Structured failure report for an executable tool run (spec 005, R2).

    Embeds the full captured result — what ran, status, exit code, captured
    stdout/stderr, error — so the run record stays diagnosable on failure.
    """
    return f"tool '{node.tool_name}' execution {result.status}: {json.dumps(result.as_dict())}"


def _read_stdin_line(prompt: str) -> str:
    """Echo *prompt* to stdout and read one line from stdin (blocking)."""
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - display failures must not break the run
        logger.debug("failed to write human_in_loop prompt to stdout")
    line = sys.stdin.readline()
    return line.rstrip("\n")


def build_stdin_input_source() -> InputSource:
    """Return an input source that prompts on stdout and reads stdin (headless)."""

    async def _source(node_id: str, prompt: str) -> str:
        del node_id
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _read_stdin_line, prompt)

    return _source


class NonAgentRunner:
    """Executes the non-agent node types, returning a :class:`RunResult`.

    Emits no ``Node*`` events. Collaborators are injected structurally:

    - ``tool_registry``: lookup for ``tool_call`` (defaults ``DEFAULT_REGISTRY``).
    - ``input_source``: async ``(node_id, prompt) -> text`` callable for
      ``human_in_loop`` (defaults to stdout/stdin so headless runs work).
    - ``context_store``: optional phase-context recorder for node outputs.
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        input_source: InputSource | None = None,
        context_store: _ContextStore | None = None,
    ) -> None:
        self._tool_registry = tool_registry if tool_registry is not None else DEFAULT_REGISTRY
        self._input_source = input_source if input_source is not None else build_stdin_input_source()
        self._context_store = context_store
        self._resolver = VariableResolver()

    async def run(
        self,
        node: ConditionalNode | MergeNode | BroadcastNode | HumanInLoopNode | ToolCallNode,
        context: dict[str, str],
        *,
        phase_id: str | None = None,
        deps: tuple[str, ...] | list[str] = (),
    ) -> RunResult:
        started = time.monotonic()
        if isinstance(node, ConditionalNode):
            result = self._run_conditional(node, context, phase_id)
        elif isinstance(node, MergeNode):
            result = self._run_merge(node, context, phase_id)
        elif isinstance(node, BroadcastNode):
            result = self._run_broadcast(node, deps, context, phase_id)
        elif isinstance(node, HumanInLoopNode):
            result = await self._run_human(node, phase_id)
        elif isinstance(node, ToolCallNode):
            result = await self._run_tool(node, context, phase_id)
        else:  # pragma: no cover - schema guarantees one of the above
            result = RunResult(
                node_id=node.id,
                outcome="failed",
                error=f"unsupported node type: {type(node).__name__}",
            )
        return replace(result, duration_seconds=time.monotonic() - started)

    # -- helpers ------------------------------------------------------------

    def _key(self, node_id: str) -> str:
        return f"nodes.{node_id}.output"

    def _record(self, phase_id: str | None, node_id: str, value: Any) -> None:
        if self._context_store is not None and phase_id is not None:
            self._context_store.record_output(phase_id, node_id, value)

    def _failed(self, node_id: str, error: str) -> RunResult:
        return RunResult(node_id=node_id, outcome="failed", error=error)

    def _skipped(self, node_id: str, skipped_bindings: tuple[str, ...]) -> RunResult:
        return RunResult(node_id=node_id, outcome="skipped", skipped_bindings=skipped_bindings)

    def _missing_bindings(self, template: str, context: dict[str, str]) -> tuple[str, ...]:
        """Keys referenced by *template* that are absent from *context*."""
        required = VariableResolver.find_bindings(template)
        return tuple(key for key in required if key not in context)

    # -- node handlers ------------------------------------------------------

    def _run_conditional(self, node: ConditionalNode, context: dict[str, str], phase_id: str | None) -> RunResult:
        missing = self._missing_bindings(node.condition, context)
        if missing:
            return self._skipped(node.id, missing)
        expr = self._resolver.resolve(node.condition, context)
        try:
            truthy = parse_condition(expr)
        except ConditionError as exc:
            return self._failed(node.id, f"conditional node '{node.id}': {exc}")
        label = "true" if truthy else "false"
        target = node.branches.get(label)
        if target is None:
            return self._failed(
                node.id,
                f"conditional node '{node.id}': branches is missing label '{label}'",
            )
        skipped_targets = tuple(t for k, t in node.branches.items() if k != label)
        logger.info("conditional '%s' -> branch '%s' (target '%s')", node.id, label, target)
        self._record(phase_id, node.id, label)
        return RunResult(
            node_id=node.id,
            outcome="completed",
            output=label,
            selected_label=label,
            branch_target=target,
            skipped_targets=skipped_targets,
        )

    def _run_merge(self, node: MergeNode, context: dict[str, str], phase_id: str | None) -> RunResult:
        pairs = [(nid, self._key(nid)) for nid in node.inputs]
        if node.strategy == "all":
            values = []
            missing = []
            for nid, key in pairs:
                if key in context:
                    values.append(str(context[key]))
                else:
                    missing.append(key)
            if missing:
                return self._skipped(node.id, tuple(missing))
            output = "\n".join(values)
        else:  # strategy == "any"
            chosen_idx: int | None = None
            for idx, (_nid, key) in enumerate(pairs):
                if key in context:
                    chosen_idx = idx
                    break
            if chosen_idx is None:
                return self._skipped(node.id, tuple(key for _nid, key in pairs))
            chosen_nid, chosen_key = pairs[chosen_idx]
            output = str(context[chosen_key])
            discarded = [nid for nid, _key in pairs if nid != chosen_nid]
            if discarded:
                logger.info("merge '%s' discarded inputs: %s", node.id, ", ".join(discarded))
        self._record(phase_id, node.id, output)
        return RunResult(node_id=node.id, outcome="completed", output=output)

    def _run_broadcast(
        self, node: BroadcastNode, deps: tuple[str, ...] | list[str], context: dict[str, str], phase_id: str | None
    ) -> RunResult:
        values = [str(context[self._key(dep)]) for dep in deps if self._key(dep) in context]
        output = "\n".join(values)
        logger.debug("broadcast '%s' fanning out to %s", node.id, node.targets)
        self._record(phase_id, node.id, output)
        return RunResult(node_id=node.id, outcome="completed", output=output)

    async def _run_human(self, node: HumanInLoopNode, phase_id: str | None) -> RunResult:
        try:
            if node.timeout_seconds is not None:
                value = await asyncio.wait_for(
                    self._input_source(node.id, node.prompt), timeout=node.timeout_seconds
                )
            else:
                value = await self._input_source(node.id, node.prompt)
        except asyncio.TimeoutError:
            return self._failed(
                node.id, f"human_in_loop node '{node.id}' timed out after {node.timeout_seconds}s"
            )
        except Exception as exc:  # noqa: BLE001 - never crash the run on I/O failure
            return self._failed(node.id, f"human_in_loop node '{node.id}' input failed: {exc}")
        self._record(phase_id, node.id, value)
        return RunResult(node_id=node.id, outcome="completed", output=value)

    @staticmethod
    def _resolve_json_value(value: object, resolver: VariableResolver, context: dict[str, str]) -> object:
        if isinstance(value, str):
            return resolver.resolve(value, context)
        if isinstance(value, list):
            return [NonAgentRunner._resolve_json_value(v, resolver, context) for v in value]
        if isinstance(value, dict):
            return {k: NonAgentRunner._resolve_json_value(v, resolver, context) for k, v in value.items()}
        return value

    def _resolve_tool_args(self, node: ToolCallNode, context: dict[str, str]) -> tuple[dict[str, object] | None, str | None]:
        try:
            template_obj: object = json.loads(node.arguments_template)
        except json.JSONDecodeError:
            template_obj = None
        if isinstance(template_obj, dict):
            # Strict path: bindings live inside JSON string values; resolving the parsed
            # structure keeps quote/backslash/newline content escaping-correct.
            resolved = self._resolve_json_value(template_obj, self._resolver, context)
            if not isinstance(resolved, dict):
                return None, "arguments must be a JSON object"
            return resolved, None
        args_json = self._resolver.resolve(node.arguments_template, context)
        try:
            kwargs = json.loads(args_json)
        except json.JSONDecodeError as exc:
            return None, f"invalid arguments JSON ({exc})"
        if not isinstance(kwargs, dict):
            return None, "arguments must be a JSON object"
        return kwargs, None

    async def _run_tool(self, node: ToolCallNode, context: dict[str, str], phase_id: str | None) -> RunResult:
        missing = self._missing_bindings(node.arguments_template, context)
        if missing:
            return self._skipped(node.id, missing)
        kwargs, args_error = self._resolve_tool_args(node, context)
        if kwargs is None:
            return self._failed(node.id, f"tool_call node '{node.id}': {args_error}")
        fn = self._tool_registry.get(node.tool_name)
        if fn is None:
            return self._failed(node.id, f"tool_call node '{node.id}': unknown tool '{node.tool_name}'")
        try:
            ret = fn(**kwargs)
            if inspect.isawaitable(ret):
                ret = await ret
        except ToolExecutionError as exc:
            return self._failed(node.id, _tool_error_report(node, exc.result))
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed node, not a crash
            return self._failed(node.id, f"tool '{node.tool_name}' raised: {exc}")
        if isinstance(ret, ToolExecutionResult):
            if ret.status != "success":
                return self._failed(node.id, _tool_error_report(node, ret))
            if ret.kind == "file_write":
                output = ret.output  # written path as a plain string (contract §3)
            else:
                output = json.dumps(ret.as_dict())
        else:
            output = ret if isinstance(ret, str) else str(ret)
        self._record(phase_id, node.id, output)
        return RunResult(node_id=node.id, outcome="completed", output=output)
