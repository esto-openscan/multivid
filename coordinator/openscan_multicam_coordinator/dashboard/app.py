from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import DashboardConfig, NodeConfig, load_dashboard_config, load_nodes_config
from ..operations import (
    RequestSpec,
    aggregate_operation_response,
    aggregate_profiles,
    dashboard_node_config,
    request_nodes,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class PositioningStartRequest(BaseModel):
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: int | None = Field(default=None, gt=0)
    jpeg_quality: int | None = Field(default=None, ge=1, le=100)
    overlays: list[str] | None = None
    profile: str | None = None


class ReferenceStillRequest(BaseModel):
    session_id: str = Field(min_length=1)
    label: str | None = None
    profile: str | None = None
    notes: str | None = None


class CalibrationRunRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    duration_seconds: float | None = Field(default=None, gt=0)
    target: str | None = None
    notes: str | None = None


class RecordingStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    take_id: str | None = None
    notes: str | None = None


class PrepareResetRequest(BaseModel):
    session_id: str = Field(min_length=1)


class DashboardState:
    def __init__(self, nodes: list[NodeConfig], config_path: str | Path, dashboard_config: DashboardConfig) -> None:
        self.nodes = nodes
        self.config_path = str(config_path)
        self.dashboard_config = dashboard_config


def create_app(
    config_path: str | Path,
    nodes: list[NodeConfig] | None = None,
    dashboard_config: DashboardConfig | None = None,
) -> FastAPI:
    resolved_nodes = load_nodes_config(config_path) if nodes is None else nodes
    resolved_dashboard_config = load_dashboard_config(config_path) if dashboard_config is None else dashboard_config
    state = DashboardState(resolved_nodes, config_path, resolved_dashboard_config)

    app = FastAPI(title="OpenScan Multicam Dashboard")
    app.state.dashboard = state
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/nodes")
    def api_nodes() -> dict[str, Any]:
        return {
            "config_path": state.config_path,
            "node_count": len(state.nodes),
            "nodes": [dashboard_node_config(node) for node in state.nodes],
            "dashboard": _dashboard_config_response(state.dashboard_config),
            "warning": "local/trusted-network MVP; no authentication is enabled",
        }

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return await _run_operation(state, "status", RequestSpec("GET", "/status"))

    @app.get("/api/previews/{node_name}/snapshot.jpg")
    async def api_preview_snapshot(node_name: str) -> Response:
        node = _node_by_name(state, node_name)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                response = await client.get(f"{node.base_url}/positioning/snapshot.jpg")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"{node.name}: preview snapshot unavailable: {exc}") from exc

        if not response.is_success:
            detail = _response_error_detail(response)
            raise HTTPException(status_code=response.status_code, detail=detail)

        return Response(
            content=response.content,
            media_type=response.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/previews/{node_name}/stream.mjpg")
    async def api_preview_stream(node_name: str) -> StreamingResponse:
        node = _node_by_name(state, node_name)
        timeout = httpx.Timeout(connect=8.0, read=None, write=8.0, pool=8.0)
        client = httpx.AsyncClient(timeout=timeout)
        request = client.build_request("GET", f"{node.base_url}/positioning/stream.mjpg")
        try:
            response = await client.send(request, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"{node.name}: preview stream unavailable: {exc}") from exc

        if not response.is_success:
            detail = await _stream_error_detail(response)
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=response.status_code, detail=detail)

        return StreamingResponse(
            _stream_upstream_response(response, client),
            media_type=response.headers.get("content-type", "multipart/x-mixed-replace; boundary=openscan-positioning"),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/positioning/start")
    async def api_positioning_start(request: PositioningStartRequest) -> dict[str, Any]:
        defaults = state.dashboard_config.positioning
        body: dict[str, Any] = {
            "width": request.width or defaults.width,
            "height": request.height or defaults.height,
            "fps": request.fps or defaults.fps,
            "jpeg_quality": request.jpeg_quality or defaults.jpeg_quality,
            "overlays": request.overlays if request.overlays is not None else list(defaults.overlays),
        }
        if request.profile:
            body["profile"] = request.profile
        return await _run_operation(state, "positioning_start", RequestSpec("POST", "/positioning/start", body))

    @app.post("/api/positioning/stop")
    async def api_positioning_stop() -> dict[str, Any]:
        return await _run_operation(state, "positioning_stop", RequestSpec("POST", "/positioning/stop"))

    @app.post("/api/stills/capture")
    async def api_stills_capture(request: ReferenceStillRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"session_id": request.session_id}
        if request.label:
            body["label"] = request.label
        if request.profile:
            body["profile"] = request.profile
        if request.notes:
            body["notes"] = request.notes
        return await _run_operation(state, "stills_capture", RequestSpec("POST", "/stills/capture", body, timeout_seconds=30.0))

    @app.post("/api/calibration/run")
    async def api_calibration_run(request: CalibrationRunRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": request.session_id,
            "profile": request.profile,
            "apply_to_session": False,
        }
        if request.duration_seconds is not None:
            body["duration_seconds"] = request.duration_seconds
        if request.target:
            body["target"] = request.target
        if request.notes:
            body["notes"] = request.notes
        timeout_seconds = max(8.0, float(body.get("duration_seconds", 5.0)) + 20.0)
        return await _run_operation(
            state,
            "calibration_run",
            RequestSpec("POST", "/calibration/run", body, timeout_seconds=timeout_seconds),
        )

    @app.post("/api/recordings/start")
    async def api_recordings_start(request: RecordingStartRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"session_id": request.session_id, "profile": request.profile}
        if request.take_id:
            body["take_id"] = request.take_id
        if request.notes:
            body["notes"] = request.notes
        return await _run_operation(state, "recordings_start", RequestSpec("POST", "/recordings/start", body))

    @app.post("/api/recordings/stop")
    async def api_recordings_stop() -> dict[str, Any]:
        return await _run_operation(state, "recordings_stop", RequestSpec("POST", "/recordings/stop"))

    @app.post("/api/prepare/reset")
    async def api_prepare_reset(request: PrepareResetRequest) -> dict[str, Any]:
        body = {"session_id": request.session_id}
        return await _run_operation(state, "prepare_reset", RequestSpec("POST", "/prepare/reset", body))

    @app.get("/api/profiles")
    async def api_profiles() -> dict[str, Any]:
        results = await request_nodes(state.nodes, RequestSpec("GET", "/profiles"))
        return aggregate_profiles(results)

    return app


async def _run_operation(state: DashboardState, operation: str, spec: RequestSpec) -> dict[str, Any]:
    results = await request_nodes(state.nodes, spec)
    return _with_preview_proxy_urls(aggregate_operation_response(operation, results))


def _node_by_name(state: DashboardState, node_name: str) -> NodeConfig:
    for node in state.nodes:
        if node.name == node_name:
            return node
    raise HTTPException(status_code=404, detail=f"unknown camera node: {node_name}")


def _with_preview_proxy_urls(response: dict[str, Any]) -> dict[str, Any]:
    for item in response.get("nodes", []):
        if not isinstance(item, dict):
            continue
        node = item.get("node") if isinstance(item.get("node"), dict) else {}
        node_name = node.get("name")
        if not node_name:
            continue
        quoted_name = quote(str(node_name), safe="")
        item["snapshot_url"] = f"/api/previews/{quoted_name}/snapshot.jpg"
        item["stream_url"] = f"/api/previews/{quoted_name}/stream.mjpg"
    return response


async def _stream_upstream_response(response: httpx.Response, client: httpx.AsyncClient):
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()


def _response_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(data, dict) and data.get("detail"):
        return str(data["detail"])
    return response.text or f"HTTP {response.status_code}"


async def _stream_error_detail(response: httpx.Response) -> str:
    content = await response.aread()
    try:
        data = response.json()
    except ValueError:
        text = content.decode(response.encoding or "utf-8", errors="replace")
        return text or f"HTTP {response.status_code}"
    if isinstance(data, dict) and data.get("detail"):
        return str(data["detail"])
    text = content.decode(response.encoding or "utf-8", errors="replace")
    return text or f"HTTP {response.status_code}"


def _dashboard_config_response(config: DashboardConfig) -> dict[str, Any]:
    return {
        "positioning": {
            "width": config.positioning.width,
            "height": config.positioning.height,
            "fps": config.positioning.fps,
            "jpeg_quality": config.positioning.jpeg_quality,
            "overlays": list(config.positioning.overlays),
        },
        "status_refresh_seconds": config.status_refresh_seconds,
    }


def serve(config_path: str | Path, host: str, port: int, open_browser: bool = False) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run the dashboard; install the coordinator package dependencies") from exc

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(create_app(config_path), host=host, port=port, log_level="info")
