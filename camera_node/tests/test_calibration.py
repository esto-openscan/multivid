from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_camera_node.calibration import build_suggested_controls, parse_rpicam_metadata, suggestion_values


class CalibrationTests(unittest.TestCase):
    def test_parse_json_lines_metadata_and_suggest_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata_path = Path(temp_dir) / "metadata.json"
            metadata_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "ExposureTime": 9000,
                                "AnalogueGain": 1.4,
                                "ColourGains": [1.7, 1.3],
                            }
                        ),
                        json.dumps(
                            {
                                "ExposureTime": 10000,
                                "AnalogueGain": 1.7,
                                "ColourGains": [1.82, 1.41],
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            metadata = parse_rpicam_metadata(metadata_path)
            suggestions = build_suggested_controls(
                metadata_result=metadata,
                profile_name="video",
                profile_snapshot={"description": "test"},
                camera_id="cam-a",
                calibration_id="cal-1",
                calibration_manifest_path=Path(temp_dir) / "calibration_manifest.json",
            )

        values = suggestion_values(suggestions)
        self.assertEqual(values["shutter_us"], 10000)
        self.assertEqual(values["gain"], 1.7)
        self.assertEqual(values["awbgains"], [1.82, 1.41])
        self.assertNotIn("lens_position", values)
        self.assertEqual(suggestions["confidence"], "medium")
        self.assertEqual(
            suggestions["suggested_controls"]["lens_position"]["warning"],
            "Not available from rpicam-vid metadata on this backend",
        )

    def test_missing_metadata_keeps_null_values_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = parse_rpicam_metadata(Path(temp_dir) / "missing.json")
            suggestions = build_suggested_controls(
                metadata_result=metadata,
                profile_name="video",
                profile_snapshot={},
                camera_id="cam-a",
                calibration_id="cal-1",
                calibration_manifest_path=Path(temp_dir) / "calibration_manifest.json",
            )

        self.assertEqual(suggestion_values(suggestions), {})
        self.assertEqual(suggestions["confidence"], "low")
        self.assertIn("shutter_us", suggestions["unavailable_fields"])


if __name__ == "__main__":
    unittest.main()
