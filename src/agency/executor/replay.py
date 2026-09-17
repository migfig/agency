"""Checkpoint restore & replay (Story 5.6).

Given a workflow and a checkpoint node, locate the source run that completed
that node, restore the checkpoint node and all its transitive ancestors from
the source run (checkpoint files and raw outputs, byte-for-byte), then tail-execute
the remaining nodes using the live orchestrator — all into a fresh
``runs/{new_run_id}/`` whose ``metadata.json`` references the source run.

Every precondition is validated before any file is created or event published,
so a failing replay leaves no artifacts behind.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agency.core.event_bus import EventBus, get_event_bus
from agency.executor.run_log import RunLogger
from agency.executor.run_state import COMPLETED, COMPLETED_FALLBACK, RunStateStore
from agency.yaml_engine.dag import DAGBuilder
from agency.yaml_engine.schema import Workflow

logger = logging.getLogger(__name__)

def get_folder_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")  # noqa: DTZ005


class ReplayError(Exception):
    """Replay precondition failure with a directly printable message."""


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of a replay: the fresh run, its source, and the split."""

    new_run_id: str
    source_run_id: str
    restored: list[str]
    executed: list[str]


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def restore_set_for(workflow: Workflow, node_id: str) -> list[str]:
    """The checkpoint node plus all its transitive ancestors, in topo order."""
    if node_id not in workflow.nodes:
        raise ReplayError(
            f"checkpoint node '{node_id}' is not in workflow '{workflow.name}'"
        )
    predecessors: dict[str, list[str]] = {}
    for edge in workflow.edges:
        predecessors.setdefault(edge.to_id, []).append(edge.from_id)
    wanted: set[str] = {node_id}
    stack = [node_id]
    while stack:
        for parent in predecessors.get(stack.pop(), ()):
            if parent not in wanted:
                wanted.add(parent)
                stack.append(parent)
    topo = DAGBuilder(workflow).topological_sort()
    return [nid for nid in topo if nid in wanted]


def _checkpoint(run_dir: Path, node_id: str) -> dict | None:
    return _load_json(run_dir / "checkpoints" / f"{node_id}.json")


COMPLETED_STATUSES: frozenset[str] = frozenset({COMPLETED, COMPLETED_FALLBACK})


def _checkpoint_complete(run_dir: Path, node_id: str) -> bool:
    checkpoint = _checkpoint(run_dir, node_id)
    return checkpoint is not None and checkpoint.get("status") in COMPLETED_STATUSES


def _run_started_at(log_dir: Path, run_id: str) -> str:
    meta = _load_json(log_dir / run_id / "metadata.json")
    if meta is None or not isinstance(meta.get("started_at"), str):
        return ""
    return meta["started_at"]


def _eligible_run(log_dir: Path, run_id: str, workflow_name: str, node_id: str) -> bool:
    meta = _load_json(log_dir / run_id / "metadata.json")
    if meta is None or meta.get("workflow_name") != workflow_name:
        return False
    return _checkpoint_complete(log_dir / run_id, node_id)


def find_source_run(
    log_dir: Path | str,
    source_run_id: str | None,
    workflow_name: str,
    node_id: str,
) -> str:
    log_dir = Path(log_dir)
    if source_run_id is not None:
        run_dir = log_dir / source_run_id
        meta = _load_json(run_dir / "metadata.json")
        if meta is None:
            raise ReplayError(f"source run '{source_run_id}' not found in '{log_dir}'")
        if meta.get("workflow_name") != workflow_name:
            raise ReplayError(
                f"source run '{source_run_id}' belongs to workflow "
                f"'{meta.get('workflow_name')}', not '{workflow_name}'"
            )
        if not _checkpoint_complete(run_dir, node_id):
            raise ReplayError(
                f"source run '{source_run_id}' has no completed checkpoint "
                f"for node '{node_id}'"
            )
        return source_run_id

    if not log_dir.is_dir():
        raise ReplayError(
            f"no source run found with a completed checkpoint for node "
            f"'{node_id}' (log dir '{log_dir}' does not exist)"
        )
    candidates = [
        path.name
        for path in log_dir.iterdir()
        if path.is_dir()
        and _eligible_run(log_dir, path.name, workflow_name, node_id)
    ]
    if not candidates:
        raise ReplayError(
            f"no source run found with a completed checkpoint for node "
            f"'{node_id}' in '{log_dir}'"
        )
    newest = max(candidates, key=lambda name: (_run_started_at(log_dir, name), name))
    return newest


async def replay_checkpoint(
    log_dir: Path | str,
    workflow: Workflow,
    checkpoint_node_id: str,
    *,
    source_run_id: str | None = None,
    new_run_id: str | None = None,
    event_bus: EventBus | None = None,
    agent_runner: Any,
    non_agent_runner: Any,
    context_store: Any,
    load_manager: Any,
    execution_environment: str | None = None,
) -> ReplayResult:
    """Restore checkpoint prefix from a source run, then tail-execute live."""
    log_dir = Path(log_dir)
    restore_set = set(restore_set_for(workflow, checkpoint_node_id))
    topo = DAGBuilder(workflow).topological_sort()
    restore_order = [nid for nid in topo if nid in restore_set]
    source_run_id = find_source_run(
        log_dir, source_run_id, workflow.name, checkpoint_node_id
    )
    source_run_dir = log_dir / source_run_id

    problems: list[str] = []
    loaded: dict[str, dict] = {}
    for nid in restore_order:
        checkpoint = _checkpoint(source_run_dir, nid)
        if checkpoint is None:
            problems.append(f"'{nid}': no checkpoint in source run")
            continue
        loaded[nid] = checkpoint
        if checkpoint.get("status") not in COMPLETED_STATUSES:
            problems.append(
                f"'{nid}': source checkpoint status "
                f"'{checkpoint.get('status')}' is not completed"
            )
            continue
        if checkpoint.get("workflow_id") != workflow.name:
            problems.append(
                f"'{nid}': checkpoint workflow_id mismatch"
            )
            continue
        output_ref = checkpoint.get("output_ref")
        if output_ref and not (source_run_dir / str(output_ref)).is_file():
            problems.append(
                f"'{nid}': referenced output '{output_ref}' missing"
            )
    if problems:
        raise ReplayError(
            f"cannot replay from source run '{source_run_id}': "
            + "; ".join(problems)
        )

    if new_run_id is None:
        new_run_id = get_folder_id()
    bus = event_bus if event_bus is not None else get_event_bus()
    source_ref = {
        "source_run_id": source_run_id,
        "checkpoint_node": checkpoint_node_id,
    }
    store = RunStateStore(
        log_dir,
        new_run_id,
        workflow,
        bus,
        replay_source=source_ref,
        execution_environment=execution_environment,
    )
    await store.start()

    restored: list[str] = []
    restored_outputs: dict[str, str | None] = {}
    for nid in restore_order:
        checkpoint = loaded[nid]
        output: str | None = None
        output_ref = checkpoint.get("output_ref")
        if output_ref:
            src_path = source_run_dir / str(output_ref)
            dst_path = store._run_dir / "outputs" / f"{nid}.txt"
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            dst_path.write_bytes(src_path.read_bytes())
            output = src_path.read_text(encoding="utf-8")

        if not store.record_restored(
            nid,
            output_text=output,
            output_ref=f"outputs/{nid}.txt" if output else None,
            summary=checkpoint.get("summary"),
            tokens_used=int(checkpoint.get("tokens_used") or 0),
            elapsed_seconds=float(checkpoint.get("elapsed_seconds") or 0.0),
            model=checkpoint.get("model"),
        ):
            await store.close()
            raise ReplayError(f"failed to persist restored node '{nid}'")
        restored.append(nid)
        restored_outputs[nid] = output

    bus.set_base_seq(store._next_restore_seq - 1)

    seed_nodes: dict[str, str] = {}
    for nid in restored:
        out_path = store._run_dir / "outputs" / f"{nid}.txt"
        if out_path.is_file():
            seed_nodes[nid] = out_path.read_text(encoding="utf-8")

    from agency.executor.orchestrator import DagOrchestrator

    orchestrator = DagOrchestrator(
        workflow,
        new_run_id,
        agent_runner=agent_runner,
        non_agent_runner=non_agent_runner,
        context_store=context_store,
        run_state=store,
        run_logger=RunLogger(log_dir, new_run_id, workflow, bus),
        load_manager=load_manager,
        bus=bus,
    )
    result = await orchestrator.run(seed_nodes=seed_nodes)

    # Insert execution.jsonl entries for restored nodes after run_started
    exec_path = store._run_dir / "execution.jsonl"
    if exec_path.is_file():
        existing_lines = exec_path.read_text(encoding="utf-8").splitlines()
        first_line = existing_lines[0] if existing_lines else ""
        restored_lines: list[str] = []
        for nid in restored:
            restored_lines.append(json.dumps({
                "type": "node_completed",
                "run_id": new_run_id,
                "seq": 0,
                "node_id": nid,
                "status": "completed",
                "model": loaded[nid].get("model"),
                "input": None,
                "output": restored_outputs.get(nid),
                "started_at": None,
                "ended_at": None,
                "duration_seconds": None,
                "tokens_used": None,
            }, separators=(",", ":")))
        exec_path.write_text(
            (first_line + "\n" if first_line else "") +
            "\n".join(restored_lines) + ("\n" if restored_lines else "") +
            ("\n".join(existing_lines[1:]) + "\n" if len(existing_lines) > 1 else ""),
            encoding="utf-8",
        )

    executed = [n.node_id for n in result.nodes if n.node_id not in restore_set]
    logger.info(
        "Replayed %s: restored %s from %s, executed %s",
        new_run_id,
        restored,
        source_run_id,
        executed,
    )
    return ReplayResult(
        new_run_id=new_run_id,
        source_run_id=source_run_id,
        restored=restored,
        executed=executed,
    )
