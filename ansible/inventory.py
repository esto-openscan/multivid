#!/usr/bin/env python3
"""Dynamic Ansible inventory derived from the single multivid.yml fleet file."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "coordinator"))

from openscan_multicam_coordinator.config import load_fleet_config  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"--list", "--host"}:
        print("Usage: inventory.py --list", file=sys.stderr)
        return 2
    if sys.argv[1] == "--host":
        print("{}")
        return 0

    fleet = load_fleet_config(REPOSITORY_ROOT / "multivid.yml")
    hosts: dict[str, dict[str, object]] = {}
    for node in fleet.nodes:
        hosts[node.name] = {
            "ansible_host": node.host,
            "camera_id": node.camera_id,
            "openscan_camera_transform": node.camera_transform,
            "openscan_camera_node_profile_overrides": node.profile_overrides,
            "enabled": node.enabled,
        }
    inventory = {
        "camera_nodes": {
            "hosts": list(hosts),
            "vars": {
                "ansible_user": fleet.bootstrap_user,
                "ansible_ssh_private_key_file": str(fleet.identity_file),
                "openscan_harvest_ssh_public_key_file": str(fleet.public_key_file),
            },
        },
        "_meta": {"hostvars": hosts},
    }
    print(json.dumps(inventory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
