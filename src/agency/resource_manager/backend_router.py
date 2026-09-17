from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .model_registry import ModelRegistry


class RoutingError(Exception):
    """Raised when inference routing fails (unknown model or unreachable endpoint)."""


def _validate_endpoint(endpoint: str) -> None:
    """Raise ``ValueError`` if *endpoint* is not a well-formed HTTP(S) URL."""
    stripped = endpoint.strip()
    if not stripped.startswith(("http://", "https://")):
        raise ValueError(
            f"Endpoint must be an HTTP(S) URL, got '{endpoint}'"
        )


def _write_log(log_path: Path, entry: dict[str, object]) -> None:
    """Append a single JSON line to *log_path*, creating parent dirs if needed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


class BackendRouter:
    """Routes inference requests to per-model llama.cpp endpoints.

    Each model name is registered with a base endpoint URL.  When
    ``route_inference`` is called the router looks up the endpoint,
   POSTs to ``/v1/chat/completions``, logs the call, and returns the
    JSON response body as a Python dict.
    """

    def __init__(self, log_dir: Path = Path("runs")) -> None:
        self._registry = ModelRegistry()
        self._log_dir = log_dir

    # -- public API -------------------------------------------------------

    def register(self, model: str, endpoint: str) -> None:
        """Register *model* with its inference *endpoint*.

        Raises ``ValueError`` on duplicate registration or malformed URL.
        """
        _validate_endpoint(endpoint)
        self._registry.register(model, endpoint.strip())

    async def route_inference(
        self,
        *,
        model: str,
        messages: list[dict],
        run_id: str,
        node_id: str,
        timeout: float = 600.0,
        **kwargs,
    ) -> dict:
        """Route an inference request to the endpoint registered for *model*.

        Args:
            model: Model identifier matching ``AgentNode.model``.
            messages: Chat messages list (OpenAI-compatible format).
            run_id: Identifier for the current workflow execution.
            node_id: Identifier of the agent node making this call.
            timeout: HTTP request timeout in seconds.
            **kwargs: Extra keys forwarded into the request body
                (e.g. ``temperature``, ``max_tokens``).

        Returns:
            Parsed JSON response body from llama.cpp as a dict.

        Raises:
            RoutingError: If *model* is not registered or the endpoint
                is unreachable.
        """
        endpoint = self._registry.lookup(model)
        if endpoint is None:
            raise RoutingError(f"Model '{model}' not registered")

        log_path = self._log_dir / run_id / "inference.log"
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        body = {"messages": messages, **kwargs}

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body)
                latency_ms = round((time.monotonic() - start) * 1000, 2)

                response.raise_for_status()
                result = response.json()

                _write_log(log_path, {
                    "run_id": run_id,
                    "node_id": node_id,
                    "model": model,
                    "endpoint": endpoint,
                    "status": "success",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "latency_ms": latency_ms,
                })
                return result

        except (httpx.HTTPError, httpx.ConnectError, OSError) as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            _write_log(log_path, {
                "run_id": run_id,
                "node_id": node_id,
                "model": model,
                "endpoint": endpoint,
                "status": "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "latency_ms": latency_ms,
                "error": str(exc),
            })
            raise RoutingError(
                f"Inference failed for model '{model}': {exc}"
            ) from exc
