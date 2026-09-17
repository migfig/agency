from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agency.core.events import (
    EVENT_SUMMARIZATION_TRIGGERED,
    EventEnvelope,
    SummarizationTriggered,
)
from agency.executor.context_store import ContextStore

if TYPE_CHECKING:
    from agency.core.event_bus import EventBus
    from agency.resource_manager.backend_router import BackendRouter

logger = logging.getLogger(__name__)

SUMMARIZER_SYSTEM_PROMPT = (
    "You are the context summarizer for a multi-agent workflow. "
    "Compress the shared phase context into a compact summary that preserves "
    "key facts, decisions, and node outputs. Output only the summary text."
)


def serialize_context(context: dict[str, Any]) -> str:
    """Canonical, deterministic JSON encoding of a phase context."""
    return json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """Estimate a string's token count as ~4 characters per token."""
    return len(text) // 4


class ContextSummarizer:
    """Summarizes a phase's shared context when it approaches the context window.

    The executor calls :meth:`before_agent_run` before each agent starts in a
    phase.  Once the phase context reaches the configured token threshold it
    is replaced with a summary produced by a dedicated model (Architectural
    Invariant #8 — summarization never borrows a workflow agent's model).

    The router is accepted structurally: ``executor/`` must not import
    ``resource_manager/`` at runtime (dependency rule, architecture.md:33),
    so the parameter is type-checked against ``BackendRouter`` only under
    ``TYPE_CHECKING``.
    """

    def __init__(
        self,
        *,
        run_id: str,
        store: ContextStore,
        router: BackendRouter,
        model: str,
        context_window: int,
        threshold_percent: int,
        event_bus: EventBus | None = None,
    ) -> None:
        if not 0 < threshold_percent <= 100:
            raise ValueError(f"threshold_percent must be in (0, 100], got {threshold_percent}")
        if context_window <= 0:
            raise ValueError(f"context_window must be positive, got {context_window}")
        self._run_id = run_id
        self._store = store
        self._router = router
        self._model = model
        self._context_window = context_window
        self._threshold_percent = threshold_percent
        self._event_bus = event_bus
        self._seq = 0

    @property
    def threshold_tokens(self) -> int:
        """Token count at which summarization triggers."""
        return self._context_window * self._threshold_percent // 100

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def before_agent_run(self, phase_id: str) -> bool:
        """Summarize *phase_id*'s context if it has reached the threshold.

        Returns True when the context was replaced with a summary.  Returns
        False (preserving the original context) when the context is empty,
        below the threshold, or when the summarization request fails.  Never
        raises.
        """
        context = self._store.get_context(phase_id)
        if not context:
            return False

        try:
            serialized = serialize_context(context)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.error(
                "Context summarization failed for phase '%s': %s", phase_id, exc
            )
            return False
        tokens_before = estimate_tokens(serialized)
        if tokens_before < self.threshold_tokens:
            return False

        messages = [
            {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
            {"role": "user", "content": serialized},
        ]
        try:
            result = await self._router.route_inference(
                model=self._model,
                messages=messages,
                run_id=self._run_id,
                node_id=f"__summarize__-{phase_id}",
            )
            content = result["choices"][0]["message"]["content"]
            if content is None or not str(content).strip():
                logger.error(
                    "Summarizer returned no content for phase '%s'; "
                    "original context preserved",
                    phase_id,
                )
                return False
            summarized = {"summary": str(content)}
            tokens_after = estimate_tokens(serialize_context(summarized))
            self._store.set_context(phase_id, summarized)
            if self._event_bus is not None:
                await self._event_bus.publish(
                    EventEnvelope.create(
                        EVENT_SUMMARIZATION_TRIGGERED,
                        self._next_seq(),
                        SummarizationTriggered(
                            phase_id=phase_id,
                            tokens_before=tokens_before,
                            tokens_after=tokens_after,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            logger.error(
                "Context summarization failed for phase '%s': %s", phase_id, exc
            )
            return False

        logger.info(
            "Context for phase '%s' summarized: %d -> %d tokens (%d saved)",
            phase_id,
            tokens_before,
            tokens_after,
            tokens_before - tokens_after,
        )
        return True
