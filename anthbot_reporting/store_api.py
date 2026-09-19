from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Literal
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import app as core


router = APIRouter()

STORE_SCHEMA = "anthbot-community-voice-store-v1"
_CURRENCY_RE = re.compile(r"^[a-zA-Z]{3}$")
_SESSION_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
_LICENSE_RE = re.compile(r"^abv1\.([A-Za-z0-9_-]+)\.([0-9a-f]{64})$")
_PAIR_RE = re.compile(r"^abp_[A-Za-z0-9_-]{24,128}$")
_CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300
_STORE_PAIR_TTL_SECONDS = 7 * 24 * 60 * 60
# Stripe Managed Payments requires an eligible product tax code. Community
# voice packs are one-time downloadable digital audio with permanent access.
_VOICE_PACK_TAX_CODE = "txcd_10401100"

_LOGGER = logging.getLogger(__name__)


class CheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(min_length=1, max_length=160)
    pair_code: str | None = Field(default=None, min_length=20, max_length=160)


class StoreClientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=32, max_length=512)

    @field_validator("client_token")
    @classmethod
    def _validate_client_token(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLIENT_TOKEN_RE.fullmatch(normalized):
            raise ValueError("invalid store client token")
        return normalized


class EntitlementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license_key: str = Field(min_length=20, max_length=1024)


class StorePricingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: Literal["free", "paid"]
    price_amount: int = Field(default=0, ge=0, le=100_000_000)
    currency: str = Field(default="eur", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _CURRENCY_RE.fullmatch(normalized):
            raise ValueError("currency must be a three-letter ISO code")
        return normalized

    @model_validator(mode="after")
    def _validate_paid_price(self) -> "StorePricingPayload":
        if self.access == "paid" and self.price_amount <= 0:
            raise ValueError("paid voice packs require price_amount > 0")
        return self


def _iso_from_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return core._iso()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _store_enabled() -> bool:
    return _env_flag("ANTHBOT_STORE_ENABLED", False)


def _stripe_secret_key() -> str:
    return os.environ.get("ANTHBOT_STRIPE_SECRET_KEY", "").strip()


def _stripe_webhook_secret() -> str:
    return os.environ.get("ANTHBOT_STRIPE_WEBHOOK_SECRET", "").strip()


def _license_secret() -> str:
    return os.environ.get("ANTHBOT_STORE_LICENSE_SECRET", "").strip()


def _stripe_automatic_tax() -> bool:
    return _env_flag("ANTHBOT_STRIPE_AUTOMATIC_TAX", False)


def _checkout_ready() -> bool:
    return bool(
        _store_enabled()
        and _stripe_secret_key()
        and _stripe_webhook_secret()
        and _license_secret()
    )


def _require_checkout_ready() -> None:
    if not _store_enabled():
        raise HTTPException(status_code=503, detail="voice store checkout is disabled")
    if not _stripe_secret_key():
        raise HTTPException(status_code=503, detail="Stripe secret key is not configured")
    if not _stripe_webhook_secret():
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not _license_secret():
        raise HTTPException(status_code=503, detail="voice store license secret is not configured")


def _init_store_tables() -> None:
    with core._db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_orders (
                stripe_session_id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                amount_total INTEGER,
                currency TEXT,
                customer_email TEXT,
                stripe_customer_id TEXT,
                stripe_payment_intent_id TEXT,
                client_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS store_client_pairings (
                pair_code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_store_orders_pack_id
                ON store_orders(pack_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_paid_at
                ON store_orders(paid_at);
            CREATE INDEX IF NOT EXISTS idx_store_pairings_client_id
                ON store_client_pairings(client_id);
            """
        )

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(store_orders)").fetchall()
        }
        if "client_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN client_id TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_store_orders_client_id
            ON store_orders(client_id)
            """
        )


def _client_id_from_token(client_token: str) -> str:
    """Derive a stable opaque client ID without storing the bearer token."""
    normalized = client_token.strip()
    if not _CLIENT_TOKEN_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="invalid store client token")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"abvc_{digest[:40]}"


def _create_store_pairing(client_token: str) -> tuple[str, int]:
    """Create a temporary browser pairing code for one ANTHBOT Map install."""
    _init_store_tables()
    client_id = _client_id_from_token(client_token)
    pair_code = f"abp_{secrets.token_urlsafe(32)}"
    expires_at = int(time.time()) + _STORE_PAIR_TTL_SECONDS
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            "DELETE FROM store_client_pairings WHERE expires_at < ?",
            (int(time.time()),),
        )
        conn.execute(
            """
            INSERT INTO store_client_pairings (
                pair_code, client_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?)
            """,
            (pair_code, client_id, now, expires_at),
        )
    return pair_code, expires_at


def _client_id_from_pairing(pair_code: str | None) -> str | None:
    """Resolve a non-secret browser pairing code to one Map client."""
    if pair_code is None:
        return None
    normalized = pair_code.strip()
    if not _PAIR_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="invalid store pairing code")

    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT client_id, expires_at
            FROM store_client_pairings
            WHERE pair_code = ?
            """,
            (normalized,),
        ).fetchone()
    if row is None or int(row["expires_at"]) < int(time.time()):
        raise HTTPException(status_code=401, detail="voice store pairing expired")
    return str(row["client_id"])


def _uploaded_records() -> list[dict[str, Any]]:
    return [
        item
        for item in core._uploaded_voice_pack_registry().get("packs", [])
        if isinstance(item, dict)
    ]


def _is_paid(record: dict[str, Any]) -> bool:
    return str(record.get("access", "free")).strip().casefold() == "paid"


def _price_amount(record: dict[str, Any]) -> int:
    try:
        return max(0, int(record.get("price_amount", 0)))
    except (TypeError, ValueError):
        return 0


def _currency(record: dict[str, Any]) -> str:
    value = str(record.get("currency", "eur")).strip().lower()
    return value if _CURRENCY_RE.fullmatch(value) else "eur"


def _find_uploaded_pack(pack_id: str) -> dict[str, Any]:
    match = next(
        (item for item in _uploaded_records() if str(item.get("id", "")) == pack_id),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="voice pack not found")
    return match


def _public_paid_pack(record: dict[str, Any], request: Request) -> dict[str, Any]:
    public = dict(record)
    public.pop("filename", None)
    public.pop("uploaded_at", None)
    public.pop("music_url", None)
    public["access"] = "paid"
    public["price_amount"] = _price_amount(record)
    public["currency"] = _currency(record)
    public["checkout_available"] = _checkout_ready()
    public["store_url"] = f"{core._public_base_url(request)}/store"
    return public


def _public_free_pack(record: dict[str, Any]) -> dict[str, Any]:
    public = dict(record)
    public["access"] = "free"
    public["price_amount"] = 0
    public["currency"] = None
    public["checkout_available"] = False
    return public


def _store_catalog(request: Request) -> dict[str, Any]:
    free_registry = core._voice_pack_registry(request)
    free_packs = [
        _public_free_pack(item)
        for item in free_registry.get("packs", [])
        if isinstance(item, dict)
    ]
    paid_packs = [
        _public_paid_pack(item, request)
        for item in _uploaded_records()
        if _is_paid(item)
    ]
    packs = free_packs + paid_packs
    packs.sort(
        key=lambda item: (
            str(item.get("language", "")).casefold(),
            str(item.get("variant_name", "")).casefold(),
            str(item.get("id", "")).casefold(),
        )
    )
    return {
        "schema": STORE_SCHEMA,
        "checkout_available": _checkout_ready(),
        "packs": packs,
    }


def _stripe_session_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)

    # stripe-python returns StripeObject instances. Current releases expose
    # .to_dict(); older releases used .to_dict_recursive().
    for method_name in ("to_dict", "to_dict_recursive"):
        converter = getattr(value, method_name, None)
        if not callable(converter):
            continue
        try:
            payload = converter()
        except TypeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise HTTPException(
        status_code=502,
        detail=(
            "Stripe returned an unsupported Checkout Session object "
            f"({type(value).__name__})"
        ),
    )


def _stripe_error_detail(err: BaseException) -> str:
    user_message = getattr(err, "user_message", None)
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()[:500]
    message = str(err).strip()
    if message:
        return message[:500]
    return "Stripe request failed"


def _configure_stripe() -> None:
    key = _stripe_secret_key()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe secret key is not configured")
    stripe.api_key = key
    stripe.max_network_retries = 1


def _create_checkout_session(
    record: dict[str, Any],
    request: Request,
    *,
    client_id: str | None = None,
    pair_code: str | None = None,
) -> dict[str, Any]:
    amount = _price_amount(record)
    currency = _currency(record)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="voice pack price is not configured")

    base = core._public_base_url(request)
    pack_id = str(record.get("id", "")).strip()
    community_id = str(record.get("community_id", "")).strip()
    language = str(record.get("language", "")).strip() or "ANTHBOT"
    variant = str(record.get("variant_name", "")).strip()
    product_name = (
        f"{language} · {variant}"
        if variant
        else f"{language} · Community voice"
    )

    cancel_url = f"{base}/store?cancelled=1"
    if pair_code:
        cancel_url = f"{cancel_url}&pair={quote(pair_code)}"

    metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        metadata["store_client_id"] = client_id

    payment_metadata = {"pack_id": pack_id}
    if client_id:
        payment_metadata["store_client_id"] = client_id

    params: dict[str, Any] = {
        "mode": "payment",
        "success_url": f"{base}/store/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": cancel_url,
        "client_reference_id": pack_id,
        "customer_creation": "always",
        "locale": "auto",
        "managed_payments": {"enabled": False},
        "line_items": [
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": amount,
                    "product_data": {
                        "name": product_name,
                        "description": "ANTHBOT Community voice pack · one-time purchase",
                        "tax_code": _VOICE_PACK_TAX_CODE,
                    },
                },
                "quantity": 1,
            }
        ],
        "metadata": metadata,
        "payment_intent_data": {
            "metadata": payment_metadata,
        },
    }
    if _stripe_automatic_tax():
        params["automatic_tax"] = {"enabled": True}

    _configure_stripe()
    try:
        session = stripe.checkout.Session.create(**params)
    except stripe.StripeError as err:
        detail = _stripe_error_detail(err)
        _LOGGER.warning(
            "Stripe Checkout Session creation failed: %s (request_id=%s)",
            detail,
            getattr(err, "request_id", None),
        )
        raise HTTPException(status_code=502, detail=f"Stripe: {detail}") from err
    except Exception as err:
        _LOGGER.exception("Unexpected Stripe Checkout Session creation failure")
        raise HTTPException(
            status_code=502,
            detail=f"Stripe connection failed: {type(err).__name__}",
        ) from err
    return _stripe_session_dict(session)


def _retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid checkout session id")

    _configure_stripe()
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.StripeError as err:
        detail = _stripe_error_detail(err)
        _LOGGER.warning(
            "Stripe Checkout Session retrieval failed: %s (request_id=%s)",
            detail,
            getattr(err, "request_id", None),
        )
        raise HTTPException(status_code=502, detail=f"Stripe: {detail}") from err
    except Exception as err:
        _LOGGER.exception("Unexpected Stripe Checkout Session retrieval failure")
        raise HTTPException(
            status_code=502,
            detail=f"Stripe connection failed: {type(err).__name__}",
        ) from err
    return _stripe_session_dict(session)


def _session_pack_id(session: dict[str, Any]) -> str:
    metadata = session.get("metadata")
    if isinstance(metadata, dict):
        pack_id = str(metadata.get("pack_id", "")).strip()
        if pack_id:
            return pack_id
    return str(session.get("client_reference_id", "")).strip()


def _session_client_id(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("store_client_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.startswith("abvc_") or len(normalized) > 64:
        return None
    return normalized


def _session_customer_email(session: dict[str, Any]) -> str | None:
    details = session.get("customer_details")
    if isinstance(details, dict):
        email = details.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()[:320]
    email = session.get("customer_email")
    if isinstance(email, str) and email.strip():
        return email.strip()[:320]
    return None


def _upsert_order_from_session(session: dict[str, Any]) -> dict[str, Any]:
    _init_store_tables()
    session_id = str(session.get("id", "")).strip()
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid Stripe checkout session")

    pack_id = _session_pack_id(session)
    if not pack_id:
        raise HTTPException(status_code=422, detail="checkout session is missing pack_id")

    payment_status = str(session.get("payment_status", "unpaid")).strip().lower() or "unpaid"
    checkout_status = str(session.get("status", "open")).strip().lower() or "open"
    status_value = "paid" if payment_status == "paid" else checkout_status
    amount_total = session.get("amount_total")
    try:
        amount_total = int(amount_total) if amount_total is not None else None
    except (TypeError, ValueError):
        amount_total = None
    currency = str(session.get("currency", "")).strip().lower() or None
    customer_email = _session_customer_email(session)
    customer_id = session.get("customer")
    customer_id = str(customer_id)[:255] if customer_id else None
    payment_intent = session.get("payment_intent")
    payment_intent = str(payment_intent)[:255] if payment_intent else None
    client_id = _session_client_id(session)
    created_at = _iso_from_epoch(session.get("created"))
    now = core._iso()

    with core._db() as conn:
        existing = conn.execute(
            "SELECT paid_at FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
        paid_at = (
            existing["paid_at"]
            if existing is not None and existing["paid_at"]
            else (now if payment_status == "paid" else None)
        )
        conn.execute(
            """
            INSERT INTO store_orders (
                stripe_session_id, pack_id, status, payment_status,
                amount_total, currency, customer_email, stripe_customer_id,
                stripe_payment_intent_id, client_id, created_at, updated_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stripe_session_id) DO UPDATE SET
                pack_id=excluded.pack_id,
                status=excluded.status,
                payment_status=excluded.payment_status,
                amount_total=excluded.amount_total,
                currency=excluded.currency,
                customer_email=excluded.customer_email,
                stripe_customer_id=excluded.stripe_customer_id,
                stripe_payment_intent_id=excluded.stripe_payment_intent_id,
                client_id=COALESCE(excluded.client_id, store_orders.client_id),
                updated_at=excluded.updated_at,
                paid_at=COALESCE(store_orders.paid_at, excluded.paid_at)
            """,
            (
                session_id,
                pack_id,
                status_value,
                payment_status,
                amount_total,
                currency,
                customer_email,
                customer_id,
                payment_intent,
                client_id,
                created_at,
                now,
                paid_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=500, detail="could not persist store order")
    return dict(row)


def _order_by_session(session_id: str) -> dict[str, Any] | None:
    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _license_for_order(order: dict[str, Any]) -> str:
    secret = _license_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="voice store license secret is not configured")
    session_id = str(order.get("stripe_session_id", "")).strip()
    pack_id = str(order.get("pack_id", "")).strip()
    encoded = base64.urlsafe_b64encode(session_id.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{session_id}|{pack_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"abv1.{encoded}.{signature}"


def _order_from_license(license_key: str) -> dict[str, Any]:
    match = _LICENSE_RE.fullmatch(license_key.strip())
    if not match:
        raise HTTPException(status_code=401, detail="invalid voice store license")
    encoded, supplied_signature = match.groups()
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        session_id = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="invalid voice store license")

    order = _order_by_session(session_id)
    if order is None or str(order.get("payment_status", "")).casefold() != "paid":
        raise HTTPException(status_code=401, detail="voice store license is not active")

    expected = _license_for_order(order)
    expected_signature = expected.rsplit(".", 1)[1]
    if not secrets.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(status_code=401, detail="invalid voice store license")
    return order


def _order_public(order: dict[str, Any], request: Request) -> dict[str, Any]:
    pack = _find_uploaded_pack(str(order.get("pack_id", "")))
    paid = str(order.get("payment_status", "")).casefold() == "paid"
    body: dict[str, Any] = {
        "stripe_session_id": order.get("stripe_session_id"),
        "pack_id": order.get("pack_id"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "amount_total": order.get("amount_total"),
        "currency": order.get("currency"),
        "customer_email": order.get("customer_email"),
        "paid_at": order.get("paid_at"),
        "map_linked": bool(order.get("client_id")),
        "pack": _public_paid_pack(pack, request),
    }
    if paid:
        body["license_key"] = _license_for_order(order)
    return body


def _verify_stripe_signature(body: bytes, signature_header: str | None) -> None:
    secret = _stripe_webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not signature_header:
        raise HTTPException(status_code=400, detail="missing Stripe-Signature")

    timestamp: int | None = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        if key.strip() == "t":
            try:
                timestamp = int(value.strip())
            except ValueError:
                timestamp = None
        elif key.strip() == "v1":
            signatures.append(value.strip())

    if timestamp is None or not signatures:
        raise HTTPException(status_code=400, detail="invalid Stripe-Signature")
    if abs(int(time.time()) - timestamp) > _STRIPE_WEBHOOK_TOLERANCE_SECONDS:
        raise HTTPException(status_code=400, detail="stale Stripe webhook signature")

    signed_payload = str(timestamp).encode("ascii") + b"." + body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(secrets.compare_digest(expected, candidate) for candidate in signatures):
        raise HTTPException(status_code=400, detail="invalid Stripe webhook signature")


def _html_file(name: str) -> str:
    try:
        return Path(__file__).with_name(name).read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(status_code=503, detail="voice store asset unavailable") from err


@router.get("/api/anthbot/store/voice-packs")
def store_voice_packs(request: Request) -> dict[str, Any]:
    return _store_catalog(request)


@router.post("/api/anthbot/store/client/pair")
def create_store_client_pair(
    payload: StoreClientPayload,
    request: Request,
) -> dict[str, Any]:
    if not _store_enabled():
        raise HTTPException(status_code=503, detail="voice store is disabled")
    pair_code, expires_at = _create_store_pairing(payload.client_token)
    base = core._public_base_url(request)
    return {
        "paired": True,
        "store_url": f"{base}/store?pair={quote(pair_code)}",
        "expires_at": _iso_from_epoch(expires_at),
    }


@router.post("/api/anthbot/store/client/entitlements")
def store_client_entitlements(
    payload: StoreClientPayload,
    request: Request,
) -> dict[str, Any]:
    client_id = _client_id_from_token(payload.client_token)
    _init_store_tables()
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE client_id = ? AND payment_status = 'paid'
            ORDER BY COALESCE(paid_at, updated_at) DESC
            """,
            (client_id,),
        ).fetchall()

    packs: list[dict[str, Any]] = []
    seen_pack_ids: set[str] = set()
    base = core._public_base_url(request)
    for row in rows:
        order = dict(row)
        pack_id = str(order.get("pack_id", "")).strip()
        if not pack_id or pack_id in seen_pack_ids:
            continue
        try:
            pack = _find_uploaded_pack(pack_id)
        except HTTPException:
            continue
        if not _is_paid(pack):
            continue

        license_key = _license_for_order(order)
        public = _public_paid_pack(pack, request)
        public["music_url"] = (
            f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}/download"
            f"?license={quote(license_key)}"
        )
        public["entitlement"] = "purchased"
        packs.append(public)
        seen_pack_ids.add(pack_id)

    return {
        "licensed": bool(packs),
        "license_version": 1,
        "packs": packs,
    }


@router.post("/api/anthbot/store/checkout")
async def create_store_checkout(
    payload: CheckoutPayload,
    request: Request,
) -> dict[str, Any]:
    _require_checkout_ready()
    record = _find_uploaded_pack(payload.pack_id)
    if not _is_paid(record):
        raise HTTPException(status_code=409, detail="voice pack is not a paid product")

    client_id = _client_id_from_pairing(payload.pair_code)
    session = await asyncio.to_thread(
        _create_checkout_session,
        record,
        request,
        client_id=client_id,
        pair_code=payload.pair_code,
    )
    order = _upsert_order_from_session(session)
    checkout_url = session.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {
        "checkout_url": checkout_url,
        "session_id": order["stripe_session_id"],
    }


@router.post("/api/anthbot/store/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    body = await request.body()
    _verify_stripe_signature(body, stripe_signature)
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="invalid Stripe webhook body") from err

    event_type = str(event.get("type", ""))
    data = event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    if (
        event_type
        in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }
        and isinstance(session, dict)
        and session.get("object") == "checkout.session"
    ):
        _upsert_order_from_session(session)
    elif event_type == "charge.refunded" and isinstance(session, dict):
        payment_intent = session.get("payment_intent")
        if payment_intent:
            _init_store_tables()
            with core._db() as conn:
                conn.execute(
                    """
                    UPDATE store_orders
                    SET status = 'refunded',
                        payment_status = 'refunded',
                        updated_at = ?
                    WHERE stripe_payment_intent_id = ?
                    """,
                    (core._iso(), str(payment_intent)),
                )

    return {"received": True}


@router.get("/api/anthbot/store/orders/{session_id}")
async def store_order(session_id: str, request: Request) -> dict[str, Any]:
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid checkout session id")

    order = _order_by_session(session_id)
    payment_state = (
        str(order.get("payment_status", "")).casefold()
        if order is not None
        else ""
    )
    if order is None or payment_state not in {"paid", "refunded"}:
        _require_checkout_ready()
        session = await asyncio.to_thread(_retrieve_checkout_session, session_id)
        order = _upsert_order_from_session(session)

    return _order_public(order, request)


@router.post("/api/anthbot/store/entitlements")
def store_entitlements(
    payload: EntitlementPayload,
    request: Request,
) -> dict[str, Any]:
    order = _order_from_license(payload.license_key)
    pack = _find_uploaded_pack(str(order.get("pack_id", "")))
    if not _is_paid(pack):
        raise HTTPException(status_code=409, detail="licensed voice pack is no longer paid")

    pack_id = str(pack.get("id", ""))
    base = core._public_base_url(request)
    public = _public_paid_pack(pack, request)
    public["music_url"] = (
        f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}/download"
        f"?license={quote(payload.license_key)}"
    )
    return {
        "licensed": True,
        "license_version": 1,
        "packs": [public],
    }


@router.get("/api/anthbot/store/voice-packs/{pack_id}/download")
def download_paid_voice_pack(
    pack_id: str,
    license: str,
) -> FileResponse:
    order = _order_from_license(license)
    if str(order.get("pack_id", "")) != pack_id:
        raise HTTPException(status_code=403, detail="license does not cover this voice pack")

    pack = _find_uploaded_pack(pack_id)
    if not _is_paid(pack):
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    filename = str(pack.get("filename", ""))
    if Path(filename).name != filename or not core._VOICE_PACK_SAFE_PART.fullmatch(filename):
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get(
    "/api/anthbot/admin/store/voice-packs",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_voice_packs(request: Request) -> dict[str, Any]:
    _init_store_tables()
    with core._db() as conn:
        sales_rows = conn.execute(
            """
            SELECT pack_id,
                   COUNT(*) AS sales,
                   COALESCE(SUM(amount_total), 0) AS revenue
            FROM store_orders
            WHERE payment_status = 'paid'
            GROUP BY pack_id
            """
        ).fetchall()
    sales = {
        row["pack_id"]: {"sales": row["sales"], "revenue": row["revenue"]}
        for row in sales_rows
    }

    items: list[dict[str, Any]] = []
    for record in _uploaded_records():
        public = core._public_voice_pack(record, request)
        public["access"] = "paid" if _is_paid(record) else "free"
        public["price_amount"] = _price_amount(record)
        public["currency"] = _currency(record)
        public["sales"] = int(sales.get(str(record.get("id")), {}).get("sales", 0))
        public["revenue"] = int(sales.get(str(record.get("id")), {}).get("revenue", 0))
        items.append(public)
    items.sort(key=lambda item: str(item.get("id", "")).casefold())
    return {
        "schema": STORE_SCHEMA,
        "store_enabled": _store_enabled(),
        "checkout_ready": _checkout_ready(),
        "items": items,
    }


@router.patch(
    "/api/anthbot/admin/store/voice-packs/{pack_id}",
    dependencies=[Depends(core.require_admin)],
)
def update_store_voice_pack(
    pack_id: str,
    payload: StorePricingPayload,
    request: Request,
) -> dict[str, Any]:
    registry = core._uploaded_voice_pack_registry()
    packs = [item for item in registry.get("packs", []) if isinstance(item, dict)]
    target = next((item for item in packs if str(item.get("id", "")) == pack_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="uploaded voice pack not found")

    target["access"] = payload.access
    target["price_amount"] = payload.price_amount if payload.access == "paid" else 0
    target["currency"] = payload.currency
    core._write_uploaded_voice_registry(
        {"schema": core.VOICE_PACKS_SCHEMA, "packs": packs}
    )

    public = core._public_voice_pack(target, request)
    public["access"] = payload.access
    public["price_amount"] = target["price_amount"]
    public["currency"] = target["currency"]
    return {"updated": True, "pack": public}


@router.get(
    "/api/anthbot/admin/store/orders",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_orders(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT stripe_session_id, pack_id, status, payment_status,
                   amount_total, currency, customer_email, client_id,
                   created_at, updated_at, paid_at
            FROM store_orders
            ORDER BY COALESCE(paid_at, updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get(
    "/api/anthbot/admin/store/stats",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_stats() -> dict[str, Any]:
    _init_store_tables()
    with core._db() as conn:
        total_orders = conn.execute(
            "SELECT COUNT(*) FROM store_orders"
        ).fetchone()[0]
        paid_orders = conn.execute(
            "SELECT COUNT(*) FROM store_orders WHERE payment_status = 'paid'"
        ).fetchone()[0]
        revenue_rows = conn.execute(
            """
            SELECT COALESCE(currency, 'unknown') AS currency,
                   COALESCE(SUM(amount_total), 0) AS amount
            FROM store_orders
            WHERE payment_status = 'paid'
            GROUP BY COALESCE(currency, 'unknown')
            ORDER BY currency
            """
        ).fetchall()
    return {
        "orders": total_orders,
        "paid_orders": paid_orders,
        "revenue": [dict(row) for row in revenue_rows],
        "store_enabled": _store_enabled(),
        "checkout_ready": _checkout_ready(),
        "automatic_tax": _stripe_automatic_tax(),
    }


@router.get("/store", response_class=HTMLResponse)
def store_page() -> HTMLResponse:
    return HTMLResponse(_html_file("store.html"))


@router.get("/store/success", response_class=HTMLResponse)
def store_success_page() -> HTMLResponse:
    return HTMLResponse(_html_file("store_success.html"))


@router.get("/dashboard/store", response_class=HTMLResponse)
def store_admin_page(request: Request):
    if not core._request_is_admin(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(_html_file("store_admin.html"))
