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

    def test_test_push_only_uses_selected_real_mobile_target(self):
        with patch.object(notify,"_available_ha_targets",
                          return_value=["notify.mobile_app_phone"]),\
             patch.object(notify,"_notify_ha") as send:
            result=self.c.post("/api/anthbot/admin/notifications/ha-test",
                headers=self.write,json={"ha_target":"notify.mobile_app_phone"})
            self.assertEqual(result.status_code,200,result.text)
            send.assert_called_once()
            reject=self.c.post("/api/anthbot/admin/notifications/ha-test",
                headers=self.write,json={"ha_target":"notify.mobile_app_unknown"})
            self.assertEqual(reject.status_code,422)
            send.assert_called_once()

    def test_ai_customer_reply_is_cached_and_never_auto_sends(self):
        import json
        import custom_voice_ai
        import voice_builder_state as builder
        import voice_builder_engine as engine
        import store_accounts
        made=self.c.post("/api/anthbot/store/custom-voice-requests",json={
            "requested_language":"čeština","voice_style":"vřelý ženský",
            "model":"Genie 1000","contact":"customer@example.test",
            "notes":"","site_language":"cs"
        })
        self.assertEqual(made.status_code,201,made.text)
        request_id=made.json()["request_id"]
        url="/api/anthbot/admin/store/custom-voice-workflow/"+request_id+"/ai"
        answer={"output_text":json.dumps({
            "subject":"Žádost","body":"Dobrý den",
            "subject_hu":"Kérés","body_hu":"Jó napot","request_hu":"Női hangot kér"
        })}
        with patch.object(builder,"credentials",return_value={"openai_key":"test-key"}),\
             patch.object(engine,"request",return_value=answer) as provider,\
             patch.object(store_accounts,"_smtp_send_message") as mail:
            first=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.json()["body_hu"],"Jó napot")
            second=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(second.status_code,200,second.text)
            self.assertTrue(second.json()["cached"])
            provider.assert_called_once()
            mail.assert_not_called()

    def test_security_and_notify_target_validation(self):
        self.assertEqual(self.c.get("/api/anthbot/admin/notifications").status_code,401)
        invalid=self.c.put("/api/anthbot/admin/notifications/settings",
            headers=self.write,json={"ha_targets":["light.turn_on"]})
        self.assertEqual(invalid.status_code,422)
        forbidden=self.c.post("/api/anthbot/admin/notifications/read",headers=self.head)
        self.assertEqual(forbidden.status_code,403)
