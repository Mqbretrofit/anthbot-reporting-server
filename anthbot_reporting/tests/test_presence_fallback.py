from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import app as server
import presence_api


class PresenceTelemetryFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(
            Path(self.tempdir.name) / "reporting.sqlite3"
        )
        server._init_db()
        presence_api.init_presence_tables()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _telemetry(
        self,
        *,
        installation_id: str | None = None,
        version: str = "2.4.9.2",
        models: list[str] | None = None,
    ) -> str:
        install_id = installation_id or str(uuid4())
        models = models or ["Anthbot M9 Pro"]
        counts = {model: models.count(model) for model in sorted(set(models))}
        payload = server.UsagePayload.model_validate(
            {
                "schema": server.USAGE_SCHEMA,
                "event": "heartbeat",
                "generated_at": presence_api._now(),
                "installation_id": install_id,
                "integration_version": version,
                "home_assistant_version": "2026.9.3",
                "country": "Hungary",
                "device_count": len(models),
                "models": sorted(set(models)),
                "model_counts": counts,
            }
        )
        server.ingest_telemetry(payload)
        return install_id

    def _presence(
        self,
        *,
        install_id: str | None = None,
        version: str = "2.4.9.2",
        model: str = "Anthbot M9 Pro",
    ) -> str:
        value = install_id or str(uuid4())
        payload = presence_api.PresencePayload.model_validate(
            {
                "install_id": value,
                "version": version,
                "model": model,
            }
        )
        presence_api.presence_heartbeat(payload)
        return value

    def test_telemetry_only_current_version_is_used_as_fallback(self) -> None:
        telemetry_id = self._telemetry()
        stats = presence_api._presence_stats_data()

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["items"][0]["install_id"], telemetry_id)
        self.assertEqual(stats["items"][0]["version"], "2.4.9.2")
        self.assertEqual(stats["items"][0]["models"], "Anthbot M9 Pro")

    def test_same_load_presence_and_telemetry_are_not_double_counted(self) -> None:
        self._presence()
        self._telemetry()
        stats = presence_api._presence_stats_data()

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_version"], [{"name": "2.4.9.2", "count": 1}])
        self.assertEqual(
            stats["by_model"],
            [{"name": "Anthbot M9 Pro", "count": 1}],
        )

    def test_pre_presence_versions_are_not_synthesized(self) -> None:
        self._telemetry(version="2.4.9.1")
        stats = presence_api._presence_stats_data()

        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["items"], [])

    def test_unrelated_same_model_installations_are_kept_separate(self) -> None:
        presence_id = self._presence()
        old_last_seen = (
            presence_api._now_dt() - timedelta(minutes=10)
        ).isoformat()
        conn = presence_api._connect()
        try:
            conn.execute(
                "UPDATE installation_presence SET last_seen=? WHERE install_id=?",
                (old_last_seen, presence_id),
            )
            conn.commit()
        finally:
            conn.close()

        self._telemetry()
        stats = presence_api._presence_stats_data()

        self.assertEqual(stats["total"], 2)


if __name__ == "__main__":
    unittest.main()
