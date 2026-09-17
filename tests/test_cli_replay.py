"""Tests for the `agency replay` CLI command and bare positional fallback."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agency.cli.commands import agency_cli
from agency.executor.contracts import RunResult
from agency.executor.execution_env import LocalToolBackend
from agency.executor.tool_registry import build_workflow_tool_registry
from agency.resource_manager.provisioning import ProvisionedRun, ProvisionError

_FAKE_PROVISIONED = ProvisionedRun(
    workflow=None,  # type: ignore[arg-type]
    router=None,  # type: ignore[arg-type]
    models={},
    vram_sizes={},
    owned_servers=[],
)


class MockAgentRunner:
    async def run(self, node, context, *, phase_id=None, attempt=1, fallback=None, node_id=None):
        nid = node.id if node_id is None else node_id
        return RunResult(
            node_id=nid,
            outcome="completed",
            output=f"simulated output for {nid}",
            tokens_used=100,
            duration_seconds=0.5,
            model=node.model,
        )


class MockNonAgentRunner:
    async def run(self, node, context, *, phase_id=None, deps=()):
        return RunResult(
            node_id=node.id,
            outcome="completed",
            output=f"simulated non-agent for {node.id}",
            tokens_used=0,
            duration_seconds=0.0,
        )


class MockContextStore:
    def get_context(self, phase_id):
        return {}

    async def flush_events(self):
        pass

    def record_output(self, phase_id, attribution, output):
        pass


class MockLoadManager:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def acquire_slot(self, node_id, model_name, estimated_bytes):
        return None

    def release_slot(self, model_name, node_id, freed_bytes=None):
        pass

WORKFLOW_YAML = """\
name: replay-cli-chain
nodes:
  a:
    id: a
    type: agent
    model: m-a
    prompt_template: "step a"
  b:
    id: b
    type: agent
    model: m-b
    prompt_template: "step b: {{ nodes.a.output }}"
  c:
    id: c
    type: agent
    model: m-c
    prompt_template: "step c: {{ nodes.b.output }}"
  d:
    id: d
    type: agent
    model: m-d
    prompt_template: "step d: {{ nodes.c.output }}"
entry_point: a
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
  - from_id: c
    to_id: d
"""

BASE_STARTED_AT = "2026-08-12T08:00:00+00:00"


@pytest.fixture
def wf_path(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text(WORKFLOW_YAML, encoding="utf-8")
    return path


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def runner():
    return CliRunner()


def craft_source_run(log_dir, run_id, completed_nodes, *, started_at=BASE_STARTED_AT):
    run_dir = log_dir / run_id
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow_name": "replay-cli-chain",
                "started_at": started_at,
                "node_count": 4,
                "vram_limit_bytes": None,
            }
        ),
        encoding="utf-8",
    )
    for node_id in completed_nodes:
        (run_dir / "outputs" / f"{node_id}.txt").write_text(f"src-out-{node_id}", encoding="utf-8")
        (run_dir / "checkpoints" / f"{node_id}.json").write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "workflow_id": "replay-cli-chain",
                    "run_id": run_id,
                    "status": "completed",
                    "output_ref": f"outputs/{node_id}.txt",
                    "summary": f"src-out-{node_id}",
                    "tokens_used": 100,
                    "model": f"m-{node_id}",
                    "elapsed_seconds": 0.5,
                    "timestamp": started_at,
                }
            ),
            encoding="utf-8",
        )


def new_run_dirs(log_dir):
    return sorted(p.name for p in log_dir.iterdir() if p.is_dir())


def test_replay_cli_happy_path(wf_path, log_dir, runner):
    craft_source_run(log_dir, "src-run", ["a", "b", "c"], started_at=BASE_STARTED_AT)

    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with patch("agency.resource_manager.provisioning.provision", new=fake_provision), \
         patch("agency.executor.agent_runner.AgentRunner", return_value=MockAgentRunner()), \
         patch("agency.executor.node_runners.NonAgentRunner", return_value=MockNonAgentRunner()), \
         patch("agency.executor.context_store.ContextStore", return_value=MockContextStore()), \
         patch("agency.resource_manager.model_load_manager.ModelLoadManager", return_value=MockLoadManager()):
        result = runner.invoke(
            agency_cli,
            [
                "replay",
                str(wf_path),
                "--checkpoint",
                "c",
                "--log-dir",
                str(log_dir),
                # This test exercises replay mechanics, not the sandbox; pin to
                # local so the (default) Docker gate is not consulted.
                "--environment",
                "local",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Replaying 'replay-cli-chain' from checkpoint 'c' (source run: src-run)" in result.output
    assert "New run id: " in result.output
    assert "  restored: a" in result.output
    assert "  restored: b" in result.output
    assert "  restored: c" in result.output
    assert "  executed: d" in result.output
    assert "New run dir: " in result.output

    [new_dir] = [n for n in new_run_dirs(log_dir) if n != "src-run"]
    new_run = log_dir / new_dir
    meta = json.loads((new_run / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) == {
        "run_id",
        "workflow_name",
        "started_at",
        "node_count",
        "vram_limit_bytes",
        "replay_source",
        "execution_environment",
    }
    assert meta["replay_source"] == {"source_run_id": "src-run", "checkpoint_node": "c"}
    assert meta["execution_environment"] == "local"
    assert meta["node_count"] == 4

    # restored outputs are byte-for-byte source bytes; executed node is simulated.
    assert (new_run / "outputs" / "a.txt").read_bytes() == b"src-out-a"
    assert (new_run / "outputs" / "c.txt").read_bytes() == b"src-out-c"
    assert (new_run / "outputs" / "d.txt").read_text(encoding="utf-8") == "simulated output for d"

    # source run is untouched.
    assert (log_dir / "src-run" / "outputs" / "c.txt").read_bytes() == b"src-out-c"
    src_meta = json.loads((log_dir / "src-run" / "metadata.json").read_text(encoding="utf-8"))
    assert "replay_source" not in src_meta


def test_replay_cli_no_source_run(wf_path, log_dir, runner):
    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with patch("agency.resource_manager.provisioning.provision", new=fake_provision):
        result = runner.invoke(
            agency_cli,
            ["replay", str(wf_path), "--checkpoint", "c", "--log-dir", str(log_dir)],
        )

    assert result.exit_code == 1
    assert f"no source run found with a completed checkpoint for node 'c' in '{log_dir}'" in result.output
    # failed replay creates no artifacts.
    assert list(log_dir.iterdir()) == []


def test_replay_cli_explicit_source_missing_checkpoint(wf_path, log_dir, runner):
    craft_source_run(log_dir, "src-run", ["a", "b"], started_at=BASE_STARTED_AT)

    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with patch("agency.resource_manager.provisioning.provision", new=fake_provision):
        result = runner.invoke(
            agency_cli,
            [
                "replay",
                str(wf_path),
                "--checkpoint",
                "c",
                "--run",
                "src-run",
                "--log-dir",
                str(log_dir),
            ],
        )

    assert result.exit_code == 1
    assert "has no completed checkpoint for node 'c'" in result.output
    assert new_run_dirs(log_dir) == ["src-run"]


def test_bare_positional_falls_back_to_run(wf_path, runner):
    # Legacy ``agency <workflow.yaml> ...`` dispatch: first token is not a
    # subcommand name, so the full argument vector routes to ``run``.
    result = runner.invoke(agency_cli, [str(wf_path), "--demo"])
    assert result.exit_code == 2
    assert "--demo requires --tui" in result.output


def test_top_level_help_lists_subcommands(runner):
    result = runner.invoke(agency_cli, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "replay" in result.output


def test_unknown_top_level_flag(runner):
    result = runner.invoke(agency_cli, ["--bogus"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_replay_requires_checkpoint(wf_path, runner):
    result = runner.invoke(agency_cli, ["replay", str(wf_path)])
    assert result.exit_code == 2
    assert "Missing option '--checkpoint'" in result.output


# ---------------------------------------------------------------------------
# Execution-environment flags (spec 006, T020/T025)
# ---------------------------------------------------------------------------

SANDBOX_WF_YAML = WORKFLOW_YAML + """
execution_environment: local
sandbox:
  cpu: 3
  memory: 256MB
  pids_limit: 64
  timeout_seconds: 42
  output_limit_bytes: 123456
  scratch_size: 8MB
"""

SANDBOX_ON_WF_YAML = SANDBOX_WF_YAML.replace(
    "execution_environment: local", "execution_environment: sandbox"
)

PLAIN_WF_YAML = WORKFLOW_YAML


class _FakeSandboxBackend:
    """Marker backend standing in for a provisioned DockerSandboxBackend."""


_SANDBOX_BACKEND = _FakeSandboxBackend()


def _make_gate(failures=None):
    """Async stand-in for agency.resource_manager.sandbox.provision_sandbox.

    Returns (calls, fake); each call appends (policy, image_root, run_id,
    kwargs). When *failures* is given the gate raises ProvisionError instead
    of returning the marker backend.
    """
    calls: list = []

    async def provision_sandbox(policy, image_root, run_id, **kwargs):
        calls.append((policy, str(image_root), run_id, kwargs))
        if failures:
            raise ProvisionError(list(failures))
        return _SANDBOX_BACKEND

    return calls, provision_sandbox


def _invoke_replay_with_env(wf_path, log_dir, extra_args, failures=None):
    """Run the `replay` CLI with the model gate, sandbox gate, runner and
    tool-registry construction patched. Returns (result, gate_calls, backends)
    where backends is the execution_backend each registry build saw."""
    gate_calls, fake_gate = _make_gate(failures=failures)
    backends: list = []
    real_build = build_workflow_tool_registry

    def build_spy(workflow, *, execution_backend=None):
        backends.append(execution_backend)
        return real_build(workflow, execution_backend=execution_backend)

    async def fake_provision(*args, **kwargs):
        return _FAKE_PROVISIONED

    with (
        patch("agency.resource_manager.provisioning.provision", new=fake_provision),
        patch("agency.resource_manager.sandbox.provision_sandbox", new=fake_gate),
        patch("agency.executor.tool_registry.build_workflow_tool_registry", new=build_spy),
        patch("agency.executor.agent_runner.AgentRunner", return_value=MockAgentRunner()),
        patch("agency.executor.node_runners.NonAgentRunner", return_value=MockNonAgentRunner()),
        patch("agency.executor.context_store.ContextStore", return_value=MockContextStore()),
        patch("agency.resource_manager.model_load_manager.ModelLoadManager", return_value=MockLoadManager()),
    ):
        runner = CliRunner()
        result = runner.invoke(
            agency_cli,
            ["replay", str(wf_path), "--checkpoint", "c", "--log-dir", str(log_dir), *extra_args],
        )
    return result, gate_calls, backends


@pytest.fixture
def sandbox_wf_path(tmp_path):
    p = tmp_path / "sandbox_wf.yaml"
    p.write_text(SANDBOX_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def sandbox_on_wf_path(tmp_path):
    p = tmp_path / "sandbox_on_wf.yaml"
    p.write_text(SANDBOX_ON_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def plain_wf_path(tmp_path):
    p = tmp_path / "plain_wf.yaml"
    p.write_text(PLAIN_WF_YAML, encoding="utf-8")
    return p


def test_environment_flags_listed_in_help(runner):
    for cmd in ("run", "replay"):
        result = runner.invoke(agency_cli, [cmd, "--help"])
        assert result.exit_code == 0
        for flag in (
            "--environment",
            "--sandbox-cpu",
            "--sandbox-memory",
            "--sandbox-pids",
            "--sandbox-timeout",
        ):
            assert flag in result.output, f"{cmd} --help missing {flag}"


def test_invalid_environment_rejected_by_click(runner, wf_path):
    # bare positional routes to `run`
    result = runner.invoke(agency_cli, [str(wf_path), "--environment", "bogus"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    # explicit replay
    result = runner.invoke(
        agency_cli,
        ["replay", str(wf_path), "--checkpoint", "c", "--environment", "bogus"],
    )
    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_invalid_sandbox_flag_types_rejected_by_click(runner, wf_path):
    for bad in (
        [str(wf_path), "--sandbox-cpu", "not-a-float"],
        [str(wf_path), "--sandbox-pids", "nope"],
        [str(wf_path), "--sandbox-timeout", "soon"],
        ["replay", str(wf_path), "--checkpoint", "c", "--sandbox-cpu", "-1"],
    ):
        result = runner.invoke(agency_cli, bad)
        assert result.exit_code == 2, f"{bad} should be rejected: {result.output}"
        assert "Invalid value" in result.output


def test_replay_env_flag_wins_and_caps_override_workflow(
    sandbox_wf_path, log_dir, runner
):
    """--environment sandbox beats workflow `execution_environment: local`;
    per-run --sandbox-* caps beat the workflow `sandbox:` mapping, which
    beats built-in defaults (contract §4)."""
    craft_source_run(log_dir, "src-run", ["a", "b", "c"])
    result, gate_calls, backends = _invoke_replay_with_env(
        sandbox_wf_path,
        log_dir,
        [
            "--environment", "sandbox",
            "--sandbox-cpu", "1.5",
            "--sandbox-memory", "1GB",
            "--sandbox-pids", "128",
            "--sandbox-timeout", "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(gate_calls) == 1
    policy, image_root, run_id, _ = gate_calls[0]
    assert policy.cpu == 1.5
    assert policy.memory_bytes == 1 * 2**30
    assert policy.pids_limit == 128
    assert policy.timeout_seconds == 60.0
    # No CLI flags exist for these two -> workflow `sandbox:` mapping wins.
    assert policy.output_limit_bytes == 123456
    assert policy.scratch_size_bytes == 8 * 2**20
    assert run_id
    assert image_root.endswith("sandbox")
    # The tool registry was built with the sandboxed backend, not local.
    assert backends == [_SANDBOX_BACKEND]


def test_replay_workflow_sandbox_used_when_flags_absent(sandbox_on_wf_path, log_dir, runner):
    """No flags + workflow `execution_environment: sandbox` -> gate runs with
    the workflow `sandbox:` mapping."""
    craft_source_run(log_dir, "src-run", ["a", "b", "c"])
    result, gate_calls, backends = _invoke_replay_with_env(sandbox_on_wf_path, log_dir, [])
    assert result.exit_code == 0, result.output
    assert len(gate_calls) == 1
    policy, _, _, _ = gate_calls[0]
    assert policy.cpu == 3
    assert policy.memory_bytes == 256 * 2**20
    assert policy.pids_limit == 64
    assert policy.timeout_seconds == 42.0
    assert policy.output_limit_bytes == 123456
    assert policy.scratch_size_bytes == 8 * 2**20
    assert backends == [_SANDBOX_BACKEND]


def test_replay_workflow_local_skips_gate(sandbox_wf_path, log_dir, runner):
    """No flags + workflow `execution_environment: local` -> no gate, local
    backend."""
    craft_source_run(log_dir, "src-run", ["a", "b", "c"])
    result, gate_calls, backends = _invoke_replay_with_env(sandbox_wf_path, log_dir, [])
    assert result.exit_code == 0, result.output
    assert gate_calls == []
    assert backends and all(isinstance(b, LocalToolBackend) for b in backends)


def test_replay_default_is_sandbox_with_builtin_policy(plain_wf_path, log_dir, runner):
    """No flags, no workflow env/sandbox -> built-in defaults, gate runs."""
    craft_source_run(log_dir, "src-run", ["a", "b", "c"])
    result, gate_calls, backends = _invoke_replay_with_env(plain_wf_path, log_dir, [])
    assert result.exit_code == 0, result.output
    assert len(gate_calls) == 1
    policy, _, _, _ = gate_calls[0]
    assert policy.cpu == 2.0
    assert policy.memory_bytes == 512 * 2**20
    assert policy.pids_limit == 256
    assert policy.timeout_seconds == 300.0
    assert policy.output_limit_bytes == 1 * 2**20
    assert policy.scratch_size_bytes == 64 * 2**20
    assert backends == [_SANDBOX_BACKEND]


def test_replay_gate_failure_aborts_before_any_node_runs(sandbox_on_wf_path, log_dir, runner):
    """A failed sandbox gate exits 1 with the failure reasons, tears down
    the model-gate ProvisionedRun, creates no run dir, and falls back to
    nothing (no silent local execution)."""
    craft_source_run(log_dir, "src-run", ["a", "b", "c"])
    failures = ["docker runtime unavailable: no daemon at /var/run/docker.sock"]
    result, gate_calls, backends = _invoke_replay_with_env(
        sandbox_on_wf_path, log_dir, [], failures=failures
    )
    assert result.exit_code == 1, result.output
    assert failures[0] in result.output
    assert len(gate_calls) == 1
    assert backends == []  # no registry was built -> no node executed
    # No run was started: only the pre-existing source run dir remains.
    assert new_run_dirs(log_dir) == ["src-run"]
