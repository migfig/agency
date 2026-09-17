"""llama.cpp server subprocess lifecycle management."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from typing_extensions import Self

logger = logging.getLogger(__name__)


@dataclass
class ServerInfo:
    """Information about a running llama.cpp server instance."""
    port: int
    model_path: str
    pid: int
    health_endpoint: str = field(init=False)

    def __post_init__(self) -> None:
        self.health_endpoint = f"http://127.0.0.1:{self.port}/"


class LlamaCppError(Exception):
    """Raised when llama.cpp server operations fail."""


class LlamaCppServer:
    """Manages the llama.cpp inference server subprocess lifecycle.

    Supports configurable model path and port, HTTP health-checks on start,
    and graceful shutdown (SIGTERM → 10s wait → SIGKILL fallback).
    Can be used as a context manager.
    """

    def __init__(
        self,
        llama_server_path: str | None = None,
        model_path: str | None = None,
        port: int | None = None,
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 10.0,
    ) -> None:
        self._server_binary = llama_server_path or os.environ.get("LLAMA_CPP_SERVER_PATH", "llama-server")
        self._model_path = model_path or os.environ.get("LLAMA_CPP_MODEL_PATH", "")
        self._port = port or int(os.environ.get("LLAMA_CPP_PORT", "8080"))
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout

        self._process: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        """Return True if the server subprocess is alive."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def start(self) -> ServerInfo:
        """Start the llama.cpp server and wait for health-check to pass.

        Raises:
            LlamaCppError: If binary is missing, model is missing, or health-check times out.
        """
        if self.is_running:
            raise LlamaCppError("Server already running")

        if not Path(self._server_binary).exists() and self._server_binary not in ("llama-server",):
            raise LlamaCppError(f"Server binary not found: {self._server_binary}")

        if not self._model_path:
            raise LlamaCppError(
                "Model path not set. Provide model_path or set LLAMA_CPP_MODEL_PATH env var."
            )

        cmd = [
            self._server_binary,
            "--model", self._model_path,
            "--port", str(self._port),
            "--host", "127.0.0.1",
        ]

        logger.info("Starting llama.cpp server: %s", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if not self._health_check(self._startup_timeout):
            self._kill_process()
            raise LlamaCppError(
                f"Health-check failed after {self._startup_timeout}s "
                f"(port={self._port}, model={self._model_path})"
            )

        info = ServerInfo(port=self._port, model_path=self._model_path, pid=self._process.pid)
        logger.info("Server started: %s", info.health_endpoint)
        return info

    def _health_check(self, timeout: float) -> bool:
        """Probe the server health endpoint until it responds or timeout expires."""
        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self._port}/health",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except OSError as exc:
                logger.debug("Health probe not up yet: %s", exc)
            __import__("time").sleep(0.5)
        return False

    def shutdown(self) -> None:
        """Gracefully shut down the server. SIGTERM first, SIGKILL after timeout."""
        if not self.is_running:
            logger.info("Server not running; skipping shutdown")
            return

        logger.info("Sending SIGTERM to PID %d", self._process.pid)
        try:
            self._process.send_signal(signal.SIGTERM)
        except OSError:
            pass

        import time as _time
        deadline = _time.time() + self._shutdown_timeout
        while _time.time() < deadline:
            if self._process.poll() is not None:
                logger.info("Server exited cleanly (PID %d)", self._process.pid)
                self._process = None
                return
            _time.sleep(0.2)

        logger.warning("Shutdown timeout reached, sending SIGKILL to PID %d", self._process.pid)
        self._kill_process()

    def _kill_process(self) -> None:
        """Force-kill the server subprocess."""
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.debug(
                    "Forced kill of PID %s did not complete: %s",
                    self._process.pid,
                    exc,
                )
        self._process = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()
