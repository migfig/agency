"""WebSocket event broadcast: live mirror of the singleton event bus (002).

One bounded queue per connected client, one consumer task per queue, a shared
serialized frame per event, and no backfill.  See
``specs/002-websocket-event-broadcast`` for the contract.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_AGENT_DEQUEUED,
    EVENT_AGENT_QUEUED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_MODEL_OFFLOADED,
    EVENT_MODEL_RELOADED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_INPUT_REQUESTED,
    EVENT_NODE_INPUT_RESOLVED,
    EVENT_NODE_QUEUED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_STARTED,
    EVENT_RUN_CANCEL,
    EVENT_SUMMARIZATION_TRIGGERED,
    EVENT_VRAM_FREED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
)

logger = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EVENT_VRAM_FREED,
    EVENT_MODEL_OFFLOADED,
    EVENT_MODEL_RELOADED,
    EVENT_AGENT_QUEUED,
    EVENT_AGENT_DEQUEUED,
    EVENT_PHASE_STARTED,
    EVENT_PHASE_COMPLETED,
    EVENT_SUMMARIZATION_TRIGGERED,
    EVENT_NODE_QUEUED,
    EVENT_NODE_STARTED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_SKIPPED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_RUN_CANCEL,
    EVENT_NODE_INPUT_REQUESTED,
    EVENT_NODE_INPUT_RESOLVED,
)

DEFAULT_QUEUE_MAXSIZE = 256

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_SLOW_CONSUMER = 1008

SHUTDOWN_REASON = "server shutting down"
SLOW_CONSUMER_REASON = "slow consumer"


def _jsonify(value: Any) -> Any:
    """Render datetimes as ISO-8601 and tuples as arrays for JSON (FR-012)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def serialize_frame(envelope: EventEnvelope) -> str:
    """Serialize one envelope into the wire frame.

    The frame carries exactly ``event_type``, ``seq``, ``timestamp`` (ISO-8601),
    ``run_id`` (``None`` for the 7 kinds whose payload has no run_id), and the
    dataclass-flattened ``payload``.
    """
    payload = envelope.payload
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    frame = {
        "event_type": envelope.event_type,
        "seq": envelope.seq,
        "timestamp": envelope.timestamp.isoformat(),
        "run_id": getattr(envelope.payload, "run_id", None),
        "payload": payload,
    }
    return json.dumps(_jsonify(frame))


@dataclass(eq=False)
class StreamConnection:
    """One live ``/events`` client: bounded queue + consumer task."""

    id: int
    websocket: Any
    queue: asyncio.Queue = field(repr=False)
    consumer: asyncio.Task | None = field(default=None, repr=False)
    state: str = "streaming"
    close_code: int | None = None
    overflowing: bool = False


class EventBroadcaster:
    """Fan out the singleton event bus to live ``/events`` clients."""

    def __init__(
        self,
        bus: EventBus | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._bus = bus if bus is not None else get_event_bus()
        self._queue_maxsize = queue_maxsize
        self._connections: set[StreamConnection] = set()
        self._tokens: list[UnsubscribeToken] = []
        self._running = False
        self._next_id = 0

    @property
    def connections(self) -> set[StreamConnection]:
        return self._connections

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Subscribe to every event type on the bus (idempotent)."""
        if self._running:
            return
        self._running = True
        for event_type in EVENT_TYPES:
            self._tokens.append(
                await self._bus.subscribe(event_type, self._on_event)
            )

    async def _on_event(self, envelope: EventEnvelope) -> None:
        frame = serialize_frame(envelope)
        for conn in list(self._connections):
            if conn.overflowing:
                continue
            try:
                conn.queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._overflow(conn)

    def _overflow(self, conn: StreamConnection) -> None:
        """FR-014: a client whose queue overflowed is closed with 1008."""
        conn.overflowing = True
        conn.close_code = CLOSE_SLOW_CONSUMER
        logger.warning("stream client %d overflowed; closing with 1008", conn.id)
        asyncio.create_task(self._close_overflow(conn))

    async def _close_overflow(self, conn: StreamConnection) -> None:
        if conn.state == "closed":
            return
        conn.state = "closing"
        try:
            await conn.websocket.close(
                code=CLOSE_SLOW_CONSUMER, reason=SLOW_CONSUMER_REASON
            )
        except Exception:
            logger.debug(
                "overflow close for client %d failed", conn.id, exc_info=True
            )
        finally:
            self.disconnect(conn)

    def connect(self, websocket: Any) -> StreamConnection:
        """Register a websocket and start its consumer task (FR-003)."""
        self._next_id += 1
        conn = StreamConnection(
            id=self._next_id,
            websocket=websocket,
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
        )
        self._connections.add(conn)
        conn.consumer = asyncio.create_task(
            self._consume(conn), name=f"stream-consume-{conn.id}"
        )
        return conn

    def disconnect(self, conn: StreamConnection) -> None:
        """Idempotent cleanup funnel for every disconnect path (research R6)."""
        if conn.state == "closed":
            return
        self._connections.discard(conn)
        task = conn.consumer
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        conn.state = "closed"

    async def _consume(self, conn: StreamConnection) -> None:
        try:
            while True:
                frame = await conn.queue.get()
                await conn.websocket.send_text(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "stream client %d send failed; disconnecting", conn.id, exc_info=True
            )
        finally:
            self.disconnect(conn)

    async def stop(self) -> None:
        """Drain every connection, unsubscribe all handlers, cancel consumers."""
        if not self._running:
            return
        self._running = False
        conns = list(self._connections)
        for conn in conns:
            if conn.state == "closed":
                continue
            conn.state = "closing"
            conn.close_code = CLOSE_GOING_AWAY
            try:
                await conn.websocket.close(
                    code=CLOSE_GOING_AWAY, reason=SHUTDOWN_REASON
                )
            except Exception:
                logger.debug(
                    "shutdown close for client %d failed", conn.id, exc_info=True
                )
        for conn in conns:
            self.disconnect(conn)
        for token in self._tokens:
            token.cancel()
        self._tokens = []
        tasks = [conn.consumer for conn in conns if conn.consumer is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
