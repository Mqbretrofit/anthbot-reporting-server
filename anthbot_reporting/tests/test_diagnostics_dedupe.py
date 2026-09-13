from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

import diagnostics_dedupe as dedupe


class DiagnosticsDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "reporting.sqlite3"
        os.environ["ANTHBOT_DB_PATH"] = str(self.db_path)
        self.installation_id = str(uuid4())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _map_error(request_id: str, host_id: str) -> str:
        return (
            'multi_maps definition download failed (404): <?xml version="1.0" '
            'encoding="UTF-8"?><Error><Code>NoSuchKey</Code>'
            '<Message>The specified key does not exist.</Message>'
            '<Key>MGS02/device/26060LGR00002199/multi_maps/'
            'map_26060LGR00002199_0</Key>'
            f'<RequestId>{request_id}</RequestId><HostId>{host_id}'
        )

    def _payload(
        self,
        *,
        request_id: str = "REQ-A",
        host_id: str = "HOST-A",
        serial_hash: str = "robot-a-hash",
        trigger: str = "map_definition_error",
    ) -> dict:
        report = {
            "device": {
                "model": "Anthbot M9",
                "serial_sha256": serial_hash,
                "serial_suffix": "2199",
            },
            "definitions": {
                "map": {"error": self._map_error(request_id, host_id)},
                "path": {"error": None},
            },
            "connection": {"live_shadow_error": None},
            "telemetry": {"err_code": 0},
        }
        return {
            "schema": "anthbot-map-diagnostics-upload-v1",
            "generated_at": "2026-09-13T14:39:31+00:00",
            "installation_id": self.installation_id,
            "trigger": trigger,
            "report": report,
        }

    def test_truncated_request_and_host_ids_normalize_identically(self) -> None:
        first = self._map_error("A7C5MKCC82NTWXSA", "HOST-ONE-TRUNCATED")
        second = self._map_error("DIFFERENT-REQUEST", "HOST-TWO-TRUNCATED")
        self.assertEqual(
            dedupe.normalize_cloud_error(first),
            dedupe.normalize_cloud_error(second),
        )
        self.assertNotIn(
            "A7C5MKCC82NTWXSA", dedupe.normalize_cloud_error(first)
        )
        self.assertNotIn(
            "HOST-ONE-TRUNCATED", dedupe.normalize_cloud_error(first)
        )

    def test_m9_map_reports_with_new_aws_ids_share_semantic_key(self) -> None:
        first = self._payload(request_id="REQ-A", host_id="HOST-A")
        second = self._payload(request_id="REQ-B", host_id="HOST-B")
        second["generated_at"] = "2026-09-13T14:39:41+00:00"
        self.assertEqual(
            dedupe.semantic_dedupe_key(first),
            dedupe.semantic_dedupe_key(second),
        )

    def test_same_error_on_two_robots_does_not_collide(self) -> None:
        first = self._payload(serial_hash="robot-a-hash")
        second = self._payload(serial_hash="robot-b-hash")
        self.assertNotEqual(
            dedupe.semantic_dedupe_key(first),
            dedupe.semantic_dedupe_key(second),
        )

    def test_different_real_error_does_not_collide(self) -> None:
        first = self._payload()
        second = self._payload()
        second["report"]["definitions"]["map"]["error"] = (
            "map_manager downloaded but iot_map.bin was not recognized"
        )
        self.assertNotEqual(
            dedupe.semantic_dedupe_key(first),
            dedupe.semantic_dedupe_key(second),
        )

    def test_unknown_future_trigger_is_not_deduplicated(self) -> None:
        payload = self._payload(trigger="future_new_trigger")
        self.assertIsNone(dedupe.semantic_dedupe_key(payload))

    def test_reservation_is_persistent_and_windowed(self) -> None:
        key = dedupe.semantic_dedupe_key(self._payload())
        self.assertIsNotNone(key)
        assert key is not None
        start = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)

        first = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start,
        )
        self.assertFalse(first[0])

        duplicate = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start + timedelta(minutes=5),
        )
        self.assertTrue(duplicate[0])

        after_window = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start + timedelta(hours=1, minutes=6),
        )
        self.assertFalse(after_window[0])

    def test_finalize_attaches_existing_report_id_to_duplicates(self) -> None:
        payload = self._payload()
        key = dedupe.semantic_dedupe_key(payload)
        self.assertIsNotNone(key)
        assert key is not None
        start = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)
        reserved = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start,
        )
        self.assertFalse(reserved[0])
        reservation_time = reserved[2]

        report_json = json.dumps(
            payload["report"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        report_sha = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostics (
                    report_id TEXT PRIMARY KEY,
                    installation_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO diagnostics (
                    report_id, installation_id, trigger, generated_at,
                    received_at, report_sha256, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AB-20260913-TEST0001",
                    self.installation_id,
                    "map_definition_error",
                    "2026-09-13T14:00:00+00:00",
                    "2026-09-13T14:00:01+00:00",
                    report_sha,
                    report_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        dedupe.finalize_reservation(
            key,
            reservation_time=reservation_time,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            report_sha256=report_sha,
            success=True,
        )

        duplicate = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start + timedelta(minutes=10),
        )
        self.assertTrue(duplicate[0])
        self.assertEqual(duplicate[1], "AB-20260913-TEST0001")

    def test_failed_request_releases_reservation(self) -> None:
        key = dedupe.semantic_dedupe_key(self._payload())
        assert key is not None
        start = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)
        reserved = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start,
        )
        dedupe.finalize_reservation(
            key,
            reservation_time=reserved[2],
            installation_id=self.installation_id,
            trigger="map_definition_error",
            report_sha256="does-not-matter",
            success=False,
        )
        retry = dedupe.reserve_or_get_duplicate(
            key,
            installation_id=self.installation_id,
            trigger="map_definition_error",
            now=start + timedelta(seconds=5),
        )
        self.assertFalse(retry[0])

    def test_dockerfile_packages_middleware(self) -> None:
        dockerfile = Path(__file__).parents[1] / "Dockerfile"
        source = dockerfile.read_text(encoding="utf-8")
        self.assertIn("COPY diagnostics_dedupe.py ./diagnostics_dedupe.py", source)


if __name__ == "__main__":
    unittest.main()
