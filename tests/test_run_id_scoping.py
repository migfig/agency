"""Run-id scoping for the singleton EventBus (research R2).

Two concurrent fake runs share the global singleton bus. Every per-run bus
subscriber must react only to events for its own run, so run A's node events
never land in run B's durable artifacts, the retry/fallback supervisors only
dispatch for their own run's failures, and the orchestrator cancels only its
own run on ``RunCancelled``.

These tests are test-first: they stay red until T006-T010 land the run-id
guards in the node-event handlers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agency.core.event_bus import EventBus, get_event_bus, reset_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_STARTED,
    EVENT_RUN_CANCEL,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    RunCancelled,
)
from agency.executor.fallback import FallbackSupervisor
from agency.executor.orchestrator import DagOrchestrator
from agency.executor.retry import RetrySupervisor
from agency.executor.run_log import RunLogger
from agency.executor.run_state import COMPLETED, PENDING, RunStateStore
from agency.yaml_engine.schema import AgentNode, RetryPolicy, Workflow

RUN_A = "runA"
RUN_B = "runB"


def _workflow() -> Workflow:
    return Workflow(
        name="wf",
        nodes={
            "a": AgentNode(id="a", type="agent", model="m-a", prompt_template="p"),
            "b": AgentNode(id="b", type="agent", model="m-b", prompt_template="p"),
        },
        entry_point="a",
        edges=[],
    )


def _env(bus: EventBus, event_type: str, payload) -> EventEnvelope:
    return EventEnvelope.create(event_type, bus._next_seq(), payload)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _clean_bus() -> None:
    reset_event_bus()
    yield
    reset_event_bus()


# -- RunStateStore (T006) -------------------------------------------------


async def test_run_state_store_ignores_other_runs(tmp_path: Path) -> None:
    wf = _workflow()
    bus = get_event_bus()
    store_a = RunStateStore(tmp_path, RUN_A, wf, event_bus=bus)
    store_b = RunStateStore(tmp_path, RUN_B, wf, event_bus=bus)
    await store_a.start()
    await store_b.start()
    try:
        await bus.publish(_env(bus, EVENT_NODE_STARTED, NodeStarted("a", RUN_A, "m-a")))
        await bus.publish(_env(bus, EVENT_NODE_COMPLETED, NodeCompleted("a", RUN_A, "m-a", 5, 0.1)))
    finally:
        await store_a.close()
        await store_b.close()

    # run A's store recorded the transition.
    assert store_a._status["a"] == COMPLETED
    assert (tmp_path / RUN_A / "checkpoints" / "a.json").exists()

    # run B's store must be untouched by run A's events: no in-memory change,
    # no checkpoint, and no node_status mutation in its WAL.
    assert store_b._status["a"] == PENDING
    assert not (tmp_path / RUN_B / "checkpoints" / "a.json").exists()
    assert all(line.get("mutation") != "node_status" for line in _read_jsonl(tmp_path / RUN_B / "wal.jsonl"))


# -- RunLogger (T007) -----------------------------------------------------


async def test_run_logger_ignores_other_runs(tmp_path: Path) -> None:
    wf = _workflow()
    bus = get_event_bus()
    log_a = RunLogger(tmp_path, RUN_A, wf, event_bus=bus)
    log_b = RunLogger(tmp_path, RUN_B, wf, event_bus=bus)
    await log_a.start()
    await log_b.start()
    try:
        await bus.publish(_env(bus, EVENT_NODE_COMPLETED, NodeCompleted("a", RUN_A, "m-a", 5, 0.1)))
    finally:
        await log_a.close()
        await log_b.close()

    lines_a = _read_jsonl(tmp_path / RUN_A / "execution.jsonl")
    lines_b = _read_jsonl(tmp_path / RUN_B / "execution.jsonl")

    # run A's log carries the node completion.
    assert any(line.get("type") == "node_completed" and line.get("node_id") == "a" for line in lines_a)
    # run B's log must contain only its own run_started line.
    assert lines_b and all(line.get("type") == "run_started" for line in lines_b)


# -- RetrySupervisor (T008) -----------------------------------------------


async def test_retry_supervisor_only_reacts_to_own_run() -> None:
    wf = Workflow(
        name="wf",
        nodes={
            "a": AgentNode(
                id="a",
                type="agent",
                model="m-a",
                prompt_template="p",
                retry=RetryPolicy(max_attempts=3),
            )
        },
        entry_point="a",
        edges=[],
    )
    bus = get_event_bus()
    executed: list[tuple[str, int]] = []

    async def _execute(node_id: str, attempt: int) -> None:
        executed.append((node_id, attempt))

    async def _sleep(delay: float) -> None:
        return None

    supervisor = RetrySupervisor(wf, run_id=RUN_A, bus=bus, execute=_execute, sleep=_sleep)
    await supervisor.start()
    try:
        # A failure from another run must not trigger this supervisor.
        await bus.publish(_env(bus, EVENT_NODE_FAILED, NodeFailed("a", RUN_B, "m-a", "boom")))
        assert executed == []
        # A failure from its own run triggers the retry.
        await bus.publish(_env(bus, EVENT_NODE_FAILED, NodeFailed("a", RUN_A, "m-a", "boom")))
        assert executed == [("a", 2)]
    finally:
        await supervisor.close()


# -- FallbackSupervisor (T009) --------------------------------------------


async def test_fallback_supervisor_only_reacts_to_own_run() -> None:
    wf = Workflow(
        name="wf",
        nodes={
            "a": AgentNode(id="a", type="agent", model="m-a", prompt_template="p", fallback="fb")
        },
        entry_point="a",
        edges=[],
    )
    bus = get_event_bus()
    executed: list[tuple[str, str, int]] = []

    async def _execute(node_id: str, model: str, attempt: int) -> None:
        executed.append((node_id, model, attempt))

    supervisor = FallbackSupervisor(wf, run_id=RUN_A, bus=bus, execute=_execute)
    await supervisor.start()
    try:
        # A terminal failure from another run must not activate this supervisor's fallback.
        await bus.publish(_env(bus, EVENT_NODE_FAILED, NodeFailed("a", RUN_B, "m-a", "boom")))
        assert executed == []
        # A terminal failure from its own run activates the fallback.
        await bus.publish(_env(bus, EVENT_NODE_FAILED, NodeFailed("a", RUN_A, "m-a", "boom")))
        assert executed == [("a", "fb", 1)]
    finally:
        await supervisor.close()


# -- DagOrchestrator._on_cancel (T010) ------------------------------------


async def test_orchestrator_cancels_only_own_run() -> None:
    orch = DagOrchestrator(
        _workflow(),
        RUN_A,
        agent_runner=object(),
        non_agent_runner=object(),
        context_store=object(),
        run_state=object(),
        run_logger=object(),
        load_manager=object(),
    )
    # A RunCancelled for another run must not cancel this orchestrator.
    orch._on_cancel(EventEnvelope.create(EVENT_RUN_CANCEL, 1, RunCancelled(RUN_B)))
    assert orch._cancelled is False
    # A RunCancelled for its own run cancels it.
    orch._on_cancel(EventEnvelope.create(EVENT_RUN_CANCEL, 2, RunCancelled(RUN_A)))
    assert orch._cancelled is True
