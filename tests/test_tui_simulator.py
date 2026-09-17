"""Tests for the deterministic demo producer (Story 3.3)."""
from __future__ import annotations

import inspect

import pytest

from agency.core.event_bus import EventBus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeStarted,
)
from agency.tui.simulator import simulate_run
from agency.yaml_engine.parser import load_workflow

WORKFLOW_YAML = """
name: sim-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: p
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: p
  c:
    id: c
    type: merge
    inputs: [b]
    strategy: all
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
"""


@pytest.fixture
def workflow():
    return load_workflow(WORKFLOW_YAML)


async def _capture_and_run(workflow, *, delay: float = 0.01):
    bus = EventBus()
    events: list[EventEnvelope] = []

    async def capture(event: EventEnvelope) -> None:
        events.append(event)

    await bus.subscribe(EVENT_NODE_STARTED, capture)
    await bus.subscribe(EVENT_NODE_COMPLETED, capture)
    await simulate_run(workflow, bus, run_id="sim-run", delay=delay)
    return events


async def test_publishes_started_then_completed_per_node_in_topo_order(workflow):
    events = await _capture_and_run(workflow)

    assert [e.event_type for e in events] == [
        EVENT_NODE_STARTED,
        EVENT_NODE_COMPLETED,
        EVENT_NODE_STARTED,
        EVENT_NODE_COMPLETED,
        EVENT_NODE_STARTED,
        EVENT_NODE_COMPLETED,
    ]
    assert [e.payload.node_id for e in events] == ["a", "a", "b", "b", "c", "c"]


async def test_payload_types_and_run_id(workflow):
    events = await _capture_and_run(workflow)

    for event in events:
        assert event.payload.run_id == "sim-run"
    for event in events:
        if event.event_type == EVENT_NODE_STARTED:
            assert isinstance(event.payload, NodeStarted)
        elif event.event_type == EVENT_NODE_COMPLETED:
            assert isinstance(event.payload, NodeCompleted)


async def test_completed_values_are_deterministic_per_topo_index(workflow):
    events = await _capture_and_run(workflow)
    completed = [e.payload for e in events if e.event_type == EVENT_NODE_COMPLETED]

    # index 0 -> 100 tok / 0.5s, 1 -> 200 / 1.0, 2 -> 300 / 1.5
    assert [p.tokens_used for p in completed] == [100, 200, 300]
    assert [p.duration_seconds for p in completed] == [0.5, 1.0, 1.5]
    # agent nodes carry their model, non-agent (merge) rows carry None
    assert [p.model for p in completed] == ["m-a", "m-b", None]


async def test_started_values_carry_node_model(workflow):
    events = await _capture_and_run(workflow)
    started = [e.payload for e in events if e.event_type == EVENT_NODE_STARTED]

    assert [p.model for p in started] == ["m-a", "m-b", None]


async def test_published_events_carry_synthetic_input_and_output(workflow):
    events = await _capture_and_run(workflow)

    started = [e.payload for e in events if e.event_type == EVENT_NODE_STARTED]
    completed = [e.payload for e in events if e.event_type == EVENT_NODE_COMPLETED]

    # agent nodes carry their prompt template, non-agent (merge) rows carry None
    assert [p.input for p in started] == ["p", "p", None]
    assert [p.output for p in completed] == [
        "simulated output for a",
        "simulated output for b",
        "simulated output for c",
    ]


def test_default_delay_is_half_second():
    assert inspect.signature(simulate_run).parameters["delay"].default == 0.5
