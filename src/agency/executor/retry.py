"""Retry policy helpers and failure handler (Story 4.4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from agency.core.event_bus import EventBus, UnsubscribeToken, get_event_bus
from agency.core.events import (
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
    EventEnvelope,
    NodeFailed,
    NodeRetrying,
)
from agency.yaml_engine.schema import RetryPolicy, Workflow

logger = logging.getLogger(__name__)


def get_retry_policy(workflow: Workflow, node_id: str) -> RetryPolicy | None:
    """Return the retry policy for *node_id*, or ``None`` if not configured."""
    from agency.yaml_engine.schema import AgentNode, ToolCallNode

    node = workflow.nodes.get(node_id)
    if node is None:
        return None
    if isinstance(node, (AgentNode, ToolCallNode)):
        return node.retry
    return None


def is_terminal_failure(policy: RetryPolicy | None, attempt: int) -> bool:
    """A failure is terminal when there is no policy or the attempt budget is exhausted."""
    return policy is None or attempt >= policy.max_attempts


def get_fallback(workflow: Workflow, node_id: str) -> str | None:
    """Return the fallback agent id for *node_id*, or ``None`` if not configured."""
    from agency.yaml_engine.schema import AgentNode, ToolCallNode

    node = workflow.nodes.get(node_id)
    if node is None:
        return None
    if isinstance(node, (AgentNode, ToolCallNode)):
        return node.fallback
    return None


def is_final_failure(
    policy: RetryPolicy | None,
    attempt: int,
    *,
    fallback_configured: bool,
    fallback_failed: bool,
) -> bool:
    """A failure is final when it is terminal and no fallback can still take over.

    *fallback_failed* means the failing attempt ``NodeFailed.fallback`` is set —
    i.e. this is already the fallback agent's own failure.
    """
    return is_terminal_failure(policy, attempt) and (fallback_failed or not fallback_configured)


def exponential_delay(policy: RetryPolicy, attempt: int) -> float:
    """Deterministic backoff after failed *attempt* (1-indexed).

    ``min(base_delay_seconds * 2**(attempt-1), max_delay_seconds or inf)``.
    """
    delay = policy.base_delay_seconds * (2 ** (attempt - 1))
    if policy.max_delay_seconds is not None:
        delay = min(delay, policy.max_delay_seconds)
    return delay


ExecuteFn = Callable[[str, int], Coroutine[Any, Any, None]]


class RetrySupervisor:
    """Subscribes to ``EVENT_NODE_FAILED``; retries non-terminal failures.

    On a non-terminal failure it publishes ``NodeRetrying``, sleeps the
    backoff delay, then calls *execute* to re-run the node with the next
    attempt number. Only one retry per node may be in flight at a time.
    """

    def __init__(
        self,
        workflow: Workflow,
        run_id: str,
        bus: EventBus | None = None,
        execute: ExecuteFn | None = None,
        sleep: Callable[[float], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._workflow = workflow
        self._run_id = run_id
        self._bus = bus if bus is not None else get_event_bus()
        self._execute = execute
        self._sleep = sleep if sleep is not None else asyncio.sleep
        # A retry is dispatched at most once per (node, attempt). Keying on the
        # attempt number (not just the node id) lets a retried attempt's own
        # failure — published nested inside execute — re-enter and dispatch the
        # next attempt, while a literal duplicate of an already-dispatched
        # (node, attempt) is suppressed.
        self._dispatched: set[tuple[str, int]] = set()
        self._token: UnsubscribeToken | None = None

    async def start(self) -> None:
        """Subscribe to ``EVENT_NODE_FAILED``."""
        self._token = await self._bus.subscribe(EVENT_NODE_FAILED, self._on_node_failed)

    async def close(self) -> None:
        """Unsubscribe and forget any dispatched retries."""
        if self._token is not None:
            self._token.cancel()
            self._token = None
        self._dispatched.clear()

    async def _on_node_failed(self, envelope: EventEnvelope) -> None:
        payload: NodeFailed = envelope.payload
        if getattr(payload, "run_id", None) != self._run_id:
            return
        node_id = payload.node_id
        policy = get_retry_policy(self._workflow, node_id)
        if is_terminal_failure(policy, payload.attempt):
            return
        key = (node_id, payload.attempt)
        if key in self._dispatched:
            logger.debug(
                "retry already dispatched for node '%s' attempt %d, ignoring",
                node_id,
                payload.attempt,
            )
            return
        if self._execute is None:
            return
        self._dispatched.add(key)
        delay = exponential_delay(policy, payload.attempt)
        next_attempt = payload.attempt + 1
        try:
            await self._bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_RETRYING,
                    self._bus._next_seq(),
                    NodeRetrying(
                        node_id=node_id,
                        run_id=payload.run_id,
                        model=payload.model,
                        error=payload.error,
                        attempt=payload.attempt,
                        next_attempt=next_attempt,
                        delay_seconds=delay,
                    ),
                )
            )
            await self._sleep(delay)
            await self._execute(node_id, next_attempt)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.warning("retry supervisor error for node '%s': %s", node_id, exc)
