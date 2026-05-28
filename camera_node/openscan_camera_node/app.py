from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .config import default_profiles_path, load_camera_node_config
from .profiles import load_recording_profiles
from .recorder import (
    AlreadyRecordingError,
    InvalidSessionIdError,
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


class PrepareRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    force: bool = False
    refocus: bool = False


class PrepareResetRequest(BaseModel):
    session_id: str = Field(min_length=1)


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
            )
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TakeAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidSessionIdError, InvalidTakeIdError, UnknownProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_camera_node_config()
    uvicorn.run(
        create_app(),
        host=config.listen_host,
        port=config.listen_port,
        log_level="info",
    )
