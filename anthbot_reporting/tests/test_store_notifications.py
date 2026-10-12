"""Admin native Home Assistant Companion notification regression tests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import entrypoint
import store_notifications as notify


class AdminAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        os.environ["ANTHBOT_DB_PATH"]=str(Path(self.temp.name)/"server.sqlite3")
        os.environ["ANTHBOT_VOICE_BUILDER_DIR"]=str(Path(self.temp.name)/"builder")
        os.environ["ANTHBOT_ADMIN_TOKEN"]="test-admin-token"
        self.c=TestClient(entrypoint.app,base_url="https://testserver")
        self.c.__enter__()
        self.head={"Authorization":"Bearer test-admin-token"}
        self.write={**self.head,"X-ANTHBOT-Builder":"1"}

    def tearDown(self):
        self.c.__exit__(None,None,None)
        self.temp.cleanup()

    def test_event_ids_are_deduplicated_and_delivered_once(self):
        with patch.object(notify,"dispatch_soon"):
            notify.create_event("custom_voice","test-request","Új kérés","Első")
            notify.create_event("custom_voice","test-request","Új kérés","Második")
            with patch.object(notify,"_available_ha_targets",
                              return_value=["notify.mobile_app_phone"]):
                r=self.c.put("/api/anthbot/admin/notifications/settings",
                     headers=self.write,json={
                        "admin_email":"","email_enabled":False,
                        "ha_enabled":True,"ha_targets":["notify.mobile_app_phone"]})
                self.assertEqual(r.status_code,200,r.text)
            notify.create_event("purchase","test-order","Fizetett","799 EUR")
        with patch.object(notify,"_notify_ha") as deliver:
            notify.dispatch()
            notify.dispatch()
            self.assertEqual(deliver.call_count,1)
            self.assertEqual(deliver.call_args.args[0],"notify.mobile_app_phone")
        result=self.c.get("/api/anthbot/admin/notifications",headers=self.head)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()["unread"],2)

    def test_security_and_notify_target_validation(self):
        self.assertEqual(self.c.get("/api/anthbot/admin/notifications").status_code,401)
        invalid=self.c.put("/api/anthbot/admin/notifications/settings",
            headers=self.write,json={"ha_targets":["light.turn_on"]})
        self.assertEqual(invalid.status_code,422)
        forbidden=self.c.post("/api/anthbot/admin/notifications/read",headers=self.head)
        self.assertEqual(forbidden.status_code,403)
