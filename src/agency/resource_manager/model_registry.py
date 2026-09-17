from __future__ import annotations


class ModelRegistry:
    """In-memory registry mapping model names to endpoint URLs."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def register(self, model: str, endpoint: str) -> None:
        """Register a model name with its inference endpoint URL.

        Args:
            model: Model identifier (matches ``AgentNode.model`` from YAML).
            endpoint: Base URL of the running llama.cpp server (e.g. ``http://localhost:8080``).

        Raises:
            ValueError: If *model* is already registered.
        """
        if model in self._map:
            raise ValueError(f"Model '{model}' is already registered")
        self._map[model] = endpoint

    def lookup(self, model: str) -> str | None:
        """Return the endpoint URL for *model*, or ``None`` if unregistered."""
        return self._map.get(model)

    def get_all(self) -> dict[str, str]:
        """Return a shallow copy of all registered model→endpoint mappings."""
        return dict(self._map)
