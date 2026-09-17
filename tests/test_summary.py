from __future__ import annotations

import json
from datetime import datetime, timezone

from agency.tui.dag_view import NodeRow
from agency.tui.summary import (
    RunSummary,
    append_summary_to_log,
    render_summary_table,
)

COMPLETED_AT = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_summary() -> RunSummary:
    nodes = (
        NodeRow("a", "agent", "m-a", "done", 100, 0.5),
        NodeRow("b", "agent", "m-b", "failed", None, None),
        NodeRow("c", "merge", None, "skipped", None, None),
    )
    return RunSummary(
        run_id="abc123",
        completed_at=COMPLETED_AT,
        total_tokens=250,
        vram_peak_bytes=None,
        duration_seconds=12.34,
        nodes=nodes,
    )


def table_lines(summary: RunSummary) -> list[str]:
    # collapse the right-alignment padding so we assert on cell values
    return [" ".join(line.split()) for line in render_summary_table(summary).splitlines()]


def _nodes_of_line(line: str) -> dict:
    return json.loads(line)


class TestRenderSummaryTable:
    def test_headers_and_rows(self):
        lines = table_lines(make_summary())

        assert lines[0] == "id type model status tokens duration"
        assert lines[1] == "a agent m-a done 100 0.5s"
        assert lines[2] == "b agent m-b failed - -"
        assert lines[3] == "c merge - skipped - -"

    def test_totals_row_uses_run_level_totals(self):
        lines = table_lines(make_summary())

        # totals are the run's aggregate (250 / 12.3s), not the row sum
        assert lines[4] == "totals 250 12.3s"

    def test_none_cells_render_as_dash(self):
        summary = RunSummary(
            run_id="r",
            completed_at=COMPLETED_AT,
            total_tokens=0,
            vram_peak_bytes=None,
            duration_seconds=0.0,
            nodes=(NodeRow("a", "agent", None, "pending", None, None),),
        )

        lines = table_lines(summary)

        assert lines[1] == "a agent - pending - -"
        assert lines[2] == "totals 0 0.0s"


class TestAppendSummaryToLog:
    def test_writes_single_json_line_with_expected_keys(self, tmp_path):
        log_dir = tmp_path / "runs"
        summary = make_summary()

        path = append_summary_to_log(log_dir, "abc123", summary)

        assert path == log_dir / "abc123" / "summary.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = _nodes_of_line(lines[0])
        assert entry["type"] == "run_summary"
        assert entry["run_id"] == "abc123"
        assert entry["total_tokens"] == 250
        assert entry["vram_peak_bytes"] is None
        assert entry["duration_seconds"] == 12.34
        assert entry["completed_at"] == COMPLETED_AT.isoformat()
        assert entry["nodes"] == [
            {
                "id": "a",
                "status": "done",
                "model": "m-a",
                "tokens": 100,
                "duration_seconds": 0.5,
            },
            {
                "id": "b",
                "status": "failed",
                "model": "m-b",
                "tokens": None,
                "duration_seconds": None,
            },
            {
                "id": "c",
                "status": "skipped",
                "model": None,
                "tokens": None,
                "duration_seconds": None,
            },
        ]

    def test_creates_log_dir_when_missing(self, tmp_path):
        log_dir = tmp_path / "runs"
        assert not log_dir.exists()

        append_summary_to_log(log_dir, "abc123", make_summary())

        assert log_dir.exists()

    def test_appends_new_line_per_call(self, tmp_path):
        log_dir = tmp_path / "runs"

        first = append_summary_to_log(log_dir, "abc123", make_summary())
        append_summary_to_log(log_dir, "abc123", make_summary())

        assert len(first.read_text(encoding="utf-8").splitlines()) == 2
