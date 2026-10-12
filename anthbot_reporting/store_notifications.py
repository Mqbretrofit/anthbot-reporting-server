"""ANTHBOT Store owner notifications via Home Assistant Companion and email.

The HA mobile-app notifications are the SAME channel as Home Assistant arrival
automations. Use the add-on Supervisor Core proxy, never a supplied external
endpoint or a separately configured bearer token.

Events and individual channel deliveries are persisted and idempotent.
"""
from __future__ import annotations

from email.message import EmailMessage
import json
import logging
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request as HttpRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import app as core
import store_accounts
from voice_builder_api import require_builder_admin

_LOG = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_builder_admin)])
ROOT = "/api/anthbot/admin/notifications"
_TYPES = ("purchase", "custom_voice", "privacy_request", "refund")
_DEVICE_RE = re.compile(r"^notify\.mobile_app_[a-z0-9_]{1,100}$")
_HA_API = "http://supervisor/core/api"
_SEND_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admin_email: str = Field(default="", max_length=254)
    email_enabled: bool = True
    ha_enabled: bool = True
    ha_targets: list[str] = Field(default_factory=list, max_length=10)


class TestPush(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ha_target: str = Field(min_length=1, max_length=128)


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
                ha_enabled INTEGER NOT NULL DEFAULT 1,
                ha_targets_json TEXT NOT NULL DEFAULT '[]'
            );
            INSERT OR IGNORE INTO store_admin_notification_config
                (id,admin_email,email_enabled,ha_enabled,ha_targets_json)
                VALUES (1,'',1,1,'[]');
        """)


def _config(conn) -> dict:
    row=conn.execute(
        """SELECT admin_email,email_enabled,ha_enabled,ha_targets_json
           FROM store_admin_notification_config WHERE id=1"""
    ).fetchone()
    try:
        targets=json.loads(row["ha_targets_json"] or "[]")
        if not isinstance(targets,list):
            targets=[]
    except (ValueError, TypeError):
        targets=[]
    return {
        "admin_email":str(row["admin_email"] or ""),
        "email_enabled":bool(row["email_enabled"]),
        "ha_enabled":bool(row["ha_enabled"]),
        "ha_targets":[x for x in targets if isinstance(x,str) and _DEVICE_RE.fullmatch(x)],
    }


def _ha_request(path: str, payload: dict | None = None):
    token=os.environ.get("SUPERVISOR_TOKEN","")
    if not token:
        raise RuntimeError("A Supervisor Home Assistant API nem elérhető. Az Appot homeassistant_api: true beállítással kell futtatni.")
    # Paths are constructed in this module only. Do not accept arbitrary URLs.
    request=HttpRequest(
        _HA_API+path,
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8") if payload is not None else None,
        headers={"Authorization":"Bearer "+token, "Content-Type":"application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request,timeout=12) as response:
            if response.status >= 400:
                raise RuntimeError("Home Assistant API nem fogadta el a hívást")
            content=response.read(512*1024)
    except (HTTPError,URLError,OSError,TimeoutError) as err:
        # Never expose Supervisor bearer credentials, full URLs or server response.
        raise RuntimeError("A Home Assistant értesítési szolgáltatás nem érhető el ("+type(err).__name__+")") from None
    try:
        return json.loads(content) if content else []
    except (ValueError,UnicodeError):
        raise RuntimeError("Érvénytelen Home Assistant API válasz") from None


def _available_ha_targets() -> list[str]:
    result=_ha_request("/services")
    if not isinstance(result,list):
        raise RuntimeError("Nem sikerült lekérni a Home Assistant értesítési szolgáltatásait")
    services=[]
    for domain in result:
        if not isinstance(domain,dict) or domain.get("domain")!="notify":
            continue
        service_map=domain.get("services") or {}
        names=service_map.keys() if isinstance(service_map,dict) else service_map
        for name in names:
            if isinstance(name,str) and _DEVICE_RE.fullmatch("notify."+name):
                services.append("notify."+name)
    return sorted(set(services))


def _notify_email(title: str, body: str, recipient: str) -> None:
    if not store_accounts._smtp_ready():
        raise RuntimeError("SMTP nincs konfigurálva")
    mail=EmailMessage()
    mail["Subject"]="[ANTHBOT] "+title
    mail["From"]=f"{store_accounts._smtp_from_name()} <{store_accounts._smtp_from_email()}>"
    mail["To"]=recipient
    mail.set_content(body+"\n\nHangbolt admin: https://reports.mqbretrofithungary.online/dashboard/store")
    store_accounts._smtp_send_message(mail)


def _notify_ha(target: str, title: str, message: str, event_id: str) -> None:
    if not _DEVICE_RE.fullmatch(target):
        raise ValueError("Érvénytelen Home Assistant mobile_app notify cél")
    _,name=target.split(".",1)
    _ha_request("/services/notify/"+name,{
        "title":title,
        "message":message,
        "data":{
            "tag":"anthbot-"+re.sub(r"[^A-Za-z0-9_-]","-",event_id)[:80],
            "group":"anthbot-store",
            "url":"https://reports.mqbretrofithungary.online/dashboard/store",
            "clickAction":"https://reports.mqbretrofithungary.online/dashboard/store",
        },
    })


def _send_event_once(event: dict, channel: str, target: str) -> None:
    now=int(time.time())
    with core._db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO store_admin_notification_deliveries
                (event_id,channel,target,status,updated_at)
                VALUES (?,?,?,'pending',?)
        """,(event["id"],channel,target,core._iso()))
        claim=conn.execute("""
            UPDATE store_admin_notification_deliveries
            SET status='sending', attempts=attempts+1, retry_at=?, updated_at=?
            WHERE event_id=? AND channel=? AND target=?
                AND status!='sent' AND attempts<5 AND retry_at<=?
        """,(now+120,core._iso(),event["id"],channel,target,now))
        if not claim.rowcount:
            return
    try:
        if channel=="email":
            _notify_email(event["title"],event["message"],target)
        else:
            _notify_ha(target,event["title"],event["message"],str(event["id"]))
    except Exception as err:
        _LOG.warning("ANTHBOT admin notification %s not delivered: %s",channel,type(err).__name__)
        with core._db() as conn:
            conn.execute("""
                UPDATE store_admin_notification_deliveries
                SET status='failed', retry_at=?,updated_at=?
                WHERE event_id=? AND channel=? AND target=?
            """,(now+300,core._iso(),event["id"],channel,target))
    else:
        with core._db() as conn:
            conn.execute("""
                UPDATE store_admin_notification_deliveries
                SET status='sent',retry_at=0,updated_at=?
                WHERE event_id=? AND channel=? AND target=?
            """,(core._iso(),event["id"],channel,target))


def dispatch() -> None:
    if not _SEND_LOCK.acquire(blocking=False):
        return
    try:
        _db_init()
        with core._db() as conn:
            config=_config(conn)
            events=[dict(x) for x in conn.execute("""
                SELECT id,title,message FROM store_admin_notifications
                ORDER BY id DESC LIMIT 200
            """)]
        for event in events:
            if config["email_enabled"] and config["admin_email"]:
                _send_event_once(event,"email",config["admin_email"])
            if config["ha_enabled"]:
                for target in config["ha_targets"]:
                    _send_event_once(event,"ha",target)
    except Exception as err:
        _LOG.warning("ANTHBOT notification delivery loop: %s",type(err).__name__)
    finally:
        _SEND_LOCK.release()


def dispatch_soon() -> None:
    # This does not block customer checkout or the privacy form.
    threading.Thread(target=dispatch,name="anthbot-owner-alert",daemon=True).start()


def _loop() -> None:
    while True:
        # Retry temporary failures and survive Home Assistant restarts.
        threading.Event().wait(90)
        dispatch()


def start_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED=True
        threading.Thread(target=_loop,name="anthbot-owner-alert-retry",daemon=True).start()
    dispatch_soon()


def create_event(event_type: str, key: str, title: str, message: str) -> None:
    if event_type not in _TYPES or not key:
        raise ValueError("Unknown owner alert event")
    _db_init()
    with core._db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO store_admin_notifications
                (event_type,event_key,title,message,created_at)
            VALUES (?,?,?,?,?)
        """,(event_type,key,title[:180],message[:2500],core._iso()))
        config=_config(conn)
    if (config["email_enabled"] and config["admin_email"]) or (
        config["ha_enabled"] and config["ha_targets"]
    ):
        dispatch_soon()


def on_voice_request(request_id: str, *, language: str, voice: str, model: str, contact: str) -> None:
    create_event("custom_voice",request_id,"Új egyedi ANTHBOT hangkérés",
                 f"Robot: {model}\nNyelv: {language}\nHang: {voice}\nKapcsolat: {contact}\nAzonosító: {request_id}")


def on_paid_purchase(order: dict) -> None:
    if str(order.get("payment_status") or "").casefold()!="paid":
        return
    session=str(order.get("stripe_session_id") or "")
    if not session:
        return
    import store_api
    try:
        name=store_api._purchase_pack_name(store_api._resolve_order_pack(order))
    except HTTPException:
        name=str(order.get("community_id") or order.get("pack_id") or "Hangcsomag")
    amount=order.get("amount_total")
    try:
        money=f"{int(amount)/100:.2f} {str(order.get('currency') or 'eur').upper()}"
    except (TypeError,ValueError):
        money="Ismeretlen összeg"
    create_event("purchase",session,"Sikeres ANTHBOT hangvásárlás",
                 f"Hang: {name}\nÖsszeg: {money}\nVásárló: {order.get('customer_email') or 'Nincs email'}\nStripe session: {session}")


def on_privacy_request(request_id: str, request_type: str) -> None:
    create_event("privacy_request",request_id,"Új adatvédelmi kérelem",
                 f"Típus: {request_type}\nAzonosító: {request_id}")


def on_refund(payment_intent: str, count: int) -> None:
    if count:
        create_event("refund",payment_intent,"ANTHBOT visszatérítés",
                     f"A Stripe visszatérítést jelzett ({count} rendelés). Ellenőrizd a Hangbolt adminfelületet.")


@router.get(ROOT)
def get_notifications(limit: int = 30) -> dict:
    _db_init()
    limit=max(1,min(limit,100))
    with core._db() as conn:
        items=[dict(x) for x in conn.execute("""
            SELECT id,event_type,title,message,created_at,read_at
            FROM store_admin_notifications ORDER BY id DESC LIMIT ?
        """,(limit,))]
        unread=conn.execute("""
            SELECT COUNT(*) FROM store_admin_notifications WHERE read_at IS NULL
        """).fetchone()[0]
        config=_config(conn)
    return {"items":items,"unread":unread,"settings":config}


@router.get(ROOT+"/ha-devices")
def ha_devices() -> dict:
    try:
        return {"items":_available_ha_targets(),"available":True}
    except RuntimeError as err:
        return {"items":[],"available":False,"detail":str(err)}


@router.put(ROOT+"/settings")
def save_settings(payload: Settings) -> dict:
    _db_init()
    email=payload.admin_email.strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email):
        raise HTTPException(422,"Érvénytelen admin email cím")
    targets=list(dict.fromkeys(payload.ha_targets))
    if any(not _DEVICE_RE.fullmatch(x) for x in targets):
        raise HTTPException(422,"Csak Home Assistant mobile_app értesítések állíthatók be")
    if targets:
        try:
            available=_available_ha_targets()
        except RuntimeError as err:
            raise HTTPException(503,str(err)) from None
        if any(x not in available for x in targets):
            raise HTTPException(422,"A kiválasztott telefon notify szolgáltatása nem érhető el Home Assistantban")
    with core._db() as conn:
        old=_config(conn)
        conn.execute("""
            UPDATE store_admin_notification_config
            SET admin_email=?,email_enabled=?,ha_enabled=?,ha_targets_json=?
            WHERE id=1
        """,(email,int(payload.email_enabled),int(payload.ha_enabled),
              json.dumps(targets)))
        # Do not replay old events when newly configuring a destination.
        new_targets=[x for x in targets if x not in old["ha_targets"]]
        for target in new_targets:
            conn.execute("""
                INSERT OR IGNORE INTO store_admin_notification_deliveries
                    (event_id,channel,target,status,updated_at)
                SELECT id,'ha',?,'sent',? FROM store_admin_notifications
            """,(target,core._iso()))
        if email and email!=old["admin_email"]:
            conn.execute("""
                INSERT OR IGNORE INTO store_admin_notification_deliveries
                    (event_id,channel,target,status,updated_at)
                SELECT id,'email',?,'sent',? FROM store_admin_notifications
            """,(email,core._iso()))
    dispatch_soon()
    return {"saved":True,"settings":{
        "admin_email":email,"email_enabled":payload.email_enabled,
        "ha_enabled":payload.ha_enabled,"ha_targets":targets,
    }}


@router.post(ROOT+"/ha-test")
def send_test(payload: TestPush) -> dict:
    target=payload.ha_target
    if not _DEVICE_RE.fullmatch(target):
        raise HTTPException(422,"Érvénytelen notify.mobile_app szolgáltatás")
    try:
        available=_available_ha_targets()
        if target not in available:
            raise HTTPException(422,"Ez a telefon nem található a Home Assistantban")
        _notify_ha(target,"ANTHBOT tesztértesítés",
                   "A hangvásárlásokról és egyedi hangkérésekről ide fog érkezni az értesítés.",
                   "test-"+str(int(time.time())))
    except RuntimeError as err:
        raise HTTPException(502,str(err)) from None
    return {"sent":True,"target":target}


@router.post(ROOT+"/read")
def mark_read() -> dict:
    _db_init()
    with core._db() as conn:
        conn.execute("UPDATE store_admin_notifications SET read_at=? WHERE read_at IS NULL",(core._iso(),))
    return {"marked":True}
