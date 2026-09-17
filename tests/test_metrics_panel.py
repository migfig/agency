from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agency.tui.metrics_panel import (
    MetricsCollector,
    MetricsPanel,
    MetricsSnapshot,
    format_elapsed,
    format_vram,
)

T0 = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


class TestCollectorMath:
    def test_initial_snapshot_is_zeroed(self):
        c = MetricsCollector()

        snap = c.snapshot(now=T0)

        assert snap.total_tokens == 0
        assert snap.active_agents == 0
        assert snap.vram_bytes is None
        assert snap.vram_peak_bytes is None
        assert snap.elapsed_seconds == 0.0

    def test_started_nodes_join_active_set(self):
        c = MetricsCollector()
        c.note_started("a", T0)
        c.note_started("b", T0 + timedelta(seconds=5))

        snap = c.snapshot(now=T0 + timedelta(seconds=10))

        assert snap.active_agents == 2
        # elapsed is anchored to the first node start, not the last
        assert snap.elapsed_seconds == pytest.approx(10.0)

    def test_completed_accumulates_tokens_and_drops_active(self):
        c = MetricsCollector()
        c.note_started("a", T0)
        c.note_started("b", T0)
        c.note_completed("a", 100)

        snap = c.snapshot(now=T0)

        assert snap.total_tokens == 100
        assert snap.active_agents == 1
        c.note_completed("b", 150)
        snap = c.snapshot(now=T0)
        assert snap.total_tokens == 250
        assert snap.active_agents == 0

    def test_failed_drops_active_without_tokens(self):
        c = MetricsCollector()
        c.note_started("a", T0)
        c.note_started("b", T0)
        c.note_failed("b")

        snap = c.snapshot(now=T0)

        assert snap.total_tokens == 0
        assert snap.active_agents == 1

    def test_completed_and_failed_unknown_ids_do_not_go_negative(self):
        c = MetricsCollector()
        c.note_started("a", T0)
        c.note_completed("ghost", 20)
        c.note_failed("phantom")

        snap = c.snapshot(now=T0)

        assert snap.total_tokens == 20
        assert snap.active_agents == 1


class TestCollectorVram:
    def test_tracks_last_sample_and_peak(self):
        c = MetricsCollector()
        c.note_vram_sample(1_500_000_000)
        c.note_vram_sample(2_000_000_000)
        c.note_vram_sample(1_200_000_000)
        c.note_vram_sample(None)  # no-op

        snap = c.snapshot(now=T0)

        assert snap.vram_bytes == 1_200_000_000
        assert snap.vram_peak_bytes == 2_000_000_000

    def test_none_before_any_sample(self):
        snap = MetricsCollector().snapshot(now=T0)

        assert snap.vram_bytes is None
        assert snap.vram_peak_bytes is None


class TestFormatters:
    def test_format_vram(self):
        assert format_vram(None) == "n/a"
        assert format_vram(1_200_000_000) == "1.20 GB"
        assert format_vram(1_000_000_000) == "1.00 GB"

    def test_format_elapsed(self):
        assert format_elapsed(0) == "0:00"
        assert format_elapsed(90) == "1:30"
        assert format_elapsed(3661) == "61:01"
        # clamped at zero, never negative
        assert format_elapsed(-5) == "0:00"


class TestMetricsPanel:
    def _lines(self, panel: MetricsPanel) -> list[str]:
        return panel.render().plain.splitlines()

    def test_renders_four_lines_with_real_data(self):
        p = MetricsPanel(
            MetricsSnapshot(1024, 2, 1_540_000_000, 1_540_000_000, 90)
        )

        assert self._lines(p) == [
            "total tokens: 1024",
            "vram: 1.54 GB",
            "elapsed: 1:30",
            "active agents: 2",
        ]

    def test_renders_zeros_and_na_before_any_data(self):
        p = MetricsPanel()

        assert self._lines(p) == [
            "total tokens: 0",
            "vram: n/a",
            "elapsed: 0:00",
            "active agents: 0",
        ]

    def test_update_replaces_snapshot_in_place(self):
        p = MetricsPanel()
        p.update(MetricsSnapshot(7, 1, None, None, 5))

        assert p.snapshot.total_tokens == 7
        assert self._lines(p)[0] == "total tokens: 7"
        assert self._lines(p)[2] == "elapsed: 0:05"
        assert self._lines(p)[3] == "active agents: 1"
