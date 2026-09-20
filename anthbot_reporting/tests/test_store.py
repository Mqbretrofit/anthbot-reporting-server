from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import entrypoint
import store_api


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
        self.client_ctx = TestClient(entrypoint.app, base_url="https://testserver")
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-admin-token"}

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
            self.assertIn('<span class="logo">M</span>', html)
            self.assertIn("<span>MQB Retrofit Hungary</span>", html)
            self.assertIn('class="hero"', html)
            self.assertIn('class="topic-stage glass"', html)
            self.assertIn('class="feature-grid"', html)
            self.assertIn('<footer class="footer">', html)
            self.assertIn('href="/#anthbot-map"', html)
            self.assertIn('href="/#features"', html)
            self.assertIn('href="/#models"', html)
            self.assertIn('href="/#support"', html)

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
            "metadata": {"pack_id": pack_id, "community_id": "hu_noemi"},
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
        self.assertEqual(kwargs["metadata"]["entitlement_scope"], "web")
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

        client_token = "A" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        store_url = pairing.json()["store_url"]
        self.assertIn("/store?pair=abp_", store_url)
        pair_code = store_url.split("pair=", 1)[1]

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
        self.assertEqual(create.call_args.kwargs["client_id"], client_id)
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

        client_token = "C" * 48
        pairing = self.client.post(
            "/api/anthbot/store/client/pair",
            json={"client_token": client_token},
        )
        self.assertEqual(pairing.status_code, 200)
        pair_code = pairing.json()["store_url"].split("pair=", 1)[1]
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


if __name__ == "__main__":
    unittest.main()
