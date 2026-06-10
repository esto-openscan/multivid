from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = "/etc/openscan-camera-node/config.yaml"
DEFAULT_PROFILES_PATH = "/etc/openscan-camera-node/profiles.yaml"
POSITIONING_OVERLAYS = {"camera_label", "crosshair", "grid", "shorts_safe_area"}
POSITIONING_OVERLAY_ALIASES = {
    "camera-label": "camera_label",
    "camera_label": "camera_label",
    "label": "camera_label",
    "crosshair": "crosshair",
    "grid": "grid",
    "shorts-safe-area": "shorts_safe_area",
    "shorts_safe_area": "shorts_safe_area",
    "shorts": "shorts_safe_area",
}


@dataclass(frozen=True)
class NodeCameraControlPolicy:
    use_calibration_suggestions: bool
    apply_suggestions_to_recording: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "use_calibration_suggestions": self.use_calibration_suggestions,
            "apply_suggestions_to_recording": self.apply_suggestions_to_recording,
        }


@dataclass(frozen=True)
class CameraTransform:
    hflip: bool = False
    vflip: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "hflip": self.hflip,
            "vflip": self.vflip,
        }


@dataclass(frozen=True)
class PositioningConfig:
    width: int
    height: int
    fps: int
    jpeg_quality: int
    overlays: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "jpeg_quality": self.jpeg_quality,
            "overlays": list(self.overlays),
        }


@dataclass(frozen=True)
class ReferenceStillsConfig:
    quality: int
    width: int | None
    height: int | None
    use_recording_profile_controls: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "width": self.width,
            "height": self.height,
            "use_recording_profile_controls": self.use_recording_profile_controls,
        }


@dataclass(frozen=True)
class CameraNodeConfig:
    camera_id: str
    listen_host: str
    listen_port: int
    output_root: Path
    profile_overrides: dict[str, Any]
    camera_control_policy: NodeCameraControlPolicy
    camera_transform: CameraTransform = field(default_factory=CameraTransform)
    positioning: PositioningConfig = field(
        default_factory=lambda: PositioningConfig(
            width=640,
            height=360,
            fps=5,
            jpeg_quality=75,
            overlays=["camera_label", "crosshair", "shorts_safe_area"],
        )
    )
    reference_stills: ReferenceStillsConfig = field(
        default_factory=lambda: ReferenceStillsConfig(
            quality=95,
            width=None,
            height=None,
            use_recording_profile_controls=True,
        )
    )


def default_config_path() -> Path:
    return Path(os.environ.get("OPENSCAN_CAMERA_NODE_CONFIG", DEFAULT_CONFIG_PATH))


def default_profiles_path() -> Path:
    return Path(os.environ.get("OPENSCAN_CAMERA_NODE_PROFILES", DEFAULT_PROFILES_PATH))


def load_camera_node_config(path: str | Path | None = None) -> CameraNodeConfig:
    config_path = Path(path) if path is not None else default_config_path()
    raw_config = _load_yaml_mapping(config_path)

    camera_id = _required_string(raw_config, "camera_id", config_path)
    listen_host = str(raw_config.get("listen_host", "0.0.0.0"))
    listen_port = int(raw_config.get("listen_port", 8080))
    output_root = Path(str(raw_config.get("output_root", "/srv/openscan-camera/sessions")))
    profile_overrides = _profile_overrides(raw_config.get("profile_overrides", {}), config_path)
    camera_control_policy = _camera_control_policy(raw_config.get("camera_control_policy", {}), config_path)
    camera_transform = _camera_transform(raw_config.get("camera_transform", {}), config_path)
    positioning = _positioning_config(raw_config.get("positioning", {}), config_path)
    reference_stills = _reference_stills_config(raw_config.get("reference_stills", {}), config_path)

    if listen_port < 1 or listen_port > 65535:
        raise ValueError(f"{config_path}: listen_port must be between 1 and 65535")

    return CameraNodeConfig(
        camera_id=camera_id,
        listen_host=listen_host,
        listen_port=listen_port,
        output_root=output_root,
        profile_overrides=profile_overrides,
        camera_control_policy=camera_control_policy,
        camera_transform=camera_transform,
        positioning=positioning,
        reference_stills=reference_stills,
    )


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def _required_string(config: dict[str, Any], key: str, path: Path) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {key} must be a non-empty string")
    return value.strip()


def _profile_overrides(raw_value: Any, path: Path) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{path}: profile_overrides must be a mapping")
    overrides: dict[str, Any] = {}
    for profile_name, override in raw_value.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValueError(f"{path}: profile_overrides keys must be non-empty strings")
        if override is None:
            continue
        if not isinstance(override, dict):
            raise ValueError(f"{path}: profile_overrides.{profile_name} must be a mapping")
        overrides[profile_name] = override
    return overrides


def _camera_control_policy(raw_value: Any, path: Path) -> NodeCameraControlPolicy:
    if raw_value is None:
        raw_value = {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{path}: camera_control_policy must be a mapping")
    return NodeCameraControlPolicy(
        use_calibration_suggestions=_read_bool(
            raw_value,
            "use_calibration_suggestions",
            False,
            path,
            "camera_control_policy",
        ),
        apply_suggestions_to_recording=_read_bool(
            raw_value,
            "apply_suggestions_to_recording",
            False,
            path,
            "camera_control_policy",
        ),
    )


def _camera_transform(raw_value: Any, path: Path) -> CameraTransform:
    if raw_value is None:
        raw_value = {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{path}: camera_transform must be a mapping")
    return CameraTransform(
        hflip=_read_bool(raw_value, "hflip", False, path, "camera_transform"),
        vflip=_read_bool(raw_value, "vflip", False, path, "camera_transform"),
    )


def _positioning_config(raw_value: Any, path: Path) -> PositioningConfig:
    if raw_value is None:
        raw_value = {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{path}: positioning must be a mapping")
    return PositioningConfig(
        width=_read_int(raw_value, "width", 640, 1, path, "positioning"),
        height=_read_int(raw_value, "height", 360, 1, path, "positioning"),
        fps=_read_int(raw_value, "fps", 5, 1, path, "positioning"),
        jpeg_quality=_read_int(raw_value, "jpeg_quality", 75, 1, path, "positioning", maximum=100),
        overlays=_read_overlay_list(
            raw_value,
            "overlays",
            ["camera_label", "crosshair", "shorts_safe_area"],
            path,
            "positioning",
        ),
    )


def _reference_stills_config(raw_value: Any, path: Path) -> ReferenceStillsConfig:
    if raw_value is None:
        raw_value = {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{path}: reference_stills must be a mapping")
    return ReferenceStillsConfig(
        quality=_read_int(raw_value, "quality", 95, 1, path, "reference_stills", maximum=100),
        width=_read_optional_int(raw_value, "width", 1, path, "reference_stills"),
        height=_read_optional_int(raw_value, "height", 1, path, "reference_stills"),
        use_recording_profile_controls=_read_bool(
            raw_value,
            "use_recording_profile_controls",
            True,
            path,
            "reference_stills",
        ),
    )


def _read_bool(raw: dict[str, Any], key: str, default: bool, path: Path, section: str) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"{path}: {section}.{key} must be true or false")


def _read_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    path: Path,
    section: str,
    maximum: int | None = None,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{path}: {section}.{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {section}.{key} must be an integer") from exc
    if isinstance(value, float) and parsed != value:
        raise ValueError(f"{path}: {section}.{key} must be an integer")
    if parsed < minimum:
        raise ValueError(f"{path}: {section}.{key} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{path}: {section}.{key} must be <= {maximum}")
    return parsed


def _read_optional_int(raw: dict[str, Any], key: str, minimum: int, path: Path, section: str) -> int | None:
    if key not in raw or raw.get(key) is None:
        return None
    return _read_int(raw, key, 0, minimum, path, section)


def _read_overlay_list(
    raw: dict[str, Any],
    key: str,
    default: list[str],
    path: Path,
    section: str,
) -> list[str]:
    value = raw.get(key, default)
    return normalize_positioning_overlays(value, path=path, section=section, key=key)


def normalize_positioning_overlays(value: Any, path: Path | None = None, section: str = "positioning", key: str = "overlays") -> list[str]:
    if value is None:
        return []
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, list):
        location = f"{path}: " if path is not None else ""
        raise ValueError(f"{location}{section}.{key} must be a list of overlay names")

    overlays: list[str] = []
    for item in raw_items:
        if not isinstance(item, str) or not item.strip():
            location = f"{path}: " if path is not None else ""
            raise ValueError(f"{location}{section}.{key} entries must be non-empty strings")
        normalized = POSITIONING_OVERLAY_ALIASES.get(item.strip().lower().replace("_", "-"))
        if normalized is None:
            allowed = ", ".join(sorted(POSITIONING_OVERLAYS))
            location = f"{path}: " if path is not None else ""
            raise ValueError(f"{location}{section}.{key} contains unsupported overlay {item!r}; allowed: {allowed}")
        if normalized not in overlays:
            overlays.append(normalized)
    return overlays
