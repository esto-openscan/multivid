from __future__ import annotations

import os
from dataclasses import dataclass
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
