from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = "/etc/openscan-camera-node/config.yaml"
DEFAULT_PROFILES_PATH = "/etc/openscan-camera-node/profiles.yaml"


@dataclass(frozen=True)
class CameraNodeConfig:
    camera_id: str
    listen_host: str
    listen_port: int
    output_root: Path


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

    if listen_port < 1 or listen_port > 65535:
        raise ValueError(f"{config_path}: listen_port must be between 1 and 65535")

    return CameraNodeConfig(
        camera_id=camera_id,
        listen_host=listen_host,
        listen_port=listen_port,
        output_root=output_root,
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
