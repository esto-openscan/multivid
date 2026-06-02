from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import default_profiles_path, load_camera_node_config
from .profiles import load_recording_profiles
from .recorder import (
    AlreadyPositioningError,
    AlreadyRecordingError,
    CameraBusyError,
    InvalidCalibrationIdError,
    InvalidSessionIdError,
    InvalidStillLabelError,
    InvalidTakeIdError,
    RecorderError,
    RpicamVidRecorder,
    TakeAlreadyExistsError,
    UnknownProfileError,
)


logger = logging.getLogger(__name__)


class StartRecordingRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    take_id: str | None = None
    force_prepare: bool = False
    refocus: bool = False
    notes: str | None = None
    apply_calibration_suggestions: bool = False


class PrepareRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    force: bool = False
    refocus: bool = False


class PrepareResetRequest(BaseModel):
    session_id: str = Field(min_length=1)


class CalibrationRunRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    duration_seconds: float = Field(default=5, gt=0)
    calibration_id: str | None = None
    target: str | None = None
    notes: str | None = None
    apply_to_session: bool = False


class CalibrationApplyRequest(BaseModel):
    session_id: str = Field(min_length=1)
    calibration_id: str | None = None


class PositioningStartRequest(BaseModel):
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: int | None = Field(default=None, gt=0)
    jpeg_quality: int | None = Field(default=None, ge=1, le=100)
    overlay: str | list[str] | None = None
    overlays: list[str] | None = None
    profile: str | None = None

    def overlay_values(self) -> list[str] | None:
        if self.overlays is not None:
            return self.overlays
        if self.overlay is None:
            return None
        return [self.overlay] if isinstance(self.overlay, str) else self.overlay


class StillCaptureRequest(BaseModel):
    session_id: str = Field(min_length=1)
    label: str | None = None
    profile: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    quality: int | None = Field(default=None, ge=1, le=100)
    notes: str | None = None
    use_recording_profile_controls: bool | None = None
    force: bool = False


def create_app(config_path: str | Path | None = None, profiles_path: str | Path | None = None) -> FastAPI:
    config = load_camera_node_config(config_path)
    profiles = load_recording_profiles(
        profiles_path or default_profiles_path(),
        profile_overrides=config.profile_overrides,
    )
    recorder = RpicamVidRecorder(config=config, profiles=profiles)

    app = FastAPI(title="OpenScan Camera Node", version=__version__)
    app.state.config = config
    app.state.recorder = recorder

    @app.get("/health")
    def health() -> dict[str, Any]:
        status = recorder.status()
        return {
            "ok": True,
            "camera_id": config.camera_id,
            "hostname": status.get("hostname"),
            "state": status.get("state"),
            "service_version": __version__,
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        return recorder.status()

    @app.get("/profiles")
    def list_profiles() -> dict[str, Any]:
        return {"profiles_scope": "resolved_for_node", "profiles": recorder.profiles()}

    @app.get("/sessions")
    def list_sessions() -> dict[str, Any]:
        return recorder.list_sessions()

    @app.get("/sessions/{session_id}")
    def session_summary(session_id: str) -> dict[str, Any]:
        try:
            return recorder.session_summary(session_id)
        except InvalidSessionIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/takes")
    def session_takes(session_id: str) -> dict[str, Any]:
        try:
            return recorder.session_takes(session_id)
        except InvalidSessionIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/manifest-summary")
    def session_manifest_summary(session_id: str) -> dict[str, Any]:
        try:
            return recorder.session_manifest_summary(session_id)
        except InvalidSessionIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/recordings/start", status_code=202)
    def start_recording(request: StartRecordingRequest) -> dict[str, Any]:
        try:
            return recorder.start(
                session_id=request.session_id,
                profile_name=request.profile,
                take_id=request.take_id,
                force_prepare=request.force_prepare,
                refocus=request.refocus,
                notes=request.notes,
                apply_calibration_suggestions=request.apply_calibration_suggestions,
            )
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TakeAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidSessionIdError, InvalidTakeIdError, UnknownProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AlreadyPositioningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CameraBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            logger.exception("rpicam-vid was not found")
            raise HTTPException(status_code=500, detail="rpicam-vid was not found on this node") from exc
        except Exception as exc:
            logger.exception("failed to start recording")
            raise HTTPException(status_code=500, detail="failed to start recording") from exc

    @app.post("/recordings/stop")
    def stop_recording() -> dict[str, Any]:
        return recorder.stop()

    @app.post("/prepare")
    def prepare(request: PrepareRequest) -> dict[str, Any]:
        try:
            return recorder.prepare(
                session_id=request.session_id,
                profile_name=request.profile,
                force=request.force,
                refocus=request.refocus,
            )
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlreadyPositioningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CameraBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidSessionIdError, UnknownProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/prepare/reset")
    def prepare_reset(request: PrepareResetRequest) -> dict[str, Any]:
        try:
            return recorder.reset_prepare(session_id=request.session_id)
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidSessionIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/calibration/run", status_code=202)
    def calibration_run(request: CalibrationRunRequest) -> dict[str, Any]:
        try:
            return recorder.run_calibration(
                session_id=request.session_id,
                profile_name=request.profile,
                duration_seconds=request.duration_seconds,
                calibration_id=request.calibration_id,
                target=request.target,
                notes=request.notes,
                apply_to_session=request.apply_to_session,
            )
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlreadyPositioningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CameraBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidCalibrationIdError, InvalidSessionIdError, UnknownProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="calibration_id already exists for this camera") from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            logger.exception("rpicam-vid was not found")
            raise HTTPException(status_code=500, detail="rpicam-vid was not found on this node") from exc
        except Exception as exc:
            logger.exception("failed to run calibration")
            raise HTTPException(status_code=500, detail="failed to run calibration") from exc

    @app.get("/calibration/status")
    def calibration_status() -> dict[str, Any]:
        return recorder.calibration_status()

    @app.get("/calibration/last")
    def calibration_last() -> dict[str, Any]:
        return recorder.calibration_last()

    @app.post("/calibration/apply-to-session")
    def calibration_apply_to_session(request: CalibrationApplyRequest) -> dict[str, Any]:
        try:
            return recorder.apply_calibration_to_session(
                session_id=request.session_id,
                calibration_id=request.calibration_id,
            )
        except (InvalidCalibrationIdError, InvalidSessionIdError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/positioning/start", status_code=202)
    def positioning_start(request: PositioningStartRequest) -> dict[str, Any]:
        try:
            return recorder.start_positioning(
                width=request.width,
                height=request.height,
                fps=request.fps,
                jpeg_quality=request.jpeg_quality,
                overlays=request.overlay_values(),
                profile_name=request.profile,
            )
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CameraBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnknownProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/positioning/stop")
    def positioning_stop() -> dict[str, Any]:
        return recorder.stop_positioning()

    @app.get("/positioning/status")
    def positioning_status() -> dict[str, Any]:
        return recorder.positioning_status()

    @app.get("/positioning/snapshot.jpg")
    def positioning_snapshot() -> Response:
        try:
            image_bytes, metadata = recorder.positioning_snapshot()
        except RecorderError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-OpenScan-Captured-At": str(metadata.get("captured_at", "")),
            },
        )

    @app.get("/positioning/stream.mjpg")
    def positioning_stream() -> StreamingResponse:
        if not recorder.positioning_running():
            raise HTTPException(status_code=409, detail="positioning preview is not running; call POST /positioning/start first")
        return StreamingResponse(
            _mjpeg_stream(recorder),
            media_type="multipart/x-mixed-replace; boundary=openscan-positioning",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/stills/capture", status_code=202)
    def stills_capture(request: StillCaptureRequest) -> dict[str, Any]:
        try:
            return recorder.capture_reference_still(
                session_id=request.session_id,
                label=request.label,
                profile_name=request.profile,
                width=request.width,
                height=request.height,
                quality=request.quality,
                notes=request.notes,
                use_recording_profile_controls=request.use_recording_profile_controls,
                force=request.force,
            )
        except (AlreadyRecordingError, AlreadyPositioningError, CameraBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidSessionIdError, InvalidStillLabelError, UnknownProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecorderError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/stills/status")
    def stills_status() -> dict[str, Any]:
        return recorder.stills_status()

    return app


def _mjpeg_stream(recorder: RpicamVidRecorder):
    boundary = b"--openscan-positioning\r\n"
    while recorder.positioning_running():
        try:
            image_bytes, _metadata = recorder.positioning_snapshot()
        except RecorderError:
            break
        yield (
            boundary
            + b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(image_bytes)}\r\n\r\n".encode("ascii")
            + image_bytes
            + b"\r\n"
        )
        time.sleep(recorder.positioning_frame_interval_seconds())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_camera_node_config()
    uvicorn.run(
        create_app(),
        host=config.listen_host,
        port=config.listen_port,
        log_level="info",
    )
