"""Tests for the in-memory run registry (US1, T013).

Covers ``RunRegistry`` register/lookup of ``RunHandle``; the per-run bus
mirror updating node states (and ignoring other runs' events, per research
R2); and the pending-input lifecycle (register with prompt + deadline,
resolve once, duplicate submission has no effect, entry removed on
timeout/expiry).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agency.api.registry import RunHandle, RunRegistry, api_input_source
from agency.api.schemas import NodeStateView
from agency.core.event_bus import EventBus, get_event_bus, reset_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_INPUT_REQUESTED,
    EVENT_NODE_INPUT_RESOLVED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeInputRequested,
    NodeInputResolved,
    NodeSkipped,
    NodeStarted,
)
from agency.yaml_engine.parser import load_workflow

WORKFLOW_YAML = """
name: api-registry-demo
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "hi"}'
    retry:
      max_attempts: 3
  gate:
    id: gate
    type: human_in_loop
    prompt: "Approve release? (y/n)"
    timeout_seconds: 60
edges:
  - from_id: shout
    to_id: gate
"""

RUN_ID = "20260101_000000"
OTHER_RUN_ID = "20260101_999999"


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def workflow():
    return load_workflow(WORKFLOW_YAML)


@pytest.fixture
def bus():
    return EventBus()


class Publisher:
    """Publishes payloads as envelopes with strictly increasing seq."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._seq = 0

    async def emit(self, event_type: str, payload) -> None:
        self._seq += 1
        await self._bus.publish(EventEnvelope.create(event_type, self._seq, payload))


def make_handle(workflow, run_id: str = RUN_ID) -> RunHandle:
    return RunHandle(
        run_id=run_id,
        workflow_name=workflow.name,
        started_at="2026-01-01T00:00:00+00:00",
        workflow=workflow,
        run_dir=Path("runs") / run_id,
        nodes={nid: NodeStateView(status="pending") for nid in workflow.nodes},
    )


async def wait_for_entry(handle: RunHandle, node_id: str) -> None:
    for _ in range(10_000):
        if node_id in handle.pending_inputs:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"pending input for '{node_id}' was never registered")


class TestRegisterLookup:
    async def test_register_get_and_remove(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        assert registry.get(RUN_ID) is handle
        registry.remove(RUN_ID)
        assert registry.get(RUN_ID) is None
        assert registry.get("no-such-run") is None


class TestBusMirror:
    async def test_mirror_updates_node_states(self, workflow, bus):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        await registry.subscribe_mirror(handle, bus)
        publisher = Publisher(bus)

        await publisher.emit(
            EVENT_NODE_STARTED,
            NodeStarted(node_id="shout", run_id=RUN_ID, model=None, attempt=1),
        )
        assert handle.nodes["shout"].status == "running"
        assert handle.nodes["shout"].attempt == 1

        await publisher.emit(
            EVENT_NODE_COMPLETED,
            NodeCompleted(
                node_id="shout",
                run_id=RUN_ID,
                model=None,
                tokens_used=12,
                duration_seconds=0.5,
                attempt=1,
            ),
        )
        view = handle.nodes["shout"]
        assert view.status == "completed"
        assert view.tokens == 12
        assert view.duration_seconds == 0.5
        assert view.attempt == 1

        await publisher.emit(
            EVENT_NODE_COMPLETED,
            NodeCompleted(
                node_id="gate",
                run_id=RUN_ID,
                model=None,
                tokens_used=0,
                duration_seconds=0.25,
                fallback="gate-fb",
            ),
        )
        view = handle.nodes["gate"]
        assert view.status == "completed_fallback"
        assert view.fallback == "gate-fb"

        await publisher.emit(
            EVENT_NODE_SKIPPED,
            NodeSkipped(node_id="gate", run_id=RUN_ID, reason="dependency_failed"),
        )
        view = handle.nodes["gate"]
        assert view.status == "skipped"
        assert view.reason == "dependency_failed"

    async def test_mirror_failed_retry_then_final(self, workflow, bus):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        await registry.subscribe_mirror(handle, bus)
        publisher = Publisher(bus)

        # 'shout' has a retry policy (max_attempts=3): attempt 1 is not final.
        await publisher.emit(
            EVENT_NODE_FAILED,
            NodeFailed(node_id="shout", run_id=RUN_ID, model=None, error="boom", attempt=1),
        )
        view = handle.nodes["shout"]
        assert view.status == "awaiting_retry"
        assert view.reason == "boom"
        assert view.attempt == 1

        await publisher.emit(
            EVENT_NODE_STARTED,
            NodeStarted(node_id="shout", run_id=RUN_ID, model=None, attempt=2),
        )
        assert handle.nodes["shout"].status == "running"

        # The final attempt's failure is terminal.
        await publisher.emit(
            EVENT_NODE_FAILED,
            NodeFailed(node_id="shout", run_id=RUN_ID, model=None, error="boom again", attempt=3),
        )
        view = handle.nodes["shout"]
        assert view.status == "failed"
        assert view.attempt == 3

    async def test_mirror_ignores_other_runs(self, workflow, bus):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        await registry.subscribe_mirror(handle, bus)
        publisher = Publisher(bus)

        await publisher.emit(
            EVENT_NODE_STARTED,
            NodeStarted(node_id="shout", run_id=OTHER_RUN_ID, model=None),
        )
        await publisher.emit(
            EVENT_NODE_COMPLETED,
            NodeCompleted(
                node_id="gate",
                run_id=OTHER_RUN_ID,
                model=None,
                tokens_used=5,
                duration_seconds=1.0,
            ),
        )
        await publisher.emit(
            EVENT_NODE_FAILED,
            NodeFailed(node_id="shout", run_id=OTHER_RUN_ID, model=None, error="other failure"),
        )
        await publisher.emit(
            EVENT_NODE_SKIPPED,
            NodeSkipped(node_id="gate", run_id=OTHER_RUN_ID, reason="other skip"),
        )

        assert all(view.status == "pending" for view in handle.nodes.values())


class TestPendingInput:
    async def test_register_resolve_once_duplicate_no_effect(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=60)

        entry = handle.register_pending_input("gate", "Approve release? (y/n)", deadline)
        assert handle.pending_inputs["gate"] is entry
        assert entry.node_id == "gate"
        assert entry.prompt == "Approve release? (y/n)"
        assert entry.deadline == deadline
        assert not entry.future.done()

        assert registry.resolve_input(handle, "gate", "yes") is True
        assert entry.future.done()
        assert entry.future.result() == "yes"

        # Duplicate submission has no effect: the first value wins.
        assert registry.resolve_input(handle, "gate", "no") is False
        assert entry.future.result() == "yes"
        # Unknown node: nothing to resolve.
        assert registry.resolve_input(handle, "shout", "x") is False

    async def test_api_input_source_registers_and_resolves(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        input_source = api_input_source(RUN_ID, registry)

        task = asyncio.create_task(input_source("gate", "Approve release? (y/n)"))
        await wait_for_entry(handle, "gate")
        entry = handle.pending_inputs["gate"]
        # Deadline derived from the node's timeout_seconds (60s).
        assert entry.deadline is not None
        assert timedelta(0) < entry.deadline - datetime.now(timezone.utc) <= timedelta(seconds=65)
        # No input yet: the source is still awaiting.
        assert not task.done()

        assert registry.resolve_input(handle, "gate", "yes") is True
        assert await task == "yes"
        assert "gate" not in handle.pending_inputs

    async def test_api_input_source_entry_removed_on_timeout(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        input_source = api_input_source(RUN_ID, registry)

        task = asyncio.create_task(input_source("gate", "Approve release? (y/n)"))
        await wait_for_entry(handle, "gate")
        entry = handle.pending_inputs["gate"]

        # Simulate the runner's wait_for(timeout) expiring.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert entry.future.cancelled()
        assert "gate" not in handle.pending_inputs


class TestInputEvents:
    """The API input source publishes node_input_requested / node_input_resolved
    so live /events clients learn a node is (or is no longer) awaiting input."""

    @staticmethod
    async def _until(present, attempts: int = 10_000) -> None:
        for _ in range(attempts):
            if present():
                return
            await asyncio.sleep(0)
        raise AssertionError("expected event was never published")

    async def test_submit_publishes_requested_then_resolved(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        input_source = api_input_source(RUN_ID, registry)

        bus = get_event_bus()
        seen: list = []

        async def on_requested(env):
            seen.append(("requested", env.payload.node_id, env.payload.prompt))

        async def on_resolved(env):
            seen.append(("resolved", env.payload.node_id))

        await bus.subscribe(EVENT_NODE_INPUT_REQUESTED, on_requested)
        await bus.subscribe(EVENT_NODE_INPUT_RESOLVED, on_resolved)

        task = asyncio.create_task(input_source("gate", "Approve release? (y/n)"))
        await wait_for_entry(handle, "gate")
        await self._until(lambda: any(s[0] == "requested" for s in seen))
        assert ("requested", "gate", "Approve release? (y/n)") in seen

        assert registry.resolve_input(handle, "gate", "yes") is True
        assert await task == "yes"
        await self._until(lambda: any(s[0] == "resolved" for s in seen))
        assert ("resolved", "gate") in seen
        assert "gate" not in handle.pending_inputs

    async def test_timeout_publishes_resolved(self, workflow):
        registry = RunRegistry()
        handle = make_handle(workflow)
        registry.register(handle)
        input_source = api_input_source(RUN_ID, registry)

        bus = get_event_bus()
        seen: list = []

        async def on_resolved(env):
            seen.append(("resolved", env.payload.node_id))

        await bus.subscribe(EVENT_NODE_INPUT_RESOLVED, on_resolved)

        task = asyncio.create_task(input_source("gate", "Approve release? (y/n)"))
        await wait_for_entry(handle, "gate")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._until(lambda: bool(seen))
        assert ("resolved", "gate") in seen
        assert "gate" not in handle.pending_inputs

    async def test_requested_payload_shape(self, workflow):
        # The two payload dataclasses flatten to the wire fields the client reads.
        req = NodeInputRequested(node_id="gate", run_id=RUN_ID, prompt="ok?", deadline=None)
        res = NodeInputResolved(node_id="gate", run_id=RUN_ID)
        assert req.node_id == "gate" and req.prompt == "ok?" and req.deadline is None
        assert res.node_id == "gate" and res.run_id == RUN_ID
