from __future__ import annotations

import json
from dataclasses import replace

from agency.executor.agent_runner import DEFAULT_TIMEOUT_SECONDS, AgentRunner, RunResult
from agency.executor.exec_tools import (
    execute_file_write,
    execute_program,
    execute_shell,
)
from agency.executor.summarizer import estimate_tokens
from agency.executor.tool_registry import (
    LocalToolBackend,
    ToolParam,
    ToolRegistry,
    build_workflow_tool_registry,
)
from agency.yaml_engine.schema import AgentNode, RetryPolicy, Workflow

RUN_ID = "run-1"


class _FakeRouter:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def route_inference(
        self, *, model, messages, run_id, node_id, timeout, **kwargs
    ) -> dict:
        self.calls.append(
            {"model": model, "messages": messages, "run_id": run_id, "node_id": node_id, "timeout": timeout, **kwargs}
        )
        if self._error is not None:
            raise self._error
        return self._response


class _FakeLoadManager:
    def __init__(self, acquired: bool = True) -> None:
        self._acquired = acquired
        self.acquired_calls: list[tuple[str, str, int]] = []
        self.released: list[tuple[str, str]] = []

    async def acquire_slot(self, node_id, model_name, estimated_bytes):
        self.acquired_calls.append((node_id, model_name, estimated_bytes))
        return {"acquired": self._acquired}

    def release_slot(self, model_name, node_id, freed_bytes=None):
        self.released.append((model_name, node_id))


class _FakeContextStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], object] = {}

    def record_output(self, phase_id, node_id, value):
        self.records[(phase_id, node_id)] = value


class _FakeSummarizer:
    def __init__(self) -> None:
        self.phase_calls: list[str] = []

    async def before_agent_run(self, phase_id):
        self.phase_calls.append(phase_id)
        return False


def _node(**overrides) -> AgentNode:
    base: dict = {"id": "n", "type": "agent", "model": "llama", "prompt_template": "Summarize: {{ nodes.a.output }}"}
    base.update(overrides)
    return AgentNode(**base)


def _runner(router=None, lm=None, cs=None, summ=None, **kw) -> AgentRunner:
    return AgentRunner(
        run_id=RUN_ID,
        router=router or _FakeRouter(),
        load_manager=lm or _FakeLoadManager(),
        vram_sizes={"llama": 1_000_000},
        context_store=cs or _FakeContextStore(),
        summarizer=summ or _FakeSummarizer(),
        **kw,
    )


# -- Matrix row: HAPPY --------------------------------------------------------


async def test_happy_path_returns_completed_and_records_output():
    router = _FakeRouter(response={"choices": [{"message": {"content": "Answer: hello"}}], "usage": {"total_tokens": 42}})
    lm = _FakeLoadManager()
    cs = _FakeContextStore()
    runner = _runner(router=router, lm=lm, cs=cs)

    result = await runner.run(_node(), {"nodes.a.output": "hello"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.node_id == "n"
    assert result.model == "llama"
    assert result.output == "Answer: hello"
    assert result.tokens_used == 42
    assert result.duration_seconds > 0
    assert ("p1", "n") in cs.records and cs.records[("p1", "n")] == "Answer: hello"
    assert lm.released == [("llama", "n")]
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["run_id"] == RUN_ID and call["node_id"] == "n"
    assert call["messages"] == [
        {"role": "user", "content": "Summarize: hello"},
    ]
    # slot acquired with the model's configured size
    assert lm.acquired_calls == [("n", "llama", 1_000_000)]


async def test_happy_path_passes_temperature_and_system_prompt():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 5}})
    runner = _runner(router=router)
    node = _node(system_prompt="You are a bot {{ nodes.a.output }}", temperature=0.7)

    result = await runner.run(node, {"nodes.a.output": "!"}, phase_id="p1")

    assert result.outcome == "completed"
    call = router.calls[0]
    assert call["temperature"] == 0.7
    assert call["messages"][0] == {"role": "system", "content": "You are a bot !"}
    assert call["messages"][1]["role"] == "user"


# -- Matrix row: UNRESOLVED ---------------------------------------------------


async def test_unresolved_binding_is_skipped_without_inference():
    router = _FakeRouter(response={"choices": [{"message": {"content": "no"}}]})
    lm = _FakeLoadManager()
    runner = _runner(router=router, lm=lm, summ=None, cs=None)

    result = await runner.run(_node(), {}, phase_id="p1")

    assert result.outcome == "skipped"
    assert result.skipped_bindings == ("nodes.a.output",)
    assert result.output is None
    assert router.calls == []  # no inference attempted
    assert lm.acquired_calls == []  # no slot acquired
    assert lm.released == []


# -- Matrix row: INFERENCE_FAIL -----------------------------------------------


async def test_inference_error_yields_failed_result_and_releases_slot():
    router = _FakeRouter(error=RuntimeError("boom 500"))
    lm = _FakeLoadManager()
    runner = _runner(router=router, lm=lm)

    result = await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1", attempt=2)

    assert result.outcome == "failed"
    assert "boom 500" in result.error
    assert result.model == "llama"
    assert result.attempt == 2
    assert result.output is None
    assert lm.released == [("llama", "n")]  # slot still released
    assert len(router.calls) == 1  # exactly one attempt, no retry


async def test_injection_error_type_narrows_the_catch():
    class FakeRoutingError(Exception):
        pass

    class OtherError(Exception):
        pass

    router = _FakeRouter(error=FakeRoutingError("routed failure"))
    # A non-injected exception should propagate, proving the catch is narrowed.
    runner = _runner(router=router, routing_error=FakeRoutingError)
    result = await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")
    assert result.outcome == "failed"

    router2 = _FakeRouter(error=OtherError("not routed"))
    runner2 = _runner(router=router2, routing_error=FakeRoutingError)
    try:
        await runner2.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")
        assert False, "expected OtherError to propagate"
    except OtherError:
        pass


# -- Matrix row: TIMEOUT ------------------------------------------------------


async def test_timeout_from_retry_policy_is_forwarded_to_router():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})
    runner = _runner(router=router)
    node = _node(retry=RetryPolicy(max_attempts=3, timeout_seconds=12.5))

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert router.calls[0]["timeout"] == 12.5


async def test_default_timeout_when_no_retry_policy():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})
    runner = _runner(router=router)

    await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")

    assert router.calls[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS


async def test_model_timeout_from_workflow_models_is_forwarded_to_router():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})
    runner = _runner(router=router, model_timeouts={"llama": 600.0})

    await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")

    assert router.calls[0]["timeout"] == 600.0


async def test_retry_policy_timeout_wins_over_model_timeout():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})
    runner = _runner(router=router, model_timeouts={"llama": 600.0})
    node = _node(retry=RetryPolicy(max_attempts=3, timeout_seconds=12.5))

    await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert router.calls[0]["timeout"] == 12.5


# -- Matrix row: NO_SUMMARIZER ------------------------------------------------


async def test_summarizer_gate_runs_when_configured_and_skipped_when_absent():
    summ = _FakeSummarizer()
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1}})
    present = _runner(summ=summ, router=router)
    await present.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")
    assert summ.phase_calls == ["p1"]  # gate ran

    # No phase -> gate skipped even though a summarizer is present.
    summ2 = _FakeSummarizer()
    no_phase = _runner(summ=summ2, router=_FakeRouter())
    await no_phase.run(_node(), {"nodes.a.output": "x"})  # phase_id defaults to None
    assert summ2.phase_calls == []


# -- Matrix row: REUSE_SLOT ---------------------------------------------------


async def test_shared_slot_reuse_still_proceeds_to_completion():
    router = _FakeRouter(response={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 7}})
    lm = _FakeLoadManager(acquired=False)
    runner = _runner(router=router, lm=lm)

    result = await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1", fallback=None)

    assert result.outcome == "completed"
    assert lm.acquired_calls == [("n", "llama", 1_000_000)]  # still attempted
    assert lm.released == [("llama", "n")]


# -- Token-usage fallback -----------------------------------------------------


async def test_token_usage_falls_back_to_estimate_when_absent():
    response = {"choices": [{"message": {"content": "hello world" * 8}}]}  # 80 chars
    router = _FakeRouter(response=response)
    runner = _runner(router=router)

    result = await runner.run(_node(), {"nodes.a.output": "x"}, phase_id="p1")

    assert result.output == "hello world" * 8
    assert result.tokens_used == estimate_tokens(result.output) == len(result.output) // 4


# -- Result dataclass shape ---------------------------------------------------


def test_run_result_shape_and_fields():
    res = RunResult(node_id="n", outcome="skipped", skipped_bindings=("nodes.a.output",))
    assert res.model is None and res.tokens_used == 0 and res.attempt == 1
    assert res.skipped_bindings == ("nodes.a.output",)


# -- US4: model-driven tool calling (scripted tool_calls router) --------------


class _ScriptedRouter:
    """Fake router that returns pre-scripted responses in call order.

    Snapshots each request's ``messages`` list so later in-place mutations by
    the tool loop do not rewrite earlier snapshots.
    """

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def route_inference(self, *, model, messages, run_id, node_id, timeout, **kwargs) -> dict:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "run_id": run_id,
                "node_id": node_id,
                "timeout": timeout,
                **kwargs,
            }
        )
        if not self._responses:
            raise AssertionError("scripted router exhausted")
        return self._responses.pop(0)


def _echo_tool(text: str = "", **_: object) -> str:
    return f"echo:{text}"


def _upper_tool(text: str = "", **_: object) -> str:
    return text.upper()


def _text_param() -> ToolParam:
    return ToolParam(type="string", required=True, description="The input text.")


def _registry_with(*tools: tuple[str, object]) -> ToolRegistry:
    registry = ToolRegistry()
    for name, fn in tools:
        registry.register(name, fn, description=f"Test tool {name}.", parameters={"text": _text_param()})
    return registry


def _tool_call_response(call_id: str, name: str, arguments: str, tokens: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                    ],
                },
                "index": 0,
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": tokens},
    }


def _final_response(content: str, tokens: int) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "index": 0, "finish_reason": "stop"}],
        "usage": {"total_tokens": tokens},
    }


async def test_tool_loop_executes_call_feeds_result_back_and_completes():
    registry = _registry_with(("echo", _echo_tool))
    router = _ScriptedRouter(
        [
            _tool_call_response("call-1", "echo", json.dumps({"text": "hi"}), tokens=10),
            _final_response("done: echo:hi", tokens=5),
        ]
    )
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "done: echo:hi"
    assert result.tokens_used == 15  # summed across both turns
    assert len(router.calls) == 2
    first = router.calls[0]
    assert first["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in first["tools"]] == ["echo"]
    # The second turn must carry the assistant tool-call message plus the
    # fed-back tool result.
    second_messages = router.calls[1]["messages"]
    assistant_turns = [m for m in second_messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["tool_calls"][0]["id"] == "call-1"
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"
    assert tool_messages[0]["content"] == "echo:hi"


async def test_direct_answer_with_tools_offered_completes_without_calls():
    registry = _registry_with(("echo", _echo_tool))
    router = _ScriptedRouter([_final_response("just an answer", tokens=7)])
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})  # allow omitted -> all tools offered

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "just an answer"
    assert result.tokens_used == 7
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in call["tools"]] == ["echo"]


async def test_tool_loop_bound_exceeded_fails_after_max_rounds():
    registry = _registry_with(("echo", _echo_tool))
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "echo", json.dumps({"text": "1"}), tokens=3),
            _tool_call_response("c2", "echo", json.dumps({"text": "2"}), tokens=3),
        ]
    )
    lm = _FakeLoadManager()
    runner = _runner(router=router, lm=lm, tool_registry=registry)
    node = _node(tools={"allow": ["echo"], "max_rounds": 2})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1", attempt=2, fallback="fb")

    assert result.outcome == "failed"
    assert result.error == "tool-loop bound exceeded after 2 round-trips"
    assert result.model == "llama"
    assert result.attempt == 2
    assert result.fallback == "fb"
    assert len(router.calls) == 2
    assert lm.released == [("llama", "n")]  # one slot for the whole attempt


async def test_allow_list_restricts_offered_tools():
    registry = _registry_with(("echo", _echo_tool), ("upper", _upper_tool))
    router = _ScriptedRouter([_final_response("ok", tokens=1)])
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"allow": ["upper"], "max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert [t["function"]["name"] for t in router.calls[0]["tools"]] == ["upper"]


async def test_unknown_tool_call_is_fed_back_not_failed():
    registry = _registry_with(("echo", _echo_tool))
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "ghost", json.dumps({}), tokens=4),
            _final_response("recovered", tokens=2),
        ]
    )
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "recovered"
    tool_messages = [m for m in router.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "unknown tool 'ghost'" in tool_messages[0]["content"]


async def test_invalid_argument_json_is_fed_back_not_failed():
    registry = _registry_with(("echo", _echo_tool))
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "echo", "{not valid json", tokens=4),
            _final_response("recovered", tokens=2),
        ]
    )
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "recovered"
    tool_messages = [m for m in router.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "invalid argument JSON" in tool_messages[0]["content"]


async def test_tools_enabled_without_registry_is_defensive_failure():
    router = _ScriptedRouter([_final_response("never reached", tokens=1)])
    lm = _FakeLoadManager()
    runner = _runner(router=router, lm=lm)  # no tool_registry
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "failed"
    assert result.error == "tool calling enabled but no tool registry configured"
    assert router.calls == []  # no inference attempted
    assert lm.acquired_calls == []  # no slot acquired


async def test_node_without_tools_is_unchanged_and_offers_nothing():
    router = _ScriptedRouter([_final_response("plain", tokens=2)])
    registry = _registry_with(("echo", _echo_tool))
    runner = _runner(router=router, tool_registry=registry)
    node = _node()  # tools absent -> today's behavior

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "plain"
    assert "tools" not in router.calls[0]
    assert "tool_choice" not in router.calls[0]


# -- US4: model-driven sandboxed tool results (spec 006, T027) ------------------

# Contract §6: the standard keys plus the single additive `environment`.
_CONTRACT_KEYS = {
    "status",
    "exit_code",
    "output",
    "stderr",
    "error",
    "duration_seconds",
    "command",
    "environment",
}


class _SandboxExecBackend:
    """ToolExecutionBackend double: the local executors stamped 'sandbox'
    (the DockerSandboxBackend result seam, contract §6) without a daemon."""

    async def shell(self, command, working_dir=None, timeout_seconds=None, caps=None):
        result = await execute_shell(command, working_dir=working_dir, timeout_seconds=timeout_seconds)
        return replace(result, environment="sandbox")

    async def program(self, program, interpreter=None, working_dir=None, timeout_seconds=None, caps=None):
        result = await execute_program(
            program, interpreter=interpreter, working_dir=working_dir, timeout_seconds=timeout_seconds
        )
        return replace(result, environment="sandbox")

    async def file_write(self, path, content, caps=None):
        result = await execute_file_write(path, content)
        return replace(result, environment="sandbox")


def _us4_workflow() -> Workflow:
    return Workflow(name="us4", nodes={"n": AgentNode(id="n", type="agent", model="llama", prompt_template="p")}, entry_point="n")


def _sandboxed_registry() -> ToolRegistry:
    return build_workflow_tool_registry(_us4_workflow(), execution_backend=_SandboxExecBackend())


def _tool_message(router: _ScriptedRouter) -> dict:
    tool_messages = [m for m in router.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    return tool_messages[0]


async def _local_shell_result(command: str):
    """One local (un-sandboxed) run of the same generic tool, for shape parity."""
    local_registry = build_workflow_tool_registry(_us4_workflow(), execution_backend=LocalToolBackend())
    return await local_registry.get("shell")(command=command)


async def test_sandboxed_tool_result_fed_back_in_local_shape():
    registry = _sandboxed_registry()
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "shell", json.dumps({"command": "echo from-sandbox"}), tokens=10),
            _final_response("done", tokens=5),
        ]
    )
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    assert result.outcome == "completed"
    assert result.output == "done"
    assert result.tokens_used == 15  # summed across both turns
    offered = [t["function"]["name"] for t in router.calls[0]["tools"]]
    assert "shell" in offered
    message = _tool_message(router)
    assert message["tool_call_id"] == "c1"
    report = json.loads(message["content"])
    # Same shape as a local tool's result, key for key (SC-009/FR-017).
    local_result = await _local_shell_result("echo from-sandbox")
    local_report = local_result.as_dict()
    assert set(report) == set(local_report) == _CONTRACT_KEYS
    assert report["environment"] == "sandbox"
    assert local_report["environment"] == "local"
    assert report["status"] == "success"
    assert report["exit_code"] == 0
    assert "from-sandbox" in report["output"]
    assert report["command"] == "echo from-sandbox"
    assert report["error"] is None
    assert report["duration_seconds"] >= 0.0


async def test_sandboxed_tool_failure_fed_back_as_structured_report():
    registry = _sandboxed_registry()
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "shell", json.dumps({"command": "echo partial; exit 3"}), tokens=6),
            _final_response("recovered", tokens=2),
        ]
    )
    runner = _runner(router=router, tool_registry=registry)
    node = _node(tools={"max_rounds": 4})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1")

    # A non-success execution is fed back to the model — per-call problems
    # never fail the step.
    assert result.outcome == "completed"
    report = json.loads(_tool_message(router)["content"])
    assert set(report) == _CONTRACT_KEYS
    assert report["status"] == "failed"
    assert report["exit_code"] == 3
    assert report["error"] == "exited with code 3"
    assert "partial" in report["output"]
    assert report["environment"] == "sandbox"
    assert report["command"] == "echo partial; exit 3"


async def test_sandboxed_tool_loop_bound_behavior_unchanged():
    registry = _sandboxed_registry()
    router = _ScriptedRouter(
        [
            _tool_call_response("c1", "shell", json.dumps({"command": "echo 1"}), tokens=3),
            _tool_call_response("c2", "shell", json.dumps({"command": "echo 2"}), tokens=3),
        ]
    )
    lm = _FakeLoadManager()
    runner = _runner(router=router, lm=lm, tool_registry=registry)
    node = _node(tools={"max_rounds": 2})

    result = await runner.run(node, {"nodes.a.output": "x"}, phase_id="p1", attempt=2, fallback="fb")

    assert result.outcome == "failed"
    assert result.error == "tool-loop bound exceeded after 2 round-trips"
    assert result.model == "llama"
    assert result.attempt == 2
    assert result.fallback == "fb"
    assert len(router.calls) == 2
    assert lm.released == [("llama", "n")]  # one slot for the whole attempt
