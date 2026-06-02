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

from openscan_multicam_coordinator.cli import _format_calibration_summary, _format_positioning_settings, _positioning_url_rows
from openscan_multicam_coordinator.client import NodeResult
from openscan_multicam_coordinator.config import NodeConfig


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

    def test_positioning_urls_are_browser_openable(self) -> None:
        rows = _positioning_url_rows(
            [
                NodeResult(
                    node=NodeConfig(
                        name="cam-front",
                        camera_id="front",
                        base_url="http://cam-front.local:8080",
                    ),
                    ok=True,
                    status_code=200,
                    data={"running": True},
                    error=None,
                )
            ]
        )

        self.assertEqual(rows[0]["snapshot_url"], "http://cam-front.local:8080/positioning/snapshot.jpg")
        self.assertEqual(rows[0]["stream_url"], "http://cam-front.local:8080/positioning/stream.mjpg")

    def test_positioning_settings_format_overlays(self) -> None:
        output = _format_positioning_settings(
            {
                "width": 640,
                "height": 360,
                "fps": 5,
                "jpeg_quality": 75,
                "overlays": ["crosshair", "shorts_safe_area"],
            }
        )

        self.assertIn("640x360", output)
        self.assertIn("5fps", output)
        self.assertIn("overlays=crosshair,shorts_safe_area", output)


if __name__ == "__main__":
    unittest.main()
