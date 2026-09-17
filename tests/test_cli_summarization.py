"""Tests for CLI startup logging of the auto-summarization config (Story 3.2)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from agency.cli.commands import run
from agency.executor.orchestrator import DagRunResult

WORKFLOW_UNSET_YAML = """
name: cli-test
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


def invoke_cli(tmp_path: Path, yaml_text: str):
    wf_file = tmp_path / "workflow.yaml"
    wf_file.write_text(yaml_text)
    runner = CliRunner()
    return runner.invoke(run, [str(wf_file), "--vram-limit", "1GB"])


class _FakeHeadlessOrchestrator:
    """Stand-in so the flagless path does not drive a real DAG.

    These tests pin the CLI *startup logging* contract; the orchestrator only
    matters for exit code, and a clean completed run keeps that at zero without
    touching VRAM/inference.
    """

    def __init__(self, *args, **kwargs):
        pass

    async def run(self) -> DagRunResult:
        return DagRunResult(
            run_id="fake-run",
            status="completed",
            duration_seconds=1.0,
            total_tokens=0,
            nodes=(),
        )


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch):
    async def fake_probe(endpoint: str, timeout: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(
        "agency.resource_manager.provisioning._probe_reachable", fake_probe
    )


@pytest.fixture(autouse=True)
def _stub_orchestrator(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agency.executor.orchestrator.DagOrchestrator",
        _FakeHeadlessOrchestrator,
    )


def test_cli_logs_enabled_config_with_override(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    yaml_text = (
        WORKFLOW_UNSET_YAML
        + "summarize_threshold: 60\n"
        + "context_window: 4096\n"
        + "summarizer_model: phi-2\n"
    )
    with caplog.at_level(logging.INFO, logger="agency.cli.commands"):
        result = invoke_cli(tmp_path, yaml_text)
    assert result.exit_code == 0, result.output
    assert (
        "Auto-summarization: enabled at 60% of 4096-token window "
        "(2457 tokens) with model 'phi-2'" in caplog.text
    )


def test_cli_logs_defaults_when_only_model_set(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    yaml_text = WORKFLOW_UNSET_YAML + "summarizer_model: phi-2\n"
    with caplog.at_level(logging.INFO, logger="agency.cli.commands"):
        result = invoke_cli(tmp_path, yaml_text)
    assert result.exit_code == 0, result.output
    # Literals on purpose: this test must pin the spec'd defaults (80% of
    # 8192 = 6553 tokens), not echo whatever the constants currently hold.
    expected = (
        "Auto-summarization: enabled at 80% of 8192-token window "
        "(6553 tokens) with model 'phi-2'"
    )
    assert expected in caplog.text


def test_cli_logs_disabled_when_no_summarizer_model(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="agency.cli.commands"):
        result = invoke_cli(tmp_path, WORKFLOW_UNSET_YAML)
    assert result.exit_code == 0, result.output
    assert "Auto-summarization: disabled (no summarizer_model configured)" in caplog.text
