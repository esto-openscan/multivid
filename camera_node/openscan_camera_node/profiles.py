from __future__ import annotations

from copy import deepcopy
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
PROFILE_FIELDS = {
    "description",
    "output_extension",
    "recording",
    "camera_controls",
    "camera_control_policy",
    "rpicam_vid_args",
    "rpicam_vid_extra_args",
}
RECORDING_FIELDS = {
    "width",
    "height",
    "framerate",
    "bitrate",
    "codec",
    "container",
    "duration",
    "nopreview",
    "level",
    "save_pts",
}
CAMERA_CONTROL_FIELDS = {
    "shutter_us",
    "gain",
    "awbgains",
    "autofocus_mode",
    "lens_position",
    "denoise",
    "ev",
    "metering",
    "awb",
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
    recording: dict[str, Any]
    camera_controls: dict[str, Any]
    rpicam_vid_args: list[str]
    rpicam_vid_extra_args: list[str]
    planned_applied_controls: dict[str, Any]
    camera_control_policy: CameraControlPolicy
    unsupported_camera_control_policy: dict[str, Any]
    unsupported_controls: dict[str, Any]
    profile_overrides: dict[str, Any]
    warnings: list[str]

    def requested_controls(self) -> dict[str, Any]:
        return {
            "camera_control_policy": self.camera_control_policy.as_dict(),
            "recording": self.recording,
            "camera_controls": self.camera_controls,
        }

    def resolved_controls(self) -> dict[str, Any]:
        return {
            "recording": self.recording,
            "camera_controls": self.camera_controls,
        }

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description,
            "output_extension": self.output_extension,
            "recording": self.recording,
            "camera_controls": self.camera_controls,
            "rpicam_vid_args": self.rpicam_vid_args,
            "rpicam_vid_extra_args": self.rpicam_vid_extra_args,
            "planned_applied_controls": self.planned_applied_controls,
            "camera_control_policy": self.camera_control_policy.as_dict(),
        }
        if self.unsupported_camera_control_policy:
            data["unsupported_camera_control_policy"] = self.unsupported_camera_control_policy
        if self.unsupported_controls:
            data["unsupported_controls"] = self.unsupported_controls
        if self.profile_overrides:
            data["profile_overrides"] = self.profile_overrides
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


def load_recording_profiles(
    path: str | Path,
    profile_overrides: dict[str, Any] | None = None,
) -> RecordingProfiles:
    profiles_path = Path(path)
    with profiles_path.open("r", encoding="utf-8") as profiles_file:
        data = yaml.safe_load(profiles_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{profiles_path}: expected a YAML mapping")

    raw_profiles = data.get("profiles", data)
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{profiles_path}: profiles must be a YAML mapping")

    overrides = _validate_profile_overrides(profile_overrides or {}, profiles_path)
    unknown_override_names = sorted(name for name in overrides if name not in raw_profiles)
    if unknown_override_names:
        names = ", ".join(unknown_override_names)
        raise ValueError(f"{profiles_path}: profile_overrides references unknown profile(s): {names}")

    profiles: dict[str, RecordingProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{profiles_path}: profile names must be non-empty strings")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"{profiles_path}: profile {name!r} must be a mapping")

        profile_override = overrides.get(name, {})
        merged_profile = _deep_merge(raw_profile, profile_override)
        profiles[name] = _parse_recording_profile(
            profiles_path,
            name,
            merged_profile,
            profile_override,
        )

    if not profiles:
        raise ValueError(f"{profiles_path}: at least one recording profile is required")

    return RecordingProfiles(profiles)


def _parse_recording_profile(
    profiles_path: Path,
    name: str,
    raw_profile: dict[str, Any],
    profile_override: dict[str, Any],
) -> RecordingProfile:
    unsupported_profile_fields = {
        str(key): value
        for key, value in raw_profile.items()
        if key not in PROFILE_FIELDS
    }
    warnings = [
        f"unsupported profile field {key!r} is recorded but not applied"
        for key in sorted(unsupported_profile_fields)
    ]

    legacy_args = _read_args(raw_profile.get("rpicam_vid_args", []), "rpicam_vid_args", profiles_path, name)
    extra_args = _read_args(
        raw_profile.get("rpicam_vid_extra_args", []),
        "rpicam_vid_extra_args",
        profiles_path,
        name,
    )
    recording, unsupported_recording = _parse_recording(raw_profile.get("recording", {}), profiles_path, name)
    camera_controls, unsupported_camera_controls = _parse_camera_controls(
        raw_profile.get("camera_controls", {}),
        profiles_path,
        name,
    )
    generated_args, planned_applied_controls = _build_rpicam_args(recording, camera_controls)

    if legacy_args and (generated_args or extra_args):
        warnings.append(
            "rpicam_vid_args is passed through after structured profile args; prefer rpicam_vid_extra_args for additional flags"
        )

    policy, unsupported_policy, policy_warnings = _parse_camera_control_policy(
        raw_profile.get("camera_control_policy", {}),
        profiles_path,
        name,
    )
    warnings.extend(policy_warnings)
    warnings.extend(_manual_lock_warnings(policy, camera_controls))

    unsupported_controls: dict[str, Any] = {}
    if unsupported_profile_fields:
        unsupported_controls["profile_fields"] = unsupported_profile_fields
    if unsupported_recording:
        unsupported_controls["recording"] = unsupported_recording
    if unsupported_camera_controls:
        unsupported_controls["camera_controls"] = unsupported_camera_controls

    output_extension = _read_output_extension(raw_profile, recording)
    return RecordingProfile(
        name=name,
        description=str(raw_profile.get("description", "")),
        output_extension=output_extension,
        recording=recording,
        camera_controls=camera_controls,
        rpicam_vid_args=[*generated_args, *legacy_args, *extra_args],
        rpicam_vid_extra_args=extra_args,
        planned_applied_controls=planned_applied_controls,
        camera_control_policy=policy,
        unsupported_camera_control_policy=unsupported_policy,
        unsupported_controls=unsupported_controls,
        profile_overrides=profile_override,
        warnings=_dedupe(warnings),
    )


def _parse_recording(raw_recording: Any, path: Path, profile_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw_recording is None:
        raw_recording = {}
    if not isinstance(raw_recording, dict):
        raise ValueError(f"{path}: profile {profile_name!r} recording must be a mapping")

    recording: dict[str, Any] = {}
    unsupported = {str(key): value for key, value in raw_recording.items() if key not in RECORDING_FIELDS}

    if "width" in raw_recording:
        recording["width"] = _read_optional_int(raw_recording, "width", 1, path, profile_name, "recording")
    if "height" in raw_recording:
        recording["height"] = _read_optional_int(raw_recording, "height", 1, path, profile_name, "recording")
    if "framerate" in raw_recording:
        recording["framerate"] = _read_optional_number(
            raw_recording,
            "framerate",
            0,
            path,
            profile_name,
            "recording",
            minimum_is_exclusive=True,
        )
    if "bitrate" in raw_recording:
        recording["bitrate"] = _read_optional_int(raw_recording, "bitrate", 1, path, profile_name, "recording")
    if "codec" in raw_recording:
        recording["codec"] = _read_optional_string(raw_recording, "codec", path, profile_name, "recording")
    if "container" in raw_recording:
        recording["container"] = _read_optional_string(raw_recording, "container", path, profile_name, "recording")
    if "duration" in raw_recording:
        recording["duration"] = _read_optional_number(raw_recording, "duration", 0, path, profile_name, "recording")
    if "nopreview" in raw_recording:
        recording["nopreview"] = _read_optional_bool(raw_recording, "nopreview", path, profile_name, "recording")
    if "level" in raw_recording:
        recording["level"] = _read_optional_string(raw_recording, "level", path, profile_name, "recording")
    if "save_pts" in raw_recording:
        recording["save_pts"] = _read_optional_bool_or_string(
            raw_recording,
            "save_pts",
            path,
            profile_name,
            "recording",
        )

    return recording, unsupported


def _parse_camera_controls(raw_controls: Any, path: Path, profile_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw_controls is None:
        raw_controls = {}
    if not isinstance(raw_controls, dict):
        raise ValueError(f"{path}: profile {profile_name!r} camera_controls must be a mapping")

    controls: dict[str, Any] = {}
    unsupported = {str(key): value for key, value in raw_controls.items() if key not in CAMERA_CONTROL_FIELDS}

    if "shutter_us" in raw_controls:
        controls["shutter_us"] = _read_optional_int(raw_controls, "shutter_us", 1, path, profile_name, "camera_controls")
    if "gain" in raw_controls:
        controls["gain"] = _read_optional_number(
            raw_controls,
            "gain",
            0,
            path,
            profile_name,
            "camera_controls",
            minimum_is_exclusive=True,
        )
    if "awbgains" in raw_controls:
        controls["awbgains"] = _read_optional_awbgains(raw_controls, "awbgains", path, profile_name)
    if "autofocus_mode" in raw_controls:
        controls["autofocus_mode"] = _read_optional_string(
            raw_controls,
            "autofocus_mode",
            path,
            profile_name,
            "camera_controls",
        )
    if "lens_position" in raw_controls:
        controls["lens_position"] = _read_optional_number(
            raw_controls,
            "lens_position",
            0,
            path,
            profile_name,
            "camera_controls",
        )
    if "denoise" in raw_controls:
        controls["denoise"] = _read_optional_string(raw_controls, "denoise", path, profile_name, "camera_controls")
    if "ev" in raw_controls:
        controls["ev"] = _read_optional_number(raw_controls, "ev", None, path, profile_name, "camera_controls")
    if "metering" in raw_controls:
        controls["metering"] = _read_optional_string(raw_controls, "metering", path, profile_name, "camera_controls")
    if "awb" in raw_controls:
        controls["awb"] = _read_optional_string(raw_controls, "awb", path, profile_name, "camera_controls")

    return controls, unsupported


def _build_rpicam_args(recording: dict[str, Any], camera_controls: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    args: list[str] = []
    applied: dict[str, Any] = {}

    _add_value_arg(args, applied, "width", "--width", recording.get("width"))
    _add_value_arg(args, applied, "height", "--height", recording.get("height"))
    _add_value_arg(args, applied, "framerate", "--framerate", recording.get("framerate"))
    _add_value_arg(args, applied, "bitrate", "--bitrate", recording.get("bitrate"))
    _add_value_arg(args, applied, "codec", "--codec", recording.get("codec"))

    container = recording.get("container")
    if _has_value(container) and container != "h264":
        args.extend(["--libav-format", str(container)])
        applied["container"] = container

    duration = recording.get("duration")
    if _has_value(duration):
        timeout_ms = int(float(duration) * 1000)
        args.extend(["--timeout", str(timeout_ms)])
        applied["duration"] = duration
        applied["timeout_ms"] = timeout_ms

    nopreview = recording.get("nopreview")
    if nopreview is True:
        args.append("--nopreview")
        applied["nopreview"] = True

    _add_value_arg(args, applied, "level", "--level", recording.get("level"))

    _add_value_arg(args, applied, "shutter_us", "--shutter", camera_controls.get("shutter_us"))
    _add_value_arg(args, applied, "gain", "--gain", camera_controls.get("gain"))
    awbgains = camera_controls.get("awbgains")
    if _has_value(awbgains):
        args.extend(["--awbgains", _format_awbgains(awbgains)])
        applied["awbgains"] = awbgains
    _add_value_arg(args, applied, "autofocus_mode", "--autofocus-mode", camera_controls.get("autofocus_mode"))
    _add_value_arg(args, applied, "lens_position", "--lens-position", camera_controls.get("lens_position"))
    _add_value_arg(args, applied, "denoise", "--denoise", camera_controls.get("denoise"))
    _add_value_arg(args, applied, "ev", "--ev", camera_controls.get("ev"))
    _add_value_arg(args, applied, "metering", "--metering", camera_controls.get("metering"))
    _add_value_arg(args, applied, "awb", "--awb", camera_controls.get("awb"))

    return args, applied


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


def _manual_lock_warnings(policy: CameraControlPolicy, camera_controls: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if policy.exposure_mode == "manual":
        missing = [key for key in ("shutter_us", "gain") if not _has_value(camera_controls.get(key))]
        if missing:
            missing_fields = ", ".join(f"camera_controls.{key}" for key in missing)
            warnings.append(f"manual exposure_mode requested but {missing_fields} is missing")
    if policy.awb_mode == "manual" and not _has_value(camera_controls.get("awbgains")):
        warnings.append("manual awb_mode requested but camera_controls.awbgains is missing")
    if policy.focus_mode == "manual":
        autofocus_mode = camera_controls.get("autofocus_mode")
        if autofocus_mode != "manual":
            warnings.append("manual focus_mode requested but camera_controls.autofocus_mode is not manual")
        if not _has_value(camera_controls.get("lens_position")):
            warnings.append("manual focus_mode requested but camera_controls.lens_position is missing")
    return warnings


def _read_args(raw_args: Any, field_name: str, path: Path, profile_name: str) -> list[str]:
    if raw_args is None:
        return []
    if not isinstance(raw_args, list) or not all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in raw_args
    ):
        raise ValueError(f"{path}: profile {profile_name!r} {field_name} must be a list of scalar values")
    return [str(item) for item in raw_args]


def _read_output_extension(raw_profile: dict[str, Any], recording: dict[str, Any]) -> str:
    raw_output_extension = raw_profile.get("output_extension")
    if raw_output_extension is None:
        container = recording.get("container")
        return str(container).lstrip(".") if _has_value(container) and container != "h264" else "h264"
    output_extension = str(raw_output_extension).lstrip(".")
    return output_extension or "h264"


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


def _read_optional_int(
    raw: dict[str, Any],
    key: str,
    minimum: int,
    path: Path,
    profile_name: str,
    section: str,
) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be an integer") from exc
    if parsed != value and isinstance(value, float):
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be an integer")
    if parsed < minimum:
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be >= {minimum}")
    return parsed


def _read_optional_number(
    raw: dict[str, Any],
    key: str,
    minimum: float | None,
    path: Path,
    profile_name: str,
    section: str,
    minimum_is_exclusive: bool = False,
) -> int | float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be a number") from exc
    if minimum is not None:
        invalid = parsed <= minimum if minimum_is_exclusive else parsed < minimum
        if invalid:
            comparator = ">" if minimum_is_exclusive else ">="
            raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be {comparator} {minimum:g}")
    return _clean_number(parsed)


def _read_optional_string(
    raw: dict[str, Any],
    key: str,
    path: Path,
    profile_name: str,
    section: str,
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be a non-empty string")
    return value.strip()


def _read_optional_bool(
    raw: dict[str, Any],
    key: str,
    path: Path,
    profile_name: str,
    section: str,
) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be true or false")


def _read_optional_bool_or_string(
    raw: dict[str, Any],
    key: str,
    path: Path,
    profile_name: str,
    section: str,
) -> bool | str | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{path}: profile {profile_name!r} {section}.{key} must be true, false, or a non-empty string")


def _read_optional_awbgains(
    raw: dict[str, Any],
    key: str,
    path: Path,
    profile_name: str,
) -> list[int | float] | None:
    value = raw.get(key)
    if value is None:
        return None
    values: list[Any]
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError(f"{path}: profile {profile_name!r} camera_controls.{key} must be two numeric values")
    if len(values) != 2:
        raise ValueError(f"{path}: profile {profile_name!r} camera_controls.{key} must contain exactly two values")
    parsed = []
    for item in values:
        if isinstance(item, bool):
            raise ValueError(f"{path}: profile {profile_name!r} camera_controls.{key} values must be numbers")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: profile {profile_name!r} camera_controls.{key} values must be numbers") from exc
        if number <= 0:
            raise ValueError(f"{path}: profile {profile_name!r} camera_controls.{key} values must be > 0")
        parsed.append(_clean_number(number))
    return parsed


def _validate_profile_overrides(raw_overrides: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_overrides, dict):
        raise ValueError(f"{path}: profile_overrides must be a mapping")
    overrides: dict[str, dict[str, Any]] = {}
    for name, override in raw_overrides.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}: profile_overrides keys must be non-empty profile names")
        if override is None:
            continue
        if not isinstance(override, dict):
            raise ValueError(f"{path}: profile_overrides.{name} must be a mapping")
        overrides[name] = override
    return overrides


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _add_value_arg(
    args: list[str],
    applied: dict[str, Any],
    applied_key: str,
    option: str,
    value: Any,
) -> None:
    if not _has_value(value):
        return
    args.extend([option, _format_arg_value(value)])
    applied[applied_key] = value


def _format_awbgains(value: list[int | float]) -> str:
    return ",".join(_format_arg_value(item) for item in value)


def _format_arg_value(value: Any) -> str:
    if isinstance(value, float):
        return str(_clean_number(value))
    return str(value)


def _has_value(value: Any) -> bool:
    return value is not None and value is not False


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped
