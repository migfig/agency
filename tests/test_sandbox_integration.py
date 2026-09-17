"""Real-container integration tests (spec 006, US1 — T034).

The spec's independent tests, run against a live Docker daemon (research
R12): the module skips as a whole when the ``docker`` SDK is unavailable or
the runtime does not answer ``ping()``. Every scenario drives the real
:class:`DockerSandboxBackend` (or :func:`provision_sandbox`) with a real
client and asserts the host-side effects too — files on the host, the
host's ``/etc`` untouched, and no labeled containers left behind
(FR-004/FR-016).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from agency.executor.execution_env import LocalToolBackend
from agency.resource_manager.sandbox import (
    DockerSandboxBackend,
    SandboxPolicy,
    default_image_root,
    provision_sandbox,
)

TAG = "agency-sandbox:v1"
_RESULT_KEYS = {
    "status",
    "exit_code",
    "output",
    "stderr",
    "error",
    "duration_seconds",
    "command",
    "environment",
}


def _runtime_available() -> tuple[bool, str]:
    """(usable, skip reason): False plus the reason when the module must skip."""
    try:
        import docker
    except ImportError:
        return False, "the docker SDK is unavailable"
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any connect failure means no daemon
        return False, f"the docker runtime is unavailable: {exc}"
    return True, ""


DOCKER_AVAILABLE, _SKIP_REASON = _runtime_available()
pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=_SKIP_REASON)


@pytest.fixture(scope="module")
def docker_client():
    import docker

    return docker.from_env()


def _run_id(name: str) -> str:
    return f"int-{name}-{uuid.uuid4().hex[:8]}"


def _backend(
    tmp_path: Path, run_id: str, client, **policy_kwargs
) -> DockerSandboxBackend:
    return DockerSandboxBackend(
        SandboxPolicy(**policy_kwargs), run_id, run_root=tmp_path, client=client
    )


def _no_containers_left(client, run_id: str) -> None:
    leftover = client.containers.list(
        all=True, filters={"label": f"agency-sandbox={run_id}"}
    )
    assert leftover == [], f"containers left behind: {[c.name for c in leftover]}"


# --- stdout capture + exit code -----------------------------------------------


async def test_stdout_capture_and_exit_codes(docker_client, tmp_path: Path):
    run_id = _run_id("capture")
    backend = _backend(tmp_path, run_id, docker_client)
    result = await backend.shell("echo stdout-captured && echo stderr-captured >&2")
    assert result.status == "success"
    assert result.exit_code == 0
    assert "stdout-captured" in result.output
    assert "stderr-captured" in result.stderr
    assert result.error is None
    assert result.environment == "sandbox"
    assert result.command == "echo stdout-captured && echo stderr-captured >&2"
    assert set(result.as_dict()) == _RESULT_KEYS
    _no_containers_left(docker_client, run_id)

    failing = await backend.shell("exit 7")
    assert failing.status == "failed"
    assert failing.exit_code == 7
    assert failing.error == "exited with code 7"
    _no_containers_left(docker_client, run_id)


# --- isolation: read-only rootfs, no network ---------------------------------


async def test_out_of_bounds_write_denied(docker_client, tmp_path: Path):
    run_id = _run_id("oob")
    backend = _backend(tmp_path, run_id, docker_client)
    marker = "/etc/agency-pwn"
    result = await backend.shell(
        f"if touch {marker} 2>/dev/null; then echo OOB-WRITE-POSSIBLE; "
        "else echo OOB-WRITE-DENIED; fi"
    )
    assert result.status == "success"
    assert "OOB-WRITE-DENIED" in result.output
    assert not Path(marker).exists()  # the host filesystem is untouched
    _no_containers_left(docker_client, run_id)


async def test_network_egress_blocked(docker_client, tmp_path: Path):
    run_id = _run_id("net")
    backend = _backend(tmp_path, run_id, docker_client)
    result = await backend.shell(
        "timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null "
        "&& echo NET-EGRESS-OPEN || echo NET-EGRESS-BLOCKED"
    )
    assert result.status == "success"
    assert "NET-EGRESS-BLOCKED" in result.output
    _no_containers_left(docker_client, run_id)


# --- bounds: wall-clock kill, memory OOM --------------------------------------


async def test_timeout_kill(docker_client, tmp_path: Path):
    run_id = _run_id("timeout")
    backend = _backend(tmp_path, run_id, docker_client, timeout_seconds=2.0)
    started = time.monotonic()
    result = await backend.shell("sleep 30")
    elapsed = time.monotonic() - started
    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.error == "exceeded 2s timeout"
    assert elapsed < 15.0  # killed at the bound — the run never hangs
    _no_containers_left(docker_client, run_id)


async def test_memory_oom_termination(docker_client, tmp_path: Path):
    run_id = _run_id("oom")
    backend = _backend(tmp_path, run_id, docker_client, memory_bytes=32 * 2**20)
    hog = (
        "python3 -c 'import time\n"
        "x = []\n"
        "while True:\n"
        "    x.append(bytes(1 << 20))\n"
        "    time.sleep(0.001)'"
    )
    result = await backend.shell(hog)
    assert result.status == "failed"  # terminated, not degraded
    assert result.exit_code == 137  # SIGKILL by the cgroup OOM killer
    assert "memory limit" in (result.error or "")
    assert result.duration_seconds < 60.0
    _no_containers_left(docker_client, run_id)


# --- artifacts: copy-out retrievability ---------------------------------------


async def test_artifact_copyout_retrievability(docker_client, tmp_path: Path):
    run_id = _run_id("artifacts")
    backend = _backend(tmp_path, run_id, docker_client)

    written = await backend.file_write("/scratch/out/notes.txt", "hello artifact")
    assert written.status == "success"
    artifact = Path(written.output)  # the host path, plain string
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == "hello artifact"
    assert run_id in str(artifact) and "sandbox" in artifact.parts
    _no_containers_left(docker_client, run_id)

    ran = await backend.shell(
        "mkdir -p sub && echo nested > sub/file.txt && echo top > top.txt"
    )
    assert ran.status == "success"
    sandbox_root = tmp_path / run_id / "sandbox"
    nested = list(sandbox_root.rglob("file.txt"))
    assert nested and all(p.read_text().strip() == "nested" for p in nested)
    top = list(sandbox_root.rglob("top.txt"))
    assert top and all(p.read_text().strip() == "top" for p in top)
    _no_containers_left(docker_client, run_id)


# --- image cache reuse (SC-007) + cold start (SC-011) -------------------------


class _RecordingImages:
    def __init__(self, real) -> None:
        self._real = real
        self.build_calls: list[str | None] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name == "build":

            def build(*args, **kwargs):
                self.build_calls.append(kwargs.get("tag"))
                return attr(*args, **kwargs)

            return build
        return attr


class _RecordingClient:
    """Delegates to the real client; records ``images.build`` calls."""

    def __init__(self, real) -> None:
        self._real = real
        self.images = _RecordingImages(real.images)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_image_cache_reuse(docker_client, tmp_path: Path):
    recording = _RecordingClient(docker_client)
    present_before = bool(docker_client.images.list(name=TAG))
    first = await provision_sandbox(
        SandboxPolicy(),
        default_image_root(),
        _run_id("cache-a"),
        run_root=tmp_path,
        client=recording,
    )
    second = await provision_sandbox(
        SandboxPolicy(),
        default_image_root(),
        _run_id("cache-b"),
        run_root=tmp_path,
        client=recording,
    )
    assert isinstance(first, DockerSandboxBackend)
    assert isinstance(second, DockerSandboxBackend)
    if present_before:
        assert recording.images.build_calls == []  # cached: no rebuild (SC-007)
    else:
        assert recording.images.build_calls == [TAG]  # only the first gate built


async def test_cold_start_from_cached_image(docker_client, tmp_path: Path):
    run_id = _run_id("cold")
    backend = _backend(tmp_path, run_id, docker_client)
    result = await backend.shell("echo cold-start")
    assert result.status == "success"
    # create+start+exec+teardown from the cached image is seconds, not minutes
    assert result.duration_seconds < 30.0
    _no_containers_left(docker_client, run_id)


# --- local mode regression (SC-010) -------------------------------------------


async def test_local_mode_regression(tmp_path: Path):
    backend = LocalToolBackend()
    result = await backend.shell("echo local-echo")
    assert result.status == "success"
    assert result.exit_code == 0
    assert "local-echo" in result.output
    assert result.environment == "local"
    as_dict = result.as_dict()
    assert as_dict["environment"] == "local"
    assert set(as_dict) == _RESULT_KEYS

    target = tmp_path / "local-file.txt"
    written = await backend.file_write(str(target), "local content")
    assert written.status == "success"
    assert written.environment == "local"
    assert written.output == str(target)  # the host path, written directly
    assert target.read_text(encoding="utf-8") == "local content"
