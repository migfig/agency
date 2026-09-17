from __future__ import annotations

import inspect
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from agency.executor.binding_resolver import VariableResolver
from agency.executor.contracts import RunResult
from agency.executor.exec_tools import ToolExecutionError, ToolExecutionResult
from agency.executor.summarizer import estimate_tokens
from agency.yaml_engine.schema import AgentNode

if TYPE_CHECKING:
    from agency.executor.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0

# Conservative VRAM estimate (4 GB) when a model's size is not in the map.
_DEFAULT_VRAM_BYTES = 4 * 1024**3


class _ToolLoopBoundExceeded(Exception):
    """Internal signal: the model still requested tools on the final round-trip."""

    def __init__(self, max_rounds: int) -> None:
        self.max_rounds = max_rounds
        super().__init__(f"tool-loop bound exceeded after {max_rounds} round-trips")


class _Router(Protocol):
    async def route_inference(
        self,
        *,
        model: str,
        messages: list[dict],
        run_id: str,
        node_id: str,
        timeout: float = ...,
        **kwargs: Any,
    ) -> dict:
        ...


class _LoadManager(Protocol):
    async def acquire_slot(self, node_id: str, model_name: str, estimated_bytes: int) -> Any:
        ...

    def release_slot(self, model_name: str, node_id: str, freed_bytes: int | None = None) -> None:
        ...


class _ContextStore(Protocol):
    def record_output(self, phase_id: str, node_id: str, value: Any) -> None:
        ...


class _Summarizer(Protocol):
    async def before_agent_run(self, phase_id: str) -> bool:
        ...


# RunResult now lives in agency.executor.contracts (shared by agent and
# non-agent runners) and is re-exported here for backward compatibility.


class AgentRunner:
    """Executes a single agent node end-to-end against a real llama.cpp model.

    The runner performs the pipeline side effects (VRAM slot acquire/release,
    pre-run summarization gate, phase-context recording) but publishes no
    ``Node*`` lifecycle events — the DAG orchestrator does that from the
    returned :class:`RunResult`. Collaborators are injected structurally so the
    module has no runtime dependency on ``resource_manager`` internals.
    """

    def __init__(
        self,
        *,
        run_id: str,
        router: _Router,
        load_manager: _LoadManager,
        vram_sizes: dict[str, int],
        context_store: _ContextStore | None = None,
        summarizer: _Summarizer | None = None,
        default_vram_bytes: int = _DEFAULT_VRAM_BYTES,
        routing_error: type[BaseException] = Exception,
        tool_registry: ToolRegistry | None = None,
        model_timeouts: dict[str, float] | None = None,
    ) -> None:
        self._run_id = run_id
        self._router = router
        self._load_manager = load_manager
        self._vram_sizes = vram_sizes
        self._context_store = context_store
        self._summarizer = summarizer
        self._default_vram_bytes = default_vram_bytes
        # The DAG orchestrator (which holds the resource_manager wiring) can pass
        # the concrete RoutingError type here to narrow the catch to inference
        # failures only. The default (Exception) keeps the runner safely
        # converting any inference-time error into a failed result on its own.
        self._routing_error = routing_error
        self._tool_registry = tool_registry
        # Per-model inference timeouts from the workflow models: section
        # (model name -> seconds). Falls back to DEFAULT_TIMEOUT_SECONDS when a
        # model is absent; a node's own retry.timeout_seconds still wins.
        self._model_timeouts = dict(model_timeouts) if model_timeouts else {}

    async def run(
        self,
        node: AgentNode,
        context: dict[str, str],
        *,
        phase_id: str | None = None,
        attempt: int = 1,
        fallback: str | None = None,
        node_id: str | None = None,
    ) -> RunResult:
        attribution = node.id if node_id is None else node_id
        # 1. Resolve templates; skip if any referenced binding is unresolved.
        templates = [node.prompt_template]
        if node.system_prompt is not None:
            templates.append(node.system_prompt)
        required: list[str] = []
        for template in templates:
            for key in VariableResolver.find_bindings(template):
                if key not in required:
                    required.append(key)
        missing = tuple(key for key in required if key not in context)
        if missing:
            logger.warning(
                "Skipping node '%s': unresolved bindings %s", attribution, list(missing)
            )
            return RunResult(
                node_id=attribution,
                outcome="skipped",
                model=node.model,
                skipped_bindings=missing,
                attempt=attempt,
                fallback=fallback,
            )

        # 2. Tool-enabled node without a registry: fail the step defensively
        #    (a workflow author error, surfaced before any VRAM or model I/O).
        if node.tools is not None and self._tool_registry is None:
            logger.warning(
                "Node '%s' enables tool calling but no tool registry is configured",
                attribution,
            )
            return RunResult(
                node_id=attribution,
                outcome="failed",
                model=node.model,
                error="tool calling enabled but no tool registry configured",
                attempt=attempt,
                fallback=fallback,
            )

        prompt = VariableResolver.resolve(node.prompt_template, context)
        messages: list[dict] = []
        if node.system_prompt is not None:
            messages.append(
                {
                    "role": "system",
                    "content": VariableResolver.resolve(node.system_prompt, context),
                }
            )
        messages.append({"role": "user", "content": prompt})

        # 3. Pre-run summarization gate (only when a summarizer + phase exist).
        if self._summarizer is not None and phase_id is not None:
            await self._summarizer.before_agent_run(phase_id)

        # 4. Acquire a VRAM slot for the whole attempt (the bounded tool loop
        #    below runs its round-trips under a single slot acquire/release).
        estimated_bytes = self._vram_sizes.get(node.model, self._default_vram_bytes)
        await self._load_manager.acquire_slot(attribution, node.model, estimated_bytes)
        try:
            # Timeout precedence: node retry policy > per-model workflow
            # models: timeout_seconds > built-in default.
            timeout = self._model_timeouts.get(node.model, DEFAULT_TIMEOUT_SECONDS)
            if node.retry is not None and node.retry.timeout_seconds is not None:
                timeout = node.retry.timeout_seconds
            kwargs: dict[str, Any] = {}
            if node.temperature is not None:
                kwargs["temperature"] = node.temperature

            start = time.monotonic()
            try:
                if node.tools is None:
                    # 5a. Plain single-shot inference (the unchanged path).
                    response = await self._router.route_inference(
                        model=node.model,
                        messages=messages,
                        run_id=self._run_id,
                        node_id=attribution,
                        timeout=timeout,
                        **kwargs,
                    )
                    output = self._extract_output(response)
                    tokens_used = self._extract_tokens(response, output)
                else:
                    # 5b. Bounded model-driven tool loop: the model may request
                    #     registered tools across several round-trips until it
                    #     answers directly or the bound is hit.
                    assert self._tool_registry is not None
                    tool_names = (
                        list(node.tools.allow)
                        if node.tools.allow is not None
                        else self._tool_registry.names()
                    )
                    kwargs["tools"] = [
                        self._tool_registry.spec(name).to_openai_tool() for name in tool_names
                    ]
                    kwargs["tool_choice"] = "auto"
                    response, output, tokens_used = await self._run_tool_loop(
                        node=node,
                        attribution=attribution,
                        messages=messages,
                        timeout=timeout,
                        extra=kwargs,
                    )
            except _ToolLoopBoundExceeded as exc:
                return RunResult(
                    node_id=attribution,
                    outcome="failed",
                    model=node.model,
                    error=str(exc),
                    attempt=attempt,
                    fallback=fallback,
                    duration_seconds=time.monotonic() - start,
                )
            except self._routing_error as exc:
                return RunResult(
                    node_id=attribution,
                    outcome="failed",
                    model=node.model,
                    error=str(exc),
                    attempt=attempt,
                    fallback=fallback,
                )
            duration = time.monotonic() - start
        finally:
            self._load_manager.release_slot(node.model, attribution)

        # 6. Record the output into the phase context for downstream bindings.
        if self._context_store is not None and phase_id is not None:
            self._context_store.record_output(phase_id, attribution, output)

        return RunResult(
            node_id=attribution,
            outcome="completed",
            model=node.model,
            output=output,
            tokens_used=tokens_used,
            duration_seconds=duration,
            attempt=attempt,
            fallback=fallback,
        )

    async def _run_tool_loop(
        self,
        *,
        node: AgentNode,
        attribution: str,
        messages: list[dict],
        timeout: float,
        extra: dict[str, Any],
    ) -> tuple[dict, str, int]:
        """Drive the bounded tool loop for one tool-enabled attempt.

        ``messages`` is extended in place with each assistant tool-call turn and
        its fed-back ``role: "tool"`` results. Returns the final response, the
        final answer content, and the token usage summed across all turns.
        Raises :class:`_ToolLoopBoundExceeded` when the model still requests
        tools on the ``max_rounds``-th round-trip (nothing is executed then).
        """
        max_rounds = node.tools.max_rounds if node.tools is not None else 0
        tool_rounds = 0
        total_tokens = 0
        while True:
            response = await self._router.route_inference(
                model=node.model,
                messages=messages,
                run_id=self._run_id,
                node_id=attribution,
                timeout=timeout,
                **extra,
            )
            content = self._extract_output(response)
            total_tokens += self._extract_tokens(response, content)
            message = self._extract_message(response)
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                return response, content, total_tokens
            tool_rounds += 1
            if tool_rounds >= max_rounds:
                raise _ToolLoopBoundExceeded(max_rounds)
            messages.append(message)
            for call in tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") if isinstance(call, dict) else None,
                        "content": await self._execute_tool_call(call),
                    }
                )

    async def _execute_tool_call(self, call: Any) -> str:
        """Execute one model-requested tool call and return the model-facing text.

        Successful calls yield the serialized result (structured JSON for
        executable tools, plain text otherwise); non-success executions yield
        the structured failure report; an unknown tool or invalid argument JSON
        yields an error message. Every outcome is fed back as that call's
        ``role: "tool"`` message — per-call problems never fail the step.
        """
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        tool_fn = self._tool_registry.get(name) if name is not None else None
        if tool_fn is None:
            return f"unknown tool '{name}'"
        raw_arguments = function.get("arguments") if isinstance(function, dict) else None
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return f"invalid argument JSON for tool '{name}': {raw_arguments!r}"
        if not isinstance(arguments, dict):
            return (
                f"invalid argument JSON for tool '{name}': "
                f"expected an object, got {type(arguments).__name__}"
            )
        try:
            result = tool_fn(**arguments)
            if inspect.isawaitable(result):
                result = await result
        except ToolExecutionError as exc:
            return json.dumps(exc.result.as_dict())
        except Exception as exc:  # noqa: BLE001 - a tool bug feeds back, never crashes the step
            return f"tool '{name}' failed: {exc}"
        return self._serialize_tool_result(result)

    @staticmethod
    def _serialize_tool_result(result: Any) -> str:
        if isinstance(result, ToolExecutionResult):
            return json.dumps(result.as_dict())
        return str(result)

    @staticmethod
    def _extract_message(response: Any) -> dict:
        if isinstance(response, dict):
            choices = response.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    return message
        return {}

    @staticmethod
    def _extract_output(response: Any) -> str:
        if not isinstance(response, dict):
            return str(response)
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content
        return ""

    @staticmethod
    def _extract_tokens(response: Any, output: str) -> int:
        if isinstance(response, dict):
            usage = response.get("usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if isinstance(total, int):
                    return total
        return estimate_tokens(output)
