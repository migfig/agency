"""Tests for checkpoint restore & replay (Story 4.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeStarted,
)
from agency.executor.contracts import RunResult
from agency.executor.replay import (
    ReplayError,
    find_source_run,
    replay_checkpoint,
    restore_set_for,
)
from agency.executor.run_state import RunStateStore, recover_status
from agency.tui.simulator import (
    simulated_duration,
    simulated_output,
    simulated_tokens,
)
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.parser import load_workflow


class MockAgentRunner:
    """Simulates agent node execution for replay tests."""

    async def run(
        self,
        node,
        context,
        *,
        phase_id=None,
        attempt=1,
        fallback=None,
        node_id=None,
    ) -> RunResult:
        attribution = node.id if node_id is None else node_id
        idx = "abcd".index(attribution) if attribution in "abcd" else 0
        base_tokens = (idx + 1) * 100
        base_duration = (idx + 1) * 0.5
        return RunResult(
            node_id=attribution,
            outcome="completed",
            model=node.model,
            output=simulated_output(attribution),
            tokens_used=base_tokens,
            duration_seconds=base_duration,
        )


class MockNonAgentRunner:
    """Simulates non-agent node execution for replay tests."""

    async def run(
        self,
        node,
        context,
        *,
        phase_id=None,
        deps=(),
    ) -> RunResult:
        return RunResult(
            node_id=node.id,
            outcome="completed",
        )


class MockContextStore:
    """No-op context store for replay tests."""

    def start_phase(self, phase_id, phase_name):
        pass

    async def flush_events(self) -> None:
        return None

    def complete_phase(self, phase_id):
        pass

    def record_output(self, phase_id, attribution, output):
        pass


class MockLoadManager:
    """No-op load manager for replay tests."""

    async def start(self):
        pass

    async def stop(self):
        pass

    async def acquire_slot(self, node_id, model_name, estimated_bytes):
        return None

    def release_slot(self, model_name, node_id, freed_bytes=None):
        pass


@pytest.fixture
def agent_runner():
    return MockAgentRunner()


@pytest.fixture
def non_agent_runner():
    return MockNonAgentRunner()


@pytest.fixture
def context_store():
    return MockContextStore()


@pytest.fixture
def load_manager():
    return MockLoadManager()


CHAIN_YAML = """
name: replay-chain
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: start
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: "b: {{ nodes.a.output }}"
  c:
    id: c
    type: agent
    model: m-c
    prompt_template: "c: {{ nodes.b.output }}"
  d:
    id: d
    type: agent
    model: m-d
    prompt_template: "d: {{ nodes.c.output }}"
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
  - from_id: c
    to_id: d
"""

WORKFLOW_NAME = "replay-chain"
BASE_STARTED_AT = "2026-01-01T00:00:00+00:00"
SOURCE_STARTED_AT = "2026-01-15T10:00:00+00:00"

BASE_METADATA_KEYS = {
    "run_id",
    "workflow_name",
    "started_at",
    "node_count",
    "vram_limit_bytes",
}


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
def log_dir(tmp_path):
    return tmp_path / "runs"


class EventCollector:
    """Subscribes to the two lifecycle events and records the payloads."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.started: list[NodeStarted] = []
        self.completed: list[NodeCompleted] = []
        self._tokens = []

    async def arm(self) -> None:
        self._tokens.append(
            await self._bus.subscribe(EVENT_NODE_STARTED, self._on_started)
        )
        self._tokens.append(
            await self._bus.subscribe(EVENT_NODE_COMPLETED, self._on_completed)
        )

    async def disarm(self) -> None:
        for token in self._tokens:
            token.cancel()
        self._tokens = []

    async def _on_started(self, envelope: EventEnvelope) -> None:
        self.started.append(envelope.payload)

    async def _on_completed(self, envelope: EventEnvelope) -> None:
        self.completed.append(envelope.payload)


async def make_source_run(
    log_dir: Path,
    workflow,
    bus: EventBus,
    run_id: str,
    completed: set[str],
    *,
    outputs: dict[str, str | None] | None = None,
) -> None:
    """Run a (partial) source run through the real store + bus."""
    outputs = {} if outputs is None else outputs
    store = RunStateStore(log_dir, run_id, workflow, bus)
    await store.start()
    try:
        for index, node_id in enumerate(DAGBuilder(workflow).topological_sort()):
            if node_id not in completed:
                continue
            model = workflow.nodes[node_id].model
            seq = bus._next_seq()
            await bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_STARTED, seq, NodeStarted(node_id, run_id, model)
                )
            )
            seq = bus._next_seq()
            await bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_COMPLETED,
                    seq,
                    NodeCompleted(
                        node_id=node_id,
                        run_id=run_id,
                        model=model,
                        tokens_used=simulated_tokens(index),
                        duration_seconds=simulated_duration(index),
                        output=outputs.get(
                            node_id, f"source output for {node_id}"
                        ),
                    ),
                )
            )
    finally:
        await store.close()


def rewrite_meta(log_dir: Path, run_id: str, **fields: object) -> dict:
    path = log_dir / run_id / "metadata.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(fields)
    path.write_text(json.dumps(meta), encoding="utf-8")
    return meta


def hand_run(
    log_dir: Path,
    run_id: str,
    *,
    workflow_name: str = WORKFLOW_NAME,
    started_at: str = BASE_STARTED_AT,
    checkpoints: dict[str, str] | None = None,
) -> Path:
    """Craft a minimal run dir (no WAL) for source-resolution tests."""
    run_dir = log_dir / run_id
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow_name": workflow_name,
                "started_at": started_at,
                "node_count": 4,
                "vram_limit_bytes": None,
            }
        ),
        encoding="utf-8",
    )
    for node_id, status in (checkpoints or {}).items():
        (run_dir / "checkpoints" / f"{node_id}.json").write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "workflow_id": workflow_name,
                    "run_id": run_id,
                    "status": status,
                    "output_ref": f"outputs/{node_id}.txt",
                    "summary": None,
                    "tokens_used": 1,
                    "model": None,
                    "elapsed_seconds": 0.5,
                    "timestamp": started_at,
                }
            ),
            encoding="utf-8",
        )
    return run_dir


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def read_run_dir_tree(log_dir: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for child in sorted(log_dir.iterdir()):
        if child.is_dir():
            out[child.name] = {p.name for p in child.rglob("*") if p.is_file()}
    return out


# -- restore_set_for -------------------------------------------------------


def test_restore_set_for_chain(workflow):
    assert restore_set_for(workflow, "a") == ["a"]
    assert restore_set_for(workflow, "b") == ["a", "b"]
    assert restore_set_for(workflow, "c") == ["a", "b", "c"]
    assert restore_set_for(workflow, "d") == ["a", "b", "c", "d"]


def test_restore_set_for_unknown_node(workflow):
    with pytest.raises(ReplayError, match="not in workflow 'replay-chain'"):
        restore_set_for(workflow, "zzz")


# -- find_source_run -------------------------------------------------------


def test_find_source_run_auto_picks_newest_started(log_dir):
    hand_run(log_dir, "r-older", started_at="2026-01-01T00:00:00+00:00",
             checkpoints={"c": "completed"})
    hand_run(log_dir, "r-newer", started_at="2026-02-01T00:00:00+00:00",
             checkpoints={"c": "completed"})
    assert find_source_run(log_dir, None, WORKFLOW_NAME, "c") == "r-newer"


def test_find_source_run_tie_breaks_by_run_id(log_dir):
    hand_run(log_dir, "r1", checkpoints={"c": "completed"})
    hand_run(log_dir, "r2", checkpoints={"c": "completed"})
    assert find_source_run(log_dir, None, WORKFLOW_NAME, "c") == "r2"


def test_find_source_run_auto_skips_ineligible(log_dir):
    hand_run(log_dir, "w-mismatch", checkpoints={"c": "completed"},
             workflow_name="other-workflow")
    hand_run(log_dir, "no-checkpoint", checkpoints={"a": "completed", "b": "completed"})
    hand_run(log_dir, "failed-c", checkpoints={"a": "completed", "c": "failed"})
    (log_dir / "run.log").write_text("", encoding="utf-8")  # non-dir entry
    hand_run(log_dir, "the-good-one", checkpoints={"c": "completed"})
    assert find_source_run(log_dir, None, WORKFLOW_NAME, "c") == "the-good-one"


def test_find_source_run_no_eligible(log_dir):
    hand_run(log_dir, "w-mismatch", checkpoints={"c": "completed"},
             workflow_name="other-workflow")
    hand_run(log_dir, "no-checkpoint", checkpoints={"a": "completed"})
    with pytest.raises(
        ReplayError,
        match="no source run found with a completed checkpoint for node 'c' in",
    ):
        find_source_run(log_dir, None, WORKFLOW_NAME, "c")


def test_find_source_run_missing_log_dir(tmp_path):
    missing = tmp_path / "no-such-dir"
    with pytest.raises(
        ReplayError, match=r"log dir '.+' does not exist"
    ):
        find_source_run(missing, None, WORKFLOW_NAME, "c")


def test_find_source_run_explicit_not_found(log_dir):
    with pytest.raises(ReplayError, match="source run 'nope' not found in"):
        find_source_run(log_dir, "nope", WORKFLOW_NAME, "c")


def test_find_source_run_explicit_workflow_mismatch(log_dir):
    hand_run(log_dir, "r1", checkpoints={"c": "completed"},
             workflow_name="other-workflow")
    with pytest.raises(
        ReplayError,
        match="belongs to workflow 'other-workflow', not 'replay-chain'",
    ):
        find_source_run(log_dir, "r1", WORKFLOW_NAME, "c")


def test_find_source_run_explicit_missing_checkpoint(log_dir):
    hand_run(log_dir, "r1", checkpoints={"a": "completed"})
    with pytest.raises(
        ReplayError, match="has no completed checkpoint for node 'c'"
    ):
        find_source_run(log_dir, "r1", WORKFLOW_NAME, "c")


# -- replay_checkpoint: happy paths -----------------------------------------


async def test_replay_restores_prefix_and_executes_rest(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    source_a = "alpha-é-中文"
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c"}, outputs={"a": source_a}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)
    source_tree_before = snapshot(log_dir / "src")

    replay_bus = EventBus()
    collector = EventCollector(replay_bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "c",
            event_bus=replay_bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    assert result.source_run_id == "src"
    assert len(result.new_run_id) == 15
    assert result.new_run_id != "src"
    assert result.restored == ["a", "b", "c"]
    assert result.executed == ["d"]

    new_dir = log_dir / result.new_run_id
    assert set(read_run_dir_tree(log_dir)) == {"src", result.new_run_id}
    assert snapshot(log_dir / "src") == source_tree_before

    meta = json.loads((new_dir / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) == BASE_METADATA_KEYS | {"replay_source"}
    assert meta["replay_source"] == {
        "source_run_id": "src",
        "checkpoint_node": "c",
    }
    assert meta["run_id"] == result.new_run_id
    assert meta["workflow_name"] == WORKFLOW_NAME
    assert meta["node_count"] == 4

    outputs = new_dir / "outputs"
    assert (outputs / "a.txt").read_bytes() == source_a.encode("utf-8")
    assert (outputs / "b.txt").read_text(encoding="utf-8") == "source output for b"
    assert (outputs / "c.txt").read_text(encoding="utf-8") == "source output for c"
    assert (outputs / "d.txt").read_text(encoding="utf-8") == simulated_output("d")

    assert [e.node_id for e in collector.started] == ["d"]
    d_started = collector.started[0]
    assert d_started.run_id == result.new_run_id
    assert d_started.model == "m-d"

    assert [e.node_id for e in collector.completed] == ["d"]
    events = {e.node_id: e for e in collector.completed}
    assert all(e.run_id == result.new_run_id for e in collector.completed)
    assert (events["d"].tokens_used, events["d"].duration_seconds) == (400, 2.0)
    assert events["d"].output == simulated_output("d")

    for nid, model in (("a", "m-a"), ("b", "m-b"), ("c", "m-c")):
        cp = json.loads((new_dir / "checkpoints" / f"{nid}.json").read_text())
        assert cp["run_id"] == result.new_run_id
        assert cp["status"] == "completed"
        assert cp["workflow_id"] == WORKFLOW_NAME
        assert cp["model"] == model
        assert cp["output_ref"] == f"outputs/{nid}.txt"
    cp_a = json.loads((new_dir / "checkpoints" / "a.json").read_text())
    assert cp_a["tokens_used"] == 100
    assert cp_a["elapsed_seconds"] == 0.5
    assert cp_a["summary"] == source_a

    cp_d = json.loads((new_dir / "checkpoints" / "d.json").read_text())
    assert cp_d["tokens_used"] == 400
    assert cp_d["elapsed_seconds"] == 2.0
    assert cp_d["model"] == "m-d"
    assert cp_d["summary"] == simulated_output("d")

    assert recover_status(log_dir, result.new_run_id, workflow) == {
        "a": "completed",
        "b": "completed",
        "c": "completed",
        "d": "completed",
    }

    wal = [json.loads(line) for line in
            (new_dir / "wal.jsonl").read_text(encoding="utf-8").splitlines()]
    seqs = [line["seq"] for line in wal]
    assert len(seqs) == 6
    assert seqs[0] == 0  # run_started
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], f"seq {i} ({seqs[i]}) not > prev ({seqs[i-1]})"
    assert wal[0]["mutation"] == "run_started"
    by_node: dict[str, list[dict]] = {}
    for line in wal[1:]:
        assert line["mutation"] == "node_status"
        by_node.setdefault(line["node_id"], []).append(line)
    assert by_node["a"][0]["from"] == "pending"
    assert by_node["a"][0]["to"] == "completed"
    assert len(by_node["a"]) == 1
    assert len(by_node["b"]) == 1
    assert len(by_node["c"]) == 1
    assert len(by_node["d"]) == 2
    assert by_node["d"][0]["to"] == "running"
    assert by_node["d"][1]["from"] == "running"
    assert by_node["d"][1]["to"] == "completed"

    exec_log = [json.loads(line) for line in
                (new_dir / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in exec_log] == [
        "run_started",
        "node_completed",
        "node_completed",
        "node_completed",
        "node_started",
        "node_completed",
    ]
    nc_a = exec_log[1]
    assert nc_a["node_id"] == "a"
    assert nc_a["input"] is None
    assert nc_a["started_at"] is None
    assert nc_a["output"] == source_a
    ns_d = exec_log[4]
    assert ns_d["node_id"] == "d"
    assert ns_d["model"] == "m-d"
    assert ns_d["output"] is None
    for line in exec_log:
        assert line["run_id"] == result.new_run_id
    assert not any(
        name.endswith(".tmp") for name in snapshot(new_dir)
    )


async def test_replay_entry_point_restores_only_entry(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c", "d"}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)

    collector = EventCollector(bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "a",
            event_bus=bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    assert result.restored == ["a"]
    assert result.executed == ["b", "c", "d"]
    events = {e.node_id: e for e in collector.completed}
    assert events["b"].tokens_used == 200
    assert events["c"].tokens_used == 300
    assert events["d"].tokens_used == 400
    new_dir = log_dir / result.new_run_id
    assert (new_dir / "outputs" / "b.txt").read_text() == simulated_output("b")
    assert (new_dir / "outputs" / "c.txt").read_text() == simulated_output("c")
    assert (new_dir / "outputs" / "d.txt").read_text() == simulated_output("d")


async def test_replay_sink_restores_everything(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c", "d"}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)

    collector = EventCollector(bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "d",
            event_bus=bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    assert result.restored == ["a", "b", "c", "d"]
    assert result.executed == []
    assert collector.started == []
    assert [e.node_id for e in collector.completed] == []
    new_dir = log_dir / result.new_run_id
    for nid in "abcd":
        cp = json.loads((new_dir / "checkpoints" / f"{nid}.json").read_text())
        assert cp["run_id"] == result.new_run_id
        assert cp["status"] == "completed"
    assert (new_dir / "outputs" / "d.txt").read_text() == "source output for d"
    meta = json.loads((new_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["replay_source"] == {
        "source_run_id": "src",
        "checkpoint_node": "d",
    }


async def test_replay_explicit_source_run_wins_over_newer_auto(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "r-old", {"a"}, outputs={"a": "from-old"}
    )
    rewrite_meta(log_dir, "r-old", started_at="2026-01-01T00:00:00+00:00")
    await make_source_run(
        log_dir, workflow, bus, "r-new", {"a"}, outputs={"a": "from-new"}
    )
    rewrite_meta(log_dir, "r-new", started_at="2026-03-01T00:00:00+00:00")

    collector = EventCollector(bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "a", source_run_id="r-old",
            event_bus=bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    assert result.source_run_id == "r-old"
    new_dir = log_dir / result.new_run_id
    assert (new_dir / "outputs" / "a.txt").read_text() == "from-old"
    meta = json.loads((new_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["replay_source"]["source_run_id"] == "r-old"


# -- replay_checkpoint: null outputs and metrics ----------------------------


async def test_replay_null_output_binds_empty_and_writes_no_file(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c", "d"}, outputs={"a": None}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)

    collector = EventCollector(bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "a",
            event_bus=bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    new_dir = log_dir / result.new_run_id
    assert not (new_dir / "outputs" / "a.txt").exists()
    cp_a = json.loads((new_dir / "checkpoints" / "a.json").read_text())
    assert cp_a["output_ref"] is None
    assert cp_a["summary"] is None


async def test_replay_null_checkpoint_metrics_coerce(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c", "d"}, outputs={"a": None}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)
    cp_path = log_dir / "src" / "checkpoints" / "a.json"
    cp = json.loads(cp_path.read_text(encoding="utf-8"))
    cp["tokens_used"] = None
    cp["elapsed_seconds"] = None
    cp["model"] = None
    cp_path.write_text(json.dumps(cp), encoding="utf-8")

    collector = EventCollector(bus)
    await collector.arm()
    try:
        result = await replay_checkpoint(
            log_dir, workflow, "a",
            event_bus=bus,
            agent_runner=agent_runner,
            non_agent_runner=non_agent_runner,
            context_store=context_store,
            load_manager=load_manager,
        )
    finally:
        await collector.disarm()

    new_dir = log_dir / result.new_run_id
    cp_a = json.loads((new_dir / "checkpoints" / "a.json").read_text())
    assert cp_a["tokens_used"] == 0
    assert cp_a["elapsed_seconds"] == 0.0
    assert cp_a["model"] is None


# -- replay_checkpoint: failures leave no artifacts --------------------------


async def test_replay_missing_ancestor_checkpoint_fails_cleanly(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "c", "d"}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)

    collector = EventCollector(bus)
    await collector.arm()
    with pytest.raises(
        ReplayError,
        match=r"cannot replay from source run 'src'.*'b': no checkpoint in source run",
    ):
        try:
            await replay_checkpoint(
                log_dir, workflow, "c",
                event_bus=bus,
                agent_runner=agent_runner,
                non_agent_runner=non_agent_runner,
                context_store=context_store,
                load_manager=load_manager,
            )
        finally:
            await collector.disarm()

    assert set(read_run_dir_tree(log_dir)) == {"src"}
    assert [p.name for p in log_dir.iterdir()] == ["src"]
    assert collector.started == []
    assert collector.completed == []


async def test_replay_missing_output_file_fails_cleanly(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    await make_source_run(
        log_dir, workflow, bus, "src", {"a", "b", "c"}
    )
    rewrite_meta(log_dir, "src", started_at=SOURCE_STARTED_AT)
    (log_dir / "src" / "outputs" / "c.txt").unlink()
    source_tree_before = snapshot(log_dir / "src")

    collector = EventCollector(bus)
    await collector.arm()
    with pytest.raises(
        ReplayError,
        match=r"'c': referenced output 'outputs/c\.txt' missing",
    ):
        try:
            await replay_checkpoint(
                log_dir, workflow, "c",
                event_bus=bus,
                agent_runner=agent_runner,
                non_agent_runner=non_agent_runner,
                context_store=context_store,
                load_manager=load_manager,
            )
        finally:
            await collector.disarm()

    assert [p.name for p in log_dir.iterdir()] == ["src"]
    assert snapshot(log_dir / "src") == source_tree_before
    assert collector.started == []
    assert collector.completed == []


async def test_replay_no_eligible_source_run_in_empty_dir(
    workflow, bus, log_dir, agent_runner, non_agent_runner, context_store, load_manager
):
    log_dir.mkdir(parents=True)
    collector = EventCollector(bus)
    await collector.arm()
    with pytest.raises(
        ReplayError,
        match="no source run found with a completed checkpoint for node 'c' in",
    ):
        try:
            await replay_checkpoint(
                log_dir, workflow, "c",
                event_bus=bus,
                agent_runner=agent_runner,
                non_agent_runner=non_agent_runner,
                context_store=context_store,
                load_manager=load_manager,
            )
        finally:
            await collector.disarm()
    assert list(log_dir.iterdir()) == []
    assert collector.started == []
    assert collector.completed == []
