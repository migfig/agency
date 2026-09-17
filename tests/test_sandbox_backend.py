"""DockerSandboxBackend tests (spec 006, US1 — T011).

No Docker daemon required: a fake client mirrors the exact API surface the
backend touches (containers.create, start/kill/reload/remove, attrs, and the
raw-socket exec path: api.exec_create/exec_start(socket=True)/exec_inspect,
with docker's multiplexed 8-byte-header frame protocol on the socket).
"""

from __future__ import annotations

import asyncio
import io
import struct
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agency.executor.execution_env import ToolCaps
from agency.resource_manager.sandbox import DockerSandboxBackend, SandboxPolicy

RUN_ID = "test-run"
SCRATCH = "/scratch"


def run(coro):
    return asyncio.run(coro)


@dataclass
class Scenario:
    """Scripted behavior of the fake daemon."""

    frames: list[tuple[int, bytes]] = field(default_factory=list)
    exit_code: int = 0
    oom_killed: bool = False
    hang: bool = False  # exec1 socket stays silent until it is killed
    copy_hang: bool = False  # the copy-out exec stays silent
    copy_exit_code: int | None = None
    create_error: Exception | None = None
    start_error: Exception | None = None
    scratch_files: dict[str, bytes] = field(default_factory=dict)
    bad_tar_members: list[str] = field(default_factory=list)


class _FakeSocket:
    """Emits docker multiplexed frames (8-byte header) from a scripted buffer."""

    def __init__(self, frames: list[tuple[int, bytes]], hang: bool) -> None:
        self._buf = b"".join(
            struct.pack(">BxxxL", sid, len(data)) + data for sid, data in frames
        )
        self._hang = hang
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, n: int) -> bytes:
        if self._buf:
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk
        if self._hang:
            raise TimeoutError("timed out")
        return b""


class _FakeContainer:
    def __init__(self, client: _FakeClient, create_kwargs: dict, scratch: Path) -> None:
        self.client = client
        self.create_kwargs = create_kwargs
        self.id = "fake-" + uuid.uuid4().hex[:12]
        self.scratch = scratch
        self.started = False
        self.killed = False
        self.remove_calls: list[bool] = []
        self._running = False

    @property
    def attrs(self) -> dict:
        scenario = self.client.scenario
        return {
            "State": {
                "Running": self._running,
                "ExitCode": 137 if self.killed else scenario.exit_code,
                "OOMKilled": scenario.oom_killed,
            }
        }

    def start(self) -> None:
        self.client.order.append("start")
        if self.client.scenario.start_error is not None:
            raise self.client.scenario.start_error
        self.started = True
        self._running = True
        for rel, data in self.client.scenario.scratch_files.items():
            path = self.scratch / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def kill(self) -> None:
        self.client.order.append("kill")
        self.killed = True
        self._running = False

    def reload(self) -> None:
        self.client.order.append("reload")

    def remove(self, force: bool = False) -> None:
        self.client.order.append("remove")
        self.remove_calls.append(force)


class _FakeContainers:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def create(self, **kwargs) -> _FakeContainer:
        client = self._client
        client.order.append("create")
        if client.scenario.create_error is not None:
            raise client.scenario.create_error
        client._scratch_n += 1
        scratch = client._scratch_root / f"scratch-{client._scratch_n}"
        scratch.mkdir(parents=True, exist_ok=True)
        client.container = _FakeContainer(client, kwargs, scratch)
        client.created.append(client.container)
        return client.container


class _FakeApi:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def exec_create(
        self, container_id: str, argv: list[str], workdir: str | None = None, **kwargs
    ) -> dict:
        client = self._client
        client.order.append("exec_create")
        spec = {
            "id": f"exec-{len(client.execs)}",
            "argv": list(argv),
            "workdir": workdir,
        }
        client.execs.append(spec)
        container = client.container
        # Emulate the file_write staging cp (the only exec that runs with env).
        if (
            container is not None
            and container.create_kwargs.get("environment")
            and argv[0] == "bash"
        ):
            env = container.create_kwargs["environment"]
            src = Path(env["AGENCY_SRC"])
            dst_rel = env["AGENCY_DST"][len(SCRATCH) + 1 :]
            dst = container.scratch / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        return {"Id": spec["id"]}

    def exec_start(self, exec_id: str, socket: bool = False, **kwargs):
        client = self._client
        client.order.append("exec_start")
        spec = next(e for e in client.execs if e["id"] == exec_id)
        argv = spec["argv"]
        scenario = client.scenario
        frames: list[tuple[int, bytes]] = []
        hang = False
        if argv[0] == "python3":
            scratch = client.container.scratch
            if "tarfile" in argv[2]:
                bio = io.BytesIO()
                with tarfile.open(fileobj=bio, mode="w") as tf:
                    tf.add(scratch, arcname="scratch")
                    for name in scenario.bad_tar_members:
                        info = tarfile.TarInfo(name)
                        info.size = 3
                        tf.addfile(info, io.BytesIO(b"bad"))
                frames = [(1, bio.getvalue())]
            else:
                path = argv[3]
                frames = [(1, (scratch / path[len(SCRATCH) + 1 :]).read_bytes())]
            hang = scenario.copy_hang
        else:
            frames = scenario.frames
            hang = scenario.hang
        client.last_socket = _FakeSocket(frames, hang)
        return client.last_socket

    def exec_inspect(self, exec_id: str) -> dict:
        client = self._client
        spec = next(e for e in client.execs if e["id"] == exec_id)
        container = client.container
        killed = container.killed if container is not None else False
        if spec["argv"][0] == "python3":
            code = (
                137
                if killed
                else (
                    client.scenario.copy_exit_code
                    if client.scenario.copy_exit_code is not None
                    else 0
                )
            )
        else:
            code = 137 if killed else client.scenario.exit_code
        return {"Running": False, "ExitCode": code}


class _FakeClient:
    def __init__(self, scenario: Scenario, scratch_root: Path) -> None:
        self.scenario = scenario
        self.order: list[str] = []
        self.execs: list[dict] = []
        self.container: _FakeContainer | None = None
        self.created: list[_FakeContainer] = []
        self.last_socket: _FakeSocket | None = None
        self._scratch_root = scratch_root
        self._scratch_n = 0
        self.containers = _FakeContainers(self)
        self.api = _FakeApi(self)


def make_backend(
    tmp_path: Path, scenario: Scenario | None = None, **policy_kwargs
) -> tuple[DockerSandboxBackend, _FakeClient]:
    scenario = scenario or Scenario()
    client = _FakeClient(scenario, tmp_path / "fake-scratch")
    policy = SandboxPolicy(**policy_kwargs)
    backend = DockerSandboxBackend(
        policy, RUN_ID, run_root=tmp_path / "runs", client=client
    )
    return backend, client


def artifact_dir(tmp_path: Path) -> Path:
    sandbox = tmp_path / "runs" / RUN_ID / "sandbox"
    dirs = sorted(sandbox.iterdir()) if sandbox.is_dir() else []
    assert len(dirs) == 1, f"expected exactly one execution dir, found {dirs}"
    return dirs[0]


# --- T012: SandboxPolicy ----------------------------------------------------


def test_policy_defaults():
    policy = SandboxPolicy()
    assert policy.cpu == 2.0
    assert policy.memory_bytes == 512 * 2**20
    assert policy.pids_limit == 256
    assert policy.timeout_seconds == 300.0
    assert policy.output_limit_bytes == 1 * 2**20
    assert policy.scratch_size_bytes == 64 * 2**20
    assert policy.image == "agency-sandbox:v1"
    assert policy.user == "1000"


def test_policy_resolved_caps():
    policy = SandboxPolicy()
    assert policy.resolved_caps(None) == (2.0, 512 * 2**20, 256)
    partial = ToolCaps(cpu=4.0)
    assert policy.resolved_caps(partial) == (4.0, 512 * 2**20, 256)
    full = ToolCaps(cpu=1.5, memory_bytes=1024, pids_limit=7)
    assert policy.resolved_caps(full) == (1.5, 1024, 7)


# --- T013: protocol ops return sandbox results --------------------------------


def test_shell_success(tmp_path):
    backend, client = make_backend(
        tmp_path, Scenario(frames=[(1, b"hello\n"), (2, b"warn\n")], exit_code=0)
    )
    result = run(backend.shell("echo hello"))
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.output == "hello\n"
    assert result.stderr == "warn\n"
    assert result.error is None
    assert result.kind == "shell"
    assert result.environment == "sandbox"
    assert result.command == "echo hello"
    assert result.duration_seconds >= 0.0
    as_dict = result.as_dict()
    assert as_dict["environment"] == "sandbox"
    assert "kind" not in as_dict
    # full lifecycle order: create -> start -> exec -> copy-out exec -> teardown
    order = client.order
    assert order[0] == "create"
    assert "start" in order and "exec_start" in order
    assert order.index("exec_create") >= 2  # the command exec
    assert order.index("remove") == len(order) - 1
    assert client.container.remove_calls == [True]


def test_shell_failed_exit_code(tmp_path):
    backend, _ = make_backend(
        tmp_path, Scenario(frames=[(1, b"out\n"), (2, b"err\n")], exit_code=7)
    )
    result = run(backend.shell("false"))
    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.error == "exited with code 7"
    assert result.output == "out\n"
    assert result.stderr == "err\n"
    assert result.environment == "sandbox"


# --- T015: capture + wall-clock bound ----------------------------------------


def test_shell_timeout_kills_container(tmp_path):
    backend, client = make_backend(
        tmp_path, Scenario(frames=[(1, b"partial\n")], hang=True)
    )
    result = run(backend.shell("sleep 300"))
    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.error == "exceeded 300s timeout"
    assert result.output == "partial\n"
    assert client.container.killed is True
    assert client.order.index("kill") < client.order.index("remove")
    assert client.container.remove_calls == [True]


def test_shell_timeout_explicit_override(tmp_path):
    backend, _ = make_backend(tmp_path, Scenario(hang=True))
    result = run(backend.shell("sleep 300", timeout_seconds=2.5))
    assert result.status == "timed_out"
    assert result.error == "exceeded 2.5s timeout"


def test_shell_oom_failed(tmp_path):
    backend, _ = make_backend(
        tmp_path, Scenario(frames=[(1, b"oom\n")], exit_code=137, oom_killed=True)
    )
    result = run(backend.shell("stress"))
    assert result.status == "failed"
    assert result.exit_code == 137
    assert result.error == "terminated by sandbox memory limit (OOMKilled)"
    assert result.output == "oom\n"


def test_shell_output_capped_per_stream(tmp_path):
    backend, _ = make_backend(
        tmp_path,
        Scenario(frames=[(1, b"x" * 40), (2, b"y" * 20)]),
        output_limit_bytes=16,
    )
    result = run(backend.shell("yes"))
    assert result.status == "success"
    assert result.output.startswith("x" * 16)
    assert "[output truncated: kept first 16 of 40 bytes]" in result.output
    assert result.stderr.startswith("y" * 16)
    assert "[output truncated: kept first 16 of 20 bytes]" in result.stderr


def test_copyout_within_wall_clock_bound(tmp_path):
    # exec1 succeeds, but the copy-out exec exceeds the remaining bound.
    backend, client = make_backend(
        tmp_path, Scenario(frames=[(1, b"ok\n")], exit_code=0, copy_hang=True)
    )
    result = run(backend.shell("echo ok"))
    assert result.status == "timed_out"
    assert result.output == "ok\n"
    assert client.container.killed is True
    assert client.container.remove_calls == [True]


# --- T014: container posture + input mounts -----------------------------------


def test_create_posture_defaults(tmp_path):
    backend, client = make_backend(tmp_path)
    run(backend.shell("true"))
    kwargs = client.container.create_kwargs
    assert kwargs["image"] == "agency-sandbox:v1"
    assert kwargs["command"] == ["sleep", "infinity"]
    assert kwargs["user"] == "1000"
    assert kwargs["labels"] == {"agency-sandbox": RUN_ID}
    assert kwargs["read_only"] is True
    assert kwargs["network_mode"] == "none"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["mem_limit"] == 512 * 2**20
    assert kwargs["pids_limit"] == 256
    assert kwargs["working_dir"] == SCRATCH
    assert kwargs["tmpfs"][SCRATCH].startswith("size=64M")
    assert "mode=1777" in kwargs["tmpfs"][SCRATCH]
    assert kwargs["tmpfs"]["/tmp"].startswith("size=32M")
    assert "mode=1777" in kwargs["tmpfs"]["/tmp"]
    assert kwargs["mounts"] == []


def test_caps_override_create_kwargs(tmp_path):
    backend, client = make_backend(tmp_path)
    run(
        backend.shell(
            "true", caps=ToolCaps(cpu=4.0, memory_bytes=1 * 2**30, pids_limit=1024)
        )
    )
    kwargs = client.container.create_kwargs
    assert kwargs["nano_cpus"] == 4_000_000_000
    assert kwargs["mem_limit"] == 1 * 2**30
    assert kwargs["pids_limit"] == 1024


def test_shell_working_dir_mounted_read_only(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    backend, client = make_backend(tmp_path)
    result = run(backend.shell("true", working_dir=str(wd)))
    assert result.status == "success"
    kwargs = client.container.create_kwargs
    assert kwargs["working_dir"] == str(wd)
    assert {
        "Type": "bind",
        "Source": str(wd),
        "Target": str(wd),
        "ReadOnly": True,
    } in kwargs["mounts"]


def test_shell_working_dir_missing(tmp_path):
    backend, client = make_backend(tmp_path)
    result = run(backend.shell("true", working_dir="/definitely/not/here"))
    assert result.status == "error"
    assert (
        result.error
        == "working directory not available in sandbox: '/definitely/not/here'"
    )
    assert result.environment == "sandbox"
    assert client.container is None


# --- T014: program input ------------------------------------------------------


def _program_file(
    tmp_path: Path, name: str, content: str, executable: bool = False
) -> Path:
    path = tmp_path / name
    path.write_text(content)
    if executable:
        import os

        os.chmod(path, 0o755)
    return path


def test_program_shell_script(tmp_path):
    p = _program_file(tmp_path, "script.sh", "#!/bin/bash\necho hi\n")
    backend, client = make_backend(tmp_path)
    result = run(backend.program(str(p)))
    assert result.status == "success"
    assert result.kind == "program"
    assert result.command == str(p)
    kwargs = client.container.create_kwargs
    assert kwargs["command"] == [
        "sleep",
        "infinity",
    ]  # keep-alive main; command runs as exec
    assert client.execs[0]["argv"] == ["bash", str(p)]
    assert {
        "Type": "bind",
        "Source": str(p),
        "Target": str(p),
        "ReadOnly": True,
    } in kwargs["mounts"]


def test_program_python_extension(tmp_path):
    p = _program_file(tmp_path, "run.py", "print('hi')\n")
    backend, client = make_backend(tmp_path)
    run(backend.program(str(p)))
    assert client.execs[0]["argv"] == ["python3", str(p)]


def test_program_explicit_interpreter(tmp_path):
    p = _program_file(tmp_path, "script.rb", "puts 'hi'\n")
    backend, client = make_backend(tmp_path)
    run(backend.program(str(p), interpreter="ruby"))
    assert client.execs[0]["argv"] == ["ruby", str(p)]


def test_program_executable_bit(tmp_path):
    p = _program_file(tmp_path, "runme.bin", "bytes", executable=True)
    backend, client = make_backend(tmp_path)
    run(backend.program(str(p)))
    assert client.execs[0]["argv"] == [str(p)]


def test_program_missing_host_file(tmp_path):
    missing = tmp_path / "nope.sh"
    backend, client = make_backend(tmp_path)
    result = run(backend.program(str(missing)))
    assert result.status == "error"
    assert result.error == f"program file not available in sandbox: '{missing}'"
    assert client.container is None


def test_program_no_interpreter_not_executable(tmp_path):
    p = _program_file(tmp_path, "runme.xyz", "data")
    backend, client = make_backend(tmp_path)
    result = run(backend.program(str(p)))
    assert result.status == "error"
    assert result.error == f"no interpreter for '{p}' and the file is not executable"
    assert client.container is None


# --- T016: artifact copy-out ---------------------------------------------------


def test_shell_artifacts_layout(tmp_path):
    backend, _ = make_backend(
        tmp_path,
        Scenario(scratch_files={"nested/data.txt": b"data", "top.txt": b"top"}),
    )
    result = run(backend.shell("touch files"))
    assert result.status == "success"
    art = artifact_dir(tmp_path)
    assert (art / "nested" / "data.txt").read_bytes() == b"data"
    assert (art / "top.txt").read_bytes() == b"top"


def test_copyout_rejects_traversal_members(tmp_path):
    backend, _ = make_backend(
        tmp_path,
        Scenario(
            scratch_files={"ok.txt": b"ok"},
            bad_tar_members=["../evil.txt", "/abs/evil2.txt"],
        ),
    )
    result = run(backend.shell("true"))
    assert result.status == "success"
    art = artifact_dir(tmp_path)
    assert sorted(p.name for p in art.rglob("*")) == ["ok.txt"]
    assert not (tmp_path / "runs" / RUN_ID / "evil.txt").exists()


def test_file_write_success(tmp_path):
    backend, client = make_backend(tmp_path)
    result = run(backend.file_write("sub/result.txt", "hello world"))
    assert result.status == "success"
    assert result.kind == "file_write"
    assert result.exit_code is None
    assert result.error is None
    assert result.command == "write to 'sub/result.txt'"
    assert result.environment == "sandbox"
    art = artifact_dir(tmp_path)
    host_path = art / "result.txt"
    assert host_path.read_text() == "hello world"
    assert result.output == str(host_path)
    # staging is cleaned up
    assert not (art / ".staging").exists()
    assert client.container.remove_calls == [True]


def test_file_write_relative_run_root_mount_source_absolute(
    tmp_path, monkeypatch
):
    # Regression: the default run_root is the relative Path("runs"); the
    # daemon rejects relative bind sources ("mount path must be absolute").
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(Scenario(), tmp_path / "fake-scratch")
    backend = DockerSandboxBackend(
        SandboxPolicy(), RUN_ID, run_root=Path("runs"), client=client
    )
    result = run(backend.file_write("rel.txt", "data"))
    assert result.status == "success"
    mounts = client.container.create_kwargs["mounts"]
    assert len(mounts) == 1
    assert Path(mounts[0]["Source"]).is_absolute()
    assert mounts[0]["Source"] == mounts[0]["Target"]


def test_file_write_absolute_path_uses_basename(tmp_path):
    backend, _ = make_backend(tmp_path)
    result = run(backend.file_write("/data/out/final.txt", "abc"))
    assert result.status == "success"
    art = artifact_dir(tmp_path)
    assert (art / "final.txt").read_text() == "abc"
    assert result.output == str(art / "final.txt")


def test_file_write_missing_artifact_is_error(tmp_path):
    # copy-out exec runs but exits non-zero -> artifact never materializes.
    backend, _ = make_backend(tmp_path, Scenario(copy_exit_code=1))
    result = run(backend.file_write("x.txt", "abc"))
    assert result.status == "error"
    assert "could not retrieve sandbox artifact" in (result.error or "")


# --- T016: teardown on every path ---------------------------------------------


def test_create_failure_is_error_without_remove(tmp_path):
    backend, client = make_backend(
        tmp_path, Scenario(create_error=RuntimeError("no image"))
    )
    result = run(backend.shell("true"))
    assert result.status == "error"
    assert "could not create sandbox container" in (result.error or "")
    assert client.container is None
    assert "remove" not in client.order


def test_start_failure_is_error_and_removes(tmp_path):
    backend, client = make_backend(tmp_path, Scenario(start_error=RuntimeError("boom")))
    result = run(backend.shell("true"))
    assert result.status == "error"
    assert "could not start sandbox container" in (result.error or "")
    assert client.container.remove_calls == [True]


def test_attach_failure_is_error_and_removes(tmp_path):
    # exec_start raising (socket never established) is a runtime failure.
    backend, client = make_backend(tmp_path, Scenario())

    def boom(exec_id, socket=False, **kwargs):
        raise OSError("socket down")

    client.api.exec_start = boom
    result = run(backend.shell("true"))
    assert result.status == "error"
    assert client.container.remove_calls == [True]


# --- T026: result contract (spec 006, US4) ------------------------------------

# Contract §6: the standard keys plus the single additive `environment`.
_CONTRACT_KEYS = {
    "status",
    "exit_code",
    "output",
    "stderr",
    "error",
    "duration_seconds",
    "command",
    "environment",
}


def test_sandboxed_shell_as_dict_keeps_contract_shape(tmp_path):
    backend, _ = make_backend(
        tmp_path, Scenario(frames=[(1, b"ok\n"), (2, b"err\n")], exit_code=0)
    )
    result = run(backend.shell("echo ok"))
    as_dict = result.as_dict()
    assert set(as_dict) == _CONTRACT_KEYS
    assert "kind" not in as_dict
    assert as_dict["environment"] == "sandbox"
    assert as_dict["status"] == "success"
    assert as_dict["exit_code"] == 0
    assert as_dict["output"] == "ok\n"
    assert as_dict["stderr"] == "err\n"
    assert as_dict["error"] is None
    assert as_dict["command"] == "echo ok"
    assert as_dict["duration_seconds"] >= 0.0


def test_sandboxed_failed_shell_as_dict_keeps_contract_shape(tmp_path):
    backend, _ = make_backend(
        tmp_path, Scenario(frames=[(1, b"out\n"), (2, b"err\n")], exit_code=7)
    )
    result = run(backend.shell("false"))
    as_dict = result.as_dict()
    assert set(as_dict) == _CONTRACT_KEYS
    assert as_dict["environment"] == "sandbox"
    assert as_dict["status"] == "failed"
    assert as_dict["exit_code"] == 7
    assert as_dict["error"] == "exited with code 7"
    assert as_dict["output"] == "out\n"
    assert as_dict["stderr"] == "err\n"


def test_sandboxed_result_carries_same_keys_as_local_result(tmp_path):
    # FR-017/SC-005 at the result level: the sandboxed result keeps exactly
    # the local standard keys; only the environment value differs.
    from agency.executor.exec_tools import execute_shell

    backend, _ = make_backend(tmp_path, Scenario(frames=[(1, b"hello\n")], exit_code=0))
    sandboxed = run(backend.shell("echo hello"))
    local = run(execute_shell("echo hello"))
    assert set(sandboxed.as_dict()) == set(local.as_dict())
    assert sandboxed.as_dict()["environment"] == "sandbox"
    assert local.as_dict()["environment"] == "local"


def test_capped_streams_carry_marker_in_as_dict(tmp_path):
    backend, _ = make_backend(
        tmp_path, Scenario(frames=[(1, b"x" * 40), (2, b"y" * 20)]), output_limit_bytes=16
    )
    result = run(backend.shell("yes"))
    as_dict = result.as_dict()
    assert as_dict["output"].startswith("x" * 16)
    assert "[output truncated: kept first 16 of 40 bytes]" in as_dict["output"]
    assert as_dict["stderr"].startswith("y" * 16)
    assert "[output truncated: kept first 16 of 20 bytes]" in as_dict["stderr"]


def test_sandboxed_file_write_output_is_plain_host_artifact_path(tmp_path):
    backend, _ = make_backend(tmp_path)
    result = run(backend.file_write("sub/result.txt", "hello world"))
    as_dict = result.as_dict()
    assert set(as_dict) == _CONTRACT_KEYS
    assert as_dict["environment"] == "sandbox"
    # The reference returned is the written path as a PLAIN string — the
    # retrievable host artifact location (contract §6/§7) — not a JSON report.
    output = result.output
    assert isinstance(output, str)
    assert not output.startswith("{")
    host_path = Path(output)
    assert host_path.is_file()
    assert host_path.read_text(encoding="utf-8") == "hello world"
    sandbox_root = tmp_path / "runs" / RUN_ID / "sandbox"
    assert host_path.parent.is_relative_to(sandbox_root)
    assert host_path.name == "result.txt"
    assert as_dict["output"] == output
    assert as_dict["exit_code"] is None
    assert as_dict["error"] is None


# --- T031: bounding (spec 006, US5) — labels + no retention (FR-016/SC-002) ---


def _assert_bounded(client: _FakeClient) -> None:
    """Every container the backend created must be labeled and force-removed."""
    for container in client.created:
        assert container.create_kwargs["labels"] == {"agency-sandbox": RUN_ID}
        assert container.remove_calls == [True], "a sandbox container was retained"


@pytest.mark.parametrize(
    "scenario,expect_container",
    [
        (Scenario(frames=[(1, b"ok\n")], exit_code=0), True),  # success
        (Scenario(frames=[(1, b"out\n")], exit_code=3), True),  # failed exit
        (Scenario(frames=[(1, b"partial\n")], hang=True), True),  # timeout
        (
            Scenario(frames=[(1, b"oom\n")], exit_code=137, oom_killed=True),
            True,  # OOM
        ),
        (Scenario(frames=[(1, b"ok\n")], copy_hang=True), True),  # copy-out timeout
        (Scenario(start_error=RuntimeError("boom")), True),  # start failure
        (Scenario(create_error=RuntimeError("no image")), False),  # create failure
    ],
    ids=[
        "success",
        "failed_exit",
        "timeout",
        "oom",
        "copyout_timeout",
        "start_failure",
        "create_failure",
    ],
)
def test_bounding_label_and_force_removal(tmp_path, scenario, expect_container):
    backend, client = make_backend(tmp_path, scenario)
    run(backend.shell("cmd"))
    if expect_container:
        assert client.created, "expected a container to be created"
        _assert_bounded(client)
    else:
        assert client.created == []


def test_bounding_attach_failure_label_and_force_removal(tmp_path):
    backend, client = make_backend(tmp_path, Scenario())

    def boom(exec_id, socket=False, **kwargs):
        raise OSError("socket down")

    client.api.exec_start = boom
    run(backend.shell("true"))
    assert client.created
    _assert_bounded(client)
