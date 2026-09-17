"""Tests for RunService start/status/result/input (US1, T014).

Uses a model-free workflow (tool_call + human_in_loop nodes) so the real
orchestrator stack runs without any model server; the provisioning gate is
injected. Every test resets the singleton event bus.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agency.api.config import ServiceConfig
from agency.api.registry import RunRegistry
from agency.api.schemas import (
    CODE_INPUT_NOT_AWAITING,
    CODE_INVALID_REQUEST,
    CODE_INVALID_WORKFLOW,
    CODE_MODEL_VALIDATION_FAILED,
    CODE_NO_REPLAY_SOURCE,
    CODE_RUN_NOT_FINISHED,
    CODE_SANDBOX_UNAVAILABLE,
    CODE_UNKNOWN_NODE,
    CODE_UNKNOWN_RUN,
    CODE_UNKNOWN_WORKFLOW,
    AgencyAPIError,
    ReplayRequest,
    StartRunRequest,
)
from agency.api.service import RunService
from agency.core.event_bus import reset_event_bus
from agency.executor.execution_env import LocalToolBackend
from agency.executor.tool_registry import build_workflow_tool_registry
from agency.resource_manager.provisioning import ProvisionError

TOOL_WORKFLOW = """
name: api-tool-demo
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "hi"}'
"""

HIL_WORKFLOW = """
name: api-hil-demo
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "go"}'
  gate:
    id: gate
    type: human_in_loop
    prompt: "Approve release? (y/n)"
    timeout_seconds: 300
edges:
  - from_id: shout
    to_id: gate
"""

REPLAY_WORKFLOW = """
name: api-replay-demo
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "hello"}'
  echo:
    id: echo
    type: tool_call
    tool_name: echo
    arguments_template: '{"text": "tail"}'
edges:
  - from_id: shout
    to_id: echo
"""


class FakeProvisionedRun:
    def __init__(self) -> None:
        self.router = None
        self.vram_sizes: dict = {}
        self.model_timeouts: dict = {}
        self.owned_servers: list = []


async def ok_provision(workflow, cli_models, router):
    return FakeProvisionedRun()


async def failing_provision(workflow, cli_models, router):
    raise ProvisionError(["model 'llama': endpoint not reachable and no local path declared"])


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def service(log_dir: Path) -> RunService:
    return RunService(ServiceConfig(log_dir=log_dir), RunRegistry(), provision=ok_provision)


def write_workflow(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


async def wait_until(predicate, *, message: str = "condition", timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0)


async def start_tool_run(service: RunService, tmp_path: Path):
    wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
    accepted = await service.start_run(str(wf))
    handle = service.registry.get(accepted.run_id)
    await wait_until(lambda: handle.result is not None, message="run to finish")
    return accepted, handle


async def wait_for_result(service: RunService, run_id: str, *, timeout: float = 10.0):
    """Poll ``run_result`` until the run is terminal (works live or from disk)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            return service.run_result(run_id)
        except AgencyAPIError:
            if loop.time() > deadline:
                raise AssertionError(f"timed out waiting for run {run_id} to finish")
            await asyncio.sleep(0)


class TestStartRun:
    async def test_missing_workflow_is_unknown_workflow(self, service, log_dir: Path):
        missing = str(log_dir / "nope.yaml")
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.start_run(missing)
        err = excinfo.value
        assert err.code == CODE_UNKNOWN_WORKFLOW
        assert err.http_status == 404
        assert err.details == {"workflow": missing}
        assert list(log_dir.iterdir()) == []

    async def test_invalid_workflow_is_invalid_workflow(self, service, log_dir: Path, tmp_path: Path):
        bad = write_workflow(tmp_path, "bad.yaml", "nodes:\n  a: {id: a, type: bogus}\n")
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.start_run(str(bad))
        err = excinfo.value
        assert err.code == CODE_INVALID_WORKFLOW
        assert err.http_status == 422
        assert err.details["workflow"] == str(bad)
        assert len(err.details["problems"]) > 0
        assert list(log_dir.iterdir()) == []

    async def test_invalid_vram_limit_is_invalid_request(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.start_run(str(wf), vram_limit="not-a-size")
        err = excinfo.value
        assert err.code == CODE_INVALID_REQUEST
        assert err.http_status == 422
        assert err.details["problems"][0]["field"] == "vram_limit"

    async def test_model_validation_failed_409_no_side_effects(self, log_dir: Path, tmp_path: Path):
        service = RunService(
            ServiceConfig(log_dir=log_dir), RunRegistry(), provision=failing_provision
        )
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.start_run(str(wf))
        err = excinfo.value
        assert err.code == CODE_MODEL_VALIDATION_FAILED
        assert err.http_status == 409
        assert err.details["failures"] == [
            "model 'llama': endpoint not reachable and no local path declared"
        ]
        assert list(log_dir.iterdir()) == []

    async def test_success_returns_accepted_and_registers_handle(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        assert accepted.state == "running"
        assert accepted.workflow == "api-tool-demo"
        handle = service.registry.get(accepted.run_id)
        assert handle is not None
        assert handle.state == "running"
        assert set(handle.nodes) == {"shout"}
        await wait_until(lambda: handle.result is not None, message="run to finish")
        assert handle.state == "completed"
        assert (service.log_dir / accepted.run_id / "metadata.json").is_file()
        assert handle.result.status == "completed"


class TestRunStatus:
    async def test_live_status_reflects_mirror_and_pending_inputs(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "hil.yaml", HIL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.pending_inputs, message="pending input")
        try:
            view = service.run_status(accepted.run_id)
            assert view.run_id == accepted.run_id
            assert view.workflow_name == "api-hil-demo"
            assert view.state == "running"
            assert view.replay_source is None
            assert view.nodes["shout"].status == "completed"
            assert view.nodes["gate"].status == "running"
            assert len(view.pending_inputs) == 1
            entry = view.pending_inputs[0]
            assert entry.node_id == "gate"
            assert entry.prompt == "Approve release? (y/n)"
            assert entry.deadline is not None
        finally:
            service.submit_input(accepted.run_id, "gate", "yes")
            await wait_until(lambda: handle.result is not None, message="run to finish")

    async def test_reconstructs_terminal_disk_run_not_in_mirror(self, service, tmp_path: Path):
        # Run a real run to completion, then drop the handle to simulate a
        # service restart: the status must come from the durable run dir.
        accepted, _ = await start_tool_run(service, tmp_path)
        service.registry.remove(accepted.run_id)
        view = service.run_status(accepted.run_id)
        assert view.state == "completed"
        assert view.workflow_name == "api-tool-demo"
        assert view.nodes["shout"].status == "completed"
        assert view.pending_inputs == []
        assert view.replay_source is None

    async def test_unknown_run_404(self, service):
        with pytest.raises(AgencyAPIError) as excinfo:
            service.run_status("20990101_000000")
        err = excinfo.value
        assert err.code == CODE_UNKNOWN_RUN
        assert err.http_status == 404
        assert err.details == {"run_id": "20990101_000000"}


class TestRunResult:
    async def test_terminal_result_matches_dag_run_result(self, service, tmp_path: Path):
        accepted, handle = await start_tool_run(service, tmp_path)
        view = service.run_result(accepted.run_id)
        assert view.run_id == accepted.run_id
        assert view.status == "completed"
        assert view.total_tokens == handle.result.total_tokens
        assert view.duration_seconds == pytest.approx(handle.result.duration_seconds)
        node = view.nodes[0]
        record = handle.result.nodes[0]
        assert node.node_id == record.node_id == "shout"
        assert node.status == "completed"
        assert node.output == "HI"
        assert node.tokens == record.tokens
        assert node.model == record.model
        assert node.attempt == record.attempt

    async def test_result_reconstructed_from_disk_when_handle_gone(self, service, tmp_path: Path):
        accepted, handle = await start_tool_run(service, tmp_path)
        service.registry.remove(accepted.run_id)
        view = service.run_result(accepted.run_id)
        assert view.run_id == accepted.run_id
        assert view.status == "completed"
        assert view.total_tokens == handle.result.total_tokens
        node = view.nodes[0]
        assert node.node_id == "shout"
        assert node.status == "completed"
        assert node.output == "HI"

    async def test_non_terminal_run_409(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "hil.yaml", HIL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.pending_inputs, message="pending input")
        try:
            with pytest.raises(AgencyAPIError) as excinfo:
                service.run_result(accepted.run_id)
            err = excinfo.value
            assert err.code == CODE_RUN_NOT_FINISHED
            assert err.http_status == 409
            assert err.details == {"run_id": accepted.run_id, "state": "running"}
        finally:
            service.submit_input(accepted.run_id, "gate", "yes")
            await wait_until(lambda: handle.result is not None, message="run to finish")


class TestSubmitInput:
    async def test_resolves_pending_input(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "hil.yaml", HIL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.pending_inputs, message="pending input")
        service.submit_input(accepted.run_id, "gate", "yes")
        await wait_until(lambda: handle.result is not None, message="run to finish")
        assert handle.state == "completed"
        gate = next(r for r in handle.result.nodes if r.node_id == "gate")
        assert gate.status == "completed"
        assert gate.output == "yes"

    async def test_duplicate_input_409(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "hil.yaml", HIL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.pending_inputs, message="pending input")
        service.submit_input(accepted.run_id, "gate", "yes")
        with pytest.raises(AgencyAPIError) as excinfo:
            service.submit_input(accepted.run_id, "gate", "no")
        err = excinfo.value
        assert err.code == CODE_INPUT_NOT_AWAITING
        assert err.http_status == 409
        assert err.details == {"run_id": accepted.run_id, "node_id": "gate"}
        await wait_until(lambda: handle.result is not None, message="run to finish")
        gate = next(r for r in handle.result.nodes if r.node_id == "gate")
        assert gate.output == "yes"

    async def test_unknown_run_404(self, service):
        with pytest.raises(AgencyAPIError) as excinfo:
            service.submit_input("20990101_000000", "gate", "yes")
        err = excinfo.value
        assert err.code == CODE_UNKNOWN_RUN
        assert err.http_status == 404
        assert err.details == {"run_id": "20990101_000000"}

    async def test_unknown_node_404(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "hil.yaml", HIL_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.pending_inputs, message="pending input")
        try:
            with pytest.raises(AgencyAPIError) as excinfo:
                service.submit_input(accepted.run_id, "nope", "yes")
            err = excinfo.value
            assert err.code == CODE_UNKNOWN_NODE
            assert err.http_status == 404
            assert err.details == {"run_id": accepted.run_id, "node_id": "nope"}
        finally:
            service.submit_input(accepted.run_id, "gate", "yes")
            await wait_until(lambda: handle.result is not None, message="run to finish")


class TestReplayRun:
    async def _completed_source(self, service: RunService, tmp_path: Path):
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        accepted = await service.start_run(str(wf))
        handle = service.registry.get(accepted.run_id)
        await wait_until(lambda: handle.result is not None, message="source run to finish")
        assert handle.result.status == "completed"
        return wf, accepted

    async def test_success_returns_accepted_replay_and_restores_prefix(
        self, service: RunService, tmp_path: Path
    ):
        wf, source = await self._completed_source(service, tmp_path)
        accepted = await service.replay_run(str(wf), "shout")
        assert accepted.state == "running"
        assert accepted.checkpoint == "shout"
        assert accepted.source_run == source.run_id
        assert accepted.run_id != source.run_id
        handle = service.registry.get(accepted.run_id)
        assert handle is not None
        assert handle.replay_source is not None
        assert handle.replay_source.source_run_id == source.run_id
        assert handle.replay_source.checkpoint == "shout"
        view = await wait_for_result(service, accepted.run_id)
        # The pre-generated run id names the run folder replay_checkpoint creates.
        meta = json.loads(
            (service.log_dir / accepted.run_id / "metadata.json").read_text(encoding="utf-8")
        )
        assert meta["run_id"] == accepted.run_id
        assert meta["replay_source"] == {
            "source_run_id": source.run_id,
            "checkpoint_node": "shout",
        }
        # Restored prefix is byte-for-byte from the source run's artifacts.
        src_out = (service.log_dir / source.run_id / "outputs" / "shout.txt").read_bytes()
        new_out = (service.log_dir / accepted.run_id / "outputs" / "shout.txt").read_bytes()
        assert src_out == new_out == b"HELLO"
        assert view.status == "completed"
        by_id = {n.node_id: n for n in view.nodes}
        assert set(by_id) == {"shout", "echo"}
        assert by_id["shout"].status == "completed"
        assert by_id["shout"].output == "HELLO"
        assert by_id["echo"].output == "tail"
        # Handle retired on completion: terminal views served from durable artifacts.
        assert service.registry.get(accepted.run_id) is None
        status = service.run_status(accepted.run_id)
        assert status.state == "completed"
        assert status.replay_source is not None
        assert status.replay_source.source_run_id == source.run_id
        assert status.replay_source.checkpoint == "shout"

    async def test_explicit_source_run_is_honored(self, service: RunService, tmp_path: Path):
        wf, source = await self._completed_source(service, tmp_path)
        accepted = await service.replay_run(str(wf), "shout", source_run=source.run_id)
        assert accepted.source_run == source.run_id
        view = await wait_for_result(service, accepted.run_id)
        assert view.status == "completed"

    async def test_no_eligible_source_is_404_without_side_effects(
        self, service: RunService, tmp_path: Path
    ):
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.replay_run(str(wf), "shout")
        err = excinfo.value
        assert err.code == CODE_NO_REPLAY_SOURCE
        assert err.http_status == 404
        assert err.details == {"checkpoint": "shout", "source_run": None}
        assert list(service.log_dir.iterdir()) == []

    async def test_named_source_missing_is_404_without_side_effects(
        self, service: RunService, tmp_path: Path
    ):
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        missing = "20990101_000000"
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.replay_run(str(wf), "shout", source_run=missing)
        err = excinfo.value
        assert err.code == CODE_NO_REPLAY_SOURCE
        assert err.http_status == 404
        assert err.details == {"checkpoint": "shout", "source_run": missing}
        assert list(service.log_dir.iterdir()) == []

    async def test_named_source_ineligible_is_404_without_side_effects(
        self, service: RunService, tmp_path: Path
    ):
        # A completed run of a *different* workflow is not a valid source.
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        other = write_workflow(tmp_path, "other.yaml", TOOL_WORKFLOW)
        source = await service.start_run(str(other))
        handle = service.registry.get(source.run_id)
        await wait_until(lambda: handle.result is not None, message="run to finish")
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.replay_run(
                str(wf), "shout", source_run=source.run_id
            )
        err = excinfo.value
        assert err.code == CODE_NO_REPLAY_SOURCE
        assert err.http_status == 404
        assert err.details["checkpoint"] == "shout"
        assert err.details["source_run"] == source.run_id
        # No replay run dir created (only the unrelated source run exists).
        assert [p.name for p in service.log_dir.iterdir()] == [source.run_id]

    async def test_unknown_checkpoint_node_is_404_without_side_effects(
        self, service: RunService, tmp_path: Path
    ):
        wf, source = await self._completed_source(service, tmp_path)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.replay_run(str(wf), "nope")
        err = excinfo.value
        assert err.code == CODE_NO_REPLAY_SOURCE
        assert err.http_status == 404
        assert err.details == {"checkpoint": "nope", "source_run": None}
        assert [p.name for p in service.log_dir.iterdir()] == [source.run_id]

    async def test_provision_error_is_409_without_side_effects(
        self, log_dir: Path, tmp_path: Path
    ):
        service = RunService(
            ServiceConfig(log_dir=log_dir), RunRegistry(), provision=failing_provision
        )
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.replay_run(str(wf), "shout")
        err = excinfo.value
        assert err.code == CODE_MODEL_VALIDATION_FAILED
        assert err.http_status == 409
        assert list(log_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Execution-environment request fields (spec 006, T021)
# ---------------------------------------------------------------------------

LOCAL_TOOL_WORKFLOW = TOOL_WORKFLOW + "\nexecution_environment: local\n"


class _FakeSandboxBackend:
    """Marker backend standing in for a provisioned DockerSandboxBackend."""


_SANDBOX_BACKEND = _FakeSandboxBackend()


def _sandbox_gate(failures=None):
    """Async stand-in for provision_sandbox: records calls, returns the marker
    backend, or raises ProvisionError when *failures* is set."""
    calls: list = []

    async def provision_sandbox(policy, image_root, run_id, **kwargs):
        calls.append((policy, str(image_root), run_id, kwargs))
        if failures:
            raise ProvisionError(list(failures))
        return _SANDBOX_BACKEND

    return calls, provision_sandbox


def _build_spy():
    backends: list = []
    real_build = build_workflow_tool_registry

    def build_spy(workflow, *, execution_backend=None):
        backends.append(execution_backend)
        return real_build(workflow, execution_backend=execution_backend)

    return backends, build_spy


class TestSandboxEnvironment:
    async def test_request_models_gain_environment_and_sandbox_fields(self):
        from agency.api.schemas import SandboxRequest

        req = StartRunRequest(
            workflow="/w.yaml",
            environment="sandbox",
            sandbox={
                "cpu": 1.5,
                "memory": "256MB",
                "pids_limit": 128,
                "timeout_seconds": 60,
                "output_limit_bytes": 123456,
                "scratch_size": "8MB",
            },
        )
        assert req.environment == "sandbox"
        assert isinstance(req.sandbox, SandboxRequest)
        assert req.sandbox.cpu == 1.5
        assert req.sandbox.memory == "256MB"
        assert req.sandbox.pids_limit == 128
        assert req.sandbox.timeout_seconds == 60
        assert req.sandbox.output_limit_bytes == 123456
        assert req.sandbox.scratch_size == "8MB"
        # Additive: absent fields stay None.
        assert StartRunRequest(workflow="/w.yaml").environment is None
        assert StartRunRequest(workflow="/w.yaml").sandbox is None
        rep = ReplayRequest(workflow="/w.yaml", checkpoint="shout", environment="local")
        assert rep.environment == "local"
        assert rep.sandbox is None
        with pytest.raises(ValidationError):
            StartRunRequest(workflow="/w.yaml", environment="bogus")

    async def test_invalid_environment_is_invalid_request(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        with pytest.raises(AgencyAPIError) as excinfo:
            await service.start_run(str(wf), environment="bogus")
        err = excinfo.value
        assert err.code == CODE_INVALID_REQUEST
        assert err.http_status == 422
        assert list(service.log_dir.iterdir()) == []

    async def test_sandbox_gate_failure_is_sandbox_unavailable_and_starts_nothing(
        self, log_dir: Path, tmp_path: Path
    ):
        service = RunService(
            ServiceConfig(log_dir=log_dir), RunRegistry(), provision=ok_provision
        )
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        failures = ["docker runtime unavailable: no daemon at /var/run/docker.sock"]
        calls, fake_gate = _sandbox_gate(failures=failures)
        with (
            patch("agency.api.service.provision_sandbox", new=fake_gate),
            patch("agency.api.service.teardown") as teardown,
            pytest.raises(AgencyAPIError) as excinfo,
        ):
            await service.start_run(str(wf), environment="sandbox")
        err = excinfo.value
        assert err.code == CODE_SANDBOX_UNAVAILABLE
        assert err.http_status == 409
        assert err.details == {"failures": failures}
        assert len(calls) == 1
        # No run was started: no run dir, no registry entry.
        assert list(log_dir.iterdir()) == []
        # The model-gate ProvisionedRun is torn down, not leaked.
        teardown.assert_called_once()

    async def test_local_environment_skips_gate_and_uses_local_backend(
        self, service, tmp_path: Path
    ):
        # Workflow says local.
        wf_local = write_workflow(tmp_path, "local.yaml", LOCAL_TOOL_WORKFLOW)
        calls, fake_gate = _sandbox_gate()
        backends, build_spy = _build_spy()
        with (
            patch("agency.api.service.provision_sandbox", new=fake_gate),
            patch("agency.api.service.build_workflow_tool_registry", new=build_spy),
        ):
            accepted = await service.start_run(str(wf_local))
            handle = service.registry.get(accepted.run_id)
            await wait_until(lambda: handle.result is not None, message="run to finish")
        assert calls == []
        assert backends and all(isinstance(b, LocalToolBackend) for b in backends)
        assert handle.state == "completed"

        # Request-level local also skips the gate for a workflow with no env.
        wf_plain = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        calls, fake_gate = _sandbox_gate()
        backends, build_spy = _build_spy()
        with (
            patch("agency.api.service.provision_sandbox", new=fake_gate),
            patch("agency.api.service.build_workflow_tool_registry", new=build_spy),
        ):
            accepted = await service.start_run(str(wf_plain), environment="local")
            handle = service.registry.get(accepted.run_id)
            await wait_until(lambda: handle.result is not None, message="run to finish")
        assert calls == []
        assert backends and all(isinstance(b, LocalToolBackend) for b in backends)
        assert handle.state == "completed"

    async def test_default_environment_is_sandbox_and_request_caps_win(
        self, service, tmp_path: Path
    ):
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        calls, fake_gate = _sandbox_gate()
        backends, build_spy = _build_spy()
        with (
            patch("agency.api.service.provision_sandbox", new=fake_gate),
            patch("agency.api.service.build_workflow_tool_registry", new=build_spy),
        ):
            accepted = await service.start_run(
                str(wf),
                sandbox={
                    "cpu": 1.5,
                    "memory": "256MB",
                    "pids_limit": 128,
                    "timeout_seconds": 60,
                    "output_limit_bytes": 123456,
                    "scratch_size": "8MB",
                },
            )
            handle = service.registry.get(accepted.run_id)
            await wait_until(lambda: handle.result is not None, message="run to finish")
        assert len(calls) == 1
        policy, image_root, run_id, _ = calls[0]
        assert policy.cpu == 1.5
        assert policy.memory_bytes == 256 * 2**20
        assert policy.pids_limit == 128
        assert policy.timeout_seconds == 60.0
        assert policy.output_limit_bytes == 123456
        assert policy.scratch_size_bytes == 8 * 2**20
        assert run_id == accepted.run_id
        assert image_root.endswith("sandbox")
        assert backends and all(b is _SANDBOX_BACKEND for b in backends)
        assert handle.state == "completed"

    async def test_builtin_defaults_when_nothing_set(self, service, tmp_path: Path):
        wf = write_workflow(tmp_path, "tool.yaml", TOOL_WORKFLOW)
        calls, fake_gate = _sandbox_gate()
        with patch("agency.api.service.provision_sandbox", new=fake_gate):
            accepted = await service.start_run(str(wf))
            handle = service.registry.get(accepted.run_id)
            await wait_until(lambda: handle.result is not None, message="run to finish")
        assert len(calls) == 1
        policy, image_root, run_id, _ = calls[0]
        assert policy.cpu == 2.0
        assert policy.memory_bytes == 512 * 2**20
        assert policy.pids_limit == 256
        assert policy.timeout_seconds == 300.0
        assert policy.output_limit_bytes == 1 * 2**20
        assert policy.scratch_size_bytes == 64 * 2**20
        assert run_id == accepted.run_id
        assert image_root.endswith("sandbox")
        assert handle.state == "completed"

    async def test_replay_run_environment_fields(self, service, tmp_path: Path):
        # Source run first (environment-agnostic setup).
        wf = write_workflow(tmp_path, "replay.yaml", REPLAY_WORKFLOW)
        source = await service.start_run(str(wf))
        handle = service.registry.get(source.run_id)
        await wait_until(lambda: handle.result is not None, message="source run to finish")

        # Replay with explicit local: gate skipped, local backend, completes.
        calls, fake_gate = _sandbox_gate()
        backends, build_spy = _build_spy()
        with (
            patch("agency.api.service.provision_sandbox", new=fake_gate),
            patch("agency.api.service.build_workflow_tool_registry", new=build_spy),
        ):
            accepted = await service.replay_run(str(wf), "shout", environment="local")
            view = await wait_for_result(service, accepted.run_id)
        assert calls == []
        assert backends and all(isinstance(b, LocalToolBackend) for b in backends)
        assert view.status == "completed"

        # Replay with explicit sandbox: gate runs under the new run id.
        calls, fake_gate = _sandbox_gate()
        with patch("agency.api.service.provision_sandbox", new=fake_gate):
            accepted = await service.replay_run(str(wf), "shout", environment="sandbox")
            view = await wait_for_result(service, accepted.run_id)
        assert len(calls) == 1
        assert calls[0][2] == accepted.run_id
        assert view.status == "completed"

