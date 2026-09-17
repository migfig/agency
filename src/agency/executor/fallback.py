"""Fallback activation handler (Story 4.5)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_FALLBACK_ACTIVATED,
    EVENT_NODE_FAILED,
    EventEnvelope,
    FallbackActivated,
    NodeFailed,
)
from agency.executor.retry import get_fallback, get_retry_policy, is_terminal_failure
from agency.yaml_engine.schema import Workflow

logger = logging.getLogger(__name__)


FallbackExecuteFn = Callable[[str, str, int], Coroutine[Any, Any, None]]


class FallbackSupervisor:
    """Subscribes to ``EVENT_NODE_FAILED``; activates the node's fallback agent.

    On a terminal failure of a node that declares ``fallback`` it publishes
    ``FallbackActivated`` and calls ``execute(node_id, fallback, attempt)`` to
    run the replacement attempt. The fallback attempt never retries (``RetrySupervisor``
    bails on terminal failures) and never chains a second fallback
    (``NodeFailed.fallback`` is set on the fallback attempt's own failure).
    Only one fallback per (node, attempt) may be dispatched.
    """

    def __init__(
        self,
        workflow: Workflow,
        run_id: str,
        bus: EventBus | None = None,
        execute: FallbackExecuteFn | None = None,
    ) -> None:
        self._workflow = workflow
        self._run_id = run_id
        self._bus = bus if bus is not None else get_event_bus()
        self._execute = execute
        # A fallback is dispatched at most once per (node, attempt). Keying on
        # the attempt number (not just the node id) matches RetrySupervisor:
        # the fallback attempt's own failure is a different event (its
        # ``fallback`` field is set) and is rejected up front.
        self._dispatched: set[tuple[str, int]] = set()
        self._token: UnsubscribeToken | None = None

    async def start(self) -> None:
        """Subscribe to ``EVENT_NODE_FAILED``."""
        self._token = await self._bus.subscribe(EVENT_NODE_FAILED, self._on_node_failed)

    async def close(self) -> None:
        """Unsubscribe and forget any dispatched fallbacks."""
        if self._token is not None:
            self._token.cancel()
            self._token = None
        self._dispatched.clear()

    async def _on_node_failed(self, envelope: EventEnvelope) -> None:
        payload: NodeFailed = envelope.payload
        if getattr(payload, "run_id", None) != self._run_id:
            return
        node_id = payload.node_id
        fallback = get_fallback(self._workflow, node_id)
        if fallback is None:
            return
        if payload.fallback is not None:
            return
        policy = get_retry_policy(self._workflow, node_id)
        if not is_terminal_failure(policy, payload.attempt):
            return
        key = (node_id, payload.attempt)
        if key in self._dispatched:
            logger.debug(
                "fallback already dispatched for node '%s' attempt %d, ignoring",
                node_id,
                payload.attempt,
            )
            return
        if self._execute is None:
            return
        self._dispatched.add(key)
        try:
            await self._bus.publish(
                EventEnvelope.create(
                    EVENT_FALLBACK_ACTIVATED,
                    self._bus._next_seq(),
                    FallbackActivated(
                        node_id=node_id,
                        run_id=payload.run_id,
                        model=payload.model,
                        fallback=fallback,
                        error=payload.error,
                        attempt=payload.attempt,
                    ),
                )
            )
            await self._execute(node_id, fallback, payload.attempt)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("fallback supervisor error for node '%s': %s", node_id, exc)
