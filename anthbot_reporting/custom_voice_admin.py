"""Admin-only workflow for incoming custom ANTHBOT voice requests.

Existing public request intake, Stripe checkout, Voice Builder and Store licenses
remain unchanged. Never infer a payment from an admin-entered status.
"""
from __future__ import annotations

from email.message import EmailMessage
import re
import smtplib
import sqlite3
import ssl
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import app as core
import store_api
import store_accounts
from voice_builder_api import require_builder_admin

router = APIRouter(dependencies=[Depends(require_builder_admin)])
ROOT = "/api/anthbot/admin/store/custom-voice-workflow"
STATUS = ("new", "contacted", "preview", "quoted", "awaiting_payment",
          "in_production", "ready", "completed", "cancelled")
EXTRA_COLUMNS = {
    "admin_notes": "TEXT NOT NULL DEFAULT ''",
    "quote_amount": "INTEGER",
    "builder_job_id": "TEXT NOT NULL DEFAULT ''",
    "pack_id": "TEXT NOT NULL DEFAULT ''",
    "last_contacted_at": "TEXT",
}


def _init() -> None:
    store_api._init_store_tables()
    with core._db() as conn:
        # Hold a write lock while inspecting and extending a legacy database.
        conn.execute("BEGIN IMMEDIATE")
        known = {r["name"] for r in conn.execute(
            "PRAGMA table_info(store_custom_voice_requests)"
        )}
        for name, definition in EXTRA_COLUMNS.items():
            if name not in known:
                conn.execute(
                    f"ALTER TABLE store_custom_voice_requests ADD COLUMN {name} {definition}"
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_custom_voice_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES store_custom_voice_requests(request_id)
            )
        """)
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_custom_voice_messages_request
                        ON store_custom_voice_messages(request_id, sent_at)""")


def _verified_payment(conn: sqlite3.Connection, row: dict) -> dict:
    """Read verified Store orders; a status label is not proof of payment."""
    pack_id = str(row.get("pack_id") or "")
    contact = str(row.get("contact") or "").strip().casefold()
    if not pack_id or not contact:
        return {"verified": False}
    try:
        pack = store_api._find_uploaded_pack(pack_id)
    except HTTPException:
        return {"verified": False}
    voice_id = str(pack.get("community_id") or "").strip()
    orders = conn.execute("""
        SELECT stripe_session_id, paid_at, amount_total, currency
        FROM store_orders
        WHERE payment_status='paid'
          AND COALESCE(status, '') NOT IN ('refunded', 'canceled')
          AND LOWER(TRIM(COALESCE(customer_email, '')))=?
          AND (pack_id=? OR (? != '' AND community_id=?))
        ORDER BY paid_at DESC LIMIT 1
    """, (contact, pack_id, voice_id, voice_id)).fetchall()
    if not orders:
        return {"verified": False}
    order = dict(orders[0])
    return {"verified": True, **order}


def _entry(conn: sqlite3.Connection, request_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM store_custom_voice_requests WHERE request_id=?",
        (request_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Nincs ilyen egyedi hangkérés")
    result = dict(row)
    result["payment"] = _verified_payment(conn, result)
    result["messages"] = [
        dict(item) for item in conn.execute(
            """SELECT message_id, recipient, subject, body, sent_at
               FROM store_custom_voice_messages WHERE request_id=?
               ORDER BY message_id DESC LIMIT 30""", (request_id,)
        )
    ]
    return result


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "new", "contacted", "preview", "quoted", "awaiting_payment",
        "in_production", "ready", "completed", "cancelled"
    ] | None = None
    admin_notes: str | None = Field(default=None, max_length=4000)
    quote_amount: int | None = Field(default=None, ge=2499, le=10000000)
    builder_job_id: str | None = Field(default=None, max_length=128)
    pack_id: str | None = Field(default=None, max_length=160)


class Email(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=8000)


@router.get(ROOT)
def list_requests(limit: int = 200) -> dict:
    _init()
    limit = max(1, min(limit, 500))
    with core._db() as conn:
        ids = conn.execute(
            """SELECT request_id FROM store_custom_voice_requests
               ORDER BY created_at DESC LIMIT ?""", (limit,)
        ).fetchall()
        return {"count": len(ids), "items": [
            _entry(conn, r["request_id"]) for r in ids
        ]}


@router.get(ROOT + "/{request_id}")
def get_request(request_id: str) -> dict:
    _init()
    with core._db() as conn:
        return _entry(conn, request_id)


@router.patch(ROOT + "/{request_id}")
def update_request(request_id: str, payload: Edit) -> dict:
    _init()
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(422, "Nem adtál meg módosítandó mezőt")
    if updates.get("status") is None and "status" in updates:
        raise HTTPException(422, "Az állapot nem lehet üres")
    if "admin_notes" in updates:
        updates["admin_notes"] = (updates["admin_notes"] or "").strip()
    for key in ("builder_job_id", "pack_id"):
        if key in updates:
            updates[key] = (updates[key] or "").strip()
    if updates.get("builder_job_id"):
        from voice_builder_api import job_or_404
        job_or_404(updates["builder_job_id"])
    if updates.get("pack_id"):
        store_api._find_uploaded_pack(updates["pack_id"])
    allowed = set(EXTRA_COLUMNS) | {"status"}
    if not set(updates) <= allowed:
        raise HTTPException(422, "Ismeretlen mező")
    with core._db() as conn:
        before = _entry(conn, request_id)
        if updates.get("status") == "completed":
            pack_id = updates.get("pack_id", before["pack_id"])
            if not pack_id:
                raise HTTPException(409, "Lezáráshoz előbb csatolj egy elkészült hangcsomagot")
        updates["updated_at"] = core._iso()
        columns = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE store_custom_voice_requests SET {columns} WHERE request_id=?",
            (*updates.values(), request_id)
        )
        return {"updated": True, "item": _entry(conn, request_id)}


@router.post(ROOT + "/{request_id}/email")
def send_email(request_id: str, payload: Email) -> dict:
    _init()
    subject = payload.subject.strip()
    body = payload.body.strip()
    if not subject or not body or "\n" in subject or "\r" in subject:
        raise HTTPException(422, "Érvénytelen email tárgy vagy szöveg")
    with core._db() as conn:
        item = _entry(conn, request_id)
    recipient = str(item["contact"]).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise HTTPException(422, "A megadott kapcsolat nem email cím")
    if not store_accounts._smtp_ready():
        raise HTTPException(503, "Az SMTP nincs konfigurálva")
    mail = EmailMessage()
    mail["Subject"] = subject
    mail["To"] = recipient
    mail["From"] = (f"{store_accounts._smtp_from_name()} "
                    f"<{store_accounts._smtp_from_email()}>")
    mail.set_content(body)
    try:
        store_accounts._smtp_send_message(mail)
    except (smtplib.SMTPException, ssl.SSLError, TimeoutError, OSError) as exc:
        raise HTTPException(
            502, store_accounts._smtp_failure_detail(exc)
        ) from None
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """INSERT INTO store_custom_voice_messages
               (request_id, recipient, subject, body, sent_at)
               VALUES (?, ?, ?, ?, ?)""",
            (request_id, recipient, subject, body, now)
        )
        conn.execute(
            """UPDATE store_custom_voice_requests
               SET last_contacted_at=?, updated_at=?,
                   status=CASE WHEN status='new' THEN 'contacted' ELSE status END
               WHERE request_id=?""",
            (now, now, request_id)
        )
        return {"sent": True, "item": _entry(conn, request_id)}
