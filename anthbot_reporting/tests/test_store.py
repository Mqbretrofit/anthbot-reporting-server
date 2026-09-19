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

    def test_paid_pack_is_hidden_from_legacy_registry_and_requires_license(self) -> None:
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

        catalog = self.client.get("/api/anthbot/store/voice-packs")
        self.assertEqual(catalog.status_code, 200)
        paid = next(
            item for item in catalog.json()["packs"] if item.get("id") == pack_id
        )
        self.assertEqual(paid["access"], "paid")
        self.assertEqual(paid["price_amount"], 499)
        self.assertNotIn("music_url", paid)

        checkout_session = {
            "id": "cs_test_paid_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.com/c/pay/test",
            "created": int(time.time()),
            "client_reference_id": pack_id,
            "metadata": {"pack_id": pack_id, "community_id": "cs_vlasta_standard"},
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
        ):
            checkout = self.client.post(
                "/api/anthbot/store/checkout", json={"pack_id": pack_id}
            )
        self.assertEqual(checkout.status_code, 200)
        self.assertTrue(
            checkout.json()["checkout_url"].startswith("https://checkout.stripe.com/")
        )

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
        self.assertEqual(order.json()["payment_status"], "paid")
        license_key = order.json()["license_key"]
        self.assertTrue(license_key.startswith("abv1."))

        entitlement = self.client.post(
            "/api/anthbot/store/entitlements",
            json={"license_key": license_key},
        )
        self.assertEqual(entitlement.status_code, 200)
        licensed_pack = entitlement.json()["packs"][0]
        self.assertEqual(licensed_pack["id"], pack_id)
        self.assertIn("license=", licensed_pack["music_url"])

        download_path = licensed_pack["music_url"].removeprefix("https://testserver")
        downloaded = self.client.get(download_path)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"paid-community-pack")

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
