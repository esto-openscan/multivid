from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openscan_multicam_coordinator.config import NodeConfig, load_nodes_config
from openscan_multicam_coordinator.harvest import (
    HarvestOptions,
    _copy_remote_file,
    _remote_file_specs,
    build_rsync_command,
    build_session_index,
    harvest_session,
    remote_camera_session_path,
)


class HarvestTests(unittest.TestCase):
    def test_config_parses_harvest_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.yml"
            path.write_text(
                """
nodes:
  - name: cam-front
    camera_id: front
    base_url: http://cam-front.local:8080
    ssh_user: openscan
    remote_output_root: /srv/openscan-camera
    local_alias: front-left
    enabled: false
""",
                encoding="utf-8",
            )

            nodes = load_nodes_config(path)

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].ssh_host, "cam-front.local")
        self.assertEqual(nodes[0].ssh_user, "openscan")
        self.assertEqual(nodes[0].remote_output_root, "/srv/openscan-camera")
        self.assertEqual(nodes[0].harvest_folder, "front-left")
        self.assertFalse(nodes[0].enabled)

    def test_remote_path_normalizes_service_root_and_sessions_root(self) -> None:
        service_root = NodeConfig(
            name="cam-front",
            camera_id="front",
            base_url="http://cam-front.local:8080",
            remote_output_root="/srv/openscan-camera",
        )
        sessions_root = NodeConfig(
            name="cam-front",
            camera_id="front",
            base_url="http://cam-front.local:8080",
            remote_output_root="/srv/openscan-camera/sessions",
        )

        self.assertEqual(
            remote_camera_session_path(service_root, "session-1"),
            "/srv/openscan-camera/sessions/session-1/front",
        )
        self.assertEqual(
            remote_camera_session_path(sessions_root, "session-1"),
            "/srv/openscan-camera/sessions/session-1/front",
        )

    def test_build_rsync_command_uses_ssh_target(self) -> None:
        node = NodeConfig(
            name="cam-front",
            camera_id="front",
            base_url="http://cam-front.local:8080",
            ssh_host="cam-front.local",
            ssh_user="openscan",
        )

        command = build_rsync_command(node, "/srv/openscan-camera/sessions/s/front/take_001/manifest.json", Path("out"))

        self.assertEqual(command[0:3], ["rsync", "-a", "--protect-args"])
        self.assertEqual(command[3], "openscan@cam-front.local:/srv/openscan-camera/sessions/s/front/take_001/manifest.json")
        self.assertEqual(command[4], "out")

    def test_session_index_detects_missing_manifest_and_empty_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "harvested" / "session-1"
            take_dir = session_dir / "nodes" / "front" / "take_001"
            take_dir.mkdir(parents=True)
            (take_dir / "recording.h264").write_bytes(b"")
            node = NodeConfig(name="cam-front", camera_id="front", base_url="http://cam-front.local:8080")
            report = {
                "node_name": "cam-front",
                "status": "complete",
                "remote_session_summary": {
                    "hostname": "cam-front",
                    "takes": [
                        {
                            "take_id": "take_001",
                            "files": [
                                {"name": "manifest.json", "kind": "manifest", "exists": False},
                                {"name": "recording.h264", "kind": "recording", "exists": True, "size": 0},
                            ],
                        }
                    ],
                },
                "errors": [],
                "warnings": [],
                "missing_files": [],
            }

            index = build_session_index("session-1", session_dir, [node], [report], hash_small_files=False)

        take = index["nodes"][0]["takes"][0]
        self.assertEqual(take["status"], "incomplete")
        self.assertIn("manifest.json", take["missing_expected_files"])
        self.assertIn("manifest.json is missing or unreadable", take["errors"])
        self.assertIn("recording.h264 is empty", take["errors"])

    def test_reference_stills_are_copy_specs_and_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "harvested" / "session-1"
            stills_dir = session_dir / "nodes" / "front" / "reference_stills"
            stills_dir.mkdir(parents=True)
            (stills_dir / "alignment_001.jpg").write_bytes(b"jpeg")
            manifest = {
                "schema_version": 1,
                "status": "completed",
                "session_id": "session-1",
                "camera_id": "front",
                "label": "alignment_001",
                "timestamp": "2026-06-02T10:00:00Z",
                "image_file_name": "alignment_001.jpg",
                "actual_file_size": 4,
                "backend": "rpicam-still",
                "warnings": ["operator note"],
                "errors": [],
            }
            (stills_dir / "alignment_001_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            node = NodeConfig(name="cam-front", camera_id="front", base_url="http://cam-front.local:8080")
            summary = _remote_summary_with_reference_still("session-1", "front")
            report = {
                "node_name": "cam-front",
                "status": "complete",
                "remote_session_summary": summary,
                "errors": [],
                "warnings": [],
                "missing_files": [],
            }

            specs = _remote_file_specs(summary)
            index = build_session_index("session-1", session_dir, [node], [report], hash_small_files=True)

        spec_paths = {spec["relative_path"] for spec in specs}
        self.assertIn("reference_stills/alignment_001.jpg", spec_paths)
        self.assertIn("reference_stills/alignment_001_manifest.json", spec_paths)
        node_index = index["nodes"][0]
        self.assertEqual(node_index["reference_still_count"], 1)
        still = node_index["reference_stills"][0]
        self.assertEqual(still["label"], "alignment_001")
        self.assertEqual(still["status"], "ok")
        self.assertEqual(still["manifest_summary"]["backend"], "rpicam-still")
        self.assertIn("operator note", still["warnings"])
        self.assertEqual(index["takes"], [])

    def test_copy_remote_file_warns_on_conflict_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_root = Path(temp_dir) / "front"
            local_path = local_root / "take_001" / "recording.h264"
            local_path.parent.mkdir(parents=True)
            local_path.write_bytes(b"local")
            report = {
                "files_skipped_unchanged": 0,
                "files_conflicted": 0,
                "warnings": [],
                "missing_files": [],
                "conflict_files": [],
                "skipped_files": [],
                "copied_files": [],
                "errors": [],
                "files_copied": 0,
                "bytes_copied": 0,
            }
            node = NodeConfig(
                name="cam-front",
                camera_id="front",
                base_url="http://cam-front.local:8080",
                ssh_host="cam-front.local",
            )

            with patch("openscan_multicam_coordinator.harvest.subprocess.run") as run:
                _copy_remote_file(
                    node=node,
                    remote_root="/srv/openscan-camera/sessions/session-1/front",
                    local_root=local_root,
                    spec={"relative_path": "take_001/recording.h264", "size": 99},
                    report=report,
                    options=HarvestOptions(session_id="session-1"),
                )

        self.assertEqual(report["files_conflicted"], 1)
        self.assertIn("local file differs", report["conflict_files"][0]["reason"])
        run.assert_not_called()

    def test_copy_remote_file_treats_same_size_different_mtime_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_root = Path(temp_dir) / "front"
            local_path = local_root / "take_001" / "manifest.json"
            local_path.parent.mkdir(parents=True)
            local_path.write_text("{}", encoding="utf-8")
            report = {
                "files_skipped_unchanged": 0,
                "files_conflicted": 0,
                "warnings": [],
                "missing_files": [],
                "conflict_files": [],
                "skipped_files": [],
                "copied_files": [],
                "errors": [],
                "files_copied": 0,
                "bytes_copied": 0,
            }
            node = NodeConfig(
                name="cam-front",
                camera_id="front",
                base_url="http://cam-front.local:8080",
                ssh_host="cam-front.local",
            )

            with patch("openscan_multicam_coordinator.harvest.subprocess.run") as run:
                _copy_remote_file(
                    node=node,
                    remote_root="/srv/openscan-camera/sessions/session-1/front",
                    local_root=local_root,
                    spec={"relative_path": "take_001/manifest.json", "size": 2, "mtime": 1.0},
                    report=report,
                    options=HarvestOptions(session_id="session-1"),
                )

        self.assertEqual(report["files_conflicted"], 1)
        self.assertEqual(report["files_skipped_unchanged"], 0)
        run.assert_not_called()

    def test_harvest_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "harvested"
            node = NodeConfig(
                name="cam-front",
                camera_id="front",
                base_url="http://cam-front.local:8080",
                ssh_host="cam-front.local",
                local_alias="front",
            )
            fake_client = _fake_client(
                {
                    "http://cam-front.local:8080/status": {"hostname": "cam-front", "state": "idle"},
                    "http://cam-front.local:8080/sessions/session-1": _remote_summary("session-1", "front"),
                }
            )

            with patch("openscan_multicam_coordinator.harvest.httpx.Client", fake_client, create=True):
                outcome = harvest_session(
                    [node],
                    HarvestOptions(session_id="session-1", output_root=output_root, dry_run=True),
                )

        self.assertEqual(outcome.harvest_report["overall_status"], "complete")
        self.assertFalse((output_root / "session-1" / "harvest_report.json").exists())
        self.assertEqual(outcome.harvest_report["nodes"][0]["files_copied"], 0)
        self.assertEqual(outcome.harvest_report["nodes"][0]["skipped_files"][0]["reason"], "dry-run")

    def test_harvest_reports_partial_when_one_node_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "harvested"
            front = NodeConfig(
                name="cam-front",
                camera_id="front",
                base_url="http://cam-front.local:8080",
                ssh_host="cam-front.local",
            )
            side = NodeConfig(
                name="cam-side",
                camera_id="side",
                base_url="http://cam-side.local:8080",
                ssh_host="cam-side.local",
            )
            fake_client = _fake_client(
                {
                    "http://cam-front.local:8080/status": {"hostname": "cam-front", "state": "idle"},
                    "http://cam-front.local:8080/sessions/session-1": _remote_summary("session-1", "front"),
                }
            )

            with patch("openscan_multicam_coordinator.harvest.httpx.Client", fake_client, create=True):
                outcome = harvest_session(
                    [front, side],
                    HarvestOptions(session_id="session-1", output_root=output_root, dry_run=True),
                )

        self.assertEqual(outcome.harvest_report["overall_status"], "partial")
        statuses = {node["node_name"]: node["status"] for node in outcome.harvest_report["nodes"]}
        self.assertEqual(statuses["cam-front"], "complete")
        self.assertEqual(statuses["cam-side"], "offline")


def _remote_summary(session_id: str, camera_id: str) -> dict[str, object]:
    return {
        "camera_id": camera_id,
        "hostname": f"cam-{camera_id}",
        "session_id": session_id,
        "camera_session_path": f"/srv/openscan-camera/sessions/{session_id}/{camera_id}",
        "exists": True,
        "prepared_state": {"name": "prepared_state.json", "exists": True, "size": 12},
        "takes": [
            {
                "take_id": "take_001",
                "files": [
                    {"name": "manifest.json", "kind": "manifest", "exists": True, "size": 10},
                    {"name": "recording.h264", "kind": "recording", "exists": True, "size": 20},
                    {"name": "rpicam-vid.stderr.log", "kind": "stderr_log", "exists": True, "size": 0},
                ],
            }
        ],
    }


def _remote_summary_with_reference_still(session_id: str, camera_id: str) -> dict[str, object]:
    summary = _remote_summary(session_id, camera_id)
    summary["takes"] = []
    summary["reference_stills"] = [
        {
            "label": "alignment_001",
            "timestamp": "2026-06-02T10:00:00Z",
            "files": [
                {
                    "name": "alignment_001.jpg",
                    "relative_path": "reference_stills/alignment_001.jpg",
                    "kind": "reference_still_image",
                    "exists": True,
                    "size": 4,
                },
                {
                    "name": "alignment_001_manifest.json",
                    "relative_path": "reference_stills/alignment_001_manifest.json",
                    "kind": "reference_still_manifest",
                    "exists": True,
                    "size": 200,
                },
            ],
        }
    ]
    return summary


def _fake_client(responses: dict[str, dict[str, object]]):
    class FakeResponse:
        def __init__(self, data: dict[str, object] | None) -> None:
            self._data = data
            self.status_code = 200 if data is not None else 599
            self.text = json.dumps(data) if data is not None else "offline"
            self.is_success = data is not None

        def json(self) -> dict[str, object]:
            if self._data is None:
                raise ValueError("not json")
            return self._data

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            return FakeResponse(responses.get(url))

    return FakeClient


if __name__ == "__main__":
    unittest.main()
