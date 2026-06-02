from __future__ import annotations

import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_camera_node.config import CameraNodeConfig, NodeCameraControlPolicy
from openscan_camera_node.profiles import load_recording_profiles
from openscan_camera_node.recorder import AlreadyPositioningError, AlreadyRecordingError, RpicamVidRecorder


class FakeProcess:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self._running = True
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return None if self._running else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._running = False
        self.returncode = -15
        return self.returncode


class RecorderTests(unittest.TestCase):
    def test_prepare_reuses_valid_state_and_records_refocus_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))

            first = recorder.prepare("session-1", "video")
            second = recorder.prepare("session-1", "video")
            refocus = recorder.prepare("session-1", "video", refocus=True)

        self.assertFalse(first["prepared_state_reused"])
        self.assertTrue(second["prepared_state_reused"])
        self.assertEqual(first["prepared_state"]["prepared_at"], second["prepared_state"]["prepared_at"])
        self.assertTrue(refocus["prepared_state_reused"])
        self.assertTrue(refocus["prepared_state"]["refocus_requested"])
        self.assertEqual(
            refocus["prepared_state"]["focus_prepare_behavior"],
            "not_implemented_for_rpicam_vid_backend",
        )

    def test_auto_take_id_uses_next_take_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            camera_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a"
            (camera_dir / "take_001").mkdir(parents=True)
            (camera_dir / "take_003").mkdir()

            self.assertEqual(recorder._resolve_take_id(camera_dir, None), "take_004")

    def test_start_auto_prepares_and_writes_take_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            first_process = FakeProcess(pid=11111)
            second_process = FakeProcess(pid=11112)

            with (
                patch(
                    "openscan_camera_node.recorder.subprocess.Popen",
                    side_effect=[first_process, second_process],
                ),
                patch("openscan_camera_node.recorder.os.killpg"),
            ):
                status = recorder.start("session-1", "video")
                recorder.stop()
                second_status = recorder.start("session-1", "video")
                recorder.stop()

            take_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a" / "take_001"
            second_take_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a" / "take_002"
            manifest = json.loads((take_dir / "manifest.json").read_text(encoding="utf-8"))
            second_manifest = json.loads((second_take_dir / "manifest.json").read_text(encoding="utf-8"))
            prepared_state = json.loads(
                (Path(temp_dir) / "sessions" / "session-1" / "cam-a" / "prepared_state.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(status["state"], "recording")
        self.assertEqual(status["current_take_id"], "take_001")
        self.assertTrue(status["prepared_valid"])
        self.assertEqual(second_status["current_take_id"], "take_002")
        self.assertEqual(manifest["session_id"], "session-1")
        self.assertEqual(manifest["take_id"], "take_001")
        self.assertEqual(manifest["output_file_name"], "recording.h264")
        self.assertEqual(manifest["pre_roll_seconds"], 5)
        self.assertFalse(manifest["prepared_state_reused"])
        self.assertTrue(second_manifest["prepared_state_reused"])
        self.assertEqual(manifest["resolved_controls"]["camera_controls"]["awbgains"], [1.75, 1.42])
        self.assertEqual(manifest["applied_controls"]["shutter_us"], 20000)
        self.assertEqual(manifest["applied_controls"]["awbgains"], [1.75, 1.42])
        self.assertEqual(manifest["applied_controls"]["lens_position"], 1.8)
        self.assertEqual(manifest["actually_applied_controls"], manifest["applied_controls"])
        self.assertIn("--codec", manifest["rpicam_vid_command"])
        self.assertIn("h264", manifest["rpicam_vid_command"])
        self.assertIn("--inline", manifest["rpicam_vid_command"])
        self.assertNotIn("--libav-format", manifest["rpicam_vid_command"])
        self.assertIn("--shutter", manifest["rpicam_vid_command"])
        self.assertIn("--awbgains", manifest["rpicam_vid_command"])
        self.assertIn("1.75,1.42", manifest["rpicam_vid_command"])
        self.assertTrue(prepared_state["valid"])
        self.assertEqual(prepared_state["planned_applied_controls"]["shutter_us"], 20000)

    def test_session_summary_lists_prepared_state_takes_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            process = FakeProcess(pid=11111)
            with (
                patch("openscan_camera_node.recorder.subprocess.Popen", return_value=process),
                patch("openscan_camera_node.recorder.os.killpg"),
            ):
                recorder.start("session-1", "video")
                recorder.stop()

            take_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a" / "take_001"
            (take_dir / "recording.h264").write_bytes(b"video")
            summary = recorder.session_summary("session-1")

        self.assertTrue(summary["exists"])
        self.assertTrue(summary["prepared_state"]["exists"])
        self.assertEqual(summary["take_count"], 1)
        take = summary["takes"][0]
        self.assertEqual(take["take_id"], "take_001")
        self.assertEqual(take["recording_file_name"], "recording.h264")
        files_by_name = {file_info["name"]: file_info for file_info in take["files"]}
        self.assertEqual(files_by_name["manifest.json"]["kind"], "manifest")
        self.assertEqual(files_by_name["recording.h264"]["kind"], "recording")
        self.assertEqual(files_by_name["recording.h264"]["size"], 5)
        self.assertEqual(take["manifest_summary"]["session_id"], "session-1")

    def test_prepare_links_calibration_suggestions_and_start_applies_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_calibration_recorder(Path(temp_dir))
            missing = recorder.prepare("session-1", "calibrated")

            camera_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a"
            calibration_dir = camera_dir / "calibration" / "cal-1"
            calibration_dir.mkdir(parents=True)
            suggestions = {
                "source": "rpicam-vid metadata",
                "confidence": "medium",
                "camera_id": "cam-a",
                "calibration_id": "cal-1",
                "profile": "calibrated",
                "suggested_controls": {
                    "shutter_us": {"value": 10000, "source_field": "ExposureTime", "warning": None},
                    "gain": {"value": 1.7, "source_field": "AnalogueGain", "warning": None},
                    "awbgains": {"value": [1.82, 1.41], "source_field": "ColourGains", "warning": None},
                    "lens_position": {"value": None, "source_field": None, "warning": "missing"},
                },
                "warnings": ["focus metadata unavailable from rpicam-vid"],
            }
            (calibration_dir / "suggested_controls.json").write_text(json.dumps(suggestions), encoding="utf-8")
            last = {
                "camera_id": "cam-a",
                "session_id": "session-1",
                "calibration_id": "cal-1",
                "profile": "calibrated",
                "status": "completed",
                "calibration_manifest_path": str(calibration_dir / "calibration_manifest.json"),
                "suggested_controls_path": str(calibration_dir / "suggested_controls.json"),
                "suggested_controls": suggestions,
                "confidence": "medium",
                "warnings": suggestions["warnings"],
            }
            (camera_dir / "calibration" / "last.json").write_text(json.dumps(last), encoding="utf-8")

            prepared = recorder.prepare("session-1", "calibrated", force=True)
            process = FakeProcess(pid=11111)
            with (
                patch("openscan_camera_node.recorder.subprocess.Popen", return_value=process),
                patch("openscan_camera_node.recorder.os.killpg"),
            ):
                recorder.start("session-1", "calibrated", apply_calibration_suggestions=True)
                recorder.stop()

            manifest = json.loads((camera_dir / "take_001" / "manifest.json").read_text(encoding="utf-8"))

        self.assertIn("no suggested_controls.json was available", "\n".join(missing["prepared_state"]["warnings"]))
        self.assertEqual(prepared["prepared_state"]["calibration_id"], "cal-1")
        self.assertEqual(
            prepared["prepared_state"]["suggested_controls_snapshot"]["suggested_controls"]["shutter_us"]["value"],
            10000,
        )
        self.assertTrue(manifest["calibration_suggestions_applied"])
        self.assertEqual(manifest["applied_controls"]["shutter_us"], 10000)
        self.assertIn("--shutter", manifest["rpicam_vid_command"])
        self.assertIn("10000", manifest["rpicam_vid_command"])

    def test_positioning_start_stop_and_overlay_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))

            status = recorder.start_positioning(
                width=320,
                height=180,
                fps=3,
                jpeg_quality=70,
                overlays=["crosshair", "shorts-safe-area"],
            )
            full_status = recorder.status()
            stopped = recorder.stop_positioning()

        self.assertTrue(status["running"])
        self.assertEqual(status["state"], "positioning")
        self.assertEqual(status["settings"]["width"], 320)
        self.assertEqual(status["settings"]["overlays"], ["crosshair", "shorts_safe_area"])
        self.assertTrue(full_status["positioning_running"])
        self.assertFalse(full_status["recording_allowed"])
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["state"], "idle")

    def test_positioning_snapshot_uses_jpeg_backend_and_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            recorder.start_positioning(width=320, height=180, fps=5, overlays=[])

            with (
                patch("openscan_camera_node.imaging.shutil.which", return_value="/usr/bin/rpicam-still"),
                patch("openscan_camera_node.imaging.subprocess.run", side_effect=_fake_jpeg_run),
            ):
                image_bytes, metadata = recorder.positioning_snapshot()
                status = recorder.positioning_status()

        self.assertEqual(image_bytes, b"jpeg")
        self.assertEqual(metadata["backend"], "rpicam-still")
        self.assertEqual(status["frames_served"], 1)
        self.assertEqual(status["last_backend"], "rpicam-still")

    def test_recording_is_rejected_while_positioning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            recorder.start_positioning()

            with self.assertRaises(AlreadyPositioningError):
                recorder.start("session-1", "video")

            status = recorder.status()

        self.assertEqual(status["state"], "positioning")
        self.assertTrue(status["positioning_running"])
        self.assertIn("stop positioning first", status["last_error"])

    def test_reference_still_is_rejected_while_recording_or_positioning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))
            process = FakeProcess(pid=11111)
            with (
                patch("openscan_camera_node.recorder.subprocess.Popen", return_value=process),
                patch("openscan_camera_node.recorder.os.killpg"),
            ):
                recorder.start("session-1", "video")
                with self.assertRaises(AlreadyRecordingError):
                    recorder.capture_reference_still("session-1", label="alignment_001")
                recorder.stop()
            recorder.start_positioning()
            with self.assertRaises(AlreadyPositioningError):
                recorder.capture_reference_still("session-1", label="alignment_001")

    def test_reference_still_capture_writes_manifest_and_unique_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = _new_recorder(Path(temp_dir))

            with (
                patch("openscan_camera_node.imaging.shutil.which", return_value="/usr/bin/rpicam-still"),
                patch("openscan_camera_node.imaging.subprocess.run", side_effect=_fake_jpeg_run),
            ):
                first = recorder.capture_reference_still(
                    "session-1",
                    label="alignment_001",
                    profile_name="video",
                    quality=90,
                )
                second = recorder.capture_reference_still(
                    "session-1",
                    label="alignment_001",
                    profile_name="video",
                    quality=90,
                )
                summary = recorder.session_summary("session-1")

            camera_dir = Path(temp_dir) / "sessions" / "session-1" / "cam-a"
            first_manifest = json.loads(
                (camera_dir / "reference_stills" / "alignment_001_manifest.json").read_text(encoding="utf-8")
            )
            second_manifest = json.loads(
                (camera_dir / "reference_stills" / "alignment_001_002_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first["still_capture"]["status"], "completed")
        self.assertEqual(first["still_capture"]["label"], "alignment_001")
        self.assertEqual(second["still_capture"]["label"], "alignment_001_002")
        self.assertIn("already exists", "\n".join(second_manifest["warnings"]))
        self.assertEqual(first_manifest["session_id"], "session-1")
        self.assertEqual(first_manifest["camera_id"], "cam-a")
        self.assertEqual(first_manifest["profile"], "video")
        self.assertEqual(first_manifest["requested_quality"], 90)
        self.assertEqual(first_manifest["actual_file_size"], 4)
        self.assertEqual(first_manifest["backend"], "rpicam-still")
        self.assertTrue(first_manifest["use_recording_profile_controls"])
        self.assertEqual(first_manifest["applied_controls"]["shutter_us"], 20000)
        self.assertEqual(summary["take_count"], 0)
        self.assertEqual(summary["reference_still_count"], 2)
        image_paths = {
            still["files"][0]["relative_path"]
            for still in summary["reference_stills"]
        }
        self.assertIn("reference_stills/alignment_001.jpg", image_paths)


def _new_recorder(root: Path) -> RpicamVidRecorder:
    profiles_path = root / "profiles.yml"
    profiles_path.write_text(
        """
profiles:
  video:
    description: Test video
    output_extension: h264
    recording:
      width: 1920
      height: 1080
      framerate: 25
      bitrate: 12000000
      codec: h264
      container: null
      nopreview: true
    camera_controls:
      shutter_us: 20000
      gain: 1.5
      awbgains: [1.75, 1.42]
      autofocus_mode: manual
      lens_position: 1.8
    camera_control_policy:
      pre_roll_seconds: 5
      exposure_mode: manual
      awb_mode: manual
      focus_mode: manual
      reuse_prepared_controls: true
      refocus_on_each_take: false
      prepare_warmup_seconds: 0
    rpicam_vid_extra_args:
      - --inline
""",
        encoding="utf-8",
    )
    config = CameraNodeConfig(
        camera_id="cam-a",
        listen_host="127.0.0.1",
        listen_port=8080,
        output_root=root / "sessions",
        profile_overrides={},
        camera_control_policy=NodeCameraControlPolicy(
            use_calibration_suggestions=False,
            apply_suggestions_to_recording=False,
        ),
    )
    return RpicamVidRecorder(config=config, profiles=load_recording_profiles(profiles_path))


def _new_calibration_recorder(root: Path) -> RpicamVidRecorder:
    profiles_path = root / "profiles.yml"
    profiles_path.write_text(
        """
profiles:
  calibrated:
    output_extension: h264
    recording:
      width: 1920
      height: 1080
      framerate: 25
      bitrate: 12000000
      codec: h264
      nopreview: true
    camera_controls:
      shutter_us: null
      gain: null
      awbgains: null
      autofocus_mode: null
      lens_position: null
    camera_control_policy:
      exposure_mode: auto_then_lock
      awb_mode: auto_then_lock
      focus_mode: auto_then_lock
      reuse_prepared_controls: true
      use_calibration_suggestions: true
      apply_suggestions_to_recording: false
""",
        encoding="utf-8",
    )
    config = CameraNodeConfig(
        camera_id="cam-a",
        listen_host="127.0.0.1",
        listen_port=8080,
        output_root=root / "sessions",
        profile_overrides={},
        camera_control_policy=NodeCameraControlPolicy(
            use_calibration_suggestions=False,
            apply_suggestions_to_recording=False,
        ),
    )
    return RpicamVidRecorder(config=config, profiles=load_recording_profiles(profiles_path))


def _fake_jpeg_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
    output_file = Path(command[command.index("--output") + 1])
    output_file.write_bytes(b"jpeg")
    return SimpleNamespace(returncode=0, stderr="")


if __name__ == "__main__":
    unittest.main()
