"""provision_sandbox() gate tests (spec 006, US3 — T017).

No Docker daemon required: an injected fake client exposes exactly the
surface the gate touches (``ping``, ``images.list``, ``images.build``).
The gate either returns a ready :class:`DockerSandboxBackend` or raises the
same consolidated :class:`ProvisionError` the model gate raises — there is
no fallback to local (FR-014, SC-008).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agency.executor.execution_env import LocalToolBackend
from agency.resource_manager.provisioning import ProvisionError
from agency.resource_manager.sandbox import (
    DockerSandboxBackend,
    SandboxPolicy,
    provision_sandbox,
)

RUN_ID = "gate-run"
TAG = "agency-sandbox:v1"


@dataclass
class Scenario:
    """Scripted behavior of the fake runtime."""

    ping_error: Exception | None = None
    build_error: Exception | None = None
    image_present: bool = False


class _FakeImage:
    def __init__(self, tag: str) -> None:
        self.tags = [tag]


class _FakeImages:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def list(self, name: str | None = None) -> list:
        client = self._client
        client.calls.append(("images.list", name))
        if client.scenario.image_present:
            return [_FakeImage(TAG)]
        return []

    def build(self, path, tag: str | None = None, **kwargs) -> tuple:
        client = self._client
        client.calls.append(("images.build", str(path), tag))
        if client.scenario.build_error is not None:
            raise client.scenario.build_error
        client.scenario.image_present = True
        return (_FakeImage(tag), [])


class _FakeClient:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls: list[tuple] = []
        self.ping_calls = 0
        self.images = _FakeImages(self)

    def ping(self) -> bool:
        self.ping_calls += 1
        if self.scenario.ping_error is not None:
            raise self.scenario.ping_error
        return True


def make_image_root(tmp_path: Path) -> Path:
    image_root = tmp_path / "sandbox"
    image_root.mkdir(parents=True, exist_ok=True)
    image_root.joinpath("Dockerfile").write_text("FROM debian:bookworm-slim\n")
    return image_root


async def provision(
    tmp_path: Path, scenario: Scenario
) -> tuple[DockerSandboxBackend | None, _FakeClient]:
    """Run the gate with a fake client; return (backend or None, client)."""
    client = _FakeClient(scenario)
    backend = await provision_sandbox(
        SandboxPolicy(),
        make_image_root(tmp_path),
        RUN_ID,
        run_root=tmp_path / "runs",
        client=client,
    )
    return backend, client


# --- T017: gate failures collapse into one ProvisionError ---------------------


async def test_sdk_unavailable_single_provision_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(sys.modules, "docker", None)
    client = _FakeClient(Scenario())
    with pytest.raises(ProvisionError) as excinfo:
        await provision_sandbox(
            SandboxPolicy(),
            make_image_root(tmp_path),
            RUN_ID,
            run_root=tmp_path / "runs",
            client=client,
        )
    assert excinfo.value.failures == [
        "sandbox required but the docker client library is unavailable"
    ]
    assert client.ping_calls == 0  # short-circuits before the runtime check
    assert client.calls == []


async def test_ping_failure_single_provision_error(tmp_path: Path):
    scenario = Scenario(ping_error=RuntimeError("Is the docker daemon running?"))
    with pytest.raises(ProvisionError) as excinfo:
        await provision(tmp_path, scenario)
    assert len(excinfo.value.failures) == 1
    assert "docker runtime unavailable" in excinfo.value.failures[0]
    assert "Is the docker daemon running?" in excinfo.value.failures[0]


async def test_ping_failure_never_reaches_image_check(tmp_path: Path):
    client = _FakeClient(Scenario(ping_error=RuntimeError("no daemon")))
    with pytest.raises(ProvisionError):
        await provision_sandbox(
            SandboxPolicy(),
            make_image_root(tmp_path),
            RUN_ID,
            run_root=tmp_path / "runs",
            client=client,
        )
    assert client.ping_calls == 1
    assert client.calls == []  # never reaches the image check


async def test_build_failure_single_provision_error(tmp_path: Path):
    client = _FakeClient(
        Scenario(build_error=RuntimeError("pull access denied for debian"))
    )
    with pytest.raises(ProvisionError) as excinfo:
        await provision_sandbox(
            SandboxPolicy(),
            make_image_root(tmp_path),
            RUN_ID,
            run_root=tmp_path / "runs",
            client=client,
        )
    assert len(excinfo.value.failures) == 1
    assert f"could not build sandbox image '{TAG}'" in excinfo.value.failures[0]
    assert ("images.build", str(tmp_path / "sandbox"), TAG) in client.calls


# --- T017: image cache reuse (SC-007) -----------------------------------------


async def test_image_present_reused_without_rebuild(tmp_path: Path):
    backend, client = await provision(tmp_path, Scenario(image_present=True))
    assert isinstance(backend, DockerSandboxBackend)
    assert all(call[0] != "images.build" for call in client.calls)
    assert any(call[0] == "images.list" for call in client.calls)
    # the gate's client is the one the returned backend executes with
    assert backend._client is client


async def test_image_absent_is_built_from_image_root(tmp_path: Path):
    backend, client = await provision(tmp_path, Scenario(image_present=False))
    assert isinstance(backend, DockerSandboxBackend)
    assert client.calls[-1] == ("images.build", str(tmp_path / "sandbox"), TAG)


# --- T017: no fallback to local (FR-014, SC-008) ------------------------------


async def test_no_fallback_to_local_on_any_failure(tmp_path: Path):
    for scenario in (
        Scenario(ping_error=RuntimeError("no daemon")),
        Scenario(build_error=RuntimeError("build failed")),
    ):
        with pytest.raises(ProvisionError):
            await provision(tmp_path, scenario)
    # a passing gate returns the sandbox backend — never the local one
    backend, _ = await provision(tmp_path, Scenario(image_present=True))
    assert isinstance(backend, DockerSandboxBackend)
    assert not isinstance(backend, LocalToolBackend)
