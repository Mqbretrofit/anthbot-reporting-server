"""Durable, opt-in admin notifications for ANTHBOT Store events.

One event per source identifier (Stripe session / request). Email and each Web
Push subscription have independent delivery records so retries do not duplicate
previously successful deliveries. Events are created after business writes.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

import app as core
import store_accounts
from voice_builder_api import require_builder_admin

LOGGER = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_builder_admin)])
ROOT = "/api/anthbot/admin/notifications"
_TYPES = ("purchase", "custom_voice", "privacy_request")
_LOCK = threading.Lock()
_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com",
          "web.push.apple.com", "push.apple.com", "wns2-pn*.notify.windows.com",
          "notify.windows.com")
_KEY_PATH = "admin-push-vapid.pem"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admin_email: str = Field(default="", max_length=254)
    email_enabled: bool = True
    push_enabled: bool = True


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=8, max_length=2048)
    keys: dict[str, str]


def _db_init() -> None:
    with core._db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS store_admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT,
                UNIQUE(event_type,event_key)
            );
            CREATE INDEX IF NOT EXISTS idx_store_admin_notifications_created
                ON store_admin_notifications(created_at);
            CREATE TABLE IF NOT EXISTS store_admin_push_subscriptions (
                endpoint_hash TEXT PRIMARY KEY,
                subscription_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS store_admin_notification_deliveries (
                event_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(event_id, channel, target)
            );
            CREATE TABLE IF NOT EXISTS store_admin_notification_config (
                id INTEGER PRIMARY KEY CHECK(id=1),
                admin_email TEXT NOT NULL DEFAULT '',
                email_enabled INTEGER NOT NULL DEFAULT 1,
                push_enabled INTEGER NOT NULL DEFAULT 1
            );
            INSERT OR IGNORE INTO store_admin_notification_config
                (id,admin_email,email_enabled,push_enabled)
                VALUES (1,'',1,1);
        """)


def _config(conn) -> dict:
    row = conn.execute(
        "SELECT admin_email,email_enabled,push_enabled FROM store_admin_notification_config WHERE id=1"
    ).fetchone()
    return {
        "admin_email": str(row["admin_email"] or ""),
        "email_enabled": bool(row["email_enabled"]),
        "push_enabled": bool(row["push_enabled"]),
    }


def _vapid_key_path() -> Path:
    path = core._db_path().parent / _KEY_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    return path


def _private_key():
    path = _vapid_key_path()
    with _LOCK:
        if not path.exists():
            private = ec.generate_private_key(ec.SECP256R1())
            pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as dest:
                    dest.write(pem)
        try:
            return serialization.load_pem_private_key(path.read_bytes(),password=None)
        except (OSError, ValueError) as err:
            raise HTTPException(503, "A böngészős push kulcsa nem olvasható") from err


def _public_key() -> str:
    pub = _private_key().public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(pub).decode("ascii").rstrip("=")


def _notify_email(message_title: str, message_body: str, recipient: str) -> None:
    if not store_accounts._smtp_ready():
        raise RuntimeError("SMTP nincs konfigurálva")
    mail = EmailMessage()
    mail["Subject"] = "[ANTHBOT] " + message_title
    mail["To"] = recipient
    mail["From"] = (f"{store_accounts._smtp_from_name()} "
                    f"<{store_accounts._smtp_from_email()}>")
    mail.set_content(message_body + "\n\nANTHBOT Hangbolt admin: https://reports.mqbretrofithungary.online/dashboard/store")
    store_accounts._smtp_send_message(mail)


def _push(subscription: dict, title: str, body: str) -> None:
    from pywebpush import webpush
    private_pem = _private_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    email = store_accounts._smtp_from_email() or "support@anthbotmap.com"
    webpush(
        subscription_info=subscription,
        data=json.dumps({
            "title": title,
            "body": body,
            "url": "/dashboard/store",
        },ensure_ascii=False),
        vapid_private_key=private_pem,
        vapid_claims={"sub": "mailto:" + email},
        ttl=3600,
    )


def _deliver_once(event: dict, channel: str, target: str, subscription: dict | None = None) -> None:
    now=int(time.time())
    with core._db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO store_admin_notification_deliveries
                (event_id,channel,target,status,updated_at)
                VALUES (?, ?, ?, 'pending', ?)
        """,(event["id"],channel,target,core._iso()))
        row=conn.execute("""
            SELECT status,attempts,retry_at
            FROM store_admin_notification_deliveries
            WHERE event_id=? AND channel=? AND target=?
        """,(event["id"],channel,target)).fetchone()
        if row["status"] == "sent" or row["attempts"] >= 5 or int(row["retry_at"]) > now:
            return
        conn.execute("""
            UPDATE store_admin_notification_deliveries
            SET status='sending',attempts=attempts+1,retry_at=?,updated_at=?
            WHERE event_id=? AND channel=? AND target=?
        """,(now+120,core._iso(),event["id"],channel,target))
    try:
        if channel=="email":
            _notify_email(event["title"],event["message"],target)
        else:
            _push(subscription,event["title"],event["message"])
    except Exception as err:
        # Never include subscription authorization keys or arbitrary gateway data in logs.
        LOGGER.warning("ANTHBOT admin notification delivery failed (%s): %s",channel,type(err).__name__)
        with core._db() as conn:
            conn.execute("""
                UPDATE store_admin_notification_deliveries
                SET status='failed',retry_at=?,updated_at=?
                WHERE event_id=? AND channel=? AND target=?
            """,(now+300,core._iso(),event["id"],channel,target))
    else:
        with core._db() as conn:
            conn.execute("""
                UPDATE store_admin_notification_deliveries
                SET status='sent',retry_at=0,updated_at=?
                WHERE event_id=? AND channel=? AND target=?
            """,(core._iso(),event["id"],channel,target))


_DISPATCH_LOCK=threading.Lock()
def dispatch() -> None:
    if not _DISPATCH_LOCK.acquire(blocking=False):
        return
    try:
        _db_init()
        with core._db() as conn:
            config=_config(conn)
            events=[dict(x) for x in conn.execute(
                "SELECT * FROM store_admin_notifications ORDER BY id DESC LIMIT 100"
            )]
            subscriptions=[
                (row["endpoint_hash"],json.loads(row["subscription_json"]))
                for row in conn.execute("SELECT * FROM store_admin_push_subscriptions")
            ]
        for event in events:
            if config["email_enabled"] and config["admin_email"]:
                _deliver_once(event,"email",config["admin_email"])
            if config["push_enabled"]:
                for endpoint_hash, subscription in subscriptions:
                    _deliver_once(event,"push",endpoint_hash,subscription)
    finally:
        _DISPATCH_LOCK.release()


def dispatch_soon() -> None:
    threading.Thread(target=dispatch,name="anthbot-admin-notifications",daemon=True).start()


def create_event(event_type: str, event_key: str, title: str, message: str) -> None:
    if event_type not in _TYPES or not event_key.strip():
        raise ValueError("Invalid admin notification event")
    _db_init()
    with core._db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO store_admin_notifications
                (event_type,event_key,title,message,created_at)
            VALUES (?,?,?,?,?)
        """,(event_type,event_key,title[:180],message[:2500],core._iso()))
    # Do not open SMTP or start worker threads before an admin has configured a target.
    with core._db() as conn:
        cfg=_config(conn)
        have_push=bool(conn.execute(
            "SELECT 1 FROM store_admin_push_subscriptions LIMIT 1"
        ).fetchone())
    if (cfg["email_enabled"] and cfg["admin_email"]) or (cfg["push_enabled"] and have_push):
        dispatch_soon()


def on_voice_request(request_id: str, *, language: str, voice: str, model: str, contact: str) -> None:
    create_event(
        "custom_voice",request_id,"Új egyedi hangkérés",
        f"Robot: {model}\nNyelv: {language}\nHang: {voice}\nKapcsolat: {contact}\nAzonosító: {request_id}"
    )


def on_paid_purchase(order: dict) -> None:
    if order.get("payment_status") != "paid":
        return
    session = str(order.get("stripe_session_id") or "")
    if not session:
        return
    title="Sikeres ANTHBOT hangvásárlás"
    try:
        import store_api
        name=store_api._purchase_pack_name(store_api._resolve_order_pack(order))
    except (Exception,):
        name=str(order.get("community_id") or order.get("pack_id") or "Hangcsomag")
    amount=order.get("amount_total")
    money = f"{int(amount)/100:.2f} {str(order.get('currency') or 'eur').upper()}" if amount is not None else "Ismeretlen összeg"
    create_event("purchase",session,title,(
        f"Hang: {name}\nÖsszeg: {money}\n"
        f"Vásárló: {order.get('customer_email') or 'Nincs email'}\n"
        f"Stripe session: {session}"
    ))


def on_privacy_request(request_id: str, request_type: str) -> None:
    create_event("privacy_request",request_id,"Új adatvédelmi kérelem",
                 f"Típus: {request_type}\nAzonosító: {request_id}\nEllenőrizd a Hangbolt admin felületén.")


@router.get(ROOT)
def list_notifications(limit: int=30) -> dict:
    _db_init()
    limit=max(1,min(limit,100))
    with core._db() as conn:
        rows=[dict(x) for x in conn.execute("""
            SELECT id,event_type,title,message,created_at,read_at
            FROM store_admin_notifications ORDER BY id DESC LIMIT ?
        """,(limit,))]
        unread=conn.execute(
            "SELECT COUNT(*) FROM store_admin_notifications WHERE read_at IS NULL"
        ).fetchone()[0]
        sub_count=conn.execute("SELECT COUNT(*) FROM store_admin_push_subscriptions").fetchone()[0]
        config=_config(conn)
    return {"items":rows,"unread":unread,"push_subscriptions":sub_count,
            "settings":config,"vapid_public_key":_public_key(),
            "push_supported":True}


@router.put(ROOT+"/settings")
def save_settings(payload: Settings) -> dict:
    _db_init()
    addr=payload.admin_email.strip()
    if addr and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",addr):
        raise HTTPException(422,"Érvénytelen értesítési email cím")
    with core._db() as conn:
        existing=_config(conn)
        conn.execute("""
            UPDATE store_admin_notification_config
            SET admin_email=?,email_enabled=?,push_enabled=?
            WHERE id=1
        """,(addr,int(payload.email_enabled),int(payload.push_enabled)))
        # A new destination receives future notifications, not the entire
        # history. Earlier alerts remain visible in the admin inbox.
        if addr and addr != existing["admin_email"]:
            conn.execute("""
                INSERT OR IGNORE INTO store_admin_notification_deliveries
                    (event_id,channel,target,status,updated_at)
                SELECT id,'email',?,'sent',? FROM store_admin_notifications
            """,(addr,core._iso()))
    dispatch_soon()
    return {"saved":True,"settings":payload.model_copy(update={"admin_email":addr}).model_dump()}


def _valid_push_endpoint(endpoint: str) -> None:
    parsed=urlsplit(endpoint)
    if parsed.scheme!="https" or parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(422,"A böngésző push címe érvénytelen")
    host=(parsed.hostname or "").lower()
    if not (host in ("fcm.googleapis.com","updates.push.services.mozilla.com","web.push.apple.com",
                    "push.apple.com","notify.windows.com") or
            host.endswith(".notify.windows.com")):
        raise HTTPException(422,"Nem támogatott böngésző push szolgáltató")


@router.post(ROOT+"/subscriptions")
def subscribe(payload: Subscription) -> dict:
    _db_init()
    _valid_push_endpoint(payload.endpoint)
    if set(payload.keys) != {"p256dh","auth"} or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{8,256}",v) for v in payload.keys.values()
    ):
        raise HTTPException(422,"Érvénytelen böngésző push kulcsok")
    import hashlib
    fingerprint=hashlib.sha256(payload.endpoint.encode()).hexdigest()
    sub={"endpoint":payload.endpoint,"keys":payload.keys}
    with core._db() as conn:
        count=conn.execute("SELECT COUNT(*) FROM store_admin_push_subscriptions").fetchone()[0]
        if count>=20 and not conn.execute(
            "SELECT 1 FROM store_admin_push_subscriptions WHERE endpoint_hash=?", (fingerprint,)
        ).fetchone():
            raise HTTPException(409,"Legfeljebb 20 böngésző csatlakoztatható")
        conn.execute("""
            INSERT INTO store_admin_push_subscriptions(endpoint_hash,subscription_json,created_at)
            VALUES (?,?,?)
            ON CONFLICT(endpoint_hash) DO UPDATE SET subscription_json=excluded.subscription_json
        """,(fingerprint,json.dumps(sub),core._iso()))
        # Existing events are in the admin inbox, but not pushed retroactively.
        conn.execute("""
            INSERT OR IGNORE INTO store_admin_notification_deliveries
                (event_id,channel,target,status,updated_at)
            SELECT id,'push',?,'sent',? FROM store_admin_notifications
        """,(fingerprint,core._iso()))
    return {"subscribed":True,"endpoint_hash":fingerprint}


@router.delete(ROOT+"/subscriptions/{endpoint_hash}")
def unsubscribe(endpoint_hash: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}",endpoint_hash):
        raise HTTPException(422,"Érvénytelen push azonosító")
    _db_init()
    with core._db() as conn:
        result=conn.execute(
            "DELETE FROM store_admin_push_subscriptions WHERE endpoint_hash=?",
            (endpoint_hash,)
        )
    return {"removed":bool(result.rowcount)}


@router.post(ROOT+"/read")
def mark_read() -> dict:
    _db_init()
    with core._db() as conn:
        conn.execute(
            "UPDATE store_admin_notifications SET read_at=? WHERE read_at IS NULL",
            (core._iso(),)
        )
    return {"marked":True}


_SW = """self.addEventListener('push',event=>{
  let data={};
  try{data=event.data?event.data.json():{}}catch(_){}
  const title=String(data.title||'ANTHBOT értesítés');
  const body=String(data.body||'Új esemény érkezett.');
  event.waitUntil(self.registration.showNotification(title,{
    body,tag:'anthbot-'+title,icon:'/favicon.png',
    data:{url:'/dashboard/store'}
  }));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil(clients.openWindow('/dashboard/store'));
});
"""


@router.get("/anthbot-admin-push-sw.js")
def service_worker() -> Response:
    return Response(content=_SW,media_type="application/javascript",
                    headers={"Cache-Control":"no-cache","Service-Worker-Allowed":"/"})
