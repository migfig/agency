"""CLI contract tests for --tui/--demo/VRAM flags + summary (Stories 3.3/3.4)."""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from agency.cli.commands import _launch_tui, run
from agency.executor.orchestrator import DagRunResult, NodeRecord
from agency.resource_manager.provisioning import ProvisionedRun
from agency.tui.dag_view import NodeRow
from agency.tui.summary import RunSummary
from agency.yaml_engine.parser import load_workflow

_FAKE_PROVISIONED = ProvisionedRun(
    workflow=None,  # type: ignore[arg-type]
    router=None,  # type: ignore[arg-type]
    models={},
    vram_sizes={},
    owned_servers=[],
)

WORKFLOW_YAML = """
name: cli-tui-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
models:
  x:
    endpoint: http://127.0.0.1:8080
    vram_size: 2GB
"""


def invoke_cli(tmp_path: Path, args: list[str]):
    wf_file = tmp_path / "workflow.yaml"
    wf_file.write_text(WORKFLOW_YAML)
    runner = CliRunner()
    return runner.invoke(run, [str(wf_file), *args])


def test_demo_without_tui_is_usage_error(tmp_path: Path):
    result = invoke_cli(tmp_path, ["--demo"])
    assert result.exit_code != 0
    assert "--demo requires --tui" in result.output


def test_demo_fail_without_demo_is_usage_error(tmp_path: Path):
    result = invoke_cli(tmp_path, ["--tui", "--demo-fail", "a"])
    assert result.exit_code != 0
    assert "--demo-fail requires --demo" in result.output


class _FakeHeadlessOrchestrator:
    """Stand-in for the real orchestrator on the flagless path.

    The constructor swallows whatever the CLI passes (the provisioned router,
    real runners and store objects); ``run()`` reports a clean completed run so
    the test exercises the CLI's summary/exit-code contract in isolation.
    """

    def __init__(self, *args, **kwargs):
        pass

    async def run(self) -> DagRunResult:
        return DagRunResult(
            run_id="fake-run",
            status="completed",
            duration_seconds=12.0,
            total_tokens=600,
            nodes=(
                NodeRecord(
                    node_id="a",
                    status="completed",
                    model="x",
                    tokens=600,
                    duration_seconds=12.0,
                ),
            ),
        )


def test_flagless_invocation_runs_orchestrator_and_exits_zero(
    tmp_path: Path, monkeypatch
):
    async def fake_probe(endpoint: str, timeout: float = 5.0) -> bool:
        return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agency.resource_manager.provisioning._probe_reachable", fake_probe
    )
    monkeypatch.setattr(
        "agency.executor.orchestrator.DagOrchestrator",
        _FakeHeadlessOrchestrator,
    )
    result = invoke_cli(tmp_path, [])
    assert result.exit_code == 0, result.output
    printed = " ".join(result.output.split())
    assert "id type model status tokens duration" in printed
    assert "a agent x done 600 12.0s" in printed
    assert "totals 600 12.0s" in printed


def test_tui_flag_invokes_launch_tui(tmp_path: Path, monkeypatch):
    calls: list[bool] = []

    def fake_launch(workflow, *, demo, models_path=None, vram_limit=None, demo_fail_nodes=None, demo_fallback_fail_nodes=None, environment=None, sandbox_caps=(None, None, None, None)):
        calls.append(demo)

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(tmp_path, ["--tui"])
    assert result.exit_code == 0, result.output
    assert calls == [False]


def test_tui_demo_forwards_demo_flag_to_launch_tui(
    tmp_path: Path, monkeypatch
):
    calls: list[bool] = []

    def fake_launch(workflow, *, demo, models_path=None, vram_limit=None, demo_fail_nodes=None, demo_fallback_fail_nodes=None, environment=None, sandbox_caps=(None, None, None, None)):
        calls.append(demo)

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(tmp_path, ["--tui", "--demo"])
    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_demo_fallback_fail_without_demo_is_usage_error(tmp_path: Path):
    result = invoke_cli(
        tmp_path, ["--tui", "--demo-fallback-fail", "a"]
    )
    assert result.exit_code != 0
    assert "--demo-fallback-fail requires --demo" in result.output


def test_tui_demo_fallback_fail_forwards_nodes_to_launch_tui(
    tmp_path: Path, monkeypatch
):
    seen: dict = {}

    def fake_launch(
        workflow, *, demo, models_path=None, vram_limit=None,
        demo_fail_nodes=None, demo_fallback_fail_nodes=None,
        environment=None, sandbox_caps=(None, None, None, None),
    ):
        seen["demo"] = demo
        seen["demo_fallback_fail_nodes"] = demo_fallback_fail_nodes

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(
        tmp_path,
        ["--tui", "--demo", "--demo-fallback-fail", "a", "--demo-fallback-fail", "b"],
    )
    assert result.exit_code == 0, result.output
    assert seen["demo"] is True
    assert seen["demo_fallback_fail_nodes"] == {"a", "b"}


def test_tui_demo_fallback_fail_defaults_empty_set(
    tmp_path: Path, monkeypatch
):
    seen: dict = {}

    def fake_launch(
        workflow, *, demo, models_path=None, vram_limit=None,
        demo_fail_nodes=None, demo_fallback_fail_nodes=None,
        environment=None, sandbox_caps=(None, None, None, None),
    ):
        seen["demo_fallback_fail_nodes"] = demo_fallback_fail_nodes

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(tmp_path, ["--tui", "--demo"])
    assert result.exit_code == 0, result.output
    assert seen["demo_fallback_fail_nodes"] == set()


def test_tui_demo_fail_forwards_nodes_to_launch_tui(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_launch(workflow, *, demo, models_path=None, vram_limit=None, demo_fail_nodes=None, demo_fallback_fail_nodes=None, environment=None, sandbox_caps=(None, None, None, None)):
        seen["demo"] = demo
        seen["demo_fail_nodes"] = demo_fail_nodes

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(
        tmp_path, ["--tui", "--demo", "--demo-fail", "a", "--demo-fail", "b"]
    )
    assert result.exit_code == 0, result.output
    assert seen["demo"] is True
    assert seen["demo_fail_nodes"] == {"a", "b"}


def test_tui_demo_fail_defaults_empty_set(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_launch(workflow, *, demo, models_path=None, vram_limit=None, demo_fail_nodes=None, demo_fallback_fail_nodes=None, environment=None, sandbox_caps=(None, None, None, None)):
        seen["demo_fail_nodes"] = demo_fail_nodes

    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(tmp_path, ["--tui", "--demo"])
    assert result.exit_code == 0, result.output
    # an empty set (not None) is forwarded so the TUI can tell "none --demo-fail"
    # apart from "flag omitted on a non-demo run"; it is always a set of ids.
    assert seen["demo_fail_nodes"] == set()


def test_tui_passes_resolved_vram_limit_to_launch_tui(
    tmp_path: Path, monkeypatch
):
    seen: dict = {}

    def fake_resolve(*, cli_value, yaml_value):
        return SimpleNamespace(limit_bytes=777, source="cli")

    def fake_launch(workflow, *, demo, models_path=None, vram_limit, demo_fail_nodes=None, demo_fallback_fail_nodes=None, environment=None, sandbox_caps=(None, None, None, None)):
        seen["vram_limit"] = vram_limit

    monkeypatch.setattr("agency.cli.commands.resolve_vram_limit", fake_resolve)
    monkeypatch.setattr("agency.cli.commands._launch_tui", fake_launch)
    result = invoke_cli(tmp_path, ["--tui"])
    assert result.exit_code == 0, result.output
    assert seen["vram_limit"] == 777


def test_console_script_target_resolves():
    import importlib

    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    target = data["project"]["scripts"]["agency"]
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr))


class _FakeTTY(io.StringIO):
    """A writable buffer that pretends to be an interactive terminal."""

    def isatty(self) -> bool:
        return True


def _make_fake_app_class(summary: RunSummary | None):
    class _FakeApp:
        def __init__(
            self,
            workflow,
            run_id,
            *,
            event_bus=None,
            demo=False,
            vram_limit=None,
            demo_fail_schedule=None,
            demo_fallback_fail_schedule=None,
            orchestrator=None,
            execution_environment=None,
        ):
            self.summary = summary

        def run(self) -> None:
            pass

    return _FakeApp


def _cli_summary() -> RunSummary:
    return RunSummary(
        run_id="runc123",
        completed_at=datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc),
        total_tokens=600,
        vram_peak_bytes=None,
        duration_seconds=12.0,
        nodes=(NodeRow("a", "agent", "x", "done", 600, 12.0),),
    )


def test_launch_tui_prints_summary_table_when_present(monkeypatch):
    fake_stdout = _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        "agency.tui.app.AgencyApp", _make_fake_app_class(_cli_summary())
    )

    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with patch("agency.resource_manager.provisioning.provision", new=fake_provision):
        _launch_tui(load_workflow(WORKFLOW_YAML), demo=False, vram_limit=777)

    printed = " ".join(fake_stdout.getvalue().split())
    assert "id type model status tokens duration" in printed
    assert "a agent x done 600 12.0s" in printed
    assert "totals 600 12.0s" in printed


def test_launch_tui_prints_nothing_when_summary_absent(monkeypatch):
    fake_stdout = _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        "agency.tui.app.AgencyApp", _make_fake_app_class(None)
    )

    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with patch("agency.resource_manager.provisioning.provision", new=fake_provision):
        _launch_tui(load_workflow(WORKFLOW_YAML), demo=False)

    assert fake_stdout.getvalue() == ""
