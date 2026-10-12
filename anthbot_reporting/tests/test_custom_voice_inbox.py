"""Incoming custom voice email: safe matching, legacy compatibility, translation and no auto-replies."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import entrypoint
import app as core
import store_accounts
import store_notifications
import custom_voice_inbox as inbox
import custom_voice_admin


class CustomVoiceInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.options=patch.dict(os.environ,{
            "ANTHBOT_DB_PATH":str(Path(self.temp.name)/"report.sqlite3"),
            "ANTHBOT_VOICE_BUILDER_DIR":str(Path(self.temp.name)/"builder"),
            "ANTHBOT_VOICE_PACK_DIR":str(Path(self.temp.name)/"packs"),
            "ANTHBOT_ADMIN_TOKEN":"inbox-admin-token",
            "ANTHBOT_SMTP_HOST":"mail.example.test",
            "ANTHBOT_SMTP_FROM_EMAIL":"support@anthbotmap.com",
            "ANTHBOT_IMAP_ENABLED":"false",
        })
        self.options.start()
        self.context=TestClient(entrypoint.app,base_url="https://testserver")
        self.c=self.context.__enter__()
        self.auth={"Authorization":"Bearer inbox-admin-token"}
        self.write={**self.auth,"X-ANTHBOT-Builder":"1"}
        created=self.c.post("/api/anthbot/store/custom-voice-requests",json={
            "requested_language":"čeština (czech)",
            "voice_style":"vřelý ženský","model":"Genie 1000",
            "contact":"client@example.test","notes":"","site_language":"cs"
        })
        self.assertEqual(created.status_code,201,created.text)
        self.id=created.json()["request_id"]
        self.url="/api/anthbot/admin/store/custom-voice-workflow/"+self.id
        with patch.object(store_accounts,"_smtp_send_message") as smtp:
            outgoing=self.c.post(self.url+"/email",headers=self.write,json={
                "subject":"Žádost o hlasový balíček pro Genie 1000",
                "body":"Dobrý den. Rádi připravíme český ženský hlas."
            })
            self.assertEqual(outgoing.status_code,200,outgoing.text)
            self.mail_sent=smtp.call_args.args[0]
        self.key="imap_account_testing"

    def tearDown(self):
        self.context.__exit__(None,None,None)
        self.options.stop()
        self.temp.cleanup()

    def _message(self,sender="client@example.test",subject=None,reply=True):
        mail=EmailMessage()
        mail["From"]=sender
        mail["To"]="support@anthbotmap.com"
        mail["Date"]=format_datetime(datetime.now(timezone.utc))
        mail["Message-ID"]="<test-unique-"+str(self._testMethodName)+"@example.test>"
        mail["Subject"]=subject or "Re: Žádost o hlasový balíček pro Genie 1000"
        if reply:mail["In-Reply-To"]=self.mail_sent["Message-ID"]
        mail.set_content("Chtěl bych teplý ženský hlas. Děkuji.")
        return mail

    def test_sent_mail_has_request_tag_and_rfc_message_id(self):
        self.assertIn("[ANTHBOT:"+self.id+"]",self.mail_sent["Subject"])
        self.assertEqual(self.mail_sent["X-ANTHBOT-Request-ID"],self.id)
        self.assertTrue(str(self.mail_sent["Message-ID"]).startswith("<"))
        item=self.c.get(self.url,headers=self.auth).json()
        self.assertEqual(item["messages"][0]["rfc_message_id"],self.mail_sent["Message-ID"])
        self.assertEqual(item["incoming_messages"],[])

    def test_import_threaded_message_without_sending_or_duplicate(self):
        mail=self._message()
        with patch.object(inbox,"_launch_translation") as translate,\
             patch.object(store_notifications,"create_event") as notification:
            self.assertTrue(inbox._ingest(mail.as_bytes(),self.key,1))
            self.assertFalse(inbox._ingest(mail.as_bytes(),self.key,1))
            translate.assert_called_once()
            notification.assert_called_once()
        item=self.c.get(self.url,headers=self.auth).json()
        self.assertEqual(item["incoming_unread"],1)
        self.assertEqual(item["status"],"replied")
        incoming=item["incoming_messages"][0]
        self.assertIn("teplý ženský",incoming["body"])
        self.assertEqual(incoming["translation_state"],"pending")
        self.assertEqual(self.c.post("/api/anthbot/admin/store/custom-voice-inbox/request/"+self.id+"/read",
                                    headers=self.write).status_code,200)
        self.assertEqual(self.c.get(self.url,headers=self.auth).json()["incoming_unread"],0)

    def test_legacy_untagged_reply_and_sender_verification(self):
        mail=self._message(reply=False)
        with patch.object(inbox,"_launch_translation"):
            self.assertTrue(inbox._ingest(mail.as_bytes(),self.key,1))
            wrong=self._message(sender="attacker@example.test",subject="Re: "+mail["Subject"])
            self.assertFalse(inbox._ingest(wrong.as_bytes(),self.key,2))
        data=self.c.get(self.url,headers=self.auth).json()
        self.assertEqual(len(data["incoming_messages"]),1)

    def test_unrelated_automated_and_ambiguous_mail_is_ignored(self):
        mail=self._message(subject="Free prize winner",reply=False)
        with patch.object(inbox,"_launch_translation"):
            self.assertFalse(inbox._ingest(mail.as_bytes(),self.key,1))
            mail.replace_header("Subject","Re: Voice enquiry")
            mail["Auto-Submitted"]="auto-replied"
            self.assertFalse(inbox._ingest(mail.as_bytes(),self.key,2))
        # Two custom requests from the same contact: do not guess which one.
        new=self.c.post("/api/anthbot/store/custom-voice-requests",json={
            "requested_language":"čeština","voice_style":"male",
            "model":"Genie 1000","contact":"client@example.test",
            "notes":"","site_language":"cs"
        })
        self.assertEqual(new.status_code,201)
        mail=self._message(reply=False)
        with patch.object(inbox,"_launch_translation"):
            self.assertFalse(inbox._ingest(mail.as_bytes(),self.key,3))

    def test_imap_only_peeks_and_imports_each_uid_once(self):
        mail=self._message()
        class FakeIMAP:
            selected=[]
            fetched=[]
            def __init__(self,*args,**kwargs):
                self.calls=[]
            def login(self,user,password):
                self.asserted_login=(user,password)
            def select(self,mailbox,readonly=False):
                FakeIMAP.selected.append((mailbox,readonly))
                return ("OK",[b"2"])
            def response(self,name):
                return ("UIDVALIDITY",[b"54321"])
            def uid(self,command,*args):
                if command=="SEARCH":return ("OK",[b"101 102"])
                if command=="FETCH":
                    FakeIMAP.fetched.append(args)
                    if args[0]=="101":return ("OK",[(b"101 FETCH",mail.as_bytes())])
                    return ("OK",[(b"102 FETCH",b"unrelated")])
                raise AssertionError("unexpected IMAP operation")
            def logout(self):
                return ("BYE",[])
        with patch.dict(os.environ,{
             "ANTHBOT_IMAP_ENABLED":"true","ANTHBOT_IMAP_HOST":"imap.example.test",
             "ANTHBOT_IMAP_PORT":"993","ANTHBOT_IMAP_USERNAME":"support@example.test",
             "ANTHBOT_IMAP_PASSWORD":"test-app-password","ANTHBOT_IMAP_MAILBOX":"INBOX"
             }),patch.object(inbox.imaplib,"IMAP4_SSL",FakeIMAP),\
             patch.object(inbox,"_launch_translation"),\
             patch.object(store_notifications,"create_event"):
            self.assertEqual(inbox._poll_once(),1)
            self.assertEqual(inbox._poll_once(),0)
            self.assertTrue(FakeIMAP.selected)
            self.assertTrue(all(readonly for _,readonly in FakeIMAP.selected))
            self.assertEqual(len(FakeIMAP.fetched),2)
            self.assertTrue(all(fetch_args[1]=="(BODY.PEEK[])" for fetch_args in FakeIMAP.fetched))
        self.assertEqual(len(self.c.get(self.url,headers=self.auth).json()["incoming_messages"]),1)

    def test_incoming_message_translates_and_never_sends_email(self):
        mail=self._message()
        with patch.object(inbox,"_launch_translation"),\
             patch.object(store_notifications,"create_event"):
            self.assertTrue(inbox._ingest(mail.as_bytes(),self.key,3))
        item=self.c.get(self.url,headers=self.auth).json()
        incoming_id=item["incoming_messages"][0]["id"]
        import voice_builder_state as builder
        import voice_builder_engine as engine
        reply={"output_text":json.dumps({"translation_hu":
            "Meleg női hangot szeretnék. Köszönöm."},ensure_ascii=False)}
        with patch.object(builder,"credentials",return_value={"openai_key":"test-only"}),\
             patch.object(engine,"request",return_value=reply) as ai,\
             patch.object(store_accounts,"_smtp_send_message") as smtp:
            inbox._translate(incoming_id)
            self.assertEqual(ai.call_count,1)
            arguments=json.loads(ai.call_args.kwargs["payload"]["input"])
            self.assertIn("teplý ženský",arguments["body"])
            self.assertNotIn("client@example.test",ai.call_args.kwargs["payload"]["input"])
            smtp.assert_not_called()
        after=self.c.get(self.url,headers=self.auth).json()["incoming_messages"][0]
        self.assertEqual(after["translation_state"],"done")
        self.assertIn("Meleg női",after["translation_hu"])

    def test_reference_audio_attachment_requires_admin_to_download(self):
        mail=self._message()
        mail.add_attachment(b"ID3example-audio-bytes",maintype="audio",subtype="mpeg",
                            filename="czech-sample.mp3")
        with patch.object(inbox,"_launch_translation"),\
             patch.object(store_notifications,"create_event"):
            self.assertTrue(inbox._ingest(mail.as_bytes(),self.key,1))
        data=self.c.get(self.url,headers=self.auth).json()
        attachments=data["incoming_messages"][0]["attachments"]
        self.assertEqual(len(attachments),1)
        self.assertEqual(attachments[0]["filename"],"czech-sample.mp3")
        link="/api/anthbot/admin/store/custom-voice-inbox/files/"+str(attachments[0]["id"])
        self.assertEqual(self.c.get(link).status_code,401)
        downloaded=self.c.get(link,headers=self.auth)
        self.assertEqual(downloaded.status_code,200,downloaded.text)
        self.assertEqual(downloaded.content,b"ID3example-audio-bytes")
        self.assertIn("attachment",downloaded.headers["content-disposition"].lower())
        self.assertEqual(downloaded.headers["x-content-type-options"],"nosniff")

    def test_server_imap_sync_requires_configuration(self):
        stat=self.c.get("/api/anthbot/admin/store/custom-voice-inbox/status",headers=self.auth)
        self.assertEqual(stat.status_code,200)
        self.assertFalse(stat.json()["configured"])
        self.assertEqual(self.c.post("/api/anthbot/admin/store/custom-voice-inbox/sync",
                                     headers=self.write).status_code,503)
