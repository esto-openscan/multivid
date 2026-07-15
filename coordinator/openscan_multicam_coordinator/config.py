from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


DEFAULT_CONFIG_PATH = os.environ.get("OPENSCAN_MULTICAM_CONFIG", "multivid.yml")
DEFAULT_HTTP_PORT = 8080
DEFAULT_HARVEST_USER = "openscan"
DEFAULT_REMOTE_OUTPUT_ROOT = "/srv/openscan-camera/sessions"


@dataclass(frozen=True)
class NodeConfig:
    name: str
    camera_id: str
    base_url: str
    host: str = ""
    ssh_host: str = ""
    ssh_user: str = DEFAULT_HARVEST_USER
    ssh_identity_file: Path | None = None
    remote_output_root: str = DEFAULT_REMOTE_OUTPUT_ROOT
    local_alias: str | None = None
    camera_transform: dict[str, bool] = field(default_factory=dict)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        host = self.host or (urlparse(self.base_url).hostname or "")
        if not host:
            raise ValueError("NodeConfig requires a host or a URL with a hostname")
        object.__setattr__(self, "host", host)
        if not self.ssh_host:
            object.__setattr__(self, "ssh_host", host)

    @property
    def harvest_folder(self) -> str:
        return self.local_alias or self.camera_id


@dataclass(frozen=True)
class DashboardPositioningConfig:
    width: int = 640
    height: int = 360
    fps: int = 5
    jpeg_quality: int = 75
    overlays: tuple[str, ...] = ("camera_label", "crosshair", "shorts_safe_area")


@dataclass(frozen=True)
class DashboardConfig:
    positioning: DashboardPositioningConfig = field(default_factory=DashboardPositioningConfig)
    status_refresh_seconds: int = 3


@dataclass(frozen=True)
class FleetConfig:
    bootstrap_user: str
    identity_file: Path
    nodes: tuple[NodeConfig, ...]

    @property
    def public_key_file(self) -> Path:
        return Path(f"{self.identity_file}.pub")


def load_fleet_config(path: str | Path) -> FleetConfig:
    config_path = Path(path)
    data = _load_yaml_mapping(config_path)
    _reject_unknown_keys(data, {"version", "connection", "nodes"}, config_path, "top level")
    if data.get("version") != 1:
        raise ValueError(f"{config_path}: version must be 1")

    connection = _required_mapping(data, "connection", config_path, "top level")
    _reject_unknown_keys(connection, {"bootstrap_user", "identity_file"}, config_path, "connection")
    bootstrap_user = _required_string(connection, "bootstrap_user", config_path, "connection")
    identity_file = Path(os.path.expanduser(_required_string(connection, "identity_file", config_path, "connection")))

    raw_nodes = _required_mapping(data, "nodes", config_path, "top level")
    if not raw_nodes:
        raise ValueError(f"{config_path}: nodes must contain at least one node")
    nodes: list[NodeConfig] = []
    for camera_id, raw_node in raw_nodes.items():
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError(f"{config_path}: node ids must be non-empty strings")
        node_id = camera_id.strip()
        if not isinstance(raw_node, dict):
            raise ValueError(f"{config_path}: nodes.{node_id} must be a mapping")
        _reject_unknown_keys(raw_node, {"host", "enabled", "camera_transform", "profile_overrides"}, config_path, f"nodes.{node_id}")
        host = _required_string(raw_node, "host", config_path, f"nodes.{node_id}")
        enabled = _optional_bool(raw_node, "enabled", True, config_path, f"nodes.{node_id}")
        transform = _camera_transform(raw_node.get("camera_transform"), config_path, node_id)
        overrides = _mapping_or_empty(raw_node.get("profile_overrides"), config_path, f"nodes.{node_id}.profile_overrides")
        nodes.append(
            NodeConfig(
                name=f"cam-{node_id}",
                camera_id=node_id,
                host=host,
                base_url=f"http://{host}:{DEFAULT_HTTP_PORT}",
                ssh_host=host,
                ssh_identity_file=identity_file,
                camera_transform=transform,
                profile_overrides=overrides,
                enabled=enabled,
            )
        )
    return FleetConfig(bootstrap_user=bootstrap_user, identity_file=identity_file, nodes=tuple(nodes))


def load_nodes_config(path: str | Path) -> list[NodeConfig]:
    """Load nodes from the current fleet schema; no legacy config format is supported."""
    return list(load_fleet_config(path).nodes)


def load_dashboard_config(path: str | Path | None = None) -> DashboardConfig:
    """Dashboard settings are application defaults, not fleet configuration."""
    return DashboardConfig()


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def _required_mapping(raw: dict[str, Any], key: str, path: Path, section: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {section}.{key} must be a mapping")
    return value


def _mapping_or_empty(value: Any, path: Path, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {label} must be a mapping")
    return value


def _required_string(raw: dict[str, Any], key: str, path: Path, section: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {section}.{key} must be a non-empty string")
    return value.strip()


def _optional_bool(raw: dict[str, Any], key: str, default: bool, path: Path, section: str) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{path}: {section}.{key} must be true or false")


def _camera_transform(value: Any, path: Path, node_id: str) -> dict[str, bool]:
    raw = _mapping_or_empty(value, path, f"nodes.{node_id}.camera_transform")
    _reject_unknown_keys(raw, {"hflip", "vflip"}, path, f"nodes.{node_id}.camera_transform")
    result: dict[str, bool] = {}
    for key in ("hflip", "vflip"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{path}: nodes.{node_id}.camera_transform.{key} must be true or false")
            result[key] = raw[key]
    return result


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], path: Path, section: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"{path}: unsupported keys in {section}: {', '.join(unknown)}")
