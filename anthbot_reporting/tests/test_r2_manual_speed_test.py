"""No-network tests for the interactive R2 diagnostic uploader."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import r2_manual_speed_test as pilot


class R2ManualSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = b"test-voice-payload\x00"
        self.filename = "hu-lilla-demo-v1.pack"
        (self.root / self.filename).write_bytes(self.payload)
        self.pack = {
            "id": "test-lilla",
            "access": "paid",
            "community_id": "hu_lilla_demo",
            "language": "Magyar",
            "voice_display_name": "Lilla",
            "variant_name": "Kacer",
            "version": "1.2.99",
            "filename": self.filename,
            "size": len(self.payload),
            "music_md5": hashlib.md5(self.payload, usedforsecurity=False).hexdigest(),
        }
        (self.root / "registry.json").write_text(
            json.dumps({"schema": "anthbot-community-voice-packs-v1",
                        "packs": [self.pack]}),
            encoding="utf-8",
        )

    def test_lists_only_existing_paid_packs(self):
        self.assertEqual(pilot.available_paid_packs(self.root), [self.pack])

    def test_local_md5_and_size_match(self):
        path, size, digest = pilot.verified_local_file(self.root, self.pack)
        self.assertEqual(path.name, self.filename)
        self.assertEqual(size, len(self.payload))
        self.assertEqual(digest, self.pack["music_md5"])

    def test_md5_mismatch_stops_before_upload(self):
        with self.assertRaises(ValueError):
            pilot.verified_local_file(
                self.root, {**self.pack, "music_md5": "0" * 32}
            )

    def test_invalid_filename_not_offered(self):
        records = {**self.pack, "filename": "../other"}
        (self.root / "registry.json").write_text(
            json.dumps({"packs": [records]}), encoding="utf-8"
        )
        self.assertEqual(pilot.available_paid_packs(self.root), [])

    def test_selection_defaults_to_lilla(self):
        with patch("builtins.input", side_effect=["", ""]):
            chosen = pilot.ask_voice([self.pack])
        self.assertEqual(chosen["community_id"], "hu_lilla_demo")


if __name__ == "__main__":
    unittest.main()
