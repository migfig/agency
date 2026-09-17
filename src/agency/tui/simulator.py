"""Deterministic demo producer over the event bus (Story 3.3)."""

from __future__ import annotations

import asyncio

from agency.core.event_bus import EventBus
from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_STARTED,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
)
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import AgentNode, Workflow


def simulated_tokens(index: int) -> int:
    """Deterministic token count derived from a node's topological index."""
    return 100 * (index + 1)


def simulated_duration(index: int) -> float:
    """Deterministic duration (seconds) from a node's topological index."""
    return 0.5 * (index + 1)


def simulated_output(node_id: str) -> str:
    """Deterministic per-node output text; byte-stable across runs."""
    return f"simulated output for {node_id}"


async def simulate_node_attempt(
    workflow: Workflow,
    event_bus: EventBus,
    run_id: str,
    node_id: str,
    attempt: int,
    fail_schedule: set[str] | None = None,
) -> None:
    """Simulate a single node attempt (used by RetrySupervisor for retries).

    Publishes ``NodeStarted`` then either ``NodeCompleted`` or ``NodeFailed``.
    Nodes listed in *fail_schedule* always fail with a scripted error.
    """
    topo = DAGBuilder(workflow).topological_sort()
    index = topo.index(node_id) if node_id in topo else 0
    node = workflow.nodes[node_id]
    model = node.model if isinstance(node, AgentNode) else None
    node_input = node.prompt_template if isinstance(node, AgentNode) else None
    await event_bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED,
            event_bus._next_seq(),
            NodeStarted(
                node_id=node_id,
                run_id=run_id,
                model=model,
                input=node_input,
                attempt=attempt,
            ),
        )
    )
    await asyncio.sleep(0.1)
    if fail_schedule and node_id in fail_schedule:
        await event_bus.publish(
            EventEnvelope.create(
                EVENT_NODE_FAILED,
                event_bus._next_seq(),
                NodeFailed(
                    node_id=node_id,
                    run_id=run_id,
                    model=model,
                    error=f"scripted failure for {node_id}",
                    attempt=attempt,
                ),
            )
        )
    else:
        await event_bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                event_bus._next_seq(),
                NodeCompleted(
                    node_id=node_id,
                    run_id=run_id,
                    model=model,
                    tokens_used=simulated_tokens(index),
                    duration_seconds=simulated_duration(index),
                    output=simulated_output(node_id),
                ),
            )
        )


async def simulate_fallback_attempt(
    workflow: Workflow,
    event_bus: EventBus,
    run_id: str,
    node_id: str,
    fallback: str,
    attempt: int,
    *,
    fallback_fail_schedule: set[str] | None = None,
) -> None:
    """Simulate the node's fallback-agent replacement attempt.

    Publishes ``NodeStarted`` for the *fallback* model with the primary's
    input, then either ``NodeCompleted`` (tagged ``fallback``) or — when
    *node_id* is in *fallback_fail_schedule* — a scripted
    ``NodeFailed`` carrying the same tag. Metrics stay topo-index-derived so
    the demo replays byte-stably.
    """
    topo = DAGBuilder(workflow).topological_sort()
    index = topo.index(node_id) if node_id in topo else 0
    node = workflow.nodes[node_id]
    node_input = node.prompt_template if isinstance(node, AgentNode) else None
    await event_bus.publish(
        EventEnvelope.create(
            EVENT_NODE_STARTED,
            event_bus._next_seq(),
            NodeStarted(
                node_id=node_id,
                run_id=run_id,
                model=fallback,
                input=node_input,
                attempt=attempt,
            ),
        )
    )
    await asyncio.sleep(0.1)
    if fallback_fail_schedule and node_id in fallback_fail_schedule:
        await event_bus.publish(
            EventEnvelope.create(
                EVENT_NODE_FAILED,
                event_bus._next_seq(),
                NodeFailed(
                    node_id=node_id,
                    run_id=run_id,
                    model=fallback,
                    error=f"scripted fallback failure for {node_id}",
                    attempt=attempt,
                    fallback=fallback,
                ),
            )
        )
    else:
        await event_bus.publish(
            EventEnvelope.create(
                EVENT_NODE_COMPLETED,
                event_bus._next_seq(),
                NodeCompleted(
                    node_id=node_id,
                    run_id=run_id,
                    model=fallback,
                    tokens_used=simulated_tokens(index),
                    duration_seconds=simulated_duration(index),
                    output=simulated_output(node_id),
                    fallback=fallback,
                ),
            )
        )


async def simulate_run(
    workflow: Workflow,
    event_bus: EventBus,
    run_id: str,
    *,
    delay: float = 0.5,
    fail_schedule: set[str] | None = None,
) -> None:
    """Walk the topological order publishing started/completed pairs per node.

    All values are derived from the node's topo index (position in
    topological_sort), never from a wall clock, so the demo replays
    identically every time.

    Nodes listed in *fail_schedule* fail on their first attempt; retries
    are handled by the RetrySupervisor which calls ``simulate_node_attempt``.
    """
    topo = DAGBuilder(workflow).topological_sort()
    for index, node_id in enumerate(topo):
        node = workflow.nodes[node_id]
        model = node.model if isinstance(node, AgentNode) else None
        node_input = node.prompt_template if isinstance(node, AgentNode) else None
        await event_bus.publish(
            EventEnvelope.create(
                EVENT_NODE_STARTED,
                event_bus._next_seq(),
                NodeStarted(
                    node_id=node_id,
                    run_id=run_id,
                    model=model,
                    input=node_input,
                    attempt=1,
                ),
            )
        )
        await asyncio.sleep(delay)
        if fail_schedule and node_id in fail_schedule:
            await event_bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_FAILED,
                    event_bus._next_seq(),
                    NodeFailed(
                        node_id=node_id,
                        run_id=run_id,
                        model=model,
                        error=f"scripted failure for {node_id}",
                        attempt=1,
                    ),
                )
            )
        else:
            await event_bus.publish(
                EventEnvelope.create(
                    EVENT_NODE_COMPLETED,
                    event_bus._next_seq(),
                    NodeCompleted(
                        node_id=node_id,
                        run_id=run_id,
                        model=model,
                        tokens_used=simulated_tokens(index),
                        duration_seconds=simulated_duration(index),
                        output=simulated_output(node_id),
                    ),
                )
            )
