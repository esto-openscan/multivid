from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_multicam_coordinator.stringout import (
    StringoutOptions,
    build_slate_command,
    build_stringout_plan,
    build_take_grid_command,
    derive_stringout,
    output_name,
    take_output_name,
)


class StringoutTests(unittest.TestCase):
    def test_plan_parses_session_index_and_uses_shortest_usable_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={
                    "front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:20Z", pre_roll=5)],
                    "side": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:22Z", pre_roll=2)],
                    "top": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:21Z", pre_roll=4)],
                },
            )

            plan = build_stringout_plan(StringoutOptions(session_path=session_path, dry_run=True))

        self.assertEqual(plan.session_id, "session-1")
        self.assertEqual(len(plan.takes), 1)
        take = plan.takes[0]
        self.assertEqual([clip.camera_id for clip in take.clips], ["front", "side", "top"])
        self.assertEqual(take.common_duration_seconds, 15.0)
        self.assertEqual(take.output_duration_seconds, 3.0)
        self.assertIn("using shortest", "\n".join(plan.warnings))

    def test_plan_uses_ffprobe_callback_when_timestamp_duration_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start=None, stop=None, pre_roll=2)]},
            )

            plan = build_stringout_plan(
                StringoutOptions(session_path=session_path, dry_run=False),
                duration_probe=lambda _path, _clip: 12.0,
            )

        take = plan.takes[0]
        self.assertEqual(take.common_duration_seconds, 10.0)
        self.assertEqual(take.clips[0].source_duration_seconds, 12.0)

    def test_missing_camera_is_partial_but_available_cameras_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={
                    "front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")],
                    "side": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")],
                    "top": [],
                },
            )

            report = derive_stringout(StringoutOptions(session_path=session_path, dry_run=True))

        self.assertEqual(report["overall_status"], "partial")
        self.assertEqual(report["included_cameras_per_take"]["take_001"], ["front", "side"])
        self.assertEqual(report["take_sections"][0]["missing_cameras"], ["top"])

    def test_dry_run_writes_no_report_or_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )
            report = derive_stringout(StringoutOptions(session_path=session_path, dry_run=True))

            self.assertFalse((session_path / "derivatives" / "review" / "multicam_stringout.mp4").exists())
            self.assertFalse((session_path / "derivatives" / "review" / "takes" / "take_001_multicam_stringout.mp4").exists())
            self.assertFalse((session_path / "derivatives" / "review" / "multicam_stringout_report.json").exists())

        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(len(report["ffmpeg_commands"]), 4)
        self.assertTrue(report["render_aggregate"])
        self.assertTrue(report["render_per_take"])
        self.assertIn("take_001", report["per_take_output_file_paths"])

    def test_render_with_mocked_ffmpeg_writes_report_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )
            calls: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                Path(command[-1]).write_bytes(b"fake rendered media" * 128)
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            report = derive_stringout(StringoutOptions(session_path=session_path), command_runner=runner)

            report_path = session_path / "derivatives" / "review" / "multicam_stringout_report.json"
            commands_path = session_path / "derivatives" / "review" / "ffmpeg_commands.txt"
            saved_report = json.loads(report_path.read_text(encoding="utf-8"))
            commands_text = commands_path.read_text(encoding="utf-8")
            take_output_exists = (
                session_path / "derivatives" / "review" / "takes" / "take_001_multicam_stringout.mp4"
            ).exists()

        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(saved_report["overall_status"], "complete")
        self.assertEqual(len(calls), 4)
        self.assertIn("ffmpeg", commands_text)
        self.assertTrue(take_output_exists)

    def test_empty_grid_output_is_reported_as_failed_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                if "grid.mp4" in command[-1]:
                    Path(command[-1]).write_bytes(b"")
                else:
                    Path(command[-1]).write_bytes(b"fake rendered media" * 128)
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            report = derive_stringout(StringoutOptions(session_path=session_path), command_runner=runner)

        self.assertEqual(report["overall_status"], "failed")
        self.assertIn("produced no video frames", "\n".join(report["errors"]))

    def test_grid_command_for_three_cameras_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={
                    "front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")],
                    "side": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")],
                    "top": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")],
                },
            )
            take = build_stringout_plan(StringoutOptions(session_path=session_path, dry_run=True)).takes[0]

        command = build_take_grid_command(take=take, output_path=Path("out.mp4"), width=1920, height=1080, fps=30, speed=5)
        command_text = " ".join(command)
        self.assertIn("color=c=black:s=960x540", command_text)
        self.assertIn("[v0][v1]hstack=inputs=2[row0]", command_text)
        self.assertIn("[v2][empty]hstack=inputs=2[row1]", command_text)
        self.assertIn("[row0][row1]vstack=inputs=2[grid_base]", command_text)
        self.assertIn("drawtext=text='take_001", command_text)
        self.assertIn("x=(w-text_w)/2:y=(h-text_h)/2", command_text)
        self.assertIn("floor(t*5/60)", command_text)
        self.assertIn("mod(t*5\\,60)", command_text)
        self.assertNotIn("-ss", command)
        self.assertNotIn("-t", command)
        self.assertIn("trim=start=0:duration=10", command_text)
        self.assertIn("drawtext=text='front'", command_text)
        self.assertIn("-an", command)
        self.assertEqual(output_name(5), "multicam_stringout.mp4")
        self.assertEqual(output_name(1), "multicam_stringout_realtime.mp4")
        self.assertEqual(take_output_name("take_001", 5), "take_001_multicam_stringout.mp4")
        self.assertEqual(take_output_name("take_001", 1), "take_001_multicam_stringout_realtime.mp4")

    def test_slate_command_uses_separate_drawtext_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )
            take = build_stringout_plan(StringoutOptions(session_path=session_path, dry_run=True)).takes[0]

        command = build_slate_command(
            output_path=Path("slate.mp4"),
            width=1920,
            height=1080,
            fps=30,
            take=take,
            session_id="session-1",
            speed=5,
            with_drawtext=True,
        )
        vf = command[command.index("-vf") + 1]
        self.assertIn("drawtext=text='Session\\: session-1'", vf)
        self.assertIn("drawtext=text='Take\\: take_001'", vf)
        self.assertNotIn("\\n", vf)

    def test_existing_output_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )
            output = session_path / "derivatives" / "review" / "multicam_stringout.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"existing")

            report = derive_stringout(StringoutOptions(session_path=session_path, dry_run=True))

        self.assertEqual(report["overall_status"], "failed")
        self.assertIn("--overwrite", "\n".join(report["errors"]))

    def test_existing_per_take_output_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )
            output = session_path / "derivatives" / "review" / "takes" / "take_001_multicam_stringout.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"existing")

            report = derive_stringout(StringoutOptions(session_path=session_path, dry_run=True))

        self.assertEqual(report["overall_status"], "failed")
        self.assertIn("take_001", "\n".join(report["errors"]))
        self.assertIn("--overwrite", "\n".join(report["errors"]))

    def test_take_option_renders_only_per_take_output_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={
                    "front": [
                        _take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z"),
                        _take("take_002", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z"),
                    ]
                },
            )

            report = derive_stringout(StringoutOptions(session_path=session_path, take_id="take_002", dry_run=True))

        self.assertEqual(report["overall_status"], "complete")
        self.assertFalse(report["render_aggregate"])
        self.assertTrue(report["render_per_take"])
        self.assertEqual(report["included_takes"], ["take_002"])
        self.assertIn("take_002_multicam_stringout.mp4", report["output_file_path"])
        self.assertIn("take_002", report["per_take_output_file_paths"])
        self.assertEqual(len(report["ffmpeg_commands"]), 3)

    def test_no_per_take_keeps_aggregate_only_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start="2026-06-01T10:00:00Z", stop="2026-06-01T10:00:10Z")]},
            )

            report = derive_stringout(StringoutOptions(session_path=session_path, no_per_take=True, dry_run=True))

        self.assertEqual(report["overall_status"], "complete")
        self.assertTrue(report["render_aggregate"])
        self.assertFalse(report["render_per_take"])
        self.assertEqual(report["per_take_output_file_paths"], {})
        self.assertEqual(len(report["ffmpeg_commands"]), 3)

    def test_missing_ffprobe_is_reported_when_duration_metadata_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(
                Path(temp_dir),
                takes={"front": [_take("take_001", start=None, stop=None)]},
            )

            with patch("openscan_multicam_coordinator.stringout.shutil.which", return_value=None):
                plan = build_stringout_plan(StringoutOptions(session_path=session_path, dry_run=False))

        self.assertIn("ffprobe was not found", "\n".join(plan.errors))


def _write_session(root: Path, *, takes: dict[str, list[dict[str, object]]]) -> Path:
    session_path = root / "session-1"
    nodes: list[dict[str, object]] = []
    all_take_ids: set[str] = set()
    for camera_id, camera_takes in takes.items():
        node_takes: list[dict[str, object]] = []
        for take in camera_takes:
            take_id = str(take["take_id"])
            all_take_ids.add(take_id)
            take_dir = session_path / "nodes" / camera_id / take_id
            take_dir.mkdir(parents=True, exist_ok=True)
            (take_dir / "recording.h264").write_bytes(b"fake video bytes")
            manifest = {
                "take_id": take_id,
                "camera_id": camera_id,
                "profile": "video_1080p25_auto",
                "recording_start_time": take.get("recording_start_time"),
                "recording_stop_time": take.get("recording_stop_time"),
                "usable_start_offset_seconds": take.get("usable_start_offset_seconds"),
                "actually_applied_controls": {"framerate": 25},
            }
            (take_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            node_takes.append(
                {
                    "take_id": take_id,
                    "recording_relative_path": f"nodes/{camera_id}/{take_id}/recording.h264",
                    "manifest_relative_path": f"nodes/{camera_id}/{take_id}/manifest.json",
                    "recording_file_size": 16,
                    "recording_start_time": take.get("recording_start_time"),
                    "recording_stop_time": take.get("recording_stop_time"),
                    "usable_start_offset_seconds": take.get("usable_start_offset_seconds"),
                    "pre_roll_seconds": take.get("pre_roll_seconds"),
                    "profile": "video_1080p25_auto",
                    "manifest_summary": {
                        "camera_id": camera_id,
                        "profile": "video_1080p25_auto",
                        "recording_start_time": take.get("recording_start_time"),
                        "recording_stop_time": take.get("recording_stop_time"),
                        "usable_start_offset_seconds": take.get("usable_start_offset_seconds"),
                        "pre_roll_seconds": take.get("pre_roll_seconds"),
                    },
                }
            )
        nodes.append(
            {
                "node_name": f"cam-{camera_id}",
                "camera_id": camera_id,
                "folder": camera_id,
                "takes": node_takes,
            }
        )
    index = {
        "session_id": "session-1",
        "takes": sorted(all_take_ids),
        "nodes": nodes,
    }
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / "session_index.json").write_text(json.dumps(index), encoding="utf-8")
    return session_path


def _take(take_id: str, *, start: str | None, stop: str | None, pre_roll: float = 0) -> dict[str, object]:
    return {
        "take_id": take_id,
        "recording_start_time": start,
        "recording_stop_time": stop,
        "usable_start_offset_seconds": pre_roll,
        "pre_roll_seconds": pre_roll,
    }


if __name__ == "__main__":
    unittest.main()
