"""Textual pilot tests for the live DAG view + metrics (Stories 3.3/3.4)."""
from __future__ import annotations

import json
import logging
import re

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
from agency.resource_manager.vram_monitor import VRAMMonitor
from agency.tui.app import AgencyApp
from agency.tui.simulator import simulate_run
from agency.yaml_engine.parser import load_workflow

WORKFLOW_YAML = """
name: tui-test
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
    inputs:
      - b
    strategy: all
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
"""

CHAIN_YAML = """
name: tui-skip-test
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
    type: agent
    model: m-c
    prompt_template: p
  d:
    id: d
    type: agent
    model: m-d
    prompt_template: p
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
  - from_id: c
    to_id: d
"""


class FakeVRAMMonitor:
    """Double for VRAMMonitor: no NVML, counts lifecycle calls."""

    def __init__(self, usage_bytes: int | None = None) -> None:
        self.usage_bytes = usage_bytes
        self.start_count = 0
        self.stop_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def stop(self) -> None:
        self.stop_count += 1


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def workflow():
    return load_workflow(WORKFLOW_YAML)


@pytest.fixture
def chain_workflow():
    return load_workflow(CHAIN_YAML)


def make_app(workflow, *, log_dir=None, monitor=None):
    bus = EventBus()
    app = AgencyApp(
        workflow,
        run_id="test-run",
        event_bus=bus,
        log_dir=log_dir if log_dir is not None else "runs",
        vram_monitor=monitor if monitor is not None else FakeVRAMMonitor(),
    )
    return app, bus


def rendered_lines(app) -> list[str]:
    assert app.view is not None
    return app.view.render().plain.splitlines()


async def test_view_initial_render(workflow):
    app, _ = make_app(workflow)
    async with app.run_test():
        lines = rendered_lines(app)
        assert re.match(
            r"^nodes: 3   done: 0   running: 0   failed: 0   skipped: 0$", lines[0]
        )
        assert "a  agent  m-a  pending" in lines
        assert "b  agent  m-b  pending" in lines
        assert "c  merge  -  pending" in lines  # no model for merge nodes


async def test_started_then_completed_renders_state(workflow):
    app, bus = make_app(workflow)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        lines = rendered_lines(app)
        assert "a  agent  m-a  running  0.0s" in lines
        assert lines[0] == "nodes: 3   done: 0   running: 1   failed: 0   skipped: 0"

        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 2, NodeCompleted("a", "test-run", "m-a", tokens_used=250, duration_seconds=1.25)
            )
        )
        lines = rendered_lines(app)
        assert "a  agent  m-a  done  250 tok  1.2s" in lines
        assert lines[0] == "nodes: 3   done: 1   running: 0   failed: 0   skipped: 0"


async def test_failed_renders_failed(workflow):
    app, bus = make_app(workflow)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("b", "test-run", "m-b")))
        await bus.publish(EventEnvelope.create(EVENT_NODE_FAILED, 2, NodeFailed("b", "test-run", "m-b", "boom")))
        lines = rendered_lines(app)
        assert "b  agent  m-b  failed" in lines
        assert "c  merge  -  skipped" in lines
        assert lines[0] == "nodes: 3   done: 0   running: 0   failed: 1   skipped: 1"


async def test_duplicate_and_regressed_events_are_ignored(workflow):
    app, bus = make_app(workflow)
    async with app.run_test():
        # duplicate start (already running) is a no-op
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 2, NodeStarted("a", "test-run", "m-a")))
        lines = rendered_lines(app)
        assert sum(1 for line in lines if "  running " in line) == 1

        # done -> running (regression) is rejected
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 3, NodeStarted("b", "test-run", "m-b")))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 4, NodeCompleted("b", "test-run", "m-b", tokens_used=7, duration_seconds=2.0)
            )
        )
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 5, NodeStarted("b", "test-run", "m-b")))
        lines = rendered_lines(app)
        assert "b  agent  m-b  done  7 tok  2.0s" in lines
        assert sum(1 for line in lines if "  running " in line) == 1

        # unknown node is warned and ignored
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 6, NodeStarted("ghost", "test-run", None)))
        assert app.view.get_row("ghost") is None


async def test_terminal_node_events_are_not_double_counted(workflow):
    app, bus = make_app(workflow)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                2,
                NodeCompleted("a", "test-run", "m-a", tokens_used=250, duration_seconds=1.2),
            )
        )
        # duplicate completed after the node is terminal: no re-add of tokens
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                3,
                NodeCompleted("a", "test-run", "m-a", tokens_used=250, duration_seconds=1.2),
            )
        )
        # late start after the node is terminal: no ghost active agent
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 3, NodeStarted("a", "test-run", "m-a")))

        snapshot = app.collector.snapshot()
        assert snapshot.total_tokens == 250
        assert snapshot.active_agents == 0


async def test_unknown_node_warns_and_is_ignored(workflow, caplog):
    app, bus = make_app(workflow)
    async with app.run_test():
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("ghost", "test-run", None)))
        assert app.view is not None
        assert "ghost" not in rendered_lines(app)


async def test_failed_node_skips_transitive_dependents(chain_workflow):
    app, bus = make_app(chain_workflow)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("b", "test-run", "m-b")))
        await bus.publish(EventEnvelope.create(EVENT_NODE_FAILED, 2, NodeFailed("b", "test-run", "m-b", "boom")))

        view = app.view
        assert view.get_row("b").status == "failed"
        assert view.get_row("c").status == "skipped"
        assert view.get_row("d").status == "skipped"
        assert view.get_row("a").status == "pending"

        lines = rendered_lines(app)
        assert "b  agent  m-b  failed" in lines
        assert "c  agent  m-c  skipped" in lines
        assert "d  agent  m-d  skipped" in lines
        assert lines[0] == "nodes: 4   done: 0   running: 0   failed: 1   skipped: 2"

        # a is still pending, so the run has not finished: no summary yet
        assert app.summary is None


async def test_failed_entry_completes_run_with_summary_and_single_log(
    workflow, tmp_path
):
    log_dir = tmp_path / "runs"
    app, bus = make_app(workflow, log_dir=log_dir)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await bus.publish(
            EventEnvelope.create(EVENT_NODE_FAILED, 2, NodeFailed("a", "test-run", "m-a", "boom"))
        )

    # the app exited itself once the skips closed the DAG
    assert app.summary is not None
    assert [row.node_id for row in app.summary.nodes] == ["a", "b", "c"]
    assert [row.status for row in app.summary.nodes] == ["failed", "skipped", "skipped"]
    assert app.summary.total_tokens == 0

    log_path = log_dir / "test-run" / "summary.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "run_summary"
    assert entry["run_id"] == "test-run"
    assert entry["total_tokens"] == 0
    assert set(entry) == {
        "type",
        "run_id",
        "completed_at",
        "total_tokens",
        "vram_peak_bytes",
        "duration_seconds",
        "nodes",
    }
    assert entry["vram_peak_bytes"] is None
    assert 0.0 <= entry["duration_seconds"] < 5
    assert entry["nodes"][0]["status"] == "failed"
    assert entry["nodes"][1]["status"] == "skipped"


async def test_unknown_node_reaches_view_not_collector(workflow):
    app, bus = make_app(workflow)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("ghost", "test-run", None)))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 2, NodeCompleted("ghost", "test-run", None, tokens_used=999, duration_seconds=1.0)
            )
        )
    assert app.collector.snapshot().total_tokens == 0
    assert app.collector.snapshot().active_agents == 0


async def test_run_completion_writes_summary_jsonl_once(workflow, tmp_path):
    log_dir = tmp_path / "runs"
    app, bus = make_app(workflow, log_dir=log_dir)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 2, NodeCompleted("a", "test-run", "m-a", tokens_used=250, duration_seconds=1.2)
            )
        )
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 3, NodeStarted("b", "test-run", "m-b")))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 4, NodeCompleted("b", "test-run", "m-b", tokens_used=150, duration_seconds=0.8)
            )
        )
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 5, NodeStarted("c", "test-run", None)))
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 6, NodeCompleted("c", "test-run", None, tokens_used=100, duration_seconds=0.4)
            )
        )
        # late duplicate after completion must not write a second summary line
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 7, NodeStarted("a", "test-run", "m-a")))

    # the app exited itself when the last row turned terminal
    assert app.summary is not None
    assert app.summary.run_id == "test-run"
    assert [row.node_id for row in app.summary.nodes] == ["a", "b", "c"]
    assert app.summary.total_tokens == 500

    log_path = log_dir / "test-run" / "summary.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "run_summary"
    assert entry["run_id"] == "test-run"
    assert entry["total_tokens"] == 500
    assert [node["id"] for node in entry["nodes"]] == ["a", "b", "c"]
    assert entry["nodes"][2]["status"] == "done"


async def test_summary_write_failure_still_sets_summary_and_exits(
    workflow, tmp_path, monkeypatch
):
    log_dir = tmp_path / "runs"

    def boom(log_dir, run_id, summary):
        raise OSError("disk full")

    monkeypatch.setattr("agency.tui.app.append_summary_to_log", boom)
    app, bus = make_app(workflow, log_dir=log_dir)
    async with app.run_test():
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await bus.publish(
            EventEnvelope.create(EVENT_NODE_FAILED, 2, NodeFailed("a", "test-run", "m-a", "boom"))
        )

    # the log write failed, but the run still ends with a summary and an exit
    assert app.summary is not None
    assert app.summary.total_tokens == 0
    assert not (log_dir / "test-run" / "summary.jsonl").exists()
    # the execution log is opened at run start, so the run dir exists regardless
    assert (log_dir / "test-run" / "execution.jsonl").exists()


async def test_manual_exit_writes_no_summary(workflow, tmp_path):
    log_dir = tmp_path / "runs"
    app, bus = make_app(workflow, log_dir=log_dir)
    async with app.run_test() as pilot:
        await bus.publish(EventEnvelope.create(EVENT_NODE_STARTED, 1, NodeStarted("a", "test-run", "m-a")))
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

    assert app.summary is None
    assert not (log_dir / "test-run" / "summary.jsonl").exists()
    # but the execution log was opened at run start (run_started + node a)
    records = [
        json.loads(line)
        for line in (log_dir / "test-run" / "execution.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [r["type"] for r in records] == ["run_started", "node_started"]


async def test_tick_refreshes_panel_from_monitor(workflow):
    app, bus = make_app(workflow, monitor=FakeVRAMMonitor(usage_bytes=1_200_000_000))
    async with app.run_test():
        await bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED, 1, NodeCompleted("a", "test-run", "m-a", tokens_used=250, duration_seconds=1.2)
            )
        )
        app._tick()
        panel_lines = app.panel.render().plain.splitlines()
        assert panel_lines[0] == "total tokens: 250"
        assert panel_lines[1] == "vram: 1.20 GB"
        assert panel_lines[2] == "elapsed: 0:00"
        assert panel_lines[3] == "active agents: 0"


async def test_monitor_started_and_stopped_with_app(workflow):
    monitor = FakeVRAMMonitor()
    app, _ = make_app(workflow, monitor=monitor)
    async with app.run_test() as pilot:
        assert monitor.start_count == 1
        await pilot.press("q")
        await pilot.pause()

    assert monitor.stop_count == 1


def test_app_autobuilds_real_vram_monitor_when_not_injected(workflow, tmp_path):
    app = AgencyApp(
        workflow,
        "r-auto",
        event_bus=EventBus(),
        log_dir=tmp_path,
        vram_limit=536870912,
    )

    assert isinstance(app.vram_monitor, VRAMMonitor)
    assert app.vram_monitor._vram_limit_bytes == 536870912


def test_app_autobuilt_monitor_defaults_to_unlimited(workflow, tmp_path):
    app = AgencyApp(
        workflow,
        "r-auto",
        event_bus=EventBus(),
        log_dir=tmp_path,
    )

    assert isinstance(app.vram_monitor, VRAMMonitor)
    assert app.vram_monitor._vram_limit_bytes is None


async def test_demo_run_completes_with_deterministic_totals_and_single_log(
    workflow, tmp_path, monkeypatch
):
    def fast_simulate(wf, bus, run_id, fail_schedule=None):
        return simulate_run(wf, bus, run_id, delay=0.01)

    monkeypatch.setattr("agency.tui.app.simulate_run", fast_simulate)
    log_dir = tmp_path / "runs"
    app = AgencyApp(
        workflow,
        "demo-run",
        event_bus=EventBus(),
        demo=True,
        log_dir=log_dir,
        vram_monitor=FakeVRAMMonitor(),
    )
    async with app.run_test() as pilot:
        for _ in range(300):
            if app.summary is not None:
                break
            await pilot.pause(0.01)

    # the app exited itself when the last demo node turned terminal
    assert app.summary is not None
    assert app.summary.run_id == "demo-run"
    # deterministic totals: 100 * (topo_index + 1) -> 100 + 200 + 300
    assert app.summary.total_tokens == 600
    lines = (log_dir / "demo-run" / "summary.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 1


async def test_demo_run_writes_execution_log(workflow, tmp_path, monkeypatch):
    def fast_simulate(wf, bus, run_id, fail_schedule=None):
        return simulate_run(wf, bus, run_id, delay=0.01)

    monkeypatch.setattr("agency.tui.app.simulate_run", fast_simulate)
    log_dir = tmp_path / "runs"
    app = AgencyApp(
        workflow,
        "demo-run",
        event_bus=EventBus(),
        demo=True,
        log_dir=log_dir,
        vram_monitor=FakeVRAMMonitor(),
    )
    async with app.run_test() as pilot:
        for _ in range(300):
            if app.summary is not None:
                break
            await pilot.pause(0.01)

    assert app.summary is not None
    path = log_dir / "demo-run" / "execution.jsonl"
    assert path.exists()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["type"] for r in records] == [
        "run_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
    ]
    started = [r for r in records if r["type"] == "node_started"]
    assert [r["node_id"] for r in started] == ["a", "b", "c"]
    assert [r["input"] for r in started] == ["p", "p", None]
    for record in records:
        if record["type"] == "node_completed":
            assert record["output"] == f"simulated output for {record['node_id']}"

    run_dir = log_dir / "demo-run"
    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "demo-run"
    assert meta["workflow_name"] == "tui-test"
    assert meta["node_count"] == 3
    assert meta["vram_limit_bytes"] is None

    wal = [
        json.loads(line)
        for line in (run_dir / "wal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["seq"] for line in wal] == [0, 1, 2, 3, 4, 5, 6]
    assert wal[0]["mutation"] == "run_started"
    assert [(line["mutation"], line["node_id"], line["to"]) for line in wal[1:]] == [
        ("node_status", "a", "running"),
        ("node_status", "a", "completed"),
        ("node_status", "b", "running"),
        ("node_status", "b", "completed"),
        ("node_status", "c", "running"),
        ("node_status", "c", "completed"),
    ]
    for node_id in ("a", "b", "c"):
        cp = json.loads((run_dir / "checkpoints" / f"{node_id}.json").read_text(encoding="utf-8"))
        assert cp["status"] == "completed"
        assert cp["run_id"] == "demo-run"
        assert cp["output_ref"] == f"outputs/{node_id}.txt"
        assert (run_dir / "outputs" / f"{node_id}.txt").read_text(encoding="utf-8") == (
            f"simulated output for {node_id}"
        )
