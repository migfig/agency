from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_VRAM_FREED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
    VRAMFreed,
    VRAMThresholdExceeded,
)
from agency.resource_manager.vram_monitor import SAFETY_MARGIN_BYTES, VRAMMonitor


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()
    yield
    reset_event_bus()


@pytest.fixture
def bus() -> EventBus:
    b = EventBus()
    with patch("agency.resource_manager.vram_monitor.get_event_bus", return_value=b):
        yield b


class TestPollInterval:
    @pytest.mark.asyncio
    async def test_polls_at_configured_interval(self, bus: EventBus):
        monitor = VRAMMonitor(interval_ms=10)
        call_times: list[float] = []

        mock_info = MagicMock()
        mock_info.used = 1000
        mock_info.free = 9000
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            monitor._running = True
            for _ in range(3):
                await monitor._poll_cycle()
                call_times.append(asyncio.get_event_loop().time())
                await asyncio.sleep(0.015)

            assert len(call_times) == 3


class TestThresholdCrossing:
    @pytest.mark.asyncio
    async def test_publishes_threshold_exceeded_event(self, bus: EventBus):
        limit = 8 * 1024 * 1024 * 1024  # 8 GB GPU

        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, handler)

        monitor = VRAMMonitor(vram_limit_bytes=limit)

        # threshold = limit - 512MB = ~7.5GB, so 7.9GB exceeds it
        used = int(7.9 * 1024 * 1024 * 1024)
        mock_info = MagicMock()
        mock_info.used = used
        mock_info.free = limit - used
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            monitor._init_nvml()
            await monitor._poll_cycle()

        assert len(received) == 1
        payload = received[0].payload
        assert isinstance(payload, VRAMThresholdExceeded)
        assert payload.used_bytes == used
        assert payload.limit_bytes == limit

    @pytest.mark.asyncio
    async def test_no_duplicate_within_same_interval(self, bus: EventBus):
        limit = 10_000_000
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, handler)

        monitor = VRAMMonitor(vram_limit_bytes=limit)

        mock_info = MagicMock()
        mock_info.used = limit
        mock_info.free = 0
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            monitor._init_nvml()
            await monitor._poll_cycle()
            await monitor._poll_cycle()

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_no_event_below_threshold(self, bus: EventBus):
        limit = 8 * 1024 * 1024 * 1024  # 8 GB — realistic GPU size
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, handler)

        monitor = VRAMMonitor(vram_limit_bytes=limit)

        mock_info = MagicMock()
        mock_info.used = 1 * 1024 * 1024 * 1024  # 1 GB — well below threshold
        mock_info.free = 7 * 1024 * 1024 * 1024
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            await monitor._poll_cycle()

        assert len(received) == 0


class TestReportModelFreed:
    @pytest.mark.asyncio
    async def test_publishes_vram_freed_event(self, bus: EventBus):
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_FREED, handler)

        monitor = VRAMMonitor()
        monitor.report_model_loaded("llama-7b", 4_000_000)
        await monitor.report_model_freed("llama-7b", 2_000_000)

        assert len(received) == 1
        payload = received[0].payload
        assert isinstance(payload, VRAMFreed)
        assert payload.model_name == "llama-7b"
        assert payload.freed_bytes == 2_000_000
        assert payload.remaining_bytes == 2_000_000

    @pytest.mark.asyncio
    async def test_safety_margin_exactly_512mb(self, bus: EventBus):
        limit = 8 * 1024 * 1024 * 1024  # 8 GB
        threshold = limit - SAFETY_MARGIN_BYTES

        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, handler)

        monitor = VRAMMonitor(vram_limit_bytes=limit)

        # Used bytes exactly at threshold should NOT trigger event
        mock_info = MagicMock()
        mock_info.used = threshold
        mock_info.free = limit - threshold
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            monitor._init_nvml()
            await monitor._poll_cycle()

        assert len(received) == 0

        # Used bytes just above threshold SHOULD trigger event
        mock_info.used = threshold + 1

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            await monitor._poll_cycle()

        assert len(received) == 1
        payload = received[0].payload
        assert isinstance(payload, VRAMThresholdExceeded)


class TestIntegration:
    @pytest.mark.asyncio
    async def test_unmocked_event_bus_communication(self):
        """Verify event bus publish/subscribe works without mocking."""
        bus = EventBus()
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, handler)

        payload = VRAMThresholdExceeded(
            used_bytes=1_000_000,
            limit_bytes=2_000_000,
            gpu_index=0,
        )
        envelope = EventEnvelope.create(EVENT_VRAM_THRESHOLD_EXCEEDED, bus._next_seq(), payload)
        await bus.publish(envelope)

        assert len(received) == 1
        assert received[0].payload is payload
        assert received[0].event_type == EVENT_VRAM_THRESHOLD_EXCEEDED

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_publish(self):
        """Verify one failing handler doesn't prevent other handlers from running."""
        bus = EventBus()

        async def bad_handler(event: EventEnvelope) -> None:
            raise RuntimeError("handler crash")

        good_results: list[EventEnvelope] = []

        async def good_handler(event: EventEnvelope) -> None:
            good_results.append(event)

        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, bad_handler)
        await bus.subscribe(EVENT_VRAM_THRESHOLD_EXCEEDED, good_handler)

        payload = VRAMThresholdExceeded(
            used_bytes=1_000_000,
            limit_bytes=2_000_000,
            gpu_index=0,
        )
        envelope = EventEnvelope.create(EVENT_VRAM_THRESHOLD_EXCEEDED, bus._next_seq(), payload)
        await bus.publish(envelope)

        assert len(good_results) == 1


class TestPynvmlFailure:
    @pytest.mark.asyncio
    async def test_graceful_degradation_import_error(self, bus: EventBus):
        monitor = VRAMMonitor(vram_limit_bytes=10_000_000)
        result = monitor._query_gpu_memory()
        assert result is None

    @pytest.mark.asyncio
    async def test_graceful_degradation_nvml_error(self, bus: EventBus):
        monitor = VRAMMonitor(vram_limit_bytes=10_000_000)
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlInit.side_effect = Exception("GPU not found")

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            result = monitor._query_gpu_memory()

        assert result is None


class TestStopCleanup:
    @pytest.mark.asyncio
    async def test_stop_cancels_poll_task(self, bus: EventBus):
        mock_info = MagicMock()
        mock_info.used = 100
        mock_info.free = 900
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        monitor = VRAMMonitor(interval_ms=10, vram_limit_bytes=None)

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            await monitor.start()
            assert monitor._running is True
            assert monitor._poll_task is not None

            await asyncio.sleep(0.05)
            await monitor.stop()

        assert monitor._running is False
        assert monitor._poll_task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, bus: EventBus):
        monitor = VRAMMonitor()
        await monitor.stop()


class TestStartStopLifecycle:
    @pytest.mark.asyncio
    async def test_duplicate_start_logs_warning(self, bus: EventBus):
        monitor = VRAMMonitor(vram_limit_bytes=None)
        with patch("agency.resource_manager.vram_monitor.resolve_vram_limit"):
            await monitor.start()
            await monitor.start()
        await monitor.stop()


class TestUsageBytes:
    """Read-only ``usage_bytes``: None -> latest sample -> None after stop."""

    @pytest.mark.asyncio
    async def test_none_before_first_sample(self, bus: EventBus):
        monitor = VRAMMonitor(vram_limit_bytes=10_000_000)

        assert monitor.usage_bytes is None

    @pytest.mark.asyncio
    async def test_tracks_latest_poll_sample(self, bus: EventBus):
        monitor = VRAMMonitor(vram_limit_bytes=10_000_000)
        mock_info = MagicMock()
        mock_info.used = 4_242
        mock_info.free = 100
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            monitor._init_nvml()
            await monitor._poll_cycle()

        assert monitor.usage_bytes == 4_242

        mock_info.used = 4_343
        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            await monitor._poll_cycle()

        assert monitor.usage_bytes == 4_343

    @pytest.mark.asyncio
    async def test_none_after_stop(self, bus: EventBus):
        mock_info = MagicMock()
        mock_info.used = 100
        mock_info.free = 900
        mock_pynvml = MagicMock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info

        monitor = VRAMMonitor(interval_ms=10, vram_limit_bytes=None)

        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            await monitor.start()
            for _ in range(50):
                if monitor.usage_bytes is not None:
                    break
                await asyncio.sleep(0.01)
            assert monitor.usage_bytes is not None
            await monitor.stop()

        assert monitor.usage_bytes is None
