"""End-of-run summary: data object, plain-text table, JSONL append (Story 3.4)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agency.tui.dag_view import NodeRow


@dataclass(frozen=True)
class RunSummary:
    """Terminal snapshot of a run: per-node rows plus run-level totals."""

    run_id: str
    completed_at: datetime
    total_tokens: int
    vram_peak_bytes: int | None
    duration_seconds: float
    nodes: tuple[NodeRow, ...]


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def _fmt_duration(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.1f}s"


def render_summary_table(summary: RunSummary) -> str:
    """Render the per-node table, right-aligned, with a totals row.

    None tokens/durations render as ``-``; the totals row sums run-level
    totals (not the row cells).
    """
    headers = ["id", "type", "model", "status", "tokens", "duration"]
    rows = [
        [
            row.node_id,
            row.node_type,
            _cell(row.model),
            row.status,
            _cell(row.tokens),
            _fmt_duration(row.duration_seconds),
        ]
        for row in summary.nodes
    ]
    rows.append(["totals", "", "", "", _cell(summary.total_tokens), _fmt_duration(summary.duration_seconds)])
    widths = [
        max(len(header), max(len(row[i]) for row in rows))
        for i, header in enumerate(headers)
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.rjust(width) for cell, width in zip(cells, widths))

    return "\n".join([fmt(headers), *map(fmt, rows)])


def append_summary_to_log(log_dir: Path | str, run_id: str, summary: RunSummary) -> Path:
    """Append the run summary as a single JSON line under ``log_dir/run_id``.

    Mirrors the backend-router log convention: lazy ``mkdir -p`` and compact
    JSON. Returns the path written to.
    """
    entry = {
        "type": "run_summary",
        "run_id": summary.run_id,
        "completed_at": summary.completed_at.isoformat(),
        "duration_seconds": summary.duration_seconds,
        "total_tokens": summary.total_tokens,
        "vram_peak_bytes": summary.vram_peak_bytes,
        "nodes": [
            {
                "id": row.node_id,
                "status": row.status,
                "model": row.model,
                "tokens": row.tokens,
                "duration_seconds": row.duration_seconds,
            }
            for row in summary.nodes
        ],
    }
    log_path = Path(log_dir) / run_id / "summary.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return log_path
