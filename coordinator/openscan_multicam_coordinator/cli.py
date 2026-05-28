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

    prepare_parser = subparsers.add_parser("prepare", help="Prepare all nodes for a session/profile")
    prepare_parser.add_argument("--session", required=True, help="Session id to prepare")
    prepare_parser.add_argument("--profile", required=True, help="Recording profile name")
    prepare_parser.add_argument("--force", action="store_true", help="Recreate prepared state")
    prepare_parser.add_argument("--refocus", action="store_true", help="Request focus-related prepare behavior")

    prepare_reset_parser = subparsers.add_parser("prepare-reset", help="Clear prepared state for a session")
    prepare_reset_parser.add_argument("--session", required=True, help="Session id whose prepared state should be cleared")

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
    timeout = httpx.Timeout(args.timeout)
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
        elif args.command == "stop":
            tasks = [request_node(client, node, "POST", "/recordings/stop") for node in nodes]
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        return list(await asyncio.gather(*tasks))


def _print_results(command: str, results: list[NodeResult]) -> None:
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
    columns = ["node", "camera", "result", "state", "prepared", "recording", "session", "take", "profile", "output", "error"]
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in columns
    }
    print("  ".join(column.upper().ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    _print_failure_summary(results)


def _format_profiles(data: dict[str, Any] | None) -> str:
    if not data or not isinstance(data.get("profiles"), dict):
        return "none"
    return ", ".join(sorted(data["profiles"].keys()))


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
            "profile": _value(data.get("current_profile")),
            "output": _value(data.get("output_path")),
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
        "profile": _value(data.get("current_profile")),
        "output": _value(data.get("output_path")),
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
