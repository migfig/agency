"""End-to-end HTTP tests for the service app factory (US1, T015).

Drives ``create_app`` through Starlette's TestClient with a model-free
workflow (tool_call + human_in_loop) so no model is involved; the default
provisioning gate is patched where a failure is under test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agency.api.service as service_module
from agency.api.app import create_app
from agency.api.config import ServiceConfig
from agency.core.event_bus import reset_event_bus
from agency.resource_manager.provisioning import ProvisionError

TOOL_WORKFLOW = """
name: app-tool-demo
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "hi"}'
"""

HIL_WORKFLOW = """
name: app-hil-demo
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
name: app-replay-demo
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

BAD_WORKFLOW = "nodes:\n  a: {id: a, type: bogus}\n"

TERMINAL_NODE_STATUSES = {"completed", "completed-fallback", "failed", "skipped"}


def write_cli_run_dir(
    log_dir: Path,
    run_id: str,
    *,
    workflow_name: str,
    started_at: str,
    statuses: dict[str, str],
    outputs: dict[str, str] | None = None,
    workflow_path: str | None = None,
) -> Path:
    """Fabricate a CLI-style run dir (metadata + WAL + checkpoints) on disk."""
    run_dir = log_dir / run_id
    checkpoints = run_dir / "checkpoints"
    checkpoints.mkdir(parents=True)
    metadata: dict = {
        "run_id": run_id,
        "workflow_name": workflow_name,
        "started_at": started_at,
        "node_count": len(statuses),
        "vram_limit_bytes": None,
    }
    if workflow_path is not None:
        metadata["workflow_path"] = workflow_path
    (run_dir / "metadata.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    lines: list[dict] = [
        {"seq": 0, "ts": started_at, "mutation": "run_started", "run_id": run_id}
    ]
    for seq, (node_id, status) in enumerate(statuses.items(), start=1):
        entry: dict = {
            "seq": seq,
            "ts": started_at,
            "mutation": "node_status",
            "node_id": node_id,
            "from": "pending",
            "to": status,
        }
        if status in TERMINAL_NODE_STATUSES:
            entry["output_ref"] = f"outputs/{node_id}.txt"
            entry["tokens_used"] = 7
            entry["elapsed_seconds"] = 0.25
            (checkpoints / f"{node_id}.json").write_text(
                json.dumps(
                    {
                        "node_id": node_id,
                        "workflow_id": workflow_name,
                        "run_id": run_id,
                        "status": status,
                        "output_ref": f"outputs/{node_id}.txt",
                        "summary": None,
                        "tokens_used": 7,
                        "model": None,
                        "elapsed_seconds": 0.25,
                        "timestamp": started_at,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        lines.append(entry)
    (run_dir / "wal.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    if outputs:
        out_dir = run_dir / "outputs"
        out_dir.mkdir(exist_ok=True)
        for node_id, text in outputs.items():
            (out_dir / f"{node_id}.txt").write_text(text, encoding="utf-8")
    return run_dir


def write_bare_run_dir(
    log_dir: Path,
    run_id: str,
    *,
    workflow_name: str,
    started_at: str,
    workflow_path: str | None = None,
) -> Path:
    """Fabricate a run dir that crashed before any NodeStarted (no node statuses)."""
    run_dir = log_dir / run_id
    run_dir.mkdir(parents=True)
    metadata: dict = {
        "run_id": run_id,
        "workflow_name": workflow_name,
        "started_at": started_at,
        "node_count": 0,
        "vram_limit_bytes": None,
    }
    if workflow_path is not None:
        metadata["workflow_path"] = workflow_path
    (run_dir / "metadata.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    (run_dir / "wal.jsonl").write_text(
        json.dumps(
            {"seq": 0, "ts": started_at, "mutation": "run_started", "run_id": run_id}
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def client(log_dir: Path):
    app = create_app(ServiceConfig(log_dir=log_dir))
    with TestClient(app) as c:
        yield c


def wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)


def post_run(client: TestClient, tmp_path: Path, name: str, text: str = TOOL_WORKFLOW):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return client.post("/runs", json={"workflow": str(path)})


class TestRuns:
    def test_post_run_returns_202(self, client, tmp_path: Path):
        resp = post_run(client, tmp_path, "tool.yaml")
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"]
        assert body["state"] == "running"
        assert body["workflow"] == "app-tool-demo"

    def test_post_run_unknown_workflow_404(self, client, tmp_path: Path):
        missing = tmp_path / "nope.yaml"
        resp = client.post("/runs", json={"workflow": str(missing)})
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_workflow"
        assert err["details"] == {"workflow": str(missing)}

    def test_post_run_invalid_workflow_422(self, client, tmp_path: Path):
        resp = post_run(client, tmp_path, "bad.yaml", BAD_WORKFLOW)
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "invalid_workflow"
        assert len(err["details"]["problems"]) > 0

    def test_post_run_model_validation_failed_409(self, client, tmp_path: Path, monkeypatch):
        async def failing(workflow, cli_models, router):
            raise ProvisionError(["model 'llama': endpoint unreachable, no local path"])

        monkeypatch.setattr(service_module, "provision", failing)
        resp = post_run(client, tmp_path, "tool.yaml")
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "model_validation_failed"
        assert err["details"]["failures"] == ["model 'llama': endpoint unreachable, no local path"]

    def test_post_run_malformed_body_422(self, client):
        resp = client.post("/runs", json={})
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "invalid_request"
        problems = err["details"]["problems"]
        assert len(problems) > 0
        assert all("field" in p and "message" in p for p in problems)


class TestRunStatus:
    def test_get_run_status_200_shape(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "tool.yaml").json()["run_id"]
        resp = client.get(f"/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["state"] in {"running", "completed"}
        assert body["workflow_name"] == "app-tool-demo"
        assert body["replay_source"] is None
        assert set(body) == {"run_id", "state", "workflow_name", "started_at", "replay_source", "pending_inputs", "nodes"}
        assert set(next(iter(body["nodes"].values()))) == {
            "status",
            "model",
            "tokens",
            "duration_seconds",
            "attempt",
            "fallback",
            "reason",
        }

    def test_get_run_unknown_404(self, client):
        resp = client.get("/runs/20990101_000000")
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_run"
        assert err["details"] == {"run_id": "20990101_000000"}


class TestNodeInput:
    def test_submit_input_200(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "hil.yaml", HIL_WORKFLOW).json()["run_id"]
        wait_for(lambda: self._has_pending(client, run_id))
        resp = client.post(f"/runs/{run_id}/nodes/gate/input", json={"value": "yes"})
        assert resp.status_code == 200
        assert resp.json() == {"run_id": run_id, "node_id": "gate", "accepted": True}
        wait_for(lambda: client.get(f"/runs/{run_id}").json()["state"] in {"completed", "failed"})

    def test_submit_input_not_awaiting_409(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "hil.yaml", HIL_WORKFLOW).json()["run_id"]
        wait_for(lambda: self._has_pending(client, run_id))
        first = client.post(f"/runs/{run_id}/nodes/gate/input", json={"value": "yes"})
        assert first.status_code == 200
        second = client.post(f"/runs/{run_id}/nodes/gate/input", json={"value": "no"})
        assert second.status_code == 409
        err = second.json()["error"]
        assert err["code"] == "input_not_awaiting"
        assert err["details"] == {"run_id": run_id, "node_id": "gate"}

    def test_submit_input_unknown_run_404(self, client):
        resp = client.post("/runs/20990101_000000/nodes/gate/input", json={"value": "yes"})
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_run"

    def test_submit_input_unknown_node_404(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "hil.yaml", HIL_WORKFLOW).json()["run_id"]
        wait_for(lambda: self._has_pending(client, run_id))
        resp = client.post(f"/runs/{run_id}/nodes/nope/input", json={"value": "yes"})
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_node"
        assert err["details"] == {"run_id": run_id, "node_id": "nope"}
        # Unblock the run so the test finishes quickly.
        client.post(f"/runs/{run_id}/nodes/gate/input", json={"value": "yes"})

    @staticmethod
    def _has_pending(client: TestClient, run_id: str) -> bool:
        body = client.get(f"/runs/{run_id}").json()
        return any(e["node_id"] == "gate" for e in body["pending_inputs"])


class TestRunResult:
    def test_get_run_result_200(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "tool.yaml").json()["run_id"]
        wait_for(lambda: client.get(f"/runs/{run_id}").json()["state"] in {"completed", "failed"})
        resp = client.get(f"/runs/{run_id}/result")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["status"] == "completed"
        assert body["total_tokens"] >= 0
        assert body["duration_seconds"] >= 0
        node = body["nodes"][0]
        assert node["node_id"] == "shout"
        assert node["status"] == "completed"
        assert node["output"] == "HI"

    def test_get_run_result_not_finished_409(self, client, tmp_path: Path):
        run_id = post_run(client, tmp_path, "hil.yaml", HIL_WORKFLOW).json()["run_id"]
        wait_for(
            lambda: any(
                e["node_id"] == "gate"
                for e in client.get(f"/runs/{run_id}").json()["pending_inputs"]
            )
        )
        resp = client.get(f"/runs/{run_id}/result")
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "run_not_finished"
        assert err["details"] == {"run_id": run_id, "state": "running"}
        # Unblock the run.
        client.post(f"/runs/{run_id}/nodes/gate/input", json={"value": "yes"})
        wait_for(lambda: client.get(f"/runs/{run_id}").json()["state"] in {"completed", "failed"})

    def test_get_run_result_unknown_404(self, client):
        resp = client.get("/runs/20990101_000000/result")
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_run"


class TestReplay:
    def test_post_replay_returns_202(self, client, tmp_path: Path):
        path = tmp_path / "replay.yaml"
        path.write_text(REPLAY_WORKFLOW, encoding="utf-8")
        source_id = client.post(
            "/runs", json={"workflow": str(path)}
        ).json()["run_id"]
        wait_for(
            lambda: client.get(f"/runs/{source_id}").json()["state"]
            in {"completed", "failed"}
        )
        resp = client.post(
            "/replay", json={"workflow": str(path), "checkpoint": "shout"}
        )
        assert resp.status_code == 202
        body = resp.json()
        assert set(body) == {"run_id", "state", "source_run", "checkpoint"}
        assert body["state"] == "running"
        assert body["checkpoint"] == "shout"
        assert body["source_run"] == source_id
        assert body["run_id"] != source_id
        new_id = body["run_id"]
        wait_for(
            lambda: client.get(f"/runs/{new_id}").json()["state"]
            in {"completed", "failed"}
        )
        result = client.get(f"/runs/{new_id}/result")
        assert result.status_code == 200
        nodes = {n["node_id"]: n for n in result.json()["nodes"]}
        assert nodes["shout"]["output"] == "HELLO"
        assert nodes["echo"]["output"] == "tail"

    def test_post_replay_unknown_workflow_404(self, client, tmp_path: Path):
        missing = tmp_path / "nope.yaml"
        resp = client.post(
            "/replay", json={"workflow": str(missing), "checkpoint": "shout"}
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "unknown_workflow"
        assert err["details"] == {"workflow": str(missing)}

    def test_post_replay_no_source_404(self, client, tmp_path: Path):
        path = tmp_path / "replay.yaml"
        path.write_text(REPLAY_WORKFLOW, encoding="utf-8")
        resp = client.post(
            "/replay", json={"workflow": str(path), "checkpoint": "shout"}
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "no_replay_source"
        assert err["details"] == {"checkpoint": "shout", "source_run": None}

    def test_post_replay_model_validation_failed_409(
        self, client, tmp_path: Path, monkeypatch
    ):
        async def failing(workflow, cli_models, router):
            raise ProvisionError(["model 'llama': endpoint unreachable"])

        monkeypatch.setattr(service_module, "provision", failing)
        path = tmp_path / "replay.yaml"
        path.write_text(REPLAY_WORKFLOW, encoding="utf-8")
        resp = client.post(
            "/replay", json={"workflow": str(path), "checkpoint": "shout"}
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "model_validation_failed"
        assert err["details"]["failures"] == ["model 'llama': endpoint unreachable"]

    def test_post_replay_malformed_body_422(self, client):
        resp = client.post("/replay", json={"workflow": "x.yaml"})
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "invalid_request"
        problems = err["details"]["problems"]
        assert any("checkpoint" in p["field"] for p in problems)


class TestRunHistory:
    def test_get_runs_200_shape_sorted_and_merges_live(self, client, log_dir, tmp_path):
        write_cli_run_dir(
            log_dir,
            "20260101_000000",
            workflow_name="cli-done",
            started_at="2026-01-01T00:00:00+00:00",
            statuses={"shout": "completed"},
            outputs={"shout": "HI"},
        )
        write_cli_run_dir(
            log_dir,
            "20260102_000000",
            workflow_name="cli-fail",
            started_at="2026-01-02T00:00:00+00:00",
            statuses={"shout": "failed"},
        )
        live_id = post_run(client, tmp_path, "tool.yaml").json()["run_id"]

        resp = client.get("/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert all(
            set(entry) == {"run_id", "workflow_name", "started_at", "state"}
            for entry in body
        )
        by_id = {entry["run_id"]: entry for entry in body}
        assert by_id["20260101_000000"] == {
            "run_id": "20260101_000000",
            "workflow_name": "cli-done",
            "started_at": "2026-01-01T00:00:00+00:00",
            "state": "completed",
        }
        assert by_id["20260102_000000"]["state"] == "failed"
        assert by_id[live_id]["workflow_name"] == "app-tool-demo"
        assert [entry["run_id"] for entry in body] == [
            "20260101_000000",
            "20260102_000000",
            live_id,
        ]

    def test_get_runs_workflow_filter(self, client, log_dir, tmp_path):
        write_cli_run_dir(
            log_dir,
            "20260101_000000",
            workflow_name="cli-done",
            started_at="2026-01-01T00:00:00+00:00",
            statuses={"shout": "completed"},
        )
        live_id = post_run(client, tmp_path, "tool.yaml").json()["run_id"]

        resp = client.get("/runs", params={"workflow": "app-tool-demo"})
        assert resp.status_code == 200
        body = resp.json()
        assert [entry["run_id"] for entry in body] == [live_id]
        assert body[0]["workflow_name"] == "app-tool-demo"

        assert client.get("/runs", params={"workflow": "no-such-workflow"}).json() == []


class TestHealth:
    def test_health_200(self, client, log_dir):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "log_dir": str(log_dir)}


class TestStartupRecovery:
    def test_interrupted_run_recovered_from_workflow(self, log_dir, tmp_path):
        workflow_file = tmp_path / "recovery.yaml"
        workflow_file.write_text(HIL_WORKFLOW, encoding="utf-8")
        write_cli_run_dir(
            log_dir,
            "20260101_010101",
            workflow_name="app-hil-demo",
            started_at="2026-01-01T01:01:01+00:00",
            statuses={"shout": "completed", "gate": "running"},
            workflow_path=str(workflow_file),
            outputs={"shout": "HI"},
        )
        app = create_app(ServiceConfig(log_dir=log_dir))
        with TestClient(app) as c:
            entry = next(
                e for e in c.get("/runs").json() if e["run_id"] == "20260101_010101"
            )
            assert entry["state"] == "interrupted"
            assert entry["workflow_name"] == "app-hil-demo"

            resp = c.get("/runs/20260101_010101")
            assert resp.status_code == 200
            body = resp.json()
            assert body["state"] == "interrupted"
            assert body["pending_inputs"] == []
            assert body["nodes"]["shout"]["status"] == "completed"
            assert body["nodes"]["shout"]["tokens"] == 7
            assert body["nodes"]["gate"]["status"] == "running"

            resp = c.get("/runs/20260101_010101/result")
            assert resp.status_code == 409
            err = resp.json()["error"]
            assert err["code"] == "run_not_finished"
            assert err["details"] == {"run_id": "20260101_010101", "state": "interrupted"}

    def test_interrupted_run_workflow_file_gone_wal_only_states(self, log_dir, tmp_path):
        workflow_file = tmp_path / "gone.yaml"
        workflow_file.write_text(HIL_WORKFLOW, encoding="utf-8")
        write_cli_run_dir(
            log_dir,
            "20260101_010101",
            workflow_name="app-hil-demo",
            started_at="2026-01-01T01:01:01+00:00",
            statuses={"shout": "completed", "gate": "running"},
            workflow_path=str(workflow_file),
        )
        workflow_file.unlink()

        app = create_app(ServiceConfig(log_dir=log_dir))
        with TestClient(app) as c:
            entry = next(
                e for e in c.get("/runs").json() if e["run_id"] == "20260101_010101"
            )
            assert entry["state"] == "interrupted"
            body = c.get("/runs/20260101_010101").json()
            assert body["state"] == "interrupted"
            assert set(body["nodes"]) == {"shout", "gate"}
            assert body["nodes"]["shout"]["status"] == "completed"
            assert body["nodes"]["gate"]["status"] == "running"

    def test_zero_node_statuses_interrupted_empty_node_map(self, log_dir, tmp_path):
        workflow_file = tmp_path / "recovery.yaml"
        workflow_file.write_text(HIL_WORKFLOW, encoding="utf-8")
        write_bare_run_dir(
            log_dir,
            "20260101_020202",
            workflow_name="app-hil-demo",
            started_at="2026-01-01T02:02:02+00:00",
            workflow_path=str(workflow_file),
        )
        app = create_app(ServiceConfig(log_dir=log_dir))
        with TestClient(app) as c:
            entry = next(
                e for e in c.get("/runs").json() if e["run_id"] == "20260101_020202"
            )
            assert entry["state"] == "interrupted"
            body = c.get("/runs/20260101_020202").json()
            assert body["state"] == "interrupted"
            assert body["nodes"] == {}

    def test_terminal_run_result_survives_restart(self, log_dir):
        write_cli_run_dir(
            log_dir,
            "20260101_030303",
            workflow_name="app-tool-demo",
            started_at="2026-01-01T03:03:03+00:00",
            statuses={"shout": "completed"},
            outputs={"shout": "HI"},
        )
        app = create_app(ServiceConfig(log_dir=log_dir))
        with TestClient(app) as c:
            resp = c.get("/runs/20260101_030303/result")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "completed"
            nodes = {n["node_id"]: n for n in body["nodes"]}
            assert nodes["shout"]["status"] == "completed"
            assert nodes["shout"]["output"] == "HI"
