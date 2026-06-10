from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import httpx as _httpx  # noqa: F401
except Exception:
    sys.modules.setdefault(
        "httpx",
        types.SimpleNamespace(
            AsyncClient=object,
            RequestError=Exception,
            Response=object,
            Timeout=lambda value: value,
        ),
    )

from openscan_multicam_coordinator.client import NodeResult
from openscan_multicam_coordinator.config import DashboardConfig, NodeConfig, load_dashboard_config
from openscan_multicam_coordinator.operations import (
    aggregate_operation_response,
    aggregate_profiles,
    build_node_url,
    dashboard_result,
)

try:
    from openscan_multicam_coordinator.dashboard.app import create_app

    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False
    create_app = None  # type: ignore[assignment]


class DashboardConfigTests(unittest.TestCase):
    def test_dashboard_config_loads_defaults_from_nodes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.yml"
            path.write_text(
                """
nodes:
  - name: cam-front
    camera_id: front
    base_url: http://cam-front.local:8080
dashboard:
  positioning:
    width: 800
    height: 450
    fps: 4
    jpeg_quality: 70
    overlays:
      - camera_label
      - grid
  status_refresh_seconds: 5
""",
                encoding="utf-8",
            )

            config = load_dashboard_config(path)

        self.assertEqual(config.positioning.width, 800)
        self.assertEqual(config.positioning.height, 450)
        self.assertEqual(config.positioning.fps, 4)
        self.assertEqual(config.positioning.jpeg_quality, 70)
        self.assertEqual(config.positioning.overlays, ("camera_label", "grid"))
        self.assertEqual(config.status_refresh_seconds, 5)

    def test_dashboard_config_is_backward_compatible_without_dashboard_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.yml"
            path.write_text(
                """
nodes:
  - name: cam-front
    camera_id: front
    base_url: http://cam-front.local:8080
""",
                encoding="utf-8",
            )

            config = load_dashboard_config(path)

        self.assertEqual(config.positioning.width, 640)
        self.assertIn("shorts_safe_area", config.positioning.overlays)
        self.assertEqual(config.status_refresh_seconds, 3)


class DashboardAggregationTests(unittest.TestCase):
    def test_preview_url_generation_accepts_relative_paths(self) -> None:
        node = NodeConfig(name="cam-front", camera_id="front", base_url="http://cam-front.local:8080")

        self.assertEqual(build_node_url(node, "/positioning/snapshot.jpg"), "http://cam-front.local:8080/positioning/snapshot.jpg")
        self.assertEqual(build_node_url(node, "positioning/stream.mjpg"), "http://cam-front.local:8080/positioning/stream.mjpg")

    def test_status_aggregation_keeps_partial_failures(self) -> None:
        front = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        side = NodeConfig(name="side", camera_id="side", base_url="http://side.local:8080")
        response = aggregate_operation_response(
            "status",
            [
                NodeResult(front, True, 200, {"state": "idle", "positioning_running": False}, None),
                NodeResult(side, False, None, None, "connection failed"),
            ],
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["success_count"], 1)
        self.assertEqual(response["failure_count"], 1)
        self.assertEqual(response["nodes"][1]["state"], "offline")
        self.assertEqual(response["nodes"][1]["message"], "side: offline")

    def test_operation_messages_are_action_specific(self) -> None:
        front = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        response = aggregate_operation_response(
            "positioning_start",
            [NodeResult(front, True, 202, {"state": "positioning", "positioning_running": True}, None)],
        )

        self.assertEqual(response["nodes"][0]["message"], "front: positioning started")

    def test_dashboard_result_includes_preview_links_and_state(self) -> None:
        node = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        result = dashboard_result(
            NodeResult(
                node,
                True,
                200,
                {
                    "state": "positioning",
                    "current_session_id": "session-1",
                    "positioning_running": True,
                    "positioning_snapshot_path": "/positioning/snapshot.jpg",
                    "positioning_stream_path": "/positioning/stream.mjpg",
                },
                None,
            )
        )

        self.assertEqual(result["state"], "positioning")
        self.assertTrue(result["positioning_running"])
        self.assertEqual(result["snapshot_url"], "http://front.local:8080/positioning/snapshot.jpg")
        self.assertEqual(result["stream_url"], "http://front.local:8080/positioning/stream.mjpg")

    def test_profile_aggregation_reports_compatible_profiles(self) -> None:
        front = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        side = NodeConfig(name="side", camera_id="side", base_url="http://side.local:8080")
        profiles = {"profiles": {"video_1080p25_auto": {}, "video_1080p25_locked": {}}}

        response = aggregate_profiles(
            [
                NodeResult(front, True, 200, profiles, None),
                NodeResult(side, True, 200, profiles, None),
            ]
        )

        self.assertTrue(response["compatible"])
        self.assertEqual(response["profile_names"], ["video_1080p25_auto", "video_1080p25_locked"])
        self.assertIsNone(response["warning"])

    def test_profile_aggregation_warns_on_different_or_failed_nodes(self) -> None:
        front = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        side = NodeConfig(name="side", camera_id="side", base_url="http://side.local:8080")

        response = aggregate_profiles(
            [
                NodeResult(front, True, 200, {"profiles": {"a": {}}}, None),
                NodeResult(side, False, None, None, "offline"),
            ]
        )

        self.assertFalse(response["compatible"])
        self.assertEqual(response["profile_names"], ["a"])
        self.assertIn("manual profile entry", response["warning"])

    def test_no_node_profile_response_is_explicit(self) -> None:
        response = aggregate_profiles([])

        self.assertFalse(response["compatible"])
        self.assertEqual(response["profile_names"], [])
        self.assertEqual(response["warning"], "no camera nodes are configured")


@unittest.skipUnless(HAS_FASTAPI, "FastAPI test dependencies are not installed")
class DashboardAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_route_serves_dashboard(self) -> None:
        assert create_app is not None
        app = create_app("unused.yml", nodes=[], dashboard_config=DashboardConfig())

        route = _route_for(app, "/")
        response = route.endpoint()

        self.assertEqual(Path(response.path).name, "index.html")

    async def test_status_endpoint_returns_dashboard_response_shape(self) -> None:
        assert create_app is not None
        node = NodeConfig(name="front", camera_id="front", base_url="http://front.local:8080")
        app = create_app("unused.yml", nodes=[node], dashboard_config=DashboardConfig())

        async def fake_request_nodes(nodes, spec):
            return [NodeResult(nodes[0], True, 200, {"state": "idle"}, None)]

        with patch("openscan_multicam_coordinator.dashboard.app.request_nodes", fake_request_nodes):
            data = await _route_for(app, "/api/status").endpoint()

        self.assertEqual(data["operation"], "status")
        self.assertEqual(data["node_count"], 1)
        self.assertEqual(data["nodes"][0]["node"]["name"], "front")


def _route_for(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route
    raise AssertionError(f"route not found: {path}")


if __name__ == "__main__":
    unittest.main()
