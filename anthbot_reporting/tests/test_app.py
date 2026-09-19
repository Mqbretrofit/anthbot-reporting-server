from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import app as server


class ReportingServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(
            Path(self.tempdir.name) / "reporting.sqlite3"
        )
        os.environ["ANTHBOT_ADMIN_TOKEN"] = "test-admin-token"
        os.environ["ANTHBOT_VOICE_PACK_DIR"] = str(
            Path(self.tempdir.name) / "voice_packs"
        )
        self.client_ctx = TestClient(server.app, base_url="https://testserver")
        self.client = self.client_ctx.__enter__()
        self.installation_id = str(uuid4())

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _usage_payload(
        self,
        *,
        event: str = "installation",
        installation_id: str | None = None,
        country: str = "Hungary",
        models: list[str] | None = None,
        version: str = "2.4.5",
    ) -> dict:
        models = models or ["Anthbot Genie 1000", "M9 Pro"]
        counts = {model: models.count(model) for model in sorted(set(models))}
        return {
            "schema": server.USAGE_SCHEMA,
            "event": event,
            "generated_at": "2026-09-07T17:00:00+00:00",
            "installation_id": installation_id or self.installation_id,
            "integration_version": version,
            "home_assistant_version": "2026.9.1",
            "country": country,
            "device_count": len(models),
            "models": sorted(set(models)),
            "model_counts": counts,
        }

    def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

    def test_telemetry_upserts_installation_and_stats(self) -> None:
        response = self.client.post(
            "/api/anthbot/telemetry", json=self._usage_payload()
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])

        stats = self.client.get(
            "/api/anthbot/admin/stats", headers=self._admin_headers()
        )
        self.assertEqual(stats.status_code, 200)
        body = stats.json()
        self.assertEqual(body["installations"]["total"], 1)
        self.assertEqual(body["total_devices"], 2)
        self.assertEqual(body["by_country"][0]["name"], "Hungary")
        by_model = {item["name"]: item["count"] for item in body["by_model"]}
        self.assertEqual(by_model["M9 Pro"], 1)

    def test_usage_counts_must_be_consistent(self) -> None:
        payload = self._usage_payload()
        payload["device_count"] = 5
        response = self.client.post("/api/anthbot/telemetry", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_installation_filters_country_model_and_version(self) -> None:
        second_id = str(uuid4())
        self.client.post("/api/anthbot/telemetry", json=self._usage_payload())
        self.client.post(
            "/api/anthbot/telemetry",
            json=self._usage_payload(
                installation_id=second_id,
                country="Germany",
                models=["Anthbot M9 Pro"],
                version="2.4.6",
            ),
        )

        germany = self.client.get(
            "/api/anthbot/admin/installations?country=Germany",
            headers=self._admin_headers(),
        ).json()
        self.assertEqual(germany["count"], 1)
        self.assertEqual(germany["items"][0]["installation_id"], second_id)

        m9 = self.client.get(
            "/api/anthbot/admin/installations?model=Anthbot%20M9%20Pro",
            headers=self._admin_headers(),
        ).json()
        self.assertEqual(m9["count"], 1)
        self.assertEqual(m9["items"][0]["country"], "Germany")

        version = self.client.get(
            "/api/anthbot/admin/installations?version=2.4.5",
            headers=self._admin_headers(),
        ).json()
        self.assertEqual(version["count"], 1)
        self.assertEqual(version["items"][0]["country"], "Hungary")

    def test_diagnostics_returns_report_id(self) -> None:
        payload = {
            "schema": server.DIAGNOSTICS_SCHEMA,
            "generated_at": "2026-09-07T17:01:00+00:00",
            "installation_id": self.installation_id,
            "trigger": "mower_error_code",
            "report": {
                "schema": "anthbot-firmware-diagnostics-v1",
                "device": {"model": "M9 Pro", "serial_sha256": "abc"},
                "telemetry": {"err_code": 231},
            },
        }
        response = self.client.post("/api/anthbot/diagnostics", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["report_id"].startswith("AB-"))

    def test_diagnostics_rejects_credential_like_keys(self) -> None:
        payload = {
            "schema": server.DIAGNOSTICS_SCHEMA,
            "generated_at": "2026-09-07T17:01:00+00:00",
            "installation_id": self.installation_id,
            "trigger": "test",
            "report": {"nested": {"access_token": "should-never-arrive"}},
        }
        response = self.client.post("/api/anthbot/diagnostics", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_admin_requires_bearer_token_or_dashboard_cookie(self) -> None:
        response = self.client.get("/api/anthbot/admin/stats")
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            "/api/anthbot/admin/stats", headers=self._admin_headers()
        )
        self.assertEqual(response.status_code, 200)

    def test_dashboard_login_sets_secure_session_cookie(self) -> None:
        login_page = self.client.get("/dashboard")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("Admin felület", login_page.text)

        bad = self.client.post(
            "/dashboard/login", data={"token": "wrong"}, follow_redirects=False
        )
        self.assertEqual(bad.status_code, 401)

        good = self.client.post(
            "/dashboard/login",
            data={"token": "test-admin-token"},
            follow_redirects=False,
        )
        self.assertEqual(good.status_code, 303)
        self.assertIn(server._DASHBOARD_COOKIE, good.cookies)

        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Használati statisztikák és diagnosztika", dashboard.text)

        stats = self.client.get("/api/anthbot/admin/stats")
        self.assertEqual(stats.status_code, 200)

    def test_dashboard_does_not_expose_admin_token(self) -> None:
        page = self.client.get("/dashboard")
        self.assertNotIn("test-admin-token", page.text)
        self.client.post("/dashboard/login", data={"token": "test-admin-token"})
        page = self.client.get("/dashboard")
        self.assertNotIn("test-admin-token", page.text)

    def test_public_voice_pack_registry_contains_verified_hungarian_pack(self) -> None:
        response = self.client.get("/api/anthbot/voice-packs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema"], server.VOICE_PACKS_SCHEMA)
        self.assertEqual(len(body["packs"]), 1)

        pack = body["packs"][0]
        self.assertEqual(pack["language"], "Magyar")
        self.assertEqual(pack["language_code"], "hu")
        self.assertEqual(pack["music_package"], 3)
        self.assertEqual(pack["english_name"], "German")
        self.assertEqual(pack["sex"], "girl")
        self.assertEqual(pack["version"], "1.2.4")
        self.assertEqual(pack["variant_id"], "noemi_standard")
        self.assertEqual(pack["variant_name"], "Noémi (női) · Standard")
        self.assertEqual(pack["community_id"], "hu_noemi_standard")
        self.assertEqual(pack["voice_gender"], "female")
        self.assertEqual(pack["technical_slot"], "German_girl")
        self.assertEqual(
            pack["music_url"],
            "https://ha.mqbretrofithungary.online/local/anthbot-map-v2/girl_de-1.2.4",
        )
        self.assertEqual(
            pack["music_md5"], "74e1955f019aa422d446a0d367232826"
        )

    def test_public_cache_mirrors_official_voice_pack_and_reuses_md5(self) -> None:
        content = b"official-english-voice-pack"
        expected_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()

        def fake_fetch(source_url: str, music_md5: str, target: Path) -> int:
            self.assertEqual(source_url, "https://cdn.example.com/english.pack?sig=test")
            self.assertEqual(music_md5, expected_md5)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return len(content)

        payload = {
            "source_url": "https://cdn.example.com/english.pack?sig=test",
            "music_md5": expected_md5,
        }
        with patch.object(server, "_fetch_official_voice_pack", side_effect=fake_fetch) as fetch:
            first = self.client.post("/api/anthbot/voice-packs/cache-official", json=payload)
            second = self.client.post("/api/anthbot/voice-packs/cache-official", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["cache_hit"])
        self.assertTrue(second.json()["cache_hit"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first.json()["music_md5"], expected_md5)
        self.assertEqual(first.json()["size"], len(content))

        download_path = first.json()["music_url"].removeprefix("https://testserver")
        downloaded = self.client.get(download_path)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, content)

    def test_admin_can_seed_known_good_official_voice_cache(self) -> None:
        content = b"known-good-official-english-pack"
        expected_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()

        response = self.client.post(
            "/api/anthbot/admin/voice-packs/cache-official-upload",
            headers=self._admin_headers(),
            files={"file": ("2_girl_en-1.2.2", content, "application/octet-stream")},
            data={"expected_md5": expected_md5},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["uploaded"])
        self.assertEqual(body["music_md5"], expected_md5)
        self.assertEqual(body["size"], len(content))

        cached = self.client.post(
            "/api/anthbot/voice-packs/cache-official",
            json={
                "source_url": "https://cdn.example.com/english.pack",
                "music_md5": expected_md5,
            },
        )
        self.assertEqual(cached.status_code, 200)
        self.assertTrue(cached.json()["cache_hit"])

        download_path = cached.json()["music_url"].removeprefix("https://testserver")
        downloaded = self.client.get(download_path)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, content)

    def test_public_cache_rejects_invalid_md5(self) -> None:
        response = self.client.post(
            "/api/anthbot/voice-packs/cache-official",
            json={
                "source_url": "https://cdn.example.com/english.pack",
                "music_md5": "not-an-md5",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_admin_can_upload_serve_replace_and_delete_voice_pack(self) -> None:
        content_v1 = b"verified-czech-voice-pack-v1"
        response = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("cs.pack", content_v1, "application/octet-stream")},
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "1.0.0",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
                "english_name": "German",
                "sex": "girl",
                "music_package": "3",
                "models": "Anthbot Genie 1000",
            },
        )
        self.assertEqual(response.status_code, 201)
        response_v1 = response.json()
        self.assertTrue(response_v1["version_assigned_by_server"])
        self.assertEqual(response_v1["assigned_version"], "1.2.5")
        pack_v1 = response_v1["pack"]
        self.assertEqual(pack_v1["language"], "Čeština")
        self.assertEqual(pack_v1["community_id"], "cs_vlasta_standard")
        self.assertEqual(pack_v1["variant_id"], "vlasta_standard")
        self.assertEqual(pack_v1["variant_name"], "Vlasta (női) · Standard")
        self.assertEqual(pack_v1["voice_gender"], "female")
        self.assertEqual(pack_v1["technical_slot"], "German_girl")
        self.assertEqual(pack_v1["version"], "1.2.5")
        self.assertEqual(pack_v1["requested_version"], "1.0.0")
        self.assertEqual(pack_v1["version_source"], "reporting_server")
        self.assertEqual(
            pack_v1["music_md5"],
            hashlib.md5(content_v1, usedforsecurity=False).hexdigest(),
        )
        self.assertEqual(pack_v1["size"], len(content_v1))
        self.assertTrue(
            pack_v1["music_url"].startswith("https://testserver/voice-packs/")
        )

        download_path = pack_v1["music_url"].removeprefix("https://testserver")
        downloaded = self.client.get(download_path)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, content_v1)
        self.assertIn("immutable", downloaded.headers["cache-control"])

        funny = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("cs-funny.pack", b"czech-funny", "application/octet-stream")},
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "1.0.0",
                "community_id": "cs_vlasta_funny",
                "variant_id": "vlasta_funny",
                "variant_name": "Vlasta (női) · Vicces",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(funny.status_code, 201)
        funny_pack = funny.json()["pack"]
        self.assertEqual(funny_pack["version"], "1.2.6")
        self.assertEqual(funny_pack["community_id"], "cs_vlasta_funny")

        public = self.client.get("/api/anthbot/voice-packs").json()
        czech = [
            item for item in public["packs"] if item["language_code"] == "cs"
        ]
        self.assertEqual(len(czech), 2)
        self.assertEqual(
            {item["community_id"] for item in czech},
            {"cs_vlasta_standard", "cs_vlasta_funny"},
        )
        self.assertEqual(
            {item["version"] for item in czech},
            {"1.2.5", "1.2.6"},
        )

        content_v2 = b"verified-czech-voice-pack-v2"
        replaced = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("cs.pack", content_v2, "application/octet-stream")},
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "9.9.9",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(replaced.status_code, 201)
        pack_v2 = replaced.json()["pack"]
        self.assertEqual(pack_v2["version"], "1.2.7")
        self.assertEqual(pack_v2["requested_version"], "9.9.9")
        self.assertNotEqual(pack_v1["music_url"], pack_v2["music_url"])

        old_download = self.client.get(download_path)
        self.assertEqual(old_download.status_code, 404)

        public = self.client.get("/api/anthbot/voice-packs").json()
        czech = [
            item for item in public["packs"] if item["language_code"] == "cs"
        ]
        self.assertEqual(len(czech), 2)
        standard = [
            item for item in czech if item["community_id"] == "cs_vlasta_standard"
        ]
        self.assertEqual(len(standard), 1)
        self.assertEqual(standard[0]["version"], "1.2.7")

        deleted = self.client.delete(
            f"/api/anthbot/admin/voice-packs/{pack_v2['id']}",
            headers=self._admin_headers(),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])

        public = self.client.get("/api/anthbot/voice-packs").json()
        czech = [
            item for item in public["packs"] if item["language_code"] == "cs"
        ]
        self.assertEqual(len(czech), 1)
        self.assertEqual(czech[0]["community_id"], "cs_vlasta_funny")


    def test_voice_pack_upload_requires_admin_and_rejects_empty_file(self) -> None:
        unauthorized = self.client.post(
            "/api/anthbot/admin/voice-packs",
            files={"file": ("sk.pack", b"x", "application/octet-stream")},
            data={
                "language": "Slovenčina",
                "language_code": "sk",
                "version": "1.0.0",
            },
        )
        self.assertEqual(unauthorized.status_code, 401)

        empty = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("sk.pack", b"", "application/octet-stream")},
            data={
                "language": "Slovenčina",
                "language_code": "sk",
                "version": "1.0.0",
            },
        )
        self.assertEqual(empty.status_code, 422)

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()