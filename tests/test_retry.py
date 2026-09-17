"""Tests for configurable retry policies (Story 4.4).

Drives ``RetrySupervisor`` over a real :class:`EventBus` with an *execute*
callback that re-publishes failures, so the full retry chain (backoff math,
``NodeRetrying`` payloads, exhaustion, dedup) is exercised end to end.
Because :meth:`EventBus.publish` awaits every handler to completion, no drain
loops are required: by the time a ``publish`` returns, the handler (and any
nested retries it triggered) has finished.
"""
from __future__ import annotations

import pytest

from agency.core.event_bus import EventBus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeRetrying,
)
from agency.executor.retry import (
    RetrySupervisor,
    exponential_delay,
    get_retry_policy,
    is_terminal_failure,
)
from agency.yaml_engine.parser import load_workflow
from agency.yaml_engine.schema import AgentNode, RetryPolicy, ToolCallNode, Workflow

RUN_ID = "retry-run"

RETRY_YAML = """
name: retry-chain
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: prompt-a
    retry:
      max_attempts: 3
      base_delay_seconds: 0.5
      max_delay_seconds: 4.0
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: prompt-b
  t:
    id: t
    type: tool_call
    tool_name: add
    arguments_template: 'x: 1'
    retry:
      max_attempts: 2
edges:
  - from_id: a
    to_id: b
"""


def workflow_with_retry() -> Workflow:
    """workflow: 'a' retries 3x (base 0.5s, cap 4s); 'c' has no policy; 't' is a tool_call."""
    return Workflow(
        name="retry-wf",
        nodes={
            "a": AgentNode(
                id="a",
                type="agent",
                model="m-a",
                prompt_template="p",
                retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0),
            ),
            "c": AgentNode(id="c", type="agent", model="m-c", prompt_template="p"),
            "t": ToolCallNode(
                id="t",
                type="tool_call",
                tool_name="add",
                arguments_template="{}",
                retry=RetryPolicy(max_attempts=2),
            ),
        },
        entry_point="a",
        edges=[],
    )


# -- YAML / schema parsing -------------------------------------------------

class TestRetryPolicyYAML:
    def test_parses_retry_block_on_agent(self):
        wf = load_workflow(RETRY_YAML)
        node = wf.nodes["a"]
        assert isinstance(node, AgentNode)
        assert node.retry is not None
        assert node.retry.max_attempts == 3
        assert node.retry.backoff == "exponential"
        assert node.retry.base_delay_seconds == 0.5
        assert node.retry.max_delay_seconds == 4.0

    def test_parses_retry_block_on_tool_call(self):
        wf = load_workflow(RETRY_YAML)
        node = wf.nodes["t"]
        assert isinstance(node, ToolCallNode)
        assert node.retry is not None
        assert node.retry.max_attempts == 2
        assert node.retry.base_delay_seconds == 1.0  # model default

    def test_missing_retry_defaults_none(self):
        wf = load_workflow(RETRY_YAML)
        assert wf.nodes["b"].retry is None

    def test_model_defaults(self):
        policy = RetryPolicy(max_attempts=1)
        assert policy.backoff == "exponential"
        assert policy.base_delay_seconds == 1.0
        assert policy.max_delay_seconds is None
        assert policy.timeout_seconds is None

    def test_max_attempts_min_one(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)


# -- get_retry_policy ------------------------------------------------------

class TestGetRetryPolicy:
    def test_returns_policy_for_configured_agent(self):
        wf = workflow_with_retry()
        assert get_retry_policy(wf, "a") == wf.nodes["a"].retry

    def test_returns_policy_for_configured_tool_call(self):
        wf = workflow_with_retry()
        assert get_retry_policy(wf, "t") == wf.nodes["t"].retry

    def test_returns_none_for_unconfigured_node(self):
        wf = workflow_with_retry()
        assert get_retry_policy(wf, "c") is None

    def test_returns_none_for_unknown_node(self):
        wf = workflow_with_retry()
        assert get_retry_policy(wf, "nope") is None


# -- is_terminal_failure ---------------------------------------------------

class TestIsTerminalFailure:
    def test_no_policy_is_terminal(self):
        assert is_terminal_failure(None, 1) is True

    def test_attempt_ge_max_is_terminal(self):
        policy = RetryPolicy(max_attempts=3)
        assert is_terminal_failure(policy, 3) is True
        assert is_terminal_failure(policy, 4) is True

    def test_attempt_lt_max_is_not_terminal(self):
        policy = RetryPolicy(max_attempts=3)
        assert is_terminal_failure(policy, 1) is False
        assert is_terminal_failure(policy, 2) is False


# -- exponential_delay -----------------------------------------------------

class TestExponentialDelay:
    def test_first_attempt_is_base(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0)
        assert exponential_delay(policy, 1) == 1.0

    def test_doubles_each_attempt(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0)
        assert exponential_delay(policy, 2) == 2.0
        assert exponential_delay(policy, 3) == 4.0

    def test_capped_at_max_delay(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=4.0)
        assert exponential_delay(policy, 3) == 4.0
        assert exponential_delay(policy, 4) == 4.0

    def test_fractional_base(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.25)
        assert exponential_delay(policy, 1) == 0.25
        assert exponential_delay(policy, 2) == 0.5


# -- RetrySupervisor (integration) ----------------------------------------

class _Harness:
    """Builds a supervisor whose execute callback publishes scripted outcomes."""

    def __init__(self, wf: Workflow, *, fail_attempts: set[int] | None) -> None:
        self.bus = EventBus()
        self.retrying: list[NodeRetrying] = []
        self.executed: list[tuple[str, int]] = []
        self.slept: list[float] = []
        self.completed: list[str] = []
        self._fail = fail_attempts

        async def _sleep(d: float) -> None:
            self.slept.append(d)

        async def _execute(node_id: str, attempt: int) -> None:
            self.executed.append((node_id, attempt))
            if self._fail is not None and attempt in self._fail:
                await self.bus.publish(
                    EventEnvelope.create(
                        EVENT_NODE_FAILED,
                        self.bus._next_seq(),
                        NodeFailed(node_id, RUN_ID, "m-a", f"err{attempt}", attempt=attempt),
                    )
                )
            else:
                await self.bus.publish(
                    EventEnvelope.create(
                        EVENT_NODE_COMPLETED,
                        self.bus._next_seq(),
                        NodeCompleted(node_id, RUN_ID, "m-a", 1, 0.1),
                    )
                )
                self.completed.append(node_id)

        self.supervisor = RetrySupervisor(
            wf, RUN_ID, bus=self.bus, execute=_execute, sleep=_sleep
        )

    async def start(self) -> None:
        async def _collect_retrying(envelope: EventEnvelope) -> None:
            # EventBus.publish awaits every handler, so this must be a coroutine.
            self.retrying.append(envelope.payload)

        token = await self.bus.subscribe(EVENT_NODE_RETRYING, _collect_retrying)
        self._token = token
        await self.supervisor.start()

    async def first_failure(self, node_id: str, attempt: int) -> None:
        await self.bus.publish(
            EventEnvelope.create(
                EVENT_NODE_FAILED,
                self.bus._next_seq(),
                NodeFailed(node_id, RUN_ID, "m-a", "boom", attempt=attempt),
            )
        )

    async def stop(self) -> None:
        self._token.cancel()
        self._token = None
        await self.supervisor.close()


class TestSupervisorRetryThenSuccess:
    async def test_two_retries_then_completion(self):
        harness = _Harness(workflow_with_retry(), fail_attempts={1, 2})
        await harness.start()
        await harness.first_failure("a", 1)
        await harness.stop()

        assert harness.executed == [("a", 2), ("a", 3)]
        assert harness.completed == ["a"]
        assert [r.delay_seconds for r in harness.retrying] == [0.5, 1.0]
        assert [r.attempt for r in harness.retrying] == [1, 2]
        assert [r.next_attempt for r in harness.retrying] == [2, 3]
        assert [r.node_id for r in harness.retrying] == ["a", "a"]
        assert harness.slept == [0.5, 1.0]

    async def test_retry_payload_carries_error_and_model(self):
        harness = _Harness(workflow_with_retry(), fail_attempts={1, 2})
        await harness.start()
        await harness.first_failure("a", 1)
        await harness.stop()
        first = harness.retrying[0]
        assert first.run_id == RUN_ID
        assert first.model == "m-a"
        assert first.error == "boom"


class TestSupervisorExhaustion:
    async def test_all_attempts_fail_stops_at_max(self):
        # node 'a' retries 3x. Every attempt fails → only attempts 1 and 2
        # dispatch a retry; attempt 3 is terminal and not retried.
        harness = _Harness(workflow_with_retry(), fail_attempts={1, 2, 3})
        await harness.start()
        await harness.first_failure("a", 1)
        await harness.stop()

        assert harness.executed == [("a", 2), ("a", 3)]
        assert harness.completed == []
        # one retry emitted per non-terminal failure
        assert [r.attempt for r in harness.retrying] == [1, 2]
        assert [r.next_attempt for r in harness.retrying] == [2, 3]
        # no NodeRetrying is emitted for the terminal (attempt 3) failure


class TestSupervisorNoPolicy:
    async def test_no_policy_means_no_retry(self):
        harness = _Harness(workflow_with_retry(), fail_attempts=None)
        await harness.start()
        await harness.first_failure("c", 1)
        await harness.stop()

        assert harness.executed == []
        assert harness.retrying == []
        assert harness.slept == []
        assert harness.completed == []


class TestSupervisorTerminalAttempt:
    async def test_failure_at_max_attempt_is_not_retried(self):
        harness = _Harness(workflow_with_retry(), fail_attempts=None)
        await harness.start()
        await harness.first_failure("a", 3)  # already at max
        await harness.stop()

        assert harness.executed == []
        assert harness.retrying == []


class TestSupervisorDuplicateSuppressed:
    async def test_duplicate_same_attempt_is_ignored(self):
        harness = _Harness(workflow_with_retry(), fail_attempts={1})
        await harness.start()
        # First failure dispatches a retry; its nested failure (attempt 2) is a
        # distinct (node, attempt) and is allowed to proceed.
        await harness.first_failure("a", 1)
        retries_after_first = len(harness.retrying)
        # A literal duplicate of an already-dispatched (a,1) is suppressed.
        await harness.first_failure("a", 1)
        await harness.stop()

        assert len(harness.retrying) == retries_after_first
        # execute is only ever called for the next attempt, never a repeat.
        assert harness.executed[0] == ("a", 2)
