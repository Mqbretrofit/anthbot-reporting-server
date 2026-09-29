from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

import entrypoint


class AnnouncementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "reporting.sqlite3"
        os.environ["ANTHBOT_DB_PATH"] = str(self.db_path)
        os.environ["ANTHBOT_ADMIN_TOKEN"] = "test-admin-token"
        self.client_ctx = TestClient(entrypoint.app, base_url="https://testserver")
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

    def payload(self, **changes) -> dict:
        value = {
            "announcement_id": "beta-news",
            "category": "release",
            "priority": "important",
            "show_popup": True,
            "published": True,
            "published_at": "2026-09-29T12:00:00+00:00",
            "expires_at": "2026-12-01T12:00:00+00:00",
            "min_version": "2.4.9.5-beta1",
            "max_version": None,
            "target_models": ["M9 Pro"],
            "target_installations": [],
            "translations": {
                "hu": {
                    "title": "Béta újdonság",
                    "body": "Megérkezett az üzenetközpont.",
                    "link_label": "Részletek",
                },
                "en": {
                    "title": "Beta news",
                    "body": "The message centre is ready.",
                    "link_label": "Learn more",
                },
            },
            "link_url": "https://anthbotmap.com/",
        }
        value.update(changes)
        return value

    def save(self, **changes):
        return self.client.post(
            "/api/anthbot/admin/announcements",
            headers=self.admin_headers,
            json=self.payload(**changes),
        )

    def test_admin_can_publish_and_public_feed_is_localized_and_targeted(self) -> None:
        created = self.save()
        self.assertEqual(created.status_code, 200, created.text)

        hidden_for_old_version = self.client.get(
            "/api/anthbot/announcements",
            params={"version": "2.4.9.4", "language": "hu", "models": "M9 Pro"},
        ).json()
        self.assertEqual(hidden_for_old_version["items"], [])

        hidden_for_other_model = self.client.get(
            "/api/anthbot/announcements",
            params={"version": "2.4.9.5-beta1", "language": "hu", "models": "Genie 1000"},
        ).json()
        self.assertEqual(hidden_for_other_model["items"], [])

        visible = self.client.get(
            "/api/anthbot/announcements",
            params={"version": "2.4.9.5-beta1", "language": "hu-HU", "models": "M9 Pro"},
        )
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json()["schema"], "anthbot-map-announcements-v1")
        self.assertEqual(visible.json()["items"][0]["title"], "Béta újdonság")
        self.assertTrue(visible.json()["items"][0]["show_popup"])
        self.assertEqual(visible.json()["items"][0]["link"], "https://anthbotmap.com/")
        self.assertIn("max-age=300", visible.headers["cache-control"])

    def test_language_falls_back_to_english(self) -> None:
        self.assertEqual(self.save(target_models=[], min_version=None).status_code, 200)
        body = self.client.get(
            "/api/anthbot/announcements",
            params={"version": "2.4.9.5-beta1", "language": "ja"},
        ).json()
        self.assertEqual(body["language"], "en")
        self.assertEqual(body["items"][0]["title"], "Beta news")

    def test_message_can_target_one_or_more_exact_installations(self) -> None:
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(
            self.save(target_models=[], target_installations=[first, second]).status_code,
            200,
        )
        hidden = self.client.get(
            "/api/anthbot/announcements",
            params={
                "version": "2.4.9.5-beta2",
                "language": "hu",
                "installation_id": "33333333-3333-4333-8333-333333333333",
            },
        ).json()
        self.assertEqual(hidden["items"], [])
        visible = self.client.get(
            "/api/anthbot/announcements",
            params={
                "version": "2.4.9.5-beta2",
                "language": "hu",
                "installation_id": second,
            },
        ).json()
        self.assertEqual(visible["items"][0]["id"], "beta-news")

    def test_target_choices_are_built_from_current_presence_rows(self) -> None:
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        for install_id, model in ((first, "M9 Pro"), (second, "Genie 1000"), (second, "M9 Pro")):
            response = self.client.post(
                "/api/anthbot/presence",
                json={"install_id": install_id, "version": "2.4.9.5-beta2", "model": model},
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(
            self.client.get("/api/anthbot/admin/announcement-targets").status_code,
            401,
        )
        response = self.client.get(
            "/api/anthbot/admin/announcement-targets", headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["installations"]), 2)
        models = {item["name"]: item["installation_count"] for item in body["models"]}
        self.assertEqual(models, {"M9 Pro": 2, "Genie 1000": 1})

    def test_invalid_target_installation_is_rejected(self) -> None:
        response = self.save(target_installations=["not-a-uuid"])
        self.assertEqual(response.status_code, 422)

    def test_existing_announcement_table_is_migrated_without_data_loss(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE announcements (
                    announcement_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    show_popup INTEGER NOT NULL DEFAULT 0,
                    published INTEGER NOT NULL DEFAULT 0,
                    published_at TEXT,
                    expires_at TEXT,
                    min_version TEXT,
                    max_version TEXT,
                    target_models_json TEXT NOT NULL,
                    translations_json TEXT NOT NULL,
                    link_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["target_installations"], [])
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(announcements)")}
        self.assertIn("target_installations_json", columns)

    def test_blank_required_translation_text_is_rejected(self) -> None:
        payload = self.payload()
        payload["translations"]["en"]["title"] = "   "
        response = self.client.post(
            "/api/anthbot/admin/announcements",
            headers=self.admin_headers,
            json=payload,
        )
        self.assertEqual(response.status_code, 422)

    def test_draft_is_hidden_and_admin_list_is_protected(self) -> None:
        self.assertEqual(self.save(published=False).status_code, 200)
        self.assertEqual(self.client.get("/api/anthbot/announcements").json()["items"], [])
        self.assertEqual(self.client.get("/api/anthbot/admin/announcements").status_code, 401)
        listed = self.client.get(
            "/api/anthbot/admin/announcements", headers=self.admin_headers
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["announcement_id"], "beta-news")

    def test_save_rejects_non_https_link_and_delete_requires_admin(self) -> None:
        rejected = self.save(link_url="javascript:alert(1)")
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(
            self.client.delete("/api/anthbot/admin/announcements/beta-news").status_code,
            401,
        )
        deleted = self.client.delete(
            "/api/anthbot/admin/announcements/beta-news", headers=self.admin_headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])

    def test_dashboard_requires_existing_admin_session(self) -> None:
        response = self.client.get("/dashboard/announcements", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.client.post("/dashboard/login", data={"token": "test-admin-token"})
        response = self.client.get("/dashboard/announcements")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Üzenetek és újdonságok", response.text)
        self.assertIn("/api/anthbot/admin/announcement-targets", response.text)
        self.assertIn('id="target_installations"', response.text)


if __name__ == "__main__":
    unittest.main()
