"""Tests for the pre-execution model validation/provisioning pass (Story 5.1)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agency.core.llamacpp_backend import LlamaCppError
from agency.resource_manager import BackendRouter
from agency.resource_manager.provisioning import (
    ProvisionError,
    get_models_dirs,
    load_cli_models,
    provision,
    resolve_model_path,
    teardown,
)
from agency.resource_manager.vram_tracker import parse_vram_size
from agency.yaml_engine.schema import AgentNode, MergeNode, ModelSpec, Workflow


def _agent(
    model: str,
    node_id: str | None = None,
    fallback: str | None = None,
) -> AgentNode:
    return AgentNode(
        id=node_id or model,
        type="agent",
        model=model,
        prompt_template="p",
        fallback=fallback,
    )


def _agent_tools(
    model: str,
    node_id: str | None = None,
    fallback: str | None = None,
) -> AgentNode:
    """An agent node that has opted into model-driven tool calling."""
    return AgentNode(
        id=node_id or model,
        type="agent",
        model=model,
        prompt_template="p",
        fallback=fallback,
        tools={},
    )


def _wf(nodes: dict[str, AgentNode], models: dict[str, ModelSpec]) -> Workflow:
    return Workflow(name="wf", nodes=nodes, entry_point=next(iter(nodes)), models=models)


def _set_probe(monkeypatch, reachable: dict[str, bool] | None = None) -> None:
    mapping = reachable or {}

    async def fake_probe(endpoint: str, timeout: float = 5.0) -> bool:
        return mapping.get(endpoint, True)

    monkeypatch.setattr("agency.resource_manager.provisioning._probe_reachable", fake_probe)


def _fake_server(
    monkeypatch,
    fail_ports: set[int] | None = None,
) -> tuple[list, list]:
    fail = fail_ports or set()
    started: list = []
    shutdowns: list = []

    class FakeServer:
        def __init__(self, port=None, model_path=None, **kwargs):
            self.port = port
            self.model_path = model_path
            self.shutdown_called = False

        def start(self):
            if self.port in fail:
                raise LlamaCppError(f"startup failed on port {self.port}")
            started.append(self)

        def shutdown(self):
            self.shutdown_called = True
            shutdowns.append(self)

    monkeypatch.setattr("agency.resource_manager.provisioning.LlamaCppServer", FakeServer)
    return started, shutdowns


def _registered(router: BackendRouter) -> dict[str, str]:
    return router._registry.get_all()


# -- I/O & Edge-Case Matrix: NO_ENDPOINT --------------------------------------


class TestNoEndpoint:
    async def test_path_without_endpoint_is_unresolvable(self):
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(path="/tmp/model.bin")})
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert "m1" in exc.value.failures[0]
        assert "endpoint" in exc.value.failures[0]


# -- I/O & Edge-Case Matrix: CLI_COLLECT -------------------------------------


class TestCliCollect:
    async def test_same_model_in_both_sources_aborts(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        cli = {"m1": ModelSpec(endpoint="http://127.0.0.1:9000")}
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, cli, router)
        assert "m1" in exc.value.failures[0]


# -- I/O & Edge-Case Matrix: EXTERNAL_OK -------------------------------------


class TestExternalOk:
    async def test_reachable_endpoint_registered_non_owned(self, monkeypatch):
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": True})
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert run.owned_servers == []
        assert _registered(router)["m1"] == "http://127.0.0.1:8080"


# -- I/O & Edge-Case Matrix: PARTIAL_FAIL ------------------------------------


class TestPartialFail:
    async def test_aborts_before_spawning_the_other_model(self, monkeypatch):
        started, _ = _fake_server(monkeypatch)
        _set_probe(monkeypatch, {"http://127.0.0.1:9000": False})
        wf = _wf(
            {"a": _agent("m-a", "a"), "b": _agent("m-b", "b")},
            {"m-b": ModelSpec(endpoint="http://127.0.0.1:9000", path="/tmp/b.bin")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert any("m-a" in f for f in exc.value.failures)
        assert started == []


# -- I/O & Edge-Case Matrix: SPAWN_FAIL --------------------------------------


class TestSpawnFail:
    async def test_spawn_failure_reported_and_already_started_servers_killed(
        self,
        monkeypatch,
    ):
        started, shutdowns = _fake_server(monkeypatch, fail_ports={9001})
        _set_probe(
            monkeypatch,
            {
                "http://127.0.0.1:9000": False,
                "http://127.0.0.1:9001": False,
            },
        )
        wf = _wf(
            {"a": _agent("m-alpha", "a"), "b": _agent("m-zulu", "b")},
            {
                "m-alpha": ModelSpec(endpoint="http://127.0.0.1:9000", path="/tmp/a.bin"),
                "m-zulu": ModelSpec(endpoint="http://127.0.0.1:9001", path="/tmp/b.bin"),
            },
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert any("m-zulu" in f for f in exc.value.failures)
        assert [s.port for s in started] == [9000]
        assert len(shutdowns) == 1
        assert shutdowns[0].port == 9000
        assert shutdowns[0].shutdown_called is True


# -- AC1: every unresolvable referenced model is listed before execution -----


class TestAc1:
    async def test_every_unreferenced_because_missing_model_listed(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf({"a": _agent("ghost-a", "a"), "b": _agent("ghost-b", "b")}, {})
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        failures = exc.value.failures
        assert any("ghost-a" in f for f in failures)
        assert any("ghost-b" in f for f in failures)


# -- AC2: auto-start when endpoint unreachable and a path is declared --------


class TestAc2:
    async def test_unreachable_with_path_starts_server_and_registers(self, monkeypatch):
        started, _ = _fake_server(monkeypatch)
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        wf = _wf(
            {"a": _agent("m1", "a")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080", path="/tmp/model.bin")},
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert len(started) == 1
        assert started[0].port == 8080
        assert run.owned_servers[0] is started[0]
        assert _registered(router)["m1"] == "http://127.0.0.1:8080"

    async def test_unreachable_without_path_reports_model_and_endpoint(self, monkeypatch):
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert any(("m1" in f and "8080" in f) for f in exc.value.failures)


# -- AC3: owned servers torn down, external never killed, exactly-once -------


class TestAc3:
    async def test_teardown_shuts_down_only_owned_servers(self, monkeypatch):
        _, shutdowns = _fake_server(monkeypatch)
        _set_probe(
            monkeypatch,
            {
                "http://127.0.0.1:8080": True,
                "http://127.0.0.1:8081": False,
            },
        )
        wf = _wf(
            {"a": _agent("m-external", "a"), "b": _agent("m-spawn", "b")},
            {
                "m-external": ModelSpec(endpoint="http://127.0.0.1:8080"),
                "m-spawn": ModelSpec(endpoint="http://127.0.0.1:8081", path="/tmp/s.bin"),
            },
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert len(run.owned_servers) == 1
        assert shutdowns == []
        teardown(run)
        assert len(shutdowns) == 1
        assert shutdowns[0].port == 8081
        assert all(s.port != 8080 for s in shutdowns)

    async def test_each_referenced_model_registered_exactly_once(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert _registered(run.router) == {"m1": "http://127.0.0.1:8080"}
        with pytest.raises(ValueError):
            run.router.register("m1", "http://127.0.0.1:9999")


# -- AC4: fallback must reference an existing agent-type node ---------------


class TestAc4:
    async def test_fallback_to_missing_node_reported(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf(
            {"a": _agent("m-a", "a", fallback="ghost-b")},
            {"m-a": ModelSpec(endpoint="http://127.0.0.1:8111")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        bad = [f for f in exc.value.failures if "fallback" in f]
        assert bad and "ghost-b" in bad[0]
        assert "'a'" in bad[0]

    async def test_fallback_to_non_agent_node_rejected(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf(
            {
                "a": _agent("m-a", "a", fallback="m"),
                "b": _agent("m-b", "b"),
                "m": MergeNode(id="m", type="merge", inputs=["b"], strategy="all"),
            },
            {
                "m-a": ModelSpec(endpoint="http://127.0.0.1:8111"),
                "m-b": ModelSpec(endpoint="http://127.0.0.1:8112"),
            },
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        bad = [f for f in exc.value.failures if "fallback" in f]
        assert bad and "'m'" in bad[0]

    async def test_valid_fallback_passes(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf(
            {
                "a": _agent("m-a", "a", fallback="b"),
                "b": _agent("m-b", "b"),
            },
            {
                "m-a": ModelSpec(endpoint="http://127.0.0.1:8111"),
                "m-b": ModelSpec(endpoint="http://127.0.0.1:8112"),
            },
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert set(_registered(run.router)) == {"m-a", "m-b"}


# -- AC5: missing vram_size uses the 4GB default, logged once ---------------


class TestAc5:
    async def test_default_vram_used_and_logged_once_per_model(
        self,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ):
        _set_probe(monkeypatch)
        wf = _wf(
            {"a": _agent("m-novram", "a")},
            {"m-novram": ModelSpec(endpoint="http://127.0.0.1:8121")},
        )
        router = BackendRouter()
        caplog.clear()
        caplog.set_level(logging.INFO)
        run = await provision(wf, None, router)
        assert run.vram_sizes["m-novram"] == parse_vram_size("4GB")
        logs = [
            r.getMessage()
            for r in caplog.records
            if "no vram_size" in r.getMessage()
        ]
        assert len(logs) == 1
        assert "m-novram" in logs[0]


# -- Per-model inference timeouts (models: timeout_seconds) ------------------


class TestModelTimeouts:
    async def test_declared_model_timeout_surfaces_on_provisioned_run(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf(
            {"a": _agent("m-slow", "a")},
            {
                "m-slow": ModelSpec(
                    endpoint="http://127.0.0.1:8131",
                    timeout_seconds=600.0,
                )
            },
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert run.model_timeouts == {"m-slow": 600.0}

    async def test_model_timeouts_default_to_spec_default(self, monkeypatch):
        _set_probe(monkeypatch)
        wf = _wf(
            {"a": _agent("m-fast", "a")},
            {"m-fast": ModelSpec(endpoint="http://127.0.0.1:8132")},
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert run.model_timeouts == {"m-fast": 60.0}


# -- load_cli_models: the --models FILE override surface --------------------


class TestLoadCliModels:
    def test_valid_file_parses_to_model_specs(self, tmp_path: Path):
        p = tmp_path / "models.yaml"
        p.write_text(
            "m1:\n"
            "  endpoint: http://127.0.0.1:8080\n"
            "  path: /tmp/a.bin\n"
            "  vram_size: 2GB\n"
        )
        specs = load_cli_models(p)
        assert specs["m1"].endpoint == "http://127.0.0.1:8080"
        assert specs["m1"].path == "/tmp/a.bin"
        assert specs["m1"].vram_size == "2GB"

    def test_unknown_field_rejected(self, tmp_path: Path):
        p = tmp_path / "models.yaml"
        p.write_text("m1:\n  endpoint: http://x:1\n  bogus: 1\n")
        with pytest.raises(ProvisionError) as exc:
            load_cli_models(p)
        assert "bogus" in exc.value.failures[0]

    def test_entry_not_a_mapping_rejected(self, tmp_path: Path):
        p = tmp_path / "models.yaml"
        p.write_text("m1: just-a-string")
        with pytest.raises(ProvisionError):
            load_cli_models(p)

    def test_non_string_value_rejected(self, tmp_path: Path):
        p = tmp_path / "models.yaml"
        p.write_text("m1:\n  endpoint: {not: a-string}\n")
        with pytest.raises(ProvisionError):
            load_cli_models(p)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ProvisionError):
            load_cli_models(tmp_path / "nope.yaml")


# -- end-to-end: --models supplies the referenced model ---------------------


class TestCliOverrideEndToEnd:
    async def test_cli_overrides_supply_resolved_models(self, monkeypatch, tmp_path: Path):
        p = tmp_path / "models.yaml"
        p.write_text("m1:\n  endpoint: http://127.0.0.1:8080\n")
        _set_probe(monkeypatch)
        cli = load_cli_models(p)
        wf = _wf({"a": _agent("m1", "a")}, {})
        router = BackendRouter()
        run = await provision(wf, cli, router)
        assert _registered(run.router)["m1"] == "http://127.0.0.1:8080"


# -- Model path resolution (AGENCY_MODELS_DIRS) ------------------------------


class TestGetModelsDirs:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGENCY_MODELS_DIRS", raising=False)
        assert get_models_dirs() == [Path.home() / ".llama-cpp" / "models"]

    def test_parses_colon_separated_list(self, monkeypatch, tmp_path: Path):
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("AGENCY_MODELS_DIRS", f"{a}:{b}")
        assert get_models_dirs() == [a, b]

    def test_blank_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENCY_MODELS_DIRS", "  ")
        assert get_models_dirs() == [Path.home() / ".llama-cpp" / "models"]

    def test_trailing_colon_empty_tokens_skipped(self, monkeypatch, tmp_path: Path):
        a = tmp_path / "a"
        monkeypatch.setenv("AGENCY_MODELS_DIRS", f"{a}::")
        assert get_models_dirs() == [a]


class TestResolveModelPath:
    def test_absolute_returned_unchanged(self):
        assert resolve_model_path("/abs/model.bin") == Path("/abs/model.bin")

    def test_tilde_expanded(self):
        assert resolve_model_path("~/models/m.gguf") == (
            Path.home() / "models" / "m.gguf"
        )

    def test_env_var_expanded(self, monkeypatch):
        monkeypatch.setenv("MROOT", "/opt/models")
        assert resolve_model_path("$MROOT/m.gguf") == Path("/opt/models/m.gguf")

    def test_relative_found_in_second_dir(self, monkeypatch, tmp_path: Path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (b / "m.gguf").write_bytes(b"x")
        monkeypatch.setenv("AGENCY_MODELS_DIRS", f"{a}:{b}")
        assert resolve_model_path("m.gguf") == b / "m.gguf"

    def test_element_zero_wins_when_in_both(self, monkeypatch, tmp_path: Path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "m.gguf").write_bytes(b"a")
        (b / "m.gguf").write_bytes(b"b")
        monkeypatch.setenv("AGENCY_MODELS_DIRS", f"{a}:{b}")
        assert resolve_model_path("m.gguf") == a / "m.gguf"

    def test_relative_subpath(self, monkeypatch, tmp_path: Path):
        a = tmp_path / "a"
        (a / "sub").mkdir(parents=True)
        (a / "sub" / "m.gguf").write_bytes(b"x")
        monkeypatch.setenv("AGENCY_MODELS_DIRS", str(a))
        assert resolve_model_path("sub/m.gguf") == a / "sub" / "m.gguf"

    def test_relative_missing_returns_primary_dir(self, monkeypatch, tmp_path: Path):
        a = tmp_path / "a"
        a.mkdir()
        monkeypatch.setenv("AGENCY_MODELS_DIRS", str(a))
        resolved = resolve_model_path("ghost.gguf")
        assert resolved == a / "ghost.gguf"
        assert not resolved.is_file()


class TestPortablePathProvisioning:
    async def test_relative_not_found_fails_fast_with_searched_dirs(
        self, monkeypatch, tmp_path: Path
    ):
        started, _ = _fake_server(monkeypatch)
        a = tmp_path / "a"
        a.mkdir()
        monkeypatch.setenv("AGENCY_MODELS_DIRS", str(a))
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        wf = _wf(
            {"a": _agent("m1", "a")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080", path="ghost.gguf")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert any(
            "m1" in f and "ghost.gguf" in f and str(a) in f
            for f in exc.value.failures
        )
        assert started == []

    async def test_relative_found_spawns_with_resolved_path(
        self, monkeypatch, tmp_path: Path
    ):
        started, _ = _fake_server(monkeypatch)
        a = tmp_path / "a"
        a.mkdir()
        (a / "m.gguf").write_bytes(b"x")
        monkeypatch.setenv("AGENCY_MODELS_DIRS", str(a))
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        wf = _wf(
            {"a": _agent("m1", "a")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080", path="m.gguf")},
        )
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert len(started) == 1
        assert started[0].model_path == str(a / "m.gguf")
        assert run.owned_servers[0] is started[0]
        assert _registered(router)["m1"] == "http://127.0.0.1:8080"

    async def test_absolute_path_keeps_spawn_behavior_without_existence_check(
        self, monkeypatch
    ):
        # Absolute paths are trusted as-is: no existence check, old spawn path.
        started, _ = _fake_server(monkeypatch)
        monkeypatch.delenv("AGENCY_MODELS_DIRS", raising=False)
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        wf = _wf(
            {"a": _agent("m1", "a")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080", path="/does/not/exist.bin")},
        )
        router = BackendRouter()
        await provision(wf, None, router)
        assert len(started) == 1
        assert started[0].model_path == "/does/not/exist.bin"


# -- FR-016: tool-call capability probe --------------------------------------


def _set_capability_probe(monkeypatch, capable: dict[str, bool] | None = None) -> list:
    """Replace the gate's capability probe with a deterministic fake.

    Returns the call log so tests can assert which (endpoint, model) pairs were
    probed (and how often).
    """
    mapping = capable or {}
    calls: list[tuple[str, str]] = []

    async def fake_probe(endpoint: str, model: str) -> bool:
        calls.append((endpoint, model))
        return mapping.get(model, True)

    monkeypatch.setattr(
        "agency.resource_manager.provisioning._probe_tool_capability", fake_probe
    )
    return calls


class TestCapabilityProbe:
    async def test_probe_pass_proceeds_and_registers(self, monkeypatch):
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": True})
        calls = _set_capability_probe(monkeypatch)
        wf = _wf({"a": _agent_tools("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        await provision(wf, None, router)
        assert _registered(router)["m1"] == "http://127.0.0.1:8080"
        assert calls == [("http://127.0.0.1:8080", "m1")]

    async def test_probe_fail_aborts_with_consolidated_error(self, monkeypatch):
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": True})
        _set_capability_probe(monkeypatch, {"m1": False})
        wf = _wf({"a": _agent_tools("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        expected = (
            "agent node 'a' enables tool calling but model 'm1' "
            "at http://127.0.0.1:8080 failed the tool-call capability probe"
        )
        assert any(expected in f for f in exc.value.failures)
        # Nothing was registered: the run aborts before success.
        assert _registered(router) == {}

    async def test_probe_failure_is_reported_alongside_other_failures(self, monkeypatch):
        # One model is missing entirely (resolution failure) while the
        # tool-enabled model fails the probe; both lines share one report.
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": True})
        _set_capability_probe(monkeypatch, {"m1": False})
        wf = _wf(
            {"a": _agent_tools("m1", "a"), "b": _agent("ghost-m", "b")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        failures = exc.value.failures
        assert any("ghost-m" in f for f in failures)
        assert any("failed the tool-call capability probe" in f for f in failures)

    async def test_one_line_per_affected_node_for_shared_model(self, monkeypatch):
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": True})
        calls = _set_capability_probe(monkeypatch, {"m1": False})
        wf = _wf(
            {"a": _agent_tools("m1", "a"), "b": _agent_tools("m1", "b")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        probe_lines = [f for f in exc.value.failures if "capability probe" in f]
        assert len(probe_lines) == 2
        assert any("'a'" in f for f in probe_lines)
        assert any("'b'" in f for f in probe_lines)
        # The shared model was probed exactly once.
        assert calls == [("http://127.0.0.1:8080", "m1")]

    async def test_no_tool_nodes_zero_probe_http(self, monkeypatch):
        _set_probe(monkeypatch)
        calls = _set_capability_probe(monkeypatch)
        wf = _wf({"a": _agent("m1", "a")}, {"m1": ModelSpec(endpoint="http://127.0.0.1:8080")})
        router = BackendRouter()
        run = await provision(wf, None, router)
        assert _registered(run.router)["m1"] == "http://127.0.0.1:8080"
        assert calls == []

    async def test_probe_failure_tears_down_owned_servers(self, monkeypatch):
        started, shutdowns = _fake_server(monkeypatch)
        # The tool-enabled model's endpoint is unreachable but carries a local
        # path, so the gate auto-starts an owned server before probing it.
        _set_probe(monkeypatch, {"http://127.0.0.1:8080": False})
        _set_capability_probe(monkeypatch, {"m1": False})
        wf = _wf(
            {"a": _agent_tools("m1", "a")},
            {"m1": ModelSpec(endpoint="http://127.0.0.1:8080", path="/tmp/model.bin")},
        )
        router = BackendRouter()
        with pytest.raises(ProvisionError) as exc:
            await provision(wf, None, router)
        assert any("failed the tool-call capability probe" in f for f in exc.value.failures)
        assert len(started) == 1
        assert len(shutdowns) == 1
        assert shutdowns[0] is started[0]
        assert shutdowns[0].shutdown_called is True
        assert _registered(router) == {}
