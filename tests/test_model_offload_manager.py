from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agency.core.event_bus import EventBus, reset_event_bus
from agency.core.events import (
    EVENT_MODEL_OFFLOADED,
    EVENT_MODEL_RELOADED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
    ModelOffloaded,
    ModelReloaded,
    VRAMThresholdExceeded,
)
from agency.resource_manager.model_offload_manager import (
    ModelOffloadManager,
    OffloadResult,
)


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()
    yield
    reset_event_bus()


@pytest.fixture
def bus() -> EventBus:
    b = EventBus()
    with patch("agency.resource_manager.model_offload_manager.get_event_bus", return_value=b):
        yield b


@pytest.fixture
def mock_load_mgr():
    mgr = MagicMock()
    mgr._loaded_models = {}
    mgr._queue = []
    return mgr


@pytest.fixture
def mock_router():
    registry = MagicMock()
    registry.get_all.return_value = {
        "model-a": {"endpoint": "http://localhost:8001"},
        "model-b": {"endpoint": "http://localhost:8002"},
    }
    router = MagicMock()
    router._registry = registry
    return router


@pytest.fixture
def offload_manager(mock_load_mgr, mock_router, bus):
    mgr = ModelOffloadManager(
        model_load_manager=mock_load_mgr,
        backend_router=mock_router,
        reload_timeout=5.0,
    )
    return mgr


class TestRecordAccess:
    @pytest.mark.asyncio
    async def test_records_access_timestamp(self, offload_manager):
        await offload_manager.record_access("model-a")
        assert "model-a" in offload_manager._model_last_accessed
        assert isinstance(offload_manager._model_last_accessed["model-a"], float)


class TestSelectLRUCandidate:
    @pytest.mark.asyncio
    async def test_selects_longest_idle_model_with_no_nodes(self, offload_manager):

        offload_manager._model_last_accessed = {
            "model-a": 100.0,
            "model-b": 200.0,
        }
        # Both have zero sharing nodes; model-a is oldest (smallest timestamp)
        offload_manager._load_mgr._loaded_models = {
            "model-a": (1_000_000, set()),
            "model-b": (2_000_000, set()),
        }

        candidate = offload_manager._select_lru_candidate()
        assert candidate is not None
        model_name, endpoint = candidate
        assert model_name == "model-a"
        assert endpoint == "http://localhost:8001"

    @pytest.mark.asyncio
    async def test_skips_models_with_active_nodes(self, offload_manager):
        offload_manager._model_last_accessed = {
            "model-a": 100.0,
            "model-b": 200.0,
        }
        offload_manager._load_mgr._loaded_models = {
            "model-a": (1_000_000, {"node-1"}),
            "model-b": (2_000_000, set()),
        }

        candidate = offload_manager._select_lru_candidate()
        assert candidate is not None
        assert candidate[0] == "model-b"

    @pytest.mark.asyncio
    async def test_skips_offloaded_models(self, offload_manager):
        offload_manager._offloaded_models.add("model-a")
        offload_manager._model_last_accessed = {
            "model-a": 100.0,
            "model-b": 200.0,
        }
        offload_manager._load_mgr._loaded_models = {
            "model-a": (1_000_000, set()),
            "model-b": (2_000_000, set()),
        }

        candidate = offload_manager._select_lru_candidate()
        assert candidate is not None
        assert candidate[0] == "model-b"

    @pytest.mark.asyncio
    async def test_skips_models_with_queued_agents(self, offload_manager):
        queue_entry = MagicMock()
        queue_entry.model_name = "model-a"
        offload_manager._load_mgr._queue = [queue_entry]

        offload_manager._model_last_accessed = {
            "model-a": 100.0,
            "model-b": 200.0,
        }
        offload_manager._load_mgr._loaded_models = {
            "model-a": (1_000_000, set()),
            "model-b": (2_000_000, set()),
        }

        candidate = offload_manager._select_lru_candidate()
        assert candidate is not None
        assert candidate[0] == "model-b"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_candidates(self, offload_manager):
        offload_manager._load_mgr._loaded_models = {}
        candidate = offload_manager._select_lru_candidate()
        assert candidate is None


class TestReloadModel:
    @pytest.mark.asyncio
    async def test_reload_success(self, offload_manager, bus):
        offload_manager._offloaded_models.add("model-a")
        offload_manager._bus = bus

        received_offloaded: list[EventEnvelope] = []
        async def handler_reload(e: EventEnvelope) -> None:
            received_offloaded.append(e)
        await bus.subscribe(EVENT_MODEL_RELOADED, handler_reload)

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_client_post:
            mock_client_post.return_value = await mock_post()
            # Patch the context manager pattern
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=await mock_post())

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await offload_manager.reload_model("model-a", 1_000_000)

        assert result.success is True
        assert result.model_name == "model-a"
        assert "model-a" not in offload_manager._offloaded_models
        assert len(received_offloaded) == 1
        payload = received_offloaded[0].payload
        assert isinstance(payload, ModelReloaded)
        assert payload.model_name == "model-a"

    @pytest.mark.asyncio
    async def test_reload_failure_http_error(self, offload_manager):
        offload_manager._offloaded_models.add("model-a")

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=mock_post)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await offload_manager.reload_model("model-a", 1_000_000)

        assert result.success is False
        assert "http_500" in (result.error or "")
        assert "model-a" in offload_manager._offloaded_models

    @pytest.mark.asyncio
    async def test_reload_skips_if_not_offloaded(self, offload_manager):
        # model-c is not in _offloaded_models
        result = await offload_manager.reload_model("model-c", 1_000_000)
        assert result.success is True
        assert result.latency_ms == 0.0

    @pytest.mark.asyncio
    async def test_reload_failure_no_endpoint(self, offload_manager):
        offload_manager._offloaded_models.add("unknown-model")
        # unknown-model has no endpoint in registry

        result = await offload_manager.reload_model("unknown-model", 1_000_000)
        assert result.success is False
        assert "no endpoint" in (result.error or "")


class TestOffloadOnThreshold:
    @pytest.mark.asyncio
    async def test_triggers_offload_on_threshold_exceeded(self, offload_manager, bus):
        await offload_manager.start()

        received: list[EventEnvelope] = []
        async def handler_offload(e: EventEnvelope) -> None:
            received.append(e)
        await bus.subscribe(EVENT_MODEL_OFFLOADED, handler_offload)

        offload_manager._model_last_accessed = {"model-a": 100.0}
        offload_manager._load_mgr._loaded_models = {
            "model-a": (1_000_000, set()),
        }

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=mock_post)

        with patch("httpx.AsyncClient", return_value=mock_client):
            envelope = EventEnvelope.create(
                EVENT_VRAM_THRESHOLD_EXCEEDED,
                bus._next_seq(),
                VRAMThresholdExceeded(
                    used_bytes=7_000_000,
                    limit_bytes=8_000_000,
                    gpu_index=0,
                ),
            )
            await bus.publish(envelope)
            await asyncio.sleep(0.1)

        assert len(received) == 1
        payload = received[0].payload
        assert isinstance(payload, ModelOffloaded)
        assert payload.model_name == "model-a"
        assert "model-a" in offload_manager._offloaded_models
        await offload_manager.stop()


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_subscribes_to_events(self, offload_manager, bus):
        await offload_manager.start()
        assert offload_manager._bus is not None
        assert offload_manager._token is not None

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self, offload_manager, bus):
        await offload_manager.start()
        await offload_manager.stop()
        assert offload_manager._token is None
        assert offload_manager._bus is None

    @pytest.mark.asyncio
    async def test_duplicate_start_is_noop(self, offload_manager, bus):
        await offload_manager.start()
        await offload_manager.start()
        await offload_manager.stop()


class TestOffloadResult:
    def test_offload_result_dataclass(self):
        result = OffloadResult(
            model_name="model-a",
            success=True,
            latency_ms=150.5,
        )
        assert result.model_name == "model-a"
        assert result.success is True
        assert result.latency_ms == 150.5
        assert result.error is None

    def test_offload_result_with_error(self):
        result = OffloadResult(
            model_name="model-b",
            success=False,
            latency_ms=200.0,
            error="timeout",
        )
        assert result.error == "timeout"
