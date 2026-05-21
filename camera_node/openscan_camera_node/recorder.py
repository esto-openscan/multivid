from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .config import CameraNodeConfig
from .profiles import RecordingProfile, RecordingProfiles


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROCESS_STOP_TIMEOUT_SECONDS = 10


class RecorderError(Exception):
    """Base class for expected recorder failures."""


class AlreadyRecordingError(RecorderError):
    pass


class UnknownProfileError(RecorderError):
    pass


class InvalidSessionIdError(RecorderError):
    pass


@dataclass
class RecordingState:
    session_id: str
    profile_name: str
    output_dir: Path
    output_file: Path
    manifest_path: Path
    command: list[str]
    started_at: str
    process: subprocess.Popen[bytes]
    stderr_file: BinaryIO


class RpicamVidRecorder:
    def __init__(self, config: CameraNodeConfig, profiles: RecordingProfiles) -> None:
        self._config = config
        self._profiles = profiles
        self._lock = threading.Lock()
        self._state: RecordingState | None = None

    def start(self, session_id: str, profile_name: str) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()

            if self._state is not None and self._state.process.poll() is None:
                raise AlreadyRecordingError("recording is already running")

            if SESSION_ID_PATTERN.fullmatch(session_id) is None:
                raise InvalidSessionIdError(
                    "session_id must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens"
                )

            profile = self._profiles.get(profile_name)
            if profile is None:
                raise UnknownProfileError(f"unknown recording profile: {profile_name}")

            output_dir = self._config.output_root / session_id / self._config.camera_id
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / f"{self._config.camera_id}.{profile.output_extension}"
            manifest_path = output_dir / "manifest.json"
            command = self._build_command(profile, output_file)
            started_at = _utc_now()

            self._write_manifest(
                manifest_path,
                {
                    "schema_version": 1,
                    "camera_id": self._config.camera_id,
                    "session_id": session_id,
                    "profile": profile_name,
                    "status": "starting",
                    "started_at": started_at,
                    "ended_at": None,
                    "output_dir": str(output_dir),
                    "output_file": str(output_file),
                    "command": command,
                    "process_pid": None,
                    "exit_code": None,
                },
            )

            stderr_file = (output_dir / "rpicam-vid.stderr.log").open("ab")
            try:
                # start_new_session gives us a process group to terminate if rpicam-vid spawns helpers.
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    cwd=output_dir,
                    start_new_session=True,
                )
            except Exception:
                stderr_file.close()
                self._write_manifest(
                    manifest_path,
                    {
                        "schema_version": 1,
                        "camera_id": self._config.camera_id,
                        "session_id": session_id,
                        "profile": profile_name,
                        "status": "failed_to_start",
                        "started_at": started_at,
                        "ended_at": _utc_now(),
                        "output_dir": str(output_dir),
                        "output_file": str(output_file),
                        "command": command,
                        "process_pid": None,
                        "exit_code": None,
                    },
                )
                raise

            self._state = RecordingState(
                session_id=session_id,
                profile_name=profile_name,
                output_dir=output_dir,
                output_file=output_file,
                manifest_path=manifest_path,
                command=command,
                started_at=started_at,
                process=process,
                stderr_file=stderr_file,
            )
            self._update_manifest_locked(status="recording", ended_at=None, exit_code=None)
            return self._status_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return self._status_locked()

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

            self._update_manifest_locked(status=status, ended_at=_utc_now(), exit_code=exit_code)
            self._close_stderr_locked()
            self._state = None
            return self._status_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._finalize_if_process_exited_locked()
            return self._status_locked()

    def profiles(self) -> dict[str, dict[str, Any]]:
        return self._profiles.as_dict()

    def _build_command(self, profile: RecordingProfile, output_file: Path) -> list[str]:
        command = ["rpicam-vid", *profile.rpicam_vid_args]
        if not _contains_option(command, "--output", "-o"):
            command.extend(["--output", str(output_file)])
        if not _contains_option(command, "--timeout", "-t"):
            command.extend(["--timeout", "0"])
        return command

    def _status_locked(self) -> dict[str, Any]:
        if self._state is None:
            return {
                "camera_id": self._config.camera_id,
                "recording": False,
                "current_session_id": None,
                "current_profile": None,
                "output_path": None,
                "process_pid": None,
            }

        process = self._state.process
        return {
            "camera_id": self._config.camera_id,
            "recording": process.poll() is None,
            "current_session_id": self._state.session_id,
            "current_profile": self._state.profile_name,
            "output_path": str(self._state.output_dir),
            "process_pid": process.pid if process.poll() is None else None,
        }

    def _finalize_if_process_exited_locked(self) -> None:
        if self._state is None:
            return

        exit_code = self._state.process.poll()
        if exit_code is None:
            return

        self._update_manifest_locked(status="exited", ended_at=_utc_now(), exit_code=exit_code)
        self._close_stderr_locked()
        self._state = None

    def _update_manifest_locked(self, status: str, ended_at: str | None, exit_code: int | None) -> None:
        if self._state is None:
            return

        self._write_manifest(
            self._state.manifest_path,
            {
                "schema_version": 1,
                "camera_id": self._config.camera_id,
                "session_id": self._state.session_id,
                "profile": self._state.profile_name,
                "status": status,
                "started_at": self._state.started_at,
                "ended_at": ended_at,
                "output_dir": str(self._state.output_dir),
                "output_file": str(self._state.output_file),
                "command": self._state.command,
                "process_pid": self._state.process.pid,
                "exit_code": exit_code,
            },
        )

    def _close_stderr_locked(self) -> None:
        if self._state is not None and not self._state.stderr_file.closed:
            self._state.stderr_file.close()

    @staticmethod
    def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
        temp_path = path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
        temp_path.replace(path)


def _contains_option(command: list[str], *options: str) -> bool:
    for item in command:
        if item in options:
            return True
        if any(item.startswith(f"{option}=") for option in options):
            return True
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
