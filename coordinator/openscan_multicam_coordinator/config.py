from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


DEFAULT_NODES_PATH = os.environ.get("OPENSCAN_MULTICAM_NODES", "examples/nodes.yml")


@dataclass(frozen=True)
class NodeConfig:
    name: str
    camera_id: str
    base_url: str
    ssh_host: str | None = None
    ssh_user: str | None = None
    remote_output_root: str = "/srv/openscan-camera/sessions"
    local_alias: str | None = None
    enabled: bool = True

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


def load_nodes_config(path: str | Path) -> list[NodeConfig]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as nodes_file:
        data = yaml.safe_load(nodes_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{config_path}: expected a YAML mapping")

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError(f"{config_path}: nodes must be a list")

    nodes: list[NodeConfig] = []
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            raise ValueError(f"{config_path}: nodes[{index}] must be a mapping")
        name = _required_string(raw_node, "name", config_path, index)
        camera_id = _required_string(raw_node, "camera_id", config_path, index)
        base_url = _required_string(raw_node, "base_url", config_path, index).rstrip("/")
        ssh_host = _optional_string(raw_node, "ssh_host", config_path, index) or _host_from_base_url(base_url)
        ssh_user = _optional_string(raw_node, "ssh_user", config_path, index)
        remote_output_root = _optional_string(raw_node, "remote_output_root", config_path, index)
        local_alias = _optional_string(raw_node, "local_alias", config_path, index)
        enabled = _optional_bool(raw_node, "enabled", True, config_path, index)
        nodes.append(
            NodeConfig(
                name=name,
                camera_id=camera_id,
                base_url=base_url,
                ssh_host=ssh_host,
                ssh_user=ssh_user,
                remote_output_root=remote_output_root or "/srv/openscan-camera/sessions",
                local_alias=local_alias,
                enabled=enabled,
            )
        )

    if not nodes:
        raise ValueError(f"{config_path}: at least one node is required")

    return nodes


def load_dashboard_config(path: str | Path) -> DashboardConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as nodes_file:
        data = yaml.safe_load(nodes_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{config_path}: expected a YAML mapping")

    raw_dashboard = data.get("dashboard") or {}
    if not isinstance(raw_dashboard, dict):
        raise ValueError(f"{config_path}: dashboard must be a mapping when set")

    raw_positioning = raw_dashboard.get("positioning") or {}
    if not isinstance(raw_positioning, dict):
        raise ValueError(f"{config_path}: dashboard.positioning must be a mapping when set")

    defaults = DashboardPositioningConfig()
    positioning = DashboardPositioningConfig(
        width=_dashboard_positive_int(raw_positioning.get("width"), defaults.width, config_path, "dashboard.positioning.width"),
        height=_dashboard_positive_int(raw_positioning.get("height"), defaults.height, config_path, "dashboard.positioning.height"),
        fps=_dashboard_positive_int(raw_positioning.get("fps"), defaults.fps, config_path, "dashboard.positioning.fps"),
        jpeg_quality=_dashboard_int_range(
            raw_positioning.get("jpeg_quality"),
            defaults.jpeg_quality,
            1,
            100,
            config_path,
            "dashboard.positioning.jpeg_quality",
        ),
        overlays=tuple(
            _dashboard_string_list(
                raw_positioning.get("overlays"),
                list(defaults.overlays),
                config_path,
                "dashboard.positioning.overlays",
            )
        ),
    )
    return DashboardConfig(
        positioning=positioning,
        status_refresh_seconds=_dashboard_positive_int(
            raw_dashboard.get("status_refresh_seconds"),
            3,
            config_path,
            "dashboard.status_refresh_seconds",
        ),
    )


def _required_string(raw_node: dict[str, Any], key: str, path: Path, index: int) -> str:
    value = raw_node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: nodes[{index}].{key} must be a non-empty string")
    return value.strip()


def _optional_string(raw_node: dict[str, Any], key: str, path: Path, index: int) -> str | None:
    value = raw_node.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: nodes[{index}].{key} must be a non-empty string when set")
    return value.strip()


def _optional_bool(raw_node: dict[str, Any], key: str, default: bool, path: Path, index: int) -> bool:
    value = raw_node.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"{path}: nodes[{index}].{key} must be true or false")


def _host_from_base_url(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    return parsed.hostname


def _dashboard_positive_int(value: Any, default: int, path: Path, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{path}: {label} must be a positive integer")
    return value


def _dashboard_int_range(value: Any, default: int, minimum: int, maximum: int, path: Path, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{path}: {label} must be an integer between {minimum} and {maximum}")
    return value


def _dashboard_string_list(value: Any, default: list[str], path: Path, label: str) -> list[str]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise ValueError(f"{path}: {label} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path}: {label}[{index}] must be a non-empty string")
        stripped = item.strip()
        if stripped not in items:
            items.append(stripped)
    return items
