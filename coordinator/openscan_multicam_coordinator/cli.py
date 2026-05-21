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
            body = {"session_id": args.session, "profile": args.profile}
            tasks = [request_node(client, node, "POST", "/recordings/start", body) for node in nodes]
        elif args.command == "stop":
            tasks = [request_node(client, node, "POST", "/recordings/stop") for node in nodes]
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        return list(await asyncio.gather(*tasks))


def _print_results(command: str, results: list[NodeResult]) -> None:
    for result in results:
        prefix = f"{result.node.name} ({result.node.camera_id})"
        if not result.ok:
            status = f"HTTP {result.status_code}" if result.status_code is not None else "offline"
            print(f"{prefix}: FAILED [{status}] {result.error}")
            continue

        if command == "profiles":
            print(f"{prefix}: OK profiles={_format_profiles(result.data)}")
        else:
            print(f"{prefix}: OK {_format_status(result.data)}")


def _format_status(data: dict[str, Any] | None) -> str:
    if not data:
        return "no JSON body"

    return (
        f"recording={data.get('recording')} "
        f"session={data.get('current_session_id')} "
        f"profile={data.get('current_profile')} "
        f"output={data.get('output_path')} "
        f"pid={data.get('process_pid')}"
    )


def _format_profiles(data: dict[str, Any] | None) -> str:
    if not data or not isinstance(data.get("profiles"), dict):
        return "none"
    return ", ".join(sorted(data["profiles"].keys()))


if __name__ == "__main__":
    raise SystemExit(main())
