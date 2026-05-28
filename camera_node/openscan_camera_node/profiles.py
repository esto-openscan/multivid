from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


EXPOSURE_MODES = {"auto", "manual", "auto_then_lock"}
AWB_MODES = {"auto", "manual", "auto_then_lock"}
FOCUS_MODES = {"auto", "manual", "auto_then_lock", "continuous"}
CAMERA_CONTROL_POLICY_FIELDS = {
    "pre_roll_seconds",
    "exposure_mode",
    "awb_mode",
    "focus_mode",
    "reuse_prepared_controls",
    "refocus_on_each_take",
    "prepare_warmup_seconds",
}


@dataclass(frozen=True)
class CameraControlPolicy:
    pre_roll_seconds: float
    exposure_mode: str
    awb_mode: str
    focus_mode: str
    reuse_prepared_controls: bool
    refocus_on_each_take: bool
    prepare_warmup_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "pre_roll_seconds": _clean_number(self.pre_roll_seconds),
            "exposure_mode": self.exposure_mode,
            "awb_mode": self.awb_mode,
            "focus_mode": self.focus_mode,
            "reuse_prepared_controls": self.reuse_prepared_controls,
            "refocus_on_each_take": self.refocus_on_each_take,
            "prepare_warmup_seconds": _clean_number(self.prepare_warmup_seconds),
        }


@dataclass(frozen=True)
class RecordingProfile:
    name: str
    description: str
    output_extension: str
    rpicam_vid_args: list[str]
    camera_control_policy: CameraControlPolicy
    unsupported_camera_control_policy: dict[str, Any]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description,
            "output_extension": self.output_extension,
            "rpicam_vid_args": self.rpicam_vid_args,
            "camera_control_policy": self.camera_control_policy.as_dict(),
        }
        if self.unsupported_camera_control_policy:
            data["unsupported_camera_control_policy"] = self.unsupported_camera_control_policy
        if self.warnings:
            data["warnings"] = self.warnings
        return data


class RecordingProfiles:
    def __init__(self, profiles: dict[str, RecordingProfile]) -> None:
        self._profiles = profiles

    def get(self, name: str) -> RecordingProfile | None:
        return self._profiles.get(name)

    def names(self) -> list[str]:
        return sorted(self._profiles.keys())

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {name: profile.as_dict() for name, profile in sorted(self._profiles.items())}


def load_recording_profiles(path: str | Path) -> RecordingProfiles:
    profiles_path = Path(path)
    with profiles_path.open("r", encoding="utf-8") as profiles_file:
        data = yaml.safe_load(profiles_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{profiles_path}: expected a YAML mapping")

    raw_profiles = data.get("profiles", data)
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{profiles_path}: profiles must be a YAML mapping")

    profiles: dict[str, RecordingProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{profiles_path}: profile names must be non-empty strings")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"{profiles_path}: profile {name!r} must be a mapping")

        raw_args = raw_profile.get("rpicam_vid_args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, (str, int, float)) for item in raw_args):
            raise ValueError(f"{profiles_path}: profile {name!r} rpicam_vid_args must be a list of scalar values")

        policy, unsupported_policy, warnings = _parse_camera_control_policy(
            raw_profile.get("camera_control_policy", {}),
            profiles_path,
            name,
        )
        output_extension = str(raw_profile.get("output_extension", "h264")).lstrip(".")
        profiles[name] = RecordingProfile(
            name=name,
            description=str(raw_profile.get("description", "")),
            output_extension=output_extension or "h264",
            rpicam_vid_args=[str(item) for item in raw_args],
            camera_control_policy=policy,
            unsupported_camera_control_policy=unsupported_policy,
            warnings=warnings,
        )

    if not profiles:
        raise ValueError(f"{profiles_path}: at least one recording profile is required")

    return RecordingProfiles(profiles)


def _parse_camera_control_policy(
    raw_policy: Any,
    path: Path,
    profile_name: str,
) -> tuple[CameraControlPolicy, dict[str, Any], list[str]]:
    if raw_policy is None:
        raw_policy = {}
    if not isinstance(raw_policy, dict):
        raise ValueError(f"{path}: profile {profile_name!r} camera_control_policy must be a mapping")

    unsupported = {str(key): value for key, value in raw_policy.items() if key not in CAMERA_CONTROL_POLICY_FIELDS}
    warnings = [
        f"unsupported camera_control_policy field {key!r} is recorded but not applied"
        for key in sorted(unsupported)
    ]

    return (
        CameraControlPolicy(
            pre_roll_seconds=_read_seconds(raw_policy, "pre_roll_seconds", 5.0, path, profile_name),
            exposure_mode=_read_mode(raw_policy, "exposure_mode", "auto", EXPOSURE_MODES, path, profile_name),
            awb_mode=_read_mode(raw_policy, "awb_mode", "auto_then_lock", AWB_MODES, path, profile_name),
            focus_mode=_read_mode(raw_policy, "focus_mode", "auto", FOCUS_MODES, path, profile_name),
            reuse_prepared_controls=_read_bool(
                raw_policy,
                "reuse_prepared_controls",
                True,
                path,
                profile_name,
            ),
            refocus_on_each_take=_read_bool(raw_policy, "refocus_on_each_take", False, path, profile_name),
            prepare_warmup_seconds=_read_seconds(raw_policy, "prepare_warmup_seconds", 0.0, path, profile_name),
        ),
        unsupported,
        warnings,
    )


def _read_mode(
    raw_policy: dict[str, Any],
    key: str,
    default: str,
    allowed: set[str],
    path: Path,
    profile_name: str,
) -> str:
    value = str(raw_policy.get(key, default)).strip()
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(
            f"{path}: profile {profile_name!r} camera_control_policy.{key} must be one of: {allowed_values}"
        )
    return value


def _read_seconds(raw_policy: dict[str, Any], key: str, default: float, path: Path, profile_name: str) -> float:
    value = raw_policy.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{path}: profile {profile_name!r} camera_control_policy.{key} must be a number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: profile {profile_name!r} camera_control_policy.{key} must be a number") from exc
    if seconds < 0:
        raise ValueError(f"{path}: profile {profile_name!r} camera_control_policy.{key} must be >= 0")
    return seconds


def _read_bool(raw_policy: dict[str, Any], key: str, default: bool, path: Path, profile_name: str) -> bool:
    value = raw_policy.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"{path}: profile {profile_name!r} camera_control_policy.{key} must be true or false")


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
