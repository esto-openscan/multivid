from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import default_profiles_path, load_camera_node_config
from .profiles import load_recording_profiles
from .recorder import (
    AlreadyRecordingError,
    InvalidSessionIdError,
    RecorderError,
    RpicamVidRecorder,
    UnknownProfileError,
)


logger = logging.getLogger(__name__)


class StartRecordingRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)


def create_app(config_path: str | Path | None = None, profiles_path: str | Path | None = None) -> FastAPI:
    config = load_camera_node_config(config_path)
    profiles = load_recording_profiles(profiles_path or default_profiles_path())
    recorder = RpicamVidRecorder(config=config, profiles=profiles)

    app = FastAPI(title="OpenScan Camera Node", version="0.1.0")
    app.state.config = config
    app.state.recorder = recorder

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "camera_id": config.camera_id}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return recorder.status()

    @app.get("/profiles")
    def list_profiles() -> dict[str, Any]:
        return {"profiles": recorder.profiles()}

    @app.post("/recordings/start", status_code=202)
    def start_recording(request: StartRecordingRequest) -> dict[str, Any]:
        try:
            return recorder.start(session_id=request.session_id, profile_name=request.profile)
        except AlreadyRecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidSessionIdError, UnknownProfileError) as exc:
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
