"""Safety checks for the opt-in, owner-only R2 paid voice pilot."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import r2_voice_pilot
import store_api


PACK = {
    "id": "hu-lilla-test-v1.2.9",
    "community_id": "hu_lilla_test",
    "access": "paid",
    "filename": "hu-lilla-test-v1.2.9.pack",
    "music_md5": "a" * 32,
    "size": 4_000_000,
}

ENV = {
    "ANTHBOT_R2_PILOT_ENABLED": "true",
    "ANTHBOT_R2_PILOT_COMMUNITY_ID": PACK["community_id"],
    "ANTHBOT_R2_ACCOUNT_ID": "b" * 32,
    "ANTHBOT_R2_BUCKET": "anthbot-voice-packs",
    "ANTHBOT_R2_ACCESS_KEY_ID": "test-access-id",
    "ANTHBOT_R2_SECRET_ACCESS_KEY": "test-secret",
}


class FakeS3:
    def __init__(self):
        self.head_calls = []
        self.sign_calls = []

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        return {
            "ContentLength": PACK["size"],
            "Metadata": {"anthbot-md5": PACK["music_md5"]},
        }

    def generate_presigned_url(self, *args, **kwargs):
        self.sign_calls.append((args, kwargs))
        return (
            f"https://{ENV['ANTHBOT_R2_ACCOUNT_ID']}.r2.cloudflarestorage.com/"
            "anthbot-voice-packs/pilot/example"
            "?X-Amz-Signature=hidden-for-test"
        )


class R2VoicePilotTests(unittest.TestCase):
    def test_disabled_never_accesses_r2(self):
        with patch.dict(os.environ, ENV, clear=False):
            with patch.dict(os.environ, {"ANTHBOT_R2_PILOT_ENABLED": "false"}):
                with patch.object(r2_voice_pilot, "client_for") as client:
                    self.assertIsNone(r2_voice_pilot.pilot_download_url(PACK))
                    client.assert_not_called()

    def test_wrong_voice_and_free_voice_never_access_r2(self):
        with patch.dict(os.environ, ENV, clear=False):
            with patch.object(r2_voice_pilot, "client_for") as client:
                self.assertIsNone(r2_voice_pilot.pilot_download_url(
                    {**PACK, "community_id": "another_voice"}
                ))
                self.assertIsNone(r2_voice_pilot.pilot_download_url(
                    {**PACK, "access": "free"}
                ))
                client.assert_not_called()

    def test_private_r2_url_requires_verified_metadata(self):
        fake = FakeS3()
        with patch.dict(os.environ, ENV, clear=False):
            with patch.object(r2_voice_pilot, "client_for", return_value=fake):
                url = r2_voice_pilot.pilot_download_url(PACK)
        self.assertIn("r2.cloudflarestorage.com/", url)
        self.assertEqual(len(fake.head_calls), 1)
        self.assertEqual(len(fake.sign_calls), 1)
        self.assertEqual(fake.sign_calls[0][1]["ExpiresIn"], 900)

    def test_md5_mismatch_falls_back_without_signing(self):
        fake = FakeS3()
        fake.head_object = lambda **kwargs: {
            "ContentLength": PACK["size"],
            "Metadata": {"anthbot-md5": "0" * 32},
        }
        with patch.dict(os.environ, ENV, clear=False):
            with patch.object(r2_voice_pilot, "client_for", return_value=fake):
                self.assertIsNone(r2_voice_pilot.pilot_download_url(PACK))
        self.assertEqual(fake.sign_calls, [])

    def test_store_routes_r2_only_to_owner(self):
        with patch.object(store_api, "_download_access_token", return_value="legacy-token"):
            with patch.object(store_api.core, "_public_base_url", return_value="https://testserver"):
                with patch.object(r2_voice_pilot, "pilot_download_url", return_value="https://r2.example/signed") as pilot:
                    owner = store_api._short_lived_voice_url(
                        object(), PACK, entitlement="owner", subject="owner-client"
                    )
                    purchase = store_api._short_lived_voice_url(
                        object(), PACK, entitlement="purchase", subject="cs_test"
                    )
                    grant = store_api._short_lived_voice_url(
                        object(), PACK, entitlement="grant", subject="grant_test"
                    )
        self.assertEqual(owner, "https://r2.example/signed")
        self.assertIn("token=legacy-token", purchase)
        self.assertIn("token=legacy-token", grant)
        self.assertEqual(pilot.call_count, 1)


if __name__ == "__main__":
    unittest.main()
