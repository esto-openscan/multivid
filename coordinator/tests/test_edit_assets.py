from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_multicam_coordinator.edit_assets import (
    EditAssetOptions,
    build_edit_asset_plan,
    build_master_command,
    build_proxy_command,
    prepare_edit_assets,
)


class EditAssetTests(unittest.TestCase):
    def test_plan_parses_session_index_and_generates_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front", "side"))

            plan = build_edit_asset_plan(
                EditAssetOptions(session_path=session_path, dry_run=True),
                duration_probe=lambda _path: 12.5,
            )

        self.assertEqual(plan.session_id, "session-1")
        self.assertEqual([asset.asset_id for asset in plan.recordings], ["front_take_001", "side_take_001"])
        self.assertEqual(plan.recordings[0].master_mp4_path.name, "front_take_001.mp4")
        self.assertEqual(plan.recordings[0].source_recording_relative_path, "nodes/front/take_001/recording.h264")
        self.assertEqual(plan.recordings[0].profile, "video_1080p25_auto")
        self.assertEqual(plan.recordings[0].duration_seconds, 10.0)

    def test_include_cameras_and_take_filter_recordings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front", "side"), takes=("take_001", "take_002"))

            plan = build_edit_asset_plan(
                EditAssetOptions(
                    session_path=session_path,
                    dry_run=True,
                    include_cameras=("side",),
                    take_id="take_002",
                )
            )

        self.assertEqual([asset.asset_id for asset in plan.recordings], ["side_take_002"])

    def test_dry_run_writes_no_outputs_and_reports_planned_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front",))

            report = prepare_edit_assets(EditAssetOptions(session_path=session_path, dry_run=True, proxies=True))

            self.assertFalse((session_path / "edit_assets" / "edit_assets_report.json").exists())
            self.assertFalse((session_path / "edit_assets" / "masters_mp4" / "front_take_001.mp4").exists())

        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(report["number_of_recordings_found"], 1)
        self.assertEqual(len(report["ffmpeg_commands"]), 2)
        self.assertIn("-c copy", report["ffmpeg_commands"][0])
        self.assertIn("front_take_001_proxy.mp4", report["ffmpeg_commands"][1])

    def test_prepare_writes_indexes_report_notes_and_import_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front", "side"))
            calls: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"fake mp4")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            report = prepare_edit_assets(EditAssetOptions(session_path=session_path), command_runner=runner)
            index = json.loads((session_path / "edit_assets" / "edit_assets_index.json").read_text(encoding="utf-8"))
            saved_report = json.loads((session_path / "edit_assets" / "edit_assets_report.json").read_text(encoding="utf-8"))
            with (session_path / "edit_assets" / "edit_assets_index.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            notes = (session_path / "edit_assets" / "kdenlive_import_notes.md").read_text(encoding="utf-8")
            import_list = (session_path / "edit_assets" / "import_list.txt").read_text(encoding="utf-8")

        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(report["number_of_mp4_masters_created"], 2)
        self.assertEqual(saved_report["overall_status"], "complete")
        self.assertEqual(len(index["assets"]), 2)
        self.assertEqual(rows[0]["asset_id"], "front_take_001")
        self.assertIn("masters_mp4", notes)
        self.assertIn("front_take_001.mp4", import_list)
        self.assertEqual(len(calls), 2)

    def test_proxy_generation_is_optional_and_uses_master_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front",))
            calls: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"fake mp4")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            report = prepare_edit_assets(
                EditAssetOptions(session_path=session_path, proxies=True, proxy_height=540),
                command_runner=runner,
            )

        self.assertEqual(report["number_of_proxies_created"], 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("front_take_001.mp4", calls[1][calls[1].index("-i") + 1])
        self.assertIn("min(540\\,ih)", calls[1][calls[1].index("-vf") + 1])

    def test_existing_master_is_skipped_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front",))
            output = session_path / "edit_assets" / "masters_mp4" / "front_take_001.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"existing")
            calls: list[list[str]] = []

            report = prepare_edit_assets(
                EditAssetOptions(session_path=session_path),
                command_runner=lambda command: calls.append(command) or subprocess.CompletedProcess(command, 0),
            )

        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(report["files_skipped_existing"], ["front_take_001"])
        self.assertEqual(calls, [])

    def test_overwrite_regenerates_existing_master(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front",))
            output = session_path / "edit_assets" / "masters_mp4" / "front_take_001.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"existing")
            calls: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                Path(command[-1]).write_bytes(b"new mp4")
                return subprocess.CompletedProcess(command, 0)

            report = prepare_edit_assets(
                EditAssetOptions(session_path=session_path, overwrite=True),
                command_runner=runner,
            )

        self.assertEqual(report["number_of_mp4_masters_created"], 1)
        self.assertEqual(calls[0][1], "-y")

    def test_missing_recording_is_reported_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front", "side"))
            (session_path / "nodes" / "side" / "take_001" / "recording.h264").unlink()

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"fake mp4")
                return subprocess.CompletedProcess(command, 0)

            report = prepare_edit_assets(
                EditAssetOptions(session_path=session_path),
                command_runner=runner,
            )

        self.assertEqual(report["overall_status"], "partial")
        self.assertIn("recording is missing", report["assets"][1]["errors"][0])

    def test_missing_ffmpeg_fails_clearly_when_not_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = _write_session(Path(temp_dir), cameras=("front",))

            with patch("openscan_multicam_coordinator.edit_assets.shutil.which", return_value=None):
                report = prepare_edit_assets(EditAssetOptions(session_path=session_path))

        self.assertEqual(report["overall_status"], "failed")
        self.assertIn("ffmpeg was not found on PATH", report["errors"])

    def test_command_construction(self) -> None:
        master = build_master_command(Path("recording.h264"), Path("front_take_001.mp4"), overwrite=False)
        proxy = build_proxy_command(Path("front_take_001.mp4"), Path("front_take_001_proxy.mp4"), proxy_height=720, overwrite=True)

        self.assertEqual(master[:7], ["ffmpeg", "-n", "-hide_banner", "-fflags", "+genpts", "-i", "recording.h264"])
        self.assertIn("-c", master)
        self.assertIn("copy", master)
        self.assertEqual(proxy[1], "-y")
        self.assertIn("libx264", proxy)
        self.assertIn("min(720\\,ih)", proxy[proxy.index("-vf") + 1])


def _write_session(root: Path, *, cameras: tuple[str, ...], takes: tuple[str, ...] = ("take_001",)) -> Path:
    session_path = root / "session-1"
    nodes: list[dict[str, object]] = []
    for camera_id in cameras:
        node_takes: list[dict[str, object]] = []
        for take_id in takes:
            take_dir = session_path / "nodes" / camera_id / take_id
            take_dir.mkdir(parents=True, exist_ok=True)
            (take_dir / "recording.h264").write_bytes(b"fake video bytes")
            manifest = {
                "take_id": take_id,
                "camera_id": camera_id,
                "profile": "video_1080p25_auto",
                "recording_start_time": "2026-06-01T10:00:00Z",
                "recording_stop_time": "2026-06-01T10:00:10Z",
                "pre_roll_seconds": 2,
                "usable_start_offset_seconds": 2,
            }
            (take_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            node_takes.append(
                {
                    "take_id": take_id,
                    "recording_relative_path": f"nodes/{camera_id}/{take_id}/recording.h264",
                    "manifest_relative_path": f"nodes/{camera_id}/{take_id}/manifest.json",
                    "recording_file_size": 16,
                    "recording_start_time": "2026-06-01T10:00:00Z",
                    "recording_stop_time": "2026-06-01T10:00:10Z",
                    "pre_roll_seconds": 2,
                    "usable_start_offset_seconds": 2,
                    "profile": "video_1080p25_auto",
                    "manifest_summary": {
                        "profile": "video_1080p25_auto",
                        "recording_start_time": "2026-06-01T10:00:00Z",
                        "recording_stop_time": "2026-06-01T10:00:10Z",
                        "pre_roll_seconds": 2,
                        "usable_start_offset_seconds": 2,
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
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / "session_index.json").write_text(
        json.dumps({"session_id": "session-1", "takes": list(takes), "nodes": nodes}),
        encoding="utf-8",
    )
    return session_path


if __name__ == "__main__":
    unittest.main()
