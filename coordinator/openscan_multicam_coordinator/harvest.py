from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import NodeConfig


DEFAULT_HARVEST_OUTPUT_ROOT = Path("harvested_sessions")
DEFAULT_BACKEND = "rsync_ssh"
SMALL_HASH_LIMIT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class HarvestOptions:
    session_id: str
    output_root: Path = DEFAULT_HARVEST_OUTPUT_ROOT
    backend: str = DEFAULT_BACKEND
    dry_run: bool = False
    overwrite: bool = False
    clean: bool = False
    allow_partial: bool = False
    hash_small_files: bool = True
    hash_video_files: bool = False
    timeout: float = 8.0


@dataclass(frozen=True)
class HarvestOutcome:
    session_dir: Path
    session_index: dict[str, Any]
    harvest_report: dict[str, Any]

    @property
    def complete(self) -> bool:
        return self.harvest_report.get("overall_status") == "complete"


def harvest_session(nodes: list[NodeConfig], options: HarvestOptions) -> HarvestOutcome:
    if options.backend != DEFAULT_BACKEND:
        raise ValueError(f"unsupported harvest backend: {options.backend}")

    session_dir = options.output_root / options.session_id
    started_at = _utc_now()
    if options.clean and session_dir.exists() and not options.dry_run:
        shutil.rmtree(session_dir)
    if not options.dry_run:
        (session_dir / "nodes").mkdir(parents=True, exist_ok=True)

    node_reports: list[dict[str, Any]] = []
    command_snapshot = {
        "session_id": options.session_id,
        "output_root": str(options.output_root),
        "backend": options.backend,
        "dry_run": options.dry_run,
        "overwrite": options.overwrite,
        "clean": options.clean,
        "allow_partial": options.allow_partial,
        "hash_small_files": options.hash_small_files,
        "hash_video_files": options.hash_video_files,
    }

    with httpx.Client(timeout=httpx.Timeout(options.timeout)) as client:
        for node in nodes:
            node_reports.append(_harvest_node(client, node, session_dir, options))

    finished_at = _utc_now()
    overall_status = _overall_status(node_reports)
    report = {
        "session_id": options.session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "output_root": str(options.output_root),
        "session_dir": str(session_dir),
        "transport_backend": options.backend,
        "command_snapshot": command_snapshot,
        "nodes": node_reports,
        "files_copied": sum(int(node.get("files_copied", 0)) for node in node_reports),
        "bytes_copied": sum(int(node.get("bytes_copied", 0)) for node in node_reports),
        "files_skipped_unchanged": sum(int(node.get("files_skipped_unchanged", 0)) for node in node_reports),
        "missing_files": _flatten_node_lists(node_reports, "missing_files"),
        "warnings": _flatten_node_lists(node_reports, "warnings"),
        "errors": _flatten_node_lists(node_reports, "errors"),
        "overall_status": overall_status,
    }
    index = build_session_index(
        session_id=options.session_id,
        session_dir=session_dir,
        nodes=nodes,
        node_reports=node_reports,
        harvested_at=finished_at,
        hash_small_files=options.hash_small_files,
        hash_video_files=options.hash_video_files,
    )

    if not options.dry_run:
        _write_json(session_dir / "session_index.json", index)
        _write_json(session_dir / "harvest_report.json", report)

    return HarvestOutcome(session_dir=session_dir, session_index=index, harvest_report=report)


def build_session_index(
    session_id: str,
    session_dir: Path,
    nodes: list[NodeConfig],
    node_reports: list[dict[str, Any]],
    harvested_at: str | None = None,
    hash_small_files: bool = True,
    hash_video_files: bool = False,
) -> dict[str, Any]:
    reports_by_name = {str(report.get("node_name")): report for report in node_reports}
    index_nodes: list[dict[str, Any]] = []
    all_take_ids: set[str] = set()
    profiles_by_take: dict[str, set[str]] = {}
    duplicate_take_warnings: list[str] = []

    for node in nodes:
        report = reports_by_name.get(node.name, _skipped_node_report(node, "not processed"))
        local_node_dir = session_dir / "nodes" / node.harvest_folder
        summary = report.get("remote_session_summary")
        if not isinstance(summary, dict):
            summary = {}
        source_takes = summary.get("takes") if isinstance(summary.get("takes"), list) else []
        source_reference_stills = (
            summary.get("reference_stills") if isinstance(summary.get("reference_stills"), list) else []
        )

        seen_take_ids: set[str] = set()
        takes: list[dict[str, Any]] = []
        for source_take in source_takes:
            if not isinstance(source_take, dict):
                continue
            take_id = str(source_take.get("take_id") or "")
            if not take_id:
                continue
            if take_id in seen_take_ids:
                duplicate_take_warnings.append(f"{node.name}: duplicate take id {take_id}")
            seen_take_ids.add(take_id)
            all_take_ids.add(take_id)

            take_dir = local_node_dir / take_id
            manifest_path = take_dir / "manifest.json"
            manifest = _read_json(manifest_path) if manifest_path.exists() else None
            manifest_summary = _manifest_summary(manifest)
            profile = manifest_summary.get("profile")
            if isinstance(profile, str) and profile:
                profiles_by_take.setdefault(take_id, set()).add(profile)

            recording_name = _recording_name(source_take, manifest)
            recording_path = take_dir / recording_name if recording_name else None
            integrity = _take_integrity(
                session_id=session_id,
                expected_camera_id=node.camera_id,
                take_id=take_id,
                manifest=manifest,
                manifest_path=manifest_path,
                recording_path=recording_path,
                stderr_path=take_dir / "rpicam-vid.stderr.log",
            )
            file_entries = _local_file_entries(
                take_dir,
                base_dir=session_dir,
                hash_small_files=hash_small_files,
                hash_video_files=hash_video_files,
            )
            takes.append(
                {
                    "take_id": take_id,
                    "relative_dir": _relative(take_dir, session_dir),
                    "status": "ok" if not integrity["errors"] else "incomplete",
                    "recording_file_name": recording_name,
                    "recording_relative_path": (
                        _relative(recording_path, session_dir) if recording_path is not None else None
                    ),
                    "recording_file_size": recording_path.stat().st_size if recording_path and recording_path.exists() else None,
                    "manifest_relative_path": _relative(manifest_path, session_dir),
                    "stderr_relative_path": _relative(take_dir / "rpicam-vid.stderr.log", session_dir),
                    "manifest_summary": manifest_summary,
                    "recording_start_time": manifest_summary.get("recording_start_time"),
                    "recording_stop_time": manifest_summary.get("recording_stop_time"),
                    "pre_roll_seconds": manifest_summary.get("pre_roll_seconds"),
                    "usable_start_offset_seconds": manifest_summary.get("usable_start_offset_seconds"),
                    "profile": manifest_summary.get("profile"),
                    "warnings": integrity["warnings"],
                    "errors": integrity["errors"],
                    "missing_expected_files": integrity["missing_files"],
                    "integrity_checks": integrity,
                    "files": file_entries,
                }
            )

        reference_stills: list[dict[str, Any]] = []
        for source_still in source_reference_stills:
            if not isinstance(source_still, dict):
                continue
            label = str(source_still.get("label") or "")
            if not label:
                continue
            image_relative = _reference_still_source_relative_path(source_still, "reference_still_image")
            manifest_relative = _reference_still_source_relative_path(source_still, "reference_still_manifest")
            image_path = local_node_dir / image_relative if image_relative else None
            manifest_path = local_node_dir / manifest_relative if manifest_relative else None
            still_manifest = _read_json(manifest_path) if manifest_path and manifest_path.exists() else None
            manifest_summary = _reference_still_manifest_summary(still_manifest)
            source_warnings = source_still.get("warnings") if isinstance(source_still.get("warnings"), list) else []
            source_errors = source_still.get("errors") if isinstance(source_still.get("errors"), list) else []
            manifest_warnings = (
                manifest_summary.get("warnings") if isinstance(manifest_summary.get("warnings"), list) else []
            )
            manifest_errors = (
                manifest_summary.get("errors") if isinstance(manifest_summary.get("errors"), list) else []
            )
            warnings = _dedupe(
                [
                    *[str(item) for item in source_warnings if item],
                    *[str(item) for item in manifest_warnings if item],
                ]
            )
            errors = _dedupe(
                [
                    *[str(item) for item in source_errors if item],
                    *[str(item) for item in manifest_errors if item],
                ]
            )
            if image_path is None:
                errors.append("reference still image path could not be determined")
            elif not image_path.exists():
                errors.append(f"{image_path.name} is missing")
            elif image_path.stat().st_size <= 0:
                errors.append(f"{image_path.name} is empty")
            if manifest_path is None:
                errors.append("reference still manifest path could not be determined")
            elif not manifest_path.exists():
                errors.append(f"{manifest_path.name} is missing")
            reference_stills.append(
                {
                    "label": label,
                    "status": "ok" if not errors else "incomplete",
                    "timestamp": manifest_summary.get("timestamp") or source_still.get("timestamp"),
                    "image_relative_path": _relative(image_path, session_dir) if image_path is not None else None,
                    "image_file_size": image_path.stat().st_size if image_path and image_path.exists() else None,
                    "manifest_relative_path": _relative(manifest_path, session_dir) if manifest_path is not None else None,
                    "manifest_summary": manifest_summary,
                    "warnings": warnings,
                    "errors": _dedupe(errors),
                    "files": _reference_still_local_file_entries(
                        source_still,
                        local_node_dir=local_node_dir,
                        session_dir=session_dir,
                        hash_small_files=hash_small_files,
                        hash_video_files=hash_video_files,
                    ),
                }
            )

        prepared_path = local_node_dir / "prepared_state.json"
        prepared_state = _read_json(prepared_path) if prepared_path.exists() else None
        node_errors = list(report.get("errors", []))
        node_warnings = list(report.get("warnings", []))
        if report.get("status") == "complete" and not prepared_path.exists():
            node_warnings.append("prepared_state.json was not present for this node/session")

        index_nodes.append(
            {
                "node_name": node.name,
                "camera_id": node.camera_id,
                "folder": node.harvest_folder,
                "hostname": summary.get("hostname") or report.get("node_hostname"),
                "base_url": node.base_url,
                "ssh_host": node.ssh_host,
                "harvest_status": report.get("status"),
                "remote_camera_session_path": report.get("remote_camera_session_path"),
                "local_relative_dir": _relative(local_node_dir, session_dir),
                "prepared_state_relative_path": _relative(prepared_path, session_dir),
                "prepared_state_present": prepared_path.exists(),
                "prepared_state_summary": _prepared_summary(prepared_state),
                "takes": takes,
                "reference_stills": reference_stills,
                "reference_still_count": len(reference_stills),
                "warnings": _dedupe(node_warnings),
                "errors": _dedupe(node_errors),
                "missing_files": report.get("missing_files", []),
            }
        )

    take_warnings = _take_profile_warnings(profiles_by_take)
    return {
        "session_id": session_id,
        "harvested_at": harvested_at or _utc_now(),
        "coordinator_hostname": platform.node(),
        "source_nodes": [
            {
                "name": node.name,
                "camera_id": node.camera_id,
                "base_url": node.base_url,
                "ssh_host": node.ssh_host,
                "folder": node.harvest_folder,
            }
            for node in nodes
        ],
        "nodes": index_nodes,
        "takes": sorted(all_take_ids),
        "warnings": _dedupe(duplicate_take_warnings + take_warnings),
        "errors": _dedupe(_flatten_node_lists(node_reports, "errors")),
    }


def build_rsync_command(node: NodeConfig, remote_path: str, local_path: Path) -> list[str]:
    if not node.ssh_host:
        raise ValueError(f"{node.name}: ssh_host is required for rsync_ssh harvest")
    remote_target = f"{node.ssh_user}@{node.ssh_host}" if node.ssh_user else node.ssh_host
    return ["rsync", "-a", "--protect-args", f"{remote_target}:{remote_path}", str(local_path)]


def remote_camera_session_path(node: NodeConfig, session_id: str) -> str:
    root = node.remote_output_root.rstrip("/")
    if root.endswith("/sessions") or root == "sessions":
        return f"{root}/{session_id}/{node.camera_id}"
    return f"{root}/sessions/{session_id}/{node.camera_id}"


def _harvest_node(
    client: httpx.Client,
    node: NodeConfig,
    session_dir: Path,
    options: HarvestOptions,
) -> dict[str, Any]:
    started_at = _utc_now()
    base_report = {
        "node_name": node.name,
        "camera_id": node.camera_id,
        "base_url": node.base_url,
        "ssh_host": node.ssh_host,
        "ssh_user": node.ssh_user,
        "remote_output_root": node.remote_output_root,
        "local_folder": node.harvest_folder,
        "transport_backend": options.backend,
        "started_at": started_at,
        "finished_at": None,
        "status": "pending",
        "files_copied": 0,
        "bytes_copied": 0,
        "files_skipped_unchanged": 0,
        "files_conflicted": 0,
        "missing_files": [],
        "warnings": [],
        "errors": [],
        "copied_files": [],
        "skipped_files": [],
        "conflict_files": [],
        "dry_run": options.dry_run,
    }
    if not node.enabled:
        return {**base_report, "status": "skipped", "finished_at": _utc_now(), "warnings": ["node is disabled"]}
    if not node.ssh_host:
        return {
            **base_report,
            "status": "failed",
            "finished_at": _utc_now(),
            "errors": ["ssh_host is required for rsync_ssh harvest"],
        }

    status_result = _get_json(client, f"{node.base_url}/status")
    if not status_result["ok"]:
        return {
            **base_report,
            "status": "offline",
            "finished_at": _utc_now(),
            "errors": [f"status request failed: {status_result['error']}"],
        }
    base_report["node_hostname"] = status_result["data"].get("hostname") if isinstance(status_result["data"], dict) else None

    session_result = _get_json(client, f"{node.base_url}/sessions/{options.session_id}")
    if not session_result["ok"]:
        return {
            **base_report,
            "status": "missing",
            "finished_at": _utc_now(),
            "errors": [f"session metadata request failed: {session_result['error']}"],
        }
    summary = session_result["data"]
    if not isinstance(summary, dict):
        return {
            **base_report,
            "status": "failed",
            "finished_at": _utc_now(),
            "errors": ["session metadata response was not a JSON object"],
        }
    if not summary.get("exists", False):
        return {
            **base_report,
            "status": "missing",
            "finished_at": _utc_now(),
            "remote_session_summary": summary,
            "remote_camera_session_path": summary.get("camera_session_path")
            or remote_camera_session_path(node, options.session_id),
            "errors": ["remote session directory does not exist"],
        }

    remote_root = str(summary.get("camera_session_path") or remote_camera_session_path(node, options.session_id))
    local_root = session_dir / "nodes" / node.harvest_folder
    file_specs = _remote_file_specs(summary)
    if not file_specs:
        return {
            **base_report,
            "status": "missing",
            "finished_at": _utc_now(),
            "remote_session_summary": summary,
            "remote_camera_session_path": remote_root,
            "errors": ["remote session contains no harvestable files"],
        }

    report = {
        **base_report,
        "remote_session_summary": summary,
        "remote_camera_session_path": remote_root,
        "local_session_path": str(local_root),
    }
    for spec in file_specs:
        _copy_remote_file(node, remote_root, local_root, spec, report, options)

    report["finished_at"] = _utc_now()
    if report["errors"]:
        report["status"] = "partial" if report["copied_files"] or report["skipped_files"] else "failed"
    elif report["missing_files"] or report["conflict_files"]:
        report["status"] = "partial"
    else:
        report["status"] = "complete"
    return report


def _copy_remote_file(
    node: NodeConfig,
    remote_root: str,
    local_root: Path,
    spec: dict[str, Any],
    report: dict[str, Any],
    options: HarvestOptions,
) -> None:
    relative_path = str(spec["relative_path"])
    remote_path = f"{remote_root.rstrip('/')}/{relative_path}"
    local_path = local_root / relative_path
    remote_size = spec.get("size")
    remote_mtime = spec.get("mtime")

    if spec.get("missing"):
        report["missing_files"].append(relative_path)
        return

    if local_path.exists() and isinstance(remote_size, int):
        local_size = local_path.stat().st_size
        local_mtime = local_path.stat().st_mtime
        if local_size == remote_size and _mtime_matches(local_mtime, remote_mtime):
            report["files_skipped_unchanged"] += 1
            report["skipped_files"].append({"relative_path": relative_path, "reason": "size and mtime match"})
            return
        if not options.overwrite:
            report["files_conflicted"] += 1
            report["conflict_files"].append(
                {
                    "relative_path": relative_path,
                    "local_size": local_size,
                    "remote_size": remote_size,
                    "local_mtime": local_mtime,
                    "remote_mtime": remote_mtime,
                    "reason": "local file differs; use --overwrite to replace it",
                }
            )
            report["warnings"].append(f"{relative_path}: local file differs and was not overwritten")
            return

    command = build_rsync_command(node, remote_path, local_path)
    report.setdefault("rsync_commands", []).append(command)
    if options.dry_run:
        report["skipped_files"].append({"relative_path": relative_path, "reason": "dry-run"})
        return

    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"rsync exited {result.returncode}"
        report["errors"].append(f"{relative_path}: {detail}")
        return

    report["files_copied"] += 1
    if isinstance(remote_size, int):
        report["bytes_copied"] += remote_size
    report["copied_files"].append({"relative_path": relative_path, "bytes": remote_size})


def _remote_file_specs(summary: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    prepared = summary.get("prepared_state")
    if isinstance(prepared, dict) and prepared.get("exists"):
        specs.append(
            {
                "relative_path": "prepared_state.json",
                "size": prepared.get("size"),
                "mtime": prepared.get("mtime"),
            }
        )

    takes = summary.get("takes") if isinstance(summary.get("takes"), list) else []
    for take in takes:
        if not isinstance(take, dict):
            continue
        take_id = take.get("take_id")
        if not isinstance(take_id, str) or not take_id:
            continue
        files = take.get("files") if isinstance(take.get("files"), list) else []
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            name = file_info.get("name")
            if not isinstance(name, str) or not name:
                continue
            specs.append(
                {
                    "relative_path": f"{take_id}/{name}",
                    "size": file_info.get("size"),
                    "mtime": file_info.get("mtime"),
                    "missing": not bool(file_info.get("exists", False)),
                }
            )
    reference_stills = summary.get("reference_stills") if isinstance(summary.get("reference_stills"), list) else []
    for still in reference_stills:
        if not isinstance(still, dict):
            continue
        files = still.get("files") if isinstance(still.get("files"), list) else []
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            relative_path = file_info.get("relative_path")
            name = file_info.get("name")
            if not isinstance(relative_path, str) or not relative_path:
                if not isinstance(name, str) or not name:
                    continue
                relative_path = f"reference_stills/{name}"
            specs.append(
                {
                    "relative_path": relative_path,
                    "size": file_info.get("size"),
                    "mtime": file_info.get("mtime"),
                    "missing": not bool(file_info.get("exists", False)),
                }
            )
    return specs


def _mtime_matches(local_mtime: float, remote_mtime: Any) -> bool:
    if not isinstance(remote_mtime, (int, float)):
        return True
    return abs(local_mtime - float(remote_mtime)) <= 1.0


def _get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    try:
        response = client.get(url)
        if not response.is_success:
            return {"ok": False, "data": None, "error": f"HTTP {response.status_code}: {response.text}"}
        data = response.json()
    except (httpx.RequestError, ValueError) as exc:
        return {"ok": False, "data": None, "error": str(exc)}
    return {"ok": isinstance(data, dict), "data": data, "error": None if isinstance(data, dict) else "not a JSON object"}


def _overall_status(node_reports: list[dict[str, Any]]) -> str:
    if not node_reports:
        return "failed"
    statuses = {str(report.get("status")) for report in node_reports}
    if statuses == {"complete"}:
        return "complete"
    if any(status == "complete" for status in statuses) or any(
        int(report.get("files_copied", 0)) > 0 or int(report.get("files_skipped_unchanged", 0)) > 0
        for report in node_reports
    ):
        return "partial"
    return "failed"


def _take_integrity(
    session_id: str,
    expected_camera_id: str,
    take_id: str,
    manifest: dict[str, Any] | None,
    manifest_path: Path,
    recording_path: Path | None,
    stderr_path: Path,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    missing: list[str] = []

    if manifest is None:
        errors.append("manifest.json is missing or unreadable")
        missing.append("manifest.json")
    else:
        if manifest.get("session_id") != session_id:
            errors.append(f"manifest session_id mismatch: {manifest.get('session_id')!r}")
        if manifest.get("camera_id") != expected_camera_id:
            errors.append(f"manifest camera_id mismatch: {manifest.get('camera_id')!r}")
        if manifest.get("take_id") != take_id:
            errors.append(f"manifest take_id mismatch: {manifest.get('take_id')!r}")
        warnings.extend(str(item) for item in manifest.get("warnings", []) if item)
        errors.extend(str(item) for item in manifest.get("errors", []) if item)

    if recording_path is None:
        errors.append("recording file name could not be determined")
    elif not recording_path.exists():
        errors.append(f"{recording_path.name} is missing")
        missing.append(recording_path.name)
    elif recording_path.stat().st_size <= 0:
        errors.append(f"{recording_path.name} is empty")

    if not stderr_path.exists():
        warnings.append("rpicam-vid.stderr.log was not present")

    return {
        "manifest_exists": manifest_path.exists(),
        "recording_exists": recording_path.exists() if recording_path else False,
        "recording_size_gt_zero": recording_path.stat().st_size > 0 if recording_path and recording_path.exists() else False,
        "stderr_log_exists": stderr_path.exists(),
        "missing_files": missing,
        "warnings": _dedupe(warnings),
        "errors": _dedupe(errors),
    }


def _manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {}
    fields = (
        "schema_version",
        "status",
        "session_id",
        "take_id",
        "camera_id",
        "hostname",
        "service_version",
        "backend",
        "profile",
        "recording_start_time",
        "recording_stop_time",
        "pre_roll_seconds",
        "usable_start_offset_seconds",
        "usable_start_time",
        "output_file_name",
        "exit_code",
        "warnings",
        "errors",
    )
    return {field: manifest.get(field) for field in fields if field in manifest}


def _reference_still_manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {}
    fields = (
        "schema_version",
        "status",
        "session_id",
        "camera_id",
        "label",
        "requested_label",
        "timestamp",
        "finished_at",
        "hostname",
        "service_version",
        "image_file_name",
        "requested_size",
        "requested_quality",
        "actual_file_size",
        "backend",
        "profile",
        "use_recording_profile_controls",
        "warnings",
        "errors",
        "state_before_capture",
        "positioning_behavior",
    )
    return {field: manifest.get(field) for field in fields if field in manifest}


def _prepared_summary(prepared_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(prepared_state, dict):
        return {}
    fields = (
        "schema_version",
        "valid",
        "session_id",
        "camera_id",
        "hostname",
        "profile",
        "prepared_at",
        "backend",
        "warnings",
        "errors",
    )
    return {field: prepared_state.get(field) for field in fields if field in prepared_state}


def _recording_name(source_take: dict[str, Any], manifest: dict[str, Any] | None) -> str | None:
    if isinstance(manifest, dict) and isinstance(manifest.get("output_file_name"), str):
        return str(manifest["output_file_name"])
    files = source_take.get("files") if isinstance(source_take.get("files"), list) else []
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        name = file_info.get("name")
        kind = file_info.get("kind")
        if isinstance(name, str) and kind == "recording":
            return name
    return None


def _reference_still_source_relative_path(source_still: dict[str, Any], kind: str) -> str | None:
    files = source_still.get("files") if isinstance(source_still.get("files"), list) else []
    for file_info in files:
        if not isinstance(file_info, dict) or file_info.get("kind") != kind:
            continue
        relative_path = file_info.get("relative_path")
        if isinstance(relative_path, str) and relative_path:
            return relative_path
        name = file_info.get("name")
        if isinstance(name, str) and name:
            return f"reference_stills/{name}"

    key = "image_file_name" if kind == "reference_still_image" else "manifest_file_name"
    name = source_still.get(key)
    if isinstance(name, str) and name:
        return f"reference_stills/{name}"
    return None


def _local_file_entries(
    path: Path,
    base_dir: Path,
    hash_small_files: bool,
    hash_video_files: bool,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir()):
        if not child.is_file():
            continue
        size = child.stat().st_size
        entry = {"name": child.name, "relative_path": _relative(child, base_dir), "size": size}
        if _should_hash(child, size, hash_small_files, hash_video_files):
            entry["sha256"] = _sha256(child)
        entries.append(entry)
    return entries


def _reference_still_local_file_entries(
    source_still: dict[str, Any],
    local_node_dir: Path,
    session_dir: Path,
    hash_small_files: bool,
    hash_video_files: bool,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    files = source_still.get("files") if isinstance(source_still.get("files"), list) else []
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        relative_path = file_info.get("relative_path")
        name = file_info.get("name")
        if not isinstance(relative_path, str) or not relative_path:
            if not isinstance(name, str) or not name:
                continue
            relative_path = f"reference_stills/{name}"
        local_path = local_node_dir / relative_path
        exists = local_path.exists()
        size = local_path.stat().st_size if exists else None
        entry: dict[str, Any] = {
            "name": local_path.name,
            "kind": file_info.get("kind"),
            "relative_path": _relative(local_path, session_dir),
            "exists": exists,
            "size": size,
        }
        if exists and isinstance(size, int) and _should_hash(local_path, size, hash_small_files, hash_video_files):
            entry["sha256"] = _sha256(local_path)
        entries.append(entry)
    return entries


def _should_hash(path: Path, size: int, hash_small_files: bool, hash_video_files: bool) -> bool:
    video_suffixes = {".h264", ".mp4", ".mkv", ".mov", ".avi"}
    if path.suffix.lower() in video_suffixes:
        return hash_video_files
    return hash_small_files and size <= SMALL_HASH_LIMIT_BYTES


def _take_profile_warnings(profiles_by_take: dict[str, set[str]]) -> list[str]:
    warnings: list[str] = []
    for take_id, profiles in sorted(profiles_by_take.items()):
        if len(profiles) > 1:
            warnings.append(f"{take_id}: mismatched profiles across nodes: {', '.join(sorted(profiles))}")
    return warnings


def _skipped_node_report(node: NodeConfig, reason: str) -> dict[str, Any]:
    return {
        "node_name": node.name,
        "camera_id": node.camera_id,
        "status": "skipped",
        "warnings": [reason],
        "errors": [],
        "missing_files": [],
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def _flatten_node_lists(node_reports: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for report in node_reports:
        raw_items = report.get(key, [])
        if isinstance(raw_items, list):
            values.extend(str(item) for item in raw_items)
    return _dedupe(values)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
