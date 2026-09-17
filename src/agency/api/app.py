"""FastAPI app factory for the agency serve endpoint (US1)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agency.api.config import ServiceConfig
from agency.api.registry import RunRegistry
from agency.api.schemas import (
    CODE_INVALID_REQUEST,
    AgencyAPIError,
    HealthResponse,
    NodeInputRequest,
    ReplayAccepted,
    ReplayRequest,
    RunAccepted,
    RunHistoryEntry,
    RunResultView,
    RunStatusView,
    StartRunRequest,
)
from agency.api.service import RunService
from agency.api.stream import EventBroadcaster

logger = logging.getLogger(__name__)


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    """Build the agency HTTP service app (test- and serve-friendly)."""
    config = config if config is not None else ServiceConfig()
    log_dir = Path(config.log_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".write-probe"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(f"log dir '{log_dir}' is not writable: {exc}") from exc
        registry = RunRegistry()
        app.state.registry = registry
        app.state.service = RunService(config, registry)
        stream = EventBroadcaster()
        app.state.stream = stream
        await stream.start()
        recovered = registry.recover_interrupted(log_dir)
        if recovered:
            logger.info(
                "recovered %d interrupted run(s) from %s: %s",
                len(recovered),
                log_dir,
                ", ".join(recovered),
            )
        yield
        await stream.stop()

    app = FastAPI(title="agency", lifespan=lifespan)

    @app.exception_handler(AgencyAPIError)
    async def agency_api_error_handler(
        request: Request, exc: AgencyAPIError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.error_body().model_dump()},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": CODE_INVALID_REQUEST,
                    "message": "invalid request",
                    "details": {"problems": problems},
                }
            },
        )

    @app.post("/runs", status_code=202, response_model=RunAccepted)
    async def start_run(body: StartRunRequest, request: Request) -> RunAccepted:
        service: RunService = request.app.state.service
        return await service.start_run(
            body.workflow,
            vram_limit=body.vram_limit,
            models=body.models,
            environment=body.environment,
            sandbox=body.sandbox,
        )

    @app.post("/replay", status_code=202, response_model=ReplayAccepted)
    async def replay_run(body: ReplayRequest, request: Request) -> ReplayAccepted:
        service: RunService = request.app.state.service
        return await service.replay_run(
            body.workflow,
            body.checkpoint,
            source_run=body.source_run,
            vram_limit=body.vram_limit,
            models=body.models,
            environment=body.environment,
            sandbox=body.sandbox,
        )

    @app.get("/runs", response_model=list[RunHistoryEntry])
    async def list_runs(
        request: Request, workflow: str | None = Query(default=None)
    ) -> list[RunHistoryEntry]:
        service: RunService = request.app.state.service
        return service.list_runs(workflow)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        service: RunService = request.app.state.service
        return service.health()

    @app.get("/runs/{run_id}", response_model=RunStatusView)
    async def get_run_status(run_id: str, request: Request) -> RunStatusView:
        service: RunService = request.app.state.service
        return service.run_status(run_id)

    @app.post("/runs/{run_id}/nodes/{node_id}/input")
    async def submit_node_input(
        run_id: str, node_id: str, body: NodeInputRequest, request: Request
    ) -> dict[str, Any]:
        service: RunService = request.app.state.service
        service.submit_input(run_id, node_id, body.value)
        return {"run_id": run_id, "node_id": node_id, "accepted": True}

    @app.get("/runs/{run_id}/result", response_model=RunResultView)
    async def get_run_result(run_id: str, request: Request) -> RunResultView:
        service: RunService = request.app.state.service
        return service.run_result(run_id)

    @app.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        stream: EventBroadcaster = app.state.stream
        await websocket.accept()
        conn = stream.connect(websocket)
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        finally:
            stream.disconnect(conn)

    return app
