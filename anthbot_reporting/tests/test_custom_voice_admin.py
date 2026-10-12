"""Regression tests for custom voice request handling without real email or charges."""
from __future__ import annotations

import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import entrypoint
import store_api
import store_accounts


class CustomVoiceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"] = str(Path(self.temp.name) / "server.sqlite3")
        os.environ["ANTHBOT_VOICE_PACK_DIR"] = str(Path(self.temp.name) / "voice_packs")
        os.environ["ANTHBOT_ADMIN_TOKEN"] = "workflow-test-token"
        os.environ["ANTHBOT_VOICE_BUILDER_DIR"] = str(Path(self.temp.name) / "builder")
        os.environ["ANTHBOT_VOICE_BUILDER_NO_WORKER"] = "1"
        os.environ["ANTHBOT_SMTP_HOST"] = "smtp.example.test"
        os.environ["ANTHBOT_SMTP_FROM_EMAIL"] = "store@example.test"
        os.environ["ANTHBOT_SMTP_FROM_NAME"] = "ANTHBOT Map"
        self.context = TestClient(entrypoint.app, base_url="https://testserver")
        self.client = self.context.__enter__()
        self.read_headers = {"Authorization": "Bearer workflow-test-token"}
        self.write_headers = {
            **self.read_headers,
            "X-ANTHBOT-Builder": "1",
        }
        submitted = self.client.post("/api/anthbot/store/custom-voice-requests", json={
            "requested_language": "čeština (czech)",
            "voice_style": "vřelý ženský",
            "model": "Genie 1000",
            "contact": "customer@example.test",
            "notes": "Natural delivery",
            "site_language": "cs",
        })
        self.assertEqual(submitted.status_code, 201, submitted.text)
        self.id = submitted.json()["request_id"]
        self.endpoint = "/api/anthbot/admin/store/custom-voice-workflow/" + self.id

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_requires_admin_and_same_origin_write_marker(self):
        self.assertEqual(self.client.get(self.endpoint).status_code, 401)
        self.assertEqual(
            self.client.patch(self.endpoint, json={"status": "contacted"}, headers=self.read_headers).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.endpoint, headers=self.read_headers).status_code, 200)
        self.assertEqual(
            self.client.patch(self.endpoint, json={"status": "contacted"}, headers={
                **self.write_headers, "Origin": "https://attacker.invalid"
            }).status_code,
            403,
        )

    def test_legacy_request_visible_and_updates_persist(self):
        listing = self.client.get("/api/anthbot/admin/store/custom-voice-workflow", headers=self.read_headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["items"][0]["request_id"], self.id)
        before = self.client.get(self.endpoint, headers=self.read_headers).json()
        self.assertEqual(before["status"], "new")
        self.assertEqual(before["voice_style"], "vřelý ženský")
        update = self.client.patch(self.endpoint, headers=self.write_headers, json={
            "status": "quoted", "admin_notes": "Wait for Czech sample",
            "quote_amount": 2999,
        })
        self.assertEqual(update.status_code, 200, update.text)
        after = self.client.get(self.endpoint, headers=self.read_headers).json()
        self.assertEqual(after["quote_amount"], 2999)
        self.assertEqual(after["admin_notes"], "Wait for Czech sample")
        self.assertEqual(after["status"], "quoted")
        self.assertEqual(after["payment"], {"verified": False})
        self.assertEqual(after["notes"], "Natural delivery")
        self.assertEqual(self.client.patch(self.endpoint, headers=self.write_headers, json={
            "quote_amount": 1999
        }).status_code, 422)
        self.assertEqual(self.client.patch(self.endpoint, headers=self.write_headers, json={
            "status": "paid"
        }).status_code, 422)
        self.assertEqual(self.client.patch(self.endpoint, headers=self.write_headers, json={
            "status": "completed"
        }).status_code, 409)
        self.assertEqual(self.client.patch(self.endpoint, headers=self.write_headers, json={
            "pack_id": "missing-pack"
        }).status_code, 404)
        self.assertEqual(self.client.patch(self.endpoint, headers=self.write_headers, json={
            "builder_job_id": "missing-job"
        }).status_code, 404)

    def test_stripe_payment_is_read_from_real_paid_order_not_admin_status(self):
        from app import _db, _iso
        with patch.object(store_api, "_find_uploaded_pack", return_value={
            "id": "pack-cz", "community_id": "voice-cz"
        }):
            updated = self.client.patch(self.endpoint, headers=self.write_headers, json={
                "pack_id": "pack-cz", "status": "awaiting_payment"
            })
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertFalse(updated.json()["item"]["payment"]["verified"])
            with _db() as conn:
                conn.execute("""INSERT INTO store_orders(
                    stripe_session_id, pack_id, community_id, status, payment_status,
                    amount_total, currency, customer_email, created_at, updated_at, paid_at
                ) VALUES ('cs_test_custom', 'pack-cz', 'voice-cz', 'complete',
                          'paid', 2999, 'eur', 'other@example.test', ?, ?, ?)""",
                    (_iso(), _iso(), _iso()))
            self.assertFalse(self.client.get(self.endpoint, headers=self.read_headers).json()["payment"]["verified"])
            with _db() as conn:
                conn.execute("""UPDATE store_orders SET customer_email=?
                                WHERE stripe_session_id='cs_test_custom'""", ("customer@example.test",))
            verified = self.client.get(self.endpoint, headers=self.read_headers).json()["payment"]
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["amount_total"], 2999)
            with _db() as conn:
                conn.execute("""UPDATE store_orders SET payment_status='refunded'
                                WHERE stripe_session_id='cs_test_custom'""")
            self.assertFalse(self.client.get(self.endpoint, headers=self.read_headers).json()["payment"]["verified"])

    def test_emails_are_explicit_and_recorded_only_after_success(self):
        with patch.object(store_accounts, "_smtp_send_message") as sender:
            self.assertEqual(sender.call_count, 0)
            self.assertEqual(self.client.get(self.endpoint, headers=self.read_headers).status_code, 200)
            self.assertEqual(sender.call_count, 0)
            result = self.client.post(self.endpoint + "/email", headers=self.write_headers, json={
                "subject": "Váš hlas ANTHBOT",
                "body": "Dobrý den, potvrdíme cenu před výrobou.",
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(sender.call_count, 1)
            sent_mail = sender.call_args.args[0]
            self.assertEqual(sent_mail["To"], "customer@example.test")
            self.assertEqual(result.json()["item"]["status"], "contacted")
            self.assertEqual(result.json()["item"]["messages"][0]["subject"], "Váš hlas ANTHBOT")
        self.assertEqual(len(self.client.get(self.endpoint, headers=self.read_headers).json()["messages"]), 1)

    def test_smtp_failure_does_not_claim_sent_and_rejects_header_injection(self):
        self.assertEqual(self.client.post(self.endpoint + "/email", headers=self.write_headers, json={
            "subject": "hello\nBcc: attacker@example.test", "body": "hello",
        }).status_code, 422)
        with patch.object(store_accounts, "_smtp_send_message",
                          side_effect=smtplib.SMTPAuthenticationError(535, b"SECRET")):
            result = self.client.post(self.endpoint + "/email", headers=self.write_headers, json={
                "subject": "Voice request", "body": "Thanks",
            })
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("SECRET", result.text)
        item = self.client.get(self.endpoint, headers=self.read_headers).json()
        self.assertEqual(item["status"], "new")
        self.assertEqual(item["messages"], [])


if __name__ == "__main__":
    unittest.main()
