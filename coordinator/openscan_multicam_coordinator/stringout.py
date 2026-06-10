from __future__ import annotations

import json
import math
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_REVIEW_SUBDIR = Path("derivatives") / "review"
DEFAULT_RESOLUTION = "1920x1080"
DEFAULT_FPS = 30
DEFAULT_SPEED = 5.0
SLATE_SECONDS = 2.0
SIGNIFICANT_DURATION_DIFFERENCE_SECONDS = 1.0


@dataclass(frozen=True)
class StringoutOptions:
    session_path: Path
    output_dir: Path | None = None
    speed: float = DEFAULT_SPEED
    fps: int = DEFAULT_FPS
    resolution: str = DEFAULT_RESOLUTION
    take_id: str | None = None
    include_cameras: tuple[str, ...] = ()
    overwrite: bool = False
    dry_run: bool = False
    no_slate: bool = False
    no_labels: bool = False
    no_per_take: bool = False


@dataclass(frozen=True)
class CameraClip:
    camera_id: str
    node_name: str
    take_id: str
    recording_path: Path
    manifest_path: Path | None
    recording_relative_path: str
    manifest_relative_path: str | None
    profile: str | None
    source_fps: float | None
    usable_start_offset_seconds: float
    source_duration_seconds: float | None
    usable_duration_seconds: float | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TakePlan:
    take_id: str
    clips: tuple[CameraClip, ...]
    expected_cameras: tuple[str, ...]
    missing_cameras: tuple[str, ...]
    common_duration_seconds: float | None
    output_duration_seconds: float | None
    profile: str | None
    warnings: tuple[str, ...] = ()
    skipped_reason: str | None = None


@dataclass(frozen=True)
class StringoutPlan:
    session_id: str
    session_path: Path
    output_dir: Path
    output_file: Path
    primary_output_file: Path
    per_take_output_dir: Path
    per_take_output_files: dict[str, Path]
    render_aggregate: bool
    render_per_take: bool
    report_file: Path
    commands_file: Path
    logs_dir: Path
    work_dir: Path
    speed: float
    fps: int
    width: int
    height: int
    takes: tuple[TakePlan, ...]
    skipped_takes: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


DurationProbe = Callable[[Path, CameraClip | None], float | None]
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def derive_stringout(
    options: StringoutOptions,
    *,
    duration_probe: DurationProbe | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    started_at = _utc_now()
    plan = build_stringout_plan(
        options,
        probe_missing_durations=not options.dry_run,
        duration_probe=duration_probe,
        check_external_tools=command_runner is None,
    )
    report = build_report(plan=plan, options=options, started_at=started_at, finished_at=None, status="planned")

    if options.dry_run:
        report["finished_at"] = _utc_now()
        report["overall_status"] = _status_from_plan(plan)
        return report

    if plan.errors:
        report["finished_at"] = _utc_now()
        report["overall_status"] = "failed"
        _write_report(plan, report)
        return report

    runner = command_runner or _default_command_runner
    render_errors: list[str] = []
    render_warnings: list[str] = []
    rendered_sections: list[Path] = []
    rendered_sections_by_take: dict[str, list[Path]] = {}
    executed_commands: list[list[str]] = []

    plan.output_dir.mkdir(parents=True, exist_ok=True)
    if plan.render_per_take:
        plan.per_take_output_dir.mkdir(parents=True, exist_ok=True)
    plan.logs_dir.mkdir(parents=True, exist_ok=True)
    plan.work_dir.mkdir(parents=True, exist_ok=True)

    for take in plan.takes:
        if take.skipped_reason:
            continue
        if not take.clips or not take.common_duration_seconds:
            continue

        if not options.no_slate:
            slate_path = plan.work_dir / f"{take.take_id}_slate.mp4"
            slate_command = build_slate_command(
                output_path=slate_path,
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                take=take,
                session_id=plan.session_id,
                speed=plan.speed,
                with_drawtext=True,
            )
            executed_commands.append(slate_command)
            slate_result = _run_logged(runner, slate_command, plan.logs_dir / f"{take.take_id}_slate.stderr.log")
            if slate_result.returncode != 0:
                render_warnings.append(f"{take.take_id}: slate drawtext failed; retrying with plain black slate")
                fallback_command = build_slate_command(
                    output_path=slate_path,
                    width=plan.width,
                    height=plan.height,
                    fps=plan.fps,
                    take=take,
                    session_id=plan.session_id,
                    speed=plan.speed,
                    with_drawtext=False,
                )
                executed_commands.append(fallback_command)
                fallback_result = _run_logged(
                    runner,
                    fallback_command,
                    plan.logs_dir / f"{take.take_id}_slate_fallback.stderr.log",
                )
                if fallback_result.returncode != 0:
                    render_errors.append(
                        f"{take.take_id}: failed to render slate; see {plan.logs_dir / f'{take.take_id}_slate_fallback.stderr.log'}"
                    )
                    break
            rendered_sections.append(slate_path)
            rendered_sections_by_take.setdefault(take.take_id, []).append(slate_path)

        take_path = plan.work_dir / f"{take.take_id}_grid.mp4"
        take_command = build_take_grid_command(
            take=take,
            output_path=take_path,
            width=plan.width,
            height=plan.height,
            fps=plan.fps,
            speed=plan.speed,
            with_labels=not options.no_labels,
        )
        executed_commands.append(take_command)
        take_result = _run_logged(runner, take_command, plan.logs_dir / f"{take.take_id}_grid.stderr.log")
        if take_result.returncode != 0 and not options.no_labels:
            render_warnings.append(f"{take.take_id}: label drawtext failed; retrying grid without labels")
            fallback_command = build_take_grid_command(
                take=take,
                output_path=take_path,
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                speed=plan.speed,
                with_labels=False,
            )
            executed_commands.append(fallback_command)
            take_result = _run_logged(runner, fallback_command, plan.logs_dir / f"{take.take_id}_grid_no_labels.stderr.log")
        if take_result.returncode != 0:
            render_errors.append(f"{take.take_id}: failed to render grid; see {plan.logs_dir / f'{take.take_id}_grid.stderr.log'}")
            break
        if _empty_media_output(take_path):
            render_errors.append(
                f"{take.take_id}: grid render produced no video frames; see {plan.logs_dir / f'{take.take_id}_grid.stderr.log'}"
            )
            break
        rendered_sections.append(take_path)
        rendered_sections_by_take.setdefault(take.take_id, []).append(take_path)

    if not render_errors and plan.render_per_take:
        for take_id, take_sections in rendered_sections_by_take.items():
            take_output_file = plan.per_take_output_files.get(take_id)
            if not take_output_file:
                continue
            take_concat_list = plan.work_dir / f"{take_id}_concat.txt"
            _write_concat_list(take_concat_list, take_sections)
            take_concat_command = build_concat_command(concat_list=take_concat_list, output_path=take_output_file)
            executed_commands.append(take_concat_command)
            take_concat_result = _run_logged(runner, take_concat_command, plan.logs_dir / f"{take_id}_concat.stderr.log")
            if take_concat_result.returncode != 0:
                render_errors.append(f"{take_id}: concat failed; see {plan.logs_dir / f'{take_id}_concat.stderr.log'}")
                break
            if _empty_media_output(take_output_file):
                render_errors.append(f"{take_id}: per-take stringout produced no video frames")
                break

    if not render_errors and plan.render_aggregate and rendered_sections:
        concat_list = plan.work_dir / "concat.txt"
        _write_concat_list(concat_list, rendered_sections)
        concat_command = build_concat_command(concat_list=concat_list, output_path=plan.output_file)
        executed_commands.append(concat_command)
        concat_result = _run_logged(runner, concat_command, plan.logs_dir / "concat.stderr.log")
        if concat_result.returncode != 0:
            render_errors.append(f"concat failed; see {plan.logs_dir / 'concat.stderr.log'}")
        elif _empty_media_output(plan.output_file):
            render_errors.append("aggregate stringout produced no video frames")
    elif not rendered_sections and not render_errors:
        render_errors.append("no take sections were rendered")

    final_report = build_report(
        plan=plan,
        options=options,
        started_at=started_at,
        finished_at=_utc_now(),
        status="failed" if render_errors else _status_from_plan(plan),
        extra_warnings=render_warnings,
        extra_errors=render_errors,
        executed_commands=executed_commands,
    )
    _write_report(plan, final_report)
    return final_report


def build_stringout_plan(
    options: StringoutOptions,
    *,
    probe_missing_durations: bool = True,
    duration_probe: DurationProbe | None = None,
    check_external_tools: bool = True,
) -> StringoutPlan:
    session_path = options.session_path.expanduser().resolve()
    output_dir = (options.output_dir.expanduser().resolve() if options.output_dir else session_path / DEFAULT_REVIEW_SUBDIR)
    width, height = parse_resolution(options.resolution)
    output_file = output_dir / output_name(options.speed)
    per_take_output_dir = output_dir / "takes"
    render_per_take = not options.no_per_take
    render_aggregate = not (options.take_id and render_per_take)
    report_file = output_dir / "multicam_stringout_report.json"
    commands_file = output_dir / "ffmpeg_commands.txt"
    logs_dir = output_dir / "logs"
    work_dir = output_dir / "work"

    warnings: list[str] = []
    errors: list[str] = []
    skipped_takes: list[dict[str, str]] = []

    index_path = session_path / "session_index.json"
    if not index_path.exists():
        return StringoutPlan(
            session_id=session_path.name,
            session_path=session_path,
            output_dir=output_dir,
            output_file=output_file,
            primary_output_file=output_file,
            per_take_output_dir=per_take_output_dir,
            per_take_output_files={},
            render_aggregate=render_aggregate,
            render_per_take=render_per_take,
            report_file=report_file,
            commands_file=commands_file,
            logs_dir=logs_dir,
            work_dir=work_dir,
            speed=options.speed,
            fps=options.fps,
            width=width,
            height=height,
            takes=(),
            skipped_takes=(),
            warnings=(),
            errors=(f"session_index.json is missing: {index_path}",),
        )

    index = _read_json(index_path)
    session_id = str(index.get("session_id") or session_path.name)
    if options.speed <= 0:
        errors.append("speed must be greater than zero")
    if options.fps <= 0:
        errors.append("fps must be greater than zero")
    if check_external_tools and not options.dry_run and shutil.which("ffmpeg") is None:
        errors.append("ffmpeg was not found on PATH")

    nodes = index.get("nodes") if isinstance(index.get("nodes"), list) else []
    if not nodes:
        errors.append("session_index.json contains no nodes")

    requested_cameras = set(options.include_cameras)
    expected_cameras = tuple(
        _camera_id(node)
        for node in nodes
        if isinstance(node, dict) and (not requested_cameras or _camera_id(node) in requested_cameras)
    )
    if requested_cameras:
        indexed_cameras = {_camera_id(node) for node in nodes if isinstance(node, dict)}
        for camera_id in sorted(requested_cameras - indexed_cameras):
            warnings.append(f"requested camera {camera_id} is not present in session_index.json")
    if not expected_cameras:
        errors.append("no cameras selected")

    take_ids = _selected_take_ids(index, nodes, options.take_id)
    if options.take_id and options.take_id not in take_ids:
        skipped_takes.append({"take_id": options.take_id, "reason": "take not present in session_index.json"})
        errors.append(f"requested take is not present in session_index.json: {options.take_id}")
    if not take_ids and not options.take_id:
        errors.append("no takes found in session_index.json")

    ffprobe_missing = (
        check_external_tools
        and probe_missing_durations
        and duration_probe is None
        and shutil.which("ffprobe") is None
    )
    probe = duration_probe or ffprobe_duration
    takes: list[TakePlan] = []
    for take_id in take_ids:
        take_plan = _build_take_plan(
            session_path=session_path,
            nodes=nodes,
            take_id=take_id,
            expected_cameras=expected_cameras,
            speed=options.speed,
            probe_missing_durations=probe_missing_durations and not ffprobe_missing,
            duration_probe=probe,
            warnings=warnings,
        )
        if take_plan.skipped_reason:
            skipped_takes.append({"take_id": take_id, "reason": take_plan.skipped_reason})
        takes.append(take_plan)

    per_take_output_files = (
        {
            take.take_id: per_take_output_dir / take_output_name(take.take_id, options.speed)
            for take in takes
            if take.clips and not take.skipped_reason
        }
        if render_per_take
        else {}
    )
    primary_output_file = output_file
    if not render_aggregate and render_per_take and options.take_id and options.take_id in per_take_output_files:
        primary_output_file = per_take_output_files[options.take_id]

    if render_aggregate and output_file.exists() and not options.overwrite:
        errors.append(f"output already exists; pass --overwrite to replace it: {output_file}")
    if render_per_take and not options.overwrite:
        for take_id, take_output_file in sorted(per_take_output_files.items()):
            if take_output_file.exists():
                errors.append(f"{take_id}: output already exists; pass --overwrite to replace it: {take_output_file}")

    if not any(take.clips and not take.skipped_reason for take in takes):
        errors.append("no usable recording files were found")

    duration_probe_would_be_needed = _plan_needs_probe(takes) or any("unknown duration" in warning for warning in warnings)
    if ffprobe_missing and duration_probe_would_be_needed:
        errors.append("ffprobe was not found on PATH and at least one recording needs duration probing")

    return StringoutPlan(
        session_id=session_id,
        session_path=session_path,
        output_dir=output_dir,
        output_file=output_file,
        primary_output_file=primary_output_file,
        per_take_output_dir=per_take_output_dir,
        per_take_output_files=per_take_output_files,
        render_aggregate=render_aggregate,
        render_per_take=render_per_take,
        report_file=report_file,
        commands_file=commands_file,
        logs_dir=logs_dir,
        work_dir=work_dir,
        speed=options.speed,
        fps=options.fps,
        width=width,
        height=height,
        takes=tuple(takes),
        skipped_takes=tuple(skipped_takes),
        warnings=tuple(_dedupe(warnings)),
        errors=tuple(_dedupe(errors)),
    )


def build_take_grid_command(
    *,
    take: TakePlan,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    speed: float,
    with_labels: bool = True,
) -> list[str]:
    if not take.common_duration_seconds:
        raise ValueError(f"{take.take_id}: common duration is required")
    command = ["ffmpeg", "-y", "-hide_banner"]
    for clip in take.clips:
        command.extend(["-fflags", "+genpts"])
        if clip.source_fps:
            command.extend(["-r", _format_number(clip.source_fps)])
        command.extend(["-i", str(clip.recording_path)])

    filter_complex = _grid_filter(
        take.clips,
        common_duration_seconds=take.common_duration_seconds,
        width=width,
        height=height,
        fps=fps,
        speed=speed,
        with_labels=with_labels,
    )
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[grid]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def build_slate_command(
    *,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    take: TakePlan,
    session_id: str,
    speed: float,
    with_drawtext: bool = True,
) -> list[str]:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={_format_number(SLATE_SECONDS)}",
    ]
    if with_drawtext:
        command.extend(["-vf", _slate_filter(session_id=session_id, take=take, speed=speed)])
    command.extend(
        [
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def build_concat_command(*, concat_list: Path, output_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(output_path),
    ]


def build_report(
    *,
    plan: StringoutPlan,
    options: StringoutOptions,
    started_at: str,
    finished_at: str | None,
    status: str,
    extra_warnings: list[str] | None = None,
    extra_errors: list[str] | None = None,
    executed_commands: list[list[str]] | None = None,
) -> dict[str, Any]:
    planned_commands = planned_ffmpeg_commands(plan, no_slate=options.no_slate, no_labels=options.no_labels)
    commands = executed_commands if executed_commands is not None else planned_commands
    warnings = _dedupe([*plan.warnings, *(extra_warnings or [])])
    errors = _dedupe([*plan.errors, *(extra_errors or [])])
    missing_files: list[dict[str, str]] = []
    included_takes: list[str] = []
    included_cameras_per_take: dict[str, list[str]] = {}
    trim_values: dict[str, dict[str, float]] = {}
    durations: dict[str, dict[str, Any]] = {}

    for take in plan.takes:
        if take.clips and not take.skipped_reason:
            included_takes.append(take.take_id)
        included_cameras_per_take[take.take_id] = [clip.camera_id for clip in take.clips]
        trim_values[take.take_id] = {clip.camera_id: clip.usable_start_offset_seconds for clip in take.clips}
        durations[take.take_id] = {
            "common_duration_seconds": take.common_duration_seconds,
            "output_duration_seconds": take.output_duration_seconds,
            "cameras": {
                clip.camera_id: {
                    "source_duration_seconds": clip.source_duration_seconds,
                    "usable_duration_seconds": clip.usable_duration_seconds,
                }
                for clip in take.clips
            },
        }
        for camera_id in take.missing_cameras:
            missing_files.append({"take_id": take.take_id, "camera_id": camera_id, "reason": "missing camera/take/recording"})

    return {
        "session_id": plan.session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input_session_path": str(plan.session_path),
        "output_file_path": str(plan.primary_output_file),
        "aggregate_output_file_path": str(plan.output_file) if plan.render_aggregate else None,
        "per_take_output_dir": str(plan.per_take_output_dir) if plan.render_per_take else None,
        "per_take_output_file_paths": {
            take_id: str(path) for take_id, path in sorted(plan.per_take_output_files.items())
        },
        "render_aggregate": plan.render_aggregate,
        "render_per_take": plan.render_per_take,
        "report_file_path": str(plan.report_file),
        "ffmpeg_commands_file_path": str(plan.commands_file),
        "dry_run": options.dry_run,
        "speed_factor": plan.speed,
        "fps": plan.fps,
        "resolution": f"{plan.width}x{plan.height}",
        "included_takes": included_takes,
        "included_cameras_per_take": included_cameras_per_take,
        "skipped_takes": list(plan.skipped_takes),
        "missing_cameras_files": missing_files,
        "trim_pre_roll_values_used": trim_values,
        "durations_used": durations,
        "take_sections": [_take_report(take) for take in plan.takes],
        "warnings": warnings,
        "errors": errors,
        "ffmpeg_commands": [_quote_command(command) for command in commands],
        "overall_status": "failed" if errors and status != "planned" else status,
    }


def planned_ffmpeg_commands(plan: StringoutPlan, *, no_slate: bool, no_labels: bool) -> list[list[str]]:
    commands: list[list[str]] = []
    sections: list[Path] = []
    sections_by_take: dict[str, list[Path]] = {}
    for take in plan.takes:
        if take.skipped_reason or not take.clips or not take.common_duration_seconds:
            continue
        if not no_slate:
            slate_path = plan.work_dir / f"{take.take_id}_slate.mp4"
            commands.append(
                build_slate_command(
                    output_path=slate_path,
                    width=plan.width,
                    height=plan.height,
                    fps=plan.fps,
                    take=take,
                    session_id=plan.session_id,
                    speed=plan.speed,
                    with_drawtext=True,
                )
            )
            sections.append(slate_path)
            sections_by_take.setdefault(take.take_id, []).append(slate_path)
        grid_path = plan.work_dir / f"{take.take_id}_grid.mp4"
        commands.append(
            build_take_grid_command(
                take=take,
                output_path=grid_path,
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                speed=plan.speed,
                with_labels=not no_labels,
            )
        )
        sections.append(grid_path)
        sections_by_take.setdefault(take.take_id, []).append(grid_path)
    if plan.render_per_take:
        for take_id, take_sections in sections_by_take.items():
            take_output_file = plan.per_take_output_files.get(take_id)
            if take_output_file:
                commands.append(
                    build_concat_command(
                        concat_list=plan.work_dir / f"{take_id}_concat.txt",
                        output_path=take_output_file,
                    )
                )
    if plan.render_aggregate and sections:
        commands.append(build_concat_command(concat_list=plan.work_dir / "concat.txt", output_path=plan.output_file))
    return commands


def ffprobe_duration(path: Path, _clip: CameraClip | None = None) -> float | None:
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


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except Exception as exc:
        raise ValueError(f"resolution must be WIDTHxHEIGHT, got {value!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"resolution must be positive, got {value!r}")
    return width, height


def output_name(speed: float) -> str:
    if speed == 1:
        return "multicam_stringout_realtime.mp4"
    if speed == DEFAULT_SPEED:
        return "multicam_stringout.mp4"
    speed_text = _format_number(speed).replace(".", "p")
    return f"multicam_stringout_{speed_text}x.mp4"


def take_output_name(take_id: str, speed: float) -> str:
    safe_take_id = take_id.replace("/", "_").replace("\\", "_")
    if speed == 1:
        return f"{safe_take_id}_multicam_stringout_realtime.mp4"
    if speed == DEFAULT_SPEED:
        return f"{safe_take_id}_multicam_stringout.mp4"
    speed_text = _format_number(speed).replace(".", "p")
    return f"{safe_take_id}_multicam_stringout_{speed_text}x.mp4"


def _build_take_plan(
    *,
    session_path: Path,
    nodes: list[Any],
    take_id: str,
    expected_cameras: tuple[str, ...],
    speed: float,
    probe_missing_durations: bool,
    duration_probe: DurationProbe,
    warnings: list[str],
) -> TakePlan:
    clips: list[CameraClip] = []
    take_warnings: list[str] = []
    found_cameras: set[str] = set()
    profiles: list[str] = []

    for node in nodes:
        if not isinstance(node, dict):
            continue
        camera_id = _camera_id(node)
        if camera_id not in expected_cameras:
            continue
        node_take = _find_node_take(node, take_id)
        if node_take is None:
            take_warnings.append(f"{take_id}: camera {camera_id} is missing this take")
            continue
        clip = _clip_from_take(session_path, node, node_take, take_id)
        if clip is None:
            take_warnings.append(f"{take_id}: camera {camera_id} has no usable recording file")
            continue
        if clip.usable_duration_seconds is None and probe_missing_durations:
            probed_duration = duration_probe(clip.recording_path, clip)
            if probed_duration is not None:
                clip = _replace_clip_duration(clip, probed_duration)
            else:
                take_warnings.append(f"{take_id}: camera {camera_id} duration is unavailable")
        clips.append(clip)
        found_cameras.add(camera_id)
        if clip.profile:
            profiles.append(clip.profile)
        take_warnings.extend(clip.warnings)

    missing_cameras = tuple(camera_id for camera_id in expected_cameras if camera_id not in found_cameras)
    usable_durations = [clip.usable_duration_seconds for clip in clips if clip.usable_duration_seconds is not None]
    if not clips:
        warnings.extend(take_warnings)
        return TakePlan(
            take_id=take_id,
            clips=(),
            expected_cameras=expected_cameras,
            missing_cameras=missing_cameras,
            common_duration_seconds=None,
            output_duration_seconds=None,
            profile=None,
            warnings=tuple(_dedupe(take_warnings)),
            skipped_reason="no usable recording files",
        )
    if len(usable_durations) != len(clips):
        take_warnings.append(f"{take_id}: skipped cameras with unknown duration would be required for a deterministic grid")
        clips = [clip for clip in clips if clip.usable_duration_seconds is not None]
    if not clips:
        warnings.extend(take_warnings)
        return TakePlan(
            take_id=take_id,
            clips=(),
            expected_cameras=expected_cameras,
            missing_cameras=missing_cameras,
            common_duration_seconds=None,
            output_duration_seconds=None,
            profile=None,
            warnings=tuple(_dedupe(take_warnings)),
            skipped_reason="no recordings with known usable duration",
        )

    durations = [float(clip.usable_duration_seconds) for clip in clips if clip.usable_duration_seconds is not None]
    common_duration = min(durations)
    if common_duration <= 0:
        take_warnings.append(f"{take_id}: usable duration is zero after pre-roll trimming")
        warnings.extend(take_warnings)
        return TakePlan(
            take_id=take_id,
            clips=tuple(clips),
            expected_cameras=expected_cameras,
            missing_cameras=missing_cameras,
            common_duration_seconds=None,
            output_duration_seconds=None,
            profile=_common_profile(profiles),
            warnings=tuple(_dedupe(take_warnings)),
            skipped_reason="usable duration is zero",
        )
    if max(durations) - min(durations) > SIGNIFICANT_DURATION_DIFFERENCE_SECONDS:
        take_warnings.append(
            f"{take_id}: camera usable durations differ by more than {SIGNIFICANT_DURATION_DIFFERENCE_SECONDS:g}s; using shortest"
        )
    if missing_cameras:
        take_warnings.append(f"{take_id}: missing cameras: {', '.join(missing_cameras)}")
    if len(clips) > 4:
        take_warnings.append(f"{take_id}: only the first 4 cameras are supported in the MVP grid")
        clips = clips[:4]

    warnings.extend(take_warnings)
    return TakePlan(
        take_id=take_id,
        clips=tuple(clips),
        expected_cameras=expected_cameras,
        missing_cameras=missing_cameras,
        common_duration_seconds=common_duration,
        output_duration_seconds=common_duration / max(0.000001, speed),
        profile=_common_profile(profiles),
        warnings=tuple(_dedupe(take_warnings)),
    )


def _clip_from_take(session_path: Path, node: dict[str, Any], node_take: dict[str, Any], take_id: str) -> CameraClip | None:
    camera_id = _camera_id(node)
    node_name = str(node.get("node_name") or camera_id)
    recording_rel = node_take.get("recording_relative_path")
    if not isinstance(recording_rel, str) or not recording_rel:
        return None
    recording_path = session_path / recording_rel
    warnings: list[str] = []
    if not recording_path.exists():
        warnings.append(f"{take_id}: camera {camera_id} recording is missing: {recording_rel}")
        return None
    if recording_path.stat().st_size <= 0:
        warnings.append(f"{take_id}: camera {camera_id} recording is empty: {recording_rel}")
        return None

    manifest_rel = node_take.get("manifest_relative_path")
    manifest_path = session_path / manifest_rel if isinstance(manifest_rel, str) and manifest_rel else None
    manifest = _read_json(manifest_path) if manifest_path and manifest_path.exists() else None
    if manifest_path is None or not manifest_path.exists():
        warnings.append(f"{take_id}: camera {camera_id} manifest is missing")

    manifest_summary = node_take.get("manifest_summary") if isinstance(node_take.get("manifest_summary"), dict) else {}
    pre_roll = _number_or_none(node_take.get("usable_start_offset_seconds"))
    if pre_roll is None:
        pre_roll = _number_or_none(manifest_summary.get("usable_start_offset_seconds"))
    if pre_roll is None and isinstance(manifest, dict):
        pre_roll = _number_or_none(manifest.get("usable_start_offset_seconds"))
    if pre_roll is None:
        pre_roll = _number_or_none(node_take.get("pre_roll_seconds"))
    if pre_roll is None:
        pre_roll = _number_or_none(manifest_summary.get("pre_roll_seconds"))
    if pre_roll is None and isinstance(manifest, dict):
        pre_roll = _number_or_none(manifest.get("pre_roll_seconds"))
    if pre_roll is None:
        pre_roll = 0.0
        warnings.append(f"{take_id}: camera {camera_id} has no pre-roll metadata; using 0s")

    source_duration = _duration_from_metadata(node_take, manifest_summary, manifest)
    usable_duration = None if source_duration is None else max(0.0, source_duration - pre_roll)
    profile = _string_or_none(node_take.get("profile")) or _string_or_none(manifest_summary.get("profile"))
    source_fps = _source_fps(manifest_summary, manifest)

    return CameraClip(
        camera_id=camera_id,
        node_name=node_name,
        take_id=take_id,
        recording_path=recording_path,
        manifest_path=manifest_path,
        recording_relative_path=recording_rel,
        manifest_relative_path=manifest_rel if isinstance(manifest_rel, str) else None,
        profile=profile,
        source_fps=source_fps,
        usable_start_offset_seconds=max(0.0, pre_roll),
        source_duration_seconds=source_duration,
        usable_duration_seconds=usable_duration,
        warnings=tuple(warnings),
    )


def _replace_clip_duration(clip: CameraClip, duration: float) -> CameraClip:
    return CameraClip(
        camera_id=clip.camera_id,
        node_name=clip.node_name,
        take_id=clip.take_id,
        recording_path=clip.recording_path,
        manifest_path=clip.manifest_path,
        recording_relative_path=clip.recording_relative_path,
        manifest_relative_path=clip.manifest_relative_path,
        profile=clip.profile,
        source_fps=clip.source_fps,
        usable_start_offset_seconds=clip.usable_start_offset_seconds,
        source_duration_seconds=duration,
        usable_duration_seconds=max(0.0, duration - clip.usable_start_offset_seconds),
        warnings=clip.warnings,
    )


def _duration_from_metadata(node_take: dict[str, Any], manifest_summary: dict[str, Any], manifest: Any) -> float | None:
    for container in (node_take, manifest_summary, manifest if isinstance(manifest, dict) else {}):
        duration = _number_or_none(container.get("duration_seconds"))
        if duration is not None and duration > 0:
            return duration
    start = _string_or_none(node_take.get("recording_start_time")) or _string_or_none(manifest_summary.get("recording_start_time"))
    stop = _string_or_none(node_take.get("recording_stop_time")) or _string_or_none(manifest_summary.get("recording_stop_time"))
    if isinstance(manifest, dict):
        start = start or _string_or_none(manifest.get("recording_start_time"))
        stop = stop or _string_or_none(manifest.get("recording_stop_time"))
    if start and stop:
        start_dt = _parse_datetime(start)
        stop_dt = _parse_datetime(stop)
        if start_dt and stop_dt:
            seconds = (stop_dt - start_dt).total_seconds()
            if seconds > 0:
                return seconds
    return None


def _source_fps(manifest_summary: dict[str, Any], manifest: Any) -> float | None:
    candidates: list[Any] = [manifest_summary.get("framerate"), manifest_summary.get("fps")]
    if isinstance(manifest, dict):
        candidates.extend(
            [
                manifest.get("framerate"),
                manifest.get("fps"),
                _nested_get(manifest, "actually_applied_controls", "framerate"),
                _nested_get(manifest, "actual_applied_controls", "framerate"),
                _nested_get(manifest, "applied_controls", "framerate"),
                _nested_get(manifest, "profile_settings", "recording", "framerate"),
                _nested_get(manifest, "profile_snapshot", "recording", "framerate"),
            ]
        )
    for candidate in candidates:
        value = _number_or_none(candidate)
        if value and value > 0:
            return value
    return None


def _grid_filter(
    clips: tuple[CameraClip, ...],
    *,
    common_duration_seconds: float,
    width: int,
    height: int,
    fps: int,
    speed: float,
    with_labels: bool,
) -> str:
    cell_sizes = _cell_sizes(len(clips), width, height)
    parts: list[str] = []
    for idx, clip in enumerate(clips):
        cell_width, cell_height = cell_sizes[idx]
        filters = [
            f"[{idx}:v]setpts=PTS-STARTPTS",
            (
                f"trim=start={_format_number(clip.usable_start_offset_seconds)}:"
                f"duration={_format_number(common_duration_seconds)}"
            ),
            f"setpts=(PTS-STARTPTS)/{_format_number(speed)}",
            f"fps={fps}",
            f"scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease",
            f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2",
            "setsar=1",
        ]
        if with_labels:
            filters.append(_drawtext_filter(clip.camera_id, x="24", y="24", fontsize=max(24, cell_height // 18)))
        parts.append(",".join(filters) + f"[v{idx}]")
    if len(clips) == 1:
        parts.append("[v0]copy[grid_base]")
    elif len(clips) == 2:
        parts.append("".join(f"[v{idx}]" for idx in range(len(clips))) + f"hstack=inputs={len(clips)}[grid_base]")
    elif len(clips) == 3:
        empty_width, empty_height = cell_sizes[2]
        empty_duration = common_duration_seconds / max(0.000001, speed)
        parts.append(
            f"color=c=black:s={empty_width}x{empty_height}:r={fps}:d={_format_number(empty_duration)}[empty]"
        )
        parts.append("[v0][v1]hstack=inputs=2[row0]")
        parts.append("[v2][empty]hstack=inputs=2[row1]")
        parts.append("[row0][row1]vstack=inputs=2[grid_base]")
    elif len(clips) == 4:
        parts.append("[v0][v1]hstack=inputs=2[row0]")
        parts.append("[v2][v3]hstack=inputs=2[row1]")
        parts.append("[row0][row1]vstack=inputs=2[grid_base]")
    else:
        raise ValueError("MVP grid supports 1 to 4 cameras")
    if with_labels:
        parts.append(
            "[grid_base]"
            + _take_timecode_filter(clips[0].take_id, speed=speed, fontsize=max(32, height // 28))
            + "[grid]"
        )
    else:
        parts.append("[grid_base]copy[grid]")
    return ";".join(parts)


def _cell_sizes(camera_count: int, width: int, height: int) -> list[tuple[int, int]]:
    if camera_count == 1:
        return [(width, height)]
    if camera_count == 2:
        return [(width // 2, height), (width - width // 2, height)]
    if camera_count == 3:
        left = width // 2
        top = height // 2
        return [(left, top), (width - left, top), (left, height - top)]
    if camera_count == 4:
        left = width // 2
        top = height // 2
        return [(left, top), (width - left, top), (left, height - top), (width - left, height - top)]
    raise ValueError("MVP grid supports 1 to 4 cameras")


def _slate_lines(*, session_id: str, take: TakePlan, speed: float) -> list[str]:
    cameras = ", ".join(clip.camera_id for clip in take.clips) or "none"
    warning_count = len(take.warnings)
    profile = take.profile or "mixed/unknown"
    return [
        f"Session: {session_id}",
        f"Take: {take.take_id}",
        f"Cameras: {cameras}",
        f"Speed: {_format_number(speed)}x",
        f"Profile: {profile}",
        f"Warnings: {warning_count}",
    ]


def _slate_filter(*, session_id: str, take: TakePlan, speed: float) -> str:
    line_height = 58
    filters = [
        _drawtext_filter(line, x="80", y=str(80 + idx * line_height), fontsize=40)
        for idx, line in enumerate(_slate_lines(session_id=session_id, take=take, speed=speed))
    ]
    return ",".join(filters)


def _drawtext_filter(text: str, *, x: str, y: str, fontsize: int) -> str:
    escaped = _escape_drawtext(text)
    return f"drawtext=text='{escaped}':x={x}:y={y}:fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.55:line_spacing=12"


def _take_timecode_filter(take_id: str, *, speed: float, fontsize: int) -> str:
    escaped_take = _escape_drawtext(take_id)
    speed_text = _format_number(speed)
    # t is output time after speed-up. Multiplying by speed shows usable source-take time for review notes.
    minutes = f"%{{eif\\:floor(t*{speed_text}/60)\\:d\\:2}}"
    seconds = f"%{{eif\\:mod(t*{speed_text}\\,60)\\:d\\:2}}"
    text = f"{escaped_take} {minutes}\\:{seconds}"
    return (
        f"drawtext=text='{text}':x=(w-text_w)/2:y=(h-text_h)/2:fontsize={fontsize}:"
        "fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12"
    )


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


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


def _find_node_take(node: dict[str, Any], take_id: str) -> dict[str, Any] | None:
    takes = node.get("takes") if isinstance(node.get("takes"), list) else []
    for take in takes:
        if isinstance(take, dict) and take.get("take_id") == take_id:
            return take
    return None


def _camera_id(node: dict[str, Any]) -> str:
    return str(node.get("camera_id") or node.get("folder") or node.get("node_name") or "unknown")


def _common_profile(profiles: list[str]) -> str | None:
    unique = sorted(set(profiles))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return "mixed"


def _plan_needs_probe(takes: list[TakePlan]) -> bool:
    for take in takes:
        for clip in take.clips:
            if clip.source_duration_seconds is None:
                return True
    return False


def _status_from_plan(plan: StringoutPlan) -> str:
    if plan.errors:
        return "failed"
    if plan.skipped_takes or any(take.missing_cameras for take in plan.takes):
        return "partial"
    return "complete"


def _take_report(take: TakePlan) -> dict[str, Any]:
    return {
        "take_id": take.take_id,
        "status": "skipped" if take.skipped_reason else "included",
        "skipped_reason": take.skipped_reason,
        "expected_cameras": list(take.expected_cameras),
        "included_cameras": [clip.camera_id for clip in take.clips],
        "missing_cameras": list(take.missing_cameras),
        "common_duration_seconds": take.common_duration_seconds,
        "output_duration_seconds": take.output_duration_seconds,
        "profile": take.profile,
        "warnings": list(take.warnings),
        "clips": [
            {
                "camera_id": clip.camera_id,
                "node_name": clip.node_name,
                "recording_path": str(clip.recording_path),
                "manifest_path": str(clip.manifest_path) if clip.manifest_path else None,
                "usable_start_offset_seconds": clip.usable_start_offset_seconds,
                "source_duration_seconds": clip.source_duration_seconds,
                "usable_duration_seconds": clip.usable_duration_seconds,
                "source_fps": clip.source_fps,
                "profile": clip.profile,
                "warnings": list(clip.warnings),
            }
            for clip in take.clips
        ],
    }


def _write_report(plan: StringoutPlan, report: dict[str, Any]) -> None:
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    plan.report_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    commands = report.get("ffmpeg_commands") if isinstance(report.get("ffmpeg_commands"), list) else []
    plan.commands_file.write_text("\n".join(str(command) for command in commands) + ("\n" if commands else ""), encoding="utf-8")


def _write_concat_list(path: Path, sections: list[Path]) -> None:
    lines = [f"file '{str(section).replace(chr(39), chr(39) + '\\\\' + chr(39) + chr(39))}'" for section in sections]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_logged(runner: CommandRunner, command: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    result = runner(command)
    log_path.write_text(
        "COMMAND\n" + _quote_command(command) + "\n\nSTDOUT\n" + (result.stdout or "") + "\n\nSTDERR\n" + (result.stderr or ""),
        encoding="utf-8",
    )
    return result


def _empty_media_output(path: Path) -> bool:
    return not path.exists() or path.stat().st_size < 1024


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
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


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
