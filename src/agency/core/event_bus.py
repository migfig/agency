from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnsubscribeToken:
    """Opaque token returned by subscribe; call .cancel() to unsubscribe."""

    _bus: EventBus
    _event_type: str
    _handler: Callable

    def cancel(self) -> None:
        self._bus.unsubscribe(self._event_type, self._handler)


class EventBus:
    """Asyncio pub/sub event bus with monotonic sequence numbers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[Callable]] = {}
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def set_base_seq(self, n: int) -> None:
        """Clamp the internal sequence counter to at least *n*.

        Used by replay so restored nodes (seq 1..N) don't collide with
        live events that start publishing after the restore prefix.
        """
        self._seq = max(self._seq, n)

    async def subscribe(
        self, event_type: str, handler: Callable
    ) -> UnsubscribeToken:
        self._subscribers.setdefault(event_type, set()).add(handler)
        logger.debug("Subscribed to %s (total handlers: %d)", event_type, len(self._subscribers[event_type]))
        return UnsubscribeToken(_bus=self, _event_type=event_type, _handler=handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        handlers = self._subscribers.get(event_type)
        if handlers:
            handlers.discard(handler)
            if not handlers:
                del self._subscribers[event_type]
        logger.debug("Unsubscribed from %s", event_type)

    async def publish(self, event: Any) -> None:
        event_type = getattr(event, "event_type", None)
        if event_type is None:
            logger.warning("Published event has no event_type attribute, skipping")
            return

        handlers = self._subscribers.get(event_type, set())
        if not handlers:
            logger.debug("No subscribers for %s", event_type)
            return

        tasks = [handler(event) for handler in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Handler raised exception during publish: %s", result, exc_info=result)


# --- Singleton ---

_bus_instance: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the global singleton EventBus instance."""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = EventBus()
    return _bus_instance


def reset_event_bus() -> None:
    """Reset the singleton — useful for testing."""
    global _bus_instance
    _bus_instance = None
