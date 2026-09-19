from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import entrypoint
import store_api
import web_voice_installer
import web_voice_installer_anthbot


class WebVoiceInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(
            Path(self.tempdir.name) / "reporting.sqlite3"
        )
        os.environ["ANTHBOT_ADMIN_TOKEN"] = "test-admin-token"
        os.environ["ANTHBOT_VOICE_PACK_DIR"] = str(
            Path(self.tempdir.name) / "voice_packs"
        )
        os.environ["ANTHBOT_STORE_ENABLED"] = "true"
        os.environ["ANTHBOT_STRIPE_SECRET_KEY"] = "sk_test_example"
        os.environ["ANTHBOT_STRIPE_WEBHOOK_SECRET"] = "whsec_test_example"
        os.environ["ANTHBOT_STORE_LICENSE_SECRET"] = "license-secret-for-tests"
        os.environ["ANTHBOT_STRIPE_AUTOMATIC_TAX"] = "false"
        os.environ["ANTHBOT_WEB_VOICE_INSTALLER_ENABLED"] = "true"
        os.environ["ANTHBOT_PRIVACY_CONTROLLER_NAME"] = "Example Controller"
        os.environ["ANTHBOT_PRIVACY_CONTROLLER_ADDRESS"] = "Example Address"
        with web_voice_installer._LOCK:
            web_voice_installer._SESSIONS.clear()
            web_voice_installer._JOBS.clear()
            web_voice_installer._LOGIN_ATTEMPTS.clear()
        self.client_ctx = TestClient(entrypoint.app, base_url="https://testserver")
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        with web_voice_installer._LOCK:
            web_voice_installer._SESSIONS.clear()
            web_voice_installer._JOBS.clear()
            web_voice_installer._LOGIN_ATTEMPTS.clear()
        self.tempdir.cleanup()

    def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

    def _upload_paid_pack(self) -> dict:
        response = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={
                "file": (
                    "hu-noemi.pack",
                    b"web-installer-paid-pack",
                    "application/octet-stream",
                )
            },
            data={
                "language": "Magyar",
                "language_code": "hu",
                "version": "1.0.0",
                "community_id": "hu_noemi_standard_web",
                "variant_id": "noemi_standard_web",
                "variant_name": "Noémi · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
                "models": "Anthbot Genie 1000",
            },
        )
        self.assertEqual(response.status_code, 201)
        pack = response.json()["pack"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack['id']}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        return priced.json()["pack"]

    def _open_installer(self) -> None:
        page = self.client.get("/voice-installer")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("x-robots-tag"), "noindex, nofollow")
        self.assertIn("ANTHBOT hang telepítése Home Assistant nélkül", page.text)
        self.assertIn("anthbot_voice_store_client", self.client.cookies)

    def _login_genie(self) -> dict:
        devices = [
            {
                "serial": "25245HGD00050826",
                "serial_masked": "2524…0826",
                "alias": "Kert",
                "category": "Genie 1000",
                "supported": True,
            }
        ]
        with patch.object(
            web_voice_installer.anthbot,
            "login_and_devices",
            return_value=("temporary-anthbot-token", devices),
        ):
            response = self.client.post(
                "/api/anthbot/web-installer/login",
                json={
                    "username": "owner@example.test",
                    "password": "not-stored-password",
                    "area_code": "36",
                },
            )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_page_sets_functional_store_cookie_and_store_links_to_installer(self) -> None:
        self._open_installer()
        cookie = self.client.cookies.get("anthbot_voice_store_client")
        self.assertTrue(cookie)
        self.assertGreaterEqual(len(cookie), 32)

        store = self.client.get("/store")
        self.assertEqual(store.status_code, 200)
        self.assertIn("/voice-installer?pack=", store.text)

    def test_privacy_notice_covers_web_installer_in_all_site_languages(self) -> None:
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn(
            "Opcionális, böngészőből használható ANTHBOT hangtelepítő",
            html,
        )
        self.assertIn(
            "Optional browser-based ANTHBOT Voice Installer",
            html,
        )
        self.assertIn("20 perc", html)
        self.assertIn("HttpOnly", html)
        self.assertIn("AWS IoT", html)
        self.assertIn("GDPR 6. cikk (1) b)", html)
        self.assertIn("GDPR 6. cikk (1) f)", html)
        self.assertEqual(html.count("installerPrivacyTitle:"), 23)
        self.assertEqual(html.count("installerPrivacyCredentials:"), 23)
        self.assertEqual(html.count("installerPrivacyStore:"), 23)

    def test_genie_family_model_names_are_detected(self) -> None:
        for category in (
            "Genie 1000",
            "Genie_1000",
            "Genie-1000",
            "Genie1000",
            "ANTHBOT Genie 1000",
        ):
            self.assertTrue(
                web_voice_installer_anthbot.is_genie(category),
                category,
            )
        self.assertFalse(web_voice_installer_anthbot.is_genie("M9 Pro"))

    def test_login_keeps_password_out_of_session_and_auto_selects_single_genie(self) -> None:
        self._open_installer()
        body = self._login_genie()
        self.assertTrue(body["authenticated"])
        self.assertEqual(len(body["devices"]), 1)
        self.assertTrue(body["devices"][0]["supported"])
        self.assertEqual(body["devices"][0]["serial_masked"], "2524…0826")
        self.assertEqual(body["auto_device_id"], body["devices"][0]["device_id"])
        self.assertNotIn("serial", body["devices"][0])

        session_cookie = self.client.cookies.get("anthbot_voice_installer_session")
        self.assertTrue(session_cookie)
        with web_voice_installer._LOCK:
            stored = dict(web_voice_installer._SESSIONS[session_cookie])
        self.assertEqual(stored["access_token"], "temporary-anthbot-token")
        self.assertNotIn("password", stored)
        self.assertNotIn("username", stored)
        self.assertLessEqual(stored["expires_at"] - time.time(), 20 * 60 + 2)

    def test_non_genie_is_visible_but_install_is_rejected(self) -> None:
        self._open_installer()
        devices = [
            {
                "serial": "M9PROTEST0001",
                "serial_masked": "M9PR…0001",
                "alias": "M9 Pro",
                "category": "M9 Pro",
                "supported": False,
            }
        ]
        with patch.object(
            web_voice_installer.anthbot,
            "login_and_devices",
            return_value=("temporary-anthbot-token", devices),
        ):
            login = self.client.post(
                "/api/anthbot/web-installer/login",
                json={
                    "username": "owner@example.test",
                    "password": "secret",
                    "area_code": "36",
                },
            )
        self.assertEqual(login.status_code, 200)
        device_id = login.json()["devices"][0]["device_id"]

        catalog = self.client.get("/api/anthbot/web-installer/catalog")
        self.assertEqual(catalog.status_code, 200)
        free_pack = next(
            item
            for item in catalog.json()["packs"]
            if str(item.get("access", "free")).casefold() != "paid"
        )
        install = self.client.post(
            "/api/anthbot/web-installer/install",
            json={"device_id": device_id, "pack_id": free_pack["id"]},
        )
        self.assertEqual(install.status_code, 409)
        self.assertIn("Genie", install.json()["detail"])

    def test_paid_checkout_returns_to_installer_and_install_job_completes(self) -> None:
        pack = self._upload_paid_pack()
        self._open_installer()
        login = self._login_genie()
        device_id = login["auto_device_id"]
        client_token = self.client.cookies.get("anthbot_voice_store_client")
        client_id = store_api._client_id_from_token(client_token)

        checkout_session = {
            "id": "cs_test_web_installer_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/web-installer",
            "created": int(time.time()),
            "client_reference_id": pack["id"],
            "metadata": {
                "pack_id": pack["id"],
                "community_id": pack["community_id"],
                "store_client_id": client_id,
            },
            "payment_status": "unpaid",
            "status": "open",
            "amount_total": 799,
            "currency": "eur",
            "customer_details": None,
            "customer": None,
            "payment_intent": None,
        }
        with patch.object(
            store_api,
            "_create_checkout_session",
            return_value=checkout_session,
        ) as create_checkout:
            checkout = self.client.post(
                "/api/anthbot/web-installer/checkout",
                json={"pack_id": pack["id"]},
            )
        self.assertEqual(checkout.status_code, 200)
        kwargs = create_checkout.call_args.kwargs
        self.assertEqual(kwargs["client_id"], client_id)
        self.assertIn("/voice-installer?paid=1", kwargs["success_url_override"])
        self.assertIn(
            "session_id={CHECKOUT_SESSION_ID}",
            kwargs["success_url_override"],
        )
        self.assertIn("/voice-installer?cancelled=1", kwargs["cancel_url_override"])

        paid_session = {
            **checkout_session,
            "payment_status": "paid",
            "status": "complete",
            "customer_details": {"email": "buyer@example.test"},
            "customer": "cus_web_installer",
            "payment_intent": "pi_web_installer",
        }
        with patch.object(
            store_api,
            "_retrieve_checkout_session",
            return_value=paid_session,
        ):
            sync = self.client.post(
                "/api/anthbot/web-installer/purchase-sync/cs_test_web_installer_123",
                json={},
            )
        self.assertEqual(sync.status_code, 200)
        self.assertEqual(sync.json()["payment_status"], "paid")

        catalog = self.client.get("/api/anthbot/web-installer/catalog")
        self.assertEqual(catalog.status_code, 200)
        owned = next(
            item for item in catalog.json()["packs"] if item["id"] == pack["id"]
        )
        self.assertTrue(owned["owned"])

        def fake_install_voice(*, access_token, serial, pack, update) -> None:
            self.assertEqual(access_token, "temporary-anthbot-token")
            self.assertEqual(serial, "25245HGD00050826")
            self.assertIn("license=", pack["music_url"])
            self.assertEqual(pack["music_md5"], owned["music_md5"])
            update(progress=55, message="Downloading…")
            update(status="success", progress=100, message="Done")

        patcher = patch.object(
            web_voice_installer.anthbot,
            "install_voice",
            side_effect=fake_install_voice,
        )
        patcher.start()
        try:
            install = self.client.post(
                "/api/anthbot/web-installer/install",
                json={"device_id": device_id, "pack_id": pack["id"]},
            )
            self.assertEqual(install.status_code, 202)
            job_id = install.json()["job_id"]

            deadline = time.time() + 3
            job = None
            while time.time() < deadline:
                response = self.client.get(
                    f"/api/anthbot/web-installer/jobs/{job_id}"
                )
                self.assertEqual(response.status_code, 200)
                job = response.json()
                if job["status"] == "success":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["progress"], 100)
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
