from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import app as server
import presence_api


class AnalysisExportUsagePopulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(
            Path(self.tempdir.name) / "reporting.sqlite3"
        )
        server._init_db()
        presence_api.init_presence_tables()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_usage_statistics_count_only_opt_in_telemetry_installations(self) -> None:
        telemetry_id = str(uuid4())
        payload = server.UsagePayload.model_validate(
            {
                "schema": server.USAGE_SCHEMA,
                "event": "heartbeat",
                "generated_at": presence_api._now(),
                "installation_id": telemetry_id,
                "integration_version": "2.4.9.4",
                "home_assistant_version": "2026.9.3",
                "country": "Hungary",
                "device_count": 2,
                "models": ["Anthbot Genie 1000", "Anthbot M9 Pro"],
                "model_counts": {
                    "Anthbot Genie 1000": 1,
                    "Anthbot M9 Pro": 1,
                },
            }
        )
        server.ingest_telemetry(payload)

        # Minimal presence heartbeat is a different population and must not
        # increase the opt-in telemetry installation count.
        presence_api.presence_heartbeat(
            presence_api.PresencePayload.model_validate(
                {
                    "install_id": str(uuid4()),
                    "version": "2.4.9.4",
                    "model": "Anthbot Genie 600",
                }
            )
        )

        stats = presence_api._usage_stats_data()
        self.assertEqual(stats["source"], "opt_in_telemetry_installations")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["total_robots"], 2)
        self.assertEqual(
            {item["name"]: item["count"] for item in stats["by_model"]},
            {"Anthbot Genie 1000": 1, "Anthbot M9 Pro": 1},
        )

    def test_analysis_export_keeps_usage_and_presence_populations_separate(self) -> None:
        payload = server.UsagePayload.model_validate(
            {
                "schema": server.USAGE_SCHEMA,
                "event": "heartbeat",
                "generated_at": presence_api._now(),
                "installation_id": str(uuid4()),
                "integration_version": "2.4.9.4",
                "home_assistant_version": "2026.9.3",
                "country": "Hungary",
                "device_count": 1,
                "models": ["Anthbot M9 Pro"],
                "model_counts": {"Anthbot M9 Pro": 1},
            }
        )
        server.ingest_telemetry(payload)

        response = presence_api.analysis_export()
        body = json.loads(response.body)
        self.assertIn("usage_statistics", body)
        self.assertIn("presence", body)
        self.assertEqual(
            body["usage_statistics"]["source"],
            "opt_in_telemetry_installations",
        )
        self.assertEqual(
            body["presence"]["source"],
            "presence_heartbeat_with_telemetry_fallback",
        )


if __name__ == "__main__":
    unittest.main()
