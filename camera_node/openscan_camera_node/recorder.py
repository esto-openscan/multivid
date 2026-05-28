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
from .config import CameraNodeConfig
from .profiles import CameraControlPolicy, RecordingProfile, RecordingProfiles


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
TAKE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
AUTO_TAKE_ID_PATTERN = re.compile(r"^take_(\d{3})$")
PROCESS_STOP_TIMEOUT_SECONDS = 10
LOW_DISK_WARNING_BYTES = 500 * 1024 * 1024
BACKEND_NAME = "rpicam-vid"
MANIFEST_SCHEMA_VERSION = 3
PREPARED_STATE_SCHEMA_VERSION = 2


class RecorderError(Exception):
    """Base class for expected recorder failures."""


class AlreadyRecordingError(RecorderError):
    pass


class UnknownProfileError(RecorderError):
    pass


class InvalidSessionIdError(RecorderError):
    pass


class InvalidTakeIdError(RecorderError):
    pass


class TakeAlreadyExistsError(RecorderError):
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


class RpicamVidRecorder:
    def __init__(self, config: CameraNodeConfig, profiles: RecordingProfiles) -> None:
        self._config = config
        self._profiles = profiles
        self._lock = threading.Lock()
        self._state: RecordingState | None = None
        self._lifecycle_state = "idle"
        self._last_error: str | None = None
        self._last_recording_summary: dict[str, Any] | None = None
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
    ) -> dict[str, Any]:
        with self._lock:
            try:
                self._finalize_if_process_exited_locked()

                if self._state is not None and self._state.process.poll() is None:
                    raise AlreadyRecordingError("recording is already running")

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

                command = self._build_command(profile, output_file)
                applied_controls = _applied_controls_for_recording(profile, output_file)
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
                self._lifecycle_state = "error"
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
                self._lifecycle_state = "error"
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

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            return self._status_locked()

    def profiles(self) -> dict[str, dict[str, Any]]:
        return self._profiles.as_dict()

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
            "planned_applied_controls": _applied_controls_for_recording(profile),
            "applied_controls": _warmup_applied_controls(profile) if warmup_performed else {},
            "unsupported_controls": profile.unsupported_controls,
            "warmup_performed": warmup_performed,
            "warmup_command": warmup_command,
            "free_disk_bytes_at_prepare": free_disk_bytes,
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
            *_drop_options(profile.rpicam_vid_args, "--output", "-o", "--timeout", "-t", "--save-pts"),
            "--output",
            os.devnull,
            "--timeout",
            str(max(1, int(timeout_seconds * 1000))),
        ]
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

    def _build_command(self, profile: RecordingProfile, output_file: Path) -> list[str]:
        command = [BACKEND_NAME, *profile.rpicam_vid_args]
        save_pts_path = _save_pts_path(profile, output_file)
        if save_pts_path is not None and not _contains_option(command, "--save-pts"):
            command.extend(["--save-pts", save_pts_path])
        if not _contains_option(command, "--output", "-o"):
            command.extend(["--output", str(output_file)])
        if not _contains_option(command, "--timeout", "-t"):
            command.extend(["--timeout", "0"])
        return command

    def _status_locked(self) -> dict[str, Any]:
        recording_running = self._state is not None and self._state.process.poll() is None
        prepared_state = self._prepared_state
        if prepared_state is None and self._state is not None:
            prepared_state = self._state.manifest.get("prepared_state")
        active_manifest = self._state.manifest if self._state is not None else None
        control_status = _control_status(active_manifest, self._last_recording_summary, prepared_state)

        return {
            "camera_id": self._config.camera_id,
            "hostname": self._hostname,
            "node_hostname": self._hostname,
            "backend": BACKEND_NAME,
            "state": self._lifecycle_state,
            "recording_running": recording_running,
            "recording": recording_running,
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
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, indent=2, sort_keys=True)
            json_file.write("\n")
        temp_path.replace(path)


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


def _applied_controls_for_recording(profile: RecordingProfile, output_file: Path | None = None) -> dict[str, Any]:
    applied = dict(profile.planned_applied_controls)
    save_pts_path = _save_pts_path(profile, output_file)
    if save_pts_path is not None and not _contains_option([BACKEND_NAME, *profile.rpicam_vid_args], "--save-pts"):
        applied["save_pts"] = save_pts_path
    if applied:
        applied["backend"] = BACKEND_NAME
    return applied


def _warmup_applied_controls(profile: RecordingProfile) -> dict[str, Any]:
    applied = dict(profile.planned_applied_controls)
    applied.pop("duration", None)
    applied.pop("timeout_ms", None)
    if applied:
        applied["backend"] = BACKEND_NAME
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


def _backend_policy_warnings(policy: CameraControlPolicy, refocus: bool) -> list[str]:
    warnings: list[str] = []
    if policy.exposure_mode == "auto_then_lock" or policy.awb_mode == "auto_then_lock":
        warnings.append("AE/AWB lock not implemented for rpicam-vid backend yet; requested lock will run as auto")
    if policy.focus_mode == "auto_then_lock" or refocus:
        warnings.append("AF lock/refocus not implemented for rpicam-vid backend yet")
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
