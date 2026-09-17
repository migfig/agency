from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_AGENT_DEQUEUED,
    EVENT_AGENT_QUEUED,
    EVENT_VRAM_FREED,
    AgentDequeued,
    AgentQueued,
    EventEnvelope,
)
from agency.resource_manager.vram_tracker import resolve_vram_limit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AcquisitionResult:
    """Result of acquiring a VRAM slot."""

    acquired: bool  # True if fresh slot, False if shared-slot reuse
    node_id: str
    model_name: str
    queue_position: int | None  # None if not queued, position index if queued
    wait_seconds: float | None  # None if not queued


@dataclass
class _QueueEntry:
    """Internal FIFO queue entry for a waiting agent."""

    node_id: str
    model_name: str
    estimated_bytes: int
    event: asyncio.Event
    enqueued_at: float
    queue_position: int


class ModelLoadManager:
    """Coordinates concurrent model loading by tracking VRAM footprint and managing a FIFO queue."""

    def __init__(
        self,
        vram_limit_bytes: int | None = None,
        wait_timeout: float = 60.0,
    ):
        if vram_limit_bytes is None:
            resolution = resolve_vram_limit()
            if resolution.limit_bytes is not None:
                vram_limit_bytes = resolution.limit_bytes

        self._vram_limit = vram_limit_bytes
        self._free_bytes = vram_limit_bytes
        self._loaded_models: dict[str, tuple[int, set[str]]] = {}
        self._queue: list[_QueueEntry] = []
        self._lock = asyncio.Lock()
        self._bus: EventBus | None = None
        self._token: UnsubscribeToken | None = None
        self._wait_timeout = wait_timeout
        self._running = True
        self._offload_manager: Any = None

        if self._vram_limit is None:
            logger.warning(
                "No VRAM limit configured; load management will allow all loads without queuing"
            )
        else:
            logger.info(
                "ModelLoadManager initialized with %s bytes capacity", self._vram_limit
            )

    async def start(self) -> None:
        """Subscribe to VRAMFreed events. Call once during application bootstrap."""
        if self._vram_limit is None or self._bus is not None:
            return

        self._bus = get_event_bus()
        self._token = await self._bus.subscribe(EVENT_VRAM_FREED, self._on_vram_freed)
        logger.debug("ModelLoadManager subscribed to VRAMFreed events")

    async def stop(self) -> None:
        """Unsubscribe from events. Call during application shutdown."""
        async with self._lock:
            self._running = False
            for entry in self._queue:
                entry.event.set()
            self._queue.clear()

        if self._token is not None:
            self._token.cancel()
            self._token = None
            self._bus = None
            logger.debug("ModelLoadManager unsubscribed from VRAMFreed events")

    def set_offload_manager(self, offload_manager: Any) -> None:
        """Set the ModelOffloadManager reference for reload-on-acquire."""
        self._offload_manager = offload_manager

    async def acquire_slot(
        self, node_id: str, model_name: str, estimated_bytes: int
    ) -> AcquisitionResult:
        """Acquire a VRAM slot for the given model. Blocks until capacity is available."""
        if estimated_bytes <= 0:
            raise ValueError(f"estimated_bytes must be positive, got {estimated_bytes}")

        if self._vram_limit is not None and estimated_bytes > self._vram_limit:
            raise ValueError(
                f"estimated_bytes ({estimated_bytes}) exceeds VRAM limit ({self._vram_limit})"
            )

        offload_mgr = getattr(self, "_offload_manager", None)
        if offload_mgr is not None:
            offloaded = getattr(offload_mgr, "_offloaded_models", set())
            if model_name in offloaded:
                logger.info(
                    "Model %s is offloaded; triggering reload before acquire",
                    model_name,
                )
                result = await offload_mgr.reload_model(model_name, estimated_bytes)
                if not result.success:
                    logger.error(
                        "Reload failed for %s: %s; proceeding with normal queue flow",
                        model_name,
                        result.error,
                    )

        if self._vram_limit is None:
            logger.warning(
                "Load management inactive (no VRAM limit); allowing immediate load for %s",
                model_name,
            )
            return AcquisitionResult(
                acquired=True,
                node_id=node_id,
                model_name=model_name,
                queue_position=None,
                wait_seconds=None,
            )

        async with self._lock:
            if model_name in self._loaded_models:
                _, sharing_nodes = self._loaded_models[model_name]
                sharing_nodes.add(node_id)
                logger.info(
                    "Reusing existing VRAM slot for %s (node %s)", model_name, node_id
                )
                return AcquisitionResult(
                    acquired=False,
                    node_id=node_id,
                    model_name=model_name,
                    queue_position=None,
                    wait_seconds=None,
                )

            if self._free_bytes >= estimated_bytes:
                self._free_bytes -= estimated_bytes
                self._loaded_models[model_name] = (estimated_bytes, {node_id})
                logger.info(
                    "Acquired VRAM slot for %s (%s bytes); remaining: %s bytes",
                    model_name,
                    estimated_bytes,
                    self._free_bytes,
                )
                return AcquisitionResult(
                    acquired=True,
                    node_id=node_id,
                    model_name=model_name,
                    queue_position=None,
                    wait_seconds=None,
                )

            event = asyncio.Event()
            position = len(self._queue) + 1
            entry = _QueueEntry(
                node_id=node_id,
                model_name=model_name,
                estimated_bytes=estimated_bytes,
                event=event,
                enqueued_at=time.monotonic(),
                queue_position=position,
            )
            self._queue.append(entry)

            logger.info(
                "Queued agent %s for model %s at position %d (%s bytes needed)",
                node_id,
                model_name,
                position,
                estimated_bytes,
            )

        bus = self._bus or get_event_bus()
        envelope = EventEnvelope.create(
            EVENT_AGENT_QUEUED,
            bus._next_seq(),
            AgentQueued(
                node_id=node_id,
                model_name=model_name,
                queue_position=position,
                estimated_bytes=estimated_bytes,
            ),
        )
        await bus.publish(envelope)

        start_wait = time.monotonic()
        try:
            await asyncio.wait_for(event.wait(), timeout=self._wait_timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._queue[:] = [e for e in self._queue if e.event is not event]
            logger.warning(
                "Agent %s timed out after %.1fs waiting for VRAM capacity",
                node_id,
                self._wait_timeout,
            )
            return AcquisitionResult(
                acquired=False,
                node_id=node_id,
                model_name=model_name,
                queue_position=position,
                wait_seconds=self._wait_timeout,
            )

        wait_time = time.monotonic() - start_wait

        logger.info(
            "Agent %s dequeued for model %s after %.2fs (was position %d)",
            node_id,
            model_name,
            wait_time,
            position,
        )

        bus = self._bus or get_event_bus()
        envelope = EventEnvelope.create(
            EVENT_AGENT_DEQUEUED,
            bus._next_seq(),
            AgentDequeued(
                node_id=node_id,
                model_name=model_name,
                wait_seconds=wait_time,
                queue_position_was=position,
            ),
        )
        await bus.publish(envelope)

        return AcquisitionResult(
            acquired=True,
            node_id=node_id,
            model_name=model_name,
            queue_position=position,
            wait_seconds=wait_time,
        )

    def release_slot(
        self, model_name: str, node_id: str, freed_bytes: int | None = None
    ) -> None:
        """Release a VRAM slot for the given model and trigger queue drain."""
        if self._vram_limit is None:
            return

        try:
            task = asyncio.ensure_future(
                self._release_and_drain(model_name, node_id, freed_bytes)
            )
            task.add_done_callback(
                lambda t: t.result() if not t.cancelled() else None
            )
        except RuntimeError:
            logger.warning(
                "No running event loop; scheduling release_slot for %s deferred",
                model_name,
            )

    async def _release_and_drain(
        self, model_name: str, node_id: str, freed_bytes: int | None = None
    ) -> None:
        async with self._lock:
            if model_name not in self._loaded_models:
                logger.warning(
                    "Release requested for untracked model %s; no-op", model_name
                )
                return

            bytes_reserved, sharing_nodes = self._loaded_models[model_name]
            sharing_nodes.discard(node_id)

            if sharing_nodes:
                logger.info(
                    "Released VRAM slot for %s (node %s); %d other nodes still using",
                    model_name,
                    node_id,
                    len(sharing_nodes),
                )
                return

            del self._loaded_models[model_name]

            actual_freed = freed_bytes if freed_bytes is not None else bytes_reserved
            actual_freed = min(actual_freed, bytes_reserved)

            self._free_bytes += actual_freed

            logger.info(
                "Released VRAM for %s (%s bytes); free: %s bytes",
                model_name,
                actual_freed,
                self._free_bytes,
            )

        await self._drain_queue()

    async def _drain_queue(self, freed_bytes: int | None = None) -> None:
        """Iterate FIFO queue and signal next eligible agent if capacity allows."""
        if not self._queue:
            return

        async with self._lock:
            if freed_bytes is not None:
                self._free_bytes += freed_bytes

            i = 0
            while i < len(self._queue):
                entry = self._queue[i]

                if not self._running:
                    entry.event.set()
                    self._queue.pop(i)
                elif entry.model_name in self._loaded_models:
                    _, sharing_nodes = self._loaded_models[entry.model_name]
                    sharing_nodes.add(entry.node_id)
                    entry.event.set()
                    self._queue.pop(i)
                elif self._free_bytes >= entry.estimated_bytes:
                    self._free_bytes -= entry.estimated_bytes
                    self._loaded_models[entry.model_name] = (
                        entry.estimated_bytes,
                        {entry.node_id},
                    )
                    entry.event.set()
                    self._queue.pop(i)
                else:
                    i += 1

    async def _on_vram_freed(self, event: Any) -> None:
        """EventBus handler for VRAMFreed events — triggers queue drain."""
        freed_bytes = None
        if isinstance(event, EventEnvelope):
            payload = event.payload
            if hasattr(payload, "freed_bytes"):
                freed_bytes = payload.freed_bytes

        try:
            await self._drain_queue(freed_bytes)
        except RuntimeError:
            logger.warning("No running event loop; deferred queue drain on VRAMFreed")

    @property
    def free_bytes(self) -> int | None:
        return self._free_bytes if self._vram_limit is not None else None

    @property
    def queue_size(self) -> int:
        return len(self._queue)
