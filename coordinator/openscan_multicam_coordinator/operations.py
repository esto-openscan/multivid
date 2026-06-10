from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .client import NodeResult, request_node
from .config import NodeConfig


DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    json_body: dict[str, Any] | None = None
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


async def request_nodes(
    nodes: list[NodeConfig],
    spec: RequestSpec,
    client: httpx.AsyncClient | None = None,
) -> list[NodeResult]:
    if not nodes:
        return []

    if client is not None:
        tasks = [request_node(client, node, spec.method, spec.path, spec.json_body) for node in nodes]
        return list(await asyncio.gather(*tasks))

    timeout = httpx.Timeout(spec.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as managed_client:
        tasks = [request_node(managed_client, node, spec.method, spec.path, spec.json_body) for node in nodes]
        return list(await asyncio.gather(*tasks))


def dashboard_node_config(node: NodeConfig) -> dict[str, Any]:
    return {
        "name": node.name,
        "camera_id": node.camera_id,
        "base_url": node.base_url,
        "enabled": node.enabled,
    }


def aggregate_operation_response(operation: str, results: list[NodeResult]) -> dict[str, Any]:
    nodes = []
    for result in results:
        node = dashboard_result(result)
        node["message"] = operation_message(operation, result)
        nodes.append(node)
    return {
        "operation": operation,
        "ok": all(result.ok for result in results),
        "node_count": len(results),
        "success_count": sum(1 for result in results if result.ok),
        "failure_count": sum(1 for result in results if not result.ok),
        "nodes": nodes,
    }


def dashboard_result(result: NodeResult) -> dict[str, Any]:
    data = result.data or {}
    state = _state_from_result(result)
    positioning = data.get("positioning") if isinstance(data.get("positioning"), dict) else {}
    last_still = data.get("last_still_capture")
    if not isinstance(last_still, dict):
        still_capture = data.get("still_capture") if isinstance(data.get("still_capture"), dict) else None
        reference_still = data.get("reference_still") if isinstance(data.get("reference_still"), dict) else None
        last = data.get("last") if isinstance(data.get("last"), dict) else None
        last_still = still_capture or reference_still or last
    last_calibration = data.get("calibration") if isinstance(data.get("calibration"), dict) else None
    if last_calibration is None:
        last_calibration = data.get("last") if isinstance(data.get("last"), dict) else None
    if last_calibration is None and data.get("last_calibration_id"):
        last_calibration = {"calibration_id": data.get("last_calibration_id")}

    snapshot_path = _first_string(
        data.get("positioning_snapshot_path"),
        positioning.get("snapshot_path"),
        data.get("snapshot_path"),
    )
    stream_path = _first_string(
        data.get("positioning_stream_path"),
        positioning.get("stream_path"),
        data.get("stream_path"),
    )
    positioning_running = _first_bool(
        data.get("positioning_running"),
        positioning.get("running"),
    )
    recording_running = _first_bool(data.get("recording_running"), data.get("recording"))

    return {
        "node": dashboard_node_config(result.node),
        "ok": result.ok,
        "online": result.ok or result.status_code is not None,
        "status_code": result.status_code,
        "error": result.error,
        "message": result_message(result),
        "state": state,
        "current_session_id": data.get("current_session_id"),
        "current_take_id": data.get("current_take_id"),
        "current_profile": data.get("current_profile") or data.get("profile"),
        "prepared_session_id": data.get("prepared_session_id"),
        "prepared_profile": data.get("prepared_profile"),
        "prepared_valid": data.get("prepared_valid"),
        "output_path": data.get("output_path"),
        "positioning_running": bool(positioning_running),
        "recording_running": bool(recording_running),
        "calibration_running": bool(data.get("calibration_running")),
        "still_capture_running": bool(data.get("still_capture_running")),
        "last_error": data.get("last_error") or data.get("last_positioning_error"),
        "warnings": _list_of_strings(data.get("warnings")),
        "allowed": data.get("allowed") if isinstance(data.get("allowed"), dict) else {},
        "snapshot_url": build_node_url(result.node, snapshot_path or "/positioning/snapshot.jpg"),
        "stream_url": build_node_url(result.node, stream_path or "/positioning/stream.mjpg"),
        "last_still_capture": last_still,
        "last_calibration": last_calibration,
        "raw": data,
    }


def result_message(result: NodeResult) -> str:
    prefix = result.node.name
    if result.ok:
        state = _state_from_result(result)
        return f"{prefix}: {state}"
    if result.status_code is None:
        return f"{prefix}: offline"
    return f"{prefix}: rejected ({result.status_code}) {result.error or ''}".strip()


def operation_message(operation: str, result: NodeResult) -> str:
    if not result.ok:
        return result_message(result)
    actions = {
        "positioning_start": "positioning started",
        "positioning_stop": "positioning stopped",
        "stills_capture": "reference still captured",
        "calibration_run": "calibration completed",
        "recordings_start": "recording started",
        "recordings_stop": "recording stopped",
        "prepare_reset": "prepare reset",
    }
    if operation in actions:
        return f"{result.node.name}: {actions[operation]}"
    if operation == "status":
        return result_message(result)
    return f"{result.node.name}: ok"


def build_node_url(node: NodeConfig, path_or_url: str | None) -> str | None:
    if not path_or_url:
        return None
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
    return f"{node.base_url}{path}"


def aggregate_profiles(results: list[NodeResult]) -> dict[str, Any]:
    node_profiles: list[dict[str, Any]] = []
    profile_sets: list[set[str]] = []
    for result in results:
        names: list[str] = []
        if result.ok and result.data and isinstance(result.data.get("profiles"), dict):
            names = sorted(str(name) for name in result.data["profiles"].keys())
            profile_sets.append(set(names))
        node_profiles.append(
            {
                "node": dashboard_node_config(result.node),
                "ok": result.ok,
                "status_code": result.status_code,
                "error": result.error,
                "profile_names": names,
            }
        )

    compatible = bool(profile_sets) and len(profile_sets) == len(results) and len({frozenset(item) for item in profile_sets}) == 1
    profile_names = sorted(profile_sets[0]) if compatible else sorted(set().union(*profile_sets)) if profile_sets else []
    warning = None
    if not results:
        warning = "no camera nodes are configured"
    elif not compatible:
        warning = "profile names differ between nodes or at least one node did not return profiles; manual profile entry is available"

    return {
        "ok": compatible,
        "compatible": compatible,
        "profile_names": profile_names,
        "nodes": node_profiles,
        "warning": warning,
    }


def _state_from_result(result: NodeResult) -> str:
    if not result.ok and result.status_code is None:
        return "offline"
    data = result.data or {}
    value = data.get("state")
    return str(value) if value else ("online" if result.ok else "error")


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _first_bool(*values: Any) -> bool:
    for value in values:
        if isinstance(value, bool):
            return value
    return False


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
