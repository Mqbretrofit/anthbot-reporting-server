"""Regression checks for reversible, owner-only SSH R2 overlay."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
MODULE = REPO / "tools" / "install_r2_pilot_ssh.py"
spec = importlib.util.spec_from_file_location("r2_pilot_ssh_installer_test", MODULE)
installer = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(installer)


class R2SSHInstallerTests(unittest.TestCase):
    def test_store_patch_preserves_existing_code_and_guards_owner(self):
        source = (ROOT / "store_api.py").read_text(encoding="utf-8")
        patched = installer._patch_store(source)
        self.assertIn('if entitlement == "owner":', patched)
        self.assertIn('from r2_voice_pilot import pilot_download_url', patched)
        self.assertIn('    ' + installer.HOOK_MARKER, patched)
        self.assertEqual(source.count("def _short_lived_voice_url("), 1)
        self.assertEqual(patched.count("def _short_lived_voice_url("), 1)
        self.assertTrue(patched.startswith(source[:source.index("def _short_lived_voice_url(")]))
        self.assertIn('return (\n        f"{base}/api/anthbot/store/voice-packs/', patched)
        with self.assertRaises(ValueError):
            installer._patch_store(patched)

    def test_run_patch_sources_private_env_only_before_server_start(self):
        source = (ROOT / "run.sh").read_text(encoding="utf-8")
        patched = installer._patch_run(source)
        self.assertTrue(patched.startswith("#!/usr/bin/with-contenv bashio"))
        self.assertIn('  . /data/.anthbot_r2_pilot.env', patched)
        self.assertLess(patched.index(installer.ENV_MARKER),
                        patched.index('exec uvicorn entrypoint:app'))
        self.assertIn('export ANTHBOT_ADMIN_TOKEN=', patched)
        with self.assertRaises(ValueError):
            installer._patch_run(patched)


if __name__ == "__main__":
    unittest.main()
