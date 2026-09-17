"""Tests for BackendRouter model-to-endpoint routing and inference dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agency.resource_manager.backend_router import BackendRouter, RoutingError

# --- AC1: Model-to-endpoint routing -------------------------------------


class TestRouteToCorrectEndpoint:
    @pytest.mark.parametrize("model,port", [("model-a", "8080"), ("model-b", "8081")])
    @pytest.mark.asyncio
    async def test_route_to_correct_endpoint(self, model: str, port: int) -> None:
        router = BackendRouter(log_dir=Path("/tmp/test-runs"))
        endpoint = f"http://localhost:{port}"
        router.register(model, endpoint)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

        with patch("httpx.AsyncClient", autospec=True) as MockClient:
            mock_client = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await router.route_inference(
                model=model,
                messages=[{"role": "user", "content": "hello"}],
                run_id="r1",
                node_id="n1",
            )

            expected_url = f"http://localhost:{port}/v1/chat/completions"
            mock_client.post.assert_called_once()
            assert mock_client.post.call_args[0][0] == expected_url


# --- AC2: Unreachable model detection -----------------------------------


class TestUnreachableModel:
    @pytest.mark.asyncio
    async def test_unreachable_model_raises_routing_error(self) -> None:
        router = BackendRouter(log_dir=Path("/tmp/test-runs"))
        router.register("model-x", "http://localhost:9999")

        with patch("httpx.AsyncClient", autospec=True) as MockClient:
            mock_client = MagicMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with pytest.raises(RoutingError, match="Inference failed for model 'model-x'"):
                await router.route_inference(
                    model="model-x",
                    messages=[{"role": "user", "content": "hello"}],
                    run_id="r1",
                    node_id="n1",
                )

    @pytest.mark.asyncio
    async def test_routing_error_includes_model_name(self) -> None:
        router = BackendRouter(log_dir=Path("/tmp/test-runs"))
        router.register("my-model", "http://localhost:7777")

        with patch("httpx.AsyncClient", autospec=True) as MockClient:
            mock_client = MagicMock()
            mock_client.post = AsyncMock(side_effect=httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=MagicMock(),
                response=MagicMock(status_code=503),
            ))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with pytest.raises(RoutingError, match="my-model"):
                await router.route_inference(
                    model="my-model",
                    messages=[{"role": "user", "content": "test"}],
                    run_id="r1",
                    node_id="n1",
                )


# --- AC4: Unknown model handling ----------------------------------------


class TestUnknownModel:
    @pytest.mark.asyncio
    async def test_unknown_model_raises_routing_error(self) -> None:
        router = BackendRouter(log_dir=Path("/tmp/test-runs"))
        router.register("model-a", "http://localhost:8080")

        with pytest.raises(RoutingError, match="Model 'unknown' not registered"):
            await router.route_inference(
                model="unknown",
                messages=[{"role": "user", "content": "hello"}],
                run_id="r1",
                node_id="n1",
            )


# --- AC3: Structured JSON logging ---------------------------------------


class TestStructuredLogging:
    @pytest.mark.asyncio
    async def test_log_entry_written_on_success(self, tmp_path: Path) -> None:
        router = BackendRouter(log_dir=tmp_path)
        router.register("m1", "http://localhost:8080")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": []}

        with patch("httpx.AsyncClient", autospec=True) as MockClient:
            mock_client = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await router.route_inference(
                model="m1",
                messages=[{"role": "user", "content": "hi"}],
                run_id="r-test",
                node_id="n-agentA",
            )

        log_file = tmp_path / "r-test" / "inference.log"
        assert log_file.exists()
        line = log_file.read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["run_id"] == "r-test"
        assert entry["node_id"] == "n-agentA"
        assert entry["model"] == "m1"
        assert entry["status"] == "success"

    @pytest.mark.asyncio
    async def test_log_entry_written_on_failure(self, tmp_path: Path) -> None:
        router = BackendRouter(log_dir=tmp_path)
        router.register("m2", "http://localhost:9999")

        with patch("httpx.AsyncClient", autospec=True) as MockClient:
            mock_client = MagicMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with pytest.raises(RoutingError):
                await router.route_inference(
                    model="m2",
                    messages=[{"role": "user", "content": "hi"}],
                    run_id="r-fail",
                    node_id="n-agentB",
                )

        log_file = tmp_path / "r-fail" / "inference.log"
        assert log_file.exists()
        line = log_file.read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["status"] == "error"
        assert "error" in entry

    def test_log_contains_required_fields(self, tmp_path: Path) -> None:
        required = {"run_id", "node_id", "model", "endpoint", "status", "timestamp", "latency_ms"}
        entry = {k: "v" for k in required}
        log_file = tmp_path / "r-check" / "inference.log"

        from agency.resource_manager.backend_router import _write_log
        _write_log(log_file, entry)

        assert log_file.exists()
        parsed = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert required.issubset(parsed.keys())


# --- Registration edge cases --------------------------------------------


class TestRegistration:
    def test_duplicate_registration_raises(self) -> None:
        router = BackendRouter()
        router.register("m1", "http://localhost:8080")
        with pytest.raises(ValueError, match="already registered"):
            router.register("m1", "http://localhost:8081")

    def test_invalid_endpoint_raises(self) -> None:
        router = BackendRouter()
        with pytest.raises(ValueError, match="HTTP"):
            router.register("m1", "not-a-url")

    def test_whitespace_stripped(self) -> None:
        router = BackendRouter()
        router.register("m1", "  http://localhost:8080  ")
        assert router._registry.lookup("m1") == "http://localhost:8080"


import httpx
