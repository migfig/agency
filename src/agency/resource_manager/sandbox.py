"""Docker sandbox backend (spec 006, US1): sandboxed tool execution.

The executor side (``agency.executor.execution_env``) never imports
``resource_manager`` (Constitution V) — this module imports only the
result/caps types from ``agency.executor`` (the allowed direction).

Per-execution mechanism (research R6/R8/R9, validated against the daemon):

- one container per execution: a keep-alive main process (``sleep
  infinity``) so the container (and its tmpfs scratch) outlives the command;
  the real command runs as an ``exec`` whose stream is drained over a raw
  socket (docker's multiplexed 8-byte-header frame protocol);
- the wall-clock bound is the socket timeout, so a silent or chatty command
  is killed at the bound (``container.kill()``) → ``timed_out``;
- the container's root filesystem is read-only with bounded tmpfs for
  ``/scratch`` (mode 1777, so uid 1000 can write it once a bind mount is
  present) and ``/tmp``;
- the daemon's archive endpoint cannot see runtime tmpfs content (``docker
  cp`` 404s on it), so artifact copy-out is a second exec that tars the
  scratch tree (or reads the single file_write artifact) to stdout, drained
  over the same socket protocol before ``container.remove(force=True)``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import tarfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agency.executor.exec_tools import ToolExecutionResult
from agency.executor.execution_env import ToolCaps, parse_size
from agency.resource_manager.provisioning import ProvisionError

if TYPE_CHECKING:
    from agency.yaml_engine.schema import Workflow

__all__ = [
    "DockerSandboxBackend",
    "SandboxPolicy",
    "build_sandbox_policy",
    "default_image_root",
    "provision_sandbox",
]

logger = logging.getLogger(__name__)

SCRATCH_DIR = "/scratch"
_SCRATCH_ARCNAME = "scratch"
_TMP_TMPFS_SPEC = "size=32M,mode=1777"
_KEEPALIVE_COMMAND = ["sleep", "infinity"]
_STAGING_DIRNAME = ".staging"
_STREAM_STDOUT = 1
_STREAM_STDERR = 2
_RECV_SIZE = 65536
_EXEC_SETTLE_ATTEMPTS = 50
_EXEC_SETTLE_SLEEP = 0.01
_TAR_SCRATCH_SCRIPT = (
    "import io, sys, tarfile\n"
    "buf = io.BytesIO()\n"
    "t = tarfile.open(fileobj=buf, mode='w')\n"
    "t.add('/scratch', arcname='scratch')\n"
    "t.close()\n"
    "sys.stdout.buffer.write(buf.getvalue())\n"
    "sys.stdout.buffer.flush()\n"
)
_READ_FILE_SCRIPT = (
    'import sys; sys.stdout.buffer.write(open(sys.argv[1], "rb").read())'
)
_FILE_WRITE_CP = f'mkdir -p {SCRATCH_DIR}/out && cp "$AGENCY_SRC" "$AGENCY_DST"'


class _DeadlineExceeded(Exception):
    """Internal: the wall-clock bound was reached while draining an exec stream."""


class _ArtifactError(Exception):
    """Internal: the artifact copy-out produced no usable artifact."""


@dataclass(frozen=True)
class SandboxPolicy:
    """Run-level sandbox bounds (contract §1.2 defaults).

    The layering built-in defaults ← workflow ``sandbox:`` ← run caps ←
    named-tool caps is applied by the run site; this dataclass holds the
    effective run-level policy. ``resolved_caps`` applies the final
    per-invocation :class:`ToolCaps` layer (contract §4).
    """

    cpu: float = 2.0
    memory_bytes: int = 512 * 2**20
    pids_limit: int = 256
    timeout_seconds: float = 300.0
    output_limit_bytes: int = 1 * 2**20
    scratch_size_bytes: int = 64 * 2**20
    image: str = "agency-sandbox:v1"
    user: str = "1000"

    def resolved_caps(self, caps: ToolCaps | None) -> tuple[float, int, int]:
        """(cpu, memory_bytes, pids_limit) with *caps* winning where set."""
        if caps is None:
            return self.cpu, self.memory_bytes, self.pids_limit
        return (
            caps.cpu if caps.cpu is not None else self.cpu,
            caps.memory_bytes if caps.memory_bytes is not None else self.memory_bytes,
            caps.pids_limit if caps.pids_limit is not None else self.pids_limit,
        )


def _fmt_seconds(value: float) -> str:
    return f"{value:g}"


def _scratch_tmpfs_spec(scratch_size_bytes: int) -> str:
    megabytes = max(1, math.ceil(scratch_size_bytes / 2**20))
    return f"size={megabytes}M,mode=1777"


def _ro_bind(path: Path) -> dict:
    # Docker requires absolute bind paths; resolve() also canonicalises
    # relative run_roots (the default "runs") before they hit the daemon.
    resolved = Path(path).resolve()
    return {"Type": "bind", "Source": str(resolved), "Target": str(resolved), "ReadOnly": True}


def _sock_recv(sock, n: int) -> bytes:
    """Read up to *n* bytes from an exec socket.

    docker-py hands back a ``socket.SocketIO`` (a :mod:`io` raw wrapper with
    ``read``); test fakes expose the raw socket (``recv``). Both return
    ``b""`` at EOF, which terminates the framing loop.
    """
    if hasattr(sock, "recv"):
        return sock.recv(n)
    return sock.read(n)


def _sock_set_deadline(sock, deadline: float) -> None:
    """Apply the wall-clock *deadline* as a socket timeout (real or fake)."""
    target = sock
    if not hasattr(target, "settimeout"):
        raw = getattr(target, "_sock", None)  # SocketIO wraps the socket.socket
        if raw is not None:
            target = raw
    if hasattr(target, "settimeout"):
        target.settimeout(max(0.001, deadline - time.monotonic()))


def _stream_frames(sock) -> Iterator[tuple[int, bytes]]:
    """Yield ``(stream_id, data)`` frames from a docker multiplexed socket.

    Protocol: ``[1B stream id][3B zero][4B big-endian size] + payload``
    (stream 1 = stdout, 2 = stderr). A clean EOF terminates; a partial
    header at EOF is dropped. The socket must have a timeout set by the
    caller (it is the wall-clock bound).
    """
    buf = b""
    while True:
        if len(buf) < 8:
            chunk = _sock_recv(sock, _RECV_SIZE)
            if not chunk:
                return
            buf += chunk
            continue
        size = int.from_bytes(buf[4:8], "big")
        if len(buf) < 8 + size:
            chunk = _sock_recv(sock, _RECV_SIZE)
            if not chunk:
                return
            buf += chunk
            continue
        yield buf[0], buf[8 : 8 + size]
        buf = buf[8 + size :]


def _extract_scratch_tar(data: bytes, dest: Path) -> None:
    """Extract a scratch-tree tar (arcname ``scratch``) under *dest*, guarding traversal."""
    dest_resolved = dest.resolve()
    prefix = _SCRATCH_ARCNAME + "/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if not name.startswith(prefix):
                logger.warning(
                    "skipping scratch artifact member outside prefix: %r", name
                )
                continue
            rel = name[len(prefix) :]
            parts = PurePosixPath(rel).parts
            if not rel or rel.startswith("/") or ".." in parts:
                logger.warning("skipping unsafe scratch artifact member '%s'", name)
                continue
            target = dest / Path(*parts)
            if not target.resolve().is_relative_to(dest_resolved):
                logger.warning("skipping unsafe scratch artifact member '%s'", name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            target.write_bytes(extracted.read())


class _StreamSink:
    """Per-stream capture with an optional byte cap (marker appended on overflow)."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.total = 0
        self.buf = bytearray()

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.limit is None:
            self.buf.extend(chunk)
            return
        room = self.limit - len(self.buf)
        if room > 0:
            self.buf.extend(chunk[:room])

    def text(self) -> str:
        decoded = self.buf.decode(errors="replace")
        if self.limit is not None and self.total > self.limit:
            decoded += (
                f"[output truncated: kept first {self.limit} of {self.total} bytes]"
            )
        return decoded


class DockerSandboxBackend:
    """Sandboxed tool execution: one disposable Docker container per call.

    Mirrors the :class:`ToolExecutionBackend` protocol; results carry
    ``environment="sandbox"`` (contract §6).
    """

    def __init__(
        self,
        policy: SandboxPolicy,
        run_id: str,
        *,
        run_root: Path | str = Path("runs"),
        client=None,
    ) -> None:
        self._policy = policy
        self._run_id = run_id
        self._run_root = Path(run_root)
        if client is None:
            import docker  # deferred: keeps this module importable without the SDK

            client = docker.from_env()
        self._client = client

    async def shell(
        self,
        command: str,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        return await asyncio.to_thread(
            self._shell_blocking, command, working_dir, timeout_seconds, caps
        )

    async def program(
        self,
        program: str,
        interpreter: str | None = None,
        working_dir: str | None = None,
        timeout_seconds: float | None = None,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        return await asyncio.to_thread(
            self._program_blocking,
            program,
            interpreter,
            working_dir,
            timeout_seconds,
            caps,
        )

    async def file_write(
        self,
        path: str,
        content: str,
        caps: ToolCaps | None = None,
    ) -> ToolExecutionResult:
        return await asyncio.to_thread(self._file_write_blocking, path, content, caps)

    # -- blocking cores -------------------------------------------------------

    def _shell_blocking(
        self,
        command: str,
        working_dir: str | None,
        timeout_seconds: float | None,
        caps: ToolCaps | None,
    ) -> ToolExecutionResult:
        argv = ["bash", "-c", command]
        return self._run_blocking(
            "shell",
            command,
            argv,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            caps=caps,
            artifact=None,
        )

    def _program_blocking(
        self,
        program: str,
        interpreter: str | None,
        working_dir: str | None,
        timeout_seconds: float | None,
        caps: ToolCaps | None,
    ) -> ToolExecutionResult:
        start = time.monotonic()
        if not os.path.isfile(program):
            return self._error_result(
                "program",
                program,
                f"program file not available in sandbox: '{program}'",
                start,
            )
        if interpreter:
            argv = [interpreter, program]
        else:
            suffix = os.path.splitext(program)[1].lower()
            if suffix == ".py":
                argv = ["python3", program]
            elif suffix == ".sh":
                argv = ["bash", program]
            elif os.access(program, os.X_OK):
                argv = [program]
            else:
                return self._error_result(
                    "program",
                    program,
                    f"no interpreter for '{program}' and the file is not executable",
                    start,
                )
        return self._run_blocking(
            "program",
            program,
            argv,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            caps=caps,
            artifact=None,
            inputs=(Path(program),),
        )

    def _file_write_blocking(
        self, path: str, content: str, caps: ToolCaps | None
    ) -> ToolExecutionResult:
        start = time.monotonic()
        command = f"write to '{path}'"
        basename = Path(path).name
        if not basename:
            return self._error_result(
                "file_write",
                command,
                f"cannot resolve an artifact name from path '{path}'",
                start,
            )
        return self._run_blocking(
            "file_write",
            command,
            ["bash", "-c", _FILE_WRITE_CP],
            working_dir=None,
            timeout_seconds=None,
            caps=caps,
            artifact=("file", basename, content),
        )

    def _run_blocking(
        self,
        kind: str,
        command: str,
        argv: list[str],
        *,
        working_dir: str | None,
        timeout_seconds: float | None,
        caps: ToolCaps | None,
        artifact: tuple | None,
        inputs: tuple[Path, ...] = (),
    ) -> ToolExecutionResult:
        start = time.monotonic()
        if working_dir is not None and not os.path.isdir(working_dir):
            return self._error_result(
                kind,
                command,
                f"working directory not available in sandbox: '{working_dir}'",
                start,
            )
        policy = self._policy
        effective_timeout = (
            policy.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        eff_cpu, eff_mem, eff_pids = policy.resolved_caps(caps)
        exec_uuid = uuid.uuid4().hex
        # resolve() so a relative run_root (the default "runs") yields
        # absolute bind sources and the AGENCY_SRC copy path matches the
        # mount target inside the container.
        artifact_dir = (self._run_root / self._run_id / "sandbox" / exec_uuid).resolve()
        staging_path: Path | None = None
        container = None
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            env: dict[str, str] | None = None
            if artifact is not None and artifact[0] == "file":
                basename, content = artifact[1], artifact[2]
                staging_dir = artifact_dir / _STAGING_DIRNAME
                staging_dir.mkdir(parents=True, exist_ok=True)
                staging_path = staging_dir / basename
                staging_path.write_bytes(content.encode("utf-8"))
                os.chmod(staging_path, 0o644)
                env = {
                    "AGENCY_SRC": str(staging_path),
                    "AGENCY_DST": f"{SCRATCH_DIR}/out/{basename}",
                }
            mounts: list[dict] = []
            if working_dir is not None:
                mounts.append(_ro_bind(Path(working_dir)))
            mounts.extend(_ro_bind(path) for path in inputs)
            if staging_path is not None:
                mounts.append(_ro_bind(staging_path))
            create_kwargs: dict = {
                "image": policy.image,
                "command": _KEEPALIVE_COMMAND,
                "working_dir": working_dir if working_dir is not None else SCRATCH_DIR,
                "user": policy.user,
                "labels": {"agency-sandbox": self._run_id},
                "read_only": True,
                "tmpfs": {
                    SCRATCH_DIR: _scratch_tmpfs_spec(policy.scratch_size_bytes),
                    "/tmp": _TMP_TMPFS_SPEC,
                },
                "network_mode": "none",
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "nano_cpus": round(eff_cpu * 1e9),
                "mem_limit": eff_mem,
                "pids_limit": eff_pids,
                "mounts": mounts,
            }
            if env is not None:
                create_kwargs["environment"] = env
            try:
                container = self._client.containers.create(**create_kwargs)
            except Exception as exc:  # noqa: BLE001 - any daemon failure is an error result, never a crash
                return self._error_result(
                    kind, command, f"could not create sandbox container: {exc}", start
                )
            try:
                container.start()
            except Exception as exc:  # noqa: BLE001 - any daemon failure is an error result, never a crash
                return self._error_result(
                    kind, command, f"could not start sandbox container: {exc}", start
                )
            api = self._client.api
            deadline = time.monotonic() + effective_timeout
            out_sink = _StreamSink(policy.output_limit_bytes)
            err_sink = _StreamSink(policy.output_limit_bytes)
            exec_workdir = working_dir if working_dir is not None else SCRATCH_DIR
            timed_out = False
            exec_id: str | None = None
            try:
                exec_id = self._drain_exec(
                    container, api, argv, exec_workdir, deadline, out_sink, err_sink
                )
            except _DeadlineExceeded:
                timed_out = self._kill_if_running(container)
            except Exception as exc:  # noqa: BLE001 - any daemon failure is an error result, never a crash
                return self._error_result(
                    kind, command, f"could not launch sandbox execution: {exc}", start
                )
            exit_code: int | None = None
            oom_killed = False
            if not timed_out and exec_id is not None:
                exit_code = self._exec_exit_code(api, exec_id)
                container.reload()
                oom_killed = bool(container.attrs["State"].get("OOMKilled", False))
            if not timed_out:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    try:
                        if artifact is not None and artifact[0] == "file":
                            data = self._copyout_file(
                                container, api, artifact[1], deadline
                            )
                            artifact_file = artifact_dir / artifact[1]
                            artifact_file.write_bytes(data)
                            if data != artifact[2].encode("utf-8"):
                                raise _ArtifactError(
                                    "artifact content mismatch after copy-out"
                                )
                        else:
                            self._copyout_tree(container, api, artifact_dir, deadline)
                    except _DeadlineExceeded:
                        self._kill_if_running(container)
                        timed_out = True
                    except _ArtifactError as exc:
                        return self._error_result(
                            kind,
                            command,
                            f"could not retrieve sandbox artifact: {exc}",
                            start,
                        )
                else:
                    logger.warning(
                        "sandbox run %s: wall-clock bound reached before artifact copy-out; artifacts not retrieved",
                        self._run_id,
                    )
            duration = time.monotonic() - start
            if timed_out:
                return ToolExecutionResult(
                    status="timed_out",
                    exit_code=None,
                    output=out_sink.text(),
                    stderr=err_sink.text(),
                    error=f"exceeded {_fmt_seconds(effective_timeout)}s timeout",
                    duration_seconds=duration,
                    command=command,
                    kind=kind,
                    environment="sandbox",
                )
            if oom_killed:
                return ToolExecutionResult(
                    status="failed",
                    exit_code=exit_code,
                    output=out_sink.text(),
                    stderr=err_sink.text(),
                    error="terminated by sandbox memory limit (OOMKilled)",
                    duration_seconds=duration,
                    command=command,
                    kind=kind,
                    environment="sandbox",
                )
            if exit_code == 0:
                if kind == "file_write":
                    return ToolExecutionResult(
                        status="success",
                        exit_code=None,
                        output=str(artifact_dir / artifact[1]),
                        stderr="",
                        error=None,
                        duration_seconds=duration,
                        command=command,
                        kind=kind,
                        environment="sandbox",
                    )
                return ToolExecutionResult(
                    status="success",
                    exit_code=0,
                    output=out_sink.text(),
                    stderr=err_sink.text(),
                    error=None,
                    duration_seconds=duration,
                    command=command,
                    kind=kind,
                    environment="sandbox",
                )
            return ToolExecutionResult(
                status="failed",
                exit_code=exit_code,
                output=out_sink.text(),
                stderr=err_sink.text(),
                error=f"exited with code {exit_code}",
                duration_seconds=duration,
                command=command,
                kind=kind,
                environment="sandbox",
            )
        except Exception as exc:
            logger.exception(
                "sandbox execution failed unexpectedly (kind=%s, command=%r)",
                kind,
                command,
            )
            return self._error_result(
                kind, command, f"sandbox execution error: {exc}", start
            )
        finally:
            if staging_path is not None:
                try:
                    staging_path.unlink(missing_ok=True)
                    staging_path.parent.rmdir()
                except OSError:
                    pass
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    logger.warning(
                        "could not remove sandbox container %s",
                        getattr(container, "id", "?"),
                        exc_info=True,
                    )

    # -- internals -------------------------------------------------------------

    def _drain_exec(
        self,
        container,
        api,
        argv: list[str],
        workdir: str,
        deadline: float,
        out_sink: _StreamSink,
        err_sink: _StreamSink,
    ) -> str:
        """Run *argv* as an exec and drain its stream until EOF or the bound.

        Returns the exec id. Raises :class:`_DeadlineExceeded` at the bound.
        """
        ex = api.exec_create(
            container.id, argv, workdir=workdir, stdout=True, stderr=True
        )
        exec_id = ex["Id"]
        sock = api.exec_start(exec_id, socket=True)
        frames = iter(_stream_frames(sock))
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise _DeadlineExceeded
            _sock_set_deadline(sock, deadline)
            try:
                stream_id, chunk = next(frames)
            except StopIteration:
                return exec_id
            except TimeoutError:
                raise _DeadlineExceeded from None
            if stream_id == _STREAM_STDOUT:
                out_sink.add(chunk)
            elif stream_id == _STREAM_STDERR:
                err_sink.add(chunk)

    def _drain_exec_bytes(
        self, container, api, argv: list[str], workdir: str, deadline: float
    ) -> tuple[bytes, str]:
        """Run *argv* as an exec and collect its stdout as raw bytes."""
        ex = api.exec_create(
            container.id, argv, workdir=workdir, stdout=True, stderr=True
        )
        exec_id = ex["Id"]
        sock = api.exec_start(exec_id, socket=True)
        out = bytearray()
        frames = iter(_stream_frames(sock))
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise _DeadlineExceeded
            _sock_set_deadline(sock, deadline)
            try:
                stream_id, chunk = next(frames)
            except StopIteration:
                return bytes(out), exec_id
            except TimeoutError:
                raise _DeadlineExceeded from None
            if stream_id == _STREAM_STDOUT:
                out.extend(chunk)

    def _copyout_tree(
        self, container, api, artifact_dir: Path, deadline: float
    ) -> None:
        tar_data, exec_id = self._drain_exec_bytes(
            container,
            api,
            ["python3", "-c", _TAR_SCRATCH_SCRIPT],
            SCRATCH_DIR,
            deadline,
        )
        code = self._exec_exit_code(api, exec_id)
        if code != 0:
            logger.warning(
                "sandbox scratch copy-out exited with code %s; artifacts not retrieved",
                code,
            )
            return
        _extract_scratch_tar(tar_data, artifact_dir)

    def _copyout_file(self, container, api, basename: str, deadline: float) -> bytes:
        target = f"{SCRATCH_DIR}/out/{basename}"
        data, exec_id = self._drain_exec_bytes(
            container,
            api,
            ["python3", "-c", _READ_FILE_SCRIPT, target],
            SCRATCH_DIR,
            deadline,
        )
        code = self._exec_exit_code(api, exec_id)
        if code != 0:
            raise _ArtifactError(f"scratch read-out exited with code {code}")
        return data

    @staticmethod
    def _exec_exit_code(api, exec_id: str) -> int:
        state = None
        for _ in range(_EXEC_SETTLE_ATTEMPTS):
            state = api.exec_inspect(exec_id)
            if not state.get("Running", False):
                break
            time.sleep(_EXEC_SETTLE_SLEEP)
        code = state.get("ExitCode") if state is not None else None
        return code if isinstance(code, int) else -1

    @staticmethod
    def _kill_if_running(container) -> bool:
        try:
            container.reload()
            if container.attrs["State"].get("Running", False):
                container.kill()
                return True
        except Exception:
            logger.warning(
                "could not check/kill sandbox container at the wall-clock bound",
                exc_info=True,
            )
        return False

    @staticmethod
    def _error_result(
        kind: str, command: str, reason: str, start: float
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            status="error",
            exit_code=None,
            output="",
            stderr="",
            error=reason,
            duration_seconds=time.monotonic() - start,
            command=command,
            kind=kind,
            environment="sandbox",
        )


def default_image_root() -> Path:
    """The repo-root ``sandbox/`` directory holding the shipped Dockerfile."""
    return Path(__file__).resolve().parents[3] / "sandbox"


def build_sandbox_policy(
    workflow: Workflow,
    *,
    cpu: float | None = None,
    memory: str | None = None,
    pids_limit: int | None = None,
    timeout_seconds: float | None = None,
    output_limit_bytes: int | None = None,
    scratch_size: str | None = None,
) -> SandboxPolicy:
    """Layer the run-level sandbox policy (contract §4): built-in defaults
    ← workflow ``sandbox:`` ← run caps. Any ``None`` cap inherits the next
    layer down; the workflow's ``sandbox:`` mapping is the only source of
    ``output_limit_bytes``/``scratch_size`` below the run caps (the CLI and
    API expose cpu/memory/pids/timeout only, contract §2/§3)."""
    defaults = SandboxPolicy()
    ws = workflow.sandbox
    if ws is not None:
        defaults = replace(
            defaults,
            cpu=ws.cpu if ws.cpu is not None else defaults.cpu,
            memory_bytes=parse_size(ws.memory) if ws.memory is not None else defaults.memory_bytes,
            pids_limit=ws.pids_limit if ws.pids_limit is not None else defaults.pids_limit,
            timeout_seconds=ws.timeout_seconds if ws.timeout_seconds is not None else defaults.timeout_seconds,
            output_limit_bytes=(
                ws.output_limit_bytes if ws.output_limit_bytes is not None else defaults.output_limit_bytes
            ),
            scratch_size_bytes=(
                parse_size(ws.scratch_size) if ws.scratch_size is not None else defaults.scratch_size_bytes
            ),
        )
    return replace(
        defaults,
        cpu=cpu if cpu is not None else defaults.cpu,
        memory_bytes=parse_size(memory) if memory is not None else defaults.memory_bytes,
        pids_limit=pids_limit if pids_limit is not None else defaults.pids_limit,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else defaults.timeout_seconds,
        output_limit_bytes=(
            output_limit_bytes if output_limit_bytes is not None else defaults.output_limit_bytes
        ),
        scratch_size_bytes=(
            parse_size(scratch_size) if scratch_size is not None else defaults.scratch_size_bytes
        ),
    )


async def provision_sandbox(
    policy: SandboxPolicy,
    image_root: str | Path,
    run_id: str,
    *,
    run_root: Path | str = Path("runs"),
    client=None,
) -> DockerSandboxBackend:
    """Fail-fast sandbox provisioning gate (research R11, FR-014).

    Runs at the run sites, immediately after the model gate and before any
    node executes, only when the resolved environment is ``sandbox`` (a
    ``local`` run skips the gate entirely). Checks, in order — each failure
    appending one line to a single list: (1) the ``docker`` SDK is
    importable, (2) the runtime answers ``ping()``, (3) the policy image is
    present in the store (reused, SC-007) or built from *image_root* (the
    shipped ``sandbox/`` directory holding the Dockerfile). Any failure
    raises the same :class:`ProvisionError` the model gate raises — there is
    no code path that falls back to local (SC-008).
    """
    failures: list[str] = []
    docker = None
    try:
        import docker  # deferred: SDK is a main dep but keeps the module importable without it
    except Exception:  # noqa: BLE001 - ImportError (or a poisoned sys.modules entry) means no SDK
        failures.append(
            "sandbox required but the docker client library is unavailable"
        )
    if docker is not None:
        if client is None:
            try:
                client = docker.from_env()
            except Exception as exc:  # noqa: BLE001 - any connect failure aborts the run
                failures.append(f"could not connect to the docker runtime: {exc}")
        if not failures:
            try:
                client.ping()
            except Exception as exc:  # noqa: BLE001 - daemon stopped / socket unreachable
                failures.append(f"docker runtime unavailable: {exc}")
        if not failures:
            tag = policy.image
            try:
                if not client.images.list(name=tag):
                    client.images.build(path=str(image_root), tag=tag)
            except Exception as exc:  # noqa: BLE001 - e.g. restricted network at build time
                failures.append(f"could not build sandbox image '{tag}': {exc}")
    if failures:
        raise ProvisionError(failures)
    return DockerSandboxBackend(policy, run_id, run_root=run_root, client=client)
