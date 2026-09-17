"""Tests for automatic context summarization (Story 3.2)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_SUMMARIZATION_TRIGGERED,
    SummarizationTriggered,
)
from agency.executor.context_store import ContextStore
from agency.executor.summarizer import (
    ContextSummarizer,
    estimate_tokens,
    serialize_context,
)
from agency.resource_manager.backend_router import RoutingError


class FakeRouter:
    """Structural stand-in for BackendRouter (no runtime import in src)."""

    def __init__(
        self,
        summary: str | None = "Summary so far.",
        fail: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._summary = summary
        self._fail = fail

    def set_fail(self, fail: Exception | None) -> None:
        self._fail = fail

    async def route_inference(
        self,
        *,
        model: str,
        messages: list[dict],
        run_id: str,
        node_id: str,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> dict:
        self.calls.append(
            {"model": model, "messages": messages, "run_id": run_id, "node_id": node_id}
        )
        if self._fail is not None:
            raise self._fail
        return {"choices": [{"message": {"content": self._summary}}]}


@pytest.fixture(autouse=True)
def _clean_bus() -> None:
    reset_event_bus()


@pytest.fixture
def store(tmp_path: Path) -> ContextStore:
    return ContextStore(run_id="test-run", log_dir=tmp_path / "runs")


def make_summarizer(
    store: ContextStore,
    model: str = "phi-2",
    context_window: int = 100,
    threshold_percent: int = 80,
    event_bus: EventBus | None = None,
    fail: Exception | None = None,
    summary: str = "Summary so far.",
) -> tuple[ContextSummarizer, FakeRouter]:
    router = FakeRouter(summary=summary, fail=fail)
    summarizer = ContextSummarizer(
        run_id="test-run",
        store=store,
        router=router,
        model=model,
        context_window=context_window,
        threshold_percent=threshold_percent,
        event_bus=event_bus,
    )
    return summarizer, router


def fill(store: ContextStore, phase_id: str, length: int) -> None:
    store.start_phase(phase_id, "Phase One")
    store.record_output(phase_id, "a", "x" * length)


# 80-token threshold with context_window=100: serialized {"a":"..."} is
# 8 + len(value) chars, and 80 tokens == 320-323 chars.


class TestEstimateTokens:
    def test_four_chars_per_token(self) -> None:
        assert estimate_tokens("a" * 40) == 10

    def test_rounds_down(self) -> None:
        assert estimate_tokens("abc") == 0

    def test_empty(self) -> None:
        assert estimate_tokens("") == 0


class TestSerializeContext:
    def test_deterministic_regardless_of_key_order(self) -> None:
        assert serialize_context({"a": 1, "b": 2}) == serialize_context({"b": 2, "a": 1})

    def test_compact_and_sorted(self) -> None:
        assert serialize_context({"a": 1, "b": 2}) == '{"a":1,"b":2}'


class TestGate:
    async def test_empty_context_is_noop(self, store: ContextStore) -> None:
        store.start_phase("p1", "One")
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is False
        assert router.calls == []

    async def test_unknown_phase_is_noop(self, store: ContextStore) -> None:
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("ghost") is False
        assert router.calls == []

    async def test_completed_phase_is_noop(self, store: ContextStore) -> None:
        fill(store, "p1", 500)
        store.complete_phase("p1")
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is False
        assert router.calls == []

    async def test_below_threshold_is_noop(self, store: ContextStore) -> None:
        fill(store, "p1", 150)  # 158 chars -> 39 tokens < 80
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is False
        assert router.calls == []
        assert store.get_context("p1") == {"a": "x" * 150}

    async def test_boundary_triggers(self, store: ContextStore) -> None:
        fill(store, "p1", 314)  # 322 chars -> exactly 80 tokens (>=)
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is True
        assert len(router.calls) == 1

    async def test_custom_threshold_percent(self, store: ContextStore) -> None:
        fill(store, "p1", 314)  # 80 tokens
        summarizer, router = make_summarizer(store, threshold_percent=90)  # 90 tokens
        assert await summarizer.before_agent_run("p1") is False
        store.record_output("p1", "a", "x" * 354)  # 362 chars -> 90 tokens
        assert await summarizer.before_agent_run("p1") is True
        assert len(router.calls) == 1


class TestSummarization:
    async def test_context_replaced_with_summary(self, store: ContextStore) -> None:
        fill(store, "p1", 500)
        summarizer, _ = make_summarizer(store, summary="Key facts kept.")
        assert await summarizer.before_agent_run("p1") is True
        assert store.get_context("p1") == {"summary": "Key facts kept."}

    async def test_uses_dedicated_model(self, store: ContextStore) -> None:
        fill(store, "p1", 500)
        summarizer, router = make_summarizer(store, model="phi-2")
        await summarizer.before_agent_run("p1")
        call = router.calls[0]
        assert call["model"] == "phi-2"
        assert call["node_id"] == "__summarize__-p1"
        assert call["run_id"] == "test-run"
        assert [m["role"] for m in call["messages"]] == ["system", "user"]
        assert call["messages"][1]["content"] == serialize_context({"a": "x" * 500})

    async def test_event_published_with_token_counts(self, store: ContextStore) -> None:
        bus = EventBus()
        fill(store, "p1", 500)  # 508 chars -> 127 tokens
        summarizer, _ = make_summarizer(store, summary="Short.", event_bus=bus)

        received: list = []

        async def handler(e: Any) -> None:
            received.append(e)

        token = await bus.subscribe(EVENT_SUMMARIZATION_TRIGGERED, handler)
        try:
            assert await summarizer.before_agent_run("p1") is True
        finally:
            token.cancel()

        assert len(received) == 1
        payload = received[0].payload
        assert isinstance(payload, SummarizationTriggered)
        assert payload.phase_id == "p1"
        assert payload.tokens_before == 127
        assert payload.tokens_after < payload.tokens_before
        assert received[0].seq == 1

    async def test_logs_tokens_saved(self, store: ContextStore, caplog: pytest.LogCaptureFixture) -> None:
        fill(store, "p1", 500)  # 127 tokens before
        summarizer, _ = make_summarizer(store)
        with caplog.at_level(logging.INFO, logger="agency.executor.summarizer"):
            assert await summarizer.before_agent_run("p1") is True
        assert "summarized" in caplog.text
        assert "127" in caplog.text

    async def test_router_failure_preserves_context(
        self, store: ContextStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        fill(store, "p1", 500)
        summarizer, router = make_summarizer(
            store, fail=RoutingError("Model 'phi-2' not registered")
        )
        with caplog.at_level(logging.ERROR, logger="agency.executor.summarizer"):
            assert await summarizer.before_agent_run("p1") is False
        assert store.get_context("p1") == {"a": "x" * 500}
        assert len(router.calls) == 1
        assert "not registered" in caplog.text

        router.set_fail(None)  # retry on the next agent start
        assert await summarizer.before_agent_run("p1") is True


class TestPersistence:
    async def test_summary_persists_on_phase_completion(
        self, store: ContextStore, tmp_path: Path
    ) -> None:
        fill(store, "p1", 500)
        summarizer, _ = make_summarizer(store, summary="Key facts kept.")
        await summarizer.before_agent_run("p1")
        store.complete_phase("p1")

        log_file = tmp_path / "runs" / "test-run" / "phase_p1_context.jsonl"
        entry = json.loads(log_file.read_text().splitlines()[0])
        assert entry["phase_id"] == "p1"
        assert entry["context"] == {"summary": "Key facts kept."}


class TestSetContext:
    def test_replaces_store_context(self, store: ContextStore) -> None:
        fill(store, "p1", 10)
        store.set_context("p1", {"summary": "hello"})
        assert store.get_context("p1") == {"summary": "hello"}

    def test_unstarted_phase_raises(self, store: ContextStore) -> None:
        with pytest.raises(KeyError, match="not been started"):
            store.set_context("ghost", {"summary": "x"})

    def test_completed_phase_raises(self, store: ContextStore) -> None:
        fill(store, "p1", 10)
        store.complete_phase("p1")
        with pytest.raises(KeyError, match="not been started"):
            store.set_context("p1", {"summary": "x"})


class TestDegradation:
    """Failed or degenerate summarization degrades to 'no summarization', never a raise."""

    @pytest.mark.parametrize("blank_content", [None, "", "   "])
    async def test_blank_model_content_preserves_context(
        self, store: ContextStore, blank_content: str | None
    ) -> None:
        fill(store, "p1", 500)
        summarizer, router = make_summarizer(store, summary=blank_content)
        assert await summarizer.before_agent_run("p1") is False
        assert store.get_context("p1") == {"a": "x" * 500}
        assert len(router.calls) == 1

    async def test_unserializable_context_never_raises(
        self, store: ContextStore
    ) -> None:
        store.start_phase("p1", "One")
        store.record_output("p1", "a", {1, 2, 3})  # json.dumps cannot encode a set
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is False
        assert router.calls == []

    async def test_set_context_failure_never_raises(
        self, store: ContextStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fill(store, "p1", 500)

        def raise_mid_run(*args, **kwargs):
            raise KeyError("Phase 'p1' has not been started")

        monkeypatch.setattr(store, "set_context", raise_mid_run)
        summarizer, router = make_summarizer(store)
        assert await summarizer.before_agent_run("p1") is False
        assert store.get_context("p1") == {"a": "x" * 500}
        assert len(router.calls) == 1


class TestConstructorGuards:
    @pytest.mark.parametrize("bad_threshold", [0, 101, -1])
    def test_threshold_percent_out_of_range_rejected(
        self, store: ContextStore, bad_threshold: int
    ) -> None:
        with pytest.raises(ValueError, match="threshold_percent"):
            make_summarizer(store, threshold_percent=bad_threshold)

    @pytest.mark.parametrize("bad_window", [0, -1])
    def test_non_positive_window_rejected(
        self, store: ContextStore, bad_window: int
    ) -> None:
        with pytest.raises(ValueError, match="context_window"):
            make_summarizer(store, context_window=bad_window)
