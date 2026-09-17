"""Tests for fallback agent routing (Story 4.5).

Covers the ``is_final_failure`` gate, the ``FallbackSupervisor`` dispatch rules
over a real :class:`EventBus`, and the full-stack matrix scenarios (FB_SUCCESS,
FB_DOUBLE_FAIL, FB_NO_RETRY, NO_FALLBACK parity, LATE_DUP) that combine both
supervisors with :class:`RunLogger` and :class:`RunStateStore`.
"""
from __future__ import annotations

import json

from agency.core.event_bus import EventBus, UnsubscribeToken
from agency.core.events import (
    EVENT_FALLBACK_ACTIVATED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_STARTED,
    EventEnvelope,
    FallbackActivated,
    NodeCompleted,
    NodeFailed,
    NodeRetrying,
    NodeStarted,
)
from agency.executor.fallback import FallbackSupervisor
from agency.executor.retry import (
    RetrySupervisor,
    get_fallback,
    is_final_failure,
)
from agency.executor.run_log import RunLogger
from agency.executor.run_state import RunStateStore, recover_status
from agency.tui.simulator import (
    simulate_fallback_attempt,
    simulate_node_attempt,
    simulated_duration,
    simulated_output,
    simulated_tokens,
)
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import (
    AgentNode,
    Edge,
    MergeNode,
    RetryPolicy,
    ToolCallNode,
    Workflow,
)

RUN_ID = "fallback-run"


def _wf() -> Workflow:
    """Workflow: 'a' retries 2x with fallback; 'c' fallback only; 't' tool; 'm' merge; 'b', 'd' plain."""
    return Workflow(
        name="fb-wf",
        nodes={
            "a": AgentNode(
                id="a",
                type="agent",
                model="m-a",
                prompt_template="pa",
                retry=RetryPolicy(max_attempts=2),
                fallback="backup-a",
            ),
            "b": AgentNode(id="b", type="agent", model="m-b", prompt_template="pb"),
            "c": AgentNode(
                id="c", type="agent", model="m-c", prompt_template="pc", fallback="backup-c"
            ),
            "t": ToolCallNode(
                id="t",
                type="tool_call",
                tool_name="add",
                arguments_template="{}",
                fallback="backup-t",
            ),
            "m": MergeNode(id="m", type="merge", inputs=["b"], strategy="all"),
            "d": AgentNode(id="d", type="agent", model="m-d", prompt_template="pd"),
        },
        entry_point="a",
        edges=[Edge(from_id="a", to_id="b"), Edge(from_id="b", to_id="m")],
    )


# -- is_final_failure ------------------------------------------------------

class TestIsFinalFailure:
    def test_no_policy_no_fallback_is_final(self):
        assert is_final_failure(None, 1, fallback_configured=False, fallback_failed=False) is True

    def test_no_policy_with_pending_fallback_is_not_final(self):
        assert is_final_failure(None, 1, fallback_configured=True, fallback_failed=False) is False

    def test_no_policy_fallback_already_failed_is_final(self):
        assert is_final_failure(None, 1, fallback_configured=True, fallback_failed=True) is True

    def test_below_budget_with_fallback_is_not_final(self):
        policy = RetryPolicy(max_attempts=3)
        assert is_final_failure(policy, 1, fallback_configured=True, fallback_failed=False) is False

    def test_below_budget_no_fallback_is_not_final(self):
        policy = RetryPolicy(max_attempts=3)
        assert is_final_failure(policy, 1, fallback_configured=False, fallback_failed=False) is False

    def test_exhausted_with_pending_fallback_is_not_final(self):
        policy = RetryPolicy(max_attempts=2)
        assert is_final_failure(policy, 2, fallback_configured=True, fallback_failed=False) is False

    def test_exhausted_fallback_failed_is_final(self):
        policy = RetryPolicy(max_attempts=2)
        assert is_final_failure(policy, 2, fallback_configured=True, fallback_failed=True) is True

    def test_exhausted_no_fallback_is_final(self):
        policy = RetryPolicy(max_attempts=2)
        assert is_final_failure(policy, 2, fallback_configured=False, fallback_failed=False) is True

    def test_over_budget_fallback_failed_is_final(self):
        policy = RetryPolicy(max_attempts=2)
        assert is_final_failure(policy, 4, fallback_configured=False, fallback_failed=True) is True


# -- get_fallback ----------------------------------------------------------

class TestGetFallback:
    def test_returns_fallback_for_configured_agent(self):
        assert get_fallback(_wf(), "a") == "backup-a"

    def test_returns_fallback_for_configured_tool_call(self):
        assert get_fallback(_wf(), "t") == "backup-t"

    def test_returns_none_for_unconfigured_node(self):
        assert get_fallback(_wf(), "b") is None
        assert get_fallback(_wf(), "d") is None

    def test_returns_none_for_non_agent_node(self):
        assert get_fallback(_wf(), "m") is None

    def test_returns_none_for_unknown_node(self):
        assert get_fallback(_wf(), "nope") is None


# -- FallbackSupervisor (integration) --------------------------------------

class _Harness:
    """Builds a FallbackSupervisor whose execute callback publishes scripted outcomes."""

    def __init__(self, wf: Workflow, *, fb_fails: set[str] | None = None) -> None:
        self.bus = EventBus()
        self.activated: list[FallbackActivated] = []
        self.executed: list[tuple[str, str, int]] = []
        self.completed: list[str] = []
        self.failed_fb: list[str] = []
        self._fb_fails = fb_fails
        self._token: UnsubscribeToken | None = None

        async def _collect(envelope: EventEnvelope) -> None:
            self.activated.append(envelope.payload)

        # closure captured over self; subscription happens in start()
        self._collect = _collect

        async def _execute(node_id: str, fallback: str, attempt: int) -> None:
            self.executed.append((node_id, fallback, attempt))
            await self.bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_STARTED,
                    self.bus._next_seq(),
                    NodeStarted(
                        node_id=node_id, run_id=RUN_ID, model=fallback, input=None, attempt=attempt
                    ),
                )
            )
            if self._fb_fails is not None and node_id in self._fb_fails:
                await self.bus.publish(
                    EventEnvelope.create(
                        EVENT_NODE_FAILED,
                        self.bus._next_seq(),
                        NodeFailed(
                            node_id,
                            RUN_ID,
                            fallback,
                            f"fb-err-{node_id}",
                            attempt=attempt,
                            fallback=fallback,
                        ),
                    )
                )
                self.failed_fb.append(node_id)
            else:
                await self.bus.publish(
                    EventEnvelope.create(
                        EVENT_NODE_COMPLETED,
                        self.bus._next_seq(),
                        NodeCompleted(node_id, RUN_ID, fallback, 5, 0.2, "fb-out", fallback),
                    )
                )
                self.completed.append(node_id)

        self.supervisor = FallbackSupervisor(wf, RUN_ID, bus=self.bus, execute=_execute)

    async def start(self) -> None:
        self._token = await self.bus.subscribe(EVENT_FALLBACK_ACTIVATED, self._collect)
        await self.supervisor.start()

    async def fail(self, node_id: str, attempt: int = 1, **kwargs) -> None:
        await self.bus.publish(
            EventEnvelope.create(
                EVENT_NODE_FAILED,
                self.bus._next_seq(),
                NodeFailed(node_id, RUN_ID, "m-a", "boom", attempt=attempt, **kwargs),
            )
        )

    async def stop(self) -> None:
        self._token.cancel()
        self._token = None
        await self.supervisor.close()


class TestFallbackSupervisorDispatch:
    async def test_terminal_failure_dispatches_fallback(self):
        harness = _Harness(_wf())
        await harness.start()
        await harness.fail("a", 2)
        await harness.stop()

        assert harness.executed == [("a", "backup-a", 2)]
        assert harness.completed == ["a"]
        assert len(harness.activated) == 1
        event = harness.activated[0]
        assert event.node_id == "a"
        assert event.run_id == RUN_ID
        assert event.model == "m-a"
        assert event.fallback == "backup-a"
        assert event.error == "boom"
        assert event.attempt == 2

    async def test_non_terminal_failure_never_activates(self):
        harness = _Harness(_wf())
        await harness.start()
        await harness.fail("a", 1)
        await harness.stop()

        assert harness.executed == []
        assert harness.completed == []
        assert harness.activated == []

    async def test_no_fallback_configured_never_activates(self):
        harness = _Harness(_wf())
        await harness.start()
        await harness.fail("d", 1)
        await harness.stop()

        assert harness.executed == []
        assert harness.activated == []

    async def test_no_retry_fallback_activates_on_first_failure(self):
        harness = _Harness(_wf())
        await harness.start()
        await harness.fail("c", 1)
        await harness.stop()

        assert harness.executed == [("c", "backup-c", 1)]
        assert [e.attempt for e in harness.activated] == [1]
        assert harness.completed == ["c"]

    async def test_duplicate_terminal_failure_suppressed(self):
        harness = _Harness(_wf())
        await harness.start()
        await harness.fail("a", 2)
        await harness.fail("a", 2)
        await harness.stop()

        assert harness.executed == [("a", "backup-a", 2)]
        assert len(harness.activated) == 1

    async def test_fallback_attempt_failure_not_reactivated(self):
        harness = _Harness(_wf())
        await harness.start()
        # Direct failure of c's fallback attempt: the ``fallback`` field is set.
        await harness.fail("c", 1, fallback="backup-c")
        await harness.stop()

        assert harness.executed == []
        assert harness.activated == []


class TestFallbackSupervisorDoubleFail:
    async def test_fallback_failure_published_with_tag(self):
        harness = _Harness(_wf(), fb_fails={"a"})
        await harness.start()
        await harness.fail("a", 2)
        await harness.stop()

        assert harness.executed == [("a", "backup-a", 2)]
        assert harness.failed_fb == ["a"]
        assert harness.completed == []
        assert len(harness.activated) == 1


class TestFallbackSupervisorInert:
    async def test_no_execute_supervisor_publishes_nothing(self):
        bus = EventBus()
        activated: list[FallbackActivated] = []

        async def _collect(envelope: EventEnvelope) -> None:
            activated.append(envelope.payload)

        token = await bus.subscribe(EVENT_FALLBACK_ACTIVATED, _collect)
        supervisor = FallbackSupervisor(_wf(), RUN_ID, bus=bus)
        await supervisor.start()
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_FAILED,
                bus._next_seq(),
                NodeFailed("a", RUN_ID, "m-a", "boom", attempt=2),
            )
        )
        await supervisor.close()
        token.cancel()

        assert activated == []


# -- matrix scenarios (supervisors + RunLogger + RunStateStore) -----------

_MATRIX = """
name: fb-matrix
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: pa
    retry:
      max_attempts: 2
    fallback: backup-a
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: pb
  c:
    id: c
    type: agent
    model: m-c
    prompt_template: pc
    fallback: backup-c
  d:
    id: d
    type: agent
    model: m-d
    prompt_template: pd
  e:
    id: e
    type: agent
    model: m-e
    prompt_template: pe
edges:
  - from_id: a
    to_id: b
  - from_id: c
    to_id: e
  - from_id: d
    to_id: e
"""


def _matrix_wf() -> Workflow:
    from agency.yaml_engine.parser import load_workflow

    return load_workflow(_MATRIX)


class _MatrixHarness:
    """Full stack: bus + RunLogger + RunStateStore + both supervisors.

    The first attempt of the driven node is published explicitly (as the
    topological driver would); retries and the fallback then flow through the
    supervisors with the demo simulator as the execute callbacks.
    """

    def __init__(
        self,
        log_dir,
        run_id: str,
        *,
        fail: set[str] | None = None,
        fb_fail: set[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.log_dir = log_dir
        self.workflow = _matrix_wf()
        self.bus = EventBus()
        self._fail = fail
        self._fb_fail = fb_fail
        self.activated: list[FallbackActivated] = []
        self.retrying: list[NodeRetrying] = []

    async def start(self) -> None:
        bus = self.bus
        run_id = self.run_id
        wf = self.workflow
        self.logger = RunLogger(self.log_dir, run_id, wf, bus)
        self.store = RunStateStore(self.log_dir, run_id, wf, bus)
        await self.logger.start()
        await self.store.start()

        async def _collect_fb(envelope: EventEnvelope) -> None:
            self.activated.append(envelope.payload)

        async def _collect_retrying(envelope: EventEnvelope) -> None:
            self.retrying.append(envelope.payload)

        self._fb_token = await bus.subscribe(EVENT_FALLBACK_ACTIVATED, _collect_fb)
        self._retrying_token = await bus.subscribe(EVENT_NODE_RETRYING, _collect_retrying)

        fail, fb_fail = self._fail, self._fb_fail

        async def _attempt(node_id: str, attempt: int) -> None:
            await simulate_node_attempt(
                wf, bus, run_id, node_id, attempt, fail
            )

        async def _fallback(node_id: str, fallback: str, attempt: int) -> None:
            await simulate_fallback_attempt(
                wf, bus, run_id, node_id, fallback, attempt,
                fallback_fail_schedule=fb_fail,
            )

        async def _no_sleep(_delay: float) -> None:
            return None

        self.retry = RetrySupervisor(wf, run_id, bus=bus, execute=_attempt, sleep=_no_sleep)
        self.fallbacks = FallbackSupervisor(wf, run_id, bus=bus, execute=_fallback)
        await self.retry.start()
        await self.fallbacks.start()

    async def stop(self) -> None:
        self._fb_token.cancel()
        self._retrying_token.cancel()
        await self.retry.close()
        await self.fallbacks.close()
        await self.logger.close()
        await self.store.close()

    async def drive_first_attempt(self, node_id: str) -> None:
        node = self.workflow.nodes[node_id]
        model = node.model
        node_input = node.prompt_template
        await self.bus.publish(
            EventEnvelope.create(
                EVENT_NODE_STARTED,
                self.bus._next_seq(),
                NodeStarted(
                    node_id=node_id, run_id=self.run_id, model=model,
                    input=node_input, attempt=1,
                ),
            )
        )
        if self._fail is not None and node_id in self._fail:
            await self.bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_FAILED,
                    self.bus._next_seq(),
                    NodeFailed(
                        node_id, self.run_id, model,
                        f"scripted failure for {node_id}", attempt=1,
                    ),
                )
            )

    def lines(self) -> list[dict]:
        path = self.log_dir / self.run_id / "execution.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def by_type(self, line_type: str) -> list[dict]:
        return [line for line in self.lines() if line.get("type") == line_type]

    def checkpoint(self, node_id: str) -> dict:
        return json.loads(
            (self.log_dir / self.run_id / "checkpoints" / f"{node_id}.json").read_text(encoding="utf-8")
        )

    def wal(self) -> list[dict]:
        return [
            json.loads(line)
            for line in (self.log_dir / self.run_id / "wal.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def statuses(self) -> dict[str, str]:
        return recover_status(self.log_dir, self.run_id, self.workflow)

    def metrics_for(self, node_id: str) -> tuple[int, float]:
        topo = DAGBuilder(self.workflow).topological_sort()
        return simulated_tokens(topo.index(node_id)), simulated_duration(topo.index(node_id))


class TestMatrixFBSuccess:
    async def test_fallback_completion_marks_node_completed_fallback(self, tmp_path):
        harness = _MatrixHarness(tmp_path, "fb-success", fail={"a"})
        await harness.start()
        await harness.drive_first_attempt("a")
        await harness.stop()

        types = [line["type"] for line in harness.lines()]
        assert types == [
            "run_started",
            "node_started",
            "node_retrying",
            "node_fallback_activated",
            "node_completed",
        ]
        # exactly one node_started line: retry/fallback re-starts are deduped
        started = harness.by_type("node_started")
        assert len(started) == 1 and started[0]["node_id"] == "a"
        # the activation line names the primary error and the trigger attempt
        activation = harness.by_type("node_fallback_activated")[0]
        assert activation["node_id"] == "a"
        assert activation["model"] == "m-a"
        assert activation["fallback"] == "backup-a"
        assert activation["attempt"] == 2
        assert activation["error"] == "scripted failure for a"
        # the terminal completed line carries the fallback agent and its tag
        done = harness.by_type("node_completed")[0]
        tokens, duration = harness.metrics_for("a")
        assert done["node_id"] == "a"
        assert done["status"] == "completed-fallback"
        assert done["model"] == "backup-a"
        assert done["fallback"] == "backup-a"
        assert done["input"] == "pa"
        assert done["output"] == simulated_output("a")
        assert done["tokens_used"] == tokens
        assert done["duration_seconds"] == duration
        # one retry then one fallback — no chained second fallback
        assert [r.node_id for r in harness.retrying] == ["a"]
        assert len(harness.activated) == 1

        checkpoint = harness.checkpoint("a")
        assert checkpoint["status"] == "completed-fallback"
        assert checkpoint["model"] == "backup-a"
        assert checkpoint["output_ref"] == "outputs/a.txt"
        assert checkpoint["tokens_used"] == tokens
        run_dir = tmp_path / "fb-success"
        assert (run_dir / "outputs" / "a.txt").read_text(encoding="utf-8") == simulated_output("a")
        # downstream: a is terminal-success so dependents may proceed
        statuses = harness.statuses()
        assert statuses["a"] == "completed-fallback"
        assert statuses["b"] == "pending"


class TestMatrixFBDoubleFail:
    async def test_double_failure_ends_failed_with_skipped_dependents(self, tmp_path):
        harness = _MatrixHarness(tmp_path, "fb-double", fail={"a"}, fb_fail={"a"})
        await harness.start()
        await harness.drive_first_attempt("a")
        await harness.stop()

        types = [line["type"] for line in harness.lines()]
        assert types == [
            "run_started",
            "node_started",
            "node_retrying",
            "node_fallback_activated",
            "node_failed",
            "node_skipped",
        ]
        # primary error in the activation line, fallback error in the terminal line
        activation = harness.by_type("node_fallback_activated")[0]
        assert activation["error"] == "scripted failure for a"
        failed = harness.by_type("node_failed")[0]
        assert failed["node_id"] == "a"
        assert failed["status"] == "failed"
        assert failed["model"] == "backup-a"
        assert failed["fallback"] == "backup-a"
        assert failed["attempt"] == 2
        assert failed["error"] == "scripted fallback failure for a"
        assert harness.by_type("node_completed") == []
        # the fallback failed, so exactly one activation — no second dispatch
        assert len(harness.activated) == 1
        checkpoint = harness.checkpoint("a")
        assert checkpoint["status"] == "failed"
        assert checkpoint["model"] == "backup-a"
        assert checkpoint["output_ref"] is None
        assert harness.statuses()["a"] == "failed"
        # transitive dependent b is skipped: durable checkpoint + WAL line
        # inheriting the triggering failure's seq (4.2 contract)
        assert harness.checkpoint("b")["status"] == "skipped"
        wal = harness.wal()
        fail_line = next(w for w in wal if w.get("node_id") == "a" and w.get("to") == "failed")
        skip_line = next(w for w in wal if w.get("node_id") == "b" and w.get("to") == "skipped")
        assert skip_line["seq"] == fail_line["seq"]


class TestMatrixFBNoRetry:
    async def test_first_failure_triggers_immediate_fallback(self, tmp_path):
        harness = _MatrixHarness(tmp_path, "fb-noretry", fail={"c"}, fb_fail=None)
        await harness.start()
        await harness.drive_first_attempt("c")
        await harness.stop()

        types = [line["type"] for line in harness.lines()]
        assert types == [
            "run_started",
            "node_started",
            "node_fallback_activated",
            "node_completed",
        ]
        activation = harness.by_type("node_fallback_activated")[0]
        assert activation["node_id"] == "c"
        assert activation["attempt"] == 1
        assert activation["fallback"] == "backup-c"
        done = harness.by_type("node_completed")[0]
        assert done["status"] == "completed-fallback"
        assert done["model"] == "backup-c"
        assert done["fallback"] == "backup-c"

        checkpoint = harness.checkpoint("c")
        assert checkpoint["status"] == "completed-fallback"
        assert harness.statuses()["c"] == "completed-fallback"


class TestMatrixNoFallback:
    async def test_plain_failure_behavior_unchanged(self, tmp_path):
        harness = _MatrixHarness(tmp_path, "no-fb", fail={"d"})
        await harness.start()
        await harness.drive_first_attempt("d")
        await harness.stop()

        types = [line["type"] for line in harness.lines()]
        assert types == [
            "run_started",
            "node_started",
            "node_failed",
            "node_skipped",
        ]
        failed = harness.by_type("node_failed")[0]
        assert failed["node_id"] == "d"
        assert failed["status"] == "failed"
        assert failed["model"] == "m-d"
        assert "fallback" not in failed  # no fallback key in the JSON line
        assert harness.by_type("node_fallback_activated") == []
        assert harness.activated == []

        checkpoint = harness.checkpoint("d")
        assert checkpoint["status"] == "failed"
        assert checkpoint["model"] == "m-d"
        # 4.4 skip cascade intact
        assert harness.by_type("node_skipped")[0]["node_id"] == "e"
        assert harness.statuses()["d"] == "failed"
        # dependent e is skipped: durable checkpoint + WAL line inheriting the
        # triggering failure's seq (4.2 contract; dedup'd on WAL replay)
        assert harness.checkpoint("e")["status"] == "skipped"
        wal = harness.wal()
        fail_line = next(w for w in wal if w.get("node_id") == "d" and w.get("to") == "failed")
        skip_line = next(w for w in wal if w.get("node_id") == "e" and w.get("to") == "skipped")
        assert skip_line["seq"] == fail_line["seq"]
