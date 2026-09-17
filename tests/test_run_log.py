"""Tests for the structured execution logger (Story 4.1)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
)
from agency.executor.run_log import RunLogger
from agency.yaml_engine.parser import load_workflow

TWO_NODE_YAML = """
name: run-log-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: prompt-a
  b:
    id: b
    type: merge
    inputs: [a]
    strategy: all
edges:
  - from_id: a
    to_id: b
"""

CHAIN_YAML = """
name: run-log-chain
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: prompt-a
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: prompt-b
  c:
    id: c
    type: agent
    model: m-c
    prompt_template: prompt-c
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
"""

TERMINAL_FIELDS = {
    "node_id",
    "input",
    "output",
    "started_at",
    "ended_at",
    "duration_seconds",
    "model",
    "tokens_used",
    "status",
}

RUN_ID = "rl-run"


@pytest.fixture(autouse=True)
def _clean_bus() -> None:
    reset_event_bus()


@pytest.fixture
def workflow():
    return load_workflow(TWO_NODE_YAML)


@pytest.fixture
def chain_workflow():
    return load_workflow(CHAIN_YAML)


def make_logger(log_dir: Path, workflow, bus: EventBus) -> RunLogger:
    return RunLogger(log_dir, RUN_ID, workflow, bus)


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_start_writes_run_started_line(workflow, tmp_path):
    logger = make_logger(tmp_path, workflow, EventBus())
    await logger.start()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    assert len(records) == 1
    assert records[0]["type"] == "run_started"
    assert records[0]["run_id"] == RUN_ID
    assert records[0]["started_at"]

    await logger.close()


async def test_two_node_run_has_full_line_set(workflow, tmp_path):
    bus = EventBus()
    logger = make_logger(tmp_path, workflow, bus)
    await logger.start()
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 1, NodeStarted("a", RUN_ID, "m-a", input="prompt-a")
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_COMPLETED,
            2,
            NodeCompleted(
                "a", RUN_ID, "m-a", tokens_used=100, duration_seconds=0.25, output="out-a"
            ),
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 3, NodeStarted("b", RUN_ID, None, input=None)
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_COMPLETED,
            4,
            NodeCompleted(
                "b", RUN_ID, None, tokens_used=200, duration_seconds=0.5, output="out-b"
            ),
        )
    )
    await logger.close()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    assert [r["type"] for r in records] == [
        "run_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
    ]

    started_a, completed_a, started_b, completed_b = records[1:]
    assert started_a["seq"] == 1
    assert started_a["status"] == "running"
    assert started_a["model"] == "m-a"
    assert started_a["input"] == "prompt-a"
    assert started_a["started_at"]
    assert started_a["output"] is None
    assert started_a["ended_at"] is None
    assert started_a["tokens_used"] is None

    assert set(TERMINAL_FIELDS) <= set(completed_a)
    assert completed_a["node_id"] == "a"
    assert completed_a["status"] == "completed"
    assert completed_a["model"] == "m-a"
    assert completed_a["input"] == "prompt-a"
    assert completed_a["output"] == "out-a"
    assert completed_a["started_at"] == started_a["started_at"]
    assert completed_a["ended_at"]
    assert completed_a["duration_seconds"] == 0.25
    assert completed_a["tokens_used"] == 100

    assert set(TERMINAL_FIELDS) <= set(completed_b)
    assert completed_b["node_id"] == "b"
    assert completed_b["duration_seconds"] == 0.5
    assert completed_b["tokens_used"] == 200
    assert started_b["status"] == "running"


async def test_non_agent_node_has_null_model(workflow, tmp_path):
    bus = EventBus()
    logger = make_logger(tmp_path, workflow, bus)
    await logger.start()
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 1, NodeStarted("b", RUN_ID, None, input=None)
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_COMPLETED,
            2,
            NodeCompleted("b", RUN_ID, None, tokens_used=10, duration_seconds=0.1, output="out-b"),
        )
    )
    await logger.close()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    started_b = next(r for r in records if r["type"] == "node_started")
    completed_b = next(r for r in records if r["type"] == "node_completed")
    assert started_b["model"] is None
    assert completed_b["model"] is None


async def test_failure_records_error_and_skips_dependents(chain_workflow, tmp_path):
    bus = EventBus()
    logger = make_logger(tmp_path, chain_workflow, bus)
    await logger.start()
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 1, NodeStarted("a", RUN_ID, "m-a", input="prompt-a")
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_FAILED,
            2,
            NodeFailed(
                "a",
                RUN_ID,
                "m-a",
                "boom",
                stack_trace="Traceback (most recent call last):\n  File 'x.py', line 1",
            ),
        )
    )
    await logger.close()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    assert [r["type"] for r in records] == [
        "run_started",
        "node_started",
        "node_failed",
        "node_skipped",
        "node_skipped",
    ]

    failed = next(r for r in records if r["type"] == "node_failed")
    assert set(TERMINAL_FIELDS) <= set(failed)
    assert failed["node_id"] == "a"
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    assert failed["stack_trace"].startswith("Traceback (most recent call last):")
    assert failed["input"] == "prompt-a"
    assert failed["model"] == "m-a"
    assert failed["started_at"]
    assert failed["ended_at"]
    assert failed["tokens_used"] is None

    skipped = [r for r in records if r["type"] == "node_skipped"]
    assert [r["node_id"] for r in skipped] == ["b", "c"]
    for line in skipped:
        assert line["status"] == "skipped"
        assert line["model"] is None
        assert line["input"] is None
        assert line["output"] is None
        assert line["started_at"] is None
        assert line["ended_at"] is not None
        assert line["tokens_used"] is None


async def test_duplicate_terminal_is_ignored(workflow, tmp_path):
    bus = EventBus()
    logger = make_logger(tmp_path, workflow, bus)
    await logger.start()
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 1, NodeStarted("a", RUN_ID, "m-a", input="prompt-a")
        )
    )
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_COMPLETED,
            2,
            NodeCompleted(
                "a", RUN_ID, "m-a", tokens_used=100, duration_seconds=0.25, output="out-a"
            ),
        )
    )
    # duplicate completed after the node is terminal: no second terminal line
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_COMPLETED,
            3,
            NodeCompleted(
                "a", RUN_ID, "m-a", tokens_used=999, duration_seconds=9.9, output="late"
            ),
        )
    )
    # late started after the node is terminal: also ignored
    await bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED, 4, NodeStarted("a", RUN_ID, "m-a", input="late")
        )
    )
    await logger.close()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    assert [r["type"] for r in records] == [
        "run_started",
        "node_started",
        "node_completed",
    ]
    assert records[2]["tokens_used"] == 100


async def test_unknown_node_warns_and_writes_no_line(workflow, tmp_path, caplog):
    bus = EventBus()
    logger = make_logger(tmp_path, workflow, bus)
    await logger.start()
    with caplog.at_level(logging.WARNING):
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_STARTED, 1, NodeStarted("ghost", RUN_ID, None, input="x")
            )
        )
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                2,
                NodeCompleted("ghost", RUN_ID, None, tokens_used=1, duration_seconds=0.1),
            )
        )
    await logger.close()

    records = read_records(tmp_path / RUN_ID / "execution.jsonl")
    assert len(records) == 1
    assert records[0]["type"] == "run_started"
    assert any("ghost" in message for message in caplog.messages)


async def test_unwritable_log_dir_never_raises(workflow, tmp_path, caplog):
    blocker = tmp_path / "block"
    blocker.write_text("not a directory", encoding="utf-8")
    log_dir = tmp_path / "block" / "runs"
    bus = EventBus()
    logger = make_logger(log_dir, workflow, bus)
    with caplog.at_level(logging.WARNING):
        await logger.start()
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_STARTED, 1, NodeStarted("a", RUN_ID, "m-a", input="prompt-a")
            )
        )
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                2,
                NodeCompleted(
                    "a", RUN_ID, "m-a", tokens_used=1, duration_seconds=0.1, output="x"
                ),
            )
        )
        await logger.close()

    assert not (tmp_path / "block" / "runs").exists()
    assert any("execution.jsonl" in message or "block" in message for message in caplog.messages)
