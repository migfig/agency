from __future__ import annotations

import asyncio
import logging
from typing import Any

from agency.core.event_bus import get_event_bus
from agency.core.events import (
    EVENT_VRAM_FREED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
    VRAMFreed,
    VRAMThresholdExceeded,
)
from agency.resource_manager.vram_tracker import resolve_vram_limit

logger = logging.getLogger(__name__)

SAFETY_MARGIN_BYTES = 512 * 1024 * 1024  # 512 MB


class VRAMMonitor:
    """Async VRAM polling engine that publishes threshold and freed events.

    Polls the real GPU (device 0) via pynvml. Also exposes ``usage_bytes`` (the
    latest sampled used-bytes, or ``None`` before the first sample / after
    ``stop()``) for consumers that only need to read the current sample.
    """

    def __init__(
        self,
        interval_ms: float = 500,
        vram_limit_bytes: int | None = None,
    ) -> None:
        self._interval_s = interval_ms / 1000.0
        self._vram_limit_bytes = vram_limit_bytes
        self._poll_task: asyncio.Task[Any] | None = None
        self._running = False
        self._threshold_fired = False
        self._model_usage: dict[str, int] = {}
        self._nvml_initialized = False
        self._last_sample: int | None = None

    @property
    def usage_bytes(self) -> int | None:
        """Latest sampled device-0 usage in bytes, or ``None`` if no sample yet.

        ``None`` before the first successful poll and after ``stop()``.
        """
        return self._last_sample

    # --- Public lifecycle ---

    async def start(self) -> None:
        if self._running:
            logger.warning("VRAMMonitor already running")
            return

        if self._vram_limit_bytes is None:
            result = resolve_vram_limit()
            self._vram_limit_bytes = result.limit_bytes

        if self._vram_limit_bytes is None:
            logger.warning(
                "No VRAM limit configured; monitor will poll but not enforce thresholds"
            )

        self._init_nvml()

        self._running = True
        self._threshold_fired = False
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "VRAMMonitor started: interval=%.1f ms, limit=%s bytes",
            self._interval_s * 1000,
            self._vram_limit_bytes,
        )

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._last_sample = None
        self._shutdown_nvml()
        logger.info("VRAMMonitor stopped")

    # --- Model tracking ---

    async def report_model_freed(self, model_name: str, freed_bytes: int) -> None:
        bus = get_event_bus()
        remaining = self._model_usage.get(model_name, 0) - freed_bytes
        remaining = max(remaining, 0)
        if model_name in self._model_usage:
            del self._model_usage[model_name]

        payload = VRAMFreed(
            model_name=model_name,
            freed_bytes=freed_bytes,
            remaining_bytes=remaining,
        )
        envelope = EventEnvelope.create(EVENT_VRAM_FREED, bus._next_seq(), payload)
        await bus.publish(envelope)
        logger.info("Model %s freed %d bytes (remaining: %d)", model_name, freed_bytes, remaining)

    def report_model_loaded(self, model_name: str, used_bytes: int) -> None:
        self._model_usage[model_name] = used_bytes
        logger.info("Model %s loaded: %d bytes", model_name, used_bytes)

    # --- Internal polling ---

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in poll loop")
            await asyncio.sleep(self._interval_s)

    async def _poll_cycle(self) -> None:
        mem_info = self._query_gpu_memory()
        if mem_info is None:
            return

        used_bytes, free_bytes, gpu_index = mem_info
        self._last_sample = used_bytes
        logger.debug(
            "VRAM poll: used=%d free=%d gpu=%d limit=%s",
            used_bytes,
            free_bytes,
            gpu_index,
            self._vram_limit_bytes,
        )

        if self._vram_limit_bytes is None:
            return

        threshold = self._vram_limit_bytes - SAFETY_MARGIN_BYTES

        if used_bytes > threshold:
            if not self._threshold_fired:
                payload = VRAMThresholdExceeded(
                    used_bytes=used_bytes,
                    limit_bytes=self._vram_limit_bytes,
                    gpu_index=gpu_index,
                )
                bus = get_event_bus()
                envelope = EventEnvelope.create(
                    EVENT_VRAM_THRESHOLD_EXCEEDED, bus._next_seq(), payload
                )
                await bus.publish(envelope)
                self._threshold_fired = True
                logger.warning(
                    "VRAM threshold exceeded: used=%d threshold=%d", used_bytes, threshold
                )
        else:
            self._threshold_fired = False

    def _init_nvml(self) -> None:
        if self._nvml_initialized:
            return
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml_initialized = True
            logger.debug("NVML initialized")
        except Exception as exc:  # noqa: BLE001 - optional best-effort dependency
            logger.warning("pynvml init failed: %s", exc)

    def _shutdown_nvml(self) -> None:
        if not self._nvml_initialized:
            return
        try:
            import pynvml
            pynvml.nvmlShutdown()
            self._nvml_initialized = False
            logger.debug("NVML shutdown")
        except Exception as exc:  # noqa: BLE001 - optional best-effort dependency
            logger.warning("pynvml shutdown failed: %s", exc)

    def _query_gpu_memory(self) -> tuple[int, int, int] | None:
        if not self._nvml_initialized:
            return None

        try:
            import pynvml
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return (info.used, info.free, 0)
        except Exception as exc:  # noqa: BLE001 - optional best-effort dependency
            logger.warning("pynvml query failed: %s — skipping poll cycle", exc)
            return None
