from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agency.core.event_bus import (
    EventBus,
    UnsubscribeToken,
    get_event_bus,
    reset_event_bus,
)


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()
    yield
    reset_event_bus()


class TestSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_returns_token(self):
        bus = EventBus()
        token = await bus.subscribe("test", AsyncMock())
        assert isinstance(token, UnsubscribeToken)

    @pytest.mark.asyncio
    async def test_unsubscribe_via_token(self):
        bus = EventBus()
        handler = AsyncMock()
        token = await bus.subscribe("evt", handler)
        token.cancel()
        assert "evt" not in bus._subscribers

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_specific_handler(self):
        bus = EventBus()
        h1 = AsyncMock()
        h2 = AsyncMock()
        await bus.subscribe("evt", h1)
        await bus.subscribe("evt", h2)
        bus.unsubscribe("evt", h1)
        assert h2 in bus._subscribers["evt"]
        assert h1 not in bus._subscribers["evt"]

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_no_error(self):
        bus = EventBus()
        bus.unsubscribe("nope", AsyncMock())


class TestPublish:
    @pytest.mark.asyncio
    async def test_dispatches_to_subscribers(self):
        bus = EventBus()
        h1 = AsyncMock()
        h2 = AsyncMock()
        await bus.subscribe("evt", h1)
        await bus.subscribe("evt", h2)

        from agency.core.events import EventEnvelope
        env = EventEnvelope(event_type="evt", seq=1, timestamp=None, payload={"x": 1})
        await bus.publish(env)

        h1.assert_called_once_with(env)
        h2.assert_called_once_with(env)

    @pytest.mark.asyncio
    async def test_no_dispatch_without_subscribers(self):
        bus = EventBus()
        from agency.core.events import EventEnvelope
        env = EventEnvelope(event_type="unknown", seq=1, timestamp=None, payload={})
        await bus.publish(env)

    @pytest.mark.asyncio
    async def test_concurrent_handler_execution(self):
        bus = EventBus()
        order: list[int] = []

        async def slow(n: int):
            await asyncio.sleep(0.01 * n)
            order.append(n)

        h1 = lambda e: slow(2)
        h2 = lambda e: slow(1)
        await bus.subscribe("evt", h1)
        await bus.subscribe("evt", h2)

        from agency.core.events import EventEnvelope
        env = EventEnvelope(event_type="evt", seq=1, timestamp=None, payload={})
        await bus.publish(env)

        assert 1 in order and 2 in order


class TestSequenceNumbers:
    @pytest.mark.asyncio
    async def test_monotonic_increasing(self):
        bus = EventBus()
        s1 = bus._next_seq()
        s2 = bus._next_seq()
        s3 = bus._next_seq()
        assert s1 == 1
        assert s2 == 2
        assert s3 == 3


class TestSingleton:
    def test_singleton_identity(self):
        a = get_event_bus()
        b = get_event_bus()
        assert a is b

    def test_reset_creates_new(self):
        a = get_event_bus()
        reset_event_bus()
        b = get_event_bus()
        assert a is not b
