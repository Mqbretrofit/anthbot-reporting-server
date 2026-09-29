from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

import entrypoint


class AnnouncementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(Path(self.tempdir.name) / "reporting.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
