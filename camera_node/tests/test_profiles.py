from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_camera_node.profiles import load_recording_profiles


class RecordingProfileTests(unittest.TestCase):
    def test_example_calibrated_suggest_profile_applies_suggestions(self) -> None:
        profiles_path = Path(__file__).resolve().parents[2] / "examples" / "profiles.yml"

        profiles = load_recording_profiles(profiles_path)
        profile = profiles.get("video_1080p25_calibrated_suggest")

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertTrue(profile.camera_control_policy.use_calibration_suggestions)
        self.assertTrue(profile.camera_control_policy.apply_suggestions_to_recording)

    def test_camera_control_policy_defaults_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profiles.yml"
            path.write_text(
                """
profiles:
  video:
    description: Test profile
    output_extension: h264
    camera_control_policy:
      unsupported_future_field: keep-me-visible
    rpicam_vid_args:
      - --width
      - "1920"
""",
                encoding="utf-8",
            )

            profiles = load_recording_profiles(path)
            profile = profiles.get("video")

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.camera_control_policy.pre_roll_seconds, 5.0)
        self.assertEqual(profile.camera_control_policy.exposure_mode, "auto")
        self.assertEqual(profile.camera_control_policy.awb_mode, "auto_then_lock")
        self.assertEqual(profile.camera_control_policy.focus_mode, "auto")
        self.assertTrue(profile.camera_control_policy.reuse_prepared_controls)
        self.assertFalse(profile.camera_control_policy.refocus_on_each_take)
        self.assertFalse(profile.camera_control_policy.use_calibration_suggestions)
        self.assertFalse(profile.camera_control_policy.apply_suggestions_to_recording)
        self.assertEqual(profile.unsupported_camera_control_policy["unsupported_future_field"], "keep-me-visible")
        self.assertIn("unsupported camera_control_policy field", profile.warnings[0])

    def test_structured_recording_controls_and_node_overrides_build_rpicam_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profiles.yml"
            path.write_text(
                """
profiles:
  locked:
    description: Locked profile
    recording:
      width: 1920
      height: 1080
      framerate: 25
      bitrate: 12000000
      codec: h264
      container: null
      nopreview: true
      level: "4.2"
    camera_controls:
      shutter_us: 20000
      gain: 1.5
      awbgains: null
      autofocus_mode: manual
      lens_position: null
    camera_control_policy:
      exposure_mode: manual
      awb_mode: manual
      focus_mode: manual
""",
                encoding="utf-8",
            )

            profiles = load_recording_profiles(
                path,
                profile_overrides={
                    "locked": {
                        "camera_controls": {
                            "awbgains": [1.75, 1.42],
                            "lens_position": 1.8,
                        }
                    }
                },
            )
            profile = profiles.get("locked")

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.output_extension, "h264")
        self.assertEqual(profile.camera_controls["awbgains"], [1.75, 1.42])
        self.assertEqual(profile.camera_controls["lens_position"], 1.8)
        self.assertIn("--codec", profile.rpicam_vid_args)
        self.assertIn("h264", profile.rpicam_vid_args)
        self.assertNotIn("--libav-format", profile.rpicam_vid_args)
        self.assertIn("--shutter", profile.rpicam_vid_args)
        self.assertIn("20000", profile.rpicam_vid_args)
        self.assertIn("--awbgains", profile.rpicam_vid_args)
        self.assertIn("1.75,1.42", profile.rpicam_vid_args)
        self.assertIn("--lens-position", profile.rpicam_vid_args)
        self.assertEqual(profile.planned_applied_controls["awbgains"], [1.75, 1.42])
        self.assertEqual(profile.planned_applied_controls["lens_position"], 1.8)
        self.assertEqual(profile.warnings, [])

    def test_incomplete_manual_lock_generates_clear_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profiles.yml"
            path.write_text(
                """
profiles:
  incomplete:
    camera_control_policy:
      exposure_mode: manual
      awb_mode: manual
      focus_mode: manual
    camera_controls:
      shutter_us: 20000
      autofocus_mode: auto
""",
                encoding="utf-8",
            )

            profiles = load_recording_profiles(path)
            profile = profiles.get("incomplete")

        self.assertIsNotNone(profile)
        assert profile is not None
        warnings = "\n".join(profile.warnings)
        self.assertIn("camera_controls.gain is missing", warnings)
        self.assertIn("camera_controls.awbgains is missing", warnings)
        self.assertIn("camera_controls.autofocus_mode is not manual", warnings)
        self.assertIn("camera_controls.lens_position is missing", warnings)


if __name__ == "__main__":
    unittest.main()
