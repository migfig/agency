"""Tests for llama.cpp backend subprocess management."""

from __future__ import annotations

import signal
import subprocess
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from agency.core.llamacpp_backend import (
    LlamaCppError,
    LlamaCppServer,
    ServerInfo,
)

# --- ServerInfo tests ---


class TestServerInfo:
    def test_health_endpoint(self) -> None:
        info = ServerInfo(port=8080, model_path="/models/m.gguf", pid=1234)
        assert info.health_endpoint == "http://127.0.0.1:8080/"

    def test_dataclass_fields(self) -> None:
        info = ServerInfo(port=9000, model_path="/m.gguf", pid=999)
        d = asdict(info)
        assert d["port"] == 9000
        assert d["model_path"] == "/m.gguf"
        assert d["pid"] == 999


# --- Constructor & config tests ---


class TestLlamaCppServerInit:
    def test_defaults(self) -> None:
        srv = LlamaCppServer()
        assert srv._server_binary == "llama-server"
        assert srv._model_path == ""
        assert srv._port == 8080

    @patch.dict("os.environ", {"LLAMA_CPP_PORT": "9999", "LLAMA_CPP_MODEL_PATH": "/env-model.gguf"})
    def test_env_vars(self) -> None:
        srv = LlamaCppServer()
        assert srv._port == 9999
        assert srv._model_path == "/env-model.gguf"

    def test_constructor_overrides_env(self) -> None:
        with patch.dict("os.environ", {"LLAMA_CPP_PORT": "9999"}):
            srv = LlamaCppServer(port=8080)
            assert srv._port == 8080


# --- is_running tests ---


class TestIsRunning:
    def test_false_when_no_process(self) -> None:
        srv = LlamaCppServer()
        assert not srv.is_running

    def test_true_when_alive(self) -> None:
        srv = LlamaCppServer()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        srv._process = mock_proc
        assert srv.is_running

    def test_false_when_dead(self) -> None:
        srv = LlamaCppServer()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        srv._process = mock_proc
        assert not srv.is_running


# --- start() tests ---


class TestStart:
    @patch("agency.core.llamacpp_backend.LlamaCppServer._health_check", return_value=True)
    @patch("subprocess.Popen")
    @patch("agency.core.llamacpp_backend.Path.exists", return_value=True)
    def test_happy_path(self, mock_exists: MagicMock, mock_popen: MagicMock, _check: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        srv = LlamaCppServer(
            llama_server_path="/bin/llama-server",
            model_path="/models/m.gguf",
            port=8080,
        )
        info = srv.start()

        assert isinstance(info, ServerInfo)
        assert info.port == 8080
        assert info.model_path == "/models/m.gguf"
        assert info.pid == 42
        mock_popen.assert_called_once_with(
            ["/bin/llama-server", "--model", "/models/m.gguf", "--port", "8080", "--host", "127.0.0.1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @patch("agency.core.llamacpp_backend.LlamaCppServer._health_check", return_value=False)
    @patch("subprocess.Popen")
    @patch("agency.core.llamacpp_backend.Path.exists", return_value=True)
    @patch("time.sleep")
    def test_health_check_timeout_kills_process(self, _sleep: MagicMock, mock_exists: MagicMock, mock_popen: MagicMock, _check: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 10
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        srv = LlamaCppServer(
            llama_server_path="/bin/llama-server",
            model_path="/models/m.gguf",
            startup_timeout=0.5,
        )
        with pytest.raises(LlamaCppError, match="Health-check failed"):
            srv.start()
        mock_proc.kill.assert_called_once()

    def test_binary_not_found(self) -> None:
        srv = LlamaCppServer(
            llama_server_path="/nonexistent/binary",
            model_path="/models/m.gguf",
        )
        with pytest.raises(LlamaCppError, match="Server binary not found"):
            srv.start()

    def test_model_path_not_set(self) -> None:
        srv = LlamaCppServer(model_path="", port=8080)
        with pytest.raises(LlamaCppError, match="Model path not set"):
            srv.start()

    @patch("agency.core.llamacpp_backend.LlamaCppServer._health_check", return_value=True)
    @patch("subprocess.Popen")
    @patch("agency.core.llamacpp_backend.Path.exists", return_value=True)
    def test_duplicate_start_raises(self, mock_exists: MagicMock, mock_popen: MagicMock, _check: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        srv = LlamaCppServer(
            llama_server_path="/bin/llama-server",
            model_path="/models/m.gguf",
        )
        srv.start()
        with pytest.raises(LlamaCppError, match="already running"):
            srv.start()


# --- _health_check tests ---


class TestHealthCheck:
    @patch("urllib.request.urlopen")
    def test_returns_true_on_200(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        srv = LlamaCppServer(port=8080)
        assert srv._health_check(timeout=1.0) is True

    @patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused"))
    def test_returns_false_on_timeout(self, _mock: MagicMock) -> None:
        srv = LlamaCppServer(port=8080)
        assert srv._health_check(timeout=0.1) is False


# --- shutdown() tests ---


class TestShutdown:
    @patch("agency.core.llamacpp_backend.LlamaCppServer.is_running", new_callable=lambda: property(lambda self: True))
    def test_clean_shutdown(self, _prop: MagicMock) -> None:
        srv = LlamaCppServer(shutdown_timeout=1.0)
        mock_proc = MagicMock()
        poll_count = [0]

        def side_effect_poll() -> int | None:
            poll_count[0] += 1
            if poll_count[0] >= 2:
                return 0
            return None

        mock_proc.poll.side_effect = side_effect_poll
        srv._process = mock_proc

        srv.shutdown()
        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        assert srv._process is None

    def test_idempotent_shutdown_when_not_running(self) -> None:
        srv = LlamaCppServer()
        srv._process = None
        srv.shutdown()  # Should not raise

    def test_sigkill_fallback(self) -> None:
        srv = LlamaCppServer(shutdown_timeout=0.01)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process never dies on SIGTERM
        mock_proc.pid = 999
        srv._process = mock_proc

        with patch("time.sleep", side_effect=lambda x: None):
            srv.shutdown()

        mock_proc.send_signal.assert_called_with(signal.SIGTERM)
        mock_proc.kill.assert_called_once()


# --- context manager tests ---


class TestContextManager:
    @patch("agency.core.llamacpp_backend.LlamaCppServer.start")
    @patch("agency.core.llamacpp_backend.LlamaCppServer.shutdown")
    def test_enter_exit_calls(self, mock_shutdown: MagicMock, mock_start: MagicMock) -> None:
        with LlamaCppServer(model_path="/m.gguf"):
            pass
        mock_start.assert_called_once()
        mock_shutdown.assert_called_once()
