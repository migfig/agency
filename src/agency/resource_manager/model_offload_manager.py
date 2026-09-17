from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_MODEL_OFFLOADED,
    EVENT_MODEL_RELOADED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
    ModelOffloaded,
    ModelReloaded,
    VRAMThresholdExceeded,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OffloadResult:
    """Result of an offload or reload operation."""

    model_name: str
    success: bool
    latency_ms: float
    error: str | None = None


class ModelOffloadManager:
    """Manages LRU-based model offloading from VRAM to CPU RAM and on-demand reloading."""

    def __init__(
        self,
        model_load_manager: Any,
        backend_router: Any,
        reload_timeout: float = 30.0,
    ):
        self._load_mgr = model_load_manager
        self._router = backend_router
        self._reload_timeout = reload_timeout
        self._offloaded_models: set[str] = set()
        self._model_last_accessed: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._bus: EventBus | None = None
        self._token: UnsubscribeToken | None = None

    async def start(self) -> None:
        """Subscribe to VRAMThresholdExceeded events. Call once during bootstrap."""
        if self._bus is not None:
            return

        self._bus = get_event_bus()
        self._token = await self._bus.subscribe(
            EVENT_VRAM_THRESHOLD_EXCEEDED, self._on_threshold_exceeded
        )
        logger.debug("ModelOffloadManager subscribed to VRAMThresholdExceeded events")

    async def stop(self) -> None:
        """Unsubscribe from events. Call during shutdown."""
        if self._token is not None:
            self._token.cancel()
            self._token = None
            self._bus = None
            logger.debug("ModelOffloadManager unsubscribed from VRAMThresholdExceeded events")

    async def record_access(self, model_name: str) -> None:
        """Record a model access timestamp for LRU tracking."""
        self._model_last_accessed[model_name] = time.monotonic()

    async def reload_model(self, model_name: str, estimated_bytes: int) -> OffloadResult:
        """Reload an offloaded model from CPU RAM back to VRAM."""
        start = time.monotonic()

        if model_name not in self._offloaded_models:
            return OffloadResult(
                model_name=model_name,
                success=True,
                latency_ms=0.0,
            )

        endpoint = self._get_model_endpoint(model_name)
        if not endpoint:
            latency = (time.monotonic() - start) * 1000
            logger.error("No endpoint found for model %s during reload", model_name)
            return OffloadResult(
                model_name=model_name,
                success=False,
                latency_ms=latency,
                error="no endpoint configured",
            )

        try:
            async with httpx.AsyncClient(timeout=self._reload_timeout) as client:
                resp = await client.post(f"{endpoint}/v1/models/{model_name}/reload")
                if resp.status_code not in (200, 201):
                    latency = (time.monotonic() - start) * 1000
                    logger.error(
                        "Reload failed for %s: HTTP %d", model_name, resp.status_code
                    )
                    return OffloadResult(
                        model_name=model_name,
                        success=False,
                        latency_ms=latency,
                        error=f"http_{resp.status_code}",
                    )

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            latency = (time.monotonic() - start) * 1000
            logger.error("Reload endpoint unreachable for %s: %s", model_name, exc)
            return OffloadResult(
                model_name=model_name,
                success=False,
                latency_ms=latency,
                error=str(exc),
            )

        async with self._lock:
            self._offloaded_models.discard(model_name)

        latency = (time.monotonic() - start) * 1000
        logger.info("Reloaded %s from CPU RAM to VRAM in %.1fms", model_name, latency)

        if self._bus:
            envelope = EventEnvelope.create(
                EVENT_MODEL_RELOADED,
                self._bus._next_seq(),
                ModelReloaded(
                    model_name=model_name,
                    endpoint=endpoint,
                    latency_ms=latency,
                ),
            )
            await self._bus.publish(envelope)

        return OffloadResult(
            model_name=model_name,
            success=True,
            latency_ms=latency,
        )

    async def _on_threshold_exceeded(self, event: Any) -> None:
        """EventBus handler for VRAMThresholdExceeded — triggers LRU offload."""
        payload = None
        if isinstance(event, EventEnvelope):
            payload = event.payload

        if not isinstance(payload, VRAMThresholdExceeded):
            return

        try:
            asyncio.create_task(self._try_offload_idle_model())
        except RuntimeError:
            logger.warning("No running event loop; deferred offload on threshold exceeded")

    async def _try_offload_idle_model(self) -> None:
        """Find the longest-idle model with zero active nodes and no queued agents, then offload it."""
        async with self._lock:
            candidate = self._select_lru_candidate()
            if not candidate:
                logger.warning("No eligible model for offload; skipping")
                return

            model_name, endpoint = candidate

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{endpoint}/v1/models/{model_name}/offload")
                if resp.status_code not in (200, 201):
                    logger.error(
                        "Offload failed for %s: HTTP %d", model_name, resp.status_code
                    )
                    return
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.error("Offload endpoint unreachable for %s: %s", model_name, exc)
            return

        async with self._lock:
            self._offloaded_models.add(model_name)

        latency = (time.monotonic() - start) * 1000
        logger.info("Offloaded %s to CPU RAM in %.1fms", model_name, latency)

        if self._bus:
            envelope = EventEnvelope.create(
                EVENT_MODEL_OFFLOADED,
                self._bus._next_seq(),
                ModelOffloaded(
                    model_name=model_name,
                    endpoint=endpoint,
                    latency_ms=latency,
                ),
            )
            await self._bus.publish(envelope)

    def _select_lru_candidate(self) -> tuple[str, str] | None:
        """Select the longest-idle model that has zero active nodes and no queued agents."""
        if not hasattr(self._load_mgr, "_loaded_models"):
            return None

        loaded = self._load_mgr._loaded_models
        queue_models = {e.model_name for e in getattr(self._load_mgr, "_queue", [])}

        candidates: list[tuple[float, str, str]] = []
        for model_name, (bytes_reserved, sharing_nodes) in loaded.items():
            if sharing_nodes:
                continue
            if model_name in self._offloaded_models:
                continue
            if model_name in queue_models:
                logger.debug(
                    "Skipping offload of %s: agents queued for it", model_name
                )
                continue

            endpoint = self._get_model_endpoint(model_name)
            if not endpoint:
                continue

            last_accessed = self._model_last_accessed.get(
                model_name, 0.0
            )
            candidates.append((last_accessed, model_name, endpoint))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        _, model_name, endpoint = candidates[0]
        return (model_name, endpoint)

    def _get_model_endpoint(self, model_name: str) -> str | None:
        """Look up the endpoint URL for a model from the backend router registry."""
        try:
            registry = getattr(self._router, "_registry", None)
            if not registry:
                return None
            all_models = registry.get_all() if hasattr(registry, "get_all") else {}
            entry = all_models.get(model_name)
            if entry and hasattr(entry, "endpoint"):
                return entry.endpoint
            if isinstance(all_models, dict):
                for m, info in all_models.items():
                    if m == model_name and isinstance(info, dict):
                        return info.get("endpoint")
        except Exception as exc:  # noqa: BLE001 - reflective registry lookup
            logger.debug("Endpoint lookup failed for %s: %s", model_name, exc)
        return None
