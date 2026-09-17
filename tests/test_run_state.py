"""Tests for durable run state: sync-fsynced WAL + atomic checkpoints (Story 4.2)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeSkipped,
    NodeStarted,
)
from agency.executor.exec_tools import ToolExecutionResult
from agency.executor.run_log import RunLogger
from agency.executor.run_state import (
    COMPLETED,
    FAILED,
    PENDING,
    SKIPPED,
    RunStateStore,
    recover_status,
)
from agency.yaml_engine.parser import load_workflow

CHAIN_YAML = """
name: run-state-chain
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

RUN_ID = "rs-run"
WORKFLOW_NAME = "run-state-chain"

RUNNING_LINE_KEYS = {"seq", "ts", "mutation", "node_id", "from", "to"}
TERMINAL_LINE_KEYS = RUNNING_LINE_KEYS | {"output_ref", "tokens_used", "elapsed_seconds"}
CHECKPOINT_KEYS = {
    "node_id",
    "workflow_id",
    "run_id",
    "status",
    "output_ref",
    "summary",
    "tokens_used",
    "model",
    "elapsed_seconds",
    "timestamp",
}
METADATA_KEYS = {"run_id", "workflow_name", "started_at", "node_count", "vram_limit_bytes"}


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def workflow():
    return load_workflow(CHAIN_YAML)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def run_dir(tmp_path):
    return tmp_path / RUN_ID


def make_store(
    tmp_path,
    workflow,
    bus,
    *,
    log_dir=None,
    vram_limit=None,
    replay_source=None,
    execution_environment=None,
) -> RunStateStore:
    return RunStateStore(
        log_dir if log_dir is not None else tmp_path,
        RUN_ID,
        workflow,
        bus,
        vram_limit_bytes=vram_limit,
        replay_source=replay_source,
        execution_environment=execution_environment,
    )


def started(node_id: str, model: str | None, seq: int) -> EventEnvelope:
    return EventEnvelope.create(EVENT_NODE_STARTED, seq, NodeStarted(node_id, RUN_ID, model))


def completed(
    node_id: str,
    model: str | None,
    seq: int,
    *,
    output: str | None,
    tokens: int,
    duration: float,
) -> EventEnvelope:
    return EventEnvelope.create(
        EVENT_NODE_COMPLETED,
        seq,
        NodeCompleted(
            node_id,
            RUN_ID,
            model,
            tokens_used=tokens,
            duration_seconds=duration,
            output=output,
        ),
    )


def failed(node_id: str, model: str | None, seq: int, error: str) -> EventEnvelope:
    return EventEnvelope.create(EVENT_NODE_FAILED, seq, NodeFailed(node_id, RUN_ID, model, error))


def skipped(node_id: str, seq: int, *, reason: str, bindings: tuple[str, ...] = ()) -> EventEnvelope:
    return EventEnvelope.create(
        EVENT_NODE_SKIPPED, seq, NodeSkipped(node_id, RUN_ID, reason, bindings)
    )


def wal_lines(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "wal.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def checkpoint(run_dir: Path, node_id: str) -> dict:
    return json.loads((run_dir / "checkpoints" / f"{node_id}.json").read_text(encoding="utf-8"))


def store_warnings(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.name == "agency.executor.run_state"]


async def test_start_writes_metadata_and_wal_seed(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus, vram_limit=14_000_000_000)
    await store.start()

    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) == METADATA_KEYS
    assert meta["run_id"] == RUN_ID
    assert meta["workflow_name"] == WORKFLOW_NAME
    assert meta["node_count"] == 3
    assert meta["vram_limit_bytes"] == 14_000_000_000
    assert meta["started_at"].endswith("+00:00")

    lines = wal_lines(run_dir)
    assert len(lines) == 1
    assert set(lines[0]) == {"seq", "ts", "mutation", "run_id"}
    assert lines[0]["seq"] == 0
    assert lines[0]["mutation"] == "run_started"
    assert lines[0]["run_id"] == RUN_ID

    assert store._status == {"a": PENDING, "b": PENDING, "c": PENDING}
    await store.close()


async def test_replay_source_block_written_only_for_replay_runs(
    tmp_path, workflow, bus, run_dir
):
    source = {"source_run_id": "src-run", "checkpoint_node": "c"}
    store = make_store(tmp_path, workflow, bus, replay_source=source)
    await store.start()

    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) == METADATA_KEYS | {"replay_source"}
    assert meta["replay_source"] == source
    assert meta["run_id"] == RUN_ID
    assert meta["workflow_name"] == WORKFLOW_NAME
    assert meta["node_count"] == 3
    assert meta["started_at"].endswith("+00:00")
    await store.close()


async def test_happy_run_writes_monotonic_wal_checkpoints_and_outputs(
    tmp_path, workflow, bus, run_dir
):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    events = [
        started("a", "m-a", 1),
        completed("a", "m-a", 2, output="out-a", tokens=100, duration=0.25),
        started("b", "m-b", 3),
        completed("b", "m-b", 4, output="out-b", tokens=200, duration=0.5),
        started("c", "m-c", 5),
        completed("c", "m-c", 6, output="out-c", tokens=300, duration=0.75),
    ]
    for envelope in events:
        await bus.publish(envelope)

    lines = wal_lines(run_dir)
    assert [line["seq"] for line in lines] == [0, 1, 2, 3, 4, 5, 6]
    assert lines[0]["mutation"] == "run_started"
    for index, node_id in ((1, "a"), (3, "b"), (5, "c")):
        running = lines[index]
        assert set(running) == RUNNING_LINE_KEYS
        assert running["mutation"] == "node_status"
        assert running["node_id"] == node_id
        assert running["from"] == PENDING
        assert running["to"] == "running"
    for index, (node_id, (output, tokens, duration)) in {
        2: ("a", ("out-a", 100, 0.25)),
        4: ("b", ("out-b", 200, 0.5)),
        6: ("c", ("out-c", 300, 0.75)),
    }.items():
        terminal = lines[index]
        assert set(terminal) == TERMINAL_LINE_KEYS
        assert terminal["node_id"] == node_id
        assert terminal["from"] == "running"
        assert terminal["to"] == COMPLETED
        assert terminal["output_ref"] == f"outputs/{node_id}.txt"
        assert terminal["tokens_used"] == tokens
        assert terminal["elapsed_seconds"] == duration

    for index, envelope in ((2, events[1]), (4, events[3]), (6, events[5])):
        node_id = events[index - 1].payload.node_id
        cp = checkpoint(run_dir, node_id)
        assert set(cp) == CHECKPOINT_KEYS
        assert cp["status"] == COMPLETED
        assert cp["node_id"] == node_id
        assert cp["workflow_id"] == WORKFLOW_NAME
        assert cp["run_id"] == RUN_ID
        assert cp["model"] == envelope.payload.model
        assert cp["summary"] == envelope.payload.output
        assert cp["timestamp"] == envelope.timestamp.isoformat()
        assert (run_dir / "outputs" / f"{node_id}.txt").read_text(encoding="utf-8") == (
            envelope.payload.output
        )

    assert store._status == {"a": COMPLETED, "b": COMPLETED, "c": COMPLETED}
    assert list(run_dir.rglob("*.tmp")) == []
    await store.close()


async def test_failed_chain_skips_dependents_and_leaves_no_outputs(
    tmp_path, workflow, bus, run_dir
):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    await bus.publish(started("a", "m-a", 1))
    await bus.publish(completed("a", "m-a", 2, output="out-a", tokens=100, duration=0.3))
    b_started = started("b", "m-b", 3)
    await bus.publish(b_started)
    b_failed = failed("b", "m-b", 4, "boom")
    await bus.publish(b_failed)

    lines = wal_lines(run_dir)
    assert [line["seq"] for line in lines] == [0, 1, 2, 3, 4, 4]
    b_line = lines[4]
    assert set(b_line) == TERMINAL_LINE_KEYS
    assert b_line["node_id"] == "b"
    assert b_line["from"] == "running"
    assert b_line["to"] == FAILED
    assert b_line["output_ref"] is None
    assert b_line["tokens_used"] is None
    assert b_line["elapsed_seconds"] is not None
    c_line = lines[5]
    assert set(c_line) == TERMINAL_LINE_KEYS
    assert c_line["node_id"] == "c"
    assert c_line["from"] == PENDING
    assert c_line["to"] == SKIPPED
    assert c_line["output_ref"] is None

    b_cp = checkpoint(run_dir, "b")
    assert set(b_cp) == CHECKPOINT_KEYS
    assert b_cp["status"] == FAILED
    assert b_cp["summary"] == "boom"
    assert b_cp["output_ref"] is None
    assert b_cp["tokens_used"] is None
    assert b_cp["model"] == "m-b"
    assert b_cp["elapsed_seconds"] is not None
    c_cp = checkpoint(run_dir, "c")
    assert c_cp["status"] == SKIPPED
    assert c_cp["output_ref"] is None
    assert c_cp["summary"] is None
    assert c_cp["tokens_used"] is None
    assert c_cp["model"] is None
    assert c_cp["elapsed_seconds"] is None

    outputs = sorted(p.name for p in (run_dir / "outputs").iterdir())
    assert outputs == ["a.txt"]
    assert store._status == {"a": COMPLETED, "b": FAILED, "c": SKIPPED}
    assert "b" not in store._started
    await store.close()


async def test_node_skipped_persists_wal_checkpoint_and_status(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    await bus.publish(skipped("c", 1, reason="dependency_failed", bindings=("nodes.b.output",)))

    lines = wal_lines(run_dir)
    assert [line["seq"] for line in lines] == [0, 1]
    skip = lines[1]
    assert set(skip) == TERMINAL_LINE_KEYS
    assert skip["mutation"] == "node_status"
    assert skip["node_id"] == "c"
    assert skip["from"] == PENDING
    assert skip["to"] == SKIPPED
    assert skip["output_ref"] is None
    assert skip["tokens_used"] is None
    assert skip["elapsed_seconds"] is None

    cp = checkpoint(run_dir, "c")
    assert set(cp) == CHECKPOINT_KEYS
    assert cp["status"] == SKIPPED
    assert cp["workflow_id"] == WORKFLOW_NAME
    assert cp["run_id"] == RUN_ID
    assert cp["summary"] == "dependency_failed"
    assert cp["output_ref"] is None
    assert cp["tokens_used"] is None
    assert cp["model"] is None
    assert cp["elapsed_seconds"] is None

    assert not (run_dir / "outputs" / "c.txt").exists()
    assert "c" not in store._started
    assert store._status == {"a": PENDING, "b": PENDING, "c": SKIPPED}
    await store.close()


async def test_double_skip_is_a_noop(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    await bus.publish(skipped("c", 1, reason="unresolved_binding"))
    await bus.publish(skipped("c", 2, reason="dependency_failed"))

    # the second skip is ignored: one WAL line for 'c', summary not clobbered
    c_lines = [line for line in wal_lines(run_dir) if line.get("node_id") == "c"]
    assert [line["seq"] for line in c_lines] == [1]
    assert c_lines[0]["to"] == SKIPPED
    cp = checkpoint(run_dir, "c")
    assert cp["status"] == SKIPPED
    assert cp["summary"] == "unresolved_binding"
    assert store._status["c"] == SKIPPED
    await store.close()


async def test_completed_null_output_writes_no_output_file(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    await bus.publish(started("a", "m-a", 1))
    await bus.publish(completed("a", "m-a", 2, output=None, tokens=50, duration=0.1))

    assert not (run_dir / "outputs" / "a.txt").exists()
    lines = wal_lines(run_dir)
    terminal = lines[2]
    assert set(terminal) == TERMINAL_LINE_KEYS
    assert terminal["output_ref"] is None
    assert terminal["tokens_used"] == 50
    cp = checkpoint(run_dir, "a")
    assert cp["status"] == COMPLETED
    assert cp["output_ref"] is None
    assert cp["summary"] is None
    assert cp["tokens_used"] == 50
    assert cp["model"] == "m-a"
    assert store._status["a"] == COMPLETED
    await store.close()


async def test_fsync_failure_keeps_memory_behind_disk_and_recovers(
    tmp_path, workflow, bus, run_dir, monkeypatch, caplog
):
    def boom(fd):
        raise OSError("fsync denied")

    monkeypatch.setattr("agency.executor.run_state._fsync", boom)
    store = make_store(tmp_path, workflow, bus)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await store.start()
        await bus.publish(started("a", "m-a", 1))
        await bus.publish(completed("a", "m-a", 2, output="out-a", tokens=100, duration=0.25))

    assert store._status == {"a": PENDING, "b": PENDING, "c": PENDING}
    assert not (run_dir / "metadata.json").exists()
    assert not (run_dir / "checkpoints").exists()
    assert not (run_dir / "outputs" / "a.txt").exists()
    if (run_dir / "outputs").exists():
        assert list((run_dir / "outputs").iterdir()) == []
    assert list(run_dir.rglob("*.tmp")) == []
    assert store._started == {}
    warns = store_warnings(caplog)
    assert any("WAL" in w for w in warns)
    assert any("atomically" in w for w in warns)

    monkeypatch.undo()
    caplog.clear()
    await bus.publish(completed("b", "m-b", 3, output="out-b", tokens=200, duration=0.5))
    assert store._status == {"a": PENDING, "b": COMPLETED, "c": PENDING}
    assert (run_dir / "outputs" / "b.txt").read_text(encoding="utf-8") == "out-b"
    assert checkpoint(run_dir, "b")["status"] == COMPLETED
    # seq 0/1 survived as flushed-but-not-fsynced bytes (a real torn write);
    # the seq 2 completed-a transition never reached the WAL at all
    assert [line["seq"] for line in wal_lines(run_dir)] == [0, 1, 3]
    assert store_warnings(caplog) == []
    await store.close()


def test_recover_status_ignores_corrupted_lines(tmp_path, workflow, caplog):
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()
    node = '{\"seq\":%d,\"ts\":\"t\",\"mutation\":\"node_status\",\"node_id\":\"%s\",\"from\":\"%s\",\"to\":\"%s\"}'
    lines = [
        "{\"seq\":0,\"ts\":\"t\",\"mutation\":\"run_started\",\"run_id\":\"" + RUN_ID + "\"}",
        node % (1, "a", "pending", "running"),
        node % (2, "a", "running", "completed"),
        "[1,2,3]",
        node % (2, "a", "completed", "completed"),
        node % (0, "b", "pending", "running"),
        "{\"ts\":\"t\",\"mutation\":\"node_status\",\"node_id\":\"b\",\"from\":\"pending\",\"to\":\"running\"}",
        node % (6, "ghost", "pending", "running"),
        node % (8, "b", "pending", "running"),
        "{\"seq\":9,\"ts\":\"t\",\"mutation\":\"node_st",
    ]
    (run_dir / "wal.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        status = recover_status(tmp_path, RUN_ID, workflow)

    assert status == {"a": COMPLETED, "b": "running", "c": PENDING}
    warns = store_warnings(caplog)
    assert len(warns) == 6
    assert any("not a JSON object" in w for w in warns)
    assert any(w == "duplicate or out-of-order WAL seq 2, skipping" for w in warns)
    assert any(w == "duplicate or out-of-order WAL seq 0, skipping" for w in warns)
    assert any("without integer seq" in w for w in warns)
    assert any("unknown node 'ghost'" in w for w in warns)
    assert any("unparseable" in w for w in warns)


def test_recover_status_missing_wal_returns_all_pending(tmp_path, workflow, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        status = recover_status(tmp_path, RUN_ID, workflow)
    assert status == {"a": PENDING, "b": PENDING, "c": PENDING}
    assert "could not read WAL" in " | ".join(store_warnings(caplog))


async def test_unwritable_run_dir_never_raises(tmp_path, workflow, bus, caplog):
    blocker = tmp_path / "block"
    blocker.write_text("x", encoding="utf-8")
    log_dir = blocker / "runs"
    store = make_store(tmp_path, workflow, bus, log_dir=log_dir)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await store.start()
        await bus.publish(started("a", "m-a", 1))
        await bus.publish(completed("a", "m-a", 2, output="out-a", tokens=100, duration=0.25))
        await bus.publish(failed("b", "m-b", 3, "boom"))
        await store.close()

    assert not (blocker / "runs").exists()
    assert store._status == {"a": PENDING, "b": PENDING, "c": PENDING}
    assert store._started == {}
    warns = store_warnings(caplog)
    assert len(warns) >= 6


async def test_duplicate_and_late_events_are_ignored(tmp_path, workflow, bus, run_dir, caplog):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await bus.publish(started("a", "m-a", 1))
        await bus.publish(completed("a", "m-a", 2, output="out-a", tokens=100, duration=0.25))
        await bus.publish(completed("a", "m-a", 3, output="out-a", tokens=100, duration=0.25))
        await bus.publish(started("a", "m-a", 4))
        await bus.publish(started("ghost", None, 5))

    assert [line["seq"] for line in wal_lines(run_dir)] == [0, 1, 2]
    assert store._status == {"a": COMPLETED, "b": PENDING, "c": PENDING}
    assert "a" not in store._started
    assert any("ghost" in w for w in store_warnings(caplog))
    await store.close()


# --- User Story 5 (006): execution environment observability (FR-015) ---


@pytest.mark.parametrize("env", ["sandbox", "local"])
async def test_metadata_records_execution_environment(tmp_path, workflow, bus, run_dir, env):
    store = make_store(tmp_path, workflow, bus, execution_environment=env)
    await store.start()

    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) == METADATA_KEYS | {"execution_environment"}
    assert meta["execution_environment"] == env
    await store.close()


async def test_metadata_omits_execution_environment_when_unset(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus)
    await store.start()

    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert "execution_environment" not in meta
    assert set(meta) == METADATA_KEYS
    await store.close()


async def test_metadata_written_before_state_advances(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus, execution_environment="sandbox")
    await store.start()

    # the environment is on disk at start(), before any in-memory node state advances
    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["execution_environment"] == "sandbox"
    assert store._status == {"a": PENDING, "b": PENDING, "c": PENDING}
    await store.close()


def _sandbox_result(status, *, exit_code, output, error, duration, command):
    return ToolExecutionResult(
        status=status,
        exit_code=exit_code,
        output=output,
        stderr="",
        error=error,
        duration_seconds=duration,
        command=command,
        kind="shell",
        environment="sandbox",
    )


def _execution_lines(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "execution.jsonl").read_text(encoding="utf-8").splitlines()
    ]


async def test_run_record_carries_execution_fields_success(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus, execution_environment="sandbox")
    log = RunLogger(tmp_path, RUN_ID, workflow, bus)
    await store.start()
    await log.start()

    result = _sandbox_result(
        "success", exit_code=0, output="ok\n", error=None, duration=1.23, command="echo ok"
    )
    await bus.publish(started("a", "m-a", 1))
    await bus.publish(
        completed("a", "m-a", 2, output=json.dumps(result.as_dict()), tokens=0, duration=1.23)
    )

    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["execution_environment"] == "sandbox"
    lines = _execution_lines(run_dir)
    done = next(l for l in lines if l["type"] == "node_completed" and l["node_id"] == "a")
    record = json.loads(done["output"])
    assert record["command"] == "echo ok"
    assert record["duration_seconds"] == 1.23
    assert record["status"] == "success"
    assert record["environment"] == "sandbox"
    await store.close()
    await log.close()


async def test_run_record_carries_timeout_reason(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus, execution_environment="sandbox")
    log = RunLogger(tmp_path, RUN_ID, workflow, bus)
    await store.start()
    await log.start()

    result = _sandbox_result(
        "timed_out",
        exit_code=None,
        output="partial",
        error="exceeded 300s timeout",
        duration=300.5,
        command="sleep 300",
    )
    report = f"tool 'shell' execution timed_out: {json.dumps(result.as_dict())}"
    await bus.publish(started("a", "m-a", 1))
    await bus.publish(failed("a", "m-a", 2, report))

    lines = _execution_lines(run_dir)
    done = next(l for l in lines if l["type"] == "node_failed" and l["node_id"] == "a")
    record = json.loads(done["error"][done["error"].index("{"):])
    assert record["command"] == "sleep 300"
    assert record["duration_seconds"] == 300.5
    assert record["status"] == "timed_out"
    assert record["environment"] == "sandbox"
    assert record["error"] == "exceeded 300s timeout"
    await store.close()
    await log.close()


async def test_run_record_carries_resource_termination_reason(tmp_path, workflow, bus, run_dir):
    store = make_store(tmp_path, workflow, bus, execution_environment="sandbox")
    log = RunLogger(tmp_path, RUN_ID, workflow, bus)
    await store.start()
    await log.start()

    result = _sandbox_result(
        "error",
        exit_code=None,
        output="",
        error="oom-killed (container resource limit exceeded)",
        duration=2.0,
        command="sort bigfile",
    )
    report = f"tool 'shell' execution error: {json.dumps(result.as_dict())}"
    await bus.publish(started("a", "m-a", 1))
    await bus.publish(failed("a", "m-a", 2, report))

    lines = _execution_lines(run_dir)
    done = next(l for l in lines if l["type"] == "node_failed" and l["node_id"] == "a")
    record = json.loads(done["error"][done["error"].index("{"):])
    assert record["command"] == "sort bigfile"
    assert record["duration_seconds"] == 2.0
    assert record["status"] == "error"
    assert record["environment"] == "sandbox"
    assert record["error"] == "oom-killed (container resource limit exceeded)"
    await store.close()
    await log.close()
