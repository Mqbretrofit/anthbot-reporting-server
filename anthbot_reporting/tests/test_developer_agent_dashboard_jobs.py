from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import developer_agent_api as agent
import developer_agent_dashboard as dashboard


class DeveloperAgentDashboardJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(Path(self.tempdir.name) / "reporting.sqlite3")
        os.environ["ANTHBOT_ADMIN_TOKEN"] = "test-admin-token"
        agent.init_developer_agent_tables()
        app = FastAPI()
        app.include_router(agent.router)
        app.include_router(dashboard.router)
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()
        self.installation_id = str(uuid4())
        with agent._db() as conn:
            conn.execute(
                """
                INSERT INTO developer_agent_installations (
                    installation_id, agent_key_sha256, server_enabled,
                    first_seen, last_seen, integration_version, models_json,
                    capabilities_json
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    self.installation_id,
                    "0" * 64,
                    "2026-09-12T00:00:00+00:00",
                    "2026-09-12T00:00:00+00:00",
                    "2.4.6.4",
                    '["Anthbot M9 Pro"]',
                    '{}',
                ),
            )

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

    def _insert_job(self, result_json: str | None) -> int:
        with agent._db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO developer_agent_jobs (
                    installation_id, action, target_model, params_json,
                    status, created_at, claimed_at, completed_at,
                    result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.installation_id,
                    "state_schema",
                    "Anthbot M9 Pro",
                    "{}",
                    "completed",
                    "2026-09-12T00:00:00+00:00",
                    "2026-09-12T00:00:01+00:00",
                    "2026-09-12T00:00:02+00:00",
                    result_json,
                    None,
                ),
            )
            return int(cursor.lastrowid)

    def test_dashboard_uses_lazy_result_loader(self) -> None:
        response = self.client.get("/dashboard/developer-agent", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn("anthbot-developer-agent-jobs-fix", response.text)
        self.assertIn("/api/anthbot/admin/developer-agent/jobs?limit=100", response.text)
        self.assertIn("/api/anthbot/admin/developer-agent/jobs/${encodeURIComponent(jobId)}", response.text)
        self.assertNotIn("window.downloadJobResult=downloadJobResult;load();setInterval(load,30000);", response.text)

    def test_single_job_endpoint_returns_result(self) -> None:
        job_id = self._insert_job(json.dumps({"status": "ok", "value": 42}))
        response = self.client.get(
            f"/api/anthbot/admin/developer-agent/jobs/{job_id}",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["result"]["value"], 42)

    def test_invalid_historical_result_does_not_break_single_job_endpoint(self) -> None:
        job_id = self._insert_job("{broken-json")
        response = self.client.get(
            f"/api/anthbot/admin/developer-agent/jobs/{job_id}",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertIn("result_error", body)
        self.assertNotIn("result", body)


if __name__ == "__main__":
    unittest.main()
