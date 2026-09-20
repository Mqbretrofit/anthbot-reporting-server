from __future__ import annotations

import asyncio
from email.message import EmailMessage
import hashlib
import os
import re
import secrets
import smtplib
import ssl
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app as core


router = APIRouter()

_ACCOUNT_COOKIE = "anthbot_store_session"
_ACCOUNT_SESSION_SECONDS = 30 * 24 * 60 * 60
_LOGIN_CODE_SECONDS = 10 * 60
_LOGIN_CODE_RESEND_SECONDS = 60
_LOGIN_CODE_MAX_ATTEMPTS = 6
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SESSION_RE = re.compile(r"^abs_[A-Za-z0-9_-]{32,256}$")


class AccountEmailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str) -> str:
        return value.strip() or "en"


class AccountCodePayload(AccountEmailPayload):
    code: str = Field(min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            raise ValueError("code must contain exactly 6 digits")
        return normalized


def _normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("invalid email address")
    return normalized


def _init_account_tables() -> None:
    with core._db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                email_verified INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS store_login_codes (
                email TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS store_sessions (
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES store_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS store_user_clients (
                client_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES store_users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_store_sessions_user_id
                ON store_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_store_sessions_expires_at
                ON store_sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_store_user_clients_user_id
                ON store_user_clients(user_id);
            """
        )


def _smtp_host() -> str:
    return os.environ.get("ANTHBOT_SMTP_HOST", "").strip()


def _smtp_port() -> int:
    try:
        value = int(os.environ.get("ANTHBOT_SMTP_PORT", "587"))
    except ValueError:
        value = 587
    return value if 1 <= value <= 65535 else 587


def _smtp_username() -> str:
    return os.environ.get("ANTHBOT_SMTP_USERNAME", "").strip()


def _smtp_password() -> str:
    return os.environ.get("ANTHBOT_SMTP_PASSWORD", "")


def _smtp_from_email() -> str:
    email = os.environ.get("ANTHBOT_SMTP_FROM_EMAIL", "").strip() or _smtp_username()
    if email.casefold() == "support@mqbretrofithungary.online":
        return "support@anthbotmap.com"
    return email


def _smtp_from_name() -> str:
    name = os.environ.get("ANTHBOT_SMTP_FROM_NAME", "ANTHBOT Map").strip() or "ANTHBOT Map"
    if name.casefold() == "mqb retrofit hungary":
        return "ANTHBOT Map"
    return name


def _smtp_starttls() -> bool:
    return os.environ.get("ANTHBOT_SMTP_STARTTLS", "true").strip().casefold() in {
        "1", "true", "yes", "on"
    }


def _smtp_ssl() -> bool:
    return os.environ.get("ANTHBOT_SMTP_SSL", "false").strip().casefold() in {
        "1", "true", "yes", "on"
    }


def _smtp_ready() -> bool:
    return bool(_smtp_host() and _smtp_from_email())


def _login_code_hash(email: str, code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{email}|{code}".encode("utf-8")).hexdigest()


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _send_login_code(email: str, code: str, language: str) -> None:
    if not _smtp_ready():
        raise RuntimeError("Store account email delivery is not configured")

    language = language.strip().casefold()
    if language == "hu":
        subject = "ANTHBOT Map belépési kód"
        body = (
            "Az ANTHBOT Map Hangbolt belépési kódod:\n\n"
            f"{code}\n\n"
            "A kód 10 percig érvényes. Ha nem te kérted, hagyd figyelmen kívül ezt az üzenetet."
        )
    else:
        subject = "ANTHBOT Map sign-in code"
        body = (
            "Your ANTHBOT Map Voice Store sign-in code is:\n\n"
            f"{code}\n\n"
            "The code is valid for 10 minutes. If you did not request it, ignore this message."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{_smtp_from_name()} <{_smtp_from_email()}>"
    message["To"] = email
    message.set_content(body)

    context = ssl.create_default_context()
    if _smtp_ssl():
        with smtplib.SMTP_SSL(
            _smtp_host(),
            _smtp_port(),
            timeout=20,
            context=context,
        ) as smtp:
            if _smtp_username():
                smtp.login(_smtp_username(), _smtp_password())
            smtp.send_message(message)
        return

    with smtplib.SMTP(_smtp_host(), _smtp_port(), timeout=20) as smtp:
        smtp.ehlo()
        if _smtp_starttls():
            smtp.starttls(context=context)
            smtp.ehlo()
        if _smtp_username():
            smtp.login(_smtp_username(), _smtp_password())
        smtp.send_message(message)


def _store_orders_support_user_id(conn: Any) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_orders'"
    ).fetchone()
    if table is None:
        return False
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(store_orders)").fetchall()
    }
    return "user_id" in columns


def _claim_existing_orders(user_id: str, email: str) -> None:
    """Attach legacy purchases to the verified account without changing payment state."""
    _init_account_tables()
    with core._db() as conn:
        if not _store_orders_support_user_id(conn):
            return

        user = conn.execute(
            "SELECT stripe_customer_id FROM store_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        stripe_customer_id = (
            str(user["stripe_customer_id"]).strip()
            if user is not None and user["stripe_customer_id"]
            else ""
        )

        if not stripe_customer_id:
            row = conn.execute(
                """
                SELECT stripe_customer_id
                FROM store_orders
                WHERE lower(customer_email) = ?
                  AND stripe_customer_id IS NOT NULL
                  AND stripe_customer_id != ''
                ORDER BY COALESCE(paid_at, updated_at) DESC
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is not None and row["stripe_customer_id"]:
                stripe_customer_id = str(row["stripe_customer_id"]).strip()
                conn.execute(
                    """
                    UPDATE store_users
                    SET stripe_customer_id = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (stripe_customer_id, core._iso(), user_id),
                )

        if stripe_customer_id:
            conn.execute(
                """
                UPDATE store_orders
                SET user_id = ?
                WHERE (user_id IS NULL OR user_id = '')
                  AND (
                    lower(customer_email) = ?
                    OR stripe_customer_id = ?
                  )
                """,
                (user_id, email, stripe_customer_id),
            )
        else:
            conn.execute(
                """
                UPDATE store_orders
                SET user_id = ?
                WHERE (user_id IS NULL OR user_id = '')
                  AND lower(customer_email) = ?
                """,
                (user_id, email),
            )


def _create_or_get_user(email: str) -> dict[str, Any]:
    _init_account_tables()
    now = core._iso()
    with core._db() as conn:
        row = conn.execute(
            "SELECT * FROM store_users WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None:
            user_id = f"usr_{secrets.token_urlsafe(18)}"
            conn.execute(
                """
                INSERT INTO store_users (
                    user_id, email, email_verified, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (user_id, email, now, now),
            )
        else:
            user_id = str(row["user_id"])
            conn.execute(
                """
                UPDATE store_users
                SET email_verified = 1, updated_at = ?
                WHERE user_id = ?
                """,
                (now, user_id),
            )
        row = conn.execute(
            "SELECT * FROM store_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="could not create store account")
    _claim_existing_orders(user_id, email)
    return dict(row)


def _new_session(user_id: str) -> tuple[str, int]:
    _init_account_tables()
    raw_token = f"abs_{secrets.token_urlsafe(36)}"
    token_hash = _session_hash(raw_token)
    expires_at = int(time.time()) + _ACCOUNT_SESSION_SECONDS
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            "DELETE FROM store_sessions WHERE expires_at < ?",
            (int(time.time()),),
        )
        conn.execute(
            """
            INSERT INTO store_sessions (
                session_hash, user_id, created_at, expires_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, now, expires_at, now),
        )
    return raw_token, expires_at


def _set_account_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _ACCOUNT_COOKIE,
        token,
        max_age=_ACCOUNT_SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def current_user(request: Request, *, touch: bool = True) -> dict[str, Any] | None:
    _init_account_tables()
    token = request.cookies.get(_ACCOUNT_COOKIE, "").strip()
    if not _SESSION_RE.fullmatch(token):
        return None
    token_hash = _session_hash(token)
    now_epoch = int(time.time())
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT u.*, s.expires_at
            FROM store_sessions s
            JOIN store_users u ON u.user_id = s.user_id
            WHERE s.session_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None or int(row["expires_at"]) < now_epoch:
            conn.execute(
                "DELETE FROM store_sessions WHERE session_hash = ?",
                (token_hash,),
            )
            return None
        if touch:
            conn.execute(
                """
                UPDATE store_sessions
                SET last_seen_at = ?
                WHERE session_hash = ?
                """,
                (core._iso(), token_hash),
            )
    return dict(row)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Voice Store account sign-in is required")
    return user


def get_user(user_id: str) -> dict[str, Any] | None:
    _init_account_tables()
    with core._db() as conn:
        row = conn.execute(
            "SELECT * FROM store_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def user_id_for_client(client_id: str) -> str | None:
    _init_account_tables()
    with core._db() as conn:
        row = conn.execute(
            "SELECT user_id FROM store_user_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    return str(row["user_id"]) if row is not None else None


def link_client_to_user(client_id: str, user_id: str) -> None:
    _init_account_tables()
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO store_user_clients (
                client_id, user_id, linked_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                user_id = excluded.user_id,
                updated_at = excluded.updated_at
            """,
            (client_id, user_id, now, now),
        )


def set_stripe_customer_id(user_id: str, stripe_customer_id: str | None) -> None:
    normalized = str(stripe_customer_id or "").strip()
    if not normalized:
        return
    _init_account_tables()
    with core._db() as conn:
        conn.execute(
            """
            UPDATE store_users
            SET stripe_customer_id = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (normalized[:255], core._iso(), user_id),
        )


def _account_public(user: dict[str, Any]) -> dict[str, Any]:
    user_id = str(user["user_id"])
    purchased_count = 0
    linked_map_count = 0
    with core._db() as conn:
        if _store_orders_support_user_id(conn):
            purchased_count = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT COALESCE(NULLIF(community_id, ''), pack_id))
                    FROM store_orders
                    WHERE user_id = ? AND payment_status = 'paid'
                    """,
                    (user_id,),
                ).fetchone()[0]
                or 0
            )
        linked_map_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM store_user_clients WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            or 0
        )
    return {
        "authenticated": True,
        "email": str(user["email"]),
        "email_verified": bool(user["email_verified"]),
        "purchased_count": purchased_count,
        "linked_map_count": linked_map_count,
    }


@router.post("/api/anthbot/store/account/request-code")
async def request_login_code(payload: AccountEmailPayload) -> dict[str, Any]:
    _init_account_tables()
    if not _smtp_ready():
        raise HTTPException(
            status_code=503,
            detail="Voice Store sign-in email delivery is not configured",
        )

    now_epoch = int(time.time())
    with core._db() as conn:
        existing = conn.execute(
            "SELECT sent_at FROM store_login_codes WHERE email = ?",
            (payload.email,),
        ).fetchone()
        if (
            existing is not None
            and now_epoch - int(existing["sent_at"]) < _LOGIN_CODE_RESEND_SECONDS
        ):
            raise HTTPException(
                status_code=429,
                detail="Please wait before requesting another sign-in code",
            )

    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_urlsafe(18)
    code_hash = _login_code_hash(payload.email, code, salt)
    expires_at = now_epoch + _LOGIN_CODE_SECONDS
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO store_login_codes (
                email, code_hash, salt, created_at, expires_at, attempts, sent_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(email) DO UPDATE SET
                code_hash=excluded.code_hash,
                salt=excluded.salt,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                attempts=0,
                sent_at=excluded.sent_at
            """,
            (
                payload.email,
                code_hash,
                salt,
                core._iso(),
                expires_at,
                now_epoch,
            ),
        )

    try:
        await asyncio.to_thread(
            _send_login_code,
            payload.email,
            code,
            payload.language,
        )
    except Exception as err:
        with core._db() as conn:
            conn.execute(
                "DELETE FROM store_login_codes WHERE email = ?",
                (payload.email,),
            )
        raise HTTPException(
            status_code=502,
            detail=f"Could not send sign-in email: {type(err).__name__}",
        ) from err

    return {
        "sent": True,
        "expires_in": _LOGIN_CODE_SECONDS,
        "resend_after": _LOGIN_CODE_RESEND_SECONDS,
    }


@router.post("/api/anthbot/store/account/verify-code")
def verify_login_code(payload: AccountCodePayload) -> Response:
    _init_account_tables()
    now_epoch = int(time.time())
    with core._db() as conn:
        row = conn.execute(
            "SELECT * FROM store_login_codes WHERE email = ?",
            (payload.email,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
        if int(row["expires_at"]) < now_epoch:
            conn.execute(
                "DELETE FROM store_login_codes WHERE email = ?",
                (payload.email,),
            )
            raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
        attempts = int(row["attempts"])
        if attempts >= _LOGIN_CODE_MAX_ATTEMPTS:
            conn.execute(
                "DELETE FROM store_login_codes WHERE email = ?",
                (payload.email,),
            )
            raise HTTPException(status_code=429, detail="Too many invalid sign-in attempts")
        expected = _login_code_hash(payload.email, payload.code, str(row["salt"]))
        if not secrets.compare_digest(expected, str(row["code_hash"])):
            conn.execute(
                """
                UPDATE store_login_codes
                SET attempts = attempts + 1
                WHERE email = ?
                """,
                (payload.email,),
            )
            raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
        conn.execute(
            "DELETE FROM store_login_codes WHERE email = ?",
            (payload.email,),
        )

    user = _create_or_get_user(payload.email)
    token, expires_at = _new_session(str(user["user_id"]))
    response = JSONResponse(
        {
            **_account_public(user),
            "session_expires_at": expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )
    _set_account_cookie(response, token)
    return response


@router.get("/api/anthbot/store/account")
def account_status(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        return {
            "authenticated": False,
            "email": None,
            "email_verified": False,
            "purchased_count": 0,
            "linked_map_count": 0,
        }
    return _account_public(user)


@router.post("/api/anthbot/store/account/logout")
def logout(request: Request) -> Response:
    token = request.cookies.get(_ACCOUNT_COOKIE, "").strip()
    if _SESSION_RE.fullmatch(token):
        with core._db() as conn:
            conn.execute(
                "DELETE FROM store_sessions WHERE session_hash = ?",
                (_session_hash(token),),
            )
    response = JSONResponse(
        {"logged_out": True},
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(_ACCOUNT_COOKIE, path="/")
    return response
