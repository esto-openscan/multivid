from __future__ import annotations

import csv
import json
import math
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_EDIT_ASSETS_SUBDIR = Path("edit_assets")
DEFAULT_PROXY_HEIGHT = 720


@dataclass(frozen=True)
class EditAssetOptions:
    session_path: Path
    output_dir: Path | None = None
    proxies: bool = False
    proxy_height: int = DEFAULT_PROXY_HEIGHT
    overwrite: bool = False
    dry_run: bool = False
    include_cameras: tuple[str, ...] = ()
    take_id: str | None = None


@dataclass(frozen=True)
class RecordingAsset:
    asset_id: str
    camera_id: str
    node_name: str
    take_id: str
    source_recording_path: Path
    source_recording_relative_path: str
    master_mp4_path: Path
    proxy_path: Path | None
    manifest_path: Path | None
    manifest_relative_path: str | None
    recording_start_time: str | None
    recording_stop_time: str | None
    pre_roll_seconds: float | None
    usable_start_offset_seconds: float | None
    profile: str | None
    duration_seconds: float | None
    source_file_size: int | None
    master_file_size: int | None
    proxy_file_size: int | None
    master_status: str
    proxy_status: str | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditAssetPlan:
    session_id: str
    session_path: Path
    output_dir: Path
    masters_dir: Path
    proxies_dir: Path
    stills_dir: Path
    index_json_path: Path
    index_csv_path: Path
    report_path: Path
    notes_path: Path
    import_list_path: Path
    recordings: tuple[RecordingAsset, ...]
    skipped_takes: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
DurationProbe = Callable[[Path], float | None]


def prepare_edit_assets(
    options: EditAssetOptions,
    *,
    command_runner: CommandRunner | None = None,
    duration_probe: DurationProbe | None = None,
) -> dict[str, Any]:
    started_at = _utc_now()
    plan = build_edit_asset_plan(
        options,
        duration_probe=duration_probe,
        probe_durations=not options.dry_run,
        check_external_tools=command_runner is None,
    )
    planned_commands = planned_ffmpeg_commands(plan, proxy_height=options.proxy_height, overwrite=options.overwrite)
    report = build_report(
        plan=plan,
        options=options,
        started_at=started_at,
        finished_at=None,
        status="planned",
        ffmpeg_commands=planned_commands,
        executed_commands=[],
    )

    if options.dry_run:
        report["finished_at"] = _utc_now()
        report["overall_status"] = _status_from_plan(plan)
        return report

    if plan.errors:
        report["finished_at"] = _utc_now()
        report["overall_status"] = "failed"
        _write_outputs(plan, report)
        return report

    runner = command_runner or _default_command_runner
    executed_commands: list[list[str]] = []
    run_warnings: list[str] = []
    run_errors: list[str] = []
    updated_assets: list[RecordingAsset] = []

    plan.masters_dir.mkdir(parents=True, exist_ok=True)
    if options.proxies:
        plan.proxies_dir.mkdir(parents=True, exist_ok=True)
    plan.stills_dir.mkdir(parents=True, exist_ok=True)

    for asset in plan.recordings:
        current = asset
        if asset.errors:
            updated_assets.append(asset)
            continue

        if asset.master_status != "existing":
            command = build_master_command(asset.source_recording_path, asset.master_mp4_path, overwrite=options.overwrite)
            executed_commands.append(command)
            result = runner(command)
            if result.returncode != 0:
                error = f"{asset.asset_id}: ffmpeg remux failed: {(result.stderr or result.stdout or '').strip()}"
                run_errors.append(error)
                current = _replace_asset(current, master_status="failed", errors=(*current.errors, error))
            elif _empty_or_missing(asset.master_mp4_path):
                error = f"{asset.asset_id}: remux output is missing or empty: {asset.master_mp4_path}"
                run_errors.append(error)
                current = _replace_asset(current, master_status="failed", errors=(*current.errors, error))
            else:
                current = _replace_asset(current, master_status="created")

        if options.proxies and current.proxy_path and not current.errors and current.proxy_status != "existing":
            command = build_proxy_command(
                current.master_mp4_path,
                current.proxy_path,
                proxy_height=options.proxy_height,
                overwrite=options.overwrite,
            )
            executed_commands.append(command)
            result = runner(command)
            if result.returncode != 0:
                error = f"{asset.asset_id}: ffmpeg proxy generation failed: {(result.stderr or result.stdout or '').strip()}"
                run_errors.append(error)
                current = _replace_asset(current, proxy_status="failed", errors=(*current.errors, error))
            elif _empty_or_missing(current.proxy_path):
                error = f"{asset.asset_id}: proxy output is missing or empty: {current.proxy_path}"
                run_errors.append(error)
                current = _replace_asset(current, proxy_status="failed", errors=(*current.errors, error))
            else:
                current = _replace_asset(current, proxy_status="created")

        if current.master_status == "existing" and current.master_mp4_path.stat().st_size <= 0:
            warning = f"{asset.asset_id}: existing master is empty: {current.master_mp4_path}"
            run_warnings.append(warning)
            current = _replace_asset(current, warnings=(*current.warnings, warning))
        if current.proxy_path and current.proxy_status == "existing" and current.proxy_path.stat().st_size <= 0:
            warning = f"{asset.asset_id}: existing proxy is empty: {current.proxy_path}"
            run_warnings.append(warning)
            current = _replace_asset(current, warnings=(*current.warnings, warning))

        updated_assets.append(_refresh_file_sizes(current))

    final_plan = _replace_plan(plan, recordings=tuple(updated_assets))
    status = _status_from_assets(final_plan.recordings, final_plan.errors + tuple(run_errors))
    final_report = build_report(
        plan=final_plan,
        options=options,
        started_at=started_at,
        finished_at=_utc_now(),
        status=status,
        extra_warnings=run_warnings,
        extra_errors=run_errors,
        ffmpeg_commands=planned_commands,
        executed_commands=executed_commands,
    )
    _write_outputs(final_plan, final_report)
    return final_report


def build_edit_asset_plan(
    options: EditAssetOptions,
    *,
    duration_probe: DurationProbe | None = None,
    probe_durations: bool = True,
    check_external_tools: bool = True,
) -> EditAssetPlan:
    session_path = options.session_path.expanduser().resolve()
    output_dir = (options.output_dir.expanduser().resolve() if options.output_dir else session_path / DEFAULT_EDIT_ASSETS_SUBDIR)
    masters_dir = output_dir / "masters_mp4"
    proxies_dir = output_dir / "proxies"
    stills_dir = output_dir / "stills"
    warnings: list[str] = []
    errors: list[str] = []
    skipped_takes: list[dict[str, str]] = []

    if options.proxy_height <= 0:
        errors.append("proxy height must be greater than zero")
    if check_external_tools and not options.dry_run and shutil.which("ffmpeg") is None:
        errors.append("ffmpeg was not found on PATH")

    index_path = session_path / "session_index.json"
    if not index_path.exists():
        return EditAssetPlan(
            session_id=session_path.name,
            session_path=session_path,
            output_dir=output_dir,
            masters_dir=masters_dir,
            proxies_dir=proxies_dir,
            stills_dir=stills_dir,
            index_json_path=output_dir / "edit_assets_index.json",
            index_csv_path=output_dir / "edit_assets_index.csv",
            report_path=output_dir / "edit_assets_report.json",
            notes_path=output_dir / "kdenlive_import_notes.md",
            import_list_path=output_dir / "import_list.txt",
            recordings=(),
            skipped_takes=(),
            warnings=(),
            errors=(f"session_index.json is missing: {index_path}",),
        )

    index = _read_json(index_path)
    session_id = str(index.get("session_id") or session_path.name)
    nodes = index.get("nodes") if isinstance(index.get("nodes"), list) else []
    if not nodes:
        errors.append("session_index.json contains no nodes")

    requested_cameras = set(options.include_cameras)
    indexed_cameras = {_camera_id(node) for node in nodes if isinstance(node, dict)}
    for camera_id in sorted(requested_cameras - indexed_cameras):
        warnings.append(f"requested camera {camera_id} is not present in session_index.json")

    take_ids = _selected_take_ids(index, nodes, options.take_id)
    if options.take_id and options.take_id not in take_ids:
        skipped_takes.append({"take_id": options.take_id, "reason": "take not present in session_index.json"})
        errors.append(f"requested take is not present in session_index.json: {options.take_id}")
    if not take_ids and not options.take_id:
        errors.append("no takes found in session_index.json")

    probe = duration_probe or ffprobe_duration
    if check_external_tools and probe_durations and duration_probe is None and shutil.which("ffprobe") is None:
        warnings.append("ffprobe was not found on PATH; duration fields may be empty")
        probe_durations = False

    recordings: list[RecordingAsset] = []
    selected_camera_count = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        camera_id = _camera_id(node)
        if requested_cameras and camera_id not in requested_cameras:
            continue
        selected_camera_count += 1
        node_name = str(node.get("node_name") or camera_id)
        node_takes = node.get("takes") if isinstance(node.get("takes"), list) else []
        for node_take in node_takes:
            if not isinstance(node_take, dict):
                continue
            take_id = str(node_take.get("take_id") or "")
            if not take_id or take_id not in take_ids:
                continue
            asset = _asset_from_take(
                session_path=session_path,
                masters_dir=masters_dir,
                proxies_dir=proxies_dir,
                node_name=node_name,
                camera_id=camera_id,
                node_take=node_take,
                proxies=options.proxies,
                overwrite=options.overwrite,
                probe_duration=probe if probe_durations else None,
            )
            recordings.append(asset)

    if requested_cameras and selected_camera_count == 0:
        errors.append("no cameras selected")
    if not recordings:
        errors.append("no recording files were found")

    return EditAssetPlan(
        session_id=session_id,
        session_path=session_path,
        output_dir=output_dir,
        masters_dir=masters_dir,
        proxies_dir=proxies_dir,
        stills_dir=stills_dir,
        index_json_path=output_dir / "edit_assets_index.json",
        index_csv_path=output_dir / "edit_assets_index.csv",
        report_path=output_dir / "edit_assets_report.json",
        notes_path=output_dir / "kdenlive_import_notes.md",
        import_list_path=output_dir / "import_list.txt",
        recordings=tuple(recordings),
        skipped_takes=tuple(skipped_takes),
        warnings=tuple(_dedupe(warnings)),
        errors=tuple(_dedupe(errors)),
    )


def build_master_command(source_path: Path, output_path: Path, *, overwrite: bool) -> list[str]:
    command = ["ffmpeg", "-y" if overwrite else "-n", "-hide_banner", "-fflags", "+genpts", "-i", str(source_path)]
    command.extend(["-c", "copy", "-movflags", "+faststart", str(output_path)])
    return command


def build_proxy_command(source_path: Path, output_path: Path, *, proxy_height: int, overwrite: bool) -> list[str]:
    scale_filter = f"scale=-2:min({proxy_height}\\,ih)"
    return [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-i",
        str(source_path),
        "-vf",
        scale_filter,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def planned_ffmpeg_commands(plan: EditAssetPlan, *, proxy_height: int = DEFAULT_PROXY_HEIGHT, overwrite: bool = False) -> list[list[str]]:
    commands: list[list[str]] = []
    for asset in plan.recordings:
        if asset.errors:
            continue
        if asset.master_status != "existing":
            commands.append(build_master_command(asset.source_recording_path, asset.master_mp4_path, overwrite=overwrite))
        if asset.proxy_path and asset.proxy_status != "existing":
            commands.append(build_proxy_command(asset.master_mp4_path, asset.proxy_path, proxy_height=proxy_height, overwrite=overwrite))
    return commands


def build_report(
    *,
    plan: EditAssetPlan,
    options: EditAssetOptions,
    started_at: str,
    finished_at: str | None,
    status: str,
    ffmpeg_commands: list[list[str]],
    executed_commands: list[list[str]],
    extra_warnings: list[str] | None = None,
    extra_errors: list[str] | None = None,
) -> dict[str, Any]:
    asset_warnings = [warning for asset in plan.recordings for warning in asset.warnings]
    asset_errors = [error for asset in plan.recordings for error in asset.errors]
    warnings = _dedupe([*plan.warnings, *asset_warnings, *(extra_warnings or [])])
    errors = _dedupe([*plan.errors, *asset_errors, *(extra_errors or [])])
    skipped_existing = [
        asset.asset_id
        for asset in plan.recordings
        if asset.master_status == "existing" or asset.proxy_status == "existing"
    ]
    conflicts = [
        {"asset_id": asset.asset_id, "errors": list(asset.errors)}
        for asset in plan.recordings
        if any("already exists" in error for error in asset.errors)
    ]
    return {
        "session_id": plan.session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input_session_path": str(plan.session_path),
        "output_path": str(plan.output_dir),
        "dry_run": options.dry_run,
        "proxies_requested": options.proxies,
        "proxy_height": options.proxy_height,
        "number_of_recordings_found": len(plan.recordings),
        "number_of_mp4_masters_created": sum(1 for asset in plan.recordings if asset.master_status == "created"),
        "number_of_proxies_created": sum(1 for asset in plan.recordings if asset.proxy_status == "created"),
        "files_skipped_existing": skipped_existing,
        "conflicts": conflicts,
        "warnings": warnings,
        "errors": errors,
        "ffmpeg_commands": [_quote_command(command) for command in ffmpeg_commands],
        "executed_ffmpeg_commands": [_quote_command(command) for command in executed_commands],
        "assets": [_asset_report(asset) for asset in plan.recordings],
        "edit_assets_index_json": str(plan.index_json_path),
        "edit_assets_index_csv": str(plan.index_csv_path),
        "kdenlive_import_notes": str(plan.notes_path),
        "import_list": str(plan.import_list_path),
        "overall_status": status,
    }


def ffprobe_duration(path: Path) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        value = float(result.stdout.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _asset_from_take(
    *,
    session_path: Path,
    masters_dir: Path,
    proxies_dir: Path,
    node_name: str,
    camera_id: str,
    node_take: dict[str, Any],
    proxies: bool,
    overwrite: bool,
    probe_duration: DurationProbe | None,
) -> RecordingAsset:
    take_id = str(node_take.get("take_id") or "")
    asset_id = f"{_safe_name(camera_id)}_{_safe_name(take_id)}"
    warnings: list[str] = []
    errors: list[str] = []
    recording_rel = node_take.get("recording_relative_path")
    source_path = session_path / str(recording_rel or "")
    if not isinstance(recording_rel, str) or not recording_rel:
        errors.append(f"{asset_id}: recording_relative_path is missing")
    elif not source_path.exists():
        errors.append(f"{asset_id}: recording is missing: {recording_rel}")
    elif source_path.stat().st_size <= 0:
        errors.append(f"{asset_id}: recording is empty: {recording_rel}")

    manifest_rel = node_take.get("manifest_relative_path")
    manifest_path = session_path / manifest_rel if isinstance(manifest_rel, str) and manifest_rel else None
    manifest = _read_json(manifest_path) if manifest_path and manifest_path.exists() else {}
    if manifest_path is None or not manifest_path.exists():
        warnings.append(f"{asset_id}: manifest is missing")

    master_path = masters_dir / f"{asset_id}.mp4"
    proxy_path = proxies_dir / f"{asset_id}_proxy.mp4" if proxies else None
    master_status = "planned"
    proxy_status = "planned" if proxies else None
    if master_path.exists() and not overwrite:
        master_status = "existing"
        if master_path.stat().st_size <= 0:
            warnings.append(f"{asset_id}: existing master is empty: {master_path}")
    if proxy_path and proxy_path.exists() and not overwrite:
        proxy_status = "existing"
        if proxy_path.stat().st_size <= 0:
            warnings.append(f"{asset_id}: existing proxy is empty: {proxy_path}")

    manifest_summary = node_take.get("manifest_summary") if isinstance(node_take.get("manifest_summary"), dict) else {}
    duration = _duration_from_metadata(node_take, manifest_summary, manifest)
    if duration is None and probe_duration and source_path.exists() and source_path.stat().st_size > 0:
        duration = probe_duration(source_path)

    source_size = source_path.stat().st_size if source_path.exists() else None
    return RecordingAsset(
        asset_id=asset_id,
        camera_id=camera_id,
        node_name=node_name,
        take_id=take_id,
        source_recording_path=source_path,
        source_recording_relative_path=str(recording_rel or ""),
        master_mp4_path=master_path,
        proxy_path=proxy_path,
        manifest_path=manifest_path,
        manifest_relative_path=manifest_rel if isinstance(manifest_rel, str) else None,
        recording_start_time=_string_or_none(node_take.get("recording_start_time"))
        or _string_or_none(manifest_summary.get("recording_start_time"))
        or _string_or_none(manifest.get("recording_start_time")),
        recording_stop_time=_string_or_none(node_take.get("recording_stop_time"))
        or _string_or_none(manifest_summary.get("recording_stop_time"))
        or _string_or_none(manifest.get("recording_stop_time")),
        pre_roll_seconds=_first_number(node_take, manifest_summary, manifest, key="pre_roll_seconds"),
        usable_start_offset_seconds=_first_number(node_take, manifest_summary, manifest, key="usable_start_offset_seconds"),
        profile=_string_or_none(node_take.get("profile"))
        or _string_or_none(manifest_summary.get("profile"))
        or _string_or_none(manifest.get("profile")),
        duration_seconds=duration,
        source_file_size=source_size,
        master_file_size=master_path.stat().st_size if master_path.exists() else None,
        proxy_file_size=proxy_path.stat().st_size if proxy_path and proxy_path.exists() else None,
        master_status=master_status,
        proxy_status=proxy_status,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _write_outputs(plan: EditAssetPlan, report: dict[str, Any]) -> None:
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    plan.index_json_path.write_text(json.dumps(_index_document(plan), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(plan.index_csv_path, plan.recordings)
    plan.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    plan.notes_path.write_text(_kdenlive_notes(plan), encoding="utf-8")
    plan.import_list_path.write_text(_import_list(plan), encoding="utf-8")


def _index_document(plan: EditAssetPlan) -> dict[str, Any]:
    return {
        "session_id": plan.session_id,
        "generated_at": _utc_now(),
        "source_session_path": str(plan.session_path),
        "output_path": str(plan.output_dir),
        "assets": [_asset_report(asset) for asset in plan.recordings],
    }


def _write_csv(path: Path, assets: tuple[RecordingAsset, ...]) -> None:
    columns = [
        "asset_id",
        "camera_id",
        "take_id",
        "master_mp4",
        "proxy",
        "source_recording",
        "profile",
        "pre_roll_seconds",
        "duration",
        "warnings",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for asset in assets:
            writer.writerow(
                {
                    "asset_id": asset.asset_id,
                    "camera_id": asset.camera_id,
                    "take_id": asset.take_id,
                    "master_mp4": str(asset.master_mp4_path),
                    "proxy": str(asset.proxy_path) if asset.proxy_path else "",
                    "source_recording": str(asset.source_recording_path),
                    "profile": asset.profile or "",
                    "pre_roll_seconds": "" if asset.pre_roll_seconds is None else asset.pre_roll_seconds,
                    "duration": "" if asset.duration_seconds is None else asset.duration_seconds,
                    "warnings": "; ".join(asset.warnings),
                }
            )


def _kdenlive_notes(plan: EditAssetPlan) -> str:
    stringout_path = plan.session_path / "derivatives" / "review" / "multicam_stringout.mp4"
    return f"""# Kdenlive import notes

- Import `{plan.masters_dir}` into Kdenlive or another NLE for normal editing.
- Optionally import `{stringout_path}` as a review/reference clip if it exists.
- Kdenlive can generate its own proxies if playback is slow.
- Pipeline-generated proxies, when requested, are in `{plan.proxies_dir}`.
- Raw `.h264` files remain the capture masters and should not be modified.
- Reference stills can be imported from the harvested session if useful.

This milestone does not generate a `.kdenlive` project, timeline, edit decisions, overlays, Shorts, or final render.
"""


def _import_list(plan: EditAssetPlan) -> str:
    paths = [str(asset.master_mp4_path) for asset in plan.recordings if not asset.errors]
    stringout_path = plan.session_path / "derivatives" / "review" / "multicam_stringout.mp4"
    if stringout_path.exists():
        paths.append(str(stringout_path))
    return "\n".join(paths) + ("\n" if paths else "")


def _asset_report(asset: RecordingAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "camera_id": asset.camera_id,
        "node_name": asset.node_name,
        "take_id": asset.take_id,
        "source_recording_path": str(asset.source_recording_path),
        "master_mp4_path": str(asset.master_mp4_path),
        "proxy_path": str(asset.proxy_path) if asset.proxy_path else None,
        "manifest_path": str(asset.manifest_path) if asset.manifest_path else None,
        "recording_start_time": asset.recording_start_time,
        "recording_stop_time": asset.recording_stop_time,
        "pre_roll_seconds": asset.pre_roll_seconds,
        "usable_start_offset_seconds": asset.usable_start_offset_seconds,
        "profile": asset.profile,
        "duration": asset.duration_seconds,
        "source_file_size": asset.source_file_size,
        "master_file_size": asset.master_file_size,
        "proxy_file_size": asset.proxy_file_size,
        "master_status": asset.master_status,
        "proxy_status": asset.proxy_status,
        "warnings": list(asset.warnings),
        "errors": list(asset.errors),
    }


def _duration_from_metadata(node_take: dict[str, Any], manifest_summary: dict[str, Any], manifest: dict[str, Any]) -> float | None:
    for container in (node_take, manifest_summary, manifest):
        duration = _number_or_none(container.get("duration_seconds"))
        if duration is not None and duration > 0:
            return duration
    start = (
        _string_or_none(node_take.get("recording_start_time"))
        or _string_or_none(manifest_summary.get("recording_start_time"))
        or _string_or_none(manifest.get("recording_start_time"))
    )
    stop = (
        _string_or_none(node_take.get("recording_stop_time"))
        or _string_or_none(manifest_summary.get("recording_stop_time"))
        or _string_or_none(manifest.get("recording_stop_time"))
    )
    if start and stop:
        start_dt = _parse_datetime(start)
        stop_dt = _parse_datetime(stop)
        if start_dt and stop_dt:
            seconds = (stop_dt - start_dt).total_seconds()
            if seconds > 0:
                return seconds
    return None


def _selected_take_ids(index: dict[str, Any], nodes: list[Any], requested_take: str | None) -> list[str]:
    take_ids: list[str] = []
    source_takes = index.get("takes") if isinstance(index.get("takes"), list) else []
    for take_id in source_takes:
        if isinstance(take_id, str) and take_id not in take_ids:
            take_ids.append(take_id)
    if not take_ids:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for take in node.get("takes", []):
                if isinstance(take, dict) and isinstance(take.get("take_id"), str) and take["take_id"] not in take_ids:
                    take_ids.append(take["take_id"])
    if requested_take:
        return [requested_take] if requested_take in take_ids else []
    return sorted(take_ids)


def _camera_id(node: dict[str, Any]) -> str:
    return str(node.get("camera_id") or node.get("folder") or node.get("node_name") or "unknown")


def _status_from_plan(plan: EditAssetPlan) -> str:
    if plan.errors or any(asset.errors for asset in plan.recordings):
        return "failed" if not plan.recordings else "partial"
    if plan.skipped_takes:
        return "partial"
    return "complete"


def _status_from_assets(assets: tuple[RecordingAsset, ...], errors: tuple[str, ...]) -> str:
    if not assets:
        return "failed"
    failed = [asset for asset in assets if asset.errors]
    if failed and len(failed) == len(assets):
        return "failed"
    if failed or errors:
        return "partial"
    return "complete"


def _replace_asset(asset: RecordingAsset, **changes: Any) -> RecordingAsset:
    data = asset.__dict__.copy()
    data.update(changes)
    return RecordingAsset(**data)


def _replace_plan(plan: EditAssetPlan, **changes: Any) -> EditAssetPlan:
    data = plan.__dict__.copy()
    data.update(changes)
    return EditAssetPlan(**data)


def _refresh_file_sizes(asset: RecordingAsset) -> RecordingAsset:
    return _replace_asset(
        asset,
        master_file_size=asset.master_mp4_path.stat().st_size if asset.master_mp4_path.exists() else None,
        proxy_file_size=asset.proxy_path.stat().st_size if asset.proxy_path and asset.proxy_path.exists() else None,
    )


def _empty_or_missing(path: Path) -> bool:
    return not path.exists() or path.stat().st_size <= 0


def _default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _quote_command(command: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_number(*containers: dict[str, Any], key: str) -> float | None:
    for container in containers:
        value = _number_or_none(container.get(key))
        if value is not None:
            return value
    return None


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return safe or "unknown"


def _dedupe(items: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item and item not in seen:
            output.append(item)
            seen.add(item)
    return output


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
