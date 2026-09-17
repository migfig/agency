from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace

import agency.executor.node_runners as _nr_module
from agency.executor.agent_runner import RunResult as AgentReexportedRunResult
from agency.executor.contracts import RunResult
from agency.executor.exec_tools import (
    ToolExecutionError,
    ToolExecutionResult,
    execute_file_write,
    execute_program,
    execute_shell,
)
from agency.executor.execution_env import LocalToolBackend
from agency.executor.node_runners import ConditionError, NonAgentRunner, parse_condition
from agency.executor.tool_registry import (
    DEFAULT_REGISTRY,
    ToolRegistry,
    build_workflow_tool_registry,
)
from agency.yaml_engine.schema import (
    BroadcastNode,
    ConditionalNode,
    HumanInLoopNode,
    MergeNode,
    ToolCallNode,
    Workflow,
)


class _FakeContextStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], object] = {}

    def record_output(self, phase_id, node_id, value):
        self.records[(phase_id, node_id)] = value


class _FakeInput:
    def __init__(self, value: str = "yes", delay: float = 0.0) -> None:
        self.value = value
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, node_id: str, prompt: str) -> str:
        self.calls.append((node_id, prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.value


def _cond(condition: str, branches: dict[str, str] | None = None, node_id: str = "c") -> ConditionalNode:
    return ConditionalNode(
        id=node_id,
        type="conditional",
        condition=condition,
        branches=branches if branches is not None else {"true": "t1", "false": "t2"},
    )


def _merge(inputs: list[str], strategy: str = "any", node_id: str = "m") -> MergeNode:
    return MergeNode(id=node_id, type="merge", inputs=inputs, strategy=strategy)  # type: ignore[arg-type]


def _broadcast(targets: list[str], node_id: str = "b") -> BroadcastNode:
    return BroadcastNode(id=node_id, type="broadcast", targets=targets)


def _hil(prompt: str = "P", timeout: int | None = None, node_id: str = "h") -> HumanInLoopNode:
    return HumanInLoopNode(id=node_id, type="human_in_loop", prompt=prompt, timeout_seconds=timeout)


def _tool(tool_name: str = "echo", arguments_template: str = "{}", node_id: str = "tc") -> ToolCallNode:
    return ToolCallNode(id=node_id, type="tool_call", tool_name=tool_name, arguments_template=arguments_template)


def _runner(tool_registry=None, input_source=None, context_store=None) -> NonAgentRunner:
    return NonAgentRunner(
        tool_registry=tool_registry,
        input_source=input_source,
        context_store=context_store,
    )


# -- Matrix rows: COND-* ------------------------------------------------------


async def test_cond_eq_true():
    result = await _runner().run(_cond('{{ nodes.a.output }} == "x"'), {"nodes.a.output": "x"})
    assert result.outcome == "completed"
    assert result.selected_label == "true"
    assert result.branch_target == "t1"
    assert result.skipped_targets == ("t2",)


async def test_cond_eq_false():
    result = await _runner().run(_cond('{{ nodes.a.output }} == "x"'), {"nodes.a.output": "y"})
    assert result.outcome == "completed"
    assert result.selected_label == "false"
    assert result.branch_target == "t2"
    assert result.skipped_targets == ("t1",)


async def test_cond_contains_true():
    result = await _runner().run(_cond('{{ nodes.a.output }} contains "sec"'), {"nodes.a.output": "has sec here"})
    assert result.outcome == "completed"
    assert result.selected_label == "true"


async def test_cond_not_contains_true():
    result = await _runner().run(_cond('{{ nodes.a.output }} not contains "sec"'), {"nodes.a.output": "clean"})
    assert result.outcome == "completed"
    assert result.selected_label == "true"


async def test_cond_missing_label_is_failed():
    # condition is true, but branches only defines 'false'
    node = _cond('{{ nodes.a.output }} == "x"', branches={"false": "t2"})
    result = await _runner().run(node, {"nodes.a.output": "x"})
    assert result.outcome == "failed"
    assert "true" in (result.error or "")


async def test_cond_bad_expr_is_failed():
    result = await _runner().run(_cond("not a valid expression"), {"nodes.a.output": "x"})
    assert result.outcome == "failed"
    assert "invalid condition" in (result.error or "")


async def test_cond_unresolved_is_skipped():
    result = await _runner().run(_cond('{{ nodes.a.output }} == "x"'), {})
    assert result.outcome == "skipped"
    assert result.skipped_bindings == ("nodes.a.output",)


async def test_cond_records_selected_label():
    cs = _FakeContextStore()
    result = await _runner(context_store=cs).run(_cond('{{ nodes.a.output }} == "x"'), {"nodes.a.output": "x"}, phase_id="p1")
    assert result.outcome == "completed"
    assert cs.records[("p1", "c")] == "true"


# -- Matrix rows: MERGE-* -----------------------------------------------------


async def test_merge_all_joins_in_order():
    ctx = {"nodes.a.output": "a.out", "nodes.b.output": "b.out"}
    result = await _runner().run(_merge(["a", "b"], strategy="all"), ctx)
    assert result.outcome == "completed"
    assert result.output == "a.out\nb.out"


async def test_merge_all_missing_is_skipped():
    result = await _runner().run(_merge(["a", "b"], strategy="all"), {"nodes.a.output": "a.out"})
    assert result.outcome == "skipped"
    assert result.skipped_bindings == ("nodes.b.output",)


async def test_merge_any_uses_first_present_and_discards_others():
    result = await _runner().run(_merge(["a", "b"], strategy="any"), {"nodes.a.output": "a.out"})
    assert result.outcome == "completed"
    assert result.output == "a.out"


async def test_merge_none_is_skipped():
    result = await _runner().run(_merge(["a", "b"], strategy="any"), {})
    assert result.outcome == "skipped"
    assert result.skipped_bindings == ("nodes.a.output", "nodes.b.output")


async def test_merge_records_output():
    cs = _FakeContextStore()
    result = await _runner(context_store=cs).run(_merge(["a", "b"], strategy="all"),
                                                 {"nodes.a.output": "A", "nodes.b.output": "B"}, phase_id="p1")
    assert result.outcome == "completed"
    assert cs.records[("p1", "m")] == "A\nB"


# -- Matrix row: BROADCAST ----------------------------------------------------


async def test_broadcast_emits_upstream_output_and_ignores_targets():
    # targets must NOT be walked by the runner; only deps (upstream) feed output.
    node = _broadcast(targets=["x", "y"])
    result = await _runner().run(node, {"nodes.u.output": "U-out"}, deps=["u"])
    assert result.outcome == "completed"
    assert result.output == "U-out"


async def test_broadcast_no_upstream_is_empty():
    result = await _runner().run(_broadcast(targets=["x"]), {}, deps=())
    assert result.outcome == "completed"
    assert result.output == ""


# -- Matrix rows: HIL-* -------------------------------------------------------


async def test_hil_ok():
    src = _FakeInput(value="yes")
    result = await _runner(input_source=src).run(_hil(prompt="Go?", timeout=None), {})
    assert result.outcome == "completed"
    assert result.output == "yes"
    assert src.calls == [("h", "Go?")]


async def test_hil_timeout_is_failed():
    src = _FakeInput(value="yes", delay=2.0)
    result = await _runner(input_source=src).run(_hil(prompt="Go?", timeout=1), {})
    assert result.outcome == "failed"
    assert "timed out" in (result.error or "")


async def test_hil_no_timeout_completes_when_input_arrives():
    src = _FakeInput(value="later", delay=0.0)
    result = await _runner(input_source=src).run(_hil(prompt="Go?", timeout=None), {})
    assert result.outcome == "completed"
    assert result.output == "later"


async def test_hil_records_answer():
    cs = _FakeContextStore()
    src = _FakeInput(value="ship it")
    result = await _runner(input_source=src, context_store=cs).run(_hil(prompt="Go?"), {}, phase_id="p1")
    assert result.outcome == "completed"
    assert cs.records[("p1", "h")] == "ship it"


# -- Matrix rows: TOOL-* ------------------------------------------------------


async def test_tool_ok_uses_bound_args():
    node = _tool(tool_name="upper", arguments_template='{"text": "{{ nodes.a.output }}"}')
    result = await _runner().run(node, {"nodes.a.output": "hi"})
    assert result.outcome == "completed"
    assert result.output == "HI"


async def test_tool_ok_plain_json_args():
    result = await _runner().run(_tool(tool_name="echo", arguments_template='{"text": "hi"}'), {})
    assert result.outcome == "completed"
    assert result.output == "hi"


async def test_tool_records_output():
    cs = _FakeContextStore()
    result = await _runner(context_store=cs).run(_tool(tool_name="upper", arguments_template='{"text": "a"}'), {}, phase_id="p1")
    assert result.outcome == "completed"
    assert cs.records[("p1", "tc")] == "A"


async def test_tool_raises_is_failed():
    reg = ToolRegistry()

    def boom(**_):
        raise ValueError("kapow")

    reg.register("boom", boom)
    result = await _runner(tool_registry=reg).run(_tool(tool_name="boom"), {})
    assert result.outcome == "failed"
    assert "kapow" in (result.error or "")


async def test_tool_unknown_is_failed():
    result = await _runner().run(_tool(tool_name="nosuch"), {})
    assert result.outcome == "failed"
    assert "nosuch" in (result.error or "")


async def test_tool_bad_json_is_failed():
    result = await _runner().run(_tool(tool_name="echo", arguments_template="not json"), {})
    assert result.outcome == "failed"
    assert "invalid arguments JSON" in (result.error or "")


async def test_tool_args_legacy_path_numeric_binding():
    # Template that is only valid JSON after substitution keeps the legacy
    # raw-substitution path (no parse-first possible).
    reg = ToolRegistry()
    reg.register("nstr", lambda n: f"n={n}")
    node = _tool(tool_name="nstr", arguments_template='{"n": {{ nodes.a.output }}}')
    result = await _runner(tool_registry=reg).run(node, {"nodes.a.output": "5"})
    assert result.outcome == "completed"
    assert result.output == "n=5"


async def test_tool_args_binding_with_json_sensitive_content(tmp_path):
    # US3 generate->save pattern: agent output containing double quotes,
    # backslashes, and newlines must round-trip through the binding intact.
    reg = ToolRegistry()
    _register_exec_tools(reg)
    target = tmp_path / "gen.py"
    node = _tool(
        tool_name="write_file",
        arguments_template='{"path": "{{ nodes.path_node.output }}", "content": "{{ nodes.generate.output }}"}',
        node_id="save",
    )
    code = 'print("hello from the generated program")\ny = \'a \\n b\'\nprint(y)\n'
    context = {"nodes.generate.output": code, "nodes.path_node.output": str(target)}
    result = await _runner(tool_registry=reg).run(node, context)
    assert result.outcome == "completed"
    assert result.output == str(target)
    assert target.read_text(encoding="utf-8") == code


async def test_tool_unresolved_is_skipped():
    result = await _runner().run(_tool(tool_name="upper", arguments_template='{"text": "{{ nodes.a.output }}"}'), {})
    assert result.outcome == "skipped"
    assert result.skipped_bindings == ("nodes.a.output",)


# -- Matrix rows: TOOL-EXEC-* (spec 005, US1) --------------------------------


def _register_exec_tools(reg: ToolRegistry) -> None:
    """Register the generic shell/program tools as the workflow registry does:
    return the ToolExecutionResult on success, raise ToolExecutionError on
    non-success."""

    async def shell(command, working_dir=None, timeout_seconds=None):
        result = await execute_shell(command, working_dir=working_dir, timeout_seconds=timeout_seconds)
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    async def program(program, interpreter=None, working_dir=None, timeout_seconds=None):
        result = await execute_program(
            program,
            interpreter=interpreter,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
        )
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    async def write_file(path, content):
        result = await execute_file_write(path, content)
        if result.status != "success":
            raise ToolExecutionError(result)
        return result

    reg.register("shell", shell)
    reg.register("program", program)
    reg.register("write_file", write_file)


async def test_tool_execution_error_is_failed_with_structured_report():
    reg = ToolRegistry()
    captured = ToolExecutionResult(
        status="failed",
        exit_code=1,
        output="partial-out",
        stderr="partial-err",
        error="exited with code 1",
        duration_seconds=0.25,
        command="false",
        kind="shell",
    )

    def boom(**_):
        raise ToolExecutionError(captured)

    reg.register("boom", boom)
    result = await _runner(tool_registry=reg).run(_tool(tool_name="boom"), {})
    assert result.outcome == "failed"
    error = result.error or ""
    assert "boom" in error
    # the FULL structured report is embedded (what ran, status, exit code,
    # captured stdout/stderr, error) so the run record stays diagnosable
    report = json.loads(error[error.index("{"):])
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert report["output"] == "partial-out"
    assert report["stderr"] == "partial-err"
    assert report["error"] == "exited with code 1"
    assert report["command"] == "false"


async def test_shell_success_outputs_structured_json():
    reg = ToolRegistry()
    _register_exec_tools(reg)
    node = _tool(tool_name="shell", arguments_template='{"command": "echo hello-node"}')
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "completed"
    report = json.loads(result.output)
    assert report["status"] == "success"
    assert report["exit_code"] == 0
    assert report["command"] == "echo hello-node"
    assert "hello-node" in report["output"]
    assert report["stderr"] == ""
    assert report["error"] is None
    assert report["duration_seconds"] >= 0.0


async def test_program_success_outputs_structured_json(tmp_path):
    script = tmp_path / "tiny.py"
    script.write_text("print('from-program')\n", encoding="utf-8")
    reg = ToolRegistry()
    _register_exec_tools(reg)
    node = _tool(tool_name="program", arguments_template=json.dumps({"program": str(script)}))
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "completed"
    report = json.loads(result.output)
    assert report["status"] == "success"
    assert report["exit_code"] == 0
    assert report["command"] == str(script)
    assert "from-program" in report["output"]


async def test_exec_tool_timeout_is_failed_not_crash():
    reg = ToolRegistry()
    _register_exec_tools(reg)
    node = _tool(
        tool_name="shell",
        arguments_template='{"command": "sleep 5", "timeout_seconds": 1}',
    )
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "failed"
    error = result.error or ""
    report = json.loads(error[error.index("{"):])
    assert report["status"] == "timed_out"
    assert report["exit_code"] is None
    assert "exceeded 1s timeout" in (report["error"] or "")


async def test_tool_unknown_exact_reason():
    result = await _runner().run(_tool(tool_name="nosuch"), {})
    assert result.outcome == "failed"
    assert "unknown tool 'nosuch'" in (result.error or "")


async def test_string_utility_tools_byte_for_byte_pinned():
    # SC-008 regression pins: the six pre-existing string utilities behave
    # exactly as before through the tool_call path.
    cases = [
        ("echo", {"text": "hi there"}, "hi there"),
        ("upper", {"text": "hi"}, "HI"),
        ("lower", {"text": "HI"}, "hi"),
        ("trim", {"text": "  x  "}, "x"),
        ("length", {"text": "abc"}, "3"),
        ("replace", {"text": "a b", "old": "b", "new": "c"}, "a c"),
    ]
    for name, args, expected in cases:
        node = _tool(tool_name=name, arguments_template=json.dumps(args))
        result = await _runner().run(node, {})
        assert result.outcome == "completed", name
        assert result.output == expected, name


# -- Matrix rows: TOOL-EXEC-* (spec 005, US2) --------------------------------


async def test_write_file_success_outputs_plain_path(tmp_path):
    reg = ToolRegistry()
    _register_exec_tools(reg)
    target = tmp_path / "programs" / "check.py"
    node = _tool(
        tool_name="write_file",
        arguments_template=json.dumps({"path": str(target), "content": "print(6 * 7)"}),
    )
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "completed"
    # contract §3: the node output is the written path as a PLAIN string
    # (what downstream steps bind), distinct from the JSON report
    # shell/program tools produce
    assert result.output == str(target)
    assert not result.output.startswith("{")
    assert target.read_text(encoding="utf-8") == "print(6 * 7)"


async def test_write_file_failure_is_failed_with_reason(tmp_path):
    reg = ToolRegistry()
    _register_exec_tools(reg)
    blocker = tmp_path / "blocker"
    blocker.mkdir()  # target path is an existing directory
    node = _tool(
        tool_name="write_file",
        arguments_template=json.dumps({"path": str(blocker), "content": "x"}),
    )
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "failed"
    error = result.error or ""
    report = json.loads(error[error.index("{"):])
    assert report["status"] == "error"
    assert "could not write" in (report["error"] or "")
    assert str(blocker) in (report["error"] or "")


# -- Matrix rows: TOOL-EXEC-* (spec 005, US5) ---------------------------------


async def test_failed_exec_node_error_embeds_full_structured_report():
    # FR-014 / SC-005: a failed executable-tool node's error embeds the
    # complete report (what ran, status, exit_code, captured stdout/stderr,
    # error) so execution.jsonl captures output even on failure.
    reg = ToolRegistry()
    _register_exec_tools(reg)
    command = "echo partial-out; echo partial-err >&2; exit 3"
    node = _tool(tool_name="shell", arguments_template=json.dumps({"command": command}))
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "failed"
    error = result.error or ""
    assert error.startswith("tool 'shell' execution failed:")
    report = json.loads(error[error.index("{"):])
    assert report["status"] == "failed"
    assert report["exit_code"] == 3
    assert "partial-out" in report["output"]
    assert "partial-err" in report["stderr"]
    assert report["error"] == "exited with code 3"
    assert report["command"] == command
    assert report["duration_seconds"] >= 0.0


async def test_exec_node_could_not_run_is_failed_with_report(tmp_path):
    # SC-005: an unavailable run (spawn failure) is a failed node with the
    # structured report, not a crash.
    reg = ToolRegistry()
    _register_exec_tools(reg)
    node = _tool(
        tool_name="shell",
        arguments_template=json.dumps(
            {"command": "echo hi", "working_dir": str(tmp_path / "missing-dir")}
        ),
    )
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "failed"
    error = result.error or ""
    assert "tool 'shell' execution error:" in error
    report = json.loads(error[error.index("{"):])
    assert report["status"] == "error"
    assert report["exit_code"] is None
    assert "could not run" in (report["error"] or "")
    assert report["command"] == "echo hi"


async def test_unknown_generic_exec_tool_exact_reason():
    # SC-005 / contract §6: unavailable/unknown tool fails with the exact
    # reason `unknown tool '<n>'` (the default registry has no exec tools).
    result = await _runner().run(_tool(tool_name="shell"), {})
    assert result.outcome == "failed"
    assert "unknown tool 'shell'" in (result.error or "")
    assert result.error == "tool_call node 'tc': unknown tool 'shell'"


# -- Shared contract ----------------------------------------------------------


def test_agent_runner_reexports_same_run_result():
    # Relocating RunResult must not change the object the 5.2 import path sees.
    assert AgentReexportedRunResult is RunResult


def test_run_result_new_fields_default_to_empty():
    res = RunResult(node_id="n", outcome="completed")
    assert res.selected_label is None
    assert res.branch_target is None
    assert res.skipped_targets == ()


def test_parse_condition_operators():
    assert parse_condition('a == "a"') is True
    assert parse_condition('a == "b"') is False
    assert parse_condition('a != "b"') is True
    assert parse_condition('has sec here contains "sec"') is True
    assert parse_condition('clean not contains "sec"') is True


def test_parse_condition_rejects_bad_expr():
    try:
        parse_condition("nonsense without operator")
    except ConditionError:
        pass
    else:  # pragma: no cover - defensive
        assert False, "expected ConditionError"


def test_no_event_bus_usage_in_runner_source():
    src = inspect.getsource(_nr_module)
    assert "event_bus" not in src
    assert ".publish" not in src


# -- Tool registry sanity -----------------------------------------------------


def test_default_registry_has_six_pure_tools():
    names = DEFAULT_REGISTRY.names()
    assert names == ["echo", "length", "lower", "replace", "trim", "upper"]
    assert DEFAULT_REGISTRY.get("upper")("x") == "X"
    assert DEFAULT_REGISTRY.get("replace")("a b", old="b", new="c") == "a c"


# -- Matrix rows: TOOL-ENV-* (spec 006, US2) ----------------------------------


_REPORT_KEYS = {
    "status",
    "exit_code",
    "output",
    "stderr",
    "error",
    "duration_seconds",
    "command",
    "environment",
}


class _SandboxLikeBackend:
    """Test double reproducing :class:`DockerSandboxBackend`'s result seam:
    the same executor delegation, stamped ``environment="sandbox"``
    (contract §6) — without requiring a Docker daemon."""

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


def _shell_wf() -> Workflow:
    return Workflow(
        name="tool-env",
        nodes={
            "tc": ToolCallNode(
                id="tc",
                type="tool_call",
                tool_name="shell",
                arguments_template='{"command": "echo env-node"}',
            )
        },
        entry_point="tc",
    )


async def test_sandboxed_node_output_carries_sandbox_environment():
    reg = build_workflow_tool_registry(_shell_wf(), execution_backend=_SandboxLikeBackend())
    node = _tool(tool_name="shell", arguments_template='{"command": "echo sandboxed"}')
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "completed"
    report = json.loads(result.output)
    assert report["environment"] == "sandbox"
    # Same 8-field report shape, only the environment value differs (SC-010).
    assert set(report) == _REPORT_KEYS
    assert report["status"] == "success"
    assert report["exit_code"] == 0
    assert "sandboxed" in report["output"]


async def test_local_node_output_is_byte_for_byte_local_shape():
    reg = build_workflow_tool_registry(_shell_wf(), execution_backend=LocalToolBackend())
    node = _tool(tool_name="shell", arguments_template='{"command": "echo local-node"}')
    result = await _runner(tool_registry=reg).run(node, {})
    assert result.outcome == "completed"
    report = json.loads(result.output)
    assert report["environment"] == "local"
    # SC-010: byte-for-byte the pre-feature shape (the 7 standard fields)
    # plus the additive ``environment`` field — nothing else differs from a
    # direct exec_tools report.
    direct = (await execute_shell("echo local-node")).as_dict()
    assert set(report) == set(direct) == _REPORT_KEYS
    for key in ("status", "exit_code", "output", "stderr", "error", "command"):
        assert report[key] == direct[key]
