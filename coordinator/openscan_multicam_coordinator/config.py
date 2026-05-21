from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_NODES_PATH = os.environ.get("OPENSCAN_MULTICAM_NODES", "examples/nodes.yml")


@dataclass(frozen=True)
class NodeConfig:
    name: str
    camera_id: str
    base_url: str


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
        nodes.append(NodeConfig(name=name, camera_id=camera_id, base_url=base_url))

    if not nodes:
        raise ValueError(f"{config_path}: at least one node is required")

    return nodes


def _required_string(raw_node: dict[str, Any], key: str, path: Path, index: int) -> str:
    value = raw_node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: nodes[{index}].{key} must be a non-empty string")
    return value.strip()
