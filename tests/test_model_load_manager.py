from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pytest

from agency.core.event_bus import get_event_bus, reset_event_bus
from agency.core.events import (
    EVENT_AGENT_DEQUEUED,
    EVENT_AGENT_QUEUED,
    EVENT_VRAM_FREED,
    AgentDequeued,
    AgentQueued,
    EventEnvelope,
    VRAMFreed,
)
from agency.resource_manager.model_load_manager import (
    ModelLoadManager,
)


@pytest.fixture(autouse=True)
def reset_bus():
    """Reset the EventBus singleton before each test."""
    reset_event_bus()


@pytest.fixture
def mlm():
    return ModelLoadManager(vram_limit_bytes=1000, wait_timeout=5.0)


@pytest.fixture
async def running_mlm(mlm):
    await mlm.start()
    yield mlm
    await mlm.stop()


# ── Basic acquire / release (immediate capacity) ──────────────────────────

@pytest.mark.asyncio
async def test_acquire_immediate(running_mlm: ModelLoadManager):
    res = await running_mlm.acquire_slot("node-1", "phi3", 400)
    assert res.acquired is True
    assert res.node_id == "node-1"
    assert res.model_name == "phi3"
    assert res.queue_position is None
    assert res.wait_seconds is None

    assert running_mlm.free_bytes == 600
    assert running_mlm.queue_size == 0


@pytest.mark.asyncio
async def test_release_immediate(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 400)
    running_mlm.release_slot("phi3", "node-1")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 1000


@pytest.mark.asyncio
async def test_release_custom_freed_bytes(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 400)
    running_mlm.release_slot("phi3", "node-1", freed_bytes=200)
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 800


@pytest.mark.asyncio
async def test_release_freed_bytes_clamped(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 400)
    running_mlm.release_slot("phi3", "node-1", freed_bytes=9999)
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 1000


# ── Shared-slot reuse ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shared_slot_reuse(running_mlm: ModelLoadManager):
    r1 = await running_mlm.acquire_slot("node-1", "phi3", 400)
    assert r1.acquired is True

    r2 = await running_mlm.acquire_slot("node-2", "phi3", 400)
    assert r2.acquired is False
    assert running_mlm.free_bytes == 600

    running_mlm.release_slot("phi3", "node-1")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 600

    running_mlm.release_slot("phi3", "node-2")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 1000


# ── Queuing and drain ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_and_drain(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 800)
    assert running_mlm.free_bytes == 200

    task = asyncio.create_task(
        running_mlm.acquire_slot("node-2", "llama", 300)
    )
    await asyncio.sleep(0.05)
    assert running_mlm.queue_size == 1

    bus = get_event_bus()
    envelope = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="phi3", freed_bytes=600, remaining_bytes=400),
    )
    await bus.publish(envelope)
    await asyncio.sleep(0.1)

    res = await asyncio.wait_for(task, timeout=2.0)
    assert res.acquired is True
    assert res.queue_position == 1
    assert res.wait_seconds is not None
    assert running_mlm.queue_size == 0


@pytest.mark.asyncio
async def test_fifo_order(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 900)

    task_a = asyncio.create_task(
        running_mlm.acquire_slot("node-a", "model-a", 200)
    )
    await asyncio.sleep(0.02)
    task_b = asyncio.create_task(
        running_mlm.acquire_slot("node-b", "model-b", 200)
    )
    await asyncio.sleep(0.02)
    assert running_mlm.queue_size == 2

    bus = get_event_bus()
    envelope = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="phi3", freed_bytes=900, remaining_bytes=100),
    )
    await bus.publish(envelope)
    await asyncio.sleep(0.2)

    res_a = await asyncio.wait_for(task_a, timeout=2.0)
    res_b = await asyncio.wait_for(task_b, timeout=2.0)
    assert res_a.queue_position == 1
    assert res_b.queue_position == 2


# ── Timeout ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_timeout(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 1000)

    task = asyncio.create_task(
        running_mlm.acquire_slot("node-2", "llama", 500)
    )
    await asyncio.sleep(6)

    res = await asyncio.wait_for(task, timeout=2.0)
    assert res.acquired is False
    assert res.wait_seconds == 5.0


# ── Validation ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_estimated_bytes_zero_raises(running_mlm: ModelLoadManager):
    with pytest.raises(ValueError, match="positive"):
        await running_mlm.acquire_slot("node-1", "phi3", 0)


@pytest.mark.asyncio
async def test_estimated_bytes_negative_raises(running_mlm: ModelLoadManager):
    with pytest.raises(ValueError, match="positive"):
        await running_mlm.acquire_slot("node-1", "phi3", -100)


@pytest.mark.asyncio
async def test_estimated_bytes_exceeds_limit_raises(running_mlm: ModelLoadManager):
    with pytest.raises(ValueError, match="exceeds"):
        await running_mlm.acquire_slot("node-1", "phi3", 1001)


# ── No VRAM limit (pass-through mode) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_no_vram_limit_passes_through():
    with patch(
        "agency.resource_manager.model_load_manager.resolve_vram_limit"
    ) as mock:
        from agency.resource_manager.vram_tracker import VRAMResolutionResult

        mock.return_value = VRAMResolutionResult(limit_bytes=None, source="none")
        mlm = ModelLoadManager(vram_limit_bytes=None)
        res = await mlm.acquire_slot("node-1", "phi3", 5000)
        assert res.acquired is True
        assert mlm.free_bytes is None


# ── Release untracked model (no-op) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_release_untracked_model_noop(running_mlm: ModelLoadManager):
    running_mlm.release_slot("unknown-model", "node-1")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 1000


# ── Event publishing verification ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_queued_event_published(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 800)

    published_events = []
    async def on_queued(e):
        published_events.append(e)
    token = await get_event_bus().subscribe(EVENT_AGENT_QUEUED, on_queued)
    try:
        task = asyncio.create_task(
            running_mlm.acquire_slot("node-2", "llama", 300)
        )
        await asyncio.sleep(0.1)

        assert len(published_events) == 1
        evt = published_events[0]
        assert isinstance(evt, EventEnvelope)
        assert evt.event_type == EVENT_AGENT_QUEUED
        payload = evt.payload
        assert isinstance(payload, AgentQueued)
        assert payload.node_id == "node-2"
        assert payload.model_name == "llama"
        assert payload.queue_position == 1

        running_mlm.release_slot("phi3", "node-1")
        await asyncio.sleep(0.2)
        await task
    finally:
        token.cancel()


@pytest.mark.asyncio
async def test_dequeued_event_published(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 800)

    published_events = []
    async def on_dequeued(e):
        published_events.append(e)
    token = await get_event_bus().subscribe(EVENT_AGENT_DEQUEUED, on_dequeued)
    try:
        task = asyncio.create_task(
            running_mlm.acquire_slot("node-2", "llama", 300)
        )
        await asyncio.sleep(0.05)

        bus = get_event_bus()
        envelope = EventEnvelope.create(
            EVENT_VRAM_FREED,
            bus._next_seq(),
            VRAMFreed(model_name="phi3", freed_bytes=600, remaining_bytes=400),
        )
        await bus.publish(envelope)
        await asyncio.sleep(0.2)
        await task

        assert len(published_events) == 1
        evt = published_events[0]
        assert isinstance(evt, EventEnvelope)
        assert evt.event_type == EVENT_AGENT_DEQUEUED
        payload = evt.payload
        assert isinstance(payload, AgentDequeued)
        assert payload.node_id == "node-2"
        assert payload.model_name == "llama"
        assert payload.wait_seconds is not None
    finally:
        token.cancel()


# ── Stop signals pending events ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_signals_pending_events(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 1000)

    asyncio.create_task(
        running_mlm.acquire_slot("node-2", "llama", 500)
    )
    await asyncio.sleep(0.05)
    assert running_mlm.queue_size == 1

    await running_mlm.stop()
    await asyncio.sleep(0.1)
    assert running_mlm.queue_size == 0


# ── VRAMFreed handler uses freed_bytes from event ─────────────────────────

@pytest.mark.asyncio
async def test_vram_freed_uses_event_payload(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 800)
    assert running_mlm.free_bytes == 200

    task = asyncio.create_task(
        running_mlm.acquire_slot("node-2", "llama", 350)
    )
    await asyncio.sleep(0.05)
    assert running_mlm.queue_size == 1

    bus = get_event_bus()
    envelope = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="phi3", freed_bytes=100, remaining_bytes=300),
    )
    await bus.publish(envelope)
    await asyncio.sleep(0.1)

    assert running_mlm.queue_size == 1
    assert running_mlm.free_bytes == 300

    envelope2 = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="phi3", freed_bytes=100, remaining_bytes=400),
    )
    await bus.publish(envelope2)
    await asyncio.sleep(0.1)

    res = await asyncio.wait_for(task, timeout=2.0)
    assert res.acquired is True


# ── Multiple drains in sequence ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_sequential_drains(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 500)
    assert running_mlm.free_bytes == 500

    task_a = asyncio.create_task(
        running_mlm.acquire_slot("node-a", "model-a", 600)
    )
    await asyncio.sleep(0.02)
    task_b = asyncio.create_task(
        running_mlm.acquire_slot("node-b", "model-b", 600)
    )
    await asyncio.sleep(0.02)
    assert running_mlm.queue_size == 2

    bus = get_event_bus()
    envelope = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="phi3", freed_bytes=400, remaining_bytes=900),
    )
    await bus.publish(envelope)
    await asyncio.sleep(0.2)

    res_a = await asyncio.wait_for(task_a, timeout=2.0)
    assert res_a.acquired is True
    assert running_mlm.queue_size == 1

    envelope2 = EventEnvelope.create(
        EVENT_VRAM_FREED,
        bus._next_seq(),
        VRAMFreed(model_name="model-a", freed_bytes=400, remaining_bytes=500),
    )
    await bus.publish(envelope2)
    await asyncio.sleep(0.2)

    res_b = await asyncio.wait_for(task_b, timeout=2.0)
    assert res_b.acquired is True
    assert running_mlm.queue_size == 0


# ── Properties ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_size_property(running_mlm: ModelLoadManager):
    await running_mlm.acquire_slot("node-1", "phi3", 900)

    task = asyncio.create_task(
        running_mlm.acquire_slot("node-2", "llama", 200)
    )
    await asyncio.sleep(0.05)
    assert running_mlm.queue_size == 1

    running_mlm.release_slot("phi3", "node-1")
    await asyncio.sleep(0.2)
    await task
    assert running_mlm.queue_size == 0


# ── resolve_vram_limit fallback ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_vram_limit_fallback():
    with patch("agency.resource_manager.model_load_manager.resolve_vram_limit") as mock:
        mock.return_value = Mock(limit_bytes=2048)
        mlm = ModelLoadManager()
        assert mlm._vram_limit == 2048


@pytest.mark.asyncio
async def test_resolve_vram_limit_none():
    with patch("agency.resource_manager.model_load_manager.resolve_vram_limit") as mock:
        mock.return_value = Mock(limit_bytes=None)
        mlm = ModelLoadManager()
        assert mlm._vram_limit is None


# ── Acquire then release same model different nodes ───────────────────────

@pytest.mark.asyncio
async def test_acquire_release_different_nodes(running_mlm: ModelLoadManager):
    r1 = await running_mlm.acquire_slot("node-1", "phi3", 400)
    assert r1.acquired is True
    assert running_mlm.free_bytes == 600

    r2 = await running_mlm.acquire_slot("node-2", "phi3", 400)
    assert r2.acquired is False
    assert running_mlm.free_bytes == 600

    running_mlm.release_slot("phi3", "node-1")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 600

    running_mlm.release_slot("phi3", "node-2")
    await asyncio.sleep(0.05)
    assert running_mlm.free_bytes == 1000
