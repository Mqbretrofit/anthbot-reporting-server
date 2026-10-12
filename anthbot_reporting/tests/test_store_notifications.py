"""Admin native Home Assistant Companion notification regression tests."""
import os
import json
import threading
import time
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
            self.assertIn("Jó napot",first.json()["body_hu"])
            self.assertTrue(first.json()["body_hu"].endswith("ANTHBOT Map"))
            second=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(second.status_code,200,second.text)
            self.assertTrue(second.json()["cached"])
            provider.assert_called_once()
            outgoing=provider.call_args.kwargs["payload"]
            self.assertEqual(outgoing["model"],"gpt-4o-mini")
            self.assertEqual(outgoing["text"]["format"]["type"],"json_schema")
            self.assertEqual(outgoing["text"]["format"]["strict"],True)
            schema=outgoing["text"]["format"]["schema"]
            self.assertEqual(set(schema["required"]),
                {"subject","body","subject_hu","body_hu","request_hu"})
            self.assertFalse(schema["additionalProperties"])
            mail.assert_not_called()

    def test_ai_job_avoids_slow_ingress_and_reuses_completed_result(self):
        import voice_builder_state as builder
        import voice_builder_engine as engine
        made=self.c.post("/api/anthbot/store/custom-voice-requests",json={
            "requested_language":"čeština","voice_style":"vřelý ženský",
            "model":"Genie 1000","contact":"customer@example.test",
            "notes":"natural","site_language":"cs"
        })
        self.assertEqual(made.status_code,201,made.text)
        request_id=made.json()["request_id"]
        url="/api/anthbot/admin/store/custom-voice-workflow/"+request_id+"/ai/jobs"
        started=threading.Event()
        release=threading.Event()
        reply={"output_text":json.dumps({
            "subject":"Vlastní hlas","body":"Dobrý den",
            "subject_hu":"Egyedi hang","body_hu":"Jó napot",
            "request_hu":"Meleg női hang"
        })}
        def slow_ai(*a,**kw):
            started.set()
            release.wait(6)
            return reply
        with patch.object(builder,"credentials",return_value={"openai_key":"test-key"}),\
             patch.object(engine,"request",side_effect=slow_ai) as external:
            first=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(first.status_code,202,first.text)
            self.assertIn(first.json()["status"],("queued","running"))
            self.assertTrue(started.wait(2))
            again=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(again.status_code,202)
            self.assertEqual(again.json()["job_id"],first.json()["job_id"])
            self.assertEqual(external.call_count,1)
            release.set()
            progress={}
            for _ in range(100):
                checked=self.c.get(url+"/"+first.json()["job_id"],headers=self.head)
                self.assertEqual(checked.status_code,200,checked.text)
                progress=checked.json()
                if progress["status"]=="completed":
                    break
                time.sleep(.04)
            self.assertEqual(progress["status"],"completed",progress)
            self.assertIn("Jó napot",progress["result"]["body_hu"])
            self.assertTrue(progress["result"]["body_hu"].endswith("ANTHBOT Map"))
            third=self.c.post(url,headers=self.write,json={"mode":"draft"})
            self.assertEqual(third.json()["status"],"completed")
            self.assertEqual(external.call_count,1)
        self.assertEqual(self.c.get(url+"/"+first.json()["job_id"]).status_code,401)

    def test_ai_upstream_error_keeps_failed_state_until_manual_retry(self):
        import voice_builder_state as builder
        import voice_builder_engine as engine
        made=self.c.post("/api/anthbot/store/custom-voice-requests",json={
            "requested_language":"čeština","voice_style":"vřelý ženský",
            "model":"Genie 1000","contact":"customer@example.test",
            "notes":"warm","site_language":"cs"
        })
        request_id=made.json()["request_id"]
        url="/api/anthbot/admin/store/custom-voice-workflow/"+request_id+"/ai/jobs"
        reply={"output_text":json.dumps({
            "subject":"Vlastní hlas","body":"Dobrý den",
            "subject_hu":"Egyedi hang","body_hu":"Jó napot",
            "request_hu":"Meleg női hang"
        })}
        with patch.object(builder,"credentials",return_value={"openai_key":"test-key"}),\
             patch.object(engine,"request",side_effect=[
                 engine.ProviderError(502,operation="OpenAI fordítás"),reply
             ]) as external:
            first=self.c.post(url,headers=self.write,json={"mode":"draft"})
            jid=first.json()["job_id"]
            record={}
            for _ in range(100):
                record=self.c.get(url+"/"+jid,headers=self.head).json()
                if record["status"]=="failed":break
                time.sleep(.04)
            self.assertEqual(record["status"],"failed",record)
            self.assertEqual(record["error_status"],502)
            self.assertIn("HTTP 502",record["error"])
            self.assertEqual(self.c.post(url,headers=self.write,json={"mode":"draft"}).json()["status"],"failed")
            self.assertEqual(external.call_count,1)
            retry=self.c.post(url,headers=self.write,json={"mode":"draft","retry":True})
            self.assertEqual(retry.status_code,202,retry.text)
            for _ in range(100):
                record=self.c.get(url+"/"+jid,headers=self.head).json()
                if record["status"]=="completed":break
                time.sleep(.04)
            self.assertEqual(record["status"],"completed",record)
            self.assertEqual(external.call_count,2)

    def test_openai_translate_schema_and_safe_400_detail(self):
        import custom_voice_ai
        import voice_builder_engine as engine
        schema=custom_voice_ai._response_format("translate")["format"]
        self.assertEqual(schema["type"],"json_schema")
        self.assertEqual(set(schema["schema"]["required"]),
            {"subject_hu","body_hu","request_hu"})
        self.assertNotIn("body",schema["schema"]["required"])
        msg=str(engine.ProviderError(400,
            operation="OpenAI szöveg- vagy hangfeldolgozás",
            parameter="text.format.schema"))
        self.assertIn("HTTP 400",msg)
        self.assertIn("text.format.schema",msg)
        unsafe=str(engine.ProviderError(400,
            operation="OpenAI szöveg- vagy hangfeldolgozás",
            parameter="key: confidential=123"))
        self.assertNotIn("confidential",unsafe)

    def test_security_and_notify_target_validation(self):
        self.assertEqual(self.c.get("/api/anthbot/admin/notifications").status_code,401)
        invalid=self.c.put("/api/anthbot/admin/notifications/settings",
            headers=self.write,json={"ha_targets":["light.turn_on"]})
        self.assertEqual(invalid.status_code,422)
        forbidden=self.c.post("/api/anthbot/admin/notifications/read",headers=self.head)
        self.assertEqual(forbidden.status_code,403)
