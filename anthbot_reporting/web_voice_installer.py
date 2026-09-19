from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app as core
import store_api
import web_voice_installer_anthbot as anthbot


router = APIRouter()

_SESSION_COOKIE = "anthbot_voice_installer_session"
_STORE_COOKIE = "anthbot_voice_store_client"
_SESSION_TTL_SECONDS = 20 * 60
_STORE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
_JOB_RETENTION_SECONDS = 30 * 60
_LOGIN_WINDOW_SECONDS = 10 * 60
_LOGIN_MAX_ATTEMPTS = 8
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_JOBS: dict[str, dict[str, Any]] = {}
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_RATE_SECRET = secrets.token_bytes(32)


class LoginPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=512)
    area_code: str = Field(default="36", min_length=1, max_length=4)

    @field_validator("username", "area_code")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("area_code")
    @classmethod
    def _validate_area_code(cls, value: str) -> str:
        normalized = value.strip().lstrip("+")
        if not normalized.isdigit() or not (1 <= len(normalized) <= 4):
            raise ValueError("area_code must contain 1-4 digits")
        return normalized


class CheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack_id: str = Field(min_length=1, max_length=160)

    @field_validator("pack_id")
    @classmethod
    def _strip_pack_id(cls, value: str) -> str:
        return value.strip()


class InstallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=8, max_length=128)
    pack_id: str = Field(min_length=1, max_length=160)

    @field_validator("device_id", "pack_id")
    @classmethod
    def _strip_value(cls, value: str) -> str:
        return value.strip()


def _enabled() -> bool:
    raw = os.environ.get("ANTHBOT_WEB_VOICE_INSTALLER_ENABLED", "false")
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _cleanup() -> None:
    now = time.time()
    with _LOCK:
        for session_id in [
            key for key, value in _SESSIONS.items() if value.get("expires_at", 0) <= now
        ]:
            _SESSIONS.pop(session_id, None)
        for job_id in [
            key for key, value in _JOBS.items() if value.get("retain_until", 0) <= now
        ]:
            _JOBS.pop(job_id, None)
        cutoff = now - _LOGIN_WINDOW_SECONDS
        for key, attempts in list(_LOGIN_ATTEMPTS.items()):
            kept = [stamp for stamp in attempts if stamp >= cutoff]
            if kept:
                _LOGIN_ATTEMPTS[key] = kept
            else:
                _LOGIN_ATTEMPTS.pop(key, None)


def _source_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value[:128]
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        value = forwarded.split(",", 1)[0].strip()
        if value:
            return value[:128]
    if request.client is not None and request.client.host:
        return str(request.client.host)[:128]
    return "unknown"


def _rate_key(request: Request) -> str:
    return hmac.new(
        _RATE_SECRET,
        _source_ip(request).encode("utf-8", errors="ignore"),
        hashlib.sha256,
    ).hexdigest()


def _check_login_rate(request: Request) -> None:
    _cleanup()
    key = _rate_key(request)
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _LOCK:
        attempts = [stamp for stamp in _LOGIN_ATTEMPTS.get(key, []) if stamp >= cutoff]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="Too many sign-in attempts. Please wait a few minutes and try again.",
            )
        attempts.append(now)
        _LOGIN_ATTEMPTS[key] = attempts


def _store_token_valid(value: str) -> bool:
    return bool(store_api._CLIENT_TOKEN_RE.fullmatch(value))


def _store_token(request: Request) -> str:
    value = request.cookies.get(_STORE_COOKIE, "").strip()
    if not _store_token_valid(value):
        raise HTTPException(
            status_code=409,
            detail="Voice Store browser identity is missing. Reload the page.",
        )
    return value


def _set_store_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _STORE_COOKIE,
        token,
        max_age=_STORE_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _session_public(session: dict[str, Any]) -> dict[str, Any]:
    devices = [
        {
            "device_id": item["device_id"],
            "serial_masked": item["serial_masked"],
            "alias": item["alias"],
            "category": item["category"],
            "supported": bool(item["supported"]),
        }
        for item in session.get("devices", [])
    ]
    supported = [item for item in devices if item["supported"]]
    return {
        "authenticated": True,
        "devices": devices,
        "auto_device_id": supported[0]["device_id"] if len(supported) == 1 else None,
        "expires_in": max(0, int(session.get("expires_at", 0) - time.time())),
    }


def _require_session(request: Request) -> tuple[str, dict[str, Any]]:
    _cleanup()
    session_id = request.cookies.get(_SESSION_COOKIE, "")
    if not session_id:
        raise HTTPException(status_code=401, detail="ANTHBOT sign-in is required.")
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session or session.get("expires_at", 0) <= time.time():
            _SESSIONS.pop(session_id, None)
            raise HTTPException(status_code=401, detail="ANTHBOT session has expired.")
        session["expires_at"] = time.time() + _SESSION_TTL_SECONDS
        return session_id, dict(session)


def _catalog(request: Request, store_token: str) -> dict[str, Any]:
    catalog = store_api._store_catalog(request)
    client_id = store_api._client_id_from_token(store_token)
    store_api._init_store_tables()
    owned: set[str] = set()
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT pack_id, community_id
            FROM store_orders
            WHERE client_id = ?
              AND payment_status = 'paid'
              AND (
                    entitlement_scope = 'web'
                    OR entitlement_scope IS NULL
                  )
            """,
            (client_id,),
        ).fetchall()
    for row in rows:
        owned.add(str(row["pack_id"] or "").strip())
        owned.add(str(row["community_id"] or "").strip())

    packs: list[dict[str, Any]] = []
    for item in catalog.get("packs", []):
        if not isinstance(item, dict):
            continue
        public = dict(item)
        public.pop("music_url", None)
        pack_id = str(public.get("id") or "").strip()
        community_id = str(public.get("community_id") or "").strip()
        access = str(public.get("access") or "free").casefold()
        public["owned"] = (
            access != "paid"
            or pack_id in owned
            or (community_id and community_id in owned)
        )
        packs.append(public)

    return {
        "schema": "anthbot-web-voice-installer-v1",
        "enabled": _enabled(),
        "checkout_available": bool(catalog.get("checkout_available")),
        "packs": packs,
    }


def _find_device(session: dict[str, Any], device_id: str) -> dict[str, Any]:
    for item in session.get("devices", []):
        if item.get("device_id") != device_id:
            continue
        if not item.get("supported"):
            raise HTTPException(
                status_code=409,
                detail="Only ANTHBOT Genie mowers are supported by the web voice installer.",
            )
        return item
    raise HTTPException(status_code=404, detail="Selected mower was not found.")


def _normal_model(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    if normalized.startswith("anthbot"):
        normalized = normalized[len("anthbot") :]
    return normalized


def _pack_supports_device(pack: dict[str, Any], category: str) -> bool:
    models = pack.get("models")
    if not isinstance(models, list) or not models:
        return anthbot.is_genie(category)
    device = _normal_model(category)
    for candidate in models:
        model = _normal_model(str(candidate))
        if model == device:
            return True
        if model == "genie" and device.startswith("genie"):
            return True
    return False


def _resolve_pack(
    request: Request,
    store_token: str,
    pack_id: str,
) -> dict[str, Any]:
    raw: dict[str, Any] | None = None
    try:
        raw = store_api._find_uploaded_pack(pack_id)
    except HTTPException as err:
        if err.status_code != 404:
            raise

    if raw is not None:
        if store_api._is_paid(raw):
            client_id = store_api._client_id_from_token(store_token)
            order = store_api._paid_order_for_client_pack(
                client_id,
                raw,
                entitlement_scope="web",
            )
            if order is None:
                raise HTTPException(
                    status_code=402,
                    detail="This voice pack has not been purchased in this browser.",
                )
            license_key = store_api._license_for_order(order)
            base = core._public_base_url(request)
            music_url = (
                f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}/download"
                f"?license={quote(license_key)}"
            )
        else:
            music_url = str(core._public_voice_pack(raw, request).get("music_url") or "")
        pack = dict(raw)
        pack["music_url"] = music_url
    else:
        pack = next(
            (
                dict(item)
                for item in core._voice_pack_registry(request).get("packs", [])
                if isinstance(item, dict) and str(item.get("id") or "") == pack_id
            ),
            None,
        )
        if pack is None:
            raise HTTPException(status_code=404, detail="Voice pack not found.")

    music_url = str(pack.get("music_url") or "").strip()
    parsed = urlsplit(music_url)
    allowed_hosts = {
        "anthbotmap.com",
        "www.anthbotmap.com",
        "reports.mqbretrofithungary.online",
        "ha.mqbretrofithungary.online",
    }
    configured_host = urlsplit(core._public_base_url(request)).hostname
    if configured_host:
        allowed_hosts.add(configured_host.casefold())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() not in allowed_hosts
    ):
        raise HTTPException(
            status_code=409,
            detail="Voice pack download URL is not approved for robot installation.",
        )

    md5 = str(pack.get("music_md5") or "").strip().lower()
    if not _MD5_RE.fullmatch(md5):
        raise HTTPException(status_code=409, detail="Voice pack checksum is invalid.")
    try:
        size = int(pack.get("size") or 0)
        music_package = int(pack.get("music_package"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Voice pack metadata is invalid.") from None
    if size <= 0 or music_package < 0:
        raise HTTPException(status_code=409, detail="Voice pack metadata is incomplete.")

    return {
        "id": str(pack.get("id") or pack_id),
        "language": str(pack.get("language") or pack.get("language_code") or "Voice"),
        "variant_name": str(pack.get("variant_name") or ""),
        "music_package": music_package,
        "english_name": str(pack.get("english_name") or "German"),
        "sex": str(pack.get("sex") or "girl"),
        "music_url": music_url,
        "music_md5": md5,
        "size": size,
        "version": str(pack.get("version") or ""),
        "models": list(pack.get("models") or []),
    }


def _job_update(job_id: str, **values: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        job["updated_at"] = time.time()
        if values.get("status") in {"success", "failed"}:
            job["retain_until"] = time.time() + _JOB_RETENTION_SECONDS


async def _run_install(
    job_id: str,
    access_token: str,
    serial: str,
    pack: dict[str, Any],
) -> None:
    try:
        await asyncio.to_thread(
            anthbot.install_voice,
            access_token=access_token,
            serial=serial,
            pack=pack,
            update=lambda **values: _job_update(job_id, **values),
        )
    except HTTPException as err:
        _job_update(job_id, status="failed", message=str(err.detail))
    except Exception as err:
        _job_update(
            job_id,
            status="failed",
            message=str(err)[:400] or "Voice installation failed.",
        )


def _page_html() -> str:
    try:
        return Path(__file__).with_name("voice_installer.html").read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(status_code=503, detail="Voice installer page unavailable.") from err


@router.get("/voice-installer", response_class=HTMLResponse)
def voice_installer_page(request: Request) -> Response:
    redirect = store_api._canonical_public_redirect(request, "/voice-installer")
    if redirect is not None:
        return redirect
    response = HTMLResponse(
        _page_html(),
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
    token = request.cookies.get(_STORE_COOKIE, "").strip()
    if not _store_token_valid(token):
        _set_store_cookie(response, secrets.token_urlsafe(36))
    return response


@router.get("/api/anthbot/web-installer/session")
def installer_session(request: Request) -> Response:
    if not _enabled():
        return _json({"enabled": False, "authenticated": False})
    try:
        _, session = _require_session(request)
    except HTTPException as err:
        if err.status_code == 401:
            return _json({"enabled": True, "authenticated": False})
        raise
    payload = _session_public(session)
    payload["enabled"] = True
    return _json(payload)


@router.get("/api/anthbot/web-installer/catalog")
def installer_catalog(request: Request) -> Response:
    if not _enabled():
        raise HTTPException(status_code=503, detail="Web voice installer is disabled.")
    return _json(_catalog(request, _store_token(request)))


@router.post("/api/anthbot/web-installer/login")
async def installer_login(payload: LoginPayload, request: Request) -> Response:
    if not _enabled():
        raise HTTPException(status_code=503, detail="Web voice installer is disabled.")
    _check_login_rate(request)

    password = payload.password
    try:
        access_token, raw_devices = await asyncio.to_thread(
            anthbot.login_and_devices,
            payload.username,
            password,
            payload.area_code,
        )
    finally:
        password = ""

    devices = []
    for item in raw_devices:
        item = dict(item)
        item["device_id"] = secrets.token_urlsafe(12)
        devices.append(item)

    session_id = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[session_id] = {
            "access_token": access_token,
            "devices": devices,
            "created_at": time.time(),
            "expires_at": time.time() + _SESSION_TTL_SECONDS,
        }
        public = _session_public(_SESSIONS[session_id])

    response = _json(public)
    response.set_cookie(
        _SESSION_COOKIE,
        session_id,
        max_age=_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/anthbot/web-installer/logout")
def installer_logout(request: Request) -> Response:
    session_id = request.cookies.get(_SESSION_COOKIE, "")
    with _LOCK:
        _SESSIONS.pop(session_id, None)
    response = _json({"logged_out": True})
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return response


@router.post("/api/anthbot/web-installer/checkout")
async def installer_checkout(payload: CheckoutPayload, request: Request) -> Response:
    if not _enabled():
        raise HTTPException(status_code=503, detail="Web voice installer is disabled.")
    store_api._require_checkout_ready()

    token = _store_token(request)
    record = store_api._find_uploaded_pack(payload.pack_id)
    if not store_api._is_paid(record):
        raise HTTPException(status_code=409, detail="This voice pack does not require payment.")

    client_id = store_api._client_id_from_token(token)
    existing = store_api._paid_order_for_client_pack(
        client_id,
        record,
        entitlement_scope="web",
    )
    if existing is not None:
        return _json(
            {
                "already_owned": True,
                "pack_id": payload.pack_id,
                "checkout_url": None,
            }
        )

    base = core._public_base_url(request)
    success_url = (
        f"{base}/voice-installer?paid=1&pack={quote(payload.pack_id)}"
        "&session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = f"{base}/voice-installer?cancelled=1&pack={quote(payload.pack_id)}"

    checkout = await asyncio.to_thread(
        store_api._create_checkout_session,
        record,
        request,
        client_id=client_id,
        pair_code=None,
        entitlement_scope="web",
        success_url_override=success_url,
        cancel_url_override=cancel_url,
    )
    order = store_api._upsert_order_from_session(checkout)
    checkout_url = checkout.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL.")
    return _json(
        {
            "already_owned": False,
            "pack_id": payload.pack_id,
            "checkout_url": checkout_url,
            "session_id": order["stripe_session_id"],
        }
    )


@router.post("/api/anthbot/web-installer/purchase-sync/{session_id}")
async def installer_purchase_sync(session_id: str, request: Request) -> Response:
    if not _enabled():
        raise HTTPException(status_code=503, detail="Web voice installer is disabled.")
    if not store_api._SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="Invalid checkout session.")

    client_id = store_api._client_id_from_token(_store_token(request))
    order = store_api._order_by_session(session_id)
    payment_state = (
        str(order.get("payment_status") or "").casefold() if order is not None else ""
    )
    if order is None or payment_state not in {"paid", "refunded"}:
        store_api._require_checkout_ready()
        stripe_session = await asyncio.to_thread(
            store_api._retrieve_checkout_session,
            session_id,
        )
        order = store_api._upsert_order_from_session(stripe_session)

    if str(order.get("client_id") or "") != client_id:
        raise HTTPException(
            status_code=403,
            detail="This purchase belongs to a different browser.",
        )
    return _json(
        {
            "payment_status": str(order.get("payment_status") or ""),
            "pack_id": str(order.get("pack_id") or ""),
        }
    )


@router.post("/api/anthbot/web-installer/install")
async def installer_install(payload: InstallPayload, request: Request) -> Response:
    if not _enabled():
        raise HTTPException(status_code=503, detail="Web voice installer is disabled.")

    session_id, session = _require_session(request)
    device = _find_device(session, payload.device_id)
    pack = _resolve_pack(request, _store_token(request), payload.pack_id)

    if not _pack_supports_device(pack, str(device.get("category") or "")):
        raise HTTPException(
            status_code=409,
            detail="The selected voice pack is not compatible with this Genie model.",
        )

    with _LOCK:
        for job in _JOBS.values():
            if (
                job.get("session_id") == session_id
                and job.get("status") in {"queued", "running"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A voice installation is already running.",
                )

        job_id = f"vjob_{secrets.token_urlsafe(12)}"
        voice_name = " · ".join(
            value for value in (pack["language"], pack["variant_name"]) if value
        )
        _JOBS[job_id] = {
            "job_id": job_id,
            "session_id": session_id,
            "status": "running",
            "progress": 3,
            "message": "Starting secure voice installation…",
            "voice_name": voice_name,
            "device_name": str(device.get("alias") or "ANTHBOT Genie"),
            "created_at": time.time(),
            "updated_at": time.time(),
            "retain_until": time.time() + _JOB_RETENTION_SECONDS,
        }

    asyncio.create_task(
        _run_install(
            job_id,
            str(session["access_token"]),
            str(device["serial"]),
            pack,
        )
    )
    return _json(
        {
            "started": True,
            "job_id": job_id,
            "voice_name": voice_name,
            "device_name": str(device.get("alias") or "ANTHBOT Genie"),
        },
        status_code=202,
    )


@router.get("/api/anthbot/web-installer/jobs/{job_id}")
def installer_job(job_id: str, request: Request) -> Response:
    session_id, _ = _require_session(request)
    _cleanup()
    with _LOCK:
        job = dict(_JOBS.get(job_id) or {})
    if not job or job.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Installation job not found.")

    return _json(
        {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": int(job.get("progress") or 0),
            "message": str(job.get("message") or ""),
            "voice_name": str(job.get("voice_name") or ""),
            "device_name": str(job.get("device_name") or ""),
        }
    )
