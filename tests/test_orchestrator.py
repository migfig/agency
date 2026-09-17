"""DAG executor core (Story 5.4): ordering, concurrency, containment, phases, seam.

Every row drives a real ``DagOrchestrator`` over a dedicated ``EventBus`` with
fake structurally-injected collaborators, and asserts on the bus event sequence
and the returned :class:`DagRunResult` — the orchestrator is the sole publisher
of ``Node*`` lifecycle events, so the bus is the ground truth.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agency.core.event_bus import EventBus
from agency.core.events import (
    EVENT_FALLBACK_ACTIVATED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_QUEUED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    FallbackActivated,
    NodeQueued,
    NodeSkipped,
)
from agency.executor.contracts import RunResult
from agency.executor.orchestrator import (
    AWAITING_RETRY,
    COMPLETED,
    COMPLETED_FALLBACK,
    FAILED,
    PENDING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    SKIP_BRANCH_NOT_TAKEN,
    SKIP_DEPENDENCY_FAILED,
    SKIP_UNRESOLVED_BINDING,
    SKIPPED,
    DagOrchestrator,
)
from agency.yaml_engine.parser import load_workflow

_NODE_EVENTS = (
    EVENT_NODE_QUEUED,
    EVENT_NODE_STARTED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_RETRYING,
    EVENT_FALLBACK_ACTIVATED,
)


# --- YAML fixtures ---------------------------------------------------------

LIN_YAML = """
name: lin
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
  c:
    id: c
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
"""

CONC_YAML = """
name: conc
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
  c:
    id: c
    type: agent
    model: m
    prompt_template: p
  d:
    id: d
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: a, to_id: c}
  - {from_id: b, to_id: d}
  - {from_id: c, to_id: d}
"""

FAIL_CONTAIN_YAML = """
name: fail-contain
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
  c:
    id: c
    type: agent
    model: m
    prompt_template: p
  d:
    id: d
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: a, to_id: c}
  - {from_id: b, to_id: d}
"""

BRANCH_YAML = """
name: branch-diamond
entry_point: c
nodes:
  c:
    id: c
    type: conditional
    condition: cond
    branches:
      "true": t
      "false": k
  t:
    id: t
    type: agent
    model: m
    prompt_template: p
  k:
    id: k
    type: agent
    model: m
    prompt_template: p
  m:
    id: m
    type: merge
    inputs: [t, k]
    strategy: any
edges:
  - {from_id: c, to_id: t}
  - {from_id: c, to_id: k}
  - {from_id: t, to_id: m}
  - {from_id: k, to_id: m}
"""

BIND_MISS_YAML = """
name: bind-miss
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
"""

PHASE_YAML = """
name: phase
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
  z:
    id: z
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: a, to_id: z}
phases:
  - id: p1
    name: Phase One
    node_ids: [a, b]
"""

RETRY_YAML = """
name: retry-seam
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
    retry:
      max_attempts: 3
  d:
    id: d
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: d}
"""

SUM_FAIL_YAML = """
name: sum-fail
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
  b:
    id: b
    type: agent
    model: m
    prompt_template: p
  c:
    id: c
    type: agent
    model: m
    prompt_template: p
edges:
  - {from_id: a, to_id: b}
  - {from_id: a, to_id: c}
"""

RETRY_REAL_YAML = """
name: retry-real
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
    retry:
      max_attempts: 3
      base_delay_seconds: 0
"""

RETRY_EXHAUST_YAML = """
name: retry-exhaust
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
    retry:
      max_attempts: 2
      base_delay_seconds: 0
"""

FB_YAML = """
name: fb
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
    fallback: fb
  fb:
    id: fb
    type: agent
    model: m_fb
    prompt_template: p
"""

FB_FAIL_YAML = """
name: fb-fail
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
    fallback: fb
  fb:
    id: fb
    type: agent
    model: m_fb
    prompt_template: p
"""

FB_RETRY_YAML = """
name: fb-retry
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m
    prompt_template: p
    fallback: fb
    retry:
      max_attempts: 2
      base_delay_seconds: 0
  fb:
    id: fb
    type: agent
    model: m_fb
    prompt_template: p
"""


# --- fakes + harness -------------------------------------------------------


class Recorder:
    """Bus handler that records every lifecycle envelope in publish order."""

    def __init__(self, timeline: list | None = None) -> None:
        self.items: list[tuple[str, object]] = []
        self.timeline = timeline

    async def __call__(self, envelope) -> None:
        self.items.append((envelope.event_type, envelope.payload))
        if self.timeline is not None:
            self.timeline.append((envelope.event_type, envelope.payload.node_id))

    def events(self, event_type: str, node_id: str | None = None):
        return [
            payload
            for etype, payload in self.items
            if etype == event_type and (node_id is None or payload.node_id == node_id)
        ]

    def order(self, node_id: str) -> list[str]:
        """Per-node lifecycle ordering (event type names)."""
        out: list[str] = []
        for etype, payload in self.items:
            if payload.node_id != node_id:
                continue
            out.append(etype)
        return out

    def event_seq(self) -> list[tuple[str, str]]:
        """Whole-bus ordering as ``(event_type, node_id)`` pairs."""
        return [(etype, payload.node_id) for etype, payload in self.items]


class FakeRunner:
    def __init__(self, results, *, delay: float = 0.0) -> None:
        self.results = results
        self.delay = delay
        self.calls: list[tuple[str, dict, dict]] = []

    async def run(self, node, context, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        else:
            await asyncio.sleep(0)
        self.calls.append((node.id, dict(kwargs), dict(context)))
        result = self.results[node.id]
        if callable(result):
            result = result(kwargs)
        attempt = kwargs.get("attempt")
        fallback = kwargs.get("fallback")
        if attempt is not None and result.attempt != attempt:
            result = replace(result, attempt=attempt)
        if fallback is not None and result.fallback is None:
            result = replace(result, fallback=fallback)
        return result


class FakeContextStore:
    def __init__(self, timeline=None) -> None:
        self.timeline = timeline
        self.started: list[str] = []
        self.completed: list[str] = []

    def start_phase(self, phase_id: str, name: str) -> None:
        self.started.append(phase_id)
        if self.timeline is not None:
            self.timeline.append(("phase_start", phase_id))

    def complete_phase(self, phase_id: str) -> None:
        self.completed.append(phase_id)
        if self.timeline is not None:
            self.timeline.append(("phase_complete", phase_id))

    async def flush_events(self) -> None:
        if self.timeline is not None:
            self.timeline.append(("flush", None))


class FakeCollab:
    def __init__(self) -> None:
        self.starts = 0
        self.closes = 0

    async def start(self) -> None:
        self.starts += 1

    async def close(self) -> None:
        self.closes += 1


class FakeLoadManager:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


def ok(node_id: str, *, attempt: int = 1, output: str | None = "out") -> RunResult:
    return RunResult(
        node_id=node_id,
        outcome="completed",
        model="m",
        output=output,
        tokens_used=10,
        duration_seconds=0.5,
        attempt=attempt,
    )


def failed(node_id: str, *, attempt: int = 1, error: str = "boom") -> RunResult:
    return RunResult(
        node_id=node_id, outcome="failed", model="m", error=error, attempt=attempt
    )


async def build(
    workflow,
    *,
    agent_results: dict | None = None,
    non_agent_results: dict | None = None,
    agent_delay: float = 0.0,
    timeline: list | None = None,
):
    bus = EventBus()
    rec = Recorder(timeline)
    agent = FakeRunner(agent_results or {}, delay=agent_delay)
    non_agent = FakeRunner(non_agent_results or {})
    store = FakeContextStore(timeline)
    run_state = FakeCollab()
    run_logger = FakeCollab()
    load_manager = FakeLoadManager()
    orchestrator = DagOrchestrator(
        workflow,
        "run-1",
        agent_runner=agent,
        non_agent_runner=non_agent,
        context_store=store,
        run_state=run_state,
        run_logger=run_logger,
        load_manager=load_manager,
        bus=bus,
    )
    for event_type in _NODE_EVENTS:
        await bus.subscribe(event_type, rec)
    return SimpleNamespace(
        orch=orchestrator,
        rec=rec,
        agent=agent,
        non_agent=non_agent,
        store=store,
        run_state=run_state,
        run_logger=run_logger,
        load_manager=load_manager,
        bus=bus,
    )


async def wait_for(predicate, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for predicate")
        await asyncio.sleep(0.005)


# --- matrix rows -----------------------------------------------------------


async def test_lin_ok():
    b = await build(
        load_workflow(LIN_YAML),
        agent_results={"a": ok("a"), "b": ok("b", output="out-b"), "c": ok("c")},
    )
    result = await b.orch.run()

    for node_id in ("a", "b", "c"):
        assert b.rec.order(node_id) == [
            "node_queued",
            "node_started",
            "node_completed",
        ]
    # b's execution context carried a's output
    b_context = dict(b.agent.calls[1][2])
    assert b_context["nodes.a.output"] == "out"
    assert result.status == RUN_STATUS_COMPLETED
    assert [n.node_id for n in result.nodes] == ["a", "b", "c"]


async def test_conc_both_start_before_either_ends_and_d_waits():
    b = await build(
        load_workflow(CONC_YAML),
        agent_delay=0.01,
        agent_results={"a": ok("a"), "b": ok("b"), "c": ok("c"), "d": ok("d")},
    )
    result = await b.orch.run()

    seq = b.rec.event_seq()
    # both b and c started before either terminal event
    b_start = seq.index(("node_started", "b"))
    c_start = seq.index(("node_started", "c"))
    b_end = seq.index(("node_completed", "b"))
    c_end = seq.index(("node_completed", "c"))
    d_start = seq.index(("node_started", "d"))
    assert max(b_start, c_start) < min(b_end, c_end)
    # d starts only after both are terminal
    assert d_start > max(b_end, c_end)
    assert result.status == RUN_STATUS_COMPLETED


async def test_fail_contain():
    b = await build(
        load_workflow(FAIL_CONTAIN_YAML),
        agent_results={"a": ok("a"), "b": failed("b"), "c": ok("c")},
    )
    result = await b.orch.run()

    d_skips = b.rec.events(EVENT_NODE_SKIPPED, "d")
    assert len(d_skips) == 1
    assert d_skips[0].reason == SKIP_DEPENDENCY_FAILED
    # d is never started
    assert b.rec.events(EVENT_NODE_STARTED, "d") == []
    assert b.rec.events(EVENT_NODE_COMPLETED, "c") != []
    assert result.status == RUN_STATUS_FAILED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["d"].status == SKIPPED
    assert by_id["c"].status == COMPLETED
    assert by_id["b"].status == FAILED


async def test_branch_diamond():
    taken = RunResult(
        node_id="c",
        outcome="completed",
        selected_label="false",
        branch_target="k",
        skipped_targets=("t",),
    )
    b = await build(
        load_workflow(BRANCH_YAML),
        non_agent_results={"c": taken, "m": ok("m")},
        agent_results={"t": ok("t"), "k": ok("k", output="out-k")},
    )
    result = await b.orch.run()

    t_skips = b.rec.events(EVENT_NODE_SKIPPED, "t")
    assert len(t_skips) == 1
    assert t_skips[0].reason == SKIP_BRANCH_NOT_TAKEN
    assert b.rec.events(EVENT_NODE_STARTED, "t") == []
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["t"].status == SKIPPED
    assert by_id["t"].reason == SKIP_BRANCH_NOT_TAKEN
    assert by_id["k"].status == COMPLETED
    # m ran (merge) and completed from k's output, not skipped
    assert by_id["m"].status == COMPLETED
    m_calls = [c for c in b.non_agent.calls if c[0] == "m"]
    assert m_calls and m_calls[0][2]["nodes.k.output"] == "out-k"
    assert result.status == RUN_STATUS_COMPLETED


async def test_bind_miss(caplog: pytest.LogCaptureFixture):
    miss = RunResult(
        node_id="a", outcome="skipped", skipped_bindings=("nodes.x.output",)
    )
    b = await build(load_workflow(BIND_MISS_YAML), agent_results={"a": miss, "b": ok("b")})
    result = await b.orch.run()

    a_skips = b.rec.events(EVENT_NODE_SKIPPED, "a")
    assert len(a_skips) == 1
    assert a_skips[0].reason == SKIP_UNRESOLVED_BINDING
    assert a_skips[0].skipped_bindings == ("nodes.x.output",)
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == SKIPPED
    # dependents still proceed
    assert by_id["b"].status == COMPLETED
    assert result.status == RUN_STATUS_COMPLETED
    # the warning log names the unresolved bindings
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("nodes.x.output" in msg for msg in warnings)


async def test_phase_opened_before_start_and_closed_after_last():
    timeline: list = []
    b = await build(
        load_workflow(PHASE_YAML),
        agent_results={"a": ok("a"), "b": ok("b"), "z": ok("z")},
        timeline=timeline,
    )
    result = await b.orch.run()

    p1_open = timeline.index(("phase_start", "p1"))
    p1_flush = timeline.index(("flush", None))
    p1_close = timeline.index(("phase_complete", "p1"))
    a_start = timeline.index(("node_started", "a"))
    b_start = timeline.index(("node_started", "b"))
    b_done = timeline.index(("node_completed", "b"))
    # PhaseStarted flushed before the phase's first node_started
    assert p1_open < p1_flush < a_start and p1_open < b_start
    # PhaseCompleted after the last in-phase node is terminal
    assert p1_close > max(a_start, b_start) and p1_close > b_done
    # the phaseless node never opens/completes a phase
    assert b.store.started == ["p1"] and b.store.completed == ["p1"]
    assert ("node_started", "z") in timeline
    assert result.status == RUN_STATUS_COMPLETED


async def test_retry_seam_held_until_supervisor_reenters():
    def b_result(kwargs):
        attempt = kwargs.get("attempt", 1)
        if attempt == 1:
            return failed("b", attempt=1)
        return RunResult(
            node_id="b", outcome="completed", model="m", output="out-b",
            tokens_used=10, duration_seconds=0.5, attempt=attempt,
        )

    b = await build(
        load_workflow(RETRY_YAML),
        agent_results={"a": ok("a"), "b": b_result, "d": ok("d")},
    )
    run_task = asyncio.create_task(b.orch.run())
    await wait_for(lambda: b.orch._status.get("b") == AWAITING_RETRY)
    # d must NOT be skipped while b awaits a retry
    assert b.orch._status["d"] == PENDING
    # the test plays the 5.5 supervisor
    await b.orch.execute_node("b", 2)
    result = await run_task

    b_failed = b.rec.events(EVENT_NODE_FAILED, "b")
    assert [e.attempt for e in b_failed] == [1]
    assert [e.attempt for e in b.rec.events(EVENT_NODE_STARTED, "b")] == [1, 2]
    assert b.rec.events(EVENT_NODE_SKIPPED) == []
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["b"].status == COMPLETED and by_id["b"].attempt == 2
    assert by_id["d"].status == COMPLETED
    assert result.status == RUN_STATUS_COMPLETED
    # silence lint for the otherwise-unused builder
    assert b_result is not None


async def test_sum_fail_reflects_per_node_outcome():
    b = await build(
        load_workflow(SUM_FAIL_YAML),
        agent_results={
            "a": RunResult(node_id="a", outcome="completed", model="m", output="o",
                           tokens_used=10, duration_seconds=0.5),
            "b": RunResult(node_id="b", outcome="failed", model="m",
                           error="boom", attempt=1, tokens_used=5, duration_seconds=2.0),
            "c": ok("c"),
        },
    )
    result = await b.orch.run()

    by_id = {n.node_id: n for n in result.nodes}
    assert result.status == RUN_STATUS_FAILED
    assert by_id["a"].status == COMPLETED and by_id["a"].tokens == 10
    assert by_id["c"].status == COMPLETED and by_id["c"].tokens == 10
    assert by_id["b"].status == FAILED and by_id["b"].tokens == 5
    assert by_id["b"].duration_seconds == 2.0
    assert result.total_tokens == 25


# --- payload unit tests ----------------------------------------------------


def test_node_queued_payload_carries_fields():
    p = NodeQueued(node_id="n1", run_id="r1")
    assert p.node_id == "n1"
    assert p.run_id == "r1"
    assert p.queue_position is None
    assert p.wait_seconds is None


def test_node_skipped_payload_carries_fields():
    p = NodeSkipped(
        node_id="n1",
        run_id="r1",
        reason=SKIP_DEPENDENCY_FAILED,
        skipped_bindings=("nodes.x.output",),
    )
    assert p.node_id == "n1"
    assert p.run_id == "r1"
    assert p.reason == SKIP_DEPENDENCY_FAILED
    assert p.skipped_bindings == ("nodes.x.output",)
    default = NodeSkipped(node_id="n2", run_id="r1", reason="x")
    assert default.skipped_bindings == ()


# --- real retry + fallback tests (Story 5.5) -------------------------------


def _retry_then_ok(kwargs):
    if kwargs.get("attempt", 1) == 1:
        return failed("a", attempt=1, error="boom")
    return ok("a")


async def test_retry_real_succeeds_on_attempt_2():
    b = await build(
        load_workflow(RETRY_REAL_YAML),
        agent_results={"a": _retry_then_ok},
    )
    result = await b.orch.run()

    assert result.status == RUN_STATUS_COMPLETED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == COMPLETED
    assert by_id["a"].attempt == 2
    assert by_id["a"].fallback is None

    # Verify event sequence: Queued → Started(1) → Failed(1) → Retrying(1→2) → Started(2) → Completed(2)
    queued = b.rec.events(EVENT_NODE_QUEUED, "a")
    started = b.rec.events(EVENT_NODE_STARTED, "a")
    failed_evts = b.rec.events(EVENT_NODE_FAILED, "a")
    retrying = b.rec.events(EVENT_NODE_RETRYING, "a")
    completed = b.rec.events(EVENT_NODE_COMPLETED, "a")

    assert len(queued) == 1
    assert len(started) == 2
    assert started[0].attempt == 1
    assert started[1].attempt == 2
    assert len(failed_evts) == 1
    assert failed_evts[0].attempt == 1
    assert len(retrying) == 1
    assert retrying[0].attempt == 1
    assert retrying[0].next_attempt == 2
    assert len(completed) == 1
    assert completed[0].attempt == 2


async def test_retry_exhaust_max_attempts():
    b = await build(
        load_workflow(RETRY_EXHAUST_YAML),
        agent_results={"a": lambda kw: failed("a", attempt=kw.get("attempt", 1))},
    )
    result = await b.orch.run()

    assert result.status == RUN_STATUS_FAILED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == FAILED
    assert by_id["a"].attempt == 2

    started = b.rec.events(EVENT_NODE_STARTED, "a")
    failed_evts = b.rec.events(EVENT_NODE_FAILED, "a")
    retrying = b.rec.events(EVENT_NODE_RETRYING, "a")

    assert len(started) == 2
    assert len(failed_evts) == 2
    assert len(retrying) == 1


async def test_fallback_succeeds():
    b = await build(
        load_workflow(FB_YAML),
        agent_results={
            "a": lambda kw: failed("a", attempt=kw.get("attempt", 1)),
            "fb": ok("fb"),
        },
    )
    result = await b.orch.run()

    assert result.status == RUN_STATUS_COMPLETED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == COMPLETED_FALLBACK
    assert by_id["a"].attempt == 1
    assert by_id["a"].fallback == "fb"

    # Fallback activated event
    fb_evts = b.rec.events(EVENT_FALLBACK_ACTIVATED, "a")
    assert len(fb_evts) == 1
    assert isinstance(fb_evts[0], FallbackActivated)
    assert fb_evts[0].fallback == "fb"
    assert fb_evts[0].model == "m"

    # The fallback node ran via agent runner
    calls = [c for c in b.agent.calls if c[0] == "fb"]
    assert len(calls) == 1
    assert calls[0][1]["node_id"] == "a"
    assert calls[0][1]["fallback"] == "fb"

    # Standby fb node never launched on its own
    fb_started = b.rec.events(EVENT_NODE_STARTED, "fb")
    assert len(fb_started) == 0


async def test_fallback_fails():
    b = await build(
        load_workflow(FB_FAIL_YAML),
        agent_results={
            "a": lambda kw: failed("a", attempt=kw.get("attempt", 1)),
            "fb": failed("fb"),
        },
    )
    result = await b.orch.run()

    assert result.status == RUN_STATUS_FAILED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == FAILED
    assert by_id["a"].fallback == "fb"

    fb_evts = b.rec.events(EVENT_FALLBACK_ACTIVATED, "a")
    assert len(fb_evts) == 1


async def test_retry_then_fallback():
    def _primary(kw):
        attempt = kw.get("attempt", 1)
        if attempt <= 2:
            return failed("a", attempt=attempt)
        return ok("a")

    b = await build(
        load_workflow(FB_RETRY_YAML),
        agent_results={
            "a": _primary,
            "fb": ok("fb"),
        },
    )
    result = await b.orch.run()

    assert result.status == RUN_STATUS_COMPLETED
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a"].status == COMPLETED_FALLBACK
    assert by_id["a"].attempt == 2
    assert by_id["a"].fallback == "fb"

    started = b.rec.events(EVENT_NODE_STARTED, "a")
    assert len(started) == 3  # attempt 1, attempt 2 (retry), fallback attempt
    retrying = b.rec.events(EVENT_NODE_RETRYING, "a")
    assert len(retrying) == 1
