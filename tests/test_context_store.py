from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_STARTED,
    PhaseCompleted,
    PhaseStarted,
)
from agency.executor.context_store import ContextStore


@pytest.fixture(autouse=True)
def _clean_bus() -> None:
    reset_event_bus()


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def store(tmp_log_dir: Path) -> ContextStore:
    return ContextStore(run_id="test-run", log_dir=tmp_log_dir)


class TestLifecycle:
    def test_start_phase_creates_empty_context(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        assert store.get_context("p1") == {}

    def test_start_same_phase_twice_raises(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        with pytest.raises(ValueError, match="already started"):
            store.start_phase("p1", "Phase One Again")

    def test_record_output_stores_value(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        store.record_output("p1", "node_a", 42)
        assert store.get_context("p1") == {"node_a": 42}

    def test_record_multiple_outputs(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        store.record_output("p1", "a", 1)
        store.record_output("p1", "b", 2)
        assert store.get_context("p1") == {"a": 1, "b": 2}

    def test_record_on_unstarted_phase_raises(self, store: ContextStore) -> None:
        with pytest.raises(KeyError, match="not been started"):
            store.record_output("unknown", "x", 1)

    def test_complete_phase_persists_and_clears(
        self, store: ContextStore, tmp_log_dir: Path
    ) -> None:
        store.start_phase("p1", "Phase One")
        store.record_output("p1", "node_a", {"key": "value"})
        store.complete_phase("p1")

        # Memory cleared
        assert store.get_context("p1") == {}

        # File persisted
        log_file = tmp_log_dir / "test-run" / "phase_p1_context.jsonl"
        assert log_file.exists()
        with open(log_file) as f:
            entry = json.loads(f.readline())
        assert entry["phase_id"] == "p1"
        assert entry["context"]["node_a"] == {"key": "value"}

    def test_complete_unstarted_phase_raises(self, store: ContextStore) -> None:
        with pytest.raises(KeyError, match="not been started"):
            store.complete_phase("unknown")


class TestIsolation:
    def test_phases_have_separate_contexts(self, store: ContextStore) -> None:
        store.start_phase("p1", "One")
        store.start_phase("p2", "Two")
        store.record_output("p1", "x", 1)
        store.record_output("p2", "x", 2)
        assert store.get_context("p1") == {"x": 1}
        assert store.get_context("p2") == {"x": 2}

    def test_complete_one_phase_does_not_affect_other(
        self, store: ContextStore
    ) -> None:
        store.start_phase("p1", "One")
        store.start_phase("p2", "Two")
        store.record_output("p1", "a", 1)
        store.record_output("p2", "b", 2)
        store.complete_phase("p1")
        assert store.get_context("p2") == {"b": 2}


class TestGetContext:
    def test_unstarted_phase_returns_empty(self, store: ContextStore) -> None:
        assert store.get_context("nonexistent") == {}

    def test_completed_phase_returns_empty(self, store: ContextStore) -> None:
        store.start_phase("p1", "One")
        store.record_output("p1", "a", 1)
        store.complete_phase("p1")
        assert store.get_context("p1") == {}

    def test_returns_copy(self, store: ContextStore) -> None:
        store.start_phase("p1", "One")
        ctx = store.get_context("p1")
        ctx["mutated"] = True
        assert "mutated" not in store.get_context("p1")


class TestPersistencePath:
    def test_creates_nested_dirs(self, tmp_log_dir: Path) -> None:
        store = ContextStore(run_id="deep-run", log_dir=tmp_log_dir)
        store.start_phase("p1", "One")
        store.record_output("p1", "x", 1)
        store.complete_phase("p1")
        assert (tmp_log_dir / "deep-run" / "phase_p1_context.jsonl").exists()

    def test_append_multiple_completions(self, tmp_log_dir: Path) -> None:
        store = ContextStore(run_id="r", log_dir=tmp_log_dir)
        for i in range(2):
            pid = f"p{i}"
            store.start_phase(pid, f"P{i}")
            store.record_output(pid, "n", i)
            store.complete_phase(pid)

        lines = (tmp_log_dir / "r" / "phase_p0_context.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        lines = (tmp_log_dir / "r" / "phase_p1_context.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1


class TestEventPublishing:
    def test_start_phase_records_event(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        events = store.get_pending_events()
        assert len(events) == 1
        event_type, payload = events[0]
        assert event_type == EVENT_PHASE_STARTED
        assert isinstance(payload, PhaseStarted)
        assert payload.phase_id == "p1"
        assert payload.phase_name == "Phase One"
        assert payload.run_id == "test-run"

    def test_complete_phase_records_event(self, store: ContextStore) -> None:
        store.start_phase("p1", "Phase One")
        store.complete_phase("p1")
        events = store.get_pending_events()
        assert len(events) == 2
        event_type, payload = events[1]
        assert event_type == EVENT_PHASE_COMPLETED
        assert isinstance(payload, PhaseCompleted)
        assert payload.phase_id == "p1"
        assert payload.phase_name == "Phase One"

    async def test_flush_publishes_to_bus(self, tmp_log_dir: Path) -> None:
        bus = EventBus()
        store = ContextStore(run_id="r", log_dir=tmp_log_dir, event_bus=bus)

        received: list = []
        async def handler(e: Any) -> None:
            received.append(e)

        token = await bus.subscribe(EVENT_PHASE_STARTED, handler)
        try:
            store.start_phase("p1", "One")
            await store.flush_events()
            assert len(received) == 1
            assert received[0].payload.phase_id == "p1"
        finally:
            token.cancel()

    async def test_flush_clears_pending(self, tmp_log_dir: Path) -> None:
        bus = EventBus()
        store = ContextStore(run_id="r", log_dir=tmp_log_dir, event_bus=bus)
        store.start_phase("p1", "One")
        assert len(store.get_pending_events()) == 1
        await store.flush_events()
        assert store.get_pending_events() == []

    async def test_flush_without_bus_is_noop(self, store: ContextStore) -> None:
        store.start_phase("p1", "One")
        await store.flush_events()
        assert len(store.get_pending_events()) == 1  # Not cleared without bus
