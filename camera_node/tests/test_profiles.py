from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_camera_node.profiles import load_recording_profiles


class RecordingProfileTests(unittest.TestCase):
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
        self.assertEqual(profile.unsupported_camera_control_policy["unsupported_future_field"], "keep-me-visible")
        self.assertIn("unsupported camera_control_policy field", profile.warnings[0])


if __name__ == "__main__":
    unittest.main()
