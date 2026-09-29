from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import app as server
import presence_api


class ErrorStatsClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(
            Path(self.tempdir.name) / "reporting.sqlite3"
        )
        server._init_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _insert(
        self,
        *,
        serial_hash: str,
        suffix: str,
        model: str,
        code: int,
        message: str,
        trigger: str = "task_event",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        report = {
            "device": {
                "model": model,
                "serial_sha256": serial_hash,
                "serial_suffix": suffix,
            },
            "diagnostic_event": {
                "err_code": code,
                "robot_sta": "pause",
                "task_event": {
                    "code": code,
                    "event_message": message,
                },
            },
        }
        raw = json.dumps(report, sort_keys=True)
        with presence_api._connect() as conn:
            conn.execute(
                """
                INSERT INTO diagnostics (
                    report_id, installation_id, trigger, generated_at,
                    received_at, report_sha256, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"AB-TEST-{uuid4().hex}",
                    str(uuid4()),
                    trigger,
                    now,
                    now,
                    hashlib.sha256(raw.encode()).hexdigest(),
                    raw,
                ),
            )

    def test_stale_error_code_on_normal_task_event_is_not_an_error(self) -> None:
        event = {
            "err_code": 2012,
            "task_event": {"code": 2012, "event_message": "Task resumes"},
        }
        self.assertEqual(
            presence_api._classify_diagnostic_event("task_event", event),
            "status",
        )

    def test_known_fault_message_is_an_error(self) -> None:
        event = {
            "err_code": 2012,
            "task_event": {
                "code": 2012,
                "event_message": "The machine is stuck",
            },
        }
        self.assertEqual(
            presence_api._classify_diagnostic_event("task_event", event),
            "error",
        )

    def test_mower_error_trigger_without_task_message_is_an_error(self) -> None:
        event = {"err_code": 2086}
        self.assertEqual(
            presence_api._classify_diagnostic_event("mower_error_code", event),
            "error",
        )

    def test_problem_robot_ranking_uses_real_errors_only(self) -> None:
        robot_a = "a" * 64
        robot_b = "b" * 64

        self._insert(
            serial_hash=robot_a,
            suffix="A001",
            model="Anthbot M9 Pro",
            code=2012,
            message="Task resumes",
        )
        self._insert(
            serial_hash=robot_a,
            suffix="A001",
            model="Anthbot M9 Pro",
            code=2012,
            message="The machine is stuck",
        )
        self._insert(
            serial_hash=robot_a,
            suffix="A001",
            model="Anthbot M9 Pro",
            code=2011,
            message="Recharge failed",
        )
        self._insert(
            serial_hash=robot_b,
            suffix="B002",
            model="Anthbot Genie 1000",
            code=2012,
            message="The machine is stuck",
        )

        stats = presence_api._error_stats_data()

        self.assertEqual(stats["total_diagnostic_events"], 4)
        self.assertEqual(stats["total_error_events"], 3)
        self.assertEqual(stats["status_events"], 1)
        self.assertEqual(stats["unclassified_events"], 0)

        ranking = stats["problem_robots"]
        self.assertEqual(ranking[0]["robot"], "Anthbot M9 Pro · …A001")
        self.assertEqual(ranking[0]["error_count"], 2)
        self.assertEqual(ranking[0]["unique_errors"], 2)
        self.assertEqual(ranking[1]["robot"], "Anthbot Genie 1000 · …B002")
        self.assertEqual(ranking[1]["error_count"], 1)


if __name__ == "__main__":
    unittest.main()
