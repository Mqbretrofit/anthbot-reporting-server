from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import smtplib
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import entrypoint
import store_api
import store_accounts


class VoiceStoreTests(unittest.TestCase):
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
        os.environ["ANTHBOT_PRIVACY_CONTROLLER_NAME"] = "Example Controller"
        os.environ["ANTHBOT_PRIVACY_CONTROLLER_ADDRESS"] = "Example Address, Hungary"
        os.environ["ANTHBOT_PRIVACY_CONTACT_EMAIL"] = "privacy@example.test"
        os.environ["ANTHBOT_PRIVACY_CONTACT_PHONE"] = "+36 1 000 0000"
        os.environ["ANTHBOT_SITE_ANALYTICS_ENABLED"] = "true"
        os.environ["ANTHBOT_SMTP_HOST"] = "smtp.example.test"
        os.environ["ANTHBOT_SMTP_PORT"] = "587"
        os.environ["ANTHBOT_SMTP_USERNAME"] = "store@example.test"
        os.environ["ANTHBOT_SMTP_PASSWORD"] = "test-password"
        os.environ["ANTHBOT_SMTP_FROM_EMAIL"] = "store@example.test"
        os.environ["ANTHBOT_SMTP_FROM_NAME"] = "ANTHBOT Map"
        os.environ["ANTHBOT_SMTP_STARTTLS"] = "true"
        os.environ["ANTHBOT_SMTP_SSL"] = "false"
        self.client_ctx = TestClient(entrypoint.app, base_url="https://testserver")
        self.client = self.client_ctx.__enter__()
        self.purchase_email_patcher = patch.object(
            store_accounts,
            "send_purchase_installation_email",
        )
        self.purchase_email_sender = self.purchase_email_patcher.start()

    def tearDown(self) -> None:
        self.purchase_email_patcher.stop()
        self.client_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

    def _login_store_account(self, email: str = "buyer@example.com") -> dict:
        captured: dict[str, str] = {}

        def fake_send(target: str, code: str, language: str) -> None:
            captured["email"] = target
            captured["code"] = code
            captured["language"] = language

        with patch.object(store_accounts, "_send_login_code", side_effect=fake_send):
            requested = self.client.post(
                "/api/anthbot/store/account/request-code",
                json={"email": email, "language": "hu"},
            )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(captured["email"], email.casefold())
        self.assertRegex(captured["code"], r"^\d{6}$")

        verified = self.client.post(
            "/api/anthbot/store/account/verify-code",
            json={
                "email": email,
                "code": captured["code"],
                "language": "hu",
            },
        )
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["authenticated"])
        body = verified.json()
        with store_api.core._db() as conn:
            row = conn.execute(
                "SELECT user_id, preferred_language FROM store_users WHERE email = ?",
                (email.casefold(),),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["preferred_language"], "hu")
        body["_user_id"] = str(row["user_id"])
        return body

    def test_account_email_failure_returns_safe_smtp_diagnostic(self) -> None:
        with patch.object(
            store_accounts,
            "_send_login_code",
            side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials secret"),
        ):
            response = self.client.post(
                "/api/anthbot/store/account/request-code",
                json={"email": "diagnostic@example.test", "language": "en"},
            )

        self.assertEqual(response.status_code, 424)
        self.assertEqual(
            response.json()["detail"],
            "SMTP authentication failed (code 535)",
        )
        self.assertNotIn("bad credentials", response.text)
        self.assertNotIn("secret", response.text)
        with store_api.core._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM store_login_codes WHERE email = ?",
                ("diagnostic@example.test",),
            ).fetchone()
        self.assertIsNone(row)

    def test_account_email_network_failure_is_sanitized(self) -> None:
        with patch.object(
            store_accounts,
            "_send_login_code",
            side_effect=OSError(111, "connection refused internal-host"),
        ):
            response = self.client.post(
                "/api/anthbot/store/account/request-code",
                json={"email": "network@example.test", "language": "en"},
            )

        self.assertEqual(response.status_code, 424)
        self.assertEqual(response.json()["detail"], "SMTP network error (errno 111)")
        self.assertNotIn("internal-host", response.text)

    def _link_store_account_to_pair(self, pair_code: str) -> dict:
        linked = self.client.post(
            "/api/anthbot/store/account/link-map",
            json={"pair_code": pair_code},
        )
        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.json()["linked"])
        return linked.json()

    def _upload_pack(self) -> dict:
        content = b"paid-community-pack"
        response = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("cs.pack", content, "application/octet-stream")},
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "1.0.0",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["pack"]

    def test_purchase_success_page_contains_map_installation_guide(self) -> None:
        response = self.client.get("/store/success?session_id=cs_example_123")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="map-guide"', html)
        self.assertIn('id="return-ha"', html)
        self.assertIn('id="email-note"', html)
        self.assertIn('"guideTitle":"Install your purchased voice"', html)
        self.assertIn('"guideTitle":"A megvásárolt hang telepítése"', html)
        self.assertIn('"returnHa":"Vissza a Home Assistantba"', html)
        self.assertIn("data.installation_email_sent", html)
        self.assertIn("window.close()", html)

    def test_paid_map_order_sends_installation_email_once_with_license(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("mailbuyer@example.com")

        client_token = "E" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
        self._link_store_account_to_pair(pair_code)
        client_id = store_api._client_id_from_token(client_token)

        paid_session = {
            "id": "cs_test_install_email_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/install-email-test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": client_id,
                "store_user_id": account["_user_id"],
                "entitlement_scope": "map",
            },
            "payment_status": "paid",
            "status": "complete",
            "amount_total": 799,
            "currency": "eur",
            "customer_details": {"email": "mailbuyer@example.com"},
            "customer": "cus_install_email",
            "payment_intent": "pi_install_email",
        }
        stored = store_api._upsert_order_from_session(paid_session)
        self.purchase_email_sender.reset_mock()

        first = store_api._send_purchase_installation_email_once(stored)
        second = store_api._send_purchase_installation_email_once(stored)

        self.assertTrue(first)
        self.assertFalse(second)
        self.purchase_email_sender.assert_called_once()
        args, kwargs = self.purchase_email_sender.call_args
        self.assertEqual(args[0], "mailbuyer@example.com")
        self.assertEqual(kwargs["language"], "hu")
        self.assertTrue(kwargs["map_linked"])
        self.assertIn("Vlasta", kwargs["pack_name"])
        self.assertTrue(kwargs["license_key"].startswith("abv1."))
        self.assertIn(
            "/store/success?session_id=cs_test_install_email_123",
            kwargs["success_url"],
        )

        with store_api.core._db() as conn:
            row = conn.execute(
                """
                SELECT installation_email_status, installation_email_sent_at
                FROM store_orders
                WHERE stripe_session_id = ?
                """,
                ("cs_test_install_email_123",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["installation_email_status"], "sent")
        self.assertTrue(row["installation_email_sent_at"])

        public = self.client.get(
            "/api/anthbot/store/orders/cs_test_install_email_123"
        )
        self.assertEqual(public.status_code, 200)
        self.assertTrue(public.json()["installation_email_sent"])
        self.purchase_email_sender.assert_called_once()

    def test_store_has_visible_top_navigation_in_all_languages(self) -> None:
        response = self.client.get("/store")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('<nav class="navlinks"', html)
        self.assertIn('class="home-link" href="/" data-i18n="homeNav"', html)
        self.assertIn('href="/refunds" data-i18n="refund"', html)
        self.assertIn('href="/terms" data-i18n="terms"', html)
        self.assertIn('href="/privacy" data-i18n="privacy"', html)
        self.assertIn('"hu":"← Főoldal"', html)
        self.assertIn('"en":"← Home"', html)
        self.assertIn('"de":"← Startseite"', html)
        self.assertIn('"zh-CN":"← 首页"', html)
        self.assertIn('"km":"← ទំព័រដើម"', html)
        self.assertEqual(html.count('homeNav=label'), 1)

    def test_store_groups_filters_and_two_audio_previews(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for filename, content in (
                ("voice/A004.mp3", b"sample-one-mp3"),
                ("voice/A005.mp3", b"sample-two-mp3"),
                ("voice/A006.mp3", b"not-public-preview"),
            ):
                info = tarfile.TarInfo(filename)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

        uploaded = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={
                "file": (
                    "preview.pack",
                    payload.getvalue(),
                    "application/octet-stream",
                )
            },
            data={
                "language": "Magyar",
                "language_code": "hu-HU",
                "version": "1.0.0",
                "community_id": "hu_preview_standard",
                "variant_id": "preview_standard",
                "variant_name": "Noémi · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(uploaded.status_code, 201)
        pack_id = uploaded.json()["pack"]["id"]

        catalog = self.client.get("/api/anthbot/store/voice-packs")
        self.assertEqual(catalog.status_code, 200)
        pack = next(item for item in catalog.json()["packs"] if item["id"] == pack_id)
        self.assertEqual(len(pack["preview_samples"]), 2)
        self.assertTrue(pack["preview_samples"][0]["url"].endswith("/preview/1"))
        self.assertTrue(pack["preview_samples"][1]["url"].endswith("/preview/2"))

        first = self.client.get(
            pack["preview_samples"][0]["url"].removeprefix("https://testserver")
        )
        second = self.client.get(
            pack["preview_samples"][1]["url"].removeprefix("https://testserver")
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.headers["content-type"], "audio/mpeg")
        self.assertEqual(first.content, b"sample-one-mp3")
        self.assertEqual(second.content, b"sample-two-mp3")
        self.assertEqual(
            self.client.get(
                f"/api/anthbot/store/voice-packs/{pack_id}/preview/3"
            ).status_code,
            404,
        )

        store = self.client.get("/store")
        self.assertEqual(store.status_code, 200)
        html = store.text
        self.assertIn('id="voice-search"', html)
        self.assertIn('id="filter-language"', html)
        self.assertIn('id="filter-gender"', html)
        self.assertIn('id="filter-model"', html)
        self.assertIn('id="filter-access"', html)
        self.assertIn("language-groups", html)
        self.assertIn("data-preview-url", html)
        self.assertIn('"Minta 1"', html)
        self.assertIn('"Minta 2"', html)
        self.assertEqual(html.count("filtersTitle:"), 23)

    def test_public_seo_targets_anthbotmap_domain(self) -> None:
        canonical_paths = {
            "/": "https://anthbotmap.com/",
            "/store": "https://anthbotmap.com/store",
            "/privacy": "https://anthbotmap.com/privacy",
            "/terms": "https://anthbotmap.com/terms",
            "/refunds": "https://anthbotmap.com/refunds",
        }
        for path, canonical in canonical_paths.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                f'<link rel="canonical" href="{canonical}">',
                response.text,
            )
            self.assertIn(
                '<meta name="robots" content="index,follow,',
                response.text,
            )

        home = self.client.get("/")
        self.assertIn(
            "<title>ANTHBOT Map for Home Assistant – Maps, Zones &amp; Voice Packs</title>",
            home.text,
        )
        self.assertIn("support@anthbotmap.com", home.text)
        self.assertIn('"@type":"SoftwareApplication"', home.text)
        self.assertIn('"name":"ANTHBOT Map"', home.text)
        self.assertIn(
            '"downloadUrl":"https://github.com/Mqbretrofit/ha-anthbot-map-v2"',
            home.text,
        )

        store = self.client.get("/store")
        self.assertIn(
            "<title>ANTHBOT Voice Packs &amp; Custom Voices | ANTHBOT Map</title>",
            store.text,
        )
        self.assertIn(
            "Browse ANTHBOT community voice packs and request custom mower voices",
            store.text,
        )

        success = self.client.get("/store/success")
        self.assertEqual(success.status_code, 200)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', success.text)
        self.assertEqual(
            success.headers.get("x-robots-tag"),
            "noindex, nofollow",
        )

    def test_public_pages_have_branded_social_preview_metadata(self) -> None:
        expected_image = (
            "https://anthbotmap.com/brand/anthbot-map-logo.webp?v=2"
        )
        for path in (
            "/",
            "/store",
            "/privacy",
            "/terms",
            "/refunds",
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn(
                f'<meta property="og:image" content="{expected_image}">',
                html,
            )
            self.assertIn(
                '<meta property="og:image:alt" content="ANTHBOT Map">',
                html,
            )
            self.assertIn(
                '<meta name="twitter:card" content="summary_large_image">',
                html,
            )
            self.assertIn(
                f'<meta name="twitter:image" content="{expected_image}">',
                html,
            )

    def test_robots_sitemap_and_legacy_public_redirects(self) -> None:
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Sitemap: https://anthbotmap.com/sitemap.xml", robots.text)
        self.assertIn("Disallow: /api/", robots.text)

        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn("<loc>https://anthbotmap.com/</loc>", sitemap.text)
        self.assertIn("<loc>https://anthbotmap.com/store</loc>", sitemap.text)
        self.assertNotIn("reports.mqbretrofithungary.online", sitemap.text)

        redirect = self.client.get(
            "/store?cancelled=1",
            headers={"host": "reports.mqbretrofithungary.online"},
            follow_redirects=False,
        )
        self.assertEqual(redirect.status_code, 301)
        self.assertEqual(
            redirect.headers["location"],
            "https://anthbotmap.com/store?cancelled=1",
        )

        api = self.client.get(
            "/api/anthbot/voice-packs",
            headers={"host": "reports.mqbretrofithungary.online"},
            follow_redirects=False,
        )
        self.assertEqual(api.status_code, 200)

    def test_seo_landing_pages_are_indexable_and_factual(self) -> None:
        pages = {
            "/home-assistant": (
                "ANTHBOT Home Assistant Integration | ANTHBOT Map",
                "native lawn_mower entity",
            ),
            "/models/genie-1000": (
                "ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map",
                "directly hardware-tested",
            ),
            "/models/m9-pro": (
                "ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map",
                "directly hardware-tested",
            ),
            "/models/mgc1000": (
                "ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map",
                "Unverified Pion/MGC setting writes",
            ),
            "/voice-packs": (
                "ANTHBOT Voice Packs for Genie Mowers | ANTHBOT Map",
                "compatible ANTHBOT Genie robots",
            ),
        }
        for path, (title, factual_marker) in pages.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"<title>{title}</title>", response.text)
            self.assertIn(
                f'<link rel="canonical" href="https://anthbotmap.com{path}">',
                response.text,
            )
            self.assertIn(
                '<meta name="robots" content="index,follow,',
                response.text,
            )
            self.assertIn(factual_marker, response.text)
            self.assertIn('"@type":"WebPage"', response.text)
            self.assertIn('<script src="/site-analytics.js?v=1"></script>', response.text)

        unknown = self.client.get("/models/not-a-real-model")
        self.assertEqual(unknown.status_code, 404)

        home = self.client.get("/")
        self.assertIn('href="/home-assistant"', home.text)
        self.assertIn('href="/models/genie-1000"', home.text)
        self.assertIn('href="/models/m9-pro"', home.text)
        self.assertIn('href="/models/mgc1000"', home.text)
        self.assertIn('href="/voice-packs"', home.text)

    def test_seo_sitemap_contains_topic_pages(self) -> None:
        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        for path in (
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            self.assertIn(
                f"<loc>https://anthbotmap.com{path}</loc>",
                sitemap.text,
            )

        legacy = self.client.get(
            "/models/m9-pro",
            headers={"host": "reports.mqbretrofithungary.online"},
            follow_redirects=False,
        )
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(
            legacy.headers["location"],
            "https://anthbotmap.com/models/m9-pro",
        )

    def test_seo_landing_pages_match_main_site_visual_shell(self) -> None:
        for path in (
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn('<div class="bg-grid"></div>', html)
            self.assertIn('<nav class="nav">', html)
            self.assertIn('class="brand anthbot-brand"', html)
            self.assertIn('/brand/anthbot-map-logo.webp?v=2', html)
            self.assertIn('class="hero"', html)
            self.assertIn('class="topic-stage glass"', html)
            self.assertIn('class="feature-grid"', html)
            self.assertIn('<footer class="footer">', html)
            self.assertIn('href="/#anthbot-map"', html)
            self.assertIn('href="/#features"', html)
            self.assertIn('href="/#models"', html)
            self.assertIn('href="/#support"', html)

    def test_addon_dockerfile_packages_brand_assets(self) -> None:
        dockerfile = Path(__file__).parents[1] / "Dockerfile"
        content = dockerfile.read_text(encoding="utf-8")
        self.assertIn("COPY assets ./assets", content)
        self.assertIn(
            "COPY public_site_i18n.js ./public_site_i18n.js",
            content,
        )

    def test_public_pages_expose_custom_anthbot_map_branding(self) -> None:
        for path in (
            "/",
            "/store",
            "/privacy",
            "/terms",
            "/refunds",
            "/store/success",
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                '<link rel="icon" href="/favicon.png?v=3" type="image/png">',
                response.text,
            )
            self.assertIn(
                '<link rel="apple-touch-icon" href="/favicon.png?v=3">',
                response.text,
            )
            self.assertIn('/brand/anthbot-map-logo.webp?v=2', response.text)
            self.assertIn('alt="ANTHBOT Map"', response.text)

        png = self.client.get("/favicon.png")
        self.assertEqual(png.status_code, 200)
        self.assertTrue(png.headers["content-type"].startswith("image/png"))
        self.assertGreater(len(png.content), 5000)
        self.assertTrue(png.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("immutable", png.headers.get("cache-control", ""))

        logo = self.client.get("/brand/anthbot-map-logo.webp")
        self.assertEqual(logo.status_code, 200)
        self.assertTrue(logo.headers["content-type"].startswith("image/webp"))
        self.assertGreater(len(logo.content), 5000)
        self.assertTrue(logo.content.startswith(b"RIFF"))
        self.assertEqual(logo.content[8:12], b"WEBP")

        for legacy_path in ("/favicon.ico", "/favicon.svg"):
            legacy = self.client.get(legacy_path, follow_redirects=False)
            self.assertEqual(legacy.status_code, 307)
            self.assertEqual(legacy.headers["location"], "/favicon.png?v=3")

    def test_public_pages_have_web_app_manifest(self) -> None:
        for path in (
            "/",
            "/store",
            "/privacy",
            "/terms",
            "/refunds",
            "/store/success",
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                '<link rel="manifest" href="/site.webmanifest?v=1">',
                response.text,
            )
            self.assertIn(
                '<meta name="application-name" content="ANTHBOT Map">',
                response.text,
            )

        manifest = self.client.get("/site.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertTrue(
            manifest.headers["content-type"].startswith(
                "application/manifest+json"
            )
        )
        body = manifest.json()
        self.assertEqual(body["name"], "ANTHBOT Map")
        self.assertEqual(body["start_url"], "/")
        self.assertEqual(body["scope"], "/")
        self.assertEqual(body["display"], "standalone")
        self.assertEqual(body["theme_color"], "#081017")
        self.assertEqual(body["background_color"], "#081017")
        self.assertEqual(body["icons"][0]["sizes"], "192x192")
        self.assertEqual(body["icons"][1]["sizes"], "512x512")
        self.assertEqual(body["icons"][1]["src"], "/app-icon-512.svg?v=1")
        self.assertEqual(body["shortcuts"][0]["url"], "/store")

        icon = self.client.get("/app-icon-512.svg")
        self.assertEqual(icon.status_code, 200)
        self.assertTrue(icon.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn('width="512" height="512"', icon.text)
        self.assertIn("data:image/png;base64,", icon.text)
        self.assertIn("immutable", icon.headers.get("cache-control", ""))

    def test_public_pages_share_compact_typography(self) -> None:
        for path in (
            "/",
            "/store",
            "/privacy",
            "/terms",
            "/refunds",
            "/store/success",
            "/home-assistant",
            "/models/genie-1000",
            "/models/m9-pro",
            "/models/mgc1000",
            "/voice-packs",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="anthbot-compact-typography"', response.text)
            self.assertIn("html{font-size:14px}", response.text)
            self.assertIn(
                "font-size:clamp(1.85rem,3.7vw,2.95rem)!important",
                response.text,
            )

    def test_seo_pages_follow_site_language(self) -> None:
        genie = self.client.get("/models/genie-1000")
        self.assertEqual(genie.status_code, 200)
        self.assertIn('id="site-language"', genie.text)
        self.assertIn('<option value="hu">Magyar</option>', genie.text)
        self.assertIn('<option value="en">English</option>', genie.text)
        self.assertIn('"Támogatott fűnyíró"', genie.text)
        self.assertIn('"Közvetlen hardveres ellenőrzés"', genie.text)
        self.assertIn('"Vissza az ANTHBOT Maphez"', genie.text)
        self.assertIn('localStorage.getItem("mqb-site-language")', genie.text)
        self.assertIn('data-i18n="pageHeading"', genie.text)

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('id="anthbot-seo-topics-i18n"', home.text)
        self.assertIn('"ANTHBOT Map témakörök"', home.text)
        self.assertIn('"ANTHBOT hangcsomagok"', home.text)
        self.assertIn('data-topic-i18n="title"', home.text)

    def test_main_site_has_all_23_languages(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        languages = (
            ("hu", "Magyar"), ("en", "English"), ("de", "Deutsch"),
            ("fr", "Français"), ("es", "Español"), ("it", "Italiano"),
            ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"),
            ("cs", "Čeština"), ("sk", "Slovenčina"), ("ro", "Română"),
            ("da", "Dansk"), ("sv", "Svenska"), ("no", "Norsk"),
            ("fi", "Suomi"), ("zh-CN", "简体中文"), ("zh-TW", "繁體中文"),
            ("tr", "Türkçe"), ("th", "ไทย"), ("vi", "Tiếng Việt"),
            ("ko", "한국어"), ("km", "ខ្មែរ"),
        )
        for code, label in languages:
            self.assertIn(
                f'<option value="{code}">{label}</option>',
                html,
            )
        self.assertIn(
            '<script src="/public-site-i18n.js?v=1"></script>',
            html,
        )

        i18n = self.client.get("/public-site-i18n.js")
        self.assertEqual(i18n.status_code, 200)
        script = i18n.text
        self.assertIn(
            'const supported=["hu","en","de","fr","es","it","pt","nl","pl","cs","sk","ro","da","sv","no","fi","zh-CN","zh-TW","tr","th","vi","ko","km"]',
            script,
        )
        self.assertIn('"pt":{"__title":"ANTHBOT Map', script)
        self.assertIn('"zh-CN":{"__title":"ANTHBOT Map', script)
        self.assertIn('"km":{"__title":"ANTHBOT Map', script)
        self.assertIn('"Explorar temas do ANTHBOT Map"', html)
        self.assertIn('"探索 ANTHBOT Map 主题"', html)
        self.assertIn('"ស្វែងយល់ប្រធានបទ ANTHBOT Map"', html)

    def test_main_site_translation_tables_have_full_key_parity(self) -> None:
        response = self.client.get("/public-site-i18n.js")
        self.assertEqual(response.status_code, 200)
        script = response.text
        start = script.index("const translations=") + len("const translations=")
        end = script.index(";\n  Object.assign(translations,", start)
        base = json.loads(script[start:end])
        extra_start = (
            script.index("Object.assign(translations,", end)
            + len("Object.assign(translations,")
        )
        extra_end = script.index(");\n  const supported=", extra_start)
        extra = json.loads(script[extra_start:extra_end])

        translations = {**base, **extra}
        reference_keys = set(base["hu"])
        expected_languages = {
            "hu", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
            "ro", "da", "sv", "no", "fi", "zh-CN", "zh-TW", "tr", "th",
            "vi", "ko", "km",
        }
        self.assertEqual(set(translations), expected_languages)
        self.assertEqual(len(reference_keys), 167)
        for language, table in translations.items():
            self.assertEqual(
                set(table),
                reference_keys,
                f"incomplete main-site translation table: {language}",
            )

    def test_main_site_uses_cacheable_assets_instead_of_inline_payloads(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        html = home.text
        self.assertNotIn("data:image/", html)
        self.assertNotIn("const translations=", html)
        self.assertIn('/assets/genie-mower.png?v=1', html)
        self.assertIn('/assets/m-series-mower.png?v=1', html)
        self.assertIn(
            '<script src="/public-site-i18n.js?v=1"></script>',
            html,
        )
        self.assertIn('fetchpriority="high"', html)
        self.assertIn('loading="lazy"', html)
        self.assertLess(len(home.content), 150_000)

        i18n = self.client.get("/public-site-i18n.js")
        self.assertEqual(i18n.status_code, 200)
        self.assertTrue(
            i18n.headers["content-type"].startswith(
                "application/javascript"
            )
        )
        self.assertIn("immutable", i18n.headers.get("cache-control", ""))
        self.assertIn("const translations=", i18n.text)

        genie = self.client.get("/assets/genie-mower.png")
        self.assertEqual(genie.status_code, 200)
        self.assertTrue(genie.headers["content-type"].startswith("image/png"))
        self.assertTrue(genie.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(genie.content), 100_000)
        self.assertIn("immutable", genie.headers.get("cache-control", ""))

        m_series = self.client.get("/assets/m-series-mower.png")
        self.assertEqual(m_series.status_code, 200)
        self.assertTrue(
            m_series.headers["content-type"].startswith("image/png")
        )
        self.assertTrue(m_series.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(m_series.content), 10_000)
        self.assertIn("immutable", m_series.headers.get("cache-control", ""))

    def test_privacy_page_has_gdpr_information_and_23_languages(self) -> None:
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Example Controller", html)
        self.assertIn("Example Address, Hungary", html)
        self.assertIn("privacy@example.test", html)
        self.assertIn("+36 1 000 0000", html)
        self.assertNotIn("__CONTROLLER_NAME__", html)
        self.assertNotIn("__PRIVACY_CONFIG_WARNING_TEXT__", html)

        for language in (
            "en", "hu", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
            "ro", "da", "sv", "no", "fi", "zh-CN", "zh-TW", "tr", "th", "vi",
            "ko", "km",
        ):
            marker = f'"{language}":' if "-" in language else f"{language}:"
            self.assertIn(marker, html)

        self.assertIn("GDPR 6(1)(b)", html)
        self.assertIn("GDPR 6. cikk (1) b)", html)
        self.assertIn("Stripe Payments Europe Limited", html)
        self.assertIn("EU-US Data Privacy Framework", html)
        self.assertIn("Standard Contractual Clauses", html)
        self.assertIn("Nemzeti Adatvédelmi és Információszabadság Hatóság", html)
        self.assertIn("/api/anthbot/privacy-requests", html)
        self.assertIn("7 days", html)
        self.assertIn("7 nap", html)

    def test_public_pages_include_first_party_analytics_without_tracker_ids(self) -> None:
        for path in ("/", "/store", "/privacy", "/terms", "/refunds"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                '<script src="/site-analytics.js?v=1"></script>',
                response.text,
            )
        script = self.client.get("/site-analytics.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/anthbot/site/visit", script.text)
        self.assertIn('credentials: "omit"', script.text)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn("document.cookie", script.text)
        self.assertNotIn("userAgent", script.text)
        self.assertIn("globalPrivacyControl", script.text)
        self.assertIn("doNotTrack", script.text)

    def test_site_analytics_deduplicates_daily_visitors_without_storing_raw_ip(self) -> None:
        first_headers = {"CF-Connecting-IP": "203.0.113.10"}
        second_headers = {"CF-Connecting-IP": "203.0.113.11"}

        for _ in range(3):
            response = self.client.post(
                "/api/anthbot/site/visit",
                headers=first_headers,
                json={"path": "/store", "language": "hu"},
            )
            self.assertEqual(response.status_code, 204)

        response = self.client.post(
            "/api/anthbot/site/visit",
            headers=second_headers,
            json={"path": "/privacy", "language": "en"},
        )
        self.assertEqual(response.status_code, 204)

        analytics = self.client.get(
            "/api/anthbot/admin/site-analytics?days=30",
            headers=self._admin_headers(),
        )
        self.assertEqual(analytics.status_code, 200)
        body = analytics.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["today"]["views"], 4)
        self.assertEqual(body["today"]["unique_visitors"], 2)
        self.assertEqual(body["views_7d"], 4)
        self.assertEqual(body["views_30d"], 4)

        store = next(item for item in body["pages"] if item["path"] == "/store")
        privacy = next(item for item in body["pages"] if item["path"] == "/privacy")
        self.assertEqual(store["views"], 3)
        self.assertEqual(store["unique_visitor_days"], 1)
        self.assertEqual(privacy["views"], 1)
        self.assertEqual(privacy["unique_visitor_days"], 1)

        hu = next(item for item in body["languages"] if item["language"] == "hu")
        self.assertEqual(hu["views"], 3)
        self.assertEqual(hu["unique_visitor_days"], 1)

        self.assertFalse(body["privacy"]["raw_ip_persisted"])
        self.assertFalse(body["privacy"]["user_agent_persisted"])
        self.assertFalse(body["privacy"]["analytics_cookie"])
        self.assertEqual(body["privacy"]["unique_identifier_rotation"], "daily_utc")
        self.assertEqual(body["privacy"]["unique_hash_retention_days"], 35)
        self.assertEqual(body["privacy"]["aggregate_retention_days"], 400)

        with store_api.core._db() as conn:
            stored = [
                str(row["visitor_hash"])
                for row in conn.execute(
                    "SELECT visitor_hash FROM site_analytics_unique_visitors"
                ).fetchall()
            ]
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(len(value) == 32 for value in stored))
        self.assertTrue(all("203.0.113." not in value for value in stored))

    def test_privacy_page_discloses_anonymous_analytics_in_23_languages(self) -> None:
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Adatvédelmi szempontból kímélő, álnevesített webstatisztika", html)
        self.assertIn("Privacy-friendly, pseudonymous website statistics", html)
        self.assertIn("HMAC", html)
        self.assertIn("35 nap", html)
        self.assertIn("400 nap", html)
        self.assertIn("GDPR 6. cikk (1) f)", html)
        self.assertIn("browser fingerprinting", html)
        self.assertIn("跨天画像", html)
        self.assertIn("일일 HMAC", html)
        self.assertEqual(html.count("analyticsTitle:"), 23)
        self.assertEqual(html.count("analyticsText:"), 23)
        self.assertEqual(html.count("analyticsAccuracy:"), 23)
        self.assertIn("Global Privacy Control", html)
        self.assertIn("Do Not Track", html)

    def test_privacy_request_can_be_submitted_and_seen_by_admin(self) -> None:
        response = self.client.post(
            "/api/anthbot/privacy-requests",
            json={
                "request_type": "access",
                "contact": "person@example.test",
                "details": "Please provide my store and reporting data.",
                "site_language": "en",
            },
        )
        self.assertEqual(response.status_code, 201)
        request_id = response.json()["request_id"]
        self.assertTrue(request_id.startswith("prv_"))

        listing = self.client.get(
            "/api/anthbot/admin/privacy-requests",
            headers=self._admin_headers(),
        )
        self.assertEqual(listing.status_code, 200)
        item = next(
            row for row in listing.json()["items"]
            if row["request_id"] == request_id
        )
        self.assertEqual(item["request_type"], "access")
        self.assertEqual(item["contact"], "person@example.test")
        self.assertEqual(item["status"], "new")

    def test_standalone_client_checkout_links_purchase_without_pair_code(self) -> None:
        token = "A" * 48
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("buyer@example.test")
        fake_session = {
            "id": "cs_test_installer_direct",
            "url": "https://checkout.stripe.com/c/pay/test",
            "status": "open",
            "payment_status": "unpaid",
            "amount_total": 799,
            "currency": "eur",
            "client_reference_id": pack_id,
            "customer": "cus_installer",
            "payment_intent": "pi_installer",
            "metadata": {
                "pack_id": pack_id,
                "community_id": "hu_noemi",
                "store_user_id": account["_user_id"],
            },
            "customer_details": {"email": "buyer@example.test"},
            "created": 1700000000,
        }
        with patch.object(store_api, "_create_checkout_session", return_value=fake_session) as create_session:
            response = self.client.post(
                "/api/anthbot/store/client/checkout",
                json={"client_token": token, "pack_id": pack_id},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["already_owned"])
        self.assertEqual(body["pack_id"], pack_id)
        self.assertTrue(body["checkout_url"].startswith("https://checkout.stripe.com/"))
        kwargs = create_session.call_args.kwargs
        self.assertIsNotNone(kwargs["client_id"])
        self.assertEqual(kwargs["user_id"], account["_user_id"])
        self.assertIsNone(kwargs["pair_code"])
        self.assertEqual(kwargs["entitlement_scope"], "web")

    def test_direct_store_purchase_is_web_installer_only(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        direct_url = pack["music_url"].removeprefix("https://testserver")

        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 499, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        self.assertEqual(priced.json()["pack"]["access"], "paid")
        account = self._login_store_account("buyer@example.com")

        legacy = self.client.get("/api/anthbot/voice-packs")
        self.assertEqual(legacy.status_code, 200)
        self.assertFalse(
            any(item.get("id") == pack_id for item in legacy.json()["packs"])
        )

        blocked = self.client.get(direct_url)
        self.assertEqual(blocked.status_code, 404)

        store_page = self.client.get("/store")
        self.assertEqual(store_page.status_code, 200)
        browser_token = self.client.cookies.get("anthbot_voice_store_client")
        self.assertTrue(browser_token)
        browser_client_id = store_api._client_id_from_token(browser_token)

        catalog = self.client.get("/api/anthbot/store/voice-packs")
        self.assertEqual(catalog.status_code, 200)
        paid = next(
            item for item in catalog.json()["packs"] if item.get("id") == pack_id
        )
        self.assertEqual(paid["access"], "paid")
        self.assertEqual(paid["price_amount"], 799)
        self.assertFalse(paid.get("owned", False))
        self.assertNotIn("music_url", paid)

        checkout_session = {
            "id": "cs_test_paid_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": browser_client_id,
                "store_user_id": account["_user_id"],
                "entitlement_scope": "web",
            },
            "payment_status": "unpaid",
            "status": "open",
            "amount_total": 499,
            "currency": "eur",
            "customer_details": None,
            "customer": None,
            "payment_intent": None,
        }
        with patch.object(
            store_api, "_create_checkout_session", return_value=checkout_session
        ) as create:
            checkout = self.client.post(
                "/api/anthbot/store/checkout", json={"pack_id": pack_id}
            )
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.json()["entitlement_scope"], "web")
        self.assertTrue(
            checkout.json()["checkout_url"].startswith("https://checkout.stripe.com/")
        )
        self.assertEqual(create.call_args.kwargs["client_id"], browser_client_id)
        self.assertEqual(create.call_args.kwargs["user_id"], account["_user_id"])
        self.assertEqual(create.call_args.kwargs["entitlement_scope"], "web")

        paid_session = {
            **checkout_session,
            "payment_status": "paid",
            "status": "complete",
            "customer_details": {"email": "buyer@example.com"},
            "customer": "cus_test",
            "payment_intent": "pi_test",
        }
        with patch.object(
            store_api, "_retrieve_checkout_session", return_value=paid_session
        ):
            order = self.client.get(
                "/api/anthbot/store/orders/cs_test_paid_123"
            )
        self.assertEqual(order.status_code, 200)
        body = order.json()
        self.assertEqual(body["payment_status"], "paid")
        self.assertEqual(body["entitlement_scope"], "web")
        self.assertFalse(body["map_linked"])
        self.assertTrue(body["web_installer_linked"])
        self.assertNotIn("license_key", body)
        self.assertIn("/voice-installer?pack=", body["installer_url"])

        browser_catalog = self.client.get("/api/anthbot/store/voice-packs")
        owned = next(
            item for item in browser_catalog.json()["packs"]
            if item.get("id") == pack_id
        )
        self.assertTrue(owned["owned"])
        self.assertEqual(owned["ownership"], "web")

        map_entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": browser_token},
        )
        self.assertEqual(map_entitlements.status_code, 200)
        self.assertFalse(map_entitlements.json()["licensed"])
        self.assertEqual(map_entitlements.json()["packs"], [])

        with patch.object(store_api, "_create_checkout_session") as duplicate_create:
            duplicate = self.client.post(
                "/api/anthbot/store/checkout",
                json={"pack_id": pack_id},
            )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["already_owned"])
        self.assertEqual(duplicate.json()["entitlement_scope"], "web")
        duplicate_create.assert_not_called()

    def test_checkout_uses_official_stripe_sdk_payload(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 100, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("sdk@example.com")

        store_page = self.client.get("/store")
        self.assertEqual(store_page.status_code, 200)
        browser_token = self.client.cookies.get("anthbot_voice_store_client")
        self.assertTrue(browser_token)
        browser_client_id = store_api._client_id_from_token(browser_token)

        checkout_session = {
            "id": "cs_test_sdk_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/sdk-test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {"pack_id": pack_id, "community_id": "cs_vlasta_standard"},
            "payment_status": "unpaid",
            "status": "open",
            "amount_total": 100,
            "currency": "eur",
            "customer_details": None,
            "customer": None,
            "payment_intent": None,
        }
        checkout_session_object = (
            store_api.stripe.checkout.Session.construct_from(
                checkout_session,
                "sk_test_example",
            )
        )
        with patch.object(
            store_api.stripe.checkout.Session,
            "create",
            return_value=checkout_session_object,
        ) as create:
            checkout = self.client.post(
                "/api/anthbot/store/checkout",
                json={"pack_id": pack_id},
            )

        self.assertEqual(checkout.status_code, 200)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["mode"], "payment")
        self.assertEqual(kwargs["client_reference_id"], pack_id)
        self.assertEqual(kwargs["managed_payments"], {"enabled": False})
        self.assertEqual(kwargs["line_items"][0]["price_data"]["currency"], "eur")
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 799)
        self.assertEqual(
            kwargs["line_items"][0]["price_data"]["product_data"]["tax_code"],
            "txcd_10401100",
        )
        self.assertEqual(kwargs["metadata"]["pack_id"], pack_id)
        self.assertEqual(kwargs["metadata"]["store_client_id"], browser_client_id)
        self.assertEqual(kwargs["metadata"]["store_user_id"], account["_user_id"])
        self.assertEqual(kwargs["metadata"]["entitlement_scope"], "web")
        self.assertEqual(kwargs["customer_email"], "sdk@example.com")
        self.assertEqual(
            kwargs["payment_intent_data"]["metadata"]["pack_id"],
            pack_id,
        )
        self.assertEqual(
            kwargs["payment_intent_data"]["metadata"]["entitlement_scope"],
            "web",
        )

    def test_map_client_pairing_unlocks_paid_pack_without_manual_license(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 299, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("mapbuyer@example.com")

        client_token = "A" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        store_url = pairing.json()["store_url"]
        self.assertIn("/store?pair=abp_", store_url)
        pair_code = store_url.split("pair=", 1)[1]
        self._link_store_account_to_pair(pair_code)

        client_id = store_api._client_id_from_token(client_token)
        checkout_session = {
            "id": "cs_test_linked_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/linked-test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": client_id,
                "store_user_id": account["_user_id"],
                "entitlement_scope": "map",
            },
            "payment_status": "unpaid",
            "status": "open",
            "amount_total": 299,
            "currency": "eur",
            "customer_details": None,
            "customer": None,
            "payment_intent": None,
        }

        with patch.object(
            store_api,
            "_create_checkout_session",
            return_value=checkout_session,
        ) as create:
            checkout = self.client.post(
                "/api/anthbot/store/checkout",
                json={"pack_id": pack_id, "pair_code": pair_code},
            )
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(create.call_args.args[0]["id"], pack_id)
        self.assertEqual(create.call_args.kwargs["client_id"], client_id)
        self.assertEqual(create.call_args.kwargs["user_id"], account["_user_id"])
        self.assertEqual(create.call_args.kwargs["pair_code"], pair_code)
        self.assertEqual(create.call_args.kwargs["entitlement_scope"], "map")

        paid_session = {
            **checkout_session,
            "payment_status": "paid",
            "status": "complete",
            "customer_details": {"email": "buyer@example.com"},
            "customer": "cus_linked",
            "payment_intent": "pi_linked",
        }
        stored = store_api._upsert_order_from_session(paid_session)
        self.assertEqual(stored["client_id"], client_id)

        entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(entitlements.status_code, 200)
        body = entitlements.json()
        self.assertTrue(body["licensed"])
        self.assertEqual(len(body["packs"]), 1)
        entitled_pack = body["packs"][0]
        self.assertEqual(entitled_pack["id"], pack_id)
        self.assertEqual(entitled_pack["entitlement"], "purchased")
        self.assertIn("license=", entitled_pack["music_url"])

        downloaded = self.client.get(
            entitled_pack["music_url"].removeprefix("https://testserver")
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"paid-community-pack")

        with patch.object(store_api, "_create_checkout_session") as duplicate_create:
            duplicate = self.client.post(
                "/api/anthbot/store/checkout",
                json={"pack_id": pack_id, "pair_code": pair_code},
            )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["already_owned"])
        self.assertEqual(duplicate.json()["pack_id"], pack_id)
        self.assertIsNone(duplicate.json()["checkout_url"])
        duplicate_create.assert_not_called()

    def test_direct_map_checkout_redirects_straight_to_stripe(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("directmap@example.com")

        client_token = "D" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
        self._link_store_account_to_pair(pair_code)
        client_id = store_api._client_id_from_token(client_token)

        checkout_session = {
            "id": "cs_test_direct_map_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/direct-map-test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": client_id,
                "store_user_id": account["_user_id"],
                "pair_code": pair_code,
                "entitlement_scope": "map",
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
        ) as create:
            response = self.client.get(
                "/api/anthbot/store/direct-checkout",
                params={"pair": pair_code, "voice_id": "cs_vlasta_standard"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "https://checkout.stripe.com/c/pay/direct-map-test",
        )
        self.assertEqual(create.call_args.kwargs["client_id"], client_id)
        self.assertEqual(create.call_args.kwargs["user_id"], account["_user_id"])
        self.assertEqual(create.call_args.kwargs["pair_code"], pair_code)
        self.assertEqual(create.call_args.kwargs["entitlement_scope"], "map")

        store_api._upsert_order_from_session(
            {
                **checkout_session,
                "payment_status": "paid",
                "status": "complete",
                "payment_intent": "pi_direct_map",
            }
        )

        with patch.object(store_api, "_create_checkout_session") as duplicate_create:
            duplicate = self.client.get(
                "/api/anthbot/store/direct-checkout",
                params={"pair": pair_code, "voice_id": "cs_vlasta_standard"},
                follow_redirects=False,
            )
        self.assertEqual(duplicate.status_code, 303)
        self.assertIn(
            "/store/success?session_id=cs_test_direct_map_123",
            duplicate.headers["location"],
        )
        duplicate_create.assert_not_called()

    def test_live_entitlement_reconciles_refund_missed_by_webhook(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)

        client_token = "R" * 48
        client_id = store_api._client_id_from_token(client_token)
        paid_session = {
            "id": "cs_live_refund_reconcile_123",
            "object": "checkout.session",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": client_id,
            },
            "payment_status": "paid",
            "status": "complete",
            "amount_total": 799,
            "currency": "eur",
            "customer_details": {"email": "buyer@example.com"},
            "customer": "cus_refund_reconcile",
            "payment_intent": "pi_refund_reconcile",
        }
        stored = store_api._upsert_order_from_session(paid_session)
        license_key = store_api._license_for_order(stored)

        refunded_intent = {
            "id": "pi_refund_reconcile",
            "object": "payment_intent",
            "latest_charge": {
                "id": "ch_refund_reconcile",
                "object": "charge",
                "refunded": True,
                "amount": 799,
                "amount_refunded": 799,
            },
        }
        with patch.dict(
            os.environ,
            {"ANTHBOT_STRIPE_SECRET_KEY": "sk_live_refund_reconcile"},
        ), patch.object(
            store_api.stripe.PaymentIntent,
            "retrieve",
            return_value=refunded_intent,
        ) as retrieve:
            entitlements = self.client.post(
                "/api/anthbot/store/client/entitlements",
                json={"client_token": client_token},
            )

        self.assertEqual(entitlements.status_code, 200)
        self.assertFalse(entitlements.json()["licensed"])
        self.assertEqual(entitlements.json()["packs"], [])
        retrieve.assert_called_once_with(
            "pi_refund_reconcile",
            expand=["latest_charge"],
        )

        with store_api.core._db() as conn:
            row = conn.execute(
                "SELECT payment_status, status, stripe_checked_at "
                "FROM store_orders WHERE stripe_session_id = ?",
                ("cs_live_refund_reconcile_123",),
            ).fetchone()
        self.assertEqual(row["payment_status"], "refunded")
        self.assertEqual(row["status"], "refunded")
        self.assertGreater(int(row["stripe_checked_at"]), 0)

        license_response = self.client.post(
            "/api/anthbot/store/entitlements",
            json={"license_key": license_key},
        )
        self.assertEqual(license_response.status_code, 401)

    def test_unlinked_purchase_is_not_returned_to_map_client(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 299, "currency": "eur"},
        )
        store_api._upsert_order_from_session(
            {
                "id": "cs_test_unlinked_123",
                "object": "checkout.session",
                "created": int(time.time()),
                "client_reference_id": pack_id,
                "metadata": {"pack_id": pack_id},
                "payment_status": "paid",
                "status": "complete",
                "amount_total": 299,
                "currency": "eur",
            }
        )
        entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": "B" * 48},
        )
        self.assertEqual(entitlements.status_code, 200)
        self.assertFalse(entitlements.json()["licensed"])
        self.assertEqual(entitlements.json()["packs"], [])

    def test_paid_voice_ownership_survives_reupload_version_change(self) -> None:
        pack = self._upload_pack()
        old_pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{old_pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 299, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        account = self._login_store_account("upgrade@example.com")

        client_token = "C" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
        self._link_store_account_to_pair(pair_code)
        client_id = store_api._client_id_from_token(client_token)

        order = store_api._upsert_order_from_session(
            {
                "id": "cs_test_upgrade_123",
                "object": "checkout.session",
                "created": int(time.time()),
                "client_reference_id": old_pack_id,
                "metadata": {
                    "pack_id": old_pack_id,
                    "community_id": "cs_vlasta_standard",
                    "store_client_id": client_id,
                    "store_user_id": account["_user_id"],
                },
                "payment_status": "paid",
                "status": "complete",
                "amount_total": 299,
                "currency": "eur",
                "payment_intent": "pi_upgrade",
            }
        )
        self.assertEqual(order["community_id"], "cs_vlasta_standard")
        license_key = store_api._license_for_order(order)

        reupload = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={
                "file": (
                    "cs-v2.pack",
                    b"paid-community-pack-v2",
                    "application/octet-stream",
                )
            },
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "2.0.0",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(reupload.status_code, 201)
        current_pack = reupload.json()["pack"]
        current_pack_id = current_pack["id"]
        self.assertNotEqual(current_pack_id, old_pack_id)
        self.assertEqual(current_pack["access"], "paid")
        self.assertEqual(current_pack["price_amount"], 799)

        entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(entitlements.status_code, 200)
        entitled_pack = entitlements.json()["packs"][0]
        self.assertEqual(entitled_pack["id"], current_pack_id)
        upgraded_download = self.client.get(
            entitled_pack["music_url"].removeprefix("https://testserver")
        )
        self.assertEqual(upgraded_download.status_code, 200)
        self.assertEqual(upgraded_download.content, b"paid-community-pack-v2")

        manual = self.client.post(
            "/api/anthbot/store/entitlements",
            json={"license_key": license_key},
        )
        self.assertEqual(manual.status_code, 200)
        self.assertEqual(manual.json()["packs"][0]["id"], current_pack_id)

        with patch.object(store_api, "_create_checkout_session") as create:
            duplicate = self.client.post(
                "/api/anthbot/store/checkout",
                json={"pack_id": current_pack_id, "pair_code": pair_code},
            )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["already_owned"])
        create.assert_not_called()

        admin = self.client.get(
            "/api/anthbot/admin/store/voice-packs",
            headers=self._admin_headers(),
        )
        self.assertEqual(admin.status_code, 200)
        current_admin = next(
            item
            for item in admin.json()["items"]
            if item["id"] == current_pack_id
        )
        self.assertEqual(current_admin["sales"], 1)
        self.assertEqual(current_admin["revenue"], 299)

    def test_paid_pricing_survives_reupload(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)

        response = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={"file": ("cs.pack", b"paid-community-pack-v2", "application/octet-stream")},
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "2.0.0",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(response.status_code, 201)
        replacement = response.json()["pack"]
        self.assertEqual(replacement["access"], "paid")
        self.assertEqual(replacement["price_amount"], 799)
        self.assertNotIn("music_url", replacement)

        legacy = self.client.get("/api/anthbot/voice-packs").json()["packs"]
        self.assertFalse(
            any(item.get("community_id") == "cs_vlasta_standard" for item in legacy)
        )

    def test_admin_owner_can_download_any_pack_without_purchase(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        admin_url = f"/api/anthbot/admin/store/voice-packs/{pack_id}/download"

        denied = self.client.get(admin_url)
        self.assertEqual(denied.status_code, 401)

        free_download = self.client.get(
            admin_url,
            headers=self._admin_headers(),
        )
        self.assertEqual(free_download.status_code, 200)
        self.assertEqual(free_download.content, b"paid-community-pack")
        self.assertIn(
            "attachment",
            free_download.headers.get("content-disposition", "").casefold(),
        )
        self.assertEqual(
            free_download.headers.get("cache-control"),
            "no-store",
        )

        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        hidden = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}/visibility",
            headers=self._admin_headers(),
            json={"hidden": True},
        )
        self.assertEqual(hidden.status_code, 200)

        public_direct = self.client.get(
            pack["music_url"].removeprefix("https://testserver")
        )
        self.assertEqual(public_direct.status_code, 404)

        owner_download = self.client.get(
            admin_url,
            headers=self._admin_headers(),
        )
        self.assertEqual(owner_download.status_code, 200)
        self.assertEqual(owner_download.content, b"paid-community-pack")

        store_api._init_store_tables()
        with store_api.core._db() as conn:
            order_count = conn.execute(
                "SELECT COUNT(*) FROM store_orders"
            ).fetchone()[0]
        self.assertEqual(order_count, 0)

        admin_html = Path(store_api.__file__).with_name("store_admin.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Tulajdonosi hozzáférés: minden csomag letölthető", admin_html)
        self.assertIn(
            "/api/anthbot/admin/store/voice-packs/'+encodeURIComponent(id)+'/download",
            admin_html,
        )

    def test_admin_can_grant_owner_map_access_without_fake_purchase(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)

        client_token = "O" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]

        before = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(before.status_code, 200)
        self.assertFalse(before.json()["owner_access"])
        self.assertFalse(before.json()["licensed"])
        self.assertEqual(before.json()["packs"], [])

        login = self.client.post(
            "/dashboard/login",
            content="token=test-admin-token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)

        public_route = self.client.get(
            f"/store?pair={pair_code}",
            follow_redirects=False,
        )
        self.assertEqual(public_route.status_code, 200)
        self.assertIsNone(public_route.headers.get("location"))

        owner_route = self.client.get(
            f"/store?pair={pair_code}&owner=1",
            follow_redirects=False,
        )
        self.assertEqual(owner_route.status_code, 303)
        self.assertEqual(
            owner_route.headers["location"],
            f"/dashboard/store?owner_pair={pair_code}",
        )

        granted = self.client.post(
            "/api/anthbot/admin/store/owner-access",
            headers=self._admin_headers(),
            json={"pair_code": pair_code},
        )
        self.assertEqual(granted.status_code, 200)
        self.assertTrue(granted.json()["granted"])
        self.assertTrue(granted.json()["owner_access"])

        entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(entitlements.status_code, 200)
        body = entitlements.json()
        self.assertTrue(body["licensed"])
        self.assertTrue(body["owner_access"])
        self.assertEqual(len(body["packs"]), 1)
        owner_pack = body["packs"][0]
        self.assertEqual(owner_pack["id"], pack_id)
        self.assertEqual(owner_pack["entitlement"], "owner")
        self.assertTrue(owner_pack["owner_access"])
        self.assertIn("/owner-download?owner=abo1.", owner_pack["music_url"])

        owner_download_path = owner_pack["music_url"].removeprefix(
            "https://testserver"
        )
        owner_download = self.client.get(owner_download_path)
        self.assertEqual(owner_download.status_code, 200)
        self.assertEqual(owner_download.content, b"paid-community-pack")

        revoked = self.client.request(
            "DELETE",
            "/api/anthbot/admin/store/owner-access",
            headers=self._admin_headers(),
            json={"pair_code": pair_code},
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertTrue(revoked.json()["revoked"])
        self.assertFalse(revoked.json()["owner_access"])

        after_revoke = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(after_revoke.status_code, 200)
        self.assertFalse(after_revoke.json()["licensed"])
        self.assertFalse(after_revoke.json()["owner_access"])
        self.assertEqual(after_revoke.json()["packs"], [])

        revoked_download = self.client.get(owner_download_path)
        self.assertEqual(revoked_download.status_code, 401)

        public_catalog = self.client.get("/api/anthbot/store/voice-packs")
        public_pack = next(
            item for item in public_catalog.json()["packs"]
            if item["id"] == pack_id
        )
        self.assertEqual(public_pack["access"], "paid")
        self.assertNotIn("music_url", public_pack)

        store_api._init_store_tables()
        with store_api.core._db() as conn:
            order_count = conn.execute(
                "SELECT COUNT(*) FROM store_orders"
            ).fetchone()[0]
        self.assertEqual(order_count, 0)

        admin_html = Path(store_api.__file__).with_name("store_admin.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Minden hang feloldása ezen a HA-n", admin_html)
        self.assertIn("Tulajdonosi hozzáférés visszavonása", admin_html)
        self.assertIn("method:'DELETE'", admin_html)
        self.assertIn("/api/anthbot/admin/store/owner-access", admin_html)

    def test_admin_can_delete_sandbox_and_explicitly_confirmed_live_test_orders(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)

        test_client_token = "T" * 48
        test_client_id = store_api._client_id_from_token(test_client_token)
        test_session = {
            "id": "cs_test_admin_delete_123",
            "object": "checkout.session",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {
                "pack_id": pack_id,
                "community_id": "cs_vlasta_standard",
                "store_client_id": test_client_id,
                "entitlement_scope": "map",
            },
            "payment_status": "paid",
            "status": "complete",
            "amount_total": 799,
            "currency": "eur",
            "customer_details": {"email": "sandbox@example.com"},
            "customer": "cus_test_admin_delete",
            "payment_intent": "pi_test_admin_delete",
        }
        store_api._upsert_order_from_session(test_session)

        live_session = {
            **test_session,
            "id": "cs_live_admin_delete_123",
            "metadata": {
                **test_session["metadata"],
                "store_client_id": store_api._client_id_from_token("L" * 48),
            },
            "customer_details": {"email": "live@example.com"},
            "customer": "cus_live_admin_delete",
            "payment_intent": "pi_live_admin_delete",
        }
        store_api._upsert_order_from_session(live_session)

        before = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": test_client_token},
        )
        self.assertEqual(before.status_code, 200)
        self.assertTrue(before.json()["licensed"])

        denied = self.client.delete(
            "/api/anthbot/admin/store/orders/cs_test_admin_delete_123"
        )
        self.assertEqual(denied.status_code, 401)

        protected = self.client.delete(
            "/api/anthbot/admin/store/orders/cs_live_admin_delete_123",
            headers=self._admin_headers(),
        )
        self.assertEqual(protected.status_code, 409)

        live_deleted = self.client.delete(
            "/api/anthbot/admin/store/orders/cs_live_admin_delete_123?confirm_live=true",
            headers=self._admin_headers(),
        )
        self.assertEqual(live_deleted.status_code, 200)
        self.assertTrue(live_deleted.json()["deleted"])

        deleted = self.client.delete(
            "/api/anthbot/admin/store/orders/cs_test_admin_delete_123",
            headers=self._admin_headers(),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertTrue(deleted.json()["entitlement_revoked"])

        after = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": test_client_token},
        )
        self.assertEqual(after.status_code, 200)
        self.assertFalse(after.json()["licensed"])
        self.assertEqual(after.json()["packs"], [])

        second_test = {
            **test_session,
            "id": "cs_test_admin_delete_456",
            "payment_intent": "pi_test_admin_delete_456",
        }
        store_api._upsert_order_from_session(second_test)
        bulk = self.client.delete(
            "/api/anthbot/admin/store/orders",
            headers=self._admin_headers(),
        )
        self.assertEqual(bulk.status_code, 200)
        self.assertEqual(bulk.json()["deleted"], 1)

        with store_api.core._db() as conn:
            remaining = {
                row["stripe_session_id"]
                for row in conn.execute(
                    "SELECT stripe_session_id FROM store_orders"
                ).fetchall()
            }
        self.assertNotIn("cs_live_admin_delete_123", remaining)
        self.assertNotIn("cs_test_admin_delete_456", remaining)

        store_api._upsert_order_from_session(test_session)
        store_api._upsert_order_from_session(live_session)
        bulk_all = self.client.delete(
            "/api/anthbot/admin/store/orders?include_live=true",
            headers=self._admin_headers(),
        )
        self.assertEqual(bulk_all.status_code, 200)
        self.assertEqual(bulk_all.json()["deleted"], 2)
        self.assertEqual(bulk_all.json()["scope"], "all_local_stripe_orders")

        admin_html = Path(store_api.__file__).with_name("store_admin.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Összes Sandbox rendelés törlése", admin_html)
        self.assertIn("deleteOrder(", admin_html)
        self.assertIn("/api/anthbot/admin/store/orders/", admin_html)
        self.assertIn("Live törlés", admin_html)
        self.assertIn("Összes helyi rendelés törlése", admin_html)
        self.assertIn("confirm_live=true", admin_html)
        self.assertIn("include_live=true", admin_html)

    def test_admin_lists_active_map_pairings_and_owner_state(self) -> None:
        first_token = "R" * 48
        second_token = "S" * 48
        first_pair = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": first_token},
        )
        second_pair = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": second_token},
        )
        self.assertEqual(first_pair.status_code, 200)
        self.assertEqual(second_pair.status_code, 200)

        denied = self.client.get("/api/anthbot/admin/store/pairings")
        self.assertEqual(denied.status_code, 401)

        listed = self.client.get(
            "/api/anthbot/admin/store/pairings",
            headers=self._admin_headers(),
        )
        self.assertEqual(listed.status_code, 200)
        items = listed.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all("pair_code" in item for item in items))
        self.assertTrue(all("client_suffix" in item for item in items))
        self.assertTrue(all(item["owner_access"] is False for item in items))

        first_pair_code = first_pair.json()["store_url"].split("pair=", 1)[1]
        granted = self.client.post(
            "/api/anthbot/admin/store/owner-access",
            headers=self._admin_headers(),
            json={"pair_code": first_pair_code},
        )
        self.assertEqual(granted.status_code, 200)

        listed_after = self.client.get(
            "/api/anthbot/admin/store/pairings",
            headers=self._admin_headers(),
        )
        states = {
            item["client_suffix"]: item["owner_access"]
            for item in listed_after.json()["items"]
        }
        first_client = store_api._client_id_from_token(first_token)
        second_client = store_api._client_id_from_token(second_token)
        self.assertTrue(states[first_client[-10:]])
        self.assertFalse(states[second_client[-10:]])

        admin_html = Path(store_api.__file__).with_name("store_admin.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("ANTHBOT Map tulajdonosi hozzáférés", admin_html)
        self.assertIn("/api/anthbot/admin/store/pairings?limit=20", admin_html)
        self.assertIn("Minden hang feloldása", admin_html)
        self.assertIn("Tulajdonosi hozzáférés visszavonása", admin_html)

    def test_owner_access_grant_requires_admin(self) -> None:
        client_token = "Q" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
        denied = self.client.post(
            "/api/anthbot/admin/store/owner-access",
            json={"pair_code": pair_code},
        )
        self.assertEqual(denied.status_code, 401)

        denied_revoke = self.client.request(
            "DELETE",
            "/api/anthbot/admin/store/owner-access",
            json={"pair_code": pair_code},
        )
        self.assertEqual(denied_revoke.status_code, 401)

    def test_store_admin_can_hide_sold_pack_without_breaking_entitlement(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)

        client_token = "H" * 48
        client_id = store_api._client_id_from_token(client_token)
        order = store_api._upsert_order_from_session(
            {
                "id": "cs_test_hidden_pack_123",
                "object": "checkout.session",
                "created": int(time.time()),
                "client_reference_id": pack_id,
                "metadata": {
                    "pack_id": pack_id,
                    "community_id": "cs_vlasta_standard",
                    "store_client_id": client_id,
                    "entitlement_scope": "map",
                },
                "payment_status": "paid",
                "status": "complete",
                "amount_total": 799,
                "currency": "eur",
                "payment_intent": "pi_hidden_pack",
            }
        )
        self.assertEqual(order["payment_status"], "paid")

        hidden = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}/visibility",
            headers=self._admin_headers(),
            json={"hidden": True},
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertTrue(hidden.json()["pack"]["store_hidden"])

        catalog = self.client.get("/api/anthbot/store/voice-packs")
        self.assertEqual(catalog.status_code, 200)
        self.assertFalse(
            any(item.get("id") == pack_id for item in catalog.json()["packs"])
        )

        with patch.object(store_api, "_create_checkout_session") as create:
            blocked = self.client.post(
                "/api/anthbot/store/client/checkout",
                json={"client_token": "N" * 48, "pack_id": pack_id},
            )
        self.assertEqual(blocked.status_code, 404)
        create.assert_not_called()

        entitlements = self.client.post(
            "/api/anthbot/store/client/entitlements",
            json={"client_token": client_token},
        )
        self.assertEqual(entitlements.status_code, 200)
        self.assertTrue(entitlements.json()["licensed"])
        entitled = entitlements.json()["packs"][0]
        self.assertEqual(entitled["id"], pack_id)
        download = self.client.get(
            entitled["music_url"].removeprefix("https://testserver")
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"paid-community-pack")

        admin = self.client.get(
            "/api/anthbot/admin/store/voice-packs",
            headers=self._admin_headers(),
        )
        self.assertEqual(admin.status_code, 200)
        admin_pack = next(
            item for item in admin.json()["items"] if item["id"] == pack_id
        )
        self.assertEqual(admin_pack["sales"], 1)
        self.assertTrue(admin_pack["store_hidden"])

        reupload = self.client.post(
            "/api/anthbot/admin/voice-packs",
            headers=self._admin_headers(),
            files={
                "file": (
                    "cs-v2.pack",
                    b"paid-community-pack-v2",
                    "application/octet-stream",
                )
            },
            data={
                "language": "Čeština",
                "language_code": "cs",
                "version": "2.0.0",
                "community_id": "cs_vlasta_standard",
                "variant_id": "vlasta_standard",
                "variant_name": "Vlasta (női) · Standard",
                "voice_gender": "female",
                "technical_slot": "German_girl",
            },
        )
        self.assertEqual(reupload.status_code, 201)
        replacement = reupload.json()["pack"]
        self.assertTrue(replacement["store_hidden"])
        self.assertEqual(replacement["access"], "paid")

        shown = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{replacement['id']}/visibility",
            headers=self._admin_headers(),
            json={"hidden": False},
        )
        self.assertEqual(shown.status_code, 200)
        self.assertFalse(shown.json()["pack"]["store_hidden"])
        visible_catalog = self.client.get("/api/anthbot/store/voice-packs").json()
        self.assertTrue(
            any(item.get("id") == replacement["id"] for item in visible_catalog["packs"])
        )

        admin_html = Path(store_api.__file__).with_name("store_admin.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("async function deletePack", admin_html)
        self.assertIn("async function setVisibility", admin_html)
        self.assertIn("/api/anthbot/admin/voice-packs/", admin_html)
        self.assertIn("A licencek és letöltések megmaradnak.", admin_html)

    def test_stripe_webhook_signature_is_verified(self) -> None:
        body = json.dumps(
            {
                "id": "evt_test",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_webhook_1",
                        "object": "checkout.session",
                        "created": int(time.time()),
                        "client_reference_id": "missing-pack",
                        "metadata": {"pack_id": "missing-pack"},
                        "payment_status": "paid",
                        "status": "complete",
                        "amount_total": 499,
                        "currency": "eur",
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = int(time.time())
        signature = hmac.new(
            b"whsec_test_example",
            str(timestamp).encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()

        response = self.client.post(
            "/api/anthbot/store/webhooks/stripe",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={timestamp},v1={signature}",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["received"])

        invalid = self.client.post(
            "/api/anthbot/store/webhooks/stripe",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={timestamp},v1={'0' * 64}",
            },
        )
        self.assertEqual(invalid.status_code, 400)


    def test_store_account_uses_hashed_code_and_persistent_session(self) -> None:
        captured: dict[str, str] = {}

        def fake_send(target: str, code: str, language: str) -> None:
            captured["email"] = target
            captured["code"] = code

        with patch.object(store_accounts, "_send_login_code", side_effect=fake_send):
            requested = self.client.post(
                "/api/anthbot/store/account/request-code",
                json={"email": "Account@Test.Example", "language": "en"},
            )
        self.assertEqual(requested.status_code, 200)
        with store_api.core._db() as conn:
            row = conn.execute(
                "SELECT code_hash, salt FROM store_login_codes WHERE email = ?",
                ("account@test.example",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["code_hash"], captured["code"])
        self.assertNotIn(captured["code"], str(row["salt"]))

        verified = self.client.post(
            "/api/anthbot/store/account/verify-code",
            json={
                "email": "account@test.example",
                "code": captured["code"],
                "language": "en",
            },
        )
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["authenticated"])
        cookie = verified.headers.get("set-cookie", "")
        self.assertIn("anthbot_store_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)

        status = self.client.get("/api/anthbot/store/account")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["authenticated"])
        self.assertEqual(status.json()["email"], "account@test.example")

    def test_paid_checkout_requires_store_account(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        priced = self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        self.assertEqual(priced.status_code, 200)
        self.client.get("/store")
        blocked = self.client.post(
            "/api/anthbot/store/checkout",
            json={"pack_id": pack_id},
        )
        self.assertEqual(blocked.status_code, 401)
        self.assertIn("account", blocked.json()["detail"].casefold())

    def test_verified_email_claims_legacy_purchase(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        store_api._upsert_order_from_session(
            {
                "id": "cs_test_legacy_account_claim",
                "object": "checkout.session",
                "created": int(time.time()),
                "client_reference_id": pack_id,
                "metadata": {
                    "pack_id": pack_id,
                    "community_id": "cs_vlasta_standard",
                    "store_client_id": store_api._client_id_from_token("Z" * 48),
                    "entitlement_scope": "web",
                },
                "payment_status": "paid",
                "status": "complete",
                "amount_total": 799,
                "currency": "eur",
                "customer_details": {"email": "legacy@example.test"},
                "customer": "cus_legacy_claim",
                "payment_intent": "pi_legacy_claim",
            }
        )

        account = self._login_store_account("legacy@example.test")
        status = self.client.get("/api/anthbot/store/account")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["purchased_count"], 1)
        with store_api.core._db() as conn:
            row = conn.execute(
                """
                SELECT user_id, stripe_customer_id
                FROM store_orders
                WHERE stripe_session_id = ?
                """,
                ("cs_test_legacy_account_claim",),
            ).fetchone()
        self.assertEqual(row["user_id"], account["_user_id"])
        self.assertEqual(row["stripe_customer_id"], "cus_legacy_claim")

        catalog = self.client.get("/api/anthbot/store/voice-packs")
        owned = next(
            item for item in catalog.json()["packs"] if item["id"] == pack_id
        )
        self.assertTrue(owned["owned"])

    def test_logged_out_direct_map_checkout_redirects_to_account_store(self) -> None:
        pack = self._upload_pack()
        pack_id = pack["id"]
        self.client.patch(
            f"/api/anthbot/admin/store/voice-packs/{pack_id}",
            headers=self._admin_headers(),
            json={"access": "paid", "price_amount": 799, "currency": "eur"},
        )
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": "P" * 48},
        )
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
        response = self.client.get(
            "/api/anthbot/store/direct-checkout",
            params={"pair": pair_code, "voice_id": "cs_vlasta_standard"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/store?pair=", response.headers["location"])
        self.assertIn("checkout=1", response.headers["location"])
        self.assertIn("voice_id=cs_vlasta_standard", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
