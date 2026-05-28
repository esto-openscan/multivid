from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(
        AsyncClient=object,
        RequestError=Exception,
        Response=object,
        Timeout=lambda value: value,
    ),
)

from openscan_multicam_coordinator.cli import _format_calibration_summary


class CliFormattingTests(unittest.TestCase):
    def test_calibration_summary_formats_suggestions_and_yaml(self) -> None:
        output = _format_calibration_summary(
            "cam-front (front)",
            {
                "camera_id": "front",
                "status": "completed",
                "calibration_id": "cal-1",
                "profile": "video_1080p25_calibrated_suggest",
                "confidence": "medium",
                "suggested_controls": {
                    "suggested_controls": {
                        "shutter_us": {"value": 10000},
                        "gain": {"value": 1.7},
                        "awbgains": {"value": [1.82, 1.41]},
                        "lens_position": {"value": None},
                    },
                    "warnings": ["focus metadata unavailable from rpicam-vid"],
                },
            },
            show_yaml=True,
        )

        self.assertIn("suggested shutter_us: 10000", output)
        self.assertIn("suggested lens_position: unavailable", output)
        self.assertIn("awbgains: [1.82, 1.41]", output)
        self.assertIn("focus metadata unavailable", output)


if __name__ == "__main__":
    unittest.main()
