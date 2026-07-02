from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from .client import NodeResult, request_node
from .config import DEFAULT_NODES_PATH, NodeConfig, load_nodes_config
from .edit_assets import DEFAULT_PROXY_HEIGHT, EditAssetOptions, prepare_edit_assets
from .harvest import DEFAULT_BACKEND, DEFAULT_HARVEST_OUTPUT_ROOT, HarvestOptions, harvest_session
from .stringout import DEFAULT_FPS, DEFAULT_RESOLUTION, DEFAULT_SPEED, StringoutOptions, derive_stringout


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

    positioning_start_parser = subparsers.add_parser("positioning-start", help="Start low-res positioning preview")
    positioning_start_parser.add_argument("--width", type=int, help="Preview width")
    positioning_start_parser.add_argument("--height", type=int, help="Preview height")
    positioning_start_parser.add_argument("--fps", type=int, help="Preview frame rate")
    positioning_start_parser.add_argument("--jpeg-quality", type=int, help="Preview JPEG quality, 1-100")
    positioning_start_parser.add_argument(
        "--overlay",
        action="append",
        help="Overlay to draw: crosshair, grid, shorts-safe-area, camera-label. May be repeated.",
    )
    positioning_start_parser.add_argument("--profile", help="Optional recording profile whose camera controls are reused")
    subparsers.add_parser("positioning-stop", help="Stop positioning preview")
    subparsers.add_parser("positioning-status", help="Show positioning preview status")
    subparsers.add_parser("positioning-urls", help="Print browser-openable positioning preview URLs")

    stills_capture_parser = subparsers.add_parser("stills-capture", help="Capture high-resolution reference stills")
    stills_capture_parser.add_argument("--session", required=True, help="Session id for reference still storage")
    stills_capture_parser.add_argument("--label", help="Reference still label")
    stills_capture_parser.add_argument("--profile", help="Optional recording profile whose camera controls are reused")
    stills_capture_parser.add_argument("--width", type=int, help="Still width")
    stills_capture_parser.add_argument("--height", type=int, help="Still height")
    stills_capture_parser.add_argument("--quality", type=int, help="Still JPEG quality, 1-100")
    stills_capture_parser.add_argument("--notes", help="Optional operator note")
    stills_capture_parser.add_argument(
        "--no-recording-profile-controls",
        action="store_true",
        help="Do not reuse camera controls from --profile",
    )
    stills_capture_parser.add_argument("--force", action="store_true", help="Overwrite an existing label on each node")
    subparsers.add_parser("stills-status", help="Show reference still capture status")

    harvest_parser = subparsers.add_parser("harvest", help="Collect a recorded session from camera nodes")
    harvest_parser.add_argument("--session", required=True, help="Session id to harvest")
    harvest_parser.add_argument(
        "--output",
        default=str(DEFAULT_HARVEST_OUTPUT_ROOT),
        help="Directory where harvested session folders are written",
    )
    harvest_parser.add_argument("--backend", default=DEFAULT_BACKEND, choices=[DEFAULT_BACKEND], help="Fetch backend")
    harvest_parser.add_argument("--dry-run", action="store_true", help="Plan the harvest without copying files")
    harvest_parser.add_argument("--node", action="append", help="Harvest only this node name; may be repeated")
    harvest_parser.add_argument("--overwrite", action="store_true", help="Overwrite local files that differ")
    harvest_parser.add_argument("--clean", action="store_true", help="Delete the local session folder before harvesting")
    harvest_parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Return success even if some nodes/files are missing",
    )
    harvest_parser.add_argument(
        "--hash-video",
        action="store_true",
        help="Compute sha256 for video files in the generated session index",
    )

    stringout_parser = subparsers.add_parser(
        "derive-stringout",
        help="Generate a multicam review stringout from a harvested session folder",
    )
    stringout_parser.add_argument("--session-path", required=True, help="Path to a harvested session folder")
    stringout_parser.add_argument("--output-dir", help="Directory for review derivatives; defaults under the session")
    stringout_parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Playback speed factor")
    stringout_parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="Output frame rate")
    stringout_parser.add_argument("--resolution", default=DEFAULT_RESOLUTION, help="Output resolution, WIDTHxHEIGHT")
    stringout_parser.add_argument("--take", dest="take_id", help="Render only one take id")
    stringout_parser.add_argument(
        "--include-cameras",
        help="Comma-separated camera ids to include, such as front,side,top",
    )
    stringout_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing stringout output")
    stringout_parser.add_argument("--dry-run", action="store_true", help="Plan the render without calling ffmpeg")
    stringout_parser.add_argument("--no-slate", action="store_true", help="Do not render take slate sections")
    stringout_parser.add_argument("--no-labels", action="store_true", help="Do not burn camera labels into the grid")
    stringout_parser.add_argument("--no-per-take", action="store_true", help="Do not render individual per-take stringouts")
    stringout_parser.add_argument("--realtime", action="store_true", help="Shortcut for --speed 1")

    edit_assets_parser = subparsers.add_parser(
        "prepare-edit-assets",
        help="Prepare edit-friendly MP4 assets from a harvested session folder",
    )
    edit_assets_parser.add_argument("--session-path", required=True, help="Path to a harvested session folder")
    edit_assets_parser.add_argument("--output-dir", help="Directory for edit assets; defaults under the session")
    edit_assets_parser.add_argument("--proxies", action="store_true", help="Generate optional low-bitrate proxy MP4 files")
    edit_assets_parser.add_argument(
        "--proxy-height",
        type=int,
        default=DEFAULT_PROXY_HEIGHT,
        help="Maximum proxy height in pixels",
    )
    edit_assets_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing edit assets")
    edit_assets_parser.add_argument("--dry-run", action="store_true", help="Plan outputs without calling ffmpeg or writing files")
    edit_assets_parser.add_argument(
        "--include-cameras",
        help="Comma-separated camera ids to include, such as front,side,top",
    )
    edit_assets_parser.add_argument("--take", dest="take_id", help="Prepare only one take id")

    dashboard_parser = subparsers.add_parser("dashboard", help="Run the local browser operator dashboard")
    dashboard_parser.add_argument("--config", help="Path to nodes.yml; defaults to --nodes")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind host")
    dashboard_parser.add_argument("--port", type=int, default=8090, help="Dashboard bind port")
    dashboard_parser.add_argument("--open-browser", action="store_true", help="Open the dashboard URL in a browser")

    subparsers.add_parser("stop", help="Stop recording on all nodes")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "derive-stringout":
        return _run_stringout_command(args)
    if args.command == "prepare-edit-assets":
        return _run_edit_assets_command(args)

    nodes_path = getattr(args, "config", None) or args.nodes
    try:
        nodes = load_nodes_config(nodes_path)
    except Exception as exc:
        print(f"Failed to load nodes config: {exc}", file=sys.stderr)
        return 2

    if args.command == "dashboard":
        return _run_dashboard_command(args=args, nodes=nodes, config_path=nodes_path)

    if args.command == "harvest":
        return _run_harvest_command(args=args, nodes=nodes)

    results = asyncio.run(_run_command(args=args, nodes=nodes))
    _print_results(args.command, results)
    return 0 if all(result.ok for result in results) else 1


def _run_harvest_command(args: argparse.Namespace, nodes: list[NodeConfig]) -> int:
    selected_nodes = _select_harvest_nodes(nodes, args.node)
    if not selected_nodes:
        print("No matching nodes to harvest", file=sys.stderr)
        return 2

    options = HarvestOptions(
        session_id=args.session,
        output_root=Path(args.output),
        backend=args.backend,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        clean=args.clean,
        allow_partial=args.allow_partial,
        hash_video_files=args.hash_video,
        timeout=args.timeout,
    )
    try:
        outcome = harvest_session(selected_nodes, options)
    except Exception as exc:
        print(f"Harvest failed: {exc}", file=sys.stderr)
        return 1

    _print_harvest_summary(outcome.harvest_report, dry_run=args.dry_run)
    if outcome.complete or args.allow_partial:
        return 0
    return 1


def _run_stringout_command(args: argparse.Namespace) -> int:
    include_cameras = _parse_comma_list(args.include_cameras)
    options = StringoutOptions(
        session_path=Path(args.session_path),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        speed=1.0 if args.realtime else args.speed,
        fps=args.fps,
        resolution=args.resolution,
        take_id=args.take_id,
        include_cameras=tuple(include_cameras),
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        no_slate=args.no_slate,
        no_labels=args.no_labels,
        no_per_take=args.no_per_take,
    )
    try:
        report = derive_stringout(options)
    except Exception as exc:
        print(f"Stringout failed: {exc}", file=sys.stderr)
        return 1
    _print_stringout_summary(report)
    return 0 if report.get("overall_status") in {"complete", "partial"} else 1


def _run_edit_assets_command(args: argparse.Namespace) -> int:
    include_cameras = _parse_comma_list(args.include_cameras)
    options = EditAssetOptions(
        session_path=Path(args.session_path),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        proxies=args.proxies,
        proxy_height=args.proxy_height,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        include_cameras=tuple(include_cameras),
        take_id=args.take_id,
    )
    try:
        report = prepare_edit_assets(options)
    except Exception as exc:
        print(f"Edit asset preparation failed: {exc}", file=sys.stderr)
        return 1
    _print_edit_assets_summary(report)
    return 0 if report.get("overall_status") in {"complete", "partial"} else 1


def _parse_comma_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _run_dashboard_command(args: argparse.Namespace, nodes: list[NodeConfig], config_path: str | Path) -> int:
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard URL: {url}")
    print(f"Loaded nodes: {len(nodes)}")
    print(f"Config path: {config_path}")
    print("Warning: this dashboard is a local/trusted-network MVP with no authentication.")
    try:
        from .dashboard.app import serve

        serve(config_path=config_path, host=args.host, port=args.port, open_browser=args.open_browser)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Dashboard failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _select_harvest_nodes(nodes: list[NodeConfig], requested_names: list[str] | None) -> list[NodeConfig]:
    if not requested_names:
        return nodes
    requested = set(requested_names)
    return [node for node in nodes if node.name in requested]


async def _run_command(args: argparse.Namespace, nodes: list[NodeConfig]) -> list[NodeResult]:
    timeout_seconds = args.timeout
    if args.command == "calibrate":
        timeout_seconds = max(args.timeout, float(args.duration) + 20.0)
    if args.command == "stills-capture":
        timeout_seconds = max(args.timeout, 30.0)
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
        elif args.command == "positioning-start":
            body = {}
            for key in ("width", "height", "fps"):
                value = getattr(args, key)
                if value is not None:
                    body[key] = value
            if args.jpeg_quality is not None:
                body["jpeg_quality"] = args.jpeg_quality
            if args.overlay:
                body["overlays"] = args.overlay
            if args.profile:
                body["profile"] = args.profile
            tasks = [request_node(client, node, "POST", "/positioning/start", body) for node in nodes]
        elif args.command == "positioning-stop":
            tasks = [request_node(client, node, "POST", "/positioning/stop") for node in nodes]
        elif args.command in {"positioning-status", "positioning-urls"}:
            tasks = [request_node(client, node, "GET", "/positioning/status") for node in nodes]
        elif args.command == "stills-capture":
            body = {"session_id": args.session}
            if args.label:
                body["label"] = args.label
            if args.profile:
                body["profile"] = args.profile
            if args.width is not None:
                body["width"] = args.width
            if args.height is not None:
                body["height"] = args.height
            if args.quality is not None:
                body["quality"] = args.quality
            if args.notes:
                body["notes"] = args.notes
            if args.no_recording_profile_controls:
                body["use_recording_profile_controls"] = False
            if args.force:
                body["force"] = True
            tasks = [request_node(client, node, "POST", "/stills/capture", body) for node in nodes]
        elif args.command == "stills-status":
            tasks = [request_node(client, node, "GET", "/stills/status") for node in nodes]
        elif args.command == "stop":
            tasks = [request_node(client, node, "POST", "/recordings/stop") for node in nodes]
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        return list(await asyncio.gather(*tasks))


def _print_results(command: str, results: list[NodeResult]) -> None:
    if command.startswith("positioning-"):
        _print_positioning_results(command, results)
        _print_failure_summary(results)
        return

    if command.startswith("stills-"):
        _print_stills_results(command, results)
        _print_failure_summary(results)
        return

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


def _print_harvest_summary(report: dict[str, Any], dry_run: bool = False) -> None:
    mode = "DRY RUN " if dry_run else ""
    print(
        f"{mode}harvest session={report.get('session_id')} status={report.get('overall_status')} "
        f"output={report.get('session_dir')}"
    )
    nodes = report.get("nodes") if isinstance(report.get("nodes"), list) else []
    rows: list[dict[str, str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        rows.append(
            {
                "node": str(node.get("node_name", "")),
                "camera": str(node.get("camera_id", "")),
                "status": str(node.get("status", "")),
                "copied": str(node.get("files_copied", 0)),
                "unchanged": str(node.get("files_skipped_unchanged", 0)),
                "conflicts": str(node.get("files_conflicted", 0)),
                "missing": str(len(node.get("missing_files", []))),
                "errors": str(len(node.get("errors", []))),
            }
        )
    if rows:
        columns = ["node", "camera", "status", "copied", "unchanged", "conflicts", "missing", "errors"]
        widths = {column: max(len(column), *(len(row[column]) for row in rows)) for column in columns}
        print("  ".join(column.upper().ljust(widths[column]) for column in columns))
        print("  ".join("-" * widths[column] for column in columns))
        for row in rows:
            print("  ".join(row[column].ljust(widths[column]) for column in columns))

    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")
    if not dry_run:
        print(f"Index: {report.get('session_dir')}/session_index.json")
        print(f"Report: {report.get('session_dir')}/harvest_report.json")


def _print_stringout_summary(report: dict[str, Any]) -> None:
    mode = "DRY RUN " if report.get("dry_run") else ""
    print(
        f"{mode}stringout session={report.get('session_id')} status={report.get('overall_status')} "
        f"output={report.get('output_file_path')}"
    )
    take_sections = report.get("take_sections") if isinstance(report.get("take_sections"), list) else []
    rows: list[dict[str, str]] = []
    for take in take_sections:
        if not isinstance(take, dict):
            continue
        rows.append(
            {
                "take": str(take.get("take_id", "")),
                "status": str(take.get("status", "")),
                "cameras": ",".join(str(item) for item in take.get("included_cameras", [])),
                "missing": ",".join(str(item) for item in take.get("missing_cameras", [])),
                "duration": _format_seconds(take.get("common_duration_seconds")),
                "warnings": str(len(take.get("warnings", []))),
            }
        )
    if rows:
        _print_table(rows, ["take", "status", "cameras", "missing", "duration", "warnings"])

    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")
    if not report.get("dry_run"):
        print(f"Report: {report.get('report_file_path')}")
        print(f"Commands: {report.get('ffmpeg_commands_file_path')}")
    per_take_paths = report.get("per_take_output_file_paths")
    if isinstance(per_take_paths, dict) and per_take_paths:
        print(f"Per-take outputs: {len(per_take_paths)}")


def _print_edit_assets_summary(report: dict[str, Any]) -> None:
    mode = "DRY RUN " if report.get("dry_run") else ""
    print(
        f"{mode}edit-assets session={report.get('session_id')} status={report.get('overall_status')} "
        f"output={report.get('output_path')}"
    )
    assets = report.get("assets") if isinstance(report.get("assets"), list) else []
    rows: list[dict[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        rows.append(
            {
                "asset": str(asset.get("asset_id", "")),
                "camera": str(asset.get("camera_id", "")),
                "take": str(asset.get("take_id", "")),
                "master": str(asset.get("master_status", "")),
                "proxy": str(asset.get("proxy_status") or "-"),
                "duration": _format_seconds(asset.get("duration")),
                "warnings": str(len(asset.get("warnings", []))),
                "errors": str(len(asset.get("errors", []))),
            }
        )
    if rows:
        _print_table(rows, ["asset", "camera", "take", "master", "proxy", "duration", "warnings", "errors"])

    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")
    if not report.get("dry_run"):
        print(f"Index: {report.get('edit_assets_index_json')}")
        print(f"CSV: {report.get('edit_assets_index_csv')}")
        print(f"Report: {report.get('output_path')}/edit_assets_report.json")
        print(f"Import notes: {report.get('kdenlive_import_notes')}")


def _format_seconds(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}s"
    return "-"


def _print_table(rows: list[dict[str, str]], columns: list[str]) -> None:
    if not rows:
        print("No nodes")
        return
    widths = {column: max(len(column), *(len(str(row.get(column, ""))) for row in rows)) for column in columns}
    print("  ".join(column.upper().ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


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


def _print_positioning_results(command: str, results: list[NodeResult]) -> None:
    if command == "positioning-urls":
        rows = _positioning_url_rows(results)
        columns = ["node", "camera", "result", "running", "snapshot_url", "stream_url", "error"]
    else:
        rows = [_positioning_row(result) for result in results]
        columns = ["node", "camera", "result", "state", "running", "settings", "snapshot", "stream", "warnings", "error"]
    _print_table(rows, columns)


def _print_stills_results(command: str, results: list[NodeResult]) -> None:
    rows = [_stills_row(result, command) for result in results]
    columns = ["node", "camera", "result", "running", "label", "status", "image", "warnings", "error"]
    _print_table(rows, columns)


def _positioning_row(result: NodeResult) -> dict[str, str]:
    data = result.data or {}
    if not result.ok:
        status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
        return {
            "node": result.node.name,
            "camera": result.node.camera_id,
            "result": f"FAILED {status}",
            "state": _value(data.get("state")),
            "running": _value(data.get("running")),
            "settings": _format_positioning_settings(data.get("settings")),
            "snapshot": _value(data.get("snapshot_path")),
            "stream": _value(data.get("stream_path")),
            "warnings": _format_warnings(data.get("warnings")),
            "error": result.error or "",
        }
    return {
        "node": result.node.name,
        "camera": result.node.camera_id,
        "result": "OK",
        "state": _value(data.get("state")),
        "running": _value(data.get("running")),
        "settings": _format_positioning_settings(data.get("settings")),
        "snapshot": _value(data.get("snapshot_path")),
        "stream": _value(data.get("stream_path")),
        "warnings": _format_warnings(data.get("warnings")),
        "error": _value(data.get("last_error"), empty=""),
    }


def _positioning_url_rows(results: list[NodeResult]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for result in results:
        data = result.data or {}
        status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
        rows.append(
            {
                "node": result.node.name,
                "camera": result.node.camera_id,
                "result": "OK" if result.ok else f"FAILED {status}",
                "running": _value(data.get("running")),
                "snapshot_url": f"{result.node.base_url}/positioning/snapshot.jpg",
                "stream_url": f"{result.node.base_url}/positioning/stream.mjpg",
                "error": "" if result.ok else result.error or "",
            }
        )
    return rows


def _stills_row(result: NodeResult, command: str) -> dict[str, str]:
    data = result.data or {}
    if not result.ok:
        status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
        return {
            "node": result.node.name,
            "camera": result.node.camera_id,
            "result": f"FAILED {status}",
            "running": _value(data.get("running")),
            "label": "-",
            "status": "-",
            "image": "-",
            "warnings": "",
            "error": result.error or "",
        }

    summary = data.get("still_capture") if isinstance(data.get("still_capture"), dict) else None
    if summary is None and isinstance(data.get("reference_still"), dict):
        summary = data["reference_still"]
    if summary is None and isinstance(data.get("last"), dict):
        summary = data["last"]
    summary = summary or {}
    return {
        "node": result.node.name,
        "camera": result.node.camera_id,
        "result": "OK",
        "running": _value(data.get("running"), empty="false") if command == "stills-status" else _value(data.get("still_capture_running"), empty="false"),
        "label": _value(summary.get("label")),
        "status": _value(summary.get("status")),
        "image": _value(summary.get("image_file_path")),
        "warnings": _format_warnings(summary.get("warnings")),
        "error": _format_warnings(summary.get("errors")),
    }


def _format_positioning_settings(settings: Any) -> str:
    if not isinstance(settings, dict):
        return "-"
    width = settings.get("width")
    height = settings.get("height")
    fps = settings.get("fps")
    quality = settings.get("jpeg_quality")
    overlays = settings.get("overlays") if isinstance(settings.get("overlays"), list) else []
    parts = []
    if width and height:
        parts.append(f"{width}x{height}")
    if fps:
        parts.append(f"{fps}fps")
    if quality:
        parts.append(f"q={quality}")
    if overlays:
        parts.append("overlays=" + ",".join(str(item) for item in overlays))
    return " ".join(parts) if parts else "-"


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
