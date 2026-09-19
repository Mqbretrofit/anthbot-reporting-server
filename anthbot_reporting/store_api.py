from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
from html import escape
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
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
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
_STANDARD_VOICE_PACK_PRICE_AMOUNT = 799
_STANDARD_VOICE_PACK_CURRENCY = "eur"
_CUSTOM_VOICE_STARTING_PRICE_AMOUNT = 2499
_ANALYTICS_UNIQUE_RETENTION_DAYS = 35
_ANALYTICS_AGGREGATE_RETENTION_DAYS = 400
_ANALYTICS_ALLOWED_PATHS = {
    "/",
    "/store",
    "/store/success",
    "/privacy",
    "/terms",
    "/refunds",
    "/home-assistant",
    "/models/genie-1000",
    "/models/m9-pro",
    "/models/mgc1000",
    "/voice-packs",
}
_ANALYTICS_LANGUAGES = {
    "hu", "en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
    "ro", "da", "sv", "no", "fi", "zh-CN", "zh-TW", "tr", "th", "vi",
    "ko", "km",
}
_ANALYTICS_PUBLIC_HTML = {
    "public_site.html",
    "public_terms.html",
    "public_refunds.html",
    "public_privacy.html",
    "store.html",
    "store_success.html",
}
_PUBLIC_SITE_BASE_URL = "https://anthbotmap.com"
_LEGACY_PUBLIC_HOSTS = {"reports.mqbretrofithungary.online"}
_SEO_PAGES: dict[str, dict[str, Any]] = {
    "public_site.html": {
        "path": "/",
        "title": "ANTHBOT Map for Home Assistant – Maps, Zones & Voice Packs",
        "description": (
            "ANTHBOT Map is an independent Home Assistant integration for ANTHBOT "
            "robotic lawn mowers, with live maps, zones, schedules, mowing history, "
            "diagnostics and voice packs."
        ),
        "index": True,
    },
    "store.html": {
        "path": "/store",
        "title": "ANTHBOT Voice Packs & Custom Voices | ANTHBOT Map",
        "description": (
            "Browse ANTHBOT community voice packs and request custom mower voices "
            "for supported models, with automatic ANTHBOT Map integration."
        ),
        "index": True,
    },
    "public_terms.html": {
        "path": "/terms",
        "title": "Terms of Service | ANTHBOT Map",
        "description": "Terms of Service for ANTHBOT Map digital services and voice packs.",
        "index": True,
    },
    "public_refunds.html": {
        "path": "/refunds",
        "title": "Refund Policy | ANTHBOT Map",
        "description": "Refund Policy for ANTHBOT Map digital products and services.",
        "index": True,
    },
    "public_privacy.html": {
        "path": "/privacy",
        "title": "Privacy Policy | ANTHBOT Map",
        "description": "Privacy and data protection information for ANTHBOT Map services.",
        "index": True,
    },
    "store_success.html": {
        "path": "/store/success",
        "title": "ANTHBOT Voice Purchase | ANTHBOT Map",
        "description": "ANTHBOT voice pack purchase confirmation.",
        "index": False,
    },
}

_SEO_LANDING_PAGES: dict[str, dict[str, Any]] = {
    "/home-assistant": {
        "title": "ANTHBOT Home Assistant Integration | ANTHBOT Map",
        "description": (
            "Connect supported ANTHBOT robotic lawn mowers to Home Assistant with "
            "ANTHBOT Map: live map, zones, native schedules, mower controls, history "
            "and diagnostics."
        ),
        "eyebrow": "Home Assistant integration",
        "heading": "ANTHBOT in Home Assistant with ANTHBOT Map",
        "lead": (
            "ANTHBOT Map is an independent, open-source Home Assistant integration "
            "and Lovelace map card for supported ANTHBOT robotic lawn mowers."
        ),
        "sections": [
            (
                "What the integration adds",
                "ANTHBOT Map connects Home Assistant to the ANTHBOT cloud, creates a "
                "native lawn_mower entity, mirrors supported ANTHBOT app schedules, "
                "and provides model-aware controls instead of forcing every mower "
                "through one generic command path.",
            ),
            (
                "Live map and lawn data",
                "Where supported by the mower family, the card can render the lawn "
                "boundary, mowing zones, No-Go areas, mower position, live path and "
                "mowing coverage. A dedicated WebSocket live-map transport keeps "
                "high-frequency geometry out of Home Assistant Recorder.",
            ),
            (
                "Schedules and automations",
                "Native app schedules can be mirrored into Home Assistant and, on "
                "supported models, edited with write-back. Per-mower next-mow data, "
                "native mower events and timed mow/park overrides can be used in "
                "Home Assistant automations.",
            ),
            (
                "Model-aware design",
                "Genie, M-series, N8 and Pion/MGC devices use separated model routing. "
                "Capabilities remain conservative when a command or protocol detail "
                "has not been confirmed.",
            ),
        ],
        "cta": ("View ANTHBOT Map on GitHub", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/genie-1000": {
        "title": "ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map",
        "description": (
            "ANTHBOT Genie 1000 support in Home Assistant with ANTHBOT Map, including "
            "live map/path data, zones, schedules, history, mower controls and diagnostics."
        ),
        "eyebrow": "Supported mower",
        "heading": "ANTHBOT Genie 1000 + Home Assistant",
        "lead": (
            "The Genie family is supported by ANTHBOT Map and has been directly "
            "hardware-tested by the project, including Genie 1000 schedule loading."
        ),
        "sections": [
            (
                "Direct hardware validation",
                "The public ANTHBOT Map project documents direct real-device testing "
                "for the Genie family. Genie 1000 native app schedule loading has also "
                "been verified on real hardware.",
            ),
            (
                "Maps, zones and mowing history",
                "ANTHBOT Map keeps Genie-specific map/path diagnostics isolated from "
                "other mower families and exposes supported lawn boundary, zones, "
                "No-Go geometry, live mower position, path and historical mowing data.",
            ),
            (
                "Native scheduling",
                "The integration mirrors the mower's native ANTHBOT app schedule into "
                "Home Assistant and supports the model-specific schedule path rather "
                "than translating it through M-series behavior.",
            ),
            (
                "Home Assistant controls",
                "Supported operations include mower status and common mowing controls, "
                "with model-specific routing plus Battery Saver and diagnostic tooling "
                "where the underlying device capabilities are available.",
            ),
        ],
        "cta": ("Install / documentation", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/m9-pro": {
        "title": "ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map",
        "description": (
            "ANTHBOT M9 Pro support for Home Assistant with control, status, live map, "
            "path, zones, mowing history, schedules and diagnostics."
        ),
        "eyebrow": "Directly hardware-tested",
        "heading": "ANTHBOT M9 Pro + Home Assistant",
        "lead": (
            "ANTHBOT Map includes a dedicated M-series implementation, and M9 Pro "
            "control, status, map, path, zone and history handling have been directly "
            "hardware-tested by the project."
        ),
        "sections": [
            (
                "Live map architecture",
                "Real-device M9 Pro validation confirmed live WebSocket path updates, "
                "Home Assistant restart and reconnect handling, snapshot restore and "
                "reduced Recorder churn.",
            ),
            (
                "Zones and mowing data",
                "The M-series path supports map, path, zone and history handling while "
                "keeping model-specific decoding separate from Genie and N8.",
            ),
            (
                "Native schedule write-back",
                "Creating an M9 Pro schedule from the ANTHBOT Map card has been "
                "verified on real hardware, with the created rule appearing in the "
                "ANTHBOT app.",
            ),
            (
                "Home Assistant automation",
                "Mower state, next-mow information, lifecycle/schedule events and "
                "supported controls can be used in dashboards and automations.",
            ),
        ],
        "cta": ("View M9 Pro integration documentation", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/mgc1000": {
        "title": "ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map",
        "description": (
            "ANTHBOT MGC1000 and Pion-family support in ANTHBOT Map for Home Assistant: "
            "isolated model detection, status normalization, native schedules and start routing."
        ),
        "eyebrow": "Pion / MGC family",
        "heading": "ANTHBOT MGC1000 + Home Assistant",
        "lead": (
            "ANTHBOT Map has a dedicated Pion/MGC model family for identifiers such "
            "as MGC500, MGC750 and MGC1000, instead of treating these mowers as Genie."
        ),
        "sections": [
            (
                "Dedicated model handling",
                "The integration includes isolated Pion/MGC detection and a flat-shadow "
                "normalization layer for Home Assistant status data.",
            ),
            (
                "Confirmed status data",
                "The current implementation exposes confirmed cutting height, mowing "
                "progress and area, rain state, Wi-Fi/IP, path payload and firmware data "
                "when supplied by the mower/cloud.",
            ),
            (
                "Native schedules and start routing",
                "Pion/MGC uses its own native schedule shape and start path. The "
                "integration preserves its one-appointment-per-day/full-lawn schedule "
                "behavior instead of applying Genie-only payloads.",
            ),
            (
                "Conservative capability policy",
                "Unverified Pion/MGC setting writes and curpath decoding remain "
                "intentionally disabled until protocol and hardware behavior are confirmed.",
            ),
        ],
        "cta": ("Follow Pion / MGC development", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/voice-packs": {
        "title": "ANTHBOT Voice Packs for Genie Mowers | ANTHBOT Map",
        "description": (
            "ANTHBOT community voice packs and custom mower voices for compatible "
            "ANTHBOT Genie robots, integrated with the ANTHBOT Map ecosystem."
        ),
        "eyebrow": "Community voice packs",
        "heading": "ANTHBOT voice packs and custom mower voices",
        "lead": (
            "The ANTHBOT Map ecosystem includes optional Community voice packs for "
            "compatible ANTHBOT Genie robots, with ready-made packs and custom voice requests."
        ),
        "sections": [
            (
                "Ready-made Community packs",
                "Available voice packs are listed in the ANTHBOT Community Voice Store. "
                "Compatibility is shown with the pack and can vary by mower model or firmware.",
            ),
            (
                "Custom voice requests",
                "A separate custom-voice workflow is available for requests that are "
                "not covered by the ready-made catalogue.",
            ),
            (
                "ANTHBOT Map integration",
                "Purchased voice entitlements can be linked to ANTHBOT Map so compatible "
                "installed systems can recognize the purchased pack without exposing paid "
                "download URLs publicly.",
            ),
            (
                "Independent project",
                "Community voice packs and ANTHBOT Map are independent project features. "
                "ANTHBOT is a trademark of its respective owner; this site does not imply "
                "official ANTHBOT endorsement.",
            ),
        ],
        "cta": ("Open the Voice Pack Store", "/store"),
    },
}

# Stripe Managed Payments requires an eligible product tax code. Community
# voice packs are one-time downloadable digital audio with permanent access.
_VOICE_PACK_TAX_CODE = "txcd_10401100"

_LOGGER = logging.getLogger(__name__)


class CheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(min_length=1, max_length=160)
    pair_code: str | None = Field(default=None, min_length=20, max_length=160)


class StoreClientCheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=32, max_length=512)
    pack_id: str = Field(min_length=1, max_length=160)

    @field_validator("client_token")
    @classmethod
    def _validate_client_token(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLIENT_TOKEN_RE.fullmatch(normalized):
            raise ValueError("invalid store client token")
        return normalized

    @field_validator("pack_id")
    @classmethod
    def _validate_pack_id(cls, value: str) -> str:
        return value.strip()


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


class CustomVoiceRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_language: str = Field(min_length=1, max_length=64)
    voice_style: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    contact: str = Field(min_length=3, max_length=254)
    notes: str = Field(default="", max_length=2000)
    site_language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator(
        "requested_language",
        "voice_style",
        "model",
        "contact",
        "notes",
        "site_language",
    )
    @classmethod
    def _strip_custom_request_text(cls, value: str) -> str:
        return value.strip()


class SiteVisitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=64)
    language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _ANALYTICS_ALLOWED_PATHS:
            raise ValueError("unsupported analytics path")
        return normalized

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        normalized = value.strip()
        return normalized if normalized in _ANALYTICS_LANGUAGES else "en"


class PrivacyRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_type: Literal[
        "access",
        "rectification",
        "erasure",
        "restriction",
        "portability",
        "objection",
        "withdraw_consent",
        "complaint",
        "other",
    ]
    contact: str = Field(min_length=3, max_length=254)
    details: str = Field(default="", max_length=3000)
    site_language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("contact", "details", "site_language")
    @classmethod
    def _strip_privacy_request_text(cls, value: str) -> str:
        return value.strip()


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


def _site_analytics_enabled() -> bool:
    return _env_flag("ANTHBOT_SITE_ANALYTICS_ENABLED", True)


def _privacy_controller_name() -> str:
    return os.environ.get(
        "ANTHBOT_PRIVACY_CONTROLLER_NAME",
        "MQB Retrofit Hungary",
    ).strip() or "MQB Retrofit Hungary"


def _privacy_controller_address() -> str:
    return os.environ.get("ANTHBOT_PRIVACY_CONTROLLER_ADDRESS", "").strip()


def _privacy_contact_email() -> str:
    return os.environ.get("ANTHBOT_PRIVACY_CONTACT_EMAIL", "").strip()


def _privacy_contact_phone() -> str:
    return os.environ.get(
        "ANTHBOT_PRIVACY_CONTACT_PHONE",
        "+36 30 620 9015",
    ).strip() or "+36 30 620 9015"


def _checkout_ready() -> bool:
    stripe_key = _stripe_secret_key()
    base_ready = bool(
        _store_enabled()
        and stripe_key
        and _stripe_webhook_secret()
        and _license_secret()
    )
    if not base_ready:
        return False
    # Sandbox remains usable while legal details are being prepared. Live
    # commercial checkout requires the controller's postal address so the
    # public Article 13 notice cannot accidentally go live incomplete.
    if stripe_key.startswith("sk_live_") and not _privacy_controller_address():
        return False
    return True


def _require_checkout_ready() -> None:
    if not _store_enabled():
        raise HTTPException(status_code=503, detail="voice store checkout is disabled")
    if not _stripe_secret_key():
        raise HTTPException(status_code=503, detail="Stripe secret key is not configured")
    if not _stripe_webhook_secret():
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not _license_secret():
        raise HTTPException(status_code=503, detail="voice store license secret is not configured")
    if _stripe_secret_key().startswith("sk_live_") and not _privacy_controller_address():
        raise HTTPException(
            status_code=503,
            detail="privacy controller postal address must be configured before live checkout",
        )


def _init_store_tables() -> None:
    with core._db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_orders (
                stripe_session_id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                community_id TEXT,
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

            CREATE TABLE IF NOT EXISTS store_custom_voice_requests (
                request_id TEXT PRIMARY KEY,
                requested_language TEXT NOT NULL,
                voice_style TEXT NOT NULL,
                model TEXT NOT NULL,
                contact TEXT NOT NULL,
                notes TEXT NOT NULL,
                site_language TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_store_custom_voice_requests_created
                ON store_custom_voice_requests(created_at);

            CREATE TABLE IF NOT EXISTS privacy_requests (
                request_id TEXT PRIMARY KEY,
                request_type TEXT NOT NULL,
                contact TEXT NOT NULL,
                details TEXT NOT NULL,
                site_language TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_privacy_requests_created
                ON privacy_requests(created_at);
            CREATE INDEX IF NOT EXISTS idx_privacy_requests_status
                ON privacy_requests(status);

            CREATE TABLE IF NOT EXISTS site_analytics_views (
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                language TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, path, language)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_views_day
                ON site_analytics_views(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_visitors (
                day TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_day
                ON site_analytics_unique_visitors(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_page_visitors (
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, path, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_page_day
                ON site_analytics_unique_page_visitors(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_language_visitors (
                day TEXT NOT NULL,
                language TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, language, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_language_day
                ON site_analytics_unique_language_visitors(day);
            """
        )

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(store_orders)").fetchall()
        }
        if "client_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN client_id TEXT")
        if "community_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN community_id TEXT")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_store_orders_client_id
            ON store_orders(client_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_community_id
            ON store_orders(community_id);
            """
        )

        # Backfill older orders while their purchased pack is still present.
        records = {
            str(item.get("id", "")): str(item.get("community_id", "")).strip()
            for item in _uploaded_voice_pack_registry_records()
            if str(item.get("community_id", "")).strip()
        }
        for old_pack_id, community_id in records.items():
            conn.execute(
                """
                UPDATE store_orders
                SET community_id = ?
                WHERE pack_id = ?
                  AND (community_id IS NULL OR community_id = '')
                """,
                (community_id, old_pack_id),
            )


def _analytics_secret_path() -> Path:
    return core._db_path().with_name(".site_analytics_secret")


def _analytics_master_secret() -> bytes:
    """Load or create a private secret kept outside the analytics database."""
    path = _analytics_secret_path()
    try:
        raw = path.read_text(encoding="ascii").strip()
        secret = bytes.fromhex(raw)
        if len(secret) >= 32:
            return secret
    except (OSError, ValueError):
        pass

    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(secret.hex(), encoding="ascii")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _normalized_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


def _analytics_source_ip(request: Request) -> str | None:
    """Resolve the source address for counting only; never persist the raw IP."""
    for header in ("cf-connecting-ip", "x-real-ip"):
        normalized = _normalized_ip(request.headers.get(header))
        if normalized:
            return normalized

    forwarded = request.headers.get("x-forwarded-for", "")
    for item in forwarded.split(","):
        normalized = _normalized_ip(item)
        if normalized:
            return normalized

    if request.client is not None:
        return _normalized_ip(request.client.host)
    return None


def _analytics_visitor_hash(request: Request, day: str) -> str:
    source_ip = _analytics_source_ip(request) or "source-unavailable"
    master = _analytics_master_secret()
    day_key = hmac.new(
        master,
        f"anthbot-site-analytics:v1:{day}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        day_key,
        source_ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _analytics_cleanup(conn: Any, today: datetime) -> None:
    unique_cutoff = (
        today.date() - timedelta(days=_ANALYTICS_UNIQUE_RETENTION_DAYS - 1)
    ).isoformat()
    aggregate_cutoff = (
        today.date() - timedelta(days=_ANALYTICS_AGGREGATE_RETENTION_DAYS - 1)
    ).isoformat()
    conn.execute(
        "DELETE FROM site_analytics_unique_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_unique_page_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_unique_language_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_views WHERE day < ?",
        (aggregate_cutoff,),
    )


def _record_site_visit(
    request: Request,
    *,
    path: str,
    language: str,
) -> None:
    if not _site_analytics_enabled():
        return

    _init_store_tables()
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    visitor_hash = _analytics_visitor_hash(request, day)
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO site_analytics_views(day, path, language, views)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(day, path, language)
            DO UPDATE SET views = views + 1
            """,
            (day, path, language),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_visitors(day, visitor_hash)
            VALUES (?, ?)
            """,
            (day, visitor_hash),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_page_visitors(
                day, path, visitor_hash
            ) VALUES (?, ?, ?)
            """,
            (day, path, visitor_hash),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_language_visitors(
                day, language, visitor_hash
            ) VALUES (?, ?, ?)
            """,
            (day, language, visitor_hash),
        )
        _analytics_cleanup(conn, now)


def _uploaded_voice_pack_registry_records() -> list[dict[str, Any]]:
    return [
        item
        for item in core._uploaded_voice_pack_registry().get("packs", [])
        if isinstance(item, dict)
    ]


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


def _paid_order_for_client_pack(
    client_id: str,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an active purchase for the stable Community voice identity."""
    _init_store_tables()
    pack_id = str(record.get("id", "")).strip()
    community_id = str(record.get("community_id", "")).strip()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE client_id = ?
              AND payment_status = 'paid'
              AND (
                    (? != '' AND community_id = ?)
                    OR pack_id = ?
                  )
            ORDER BY COALESCE(paid_at, updated_at) DESC
            LIMIT 1
            """,
            (client_id, community_id, community_id, pack_id),
        ).fetchone()
    return dict(row) if row is not None else None


def _uploaded_records() -> list[dict[str, Any]]:
    return _uploaded_voice_pack_registry_records()


def _is_paid(record: dict[str, Any]) -> bool:
    return str(record.get("access", "free")).strip().casefold() == "paid"


def _price_amount(record: dict[str, Any]) -> int:
    if _is_paid(record):
        return _STANDARD_VOICE_PACK_PRICE_AMOUNT
    return 0


def _currency(record: dict[str, Any]) -> str:
    if _is_paid(record):
        return _STANDARD_VOICE_PACK_CURRENCY
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


def _find_uploaded_pack_by_community_id(
    community_id: str,
) -> dict[str, Any]:
    normalized = community_id.strip()
    match = next(
        (
            item
            for item in _uploaded_records()
            if str(item.get("community_id", "")).strip() == normalized
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="voice pack not found")
    return match


def _resolve_order_pack(order: dict[str, Any]) -> dict[str, Any]:
    """Resolve an old purchase to the current version of the same voice."""
    community_id = str(order.get("community_id", "")).strip()
    if community_id:
        try:
            return _find_uploaded_pack_by_community_id(community_id)
        except HTTPException:
            pass
    return _find_uploaded_pack(str(order.get("pack_id", "")).strip())


def _order_covers_pack(
    order: dict[str, Any],
    pack: dict[str, Any],
) -> bool:
    order_community_id = str(order.get("community_id", "")).strip()
    pack_community_id = str(pack.get("community_id", "")).strip()
    if order_community_id and pack_community_id:
        return secrets.compare_digest(order_community_id, pack_community_id)
    return secrets.compare_digest(
        str(order.get("pack_id", "")).strip(),
        str(pack.get("id", "")).strip(),
    )


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
        "web_installer_available": _env_flag("ANTHBOT_WEB_VOICE_INSTALLER_ENABLED", False),
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
    success_url_override: str | None = None,
    cancel_url_override: str | None = None,
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

    cancel_url = cancel_url_override or f"{base}/store?cancelled=1"
    if pair_code and cancel_url_override is None:
        cancel_url = f"{cancel_url}&pair={quote(pair_code)}"
    success_url = (
        success_url_override
        or f"{base}/store/success?session_id={{CHECKOUT_SESSION_ID}}"
    )

    metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        metadata["store_client_id"] = client_id

    payment_metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        payment_metadata["store_client_id"] = client_id

    params: dict[str, Any] = {
        "mode": "payment",
        "success_url": success_url,
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


def _session_community_id(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("community_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:160] if normalized else None


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

    community_id = _session_community_id(session)
    if not community_id:
        try:
            community_id = str(
                _find_uploaded_pack(pack_id).get("community_id", "")
            ).strip() or None
        except HTTPException:
            community_id = None

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
                stripe_session_id, pack_id, community_id, status, payment_status,
                amount_total, currency, customer_email, stripe_customer_id,
                stripe_payment_intent_id, client_id, created_at, updated_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stripe_session_id) DO UPDATE SET
                pack_id=excluded.pack_id,
                community_id=COALESCE(excluded.community_id, store_orders.community_id),
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
                community_id,
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
    pack = _resolve_order_pack(order)
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


def _canonical_public_redirect(
    request: Request,
    path: str,
) -> RedirectResponse | None:
    forwarded_host = (
        request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    )
    host = forwarded_host or request.headers.get("host", "")
    host = host.split(":", 1)[0].strip().casefold()
    if host not in _LEGACY_PUBLIC_HOSTS:
        return None

    target = f"{_PUBLIC_SITE_BASE_URL}{path}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"
    return RedirectResponse(url=target, status_code=301)


def _apply_seo_metadata(name: str, html: str) -> str:
    page = _SEO_PAGES.get(name)
    if not page or "</head>" not in html:
        return html

    title = str(page["title"])
    description = str(page["description"])
    canonical = f"{_PUBLIC_SITE_BASE_URL}{page['path']}"
    indexed = bool(page["index"])

    # Keep the rendered English title/description aligned with the server-side
    # metadata after the page language helper runs in the browser.
    if name == "public_site.html":
        html = html.replace(
            "MQB Retrofit Hungary | ANTHBOT Map & Digital Tools",
            title,
        )
        html = html.replace(
            "MQB Retrofit Hungary develops the independent open-source ANTHBOT Map "
            "Home Assistant integration, map card, diagnostics, scheduling tools and "
            "optional digital products for supported ANTHBOT robotic lawn mowers.",
            description,
        )
    elif name == "store.html":
        html = html.replace("ANTHBOT Community Voice Store", title)

    title_tag = f"<title>{escape(title)}</title>"
    if re.search(r"<title>.*?</title>", html, flags=re.IGNORECASE | re.DOTALL):
        html = re.sub(
            r"<title>.*?</title>",
            lambda _: title_tag,
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    else:
        html = html.replace("</head>", f"{title_tag}\n</head>", 1)

    description_tag = (
        f'<meta name="description" content="{escape(description, quote=True)}">'
    )
    description_pattern = re.compile(
        r'<meta\s+name="description"\s+content="[^"]*"\s*/?>',
        flags=re.IGNORECASE,
    )
    if description_pattern.search(html):
        html = description_pattern.sub(description_tag, html, count=1)
        extra_description = ""
    else:
        extra_description = description_tag + "\n"

    robots = (
        "index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"
        if indexed
        else "noindex,nofollow"
    )
    social = [
        extra_description.rstrip("\n"),
        f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
        f'<meta name="robots" content="{robots}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="ANTHBOT Map">',
        f'<meta property="og:title" content="{escape(title, quote=True)}">',
        f'<meta property="og:description" content="{escape(description, quote=True)}">',
        f'<meta property="og:url" content="{escape(canonical, quote=True)}">',
        '<meta name="twitter:card" content="summary">',
        f'<meta name="twitter:title" content="{escape(title, quote=True)}">',
        f'<meta name="twitter:description" content="{escape(description, quote=True)}">',
    ]
    social = [item for item in social if item]

    if name == "public_site.html":
        structured = json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "ANTHBOT Map",
                "applicationCategory": "HomeAutomationApplication",
                "operatingSystem": "Home Assistant",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
                "downloadUrl": "https://github.com/Mqbretrofit/ha-anthbot-map-v2",
                "sameAs": ["https://github.com/Mqbretrofit/ha-anthbot-map-v2"],
                "license": "https://opensource.org/licenses/MIT",
                "isAccessibleForFree": True,
                "description": description,
                "publisher": {
                    "@type": "Organization",
                    "name": "MQB Retrofit Hungary",
                    "url": f"{_PUBLIC_SITE_BASE_URL}/",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        social.append(
            f'<script type="application/ld+json">{structured}</script>'
        )

    html = html.replace("</head>", "\n".join(social) + "\n</head>", 1)
    if name == "public_site.html" and "</main>" in html:
        explore = """
<section class="section"><div class="wrap">
  <h2>Explore ANTHBOT Map topics</h2>
  <p class="lead">Detailed pages for Home Assistant integration, tested mower families and Community voice packs.</p>
  <div class="policy-links">
    <a class="btn" href="/home-assistant">ANTHBOT Home Assistant</a>
    <a class="btn" href="/models/genie-1000">Genie 1000</a>
    <a class="btn" href="/models/m9-pro">M9 Pro</a>
    <a class="btn" href="/models/mgc1000">MGC1000 / Pion</a>
    <a class="btn" href="/voice-packs">ANTHBOT Voice Packs</a>
  </div>
</div></section>
"""
        html = html.replace("</main>", explore + "</main>", 1)
    return html


def _seo_landing_html(path: str) -> str:
    page = _SEO_LANDING_PAGES.get(path)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")

    title = str(page["title"])
    description = str(page["description"])
    heading = str(page["heading"])
    lead = str(page["lead"])
    canonical = f"{_PUBLIC_SITE_BASE_URL}{path}"
    sections = "".join(
        (
            '<section class="card"><h2>'
            + escape(str(section_title))
            + '</h2><p>'
            + escape(str(section_text))
            + '</p></section>'
        )
        for section_title, section_text in page["sections"]
    )
    cta_label, cta_href = page["cta"]
    internal_links = (
        '<a href="/home-assistant">Home Assistant</a>'
        '<a href="/models/genie-1000">Genie 1000</a>'
        '<a href="/models/m9-pro">M9 Pro</a>'
        '<a href="/models/mgc1000">MGC1000</a>'
        '<a href="/voice-packs">Voice packs</a>'
        '<a href="/store">Voice Store</a>'
    )
    structured = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": title,
            "url": canonical,
            "description": description,
            "isPartOf": {
                "@type": "WebSite",
                "name": "ANTHBOT Map",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
            },
            "about": {
                "@type": "SoftwareApplication",
                "name": "ANTHBOT Map",
                "applicationCategory": "HomeAutomationApplication",
                "operatingSystem": "Home Assistant",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description, quote=True)}">
<link rel="canonical" href="{escape(canonical, quote=True)}">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1">
<meta property="og:type" content="website">
<meta property="og:site_name" content="ANTHBOT Map">
<meta property="og:title" content="{escape(title, quote=True)}">
<meta property="og:description" content="{escape(description, quote=True)}">
<meta property="og:url" content="{escape(canonical, quote=True)}">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{escape(title, quote=True)}">
<meta name="twitter:description" content="{escape(description, quote=True)}">
<script type="application/ld+json">{structured}</script>
<style>
:root{{color-scheme:dark;--bg:#081017;--panel:#111820;--panel2:#141d27;--line:rgba(255,255,255,.12);--text:#fff;--muted:rgba(255,255,255,.72);--green:#5ee083;--max:1060px;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 14% 0,rgba(40,94,145,.34),transparent 29rem),radial-gradient(circle at 90% 24%,rgba(94,224,131,.09),transparent 26rem),var(--bg);color:var(--text);line-height:1.65}}a{{color:#d9ffe6}}.wrap{{max-width:var(--max);margin:auto;padding:0 22px}}.nav{{position:sticky;top:0;z-index:20;background:rgba(8,16,23,.82);backdrop-filter:blur(16px);border-bottom:1px solid var(--line)}}.navin{{min-height:70px;display:flex;align-items:center;justify-content:space-between;gap:20px}}.brand{{font-weight:850;text-decoration:none}}.links{{display:flex;gap:13px;flex-wrap:wrap}}.links a{{text-decoration:none;color:var(--muted);font-size:13px}}.links a:hover{{color:#fff}}.hero{{padding:72px 0 38px}}.eyebrow{{display:inline-flex;padding:6px 11px;border:1px solid var(--line);border-radius:999px;color:var(--green);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}h1{{font-size:clamp(2.5rem,7vw,4.8rem);line-height:1.02;margin:16px 0}}.lead{{max-width:820px;font-size:1.12rem;color:var(--muted)}}.actions{{display:flex;gap:12px;flex-wrap:wrap;margin-top:25px}}.btn{{display:inline-flex;align-items:center;min-height:44px;padding:0 16px;border-radius:12px;text-decoration:none;font-weight:800;background:linear-gradient(180deg,#34c759,#248a46);color:#fff}}.btn.secondary{{background:var(--panel2);border:1px solid var(--line)}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:18px 0 72px}}.card{{padding:22px;border:1px solid var(--line);border-radius:19px;background:linear-gradient(180deg,rgba(20,29,39,.96),rgba(12,18,24,.96));box-shadow:0 18px 50px rgba(0,0,0,.24)}}.card h2{{margin:0 0 8px;font-size:1.3rem}}.card p{{margin:0;color:var(--muted)}}.notice{{margin:0 0 58px;padding:17px;border:1px solid rgba(94,224,131,.24);border-radius:15px;background:rgba(94,224,131,.06);color:#d7eee1}}.footer{{border-top:1px solid var(--line);padding:28px 0 44px;color:#94a2ad;font-size:13px}}@media(max-width:760px){{.links{{display:none}}.grid{{grid-template-columns:1fr}}.hero{{padding-top:50px}}}}
</style>
</head>
<body>
<nav class="nav"><div class="wrap navin"><a class="brand" href="/">ANTHBOT Map</a><div class="links">{internal_links}</div></div></nav>
<main>
<section class="hero"><div class="wrap"><span class="eyebrow">{escape(str(page["eyebrow"]))}</span><h1>{escape(heading)}</h1><p class="lead">{escape(lead)}</p><div class="actions"><a class="btn" href="{escape(str(cta_href), quote=True)}">{escape(str(cta_label))}</a><a class="btn secondary" href="/">ANTHBOT Map home</a></div></div></section>
<div class="wrap"><div class="grid">{sections}</div><div class="notice"><strong>Independent community project.</strong> ANTHBOT is a trademark of its respective owner. ANTHBOT Map is independent and is not an official ANTHBOT product unless explicitly stated otherwise.</div></div>
</main>
<footer class="footer"><div class="wrap">© 2026 MQB Retrofit Hungary · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/refunds">Refunds</a></div></footer>
<script src="/site-analytics.js?v=1"></script>
</body>
</html>"""


def _html_file(name: str) -> str:
    try:
        html = Path(__file__).with_name(name).read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(status_code=503, detail="voice store asset unavailable") from err
    if name in _ANALYTICS_PUBLIC_HTML and "</body>" in html:
        html = html.replace(
            "</body>",
            '<script src="/site-analytics.js?v=1"></script></body>',
            1,
        )
    return _apply_seo_metadata(name, html)


@router.get("/site-analytics.js")
def site_analytics_script() -> Response:
    script = r"""
(() => {
  if (
    navigator.globalPrivacyControl === true ||
    navigator.doNotTrack === "1" ||
    window.doNotTrack === "1"
  ) return;
  const allowed = new Set(["/","/store","/store/success","/privacy","/terms","/refunds","/home-assistant","/models/genie-1000","/models/m9-pro","/models/mgc1000","/voice-packs"]);
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (!allowed.has(path)) return;
  const language = String(document.documentElement.lang || navigator.language || "en")
    .slice(0, 16);
  const payload = JSON.stringify({path, language});
  setTimeout(() => {
    fetch("/api/anthbot/site/visit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: payload,
      credentials: "omit",
      cache: "no-store",
      keepalive: true
    }).catch(() => {});
  }, 0);
})();
"""
    return Response(
        content=script,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/api/anthbot/site/visit", status_code=204)
def record_site_visit(
    payload: SiteVisitPayload,
    request: Request,
) -> Response:
    _record_site_visit(
        request,
        path=payload.path,
        language=payload.language,
    )
    return Response(status_code=204)


@router.get(
    "/api/anthbot/admin/site-analytics",
    dependencies=[Depends(core.require_admin)],
)
def admin_site_analytics(days: int = 30) -> dict[str, Any]:
    _init_store_tables()
    days = max(1, min(int(days), _ANALYTICS_UNIQUE_RETENTION_DAYS))
    today = datetime.now(timezone.utc).date()
    start_day = (today - timedelta(days=days - 1)).isoformat()
    today_key = today.isoformat()
    seven_day = (today - timedelta(days=6)).isoformat()
    thirty_day = (today - timedelta(days=29)).isoformat()

    with core._db() as conn:
        today_views = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day = ?",
                (today_key,),
            ).fetchone()[0]
        )
        today_unique = int(
            conn.execute(
                "SELECT COUNT(*) FROM site_analytics_unique_visitors WHERE day = ?",
                (today_key,),
            ).fetchone()[0]
        )
        views_7d = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day >= ?",
                (seven_day,),
            ).fetchone()[0]
        )
        views_30d = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day >= ?",
                (thirty_day,),
            ).fetchone()[0]
        )
        daily_rows = conn.execute(
            """
            SELECT v.day,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_visitors u
                     WHERE u.day = v.day
                   ) AS unique_visitors
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.day
            ORDER BY v.day DESC
            """,
            (start_day,),
        ).fetchall()
        page_rows = conn.execute(
            """
            SELECT v.path,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_page_visitors u
                     WHERE u.path = v.path AND u.day >= ?
                   ) AS unique_visitor_days
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.path
            ORDER BY views DESC, v.path
            """,
            (start_day, start_day),
        ).fetchall()
        language_rows = conn.execute(
            """
            SELECT v.language,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_language_visitors u
                     WHERE u.language = v.language AND u.day >= ?
                   ) AS unique_visitor_days
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.language
            ORDER BY views DESC, v.language
            """,
            (start_day, start_day),
        ).fetchall()

    return {
        "enabled": _site_analytics_enabled(),
        "window_days": days,
        "today": {
            "views": today_views,
            "unique_visitors": today_unique,
        },
        "views_7d": views_7d,
        "views_30d": views_30d,
        "daily": [dict(row) for row in daily_rows],
        "pages": [dict(row) for row in page_rows],
        "languages": [dict(row) for row in language_rows],
        "privacy": {
            "raw_ip_persisted": False,
            "user_agent_persisted": False,
            "analytics_cookie": False,
            "unique_identifier_rotation": "daily_utc",
            "unique_hash_retention_days": _ANALYTICS_UNIQUE_RETENTION_DAYS,
            "aggregate_retention_days": _ANALYTICS_AGGREGATE_RETENTION_DAYS,
        },
    }


@router.get("/api/anthbot/store/voice-packs")
def store_voice_packs(request: Request) -> dict[str, Any]:
    return _store_catalog(request)


@router.post("/api/anthbot/store/client/checkout")
async def create_store_client_checkout(
    payload: StoreClientCheckoutPayload,
    request: Request,
) -> dict[str, Any]:
    """Create a checkout directly for trusted first-party clients such as the
    standalone Voice Installer, without asking the user to copy a pairing code.
    """
    _require_checkout_ready()
    record = _find_uploaded_pack(payload.pack_id)
    if not _is_paid(record):
        raise HTTPException(status_code=409, detail="voice pack is not a paid product")

    client_id = _client_id_from_token(payload.client_token)
    existing_order = _paid_order_for_client_pack(client_id, record)
    if existing_order is not None:
        return {
            "already_owned": True,
            "pack_id": payload.pack_id,
            "checkout_url": None,
            "session_id": existing_order["stripe_session_id"],
        }

    session = await asyncio.to_thread(
        _create_checkout_session,
        record,
        request,
        client_id=client_id,
        pair_code=None,
    )
    order = _upsert_order_from_session(session)
    checkout_url = session.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {
        "already_owned": False,
        "pack_id": payload.pack_id,
        "checkout_url": checkout_url,
        "session_id": order["stripe_session_id"],
    }


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
    seen_voice_ids: set[str] = set()
    base = core._public_base_url(request)
    for row in rows:
        order = dict(row)
        try:
            pack = _resolve_order_pack(order)
        except HTTPException:
            continue
        if not _is_paid(pack):
            continue

        current_pack_id = str(pack.get("id", "")).strip()
        stable_voice_id = (
            str(pack.get("community_id", "")).strip()
            or str(order.get("community_id", "")).strip()
            or current_pack_id
        )
        if not current_pack_id or stable_voice_id in seen_voice_ids:
            continue

        license_key = _license_for_order(order)
        public = _public_paid_pack(pack, request)
        public["music_url"] = (
            f"{base}/api/anthbot/store/voice-packs/{quote(current_pack_id)}/download"
            f"?license={quote(license_key)}"
        )
        public["entitlement"] = "purchased"
        packs.append(public)
        seen_voice_ids.add(stable_voice_id)

    return {
        "licensed": bool(packs),
        "license_version": 1,
        "packs": packs,
    }


@router.post("/api/anthbot/store/custom-voice-requests", status_code=201)
def create_custom_voice_request(
    payload: CustomVoiceRequestPayload,
) -> dict[str, Any]:
    _init_store_tables()
    request_id = f"cvr_{secrets.token_urlsafe(12)}"
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO store_custom_voice_requests (
                request_id, requested_language, voice_style, model, contact,
                notes, site_language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                request_id,
                payload.requested_language,
                payload.voice_style,
                payload.model,
                payload.contact,
                payload.notes,
                payload.site_language,
                now,
                now,
            ),
        )
    return {
        "submitted": True,
        "request_id": request_id,
        "starting_price_amount": _CUSTOM_VOICE_STARTING_PRICE_AMOUNT,
        "currency": _STANDARD_VOICE_PACK_CURRENCY,
    }


@router.post("/api/anthbot/privacy-requests", status_code=201)
def create_privacy_request(
    payload: PrivacyRequestPayload,
) -> dict[str, Any]:
    """Receive an electronic GDPR/data-rights request without requiring email."""
    _init_store_tables()
    request_id = f"prv_{secrets.token_urlsafe(12)}"
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO privacy_requests (
                request_id, request_type, contact, details, site_language,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                request_id,
                payload.request_type,
                payload.contact,
                payload.details,
                payload.site_language,
                now,
                now,
            ),
        )
    return {
        "submitted": True,
        "request_id": request_id,
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
    if client_id is not None:
        existing_order = _paid_order_for_client_pack(client_id, record)
        if existing_order is not None:
            return {
                "already_owned": True,
                "pack_id": payload.pack_id,
                "checkout_url": None,
                "session_id": existing_order["stripe_session_id"],
            }

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
        "already_owned": False,
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
    pack = _resolve_order_pack(order)
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
    pack = _find_uploaded_pack(pack_id)
    if not _order_covers_pack(order, pack):
        raise HTTPException(status_code=403, detail="license does not cover this voice pack")

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
            SELECT COALESCE(NULLIF(community_id, ''), pack_id) AS voice_id,
                   COUNT(*) AS sales,
                   COALESCE(SUM(amount_total), 0) AS revenue
            FROM store_orders
            WHERE payment_status = 'paid'
            GROUP BY COALESCE(NULLIF(community_id, ''), pack_id)
            """
        ).fetchall()
    sales = {
        row["voice_id"]: {"sales": row["sales"], "revenue": row["revenue"]}
        for row in sales_rows
    }

    items: list[dict[str, Any]] = []
    for record in _uploaded_records():
        public = core._public_voice_pack(record, request)
        public["access"] = "paid" if _is_paid(record) else "free"
        public["price_amount"] = _price_amount(record)
        public["currency"] = _currency(record)
        voice_id = (
            str(record.get("community_id", "")).strip()
            or str(record.get("id", ""))
        )
        public["sales"] = int(sales.get(voice_id, {}).get("sales", 0))
        public["revenue"] = int(sales.get(voice_id, {}).get("revenue", 0))
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
    target["price_amount"] = _STANDARD_VOICE_PACK_PRICE_AMOUNT if payload.access == "paid" else 0
    target["currency"] = _STANDARD_VOICE_PACK_CURRENCY if payload.access == "paid" else payload.currency
    core._write_uploaded_voice_registry(
        {"schema": core.VOICE_PACKS_SCHEMA, "packs": packs}
    )

    public = core._public_voice_pack(target, request)
    public["access"] = payload.access
    public["price_amount"] = target["price_amount"]
    public["currency"] = target["currency"]
    return {"updated": True, "pack": public}


@router.get(
    "/api/anthbot/admin/store/custom-voice-requests",
    dependencies=[Depends(core.require_admin)],
)
def admin_custom_voice_requests(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT request_id, requested_language, voice_style, model, contact,
                   notes, site_language, status, created_at, updated_at
            FROM store_custom_voice_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


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
            SELECT stripe_session_id, pack_id, community_id, status, payment_status,
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
    "/api/anthbot/admin/privacy-requests",
    dependencies=[Depends(core.require_admin)],
)
def admin_privacy_requests(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT request_id, request_type, contact, details, site_language,
                   status, created_at, updated_at
            FROM privacy_requests
            ORDER BY created_at DESC
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


@router.get("/robots.txt")
def robots_txt() -> Response:
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /dashboard\n"
            "Disallow: /api/\n"
            "Disallow: /store/success\n"
            f"Sitemap: {_PUBLIC_SITE_BASE_URL}/sitemap.xml\n"
        ),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/sitemap.xml")
def sitemap_xml() -> Response:
    urls = (
        "/",
        "/home-assistant",
        "/models/genie-1000",
        "/models/m9-pro",
        "/models/mgc1000",
        "/voice-packs",
        "/store",
        "/privacy",
        "/terms",
        "/refunds",
    )
    entries = "".join(
        f"<url><loc>{_PUBLIC_SITE_BASE_URL}{path}</loc></url>"
        for path in urls
    )
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{entries}</urlset>"
        ),
        media_type="application/xml",
    )


@router.get("/home-assistant", response_class=HTMLResponse)
def home_assistant_landing_page(request: Request) -> Response:
    path = "/home-assistant"
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/models/{model_slug}", response_class=HTMLResponse)
def model_landing_page(model_slug: str, request: Request) -> Response:
    path = f"/models/{model_slug}"
    if path not in _SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="model page not found")
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/voice-packs", response_class=HTMLResponse)
def voice_packs_landing_page(request: Request) -> Response:
    path = "/voice-packs"
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/", response_class=HTMLResponse)
def public_business_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_site.html"))


@router.get("/terms", response_class=HTMLResponse)
def public_terms_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/terms")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_terms.html"))


@router.get("/refunds", response_class=HTMLResponse)
def public_refunds_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/refunds")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_refunds.html"))


@router.get("/privacy", response_class=HTMLResponse)
def public_privacy_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/privacy")
    if redirect is not None:
        return redirect
    html = _html_file("public_privacy.html")
    address = _privacy_controller_address()
    email = _privacy_contact_email()
    replacements = {
        "__CONTROLLER_NAME__": escape(_privacy_controller_name()),
        "__CONTROLLER_ADDRESS__": escape(address or "—"),
        "__PRIVACY_EMAIL__": escape(email),
        "__PRIVACY_PHONE__": escape(_privacy_contact_phone()),
        "__PRIVACY_EMAIL_CARD_CLASS__": "info" if email else "info hidden",
        "__PRIVACY_CONFIG_WARNING_CLASS__": (
            "controller-warning" if not address else "hidden"
        ),
    }
    for needle, value in replacements.items():
        html = html.replace(needle, value)
    return HTMLResponse(html)


@router.get("/store", response_class=HTMLResponse)
def store_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/store")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("store.html"))


@router.get("/store/success", response_class=HTMLResponse)
def store_success_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/store/success")
    if redirect is not None:
        return redirect
    return HTMLResponse(
        _html_file("store_success.html"),
        headers={"X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/dashboard/store", response_class=HTMLResponse)
def store_admin_page(request: Request):
    if not core._request_is_admin(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(_html_file("store_admin.html"))
