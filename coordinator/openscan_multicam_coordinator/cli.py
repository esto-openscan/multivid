from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from .client import NodeResult, request_node
from .config import DEFAULT_NODES_PATH, NodeConfig, load_nodes_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multicam", description="Control OpenScan camera nodes")
    parser.add_argument("--nodes", default=DEFAULT_NODES_PATH, help="Path to nodes.yml")
    parser.add_argument("--timeout", type=float, default=8.0, help="Per-node HTTP timeout in seconds")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show recording status for all nodes")
    subparsers.add_parser("profiles", help="Show recording profiles exposed by all nodes")

    start_parser = subparsers.add_parser("start", help="Start recording on all nodes")
    start_parser.add_argument("--session", required=True, help="Session id to record into")
    start_parser.add_argument("--profile", required=True, help="Recording profile name")
    start_parser.add_argument("--take", dest="take_id", help="Take id to record into")
    start_parser.add_argument("--force-prepare", action="store_true", help="Recreate prepared state before recording")
    start_parser.add_argument("--refocus", action="store_true", help="Request focus-related prepare behavior")
    start_parser.add_argument(
        "--apply-calibration-suggestions",
        action="store_true",
        help="Explicitly apply linked calibration suggestions to this recording",
    )

    prepare_parser = subparsers.add_parser("prepare", help="Prepare all nodes for a session/profile")
    prepare_parser.add_argument("--session", required=True, help="Session id to prepare")
    prepare_parser.add_argument("--profile", required=True, help="Recording profile name")
    prepare_parser.add_argument("--force", action="store_true", help="Recreate prepared state")
    prepare_parser.add_argument("--refocus", action="store_true", help="Request focus-related prepare behavior")

    prepare_reset_parser = subparsers.add_parser("prepare-reset", help="Clear prepared state for a session")
    prepare_reset_parser.add_argument("--session", required=True, help="Session id whose prepared state should be cleared")

    calibrate_parser = subparsers.add_parser("calibrate", help="Run a short calibration capture on all nodes")
    calibrate_parser.add_argument("--session", required=True, help="Session id to calibrate into")
    calibrate_parser.add_argument("--profile", required=True, help="Recording profile name")
    calibrate_parser.add_argument("--duration", type=float, default=5.0, help="Calibration duration in seconds")
    calibrate_parser.add_argument("--calibration-id", help="Optional calibration id")
    calibrate_parser.add_argument("--target", help="Optional target label, such as gray_card or scene")
    calibrate_parser.add_argument("--notes", help="Optional operator note")
    calibrate_parser.add_argument(
        "--apply-to-session",
        action="store_true",
        help="Mark the resulting suggestions as active for this session",
    )

    subparsers.add_parser("calibration-status", help="Show calibration status for all nodes")
    subparsers.add_parser("calibration-last", help="Show the last calibration result for all nodes")
    subparsers.add_parser("calibration-suggestions", help="Show copyable per-node calibration suggestions")

    subparsers.add_parser("stop", help="Stop recording on all nodes")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        nodes = load_nodes_config(args.nodes)
    except Exception as exc:
        print(f"Failed to load nodes config: {exc}", file=sys.stderr)
        return 2

    results = asyncio.run(_run_command(args=args, nodes=nodes))
    _print_results(args.command, results)
    return 0 if all(result.ok for result in results) else 1


async def _run_command(args: argparse.Namespace, nodes: list[NodeConfig]) -> list[NodeResult]:
    timeout_seconds = args.timeout
    if args.command == "calibrate":
        timeout_seconds = max(args.timeout, float(args.duration) + 20.0)
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if args.command == "status":
            tasks = [request_node(client, node, "GET", "/status") for node in nodes]
        elif args.command == "profiles":
            tasks = [request_node(client, node, "GET", "/profiles") for node in nodes]
        elif args.command == "start":
            body: dict[str, Any] = {"session_id": args.session, "profile": args.profile}
            if args.take_id:
                body["take_id"] = args.take_id
            if args.force_prepare:
                body["force_prepare"] = True
            if args.refocus:
                body["refocus"] = True
            if args.apply_calibration_suggestions:
                body["apply_calibration_suggestions"] = True
            tasks = [request_node(client, node, "POST", "/recordings/start", body) for node in nodes]
        elif args.command == "prepare":
            body = {"session_id": args.session, "profile": args.profile}
            if args.force:
                body["force"] = True
            if args.refocus:
                body["refocus"] = True
            tasks = [request_node(client, node, "POST", "/prepare", body) for node in nodes]
        elif args.command == "prepare-reset":
            body = {"session_id": args.session}
            tasks = [request_node(client, node, "POST", "/prepare/reset", body) for node in nodes]
        elif args.command == "calibrate":
            body = {
                "session_id": args.session,
                "profile": args.profile,
                "duration_seconds": args.duration,
                "apply_to_session": args.apply_to_session,
            }
            if args.calibration_id:
                body["calibration_id"] = args.calibration_id
            if args.target:
                body["target"] = args.target
            if args.notes:
                body["notes"] = args.notes
            tasks = [request_node(client, node, "POST", "/calibration/run", body) for node in nodes]
        elif args.command == "calibration-status":
            tasks = [request_node(client, node, "GET", "/calibration/status") for node in nodes]
        elif args.command in {"calibration-last", "calibration-suggestions"}:
            tasks = [request_node(client, node, "GET", "/calibration/last") for node in nodes]
        elif args.command == "stop":
            tasks = [request_node(client, node, "POST", "/recordings/stop") for node in nodes]
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        return list(await asyncio.gather(*tasks))


def _print_results(command: str, results: list[NodeResult]) -> None:
    if command in {"calibrate", "calibration-status", "calibration-last", "calibration-suggestions"}:
        _print_calibration_results(command, results)
        _print_failure_summary(results)
        return

    if command == "profiles":
        for result in results:
            prefix = f"{result.node.name} ({result.node.camera_id})"
            if result.ok:
                print(f"{prefix}: OK profiles={_format_profiles(result.data)}")
            else:
                status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
                print(f"{prefix}: FAILED [{status}] {result.error}")
        _print_failure_summary(results)
        return

    rows = [_result_row(result) for result in results]
    columns = [
        "node",
        "camera",
        "result",
        "state",
        "prepared",
        "recording",
        "session",
        "take",
        "profile",
        "controls",
        "output",
        "warnings",
        "error",
    ]
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in columns
    }
    print("  ".join(column.upper().ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    _print_failure_summary(results)


def _print_calibration_results(command: str, results: list[NodeResult]) -> None:
    for result in results:
        prefix = f"{result.node.name} ({result.node.camera_id})"
        if not result.ok:
            status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
            print(f"{prefix}: FAILED [{status}] {result.error}")
            continue

        data = result.data or {}
        if command == "calibration-status":
            last = data.get("last") if isinstance(data.get("last"), dict) else {}
            running = data.get("running", False)
            calibration_id = last.get("calibration_id") if isinstance(last, dict) else None
            print(f"{prefix}: OK running={running} last={_value(calibration_id)}")
            continue

        summary = data.get("calibration") if isinstance(data.get("calibration"), dict) else data
        print(_format_calibration_summary(prefix, summary, show_yaml=command == "calibration-suggestions"))


def _format_calibration_summary(prefix: str, summary: dict[str, Any], show_yaml: bool = False) -> str:
    if not summary or summary.get("last") is None and "suggested_controls" not in summary:
        return f"{prefix}: no calibration recorded"
    suggestions = summary.get("suggested_controls")
    if not isinstance(suggestions, dict):
        suggestions = {}
    controls = suggestions.get("suggested_controls")
    if not isinstance(controls, dict):
        controls = {}

    lines = [
        f"{prefix}: {summary.get('status', 'OK')} calibration={_value(summary.get('calibration_id'))} "
        f"profile={_value(summary.get('profile'))} confidence={_value(summary.get('confidence') or suggestions.get('confidence'))}"
    ]
    for field in ("shutter_us", "gain", "awbgains", "lens_position"):
        item = controls.get(field)
        value = item.get("value") if isinstance(item, dict) else None
        label = field if field != "shutter_us" else "shutter_us"
        lines.append(f"  suggested {label}: {_format_suggested_value(value)}")
    warnings = summary.get("warnings") or suggestions.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("  warnings:")
        lines.extend(f"    - {warning}" for warning in warnings)
    if show_yaml:
        yaml_lines = _calibration_yaml_snippet(result_camera_id=summary.get("camera_id"), controls=controls)
        if yaml_lines:
            lines.append("  per-node override snippet:")
            lines.extend(f"    {line}" if line else "" for line in yaml_lines)
    return "\n".join(lines)


def _format_suggested_value(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, list) and len(value) == 2:
        return f"[{value[0]}, {value[1]}]"
    return str(value)


def _calibration_yaml_snippet(result_camera_id: Any, controls: dict[str, Any]) -> list[str]:
    values: dict[str, Any] = {}
    for field, item in controls.items():
        if isinstance(item, dict) and item.get("value") is not None:
            values[field] = item["value"]
    if not values:
        return []
    lines = ["profile_overrides:", "  video_1080p25_locked:", "    camera_controls:"]
    for field in ("shutter_us", "gain", "awbgains", "lens_position"):
        if field in values:
            lines.append(f"      {field}: {_yaml_value(values[field])}")
    if result_camera_id:
        lines.append(f"# camera_id: {result_camera_id}")
    return lines


def _yaml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _format_profiles(data: dict[str, Any] | None) -> str:
    if not data or not isinstance(data.get("profiles"), dict):
        return "none"
    parts = []
    for name, profile in sorted(data["profiles"].items()):
        profile_data = profile if isinstance(profile, dict) else {}
        summary = _format_controls_summary(profile_data.get("resolved_controls") or profile_data)
        parts.append(f"{name} [{summary}]" if summary and summary != "-" else name)
    return "; ".join(parts)


def _result_row(result: NodeResult) -> dict[str, str]:
    data = result.data or {}
    if not result.ok:
        status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
        return {
            "node": result.node.name,
            "camera": result.node.camera_id,
            "result": f"FAILED {status}",
            "state": _value(data.get("state")),
            "prepared": _format_prepared(data),
            "recording": _value(data.get("recording_running")),
            "session": _value(data.get("current_session_id")),
            "take": _value(data.get("current_take_id")),
            "profile": _status_profile(data),
            "controls": _format_controls_summary(data.get("resolved_controls")),
            "output": _value(data.get("output_path")),
            "warnings": _format_warnings(data.get("warnings")),
            "error": result.error or "",
        }

    return {
        "node": result.node.name,
        "camera": result.node.camera_id,
        "result": "OK",
        "state": _value(data.get("state")),
        "prepared": _format_prepared(data),
        "recording": _value(data.get("recording_running")),
        "session": _value(data.get("current_session_id")),
        "take": _value(data.get("current_take_id")),
        "profile": _status_profile(data),
        "controls": _format_controls_summary(data.get("resolved_controls")),
        "output": _value(data.get("output_path")),
        "warnings": _format_warnings(data.get("warnings")),
        "error": _value(data.get("last_error"), empty=""),
    }


def _format_prepared(data: dict[str, Any]) -> str:
    session_id = data.get("prepared_session_id")
    profile = data.get("prepared_profile")
    valid = data.get("prepared_valid")
    if not session_id and not profile:
        return "-"
    valid_marker = "valid" if valid else "invalid"
    return f"{session_id or '-'}/{profile or '-'}:{valid_marker}"


def _status_profile(data: dict[str, Any]) -> str:
    return _value(data.get("current_profile") or data.get("prepared_profile") or data.get("last_profile"))


def _format_controls_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return "-"
    recording = data.get("recording") if isinstance(data.get("recording"), dict) else {}
    camera_controls = data.get("camera_controls") if isinstance(data.get("camera_controls"), dict) else {}

    parts: list[str] = []
    width = recording.get("width")
    height = recording.get("height")
    framerate = recording.get("framerate")
    if width and height and framerate:
        parts.append(f"{width}x{height}@{framerate}")
    elif width and height:
        parts.append(f"{width}x{height}")
    elif framerate:
        parts.append(f"{framerate}fps")

    bitrate = recording.get("bitrate")
    if bitrate:
        parts.append(f"{bitrate}bps")

    shutter_us = camera_controls.get("shutter_us")
    if shutter_us:
        parts.append(f"shutter={shutter_us}us")
    gain = camera_controls.get("gain")
    if gain:
        parts.append(f"gain={gain}")
    awbgains = camera_controls.get("awbgains")
    if awbgains:
        parts.append(f"awb={_format_pair(awbgains)}")
    autofocus_mode = camera_controls.get("autofocus_mode")
    lens_position = camera_controls.get("lens_position")
    if autofocus_mode and lens_position is not None:
        parts.append(f"focus={autofocus_mode}:{lens_position}")
    elif autofocus_mode:
        parts.append(f"focus={autofocus_mode}")
    elif lens_position is not None:
        parts.append(f"lens={lens_position}")

    return ", ".join(parts) if parts else "-"


def _format_pair(value: Any) -> str:
    if isinstance(value, list) and len(value) == 2:
        return f"{value[0]},{value[1]}"
    return str(value)


def _format_warnings(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    if len(value) == 1:
        return str(value[0])
    return f"{len(value)} warnings: {value[0]}"


def _value(value: Any, empty: str = "-") -> str:
    if value is None:
        return empty
    return str(value)


def _print_failure_summary(results: list[NodeResult]) -> None:
    failed = [result for result in results if not result.ok]
    if failed:
        print(f"\nPartial failure: {len(failed)}/{len(results)} node requests failed.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
