from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_camera_node.config import CameraNodeConfig
from openscan_camera_node.profiles import load_recording_profiles
from openscan_camera_node.recorder import RpicamVidRecorder


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
    )
    return RpicamVidRecorder(config=config, profiles=load_recording_profiles(profiles_path))


if __name__ == "__main__":
    unittest.main()
