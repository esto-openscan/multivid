from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .calibration import build_suggested_controls, parse_rpicam_metadata, suggestion_values
from .config import CameraNodeConfig, normalize_positioning_overlays
from .imaging import (
    JpegCaptureError,
    POSITIONING_BACKEND_NAME,
    RpicamJpegBackend,
    camera_controls_from_profile,
)
from .profiles import CameraControlPolicy, RecordingProfile, RecordingProfiles


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
TAKE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CALIBRATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
STILL_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
AUTO_TAKE_ID_PATTERN = re.compile(r"^take_(\d{3})$")
AUTO_REFERENCE_LABEL_PATTERN = re.compile(r"^reference_(\d{3})$")
PROCESS_STOP_TIMEOUT_SECONDS = 10
LOW_DISK_WARNING_BYTES = 500 * 1024 * 1024
BACKEND_NAME = "rpicam-vid"
MANIFEST_SCHEMA_VERSION = 4
PREPARED_STATE_SCHEMA_VERSION = 3
CALIBRATION_MANIFEST_SCHEMA_VERSION = 1
STILL_MANIFEST_SCHEMA_VERSION = 1


class RecorderError(Exception):
    """Base class for expected recorder failures."""


class AlreadyRecordingError(RecorderError):
    pass


class AlreadyPositioningError(RecorderError):
    pass


class CameraBusyError(RecorderError):
    pass


class UnknownProfileError(RecorderError):
    pass


class InvalidSessionIdError(RecorderError):
    pass


class InvalidTakeIdError(RecorderError):
    pass


class InvalidCalibrationIdError(RecorderError):
    pass


class TakeAlreadyExistsError(RecorderError):
    pass


class InvalidStillLabelError(RecorderError):
    pass


@dataclass
class RecordingState:
    session_id: str
    take_id: str
    profile_name: str
    output_dir: Path
    output_file: Path
    manifest_path: Path
    command: list[str]
    started_at: str
    process: subprocess.Popen[bytes]
    stderr_file: BinaryIO
    manifest: dict[str, Any]


@dataclass
class PositioningState:
    settings: dict[str, Any]
    started_at: str
    updated_at: str | None
    profile_name: str | None
    frames_served: int
    last_frame_at: str | None
    last_backend: str | None
    last_command: list[str] | None
    last_error: str | None
    warnings: list[str]


class RpicamVidRecorder:
    def __init__(self, config: CameraNodeConfig, profiles: RecordingProfiles) -> None:
        self._config = config
        self._profiles = profiles
        self._lock = threading.Lock()
        self._camera_lock = threading.Lock()
        self._jpeg_backend = RpicamJpegBackend()
        self._state: RecordingState | None = None
        self._positioning_state: PositioningState | None = None
        self._lifecycle_state = "idle"
        self._last_error: str | None = None
        self._last_positioning_error: str | None = None
        self._last_recording_summary: dict[str, Any] | None = None
        self._last_calibration_summary: dict[str, Any] | None = None
        self._last_still_capture_info: dict[str, Any] | None = None
        self._calibration_running = False
        self._still_capture_running = False
        self._prepared_state: dict[str, Any] | None = None
        self._hostname = socket.gethostname()

    def start(
        self,
        session_id: str,
        profile_name: str,
        take_id: str | None = None,
        force_prepare: bool = False,
        refocus: bool = False,
        notes: str | None = None,
        apply_calibration_suggestions: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                self._finalize_if_process_exited_locked()

                if self._state is not None and self._state.process.poll() is None:
                    raise AlreadyRecordingError("recording is already running")
                if self._positioning_state is not None:
                    raise AlreadyPositioningError(
                        "cannot start recording while positioning preview is running; stop positioning first"
                    )
                if self._still_capture_running:
                    raise CameraBusyError("cannot start recording while a reference still capture is running")

                self._validate_session_id(session_id)
                profile = self._get_profile(profile_name)
                camera_session_dir = self._camera_session_dir(session_id)
                resolved_take_id = self._resolve_take_id(camera_session_dir, take_id)
                prepared_state, prepared_reused = self._prepare_locked(
                    session_id=session_id,
                    profile=profile,
                    force=force_prepare,
                    refocus=refocus or profile.camera_control_policy.refocus_on_each_take,
                )

                take_dir = camera_session_dir / resolved_take_id
                take_dir.mkdir(parents=True, exist_ok=False)
                output_file = take_dir / f"recording.{profile.output_extension}"
                manifest_path = take_dir / "manifest.json"
                if output_file.exists() or manifest_path.exists():
                    raise TakeAlreadyExistsError(f"take_id already exists for this camera: {resolved_take_id}")

                calibration_context = _calibration_context_from_prepared_state(prepared_state)
                suggestions_apply_allowed = _suggestions_apply_allowed(
                    node_policy=self._config.camera_control_policy.as_dict(),
                    profile=profile,
                    start_requested=apply_calibration_suggestions,
                )
                suggestions_applied = suggestions_apply_allowed and calibration_context is not None
                if suggestions_applied:
                    prepared_state = dict(prepared_state)
                    prepared_state["calibration_suggestions_applied_to_recording"] = True
                    self._write_json(self._prepared_state_path(session_id), prepared_state)
                    self._prepared_state = prepared_state
                command = self._build_command(
                    profile,
                    output_file,
                    calibration_context.get("suggested_controls_snapshot") if suggestions_applied else None,
                )
                applied_controls = _applied_controls_for_recording(
                    profile,
                    output_file,
                    calibration_context.get("suggested_controls_snapshot") if suggestions_applied else None,
                    self._config.camera_transform.as_dict(),
                )
                started_at_dt = _utc_now_dt()
                started_at = _format_timestamp(started_at_dt)
                usable_start_offset_seconds = profile.camera_control_policy.pre_roll_seconds
                warnings = list(prepared_state.get("warnings", []))
                if force_prepare:
                    warnings.append("force_prepare requested; prepared state was recreated before recording")
                if refocus:
                    warnings.append(
                        "refocus requested; rpicam-vid backend does not implement focus-value measurement or lock yet"
                    )
                if calibration_context is not None and not suggestions_applied:
                    warnings.append(
                        "calibration suggestions are linked in prepared state but were not applied to recording"
                    )
                if apply_calibration_suggestions and calibration_context is None:
                    warnings.append("apply_calibration_suggestions requested but no calibration suggestions were available")

                manifest = {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "status": "starting",
                    "session_id": session_id,
                    "take_id": resolved_take_id,
                    "camera_id": self._config.camera_id,
                    "hostname": self._hostname,
                    "service_version": __version__,
                    "backend": BACKEND_NAME,
                    "backend_details": {"name": BACKEND_NAME, "version": None},
                    "profile": profile_name,
                    "profile_snapshot": profile.as_dict(),
                    "profile_settings": profile.as_dict(),
                    "prepared_state": prepared_state,
                    "prepared_state_reused": prepared_reused,
                    "force_prepare_requested": force_prepare,
                    "refocus_requested": refocus,
                    "effective_refocus_requested": refocus or profile.camera_control_policy.refocus_on_each_take,
                    "notes": notes,
                    "requested_camera_control_policy": profile.camera_control_policy.as_dict(),
                    "requested_controls": profile.requested_controls(),
                    "resolved_controls": profile.resolved_controls(),
                    "applied_controls": applied_controls,
                    "actually_applied_controls": applied_controls,
                    "unsupported_controls": profile.unsupported_controls,
                    "calibration_id": calibration_context.get("calibration_id") if calibration_context else None,
                    "calibration_manifest_path": (
                        calibration_context.get("calibration_manifest_path") if calibration_context else None
                    ),
                    "suggested_controls_path": (
                        calibration_context.get("suggested_controls_path") if calibration_context else None
                    ),
                    "calibration_suggestions_snapshot": (
                        calibration_context.get("suggested_controls_snapshot") if calibration_context else None
                    ),
                    "calibration_suggestions_applied": suggestions_applied,
                    "apply_calibration_suggestions_requested": apply_calibration_suggestions,
                    "recording_start_time": started_at,
                    "recording_stop_time": None,
                    "pre_roll_seconds": _clean_number(profile.camera_control_policy.pre_roll_seconds),
                    "usable_start_offset_seconds": _clean_number(usable_start_offset_seconds),
                    "usable_start_time": _format_timestamp(
                        started_at_dt + timedelta(seconds=usable_start_offset_seconds)
                    ),
                    "output_dir": str(take_dir),
                    "output_file_name": output_file.name,
                    "output_file_path": str(output_file),
                    "rpicam_vid_command": command,
                    "process_pid": None,
                    "exit_code": None,
                    "warnings": _dedupe(warnings),
                    "errors": [],
                }
                self._write_json(manifest_path, manifest)
                self._last_recording_summary = _recording_summary_from_manifest(manifest)

                stderr_file = (take_dir / "rpicam-vid.stderr.log").open("ab")
                try:
                    # start_new_session gives us a process group to terminate if rpicam-vid spawns helpers.
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_file,
                        cwd=take_dir,
                        start_new_session=True,
                    )
                except Exception as exc:
                    stderr_file.close()
                    manifest.update(
                        {
                            "status": "failed_to_start",
                            "recording_stop_time": _utc_now(),
                            "errors": [str(exc)],
                        }
                    )
                    self._write_json(manifest_path, manifest)
                    self._last_recording_summary = _recording_summary_from_manifest(manifest)
                    self._lifecycle_state = "error"
                    self._last_error = str(exc)
                    raise

                manifest["status"] = "recording"
                manifest["process_pid"] = process.pid
                self._write_json(manifest_path, manifest)
                self._last_recording_summary = _recording_summary_from_manifest(manifest)

                self._state = RecordingState(
                    session_id=session_id,
                    take_id=resolved_take_id,
                    profile_name=profile_name,
                    output_dir=take_dir,
                    output_file=output_file,
                    manifest_path=manifest_path,
                    command=command,
                    started_at=started_at,
                    process=process,
                    stderr_file=stderr_file,
                    manifest=manifest,
                )
                self._lifecycle_state = "recording"
                self._last_error = None
                return self._status_locked()
            except RecorderError as exc:
                self._lifecycle_state = "positioning" if isinstance(exc, AlreadyPositioningError) else "error"
                self._last_error = str(exc)
                raise
            except FileNotFoundError as exc:
                self._lifecycle_state = "error"
                self._last_error = "rpicam-vid was not found on this node"
                raise

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            if self._state is None:
                return self._status_locked()

            self._lifecycle_state = "stopping"
            self._update_manifest_locked(status="stopping", stopped_at=None, exit_code=None)

            process = self._state.process
            exit_code = process.poll()
            status = "stopped"

            if exit_code is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

                try:
                    exit_code = process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    status = "killed"
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    exit_code = process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            else:
                status = "exited"

            self._update_manifest_locked(status=status, stopped_at=_utc_now(), exit_code=exit_code)
            self._close_stderr_locked()
            self._state = None
            self._lifecycle_state = "completed"
            self._last_error = None
            return self._status_locked()

    def prepare(self, session_id: str, profile_name: str, force: bool = False, refocus: bool = False) -> dict[str, Any]:
        with self._lock:
            try:
                self._finalize_if_process_exited_locked()
                if self._state is not None and self._state.process.poll() is None:
                    raise AlreadyRecordingError("cannot prepare while a recording is already running")
                if self._positioning_state is not None:
                    raise AlreadyPositioningError(
                        "cannot prepare while positioning preview is running; stop positioning first"
                    )
                if self._still_capture_running:
                    raise CameraBusyError("cannot prepare while a reference still capture is running")

                self._validate_session_id(session_id)
                profile = self._get_profile(profile_name)
                prepared_state, prepared_reused = self._prepare_locked(
                    session_id=session_id,
                    profile=profile,
                    force=force,
                    refocus=refocus,
                )
                self._lifecycle_state = "armed"
                self._last_error = None
                status = self._status_locked()
                status["prepared_state_reused"] = prepared_reused
                status["prepared_state"] = prepared_state
                return status
            except RecorderError as exc:
                self._lifecycle_state = "positioning" if isinstance(exc, AlreadyPositioningError) else "error"
                self._last_error = str(exc)
                raise

    def reset_prepare(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            if self._state is not None and self._state.process.poll() is None:
                raise AlreadyRecordingError("cannot reset prepared state while a recording is running")

            self._validate_session_id(session_id)
            prepared_path = self._prepared_state_path(session_id)
            reset = False
            if prepared_path.exists():
                prepared_path.unlink()
                reset = True
            if self._prepared_state and self._prepared_state.get("session_id") == session_id:
                self._prepared_state = None
            if self._lifecycle_state == "armed":
                self._lifecycle_state = "idle"

            status = self._status_locked()
            status["prepared_state_reset"] = reset
            return status

    def run_calibration(
        self,
        session_id: str,
        profile_name: str,
        duration_seconds: float = 5.0,
        calibration_id: str | None = None,
        target: str | None = None,
        notes: str | None = None,
        apply_to_session: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                self._finalize_if_process_exited_locked()
                if self._state is not None and self._state.process.poll() is None:
                    raise AlreadyRecordingError("cannot run calibration while a recording is running")
                if self._positioning_state is not None:
                    raise AlreadyPositioningError(
                        "cannot run calibration while positioning preview is running; stop positioning first"
                    )
                if self._still_capture_running:
                    raise CameraBusyError("cannot run calibration while a reference still capture is running")
                if self._calibration_running:
                    raise CameraBusyError("calibration is already running")

                self._validate_session_id(session_id)
                profile = self._get_profile(profile_name)
                resolved_calibration_id = calibration_id or _default_calibration_id()
                self._validate_calibration_id(resolved_calibration_id)
                if duration_seconds <= 0:
                    raise RecorderError("duration_seconds must be > 0")

                camera_session_dir = self._camera_session_dir(session_id)
                calibration_dir = self._calibration_dir(session_id, resolved_calibration_id)
                calibration_dir.mkdir(parents=True, exist_ok=False)
                self._calibration_running = True
                self._lifecycle_state = "calibrating"
            except RecorderError as exc:
                self._lifecycle_state = "positioning" if isinstance(exc, AlreadyPositioningError) else "error"
                self._last_error = str(exc)
                raise

        try:
            summary = self._run_calibration_unlocked(
                session_id=session_id,
                profile=profile,
                duration_seconds=duration_seconds,
                calibration_id=resolved_calibration_id,
                target=target,
                notes=notes,
                apply_to_session=apply_to_session,
                camera_session_dir=camera_session_dir,
                calibration_dir=calibration_dir,
            )
        finally:
            with self._lock:
                self._calibration_running = False
                if self._state is None and self._lifecycle_state == "calibrating":
                    self._lifecycle_state = "armed" if self._prepared_state else "idle"

        with self._lock:
            self._last_calibration_summary = summary
            self._write_json(self._last_calibration_path(session_id), summary)
            if apply_to_session:
                summary = self._activate_calibration_suggestions_locked(session_id, summary)
            self._last_error = None
            status = self._status_locked()
            status["calibration"] = summary
            return status

    def calibration_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "running": self._calibration_running,
                "last": self._read_last_calibration_summary(),
            }

    def calibration_last(self) -> dict[str, Any]:
        with self._lock:
            last = self._read_last_calibration_summary()
            if last is None:
                return {
                    "camera_id": self._config.camera_id,
                    "hostname": self._hostname,
                    "last": None,
                    "warnings": ["no calibration has been recorded on this node"],
                }
            return last

    def apply_calibration_to_session(
        self,
        session_id: str,
        calibration_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_session_id(session_id)
            if calibration_id is not None:
                self._validate_calibration_id(calibration_id)
            summary = self._calibration_summary_for_session(session_id, calibration_id)
            active = self._activate_calibration_suggestions_locked(session_id, summary)
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "session_id": session_id,
                "calibration_id": active.get("calibration_id"),
                "active_suggested_controls_path": str(self._active_calibration_path(session_id)),
                "suggested_controls": active.get("suggested_controls"),
                "warnings": active.get("warnings", []),
            }

    def start_positioning(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        jpeg_quality: int | None = None,
        overlays: list[str] | None = None,
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            if self._state is not None and self._state.process.poll() is None:
                raise AlreadyRecordingError("cannot start positioning preview while a recording is running")
            if self._calibration_running:
                raise CameraBusyError("cannot start positioning preview while calibration is running")
            if self._still_capture_running:
                raise CameraBusyError("cannot start positioning preview while a reference still capture is running")
            if profile_name is not None:
                self._get_profile(profile_name)

            settings = self._resolve_positioning_settings(
                width=width,
                height=height,
                fps=fps,
                jpeg_quality=jpeg_quality,
                overlays=overlays,
                profile_name=profile_name,
            )
            now = _utc_now()
            previous = self._positioning_state
            self._positioning_state = PositioningState(
                settings=settings,
                started_at=previous.started_at if previous else now,
                updated_at=now if previous else None,
                profile_name=profile_name,
                frames_served=previous.frames_served if previous else 0,
                last_frame_at=previous.last_frame_at if previous else None,
                last_backend=previous.last_backend if previous else None,
                last_command=previous.last_command if previous else None,
                last_error=None,
                warnings=[],
            )
            self._lifecycle_state = "positioning"
            self._last_error = None
            self._last_positioning_error = None
            return self._positioning_status_locked()

    def stop_positioning(self) -> dict[str, Any]:
        wait_for_active_capture = False
        with self._lock:
            if self._positioning_state is not None:
                wait_for_active_capture = True
                self._positioning_state = None
                if self._state is not None and self._state.process.poll() is None:
                    self._lifecycle_state = "recording"
                elif self._calibration_running:
                    self._lifecycle_state = "calibrating"
                else:
                    self._lifecycle_state = "armed" if self._prepared_state else "idle"
                self._last_positioning_error = None
                self._last_error = None

        if wait_for_active_capture:
            with self._camera_lock:
                pass

        with self._lock:
            return self._positioning_status_locked()

    def positioning_status(self) -> dict[str, Any]:
        with self._lock:
            return self._positioning_status_locked()

    def positioning_running(self) -> bool:
        with self._lock:
            return self._positioning_state is not None

    def positioning_frame_interval_seconds(self) -> float:
        with self._lock:
            fps = self._positioning_state.settings.get("fps") if self._positioning_state else self._config.positioning.fps
            return 1.0 / max(1.0, float(fps))

    def positioning_snapshot(self) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            if self._positioning_state is None:
                raise RecorderError("positioning preview is not running; call POST /positioning/start first")
            settings = dict(self._positioning_state.settings)
            profile = self._get_profile(self._positioning_state.profile_name) if self._positioning_state.profile_name else None

        camera_controls = camera_controls_from_profile(profile)
        timeout_ms = max(100, min(1500, int(1000 / max(1, int(settings["fps"])))))
        try:
            with self._camera_lock:
                result = self._jpeg_backend.capture_to_bytes(
                    width=int(settings["width"]),
                    height=int(settings["height"]),
                    quality=int(settings["jpeg_quality"]),
                    camera_controls=camera_controls,
                    camera_transform=self._config.camera_transform.as_dict(),
                    overlay_settings={
                        "camera_id": self._config.camera_id,
                        "overlays": list(settings.get("overlays", [])),
                    },
                    timeout_ms=timeout_ms,
                )
        except (FileNotFoundError, JpegCaptureError) as exc:
            self._record_positioning_error(str(exc))
            raise RecorderError(str(exc)) from exc

        metadata = {
            "backend": result.backend,
            "command": result.command,
            "warnings": result.warnings,
            "captured_at": _utc_now(),
        }
        with self._lock:
            if self._positioning_state is not None:
                self._positioning_state.frames_served += 1
                self._positioning_state.last_frame_at = metadata["captured_at"]
                self._positioning_state.last_backend = result.backend
                self._positioning_state.last_command = result.command
                self._positioning_state.last_error = None
                self._positioning_state.warnings = _dedupe(result.warnings)
                self._last_positioning_error = None
        return result.image_bytes or b"", metadata

    def capture_reference_still(
        self,
        session_id: str,
        label: str | None = None,
        profile_name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        quality: int | None = None,
        notes: str | None = None,
        use_recording_profile_controls: bool | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            if self._state is not None and self._state.process.poll() is None:
                raise AlreadyRecordingError("cannot capture a reference still while recording")
            if self._calibration_running:
                raise CameraBusyError("cannot capture a reference still while calibration is running")
            if self._positioning_state is not None:
                raise AlreadyPositioningError(
                    "cannot capture a reference still while positioning preview is running; stop positioning first"
                )
            if self._still_capture_running:
                raise CameraBusyError("reference still capture is already running")

            self._validate_session_id(session_id)
            if label is not None:
                self._validate_still_label(label)
            profile = self._get_profile(profile_name) if profile_name else None
            resolved_quality = self._resolve_quality(quality, self._config.reference_stills.quality, "quality")
            resolved_width = self._resolve_optional_dimension(width, self._config.reference_stills.width, "width")
            resolved_height = self._resolve_optional_dimension(height, self._config.reference_stills.height, "height")
            controls_requested = (
                self._config.reference_stills.use_recording_profile_controls
                if use_recording_profile_controls is None
                else bool(use_recording_profile_controls)
            )

            state_before_capture = self._lifecycle_state
            stills_dir = self._reference_stills_dir(session_id)
            stills_dir.mkdir(parents=True, exist_ok=True)
            resolved_label, label_warnings = self._resolve_reference_still_label(stills_dir, label, force)
            image_file = stills_dir / f"{resolved_label}.jpg"
            manifest_path = stills_dir / f"{resolved_label}_manifest.json"
            warnings = list(label_warnings)
            camera_controls = camera_controls_from_profile(profile) if controls_requested else {}
            if controls_requested and profile is None:
                warnings.append("use_recording_profile_controls is true but no profile was provided; no profile controls were applied")
            if controls_requested and profile is not None and not camera_controls:
                warnings.append(f"profile {profile.name!r} has no still-applicable camera controls")
            applied_controls = dict(camera_controls)
            _extend_applied_controls_with_camera_transform(applied_controls, self._config.camera_transform.as_dict())

            manifest: dict[str, Any] = {
                "schema_version": STILL_MANIFEST_SCHEMA_VERSION,
                "status": "running",
                "session_id": session_id,
                "camera_id": self._config.camera_id,
                "label": resolved_label,
                "requested_label": label,
                "timestamp": _utc_now(),
                "finished_at": None,
                "hostname": self._hostname,
                "node_hostname": self._hostname,
                "service_version": __version__,
                "image_file_path": str(image_file),
                "image_file_name": image_file.name,
                "manifest_path": str(manifest_path),
                "requested_size": {"width": resolved_width, "height": resolved_height},
                "requested_quality": resolved_quality,
                "actual_file_size": None,
                "backend": None,
                "backend_command": None,
                "profile": profile.name if profile else None,
                "profile_snapshot": profile.as_dict() if profile else None,
                "use_recording_profile_controls": controls_requested,
                "requested_controls": profile.requested_controls() if profile and controls_requested else None,
                "applied_controls": applied_controls,
                "notes": notes,
                "force": force,
                "warnings": _dedupe(warnings),
                "errors": [],
                "state_before_capture": state_before_capture,
                "positioning_was_active": False,
                "positioning_stopped": False,
                "positioning_behavior": "rejected_if_active",
            }
            self._write_json(manifest_path, manifest)
            self._still_capture_running = True

        try:
            with self._camera_lock:
                capture = self._jpeg_backend.capture_to_file(
                    output_file=image_file,
                    width=resolved_width,
                    height=resolved_height,
                    quality=resolved_quality,
                    camera_controls=camera_controls,
                    camera_transform=self._config.camera_transform.as_dict(),
                    timeout_ms=1200,
                )
            manifest.update(
                {
                    "status": "completed",
                    "finished_at": _utc_now(),
                    "actual_file_size": image_file.stat().st_size if image_file.exists() else None,
                    "backend": capture.backend,
                    "backend_command": capture.command,
                    "warnings": _dedupe([*manifest.get("warnings", []), *capture.warnings]),
                }
            )
        except (FileNotFoundError, JpegCaptureError) as exc:
            manifest.update(
                {
                    "status": "failed",
                    "finished_at": _utc_now(),
                    "errors": [str(exc)],
                }
            )
            self._write_json(manifest_path, manifest)
            with self._lock:
                self._still_capture_running = False
                self._last_still_capture_info = _still_capture_summary(manifest)
                self._lifecycle_state = "error"
                self._last_error = str(exc)
            raise RecorderError(str(exc)) from exc
        finally:
            with self._lock:
                self._still_capture_running = False

        self._write_json(manifest_path, manifest)
        summary = _still_capture_summary(manifest)
        with self._lock:
            self._last_still_capture_info = summary
            self._last_error = None
            if self._lifecycle_state == "error":
                self._lifecycle_state = "armed" if self._prepared_state else "idle"
            status = self._status_locked()
            status["still_capture"] = summary
            status["reference_still"] = summary
            return status

    def stills_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "running": self._still_capture_running,
                "last": self._last_still_capture_info,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            return self._status_locked()

    def profiles(self) -> dict[str, dict[str, Any]]:
        return self._profiles.as_dict()

    def list_sessions(self) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            sessions: list[dict[str, Any]] = []
            root = self._config.output_root
            if root.exists():
                for child in sorted(root.iterdir()):
                    if not child.is_dir():
                        continue
                    camera_dir = child / self._config.camera_id
                    sessions.append(
                        {
                            "session_id": child.name,
                            "camera_session_exists": camera_dir.exists(),
                            "camera_session_path": str(camera_dir),
                        }
                    )
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "output_root": str(root),
                "sessions": sessions,
            }

    def session_summary(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            self._validate_session_id(session_id)
            camera_dir = self._camera_session_dir(session_id)
            exists = camera_dir.exists()
            prepared_path = self._prepared_state_path(session_id)
            takes = self._session_takes_summary(camera_dir) if exists else []
            reference_stills = self._reference_stills_summary(camera_dir) if exists else []
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "session_id": session_id,
                "output_root": str(self._config.output_root),
                "camera_session_path": str(camera_dir),
                "exists": exists,
                "prepared_state": _file_summary(prepared_path, camera_dir) if exists else {"exists": False},
                "takes": takes,
                "take_count": len(takes),
                "reference_stills": reference_stills,
                "reference_still_count": len(reference_stills),
            }

    def session_takes(self, session_id: str) -> dict[str, Any]:
        summary = self.session_summary(session_id)
        return {
            "camera_id": summary["camera_id"],
            "hostname": summary["hostname"],
            "session_id": session_id,
            "exists": summary["exists"],
            "takes": summary["takes"],
            "take_count": summary["take_count"],
        }

    def session_manifest_summary(self, session_id: str) -> dict[str, Any]:
        summary = self.session_summary(session_id)
        manifests: list[dict[str, Any]] = []
        for take in summary["takes"]:
            manifest = take.get("manifest_summary")
            if isinstance(manifest, dict):
                manifests.append(manifest)
        return {
            "camera_id": summary["camera_id"],
            "hostname": summary["hostname"],
            "session_id": session_id,
            "exists": summary["exists"],
            "manifests": manifests,
        }

    def _session_takes_summary(self, camera_dir: Path) -> list[dict[str, Any]]:
        takes: list[dict[str, Any]] = []
        for child in sorted(camera_dir.iterdir()):
            if not child.is_dir() or child.name in {"calibration", "reference_stills"}:
                continue
            manifest_path = child / "manifest.json"
            manifest = _read_json_mapping(manifest_path)
            recording_name = None
            if manifest is not None and isinstance(manifest.get("output_file_name"), str):
                recording_name = manifest["output_file_name"]
            files = _take_file_summaries(child, camera_dir, recording_name)
            takes.append(
                {
                    "take_id": child.name,
                    "path": str(child),
                    "relative_path": str(child.relative_to(camera_dir)),
                    "manifest_summary": _manifest_harvest_summary(manifest),
                    "recording_file_name": recording_name,
                    "files": files,
                }
            )
        return takes

    def _reference_stills_summary(self, camera_dir: Path) -> list[dict[str, Any]]:
        stills_dir = camera_dir / "reference_stills"
        if not stills_dir.exists() or not stills_dir.is_dir():
            return []

        stills: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        manifests = sorted(stills_dir.glob("*_manifest.json"))
        for manifest_path in manifests:
            manifest = _read_json_mapping(manifest_path)
            label = _reference_label_from_manifest_path(manifest_path)
            if manifest is not None and isinstance(manifest.get("label"), str):
                label = manifest["label"]
            seen_labels.add(label)
            image_name = manifest.get("image_file_name") if isinstance(manifest, dict) else None
            image_path = stills_dir / str(image_name or f"{label}.jpg")
            files = _reference_still_file_summaries(
                image_path=image_path,
                manifest_path=manifest_path,
                camera_dir=camera_dir,
            )
            warnings = manifest.get("warnings", []) if isinstance(manifest, dict) and isinstance(manifest.get("warnings"), list) else []
            errors = manifest.get("errors", []) if isinstance(manifest, dict) and isinstance(manifest.get("errors"), list) else []
            stills.append(
                {
                    "label": label,
                    "path": str(stills_dir),
                    "relative_dir": str(stills_dir.relative_to(camera_dir)),
                    "image_file_name": image_path.name,
                    "manifest_file_name": manifest_path.name,
                    "image": _file_summary(image_path, camera_dir),
                    "manifest": _file_summary(manifest_path, camera_dir),
                    "manifest_summary": _still_manifest_harvest_summary(manifest),
                    "timestamp": manifest.get("timestamp") if isinstance(manifest, dict) else None,
                    "warnings": warnings,
                    "errors": errors,
                    "files": files,
                }
            )

        for image_path in sorted(stills_dir.glob("*.jpg")):
            label = image_path.stem
            if label in seen_labels:
                continue
            manifest_path = stills_dir / f"{label}_manifest.json"
            files = _reference_still_file_summaries(
                image_path=image_path,
                manifest_path=manifest_path,
                camera_dir=camera_dir,
            )
            stills.append(
                {
                    "label": label,
                    "path": str(stills_dir),
                    "relative_dir": str(stills_dir.relative_to(camera_dir)),
                    "image_file_name": image_path.name,
                    "manifest_file_name": manifest_path.name,
                    "image": _file_summary(image_path, camera_dir),
                    "manifest": _file_summary(manifest_path, camera_dir),
                    "manifest_summary": None,
                    "timestamp": None,
                    "warnings": ["reference still image has no manifest"],
                    "errors": [],
                    "files": files,
                }
            )
        return stills

    def _run_calibration_unlocked(
        self,
        session_id: str,
        profile: RecordingProfile,
        duration_seconds: float,
        calibration_id: str,
        target: str | None,
        notes: str | None,
        apply_to_session: bool,
        camera_session_dir: Path,
        calibration_dir: Path,
    ) -> dict[str, Any]:
        started_at = _utc_now()
        metadata_path = calibration_dir / "rpicam-vid.metadata.json"
        preview_file = calibration_dir / f"preview.{profile.output_extension}"
        stderr_path = calibration_dir / "rpicam-vid.stderr.log"
        manifest_path = calibration_dir / "calibration_manifest.json"
        suggested_controls_path = calibration_dir / "suggested_controls.json"

        command = self._build_calibration_command(profile, preview_file, metadata_path, duration_seconds)
        manifest: dict[str, Any] = {
            "schema_version": CALIBRATION_MANIFEST_SCHEMA_VERSION,
            "status": "running",
            "session_id": session_id,
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "service_version": __version__,
            "backend": BACKEND_NAME,
            "calibration_id": calibration_id,
            "profile": profile.name,
            "profile_snapshot": profile.as_dict(),
            "duration_seconds": _clean_number(float(duration_seconds)),
            "target": target,
            "notes": notes,
            "started_at": started_at,
            "finished_at": None,
            "output_dir": str(calibration_dir),
            "metadata_path": str(metadata_path),
            "preview_file_path": str(preview_file),
            "suggested_controls_path": str(suggested_controls_path),
            "apply_to_session_requested": apply_to_session,
            "rpicam_vid_command": command,
            "warnings": [],
            "errors": [],
            "exit_code": None,
        }
        self._write_json(manifest_path, manifest)

        exit_code, stderr_text, final_command, metadata_requested = self._run_calibration_command(
            command=command,
            cwd=calibration_dir,
            stderr_path=stderr_path,
            duration_seconds=duration_seconds,
            metadata_path=metadata_path,
            preview_file=preview_file,
            profile=profile,
        )
        metadata_result = parse_rpicam_metadata(metadata_path)
        suggested_controls = build_suggested_controls(
            metadata_result=metadata_result,
            profile_name=profile.name,
            profile_snapshot=profile.as_dict(),
            camera_id=self._config.camera_id,
            calibration_id=calibration_id,
            calibration_manifest_path=manifest_path,
        )
        warnings = list(suggested_controls.get("warnings", []))
        if not metadata_requested:
            warnings.append("rpicam-vid metadata options were not accepted; calibration ran without metadata capture")
        if exit_code != 0:
            detail = _last_stderr_line(stderr_text) or f"exit code {exit_code}"
            warnings.append(f"calibration command failed: {detail}")

        suggested_controls["warnings"] = _dedupe(warnings)
        self._write_json(suggested_controls_path, suggested_controls)

        status = "completed" if exit_code == 0 else "failed"
        manifest.update(
            {
                "status": status,
                "finished_at": _utc_now(),
                "exit_code": exit_code,
                "rpicam_vid_command": final_command,
                "metadata_requested": metadata_requested,
                "metadata_produced": metadata_path.exists() and metadata_path.stat().st_size > 0,
                "preview_produced": preview_file.exists() and preview_file.stat().st_size > 0,
                "suggested_controls": suggested_controls,
                "warnings": _dedupe(warnings),
            }
        )
        if exit_code != 0:
            manifest["errors"] = warnings[-1:]
        self._write_json(manifest_path, manifest)

        summary = {
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "session_id": session_id,
            "calibration_id": calibration_id,
            "profile": profile.name,
            "status": status,
            "started_at": started_at,
            "finished_at": manifest["finished_at"],
            "duration_seconds": _clean_number(float(duration_seconds)),
            "target": target,
            "notes": notes,
            "output_dir": str(calibration_dir),
            "calibration_manifest_path": str(manifest_path),
            "suggested_controls_path": str(suggested_controls_path),
            "metadata_path": str(metadata_path) if manifest["metadata_produced"] else None,
            "preview_file_path": str(preview_file) if manifest["preview_produced"] else None,
            "suggested_controls": suggested_controls,
            "confidence": suggested_controls.get("confidence"),
            "warnings": manifest["warnings"],
            "apply_to_session_requested": apply_to_session,
            "active_for_session": False,
            "session_calibration_dir": str(camera_session_dir / "calibration"),
        }
        return summary

    def _run_calibration_command(
        self,
        command: list[str],
        cwd: Path,
        stderr_path: Path,
        duration_seconds: float,
        metadata_path: Path,
        preview_file: Path,
        profile: RecordingProfile,
    ) -> tuple[int, str, list[str], bool]:
        timeout = max(10.0, duration_seconds + 8.0)
        with stderr_path.open("w", encoding="utf-8") as stderr_file:
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    cwd=cwd,
                    timeout=timeout,
                    check=False,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                stderr_file.write(f"\ncalibration command timed out after {timeout:g} seconds\n")
                return (
                    -1,
                    stderr_path.read_text(encoding="utf-8", errors="replace"),
                    command,
                    True,
                )
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        if result.returncode == 0 or not _looks_like_unsupported_metadata_option(stderr_text):
            return result.returncode, stderr_text, command, True

        fallback_command = self._build_calibration_command(
            profile,
            preview_file,
            None,
            duration_seconds,
        )
        metadata_path.unlink(missing_ok=True)
        with stderr_path.open("a", encoding="utf-8") as stderr_file:
            stderr_file.write("\n--- retrying without metadata options ---\n")
            try:
                fallback_result = subprocess.run(
                    fallback_command,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    cwd=cwd,
                    timeout=timeout,
                    check=False,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                stderr_file.write(f"\ncalibration fallback command timed out after {timeout:g} seconds\n")
                return (
                    -1,
                    stderr_path.read_text(encoding="utf-8", errors="replace"),
                    fallback_command,
                    False,
                )
        return (
            fallback_result.returncode,
            stderr_path.read_text(encoding="utf-8", errors="replace"),
            fallback_command,
            False,
        )

    def _prepare_locked(
        self,
        session_id: str,
        profile: RecordingProfile,
        force: bool,
        refocus: bool,
    ) -> tuple[dict[str, Any], bool]:
        self._lifecycle_state = "preparing"
        camera_session_dir = self._camera_session_dir(session_id)
        camera_session_dir.mkdir(parents=True, exist_ok=True)
        prepared_path = self._prepared_state_path(session_id)
        profile_hash = _hash_json(profile.as_dict())
        config_hash = _hash_json(
            {
                "camera_id": self._config.camera_id,
                "backend": BACKEND_NAME,
                "node_camera_control_policy": self._config.camera_control_policy.as_dict(),
                "camera_transform": self._config.camera_transform.as_dict(),
                "profile": profile.as_dict(),
            }
        )

        existing_state: dict[str, Any] | None = None
        existing_warning: str | None = None
        if force:
            prepared_path.unlink(missing_ok=True)
        else:
            existing_state, existing_warning = self._read_prepared_state(prepared_path)
            if (
                profile.camera_control_policy.reuse_prepared_controls
                and existing_state is not None
                and self._prepared_state_is_valid(existing_state, session_id, profile.name, config_hash)
                and self._prepared_state_calibration_is_current(existing_state, session_id, profile)
            ):
                if refocus:
                    existing_state = self._record_refocus_request(existing_state)
                    self._write_json(prepared_path, existing_state)
                self._prepared_state = existing_state
                self._lifecycle_state = "armed"
                return existing_state, True

        warnings = list(profile.warnings)
        if existing_warning:
            warnings.append(existing_warning)
        warnings.extend(_backend_policy_warnings(profile.camera_control_policy, refocus))
        if not profile.camera_control_policy.reuse_prepared_controls:
            warnings.append("reuse_prepared_controls is false; start will prepare again for each take")

        free_disk_bytes = self._free_disk_bytes()
        if free_disk_bytes is not None and free_disk_bytes < LOW_DISK_WARNING_BYTES:
            warnings.append(f"free disk space is low: {free_disk_bytes} bytes available")

        calibration_context = self._calibration_context_for_prepare(session_id, profile)
        if calibration_context is not None:
            warnings.extend(calibration_context.get("warnings", []))
        elif _profile_wants_calibration_suggestions(
            node_policy=self._config.camera_control_policy.as_dict(),
            profile=profile,
        ):
            warnings.append(
                "calibration suggestions were requested for auto_then_lock prepare but no suggested_controls.json was available"
            )

        warmup_performed = False
        warmup_command: list[str] | None = None
        errors: list[str] = []

        try:
            if profile.camera_control_policy.prepare_warmup_seconds > 0:
                warmup_performed = True
                warmup_command = self._run_prepare_warmup(profile, camera_session_dir)
        except Exception as exc:
            errors.append(str(exc))
            state = self._new_prepared_state(
                session_id=session_id,
                profile=profile,
                profile_hash=profile_hash,
                config_hash=config_hash,
                warnings=warnings,
                warmup_performed=warmup_performed,
                warmup_command=warmup_command,
                free_disk_bytes=free_disk_bytes,
                valid=False,
                errors=errors,
                refocus=refocus,
                calibration_context=calibration_context,
            )
            self._write_json(prepared_path, state)
            self._prepared_state = state
            self._lifecycle_state = "error"
            self._last_error = f"prepare failed: {exc}"
            raise RecorderError(f"prepare failed: {exc}") from exc

        state = self._new_prepared_state(
            session_id=session_id,
            profile=profile,
            profile_hash=profile_hash,
            config_hash=config_hash,
            warnings=warnings,
            warmup_performed=warmup_performed,
            warmup_command=warmup_command,
            free_disk_bytes=free_disk_bytes,
            valid=True,
            errors=[],
            refocus=refocus,
            calibration_context=calibration_context,
        )
        self._write_json(prepared_path, state)
        self._prepared_state = state
        self._lifecycle_state = "armed"
        return state, False

    def _new_prepared_state(
        self,
        session_id: str,
        profile: RecordingProfile,
        profile_hash: str,
        config_hash: str,
        warnings: list[str],
        warmup_performed: bool,
        warmup_command: list[str] | None,
        free_disk_bytes: int | None,
        valid: bool,
        errors: list[str],
        refocus: bool,
        calibration_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        policy = profile.camera_control_policy
        state: dict[str, Any] = {
            "schema_version": PREPARED_STATE_SCHEMA_VERSION,
            "valid": valid,
            "session_id": session_id,
            "profile": profile.name,
            "profile_hash": profile_hash,
            "config_hash": config_hash,
            "prepared_at": _utc_now(),
            "backend": BACKEND_NAME,
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "service_version": __version__,
            "camera_control_policy": policy.as_dict(),
            "requested_controls": profile.requested_controls(),
            "resolved_controls": profile.resolved_controls(),
            "camera_transform": self._config.camera_transform.as_dict(),
            "planned_applied_controls": _applied_controls_for_recording(
                profile,
                camera_transform=self._config.camera_transform.as_dict(),
            ),
            "applied_controls": (
                _warmup_applied_controls(profile, self._config.camera_transform.as_dict()) if warmup_performed else {}
            ),
            "unsupported_controls": profile.unsupported_controls,
            "warmup_performed": warmup_performed,
            "warmup_command": warmup_command,
            "free_disk_bytes_at_prepare": free_disk_bytes,
            "calibration_id": calibration_context.get("calibration_id") if calibration_context else None,
            "calibration_manifest_path": (
                calibration_context.get("calibration_manifest_path") if calibration_context else None
            ),
            "suggested_controls_path": (
                calibration_context.get("suggested_controls_path") if calibration_context else None
            ),
            "suggested_controls_snapshot": (
                calibration_context.get("suggested_controls_snapshot") if calibration_context else None
            ),
            "calibration_suggestions_applied_to_recording": False,
            "warnings": _dedupe(warnings),
            "errors": errors,
        }
        if refocus:
            state["refocus_requested"] = True
            state["refocus_requested_at"] = _utc_now()
            state["focus_prepare_behavior"] = "not_implemented_for_rpicam_vid_backend"
        return state

    def _record_refocus_request(self, prepared_state: dict[str, Any]) -> dict[str, Any]:
        updated = dict(prepared_state)
        warnings = list(updated.get("warnings", []))
        warnings.append("refocus requested; rpicam-vid backend cannot rerun focus-only prepare yet")
        updated["warnings"] = _dedupe(warnings)
        updated["refocus_requested"] = True
        updated["refocus_requested_at"] = _utc_now()
        updated["focus_prepare_behavior"] = "not_implemented_for_rpicam_vid_backend"
        return updated

    def _run_prepare_warmup(self, profile: RecordingProfile, cwd: Path) -> list[str]:
        timeout_seconds = profile.camera_control_policy.prepare_warmup_seconds
        command = [
            BACKEND_NAME,
            *_drop_flag_options(
                _drop_options(profile.rpicam_vid_args, "--output", "-o", "--timeout", "-t", "--save-pts"),
                "--hflip",
                "--vflip",
            ),
            "--output",
            os.devnull,
            "--timeout",
            str(max(1, int(timeout_seconds * 1000))),
        ]
        _extend_command_with_camera_transform(command, self._config.camera_transform.as_dict())
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=max(10.0, timeout_seconds + 5.0),
            check=False,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip().splitlines()
            detail = stderr[-1] if stderr else f"exit code {result.returncode}"
            raise RecorderError(f"prepare warmup command failed: {detail}")
        return command

    def _build_command(
        self,
        profile: RecordingProfile,
        output_file: Path,
        suggested_controls: dict[str, Any] | None = None,
    ) -> list[str]:
        command = [BACKEND_NAME, *_drop_flag_options(profile.rpicam_vid_args, "--hflip", "--vflip")]
        _extend_command_with_suggestions(command, suggestion_values(suggested_controls))
        _extend_command_with_camera_transform(command, self._config.camera_transform.as_dict())
        save_pts_path = _save_pts_path(profile, output_file)
        if save_pts_path is not None and not _contains_option(command, "--save-pts"):
            command.extend(["--save-pts", save_pts_path])
        if not _contains_option(command, "--output", "-o"):
            command.extend(["--output", str(output_file)])
        if not _contains_option(command, "--timeout", "-t"):
            command.extend(["--timeout", "0"])
        return command

    def _build_calibration_command(
        self,
        profile: RecordingProfile,
        preview_file: Path,
        metadata_path: Path | None,
        duration_seconds: float,
    ) -> list[str]:
        args = _drop_flag_options(
            _drop_options(
                profile.rpicam_vid_args,
                "--output",
                "-o",
                "--timeout",
                "-t",
                "--save-pts",
                "--shutter",
                "--gain",
                "--awbgains",
                "--autofocus-mode",
                "--lens-position",
            ),
            "--hflip",
            "--vflip",
        )
        command = [
            BACKEND_NAME,
            *args,
            "--output",
            str(preview_file),
            "--timeout",
            str(max(1, int(duration_seconds * 1000))),
        ]
        _extend_command_with_camera_transform(command, self._config.camera_transform.as_dict())
        if metadata_path is not None:
            command.extend(["--metadata", str(metadata_path), "--metadata-format", "json"])
        return command

    def _status_locked(self) -> dict[str, Any]:
        recording_running = self._state is not None and self._state.process.poll() is None
        positioning_running = self._positioning_state is not None
        prepared_state = self._prepared_state
        if prepared_state is None and self._state is not None:
            prepared_state = self._state.manifest.get("prepared_state")
        active_manifest = self._state.manifest if self._state is not None else None
        control_status = _control_status(active_manifest, self._last_recording_summary, prepared_state)
        recording_allowed = not (
            recording_running or positioning_running or self._calibration_running or self._still_capture_running
        )
        positioning_allowed = not (recording_running or self._calibration_running or self._still_capture_running)
        calibration_allowed = not (
            recording_running or positioning_running or self._calibration_running or self._still_capture_running
        )
        still_capture_allowed = not (
            recording_running or positioning_running or self._calibration_running or self._still_capture_running
        )

        return {
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "node_hostname": self._hostname,
            "backend": BACKEND_NAME,
            "state": self._lifecycle_state,
            "recording_running": recording_running,
            "recording": recording_running,
            "positioning_running": positioning_running,
            "positioning": self._positioning_status_locked(),
            "positioning_settings": dict(self._positioning_state.settings) if self._positioning_state else None,
            "positioning_snapshot_path": "/positioning/snapshot.jpg" if positioning_running else None,
            "positioning_stream_path": "/positioning/stream.mjpg" if positioning_running else None,
            "last_positioning_error": self._last_positioning_error,
            "still_capture_running": self._still_capture_running,
            "last_still_capture": self._last_still_capture_info,
            "allowed": {
                "recording": recording_allowed,
                "positioning": positioning_allowed,
                "calibration": calibration_allowed,
                "still_capture": still_capture_allowed,
            },
            "recording_allowed": recording_allowed,
            "positioning_allowed": positioning_allowed,
            "calibration_allowed": calibration_allowed,
            "still_capture_allowed": still_capture_allowed,
            "current_session_id": self._state.session_id if self._state else None,
            "current_take_id": self._state.take_id if self._state else None,
            "current_profile": self._state.profile_name if self._state else None,
            "prepared_session_id": prepared_state.get("session_id") if prepared_state else None,
            "prepared_profile": prepared_state.get("profile") if prepared_state else None,
            "prepared_valid": prepared_state.get("valid") if prepared_state else False,
            "prepared_at": prepared_state.get("prepared_at") if prepared_state else None,
            "output_path": str(self._state.output_dir) if self._state else None,
            "last_error": self._last_error,
            "process_pid": self._state.process.pid if recording_running and self._state else None,
            "free_disk_bytes": self._free_disk_bytes(),
            "service_version": __version__,
            "node_camera_control_policy": self._config.camera_control_policy.as_dict(),
            "calibration_running": self._calibration_running,
            "last_calibration_id": (
                self._last_calibration_summary.get("calibration_id") if self._last_calibration_summary else None
            ),
            "resolved_controls": control_status.get("resolved_controls"),
            "applied_controls": control_status.get("applied_controls"),
            "planned_applied_controls": control_status.get("planned_applied_controls"),
            "unsupported_controls": control_status.get("unsupported_controls"),
            "warnings": control_status.get("warnings", []),
            "last_session_id": control_status.get("last_session_id"),
            "last_take_id": control_status.get("last_take_id"),
            "last_profile": control_status.get("last_profile"),
        }

    def _finalize_if_process_exited_locked(self) -> None:
        if self._state is None:
            return

        exit_code = self._state.process.poll()
        if exit_code is None:
            return

        stopped_at = _utc_now()
        if exit_code == 0:
            self._lifecycle_state = "completed"
            self._last_error = None
            self._update_manifest_locked(status="exited", stopped_at=stopped_at, exit_code=exit_code)
        else:
            self._lifecycle_state = "error"
            self._last_error = f"recording process exited with code {exit_code}"
            self._update_manifest_locked(
                status="error",
                stopped_at=stopped_at,
                exit_code=exit_code,
                error=self._last_error,
            )
        self._close_stderr_locked()
        self._state = None

    def _update_manifest_locked(
        self,
        status: str,
        stopped_at: str | None,
        exit_code: int | None,
        error: str | None = None,
    ) -> None:
        if self._state is None:
            return

        manifest = dict(self._state.manifest)
        manifest["status"] = status
        manifest["recording_stop_time"] = stopped_at
        manifest["exit_code"] = exit_code
        manifest["process_pid"] = self._state.process.pid
        if error:
            manifest["errors"] = [*manifest.get("errors", []), error]
        self._state.manifest = manifest
        self._write_json(self._state.manifest_path, manifest)
        self._last_recording_summary = _recording_summary_from_manifest(manifest)

    def _close_stderr_locked(self) -> None:
        if self._state is not None and not self._state.stderr_file.closed:
            self._state.stderr_file.close()

    def _resolve_take_id(self, camera_session_dir: Path, take_id: str | None) -> str:
        if take_id is not None:
            self._validate_take_id(take_id)
            if (camera_session_dir / take_id).exists():
                raise TakeAlreadyExistsError(f"take_id already exists for this camera: {take_id}")
            return take_id

        highest = 0
        if camera_session_dir.exists():
            for child in camera_session_dir.iterdir():
                if not child.is_dir():
                    continue
                match = AUTO_TAKE_ID_PATTERN.fullmatch(child.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"take_{highest + 1:03d}"

    def _prepared_state_is_valid(
        self,
        prepared_state: dict[str, Any],
        session_id: str,
        profile_name: str,
        config_hash: str,
    ) -> bool:
        return (
            prepared_state.get("valid") is True
            and prepared_state.get("session_id") == session_id
            and prepared_state.get("profile") == profile_name
            and prepared_state.get("config_hash") == config_hash
        )

    def _prepared_state_calibration_is_current(
        self,
        prepared_state: dict[str, Any],
        session_id: str,
        profile: RecordingProfile,
    ) -> bool:
        if not _profile_wants_calibration_suggestions(
            node_policy=self._config.camera_control_policy.as_dict(),
            profile=profile,
        ):
            return True
        summary = self._calibration_summary_for_session(session_id, None, allow_missing=True)
        if summary is None:
            return prepared_state.get("suggested_controls_snapshot") is None
        return (
            prepared_state.get("calibration_id") == summary.get("calibration_id")
            and prepared_state.get("suggested_controls_path") == summary.get("suggested_controls_path")
        )

    def _read_prepared_state(self, path: Path) -> tuple[dict[str, Any] | None, str | None]:
        if not path.exists():
            return None, None
        try:
            with path.open("r", encoding="utf-8") as prepared_file:
                data = json.load(prepared_file)
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"existing prepared_state.json could not be read and was recreated: {exc}"
        if not isinstance(data, dict):
            return None, "existing prepared_state.json was not a JSON object and was recreated"
        return data, None

    def _camera_session_dir(self, session_id: str) -> Path:
        return self._config.output_root / session_id / self._config.camera_id

    def _prepared_state_path(self, session_id: str) -> Path:
        return self._camera_session_dir(session_id) / "prepared_state.json"

    def _calibration_root(self, session_id: str) -> Path:
        return self._camera_session_dir(session_id) / "calibration"

    def _calibration_dir(self, session_id: str, calibration_id: str) -> Path:
        return self._calibration_root(session_id) / calibration_id

    def _last_calibration_path(self, session_id: str) -> Path:
        return self._calibration_root(session_id) / "last.json"

    def _active_calibration_path(self, session_id: str) -> Path:
        return self._calibration_root(session_id) / "active_suggestions.json"

    def _reference_stills_dir(self, session_id: str) -> Path:
        return self._camera_session_dir(session_id) / "reference_stills"

    def _resolve_positioning_settings(
        self,
        width: int | None,
        height: int | None,
        fps: int | None,
        jpeg_quality: int | None,
        overlays: list[str] | None,
        profile_name: str | None,
    ) -> dict[str, Any]:
        defaults = self._config.positioning
        return {
            "width": self._resolve_dimension(width, defaults.width, "width"),
            "height": self._resolve_dimension(height, defaults.height, "height"),
            "fps": self._resolve_dimension(fps, defaults.fps, "fps"),
            "jpeg_quality": self._resolve_quality(jpeg_quality, defaults.jpeg_quality, "jpeg_quality"),
            "overlays": normalize_positioning_overlays(defaults.overlays if overlays is None else overlays),
            "snapshot_path": "/positioning/snapshot.jpg",
            "stream_path": "/positioning/stream.mjpg",
            "profile": profile_name,
            "backend": POSITIONING_BACKEND_NAME,
        }

    @staticmethod
    def _resolve_dimension(value: int | None, default: int, name: str) -> int:
        resolved = default if value is None else value
        if isinstance(resolved, bool):
            raise RecorderError(f"{name} must be an integer >= 1")
        try:
            resolved_int = int(resolved)
        except (TypeError, ValueError) as exc:
            raise RecorderError(f"{name} must be an integer >= 1") from exc
        if isinstance(resolved, float) and resolved_int != resolved:
            raise RecorderError(f"{name} must be an integer >= 1")
        if resolved_int < 1:
            raise RecorderError(f"{name} must be an integer >= 1")
        return resolved_int

    @staticmethod
    def _resolve_optional_dimension(value: int | None, default: int | None, name: str) -> int | None:
        resolved = default if value is None else value
        if resolved is None:
            return None
        if isinstance(resolved, bool):
            raise RecorderError(f"{name} must be an integer >= 1")
        try:
            resolved_int = int(resolved)
        except (TypeError, ValueError) as exc:
            raise RecorderError(f"{name} must be an integer >= 1") from exc
        if isinstance(resolved, float) and resolved_int != resolved:
            raise RecorderError(f"{name} must be an integer >= 1")
        if resolved_int < 1:
            raise RecorderError(f"{name} must be an integer >= 1")
        return resolved_int

    @staticmethod
    def _resolve_quality(value: int | None, default: int, name: str) -> int:
        resolved = default if value is None else value
        if isinstance(resolved, bool):
            raise RecorderError(f"{name} must be an integer between 1 and 100")
        try:
            resolved_int = int(resolved)
        except (TypeError, ValueError) as exc:
            raise RecorderError(f"{name} must be an integer between 1 and 100") from exc
        if isinstance(resolved, float) and resolved_int != resolved:
            raise RecorderError(f"{name} must be an integer between 1 and 100")
        if resolved_int < 1 or resolved_int > 100:
            raise RecorderError(f"{name} must be an integer between 1 and 100")
        return resolved_int

    def _positioning_status_locked(self) -> dict[str, Any]:
        state = self._positioning_state
        return {
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "running": state is not None,
            "state": self._lifecycle_state,
            "settings": dict(state.settings) if state else None,
            "snapshot_path": state.settings.get("snapshot_path") if state else None,
            "stream_path": state.settings.get("stream_path") if state else None,
            "started_at": state.started_at if state else None,
            "updated_at": state.updated_at if state else None,
            "frames_served": state.frames_served if state else 0,
            "last_frame_at": state.last_frame_at if state else None,
            "last_backend": state.last_backend if state else None,
            "last_command": state.last_command if state else None,
            "last_error": state.last_error if state else self._last_positioning_error,
            "warnings": state.warnings if state else [],
        }

    def _record_positioning_error(self, error: str) -> None:
        with self._lock:
            self._last_positioning_error = error
            if self._positioning_state is not None:
                self._positioning_state.last_error = error
            self._last_error = error

    def _resolve_reference_still_label(
        self,
        stills_dir: Path,
        requested_label: str | None,
        force: bool,
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        if requested_label is None:
            label = self._next_reference_label(stills_dir)
        else:
            label = requested_label

        image_path = stills_dir / f"{label}.jpg"
        manifest_path = stills_dir / f"{label}_manifest.json"
        if force:
            if image_path.exists() or manifest_path.exists():
                warnings.append(f"force requested; overwriting existing reference still label {label}")
            return label, warnings

        if not image_path.exists() and not manifest_path.exists():
            return label, warnings

        base = label
        index = 2
        while True:
            candidate = f"{base}_{index:03d}"
            candidate_image = stills_dir / f"{candidate}.jpg"
            candidate_manifest = stills_dir / f"{candidate}_manifest.json"
            if not candidate_image.exists() and not candidate_manifest.exists():
                warnings.append(f"label {label!r} already exists; using {candidate!r}")
                return candidate, warnings
            index += 1

    @staticmethod
    def _next_reference_label(stills_dir: Path) -> str:
        highest = 0
        if stills_dir.exists():
            for child in stills_dir.iterdir():
                if not child.is_file():
                    continue
                label = _reference_label_from_manifest_path(child) if child.name.endswith("_manifest.json") else child.stem
                match = AUTO_REFERENCE_LABEL_PATTERN.fullmatch(label)
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"reference_{highest + 1:03d}"

    @staticmethod
    def _validate_still_label(label: str) -> None:
        if STILL_LABEL_PATTERN.fullmatch(label) is None:
            raise InvalidStillLabelError(
                "label must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens"
            )

    def _calibration_context_for_prepare(
        self,
        session_id: str,
        profile: RecordingProfile,
    ) -> dict[str, Any] | None:
        if not _profile_wants_calibration_suggestions(
            node_policy=self._config.camera_control_policy.as_dict(),
            profile=profile,
        ):
            return None
        summary = self._calibration_summary_for_session(session_id, None, allow_missing=True)
        if summary is None:
            return None
        suggestions = summary.get("suggested_controls")
        if not isinstance(suggestions, dict):
            return None
        values = suggestion_values(suggestions)
        warnings = list(summary.get("warnings", []))
        if not values:
            warnings.append("calibration suggestions were found but no lockable values were available")
        return {
            "calibration_id": summary.get("calibration_id"),
            "calibration_manifest_path": summary.get("calibration_manifest_path"),
            "suggested_controls_path": summary.get("suggested_controls_path"),
            "suggested_controls_snapshot": suggestions,
            "warnings": warnings,
        }

    def _calibration_summary_for_session(
        self,
        session_id: str,
        calibration_id: str | None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        if calibration_id is not None:
            path = self._calibration_dir(session_id, calibration_id) / "suggested_controls.json"
            manifest_path = self._calibration_dir(session_id, calibration_id) / "calibration_manifest.json"
            if not path.exists():
                if allow_missing:
                    return None
                raise RecorderError(f"calibration suggestions not found: {calibration_id}")
            suggestions = self._read_json(path)
            manifest = self._read_json(manifest_path) if manifest_path.exists() else {}
            return {
                "camera_id": self._config.camera_id,
                "hostname": self._hostname,
                "session_id": session_id,
                "calibration_id": calibration_id,
                "profile": suggestions.get("profile"),
                "status": manifest.get("status", "unknown"),
                "calibration_manifest_path": str(manifest_path),
                "suggested_controls_path": str(path),
                "suggested_controls": suggestions,
                "confidence": suggestions.get("confidence"),
                "warnings": suggestions.get("warnings", []),
            }

        for path in (self._active_calibration_path(session_id), self._last_calibration_path(session_id)):
            if path.exists():
                data = self._read_json(path)
                if isinstance(data, dict):
                    return data
        if allow_missing:
            return None
        raise RecorderError(f"no calibration suggestions found for session: {session_id}")

    def _activate_calibration_suggestions_locked(self, session_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        active = dict(summary)
        active["active_for_session"] = True
        active["activated_at"] = _utc_now()
        self._write_json(self._active_calibration_path(session_id), active)
        self._last_calibration_summary = active
        return active

    def _read_last_calibration_summary(self) -> dict[str, Any] | None:
        if self._last_calibration_summary is not None:
            return self._last_calibration_summary
        root = self._config.output_root
        if not root.exists():
            return None
        candidates = sorted(root.glob(f"*/{self._config.camera_id}/calibration/last.json"), key=lambda path: path.stat().st_mtime)
        if not candidates:
            return None
        try:
            self._last_calibration_summary = self._read_json(candidates[-1])
        except RecorderError:
            return None
        return self._last_calibration_summary

    def _get_profile(self, profile_name: str) -> RecordingProfile:
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise UnknownProfileError(f"unknown recording profile: {profile_name}")
        return profile

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise InvalidSessionIdError(
                "session_id must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens"
            )

    @staticmethod
    def _validate_take_id(take_id: str) -> None:
        if TAKE_ID_PATTERN.fullmatch(take_id) is None:
            raise InvalidTakeIdError(
                "take_id must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens"
            )

    @staticmethod
    def _validate_calibration_id(calibration_id: str) -> None:
        if CALIBRATION_ID_PATTERN.fullmatch(calibration_id) is None:
            raise InvalidCalibrationIdError(
                "calibration_id must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens"
            )

    def _free_disk_bytes(self) -> int | None:
        path = self._config.output_root
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, indent=2, sort_keys=True)
            json_file.write("\n")
        temp_path.replace(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as json_file:
                data = json.load(json_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RecorderError(f"could not read JSON file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RecorderError(f"JSON file {path} was not an object")
        return data


def _contains_option(command: list[str], *options: str) -> bool:
    for item in command:
        if item in options:
            return True
        if any(item.startswith(f"{option}=") for option in options):
            return True
    return False


def _drop_options(args: list[str], *options: str) -> list[str]:
    dropped: list[str] = []
    skip_next = False
    options_with_values = set(options)
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item in options_with_values:
            skip_next = True
            continue
        if any(item.startswith(f"{option}=") for option in options_with_values):
            continue
        dropped.append(item)
    return dropped


def _drop_flag_options(args: list[str], *options: str) -> list[str]:
    return [item for item in args if item not in options]


def _applied_controls_for_recording(
    profile: RecordingProfile,
    output_file: Path | None = None,
    suggested_controls: dict[str, Any] | None = None,
    camera_transform: dict[str, Any] | None = None,
) -> dict[str, Any]:
    applied = dict(profile.planned_applied_controls)
    _extend_applied_controls_with_camera_transform(applied, camera_transform or {})
    suggested_values = suggestion_values(suggested_controls)
    if suggested_values:
        applied.update(_applied_controls_from_suggestion_values(suggested_values))
        applied["calibration_suggestions_applied"] = True
    save_pts_path = _save_pts_path(profile, output_file)
    if save_pts_path is not None and not _contains_option([BACKEND_NAME, *profile.rpicam_vid_args], "--save-pts"):
        applied["save_pts"] = save_pts_path
    if applied:
        applied["backend"] = BACKEND_NAME
    return applied


def _warmup_applied_controls(profile: RecordingProfile, camera_transform: dict[str, Any] | None = None) -> dict[str, Any]:
    applied = dict(profile.planned_applied_controls)
    _extend_applied_controls_with_camera_transform(applied, camera_transform or {})
    applied.pop("duration", None)
    applied.pop("timeout_ms", None)
    if applied:
        applied["backend"] = BACKEND_NAME
    return applied


def _extend_applied_controls_with_camera_transform(applied: dict[str, Any], camera_transform: dict[str, Any]) -> None:
    if camera_transform.get("hflip") is True:
        applied["hflip"] = True
    if camera_transform.get("vflip") is True:
        applied["vflip"] = True


def _extend_command_with_camera_transform(command: list[str], camera_transform: dict[str, Any]) -> None:
    if camera_transform.get("hflip") is True and not _contains_option(command, "--hflip"):
        command.append("--hflip")
    if camera_transform.get("vflip") is True and not _contains_option(command, "--vflip"):
        command.append("--vflip")


def _extend_command_with_suggestions(command: list[str], values: dict[str, Any]) -> None:
    shutter_us = values.get("shutter_us")
    if shutter_us is not None and not _contains_option(command, "--shutter"):
        command.extend(["--shutter", str(shutter_us)])
    gain = values.get("gain")
    if gain is not None and not _contains_option(command, "--gain"):
        command.extend(["--gain", str(gain)])
    awbgains = values.get("awbgains")
    if isinstance(awbgains, list) and len(awbgains) == 2 and not _contains_option(command, "--awbgains"):
        command.extend(["--awbgains", ",".join(str(item) for item in awbgains)])
    lens_position = values.get("lens_position")
    if lens_position is not None:
        if not _contains_option(command, "--autofocus-mode"):
            command.extend(["--autofocus-mode", "manual"])
        if not _contains_option(command, "--lens-position"):
            command.extend(["--lens-position", str(lens_position)])


def _applied_controls_from_suggestion_values(values: dict[str, Any]) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    for key in ("shutter_us", "gain", "awbgains", "lens_position"):
        if values.get(key) is not None:
            applied[key] = values[key]
    if values.get("lens_position") is not None:
        applied["autofocus_mode"] = "manual"
    return applied


def _save_pts_path(profile: RecordingProfile, output_file: Path | None) -> str | None:
    save_pts = profile.recording.get("save_pts")
    if save_pts is None or save_pts is False:
        return None
    if isinstance(save_pts, str):
        return save_pts
    if output_file is None:
        return "enabled"
    return str(output_file.with_suffix(".pts"))


def _recording_summary_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": manifest.get("session_id"),
        "take_id": manifest.get("take_id"),
        "profile": manifest.get("profile"),
        "resolved_controls": manifest.get("resolved_controls"),
        "applied_controls": manifest.get("applied_controls"),
        "unsupported_controls": manifest.get("unsupported_controls"),
        "warnings": manifest.get("warnings", []),
    }


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _file_summary(path: Path, base_dir: Path) -> dict[str, Any]:
    exists = path.exists()
    summary: dict[str, Any] = {
        "name": path.name,
        "path": str(path),
        "relative_path": str(path.relative_to(base_dir)) if exists else path.name,
        "exists": exists,
    }
    if exists and path.is_file():
        stat = path.stat()
        summary["size"] = stat.st_size
        summary["mtime"] = stat.st_mtime
    return summary


def _take_file_summaries(take_dir: Path, camera_dir: Path, recording_name: str | None) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    expected_names = ["manifest.json"]
    if recording_name:
        expected_names.append(recording_name)
    expected_names.append("rpicam-vid.stderr.log")

    seen: set[str] = set()
    for name in expected_names:
        seen.add(name)
        summary = _file_summary(take_dir / name, camera_dir)
        summary["kind"] = _file_kind(name, recording_name)
        files.append(summary)

    for child in sorted(take_dir.iterdir()):
        if not child.is_file() or child.name in seen:
            continue
        summary = _file_summary(child, camera_dir)
        summary["kind"] = _file_kind(child.name, recording_name)
        files.append(summary)
    return files


def _reference_still_file_summaries(image_path: Path, manifest_path: Path, camera_dir: Path) -> list[dict[str, Any]]:
    image_summary = _file_summary(image_path, camera_dir)
    image_summary["relative_path"] = str(image_path.relative_to(camera_dir))
    image_summary["kind"] = "reference_still_image"

    manifest_summary = _file_summary(manifest_path, camera_dir)
    manifest_summary["relative_path"] = str(manifest_path.relative_to(camera_dir))
    manifest_summary["kind"] = "reference_still_manifest"
    return [image_summary, manifest_summary]


def _file_kind(name: str, recording_name: str | None) -> str:
    if name == "manifest.json":
        return "manifest"
    if name == "rpicam-vid.stderr.log":
        return "stderr_log"
    if recording_name and name == recording_name:
        return "recording"
    return "extra"


def _reference_label_from_manifest_path(path: Path) -> str:
    stem = path.stem
    return stem[: -len("_manifest")] if stem.endswith("_manifest") else stem


def _manifest_harvest_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    fields = (
        "schema_version",
        "status",
        "session_id",
        "take_id",
        "camera_id",
        "hostname",
        "service_version",
        "backend",
        "profile",
        "recording_start_time",
        "recording_stop_time",
        "pre_roll_seconds",
        "usable_start_offset_seconds",
        "usable_start_time",
        "output_file_name",
        "exit_code",
        "warnings",
        "errors",
    )
    return {field: manifest.get(field) for field in fields if field in manifest}


def _still_manifest_harvest_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    fields = (
        "schema_version",
        "status",
        "session_id",
        "camera_id",
        "label",
        "requested_label",
        "timestamp",
        "finished_at",
        "hostname",
        "service_version",
        "image_file_name",
        "requested_size",
        "requested_quality",
        "actual_file_size",
        "backend",
        "profile",
        "use_recording_profile_controls",
        "warnings",
        "errors",
        "state_before_capture",
        "positioning_behavior",
    )
    return {field: manifest.get(field) for field in fields if field in manifest}


def _still_capture_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": manifest.get("status"),
        "session_id": manifest.get("session_id"),
        "camera_id": manifest.get("camera_id"),
        "label": manifest.get("label"),
        "timestamp": manifest.get("timestamp"),
        "finished_at": manifest.get("finished_at"),
        "image_file_path": manifest.get("image_file_path"),
        "manifest_path": manifest.get("manifest_path"),
        "actual_file_size": manifest.get("actual_file_size"),
        "backend": manifest.get("backend"),
        "profile": manifest.get("profile"),
        "warnings": manifest.get("warnings", []),
        "errors": manifest.get("errors", []),
    }


def _control_status(
    active_manifest: dict[str, Any] | None,
    last_recording_summary: dict[str, Any] | None,
    prepared_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if active_manifest is not None:
        return {
            "resolved_controls": active_manifest.get("resolved_controls"),
            "applied_controls": active_manifest.get("applied_controls"),
            "planned_applied_controls": None,
            "unsupported_controls": active_manifest.get("unsupported_controls"),
            "warnings": active_manifest.get("warnings", []),
            "last_session_id": active_manifest.get("session_id"),
            "last_take_id": active_manifest.get("take_id"),
            "last_profile": active_manifest.get("profile"),
        }
    if last_recording_summary is not None:
        return {
            "resolved_controls": last_recording_summary.get("resolved_controls"),
            "applied_controls": last_recording_summary.get("applied_controls"),
            "planned_applied_controls": None,
            "unsupported_controls": last_recording_summary.get("unsupported_controls"),
            "warnings": last_recording_summary.get("warnings", []),
            "last_session_id": last_recording_summary.get("session_id"),
            "last_take_id": last_recording_summary.get("take_id"),
            "last_profile": last_recording_summary.get("profile"),
        }
    if prepared_state is not None:
        return {
            "resolved_controls": prepared_state.get("resolved_controls"),
            "applied_controls": prepared_state.get("applied_controls"),
            "planned_applied_controls": prepared_state.get("planned_applied_controls"),
            "unsupported_controls": prepared_state.get("unsupported_controls"),
            "warnings": prepared_state.get("warnings", []),
            "last_session_id": None,
            "last_take_id": None,
            "last_profile": None,
        }
    return {}


def _profile_wants_calibration_suggestions(node_policy: dict[str, Any], profile: RecordingProfile) -> bool:
    policy = profile.camera_control_policy
    if not (
        policy.exposure_mode == "auto_then_lock"
        or policy.awb_mode == "auto_then_lock"
        or policy.focus_mode == "auto_then_lock"
    ):
        return False
    return bool(node_policy.get("use_calibration_suggestions") or policy.use_calibration_suggestions)


def _suggestions_apply_allowed(
    node_policy: dict[str, Any],
    profile: RecordingProfile,
    start_requested: bool,
) -> bool:
    return bool(
        start_requested
        or profile.camera_control_policy.apply_suggestions_to_recording
        or node_policy.get("apply_suggestions_to_recording")
    )


def _calibration_context_from_prepared_state(prepared_state: dict[str, Any]) -> dict[str, Any] | None:
    suggestions = prepared_state.get("suggested_controls_snapshot")
    if not isinstance(suggestions, dict):
        return None
    return {
        "calibration_id": prepared_state.get("calibration_id"),
        "calibration_manifest_path": prepared_state.get("calibration_manifest_path"),
        "suggested_controls_path": prepared_state.get("suggested_controls_path"),
        "suggested_controls_snapshot": suggestions,
    }


def _backend_policy_warnings(policy: CameraControlPolicy, refocus: bool) -> list[str]:
    warnings: list[str] = []
    if policy.exposure_mode == "auto_then_lock" or policy.awb_mode == "auto_then_lock":
        warnings.append(
            "AE/AWB auto_then_lock is experimental on rpicam-vid; lock values require calibration suggestions and apply_suggestions_to_recording"
        )
    if policy.focus_mode == "auto_then_lock" or refocus:
        warnings.append(
            "AF auto_then_lock/refocus is experimental on rpicam-vid; focus values require metadata support and apply_suggestions_to_recording"
        )
    if policy.focus_mode == "continuous":
        warnings.append("continuous autofocus requested; avoid for final takes unless intentionally configured")
    return warnings


def _hash_json(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat()


def _utc_now() -> str:
    return _format_timestamp(_utc_now_dt())


def _default_calibration_id() -> str:
    return "cal_" + _utc_now_dt().strftime("%Y%m%dT%H%M%SZ")


def _looks_like_unsupported_metadata_option(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return "metadata" in lowered and (
        "unrecognised" in lowered
        or "unrecognized" in lowered
        or "unknown option" in lowered
        or "invalid option" in lowered
        or "unexpected" in lowered
    )


def _last_stderr_line(stderr_text: str) -> str | None:
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    return lines[-1] if lines else None
