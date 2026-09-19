from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

USAGE_SCHEMA = "anthbot-map-anonymous-usage-v1"
DIAGNOSTICS_SCHEMA = "anthbot-map-diagnostics-upload-v1"
VOICE_PACKS_SCHEMA = "anthbot-community-voice-packs-v1"
COMMUNITY_TECHNICAL_SLOT = "German_girl"
COMMUNITY_TECHNICAL_LANGUAGE = "German"
COMMUNITY_TECHNICAL_SEX = "girl"
COMMUNITY_MUSIC_PACKAGE = 3
_COMMUNITY_VERSION_RE = re.compile(r"^1\.2\.(\d+)$")
_COMMUNITY_VERSION_MIN_PATCH = 4
DEFAULT_DB_PATH = "/data/anthbot_reporting.sqlite3"
DEFAULT_VOICE_PACK_DIR = "/data/voice_packs"
MAX_TELEMETRY_BYTES = 64 * 1024
MAX_DIAGNOSTICS_BYTES = 2 * 1024 * 1024
MAX_VOICE_PACK_BYTES = 32 * 1024 * 1024
MAX_VOICE_PACK_UPLOAD_BYTES = MAX_VOICE_PACK_BYTES + 1024 * 1024
VOICE_PACK_CHUNK_BYTES = 256 * 1024
_OFFICIAL_VOICE_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_VOICE_PACK_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DASHBOARD_COOKIE = "anthbot_admin_session"
_DASHBOARD_SESSION_SECONDS = 12 * 60 * 60

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "credential",
    "authorization",
    "cookie",
    "access_key",
    "session_key",
    "session_token",
    "bearer",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    value = dt or _utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _db_path() -> Path:
    return Path(os.environ.get("ANTHBOT_DB_PATH", DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS installations (
                installation_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_event TEXT NOT NULL,
                country TEXT,
                integration_version TEXT,
                home_assistant_version TEXT,
                device_count INTEGER NOT NULL,
                models_json TEXT NOT NULL,
                model_counts_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telemetry_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                installation_id TEXT NOT NULL,
                event TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                UNIQUE(installation_id, event, generated_at)
            );

            CREATE INDEX IF NOT EXISTS idx_telemetry_received_at
                ON telemetry_events(received_at);

            CREATE TABLE IF NOT EXISTS diagnostics (
                report_id TEXT PRIMARY KEY,
                installation_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                report_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_diagnostics_received_at
                ON diagnostics(received_at);
            CREATE INDEX IF NOT EXISTS idx_diagnostics_installation_id
                ON diagnostics(installation_id);
            CREATE INDEX IF NOT EXISTS idx_diagnostics_trigger
                ON diagnostics(trigger);
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    yield


app = FastAPI(
    title="ANTHBOT Map reporting server",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


class UsagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal[USAGE_SCHEMA] = Field(alias="schema")
    event: Literal["installation", "opt_in", "heartbeat"]
    generated_at: datetime
    installation_id: UUID
    integration_version: str | None = Field(default=None, max_length=64)
    home_assistant_version: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, max_length=128)
    device_count: int = Field(ge=0, le=100)
    models: list[str] = Field(default_factory=list, max_length=100)
    model_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            item = item.strip()
            if not item or len(item) > 128:
                raise ValueError("model names must be 1..128 characters")
            normalized.append(item)
        return normalized

    @field_validator("model_counts")
    @classmethod
    def _validate_model_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > 100:
            raise ValueError("too many model count entries")
        normalized: dict[str, int] = {}
        for key, count in value.items():
            key = key.strip()
            if not key or len(key) > 128:
                raise ValueError("model count keys must be 1..128 characters")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 100:
                raise ValueError("invalid model count")
            normalized[key] = count
        return normalized

    @model_validator(mode="after")
    def _consistent_counts(self) -> "UsagePayload":
        if sum(self.model_counts.values()) != self.device_count:
            raise ValueError("model_counts must sum to device_count")
        if set(self.models) != set(self.model_counts):
            raise ValueError("models must match model_counts keys")
        return self


class DiagnosticsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal[DIAGNOSTICS_SCHEMA] = Field(alias="schema")
    generated_at: datetime
    installation_id: UUID
    trigger: str = Field(min_length=1, max_length=128)
    report: dict[str, Any]


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.method == "POST":
        if request.url.path == "/api/anthbot/admin/voice-packs":
            limit = MAX_VOICE_PACK_UPLOAD_BYTES
        elif request.url.path.endswith("/diagnostics"):
            limit = MAX_DIAGNOSTICS_BYTES
        else:
            limit = MAX_TELEMETRY_BYTES
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    return _too_large_response(limit)
            except ValueError:
                pass
    response = await call_next(request)
    if request.url.path.startswith("/dashboard") or request.url.path.startswith(
        "/api/anthbot/admin/"
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _too_large_response(limit: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        content={"detail": f"request body exceeds {limit} bytes"},
    )


def _contains_sensitive_key(value: Any, *, depth: int = 0) -> bool:
    if depth > 20:
        return True
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_key(child, depth=depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(child, depth=depth + 1) for child in value)
    return False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _admin_token() -> str:
    return os.environ.get("ANTHBOT_ADMIN_TOKEN", "")


def _dashboard_session_value() -> str:
    token = _admin_token()
    if not token:
        return ""
    return hmac.new(
        token.encode("utf-8"),
        b"anthbot-reporting-dashboard-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _request_is_admin(request: Request, authorization: str | None = None) -> bool:
    expected = _admin_token()
    if not expected:
        return False
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied = authorization[7:]
        if supplied and secrets.compare_digest(supplied, expected):
            return True
    cookie = request.cookies.get(_DASHBOARD_COOKIE, "")
    session_value = _dashboard_session_value()
    return bool(cookie and session_value and secrets.compare_digest(cookie, session_value))


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    if not _admin_token():
        raise HTTPException(status_code=503, detail="admin access is not configured")
    if not _request_is_admin(request, authorization):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _voice_pack_dir() -> Path:
    """Return the persistent directory used for uploaded community packs."""
    return Path(os.environ.get("ANTHBOT_VOICE_PACK_DIR", DEFAULT_VOICE_PACK_DIR))


def _uploaded_voice_registry_path() -> Path:
    return _voice_pack_dir() / "registry.json"


def _load_json_registry(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {"schema": VOICE_PACKS_SCHEMA, "packs": []}
        raise HTTPException(
            status_code=503, detail="community voice registry unavailable"
        )
    except (OSError, ValueError) as err:
        raise HTTPException(
            status_code=503, detail="community voice registry unavailable"
        ) from err

    if (
        not isinstance(payload, dict)
        or payload.get("schema") != VOICE_PACKS_SCHEMA
        or not isinstance(payload.get("packs"), list)
    ):
        raise HTTPException(
            status_code=503, detail="community voice registry is invalid"
        )
    return payload


def _bundled_voice_pack_registry() -> dict[str, Any]:
    return _load_json_registry(
        Path(__file__).with_name("voice_packs.json"),
        required=True,
    )


def _uploaded_voice_pack_registry() -> dict[str, Any]:
    return _load_json_registry(_uploaded_voice_registry_path(), required=False)


def _write_uploaded_voice_registry(payload: dict[str, Any]) -> None:
    directory = _voice_pack_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = _uploaded_voice_registry_path()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _public_voice_pack(record: dict[str, Any], request: Request) -> dict[str, Any]:
    public = dict(record)
    filename = public.pop("filename", None)
    public.pop("uploaded_at", None)
    if isinstance(filename, str) and filename:
        base = str(request.base_url).rstrip("/")
        public["music_url"] = f"{base}/voice-packs/{quote(filename)}"
    return public


def _voice_pack_identity(record: dict[str, Any]) -> tuple[str, str]:
    """Return legacy language + variant identity for one Community pack."""
    language_code = str(record.get("language_code", "")).strip().casefold()
    variant_id = str(record.get("variant_id", "")).strip().casefold() or "default"
    return language_code, variant_id


def _voice_pack_community_id(record: dict[str, Any]) -> str:
    """Return the stable Builder/registry identity for one Community voice."""
    community_id = str(record.get("community_id", "")).strip().casefold()
    if community_id:
        return community_id
    language_code, variant_id = _voice_pack_identity(record)
    return f"{language_code}_{variant_id}".strip("_")


def _voice_pack_technical_slot(record: dict[str, Any]) -> str:
    slot = str(record.get("technical_slot", "")).strip()
    if slot:
        return slot
    english_name = str(record.get("english_name", "")).strip()
    sex = str(record.get("sex", "")).strip()
    if english_name and sex:
        return f"{english_name}_{sex}"
    return ""


def _next_community_version(records: list[dict[str, Any]]) -> str:
    """Allocate a unique German_girl Community version from the 1.2.x range."""
    max_patch = _COMMUNITY_VERSION_MIN_PATCH
    for item in records:
        if not isinstance(item, dict):
            continue
        if _voice_pack_technical_slot(item).casefold() != COMMUNITY_TECHNICAL_SLOT.casefold():
            continue
        match = _COMMUNITY_VERSION_RE.fullmatch(str(item.get("version", "")).strip())
        if match:
            max_patch = max(max_patch, int(match.group(1)))
    return f"1.2.{max_patch + 1}"


def _voice_pack_registry(request: Request) -> dict[str, Any]:
    """Return bundled packs plus persistent uploads, overriding exact variants only."""
    bundled = _bundled_voice_pack_registry().get("packs", [])
    uploaded = _uploaded_voice_pack_registry().get("packs", [])

    merged: list[dict[str, Any]] = []
    uploaded_identities = {
        _voice_pack_community_id(item)
        for item in uploaded
        if isinstance(item, dict) and _voice_pack_community_id(item)
    }
    for item in bundled:
        if not isinstance(item, dict):
            continue
        if _voice_pack_community_id(item) in uploaded_identities:
            continue
        merged.append(_public_voice_pack(item, request))
    for item in uploaded:
        if isinstance(item, dict):
            merged.append(_public_voice_pack(item, request))

    return {"schema": VOICE_PACKS_SCHEMA, "packs": merged}


def _voice_pack_safe_part(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not _VOICE_PACK_SAFE_PART.fullmatch(normalized):
        raise HTTPException(
            status_code=422,
            detail=f"{field} must use only letters, numbers, dot, underscore or hyphen",
        )
    return normalized


def _voice_pack_models(value: str) -> list[str]:
    models = [item.strip() for item in value.split(",") if item.strip()]
    if not models or len(models) > 20 or any(len(item) > 128 for item in models):
        raise HTTPException(status_code=422, detail="invalid voice pack models")
    return models


def _dashboard_file(name: str) -> str:
    try:
        return Path(__file__).with_name(name).read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(status_code=503, detail="dashboard asset unavailable") from err


def _installation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        model_counts = json.loads(row["model_counts_json"])
    except (TypeError, ValueError):
        model_counts = {}
    if not isinstance(model_counts, dict):
        model_counts = {}
    return {
        "installation_id": row["installation_id"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "last_event": row["last_event"],
        "country": row["country"],
        "integration_version": row["integration_version"],
        "home_assistant_version": row["home_assistant_version"],
        "device_count": row["device_count"],
        "model_counts": model_counts,
    }


@app.get("/api/anthbot/voice-packs")
def community_voice_packs(request: Request) -> dict[str, Any]:
    """Return public custom/community voice packs for ANTHBOT Map."""
    return _voice_pack_registry(request)


@app.get("/voice-packs/{filename}", name="download_voice_pack")
def download_voice_pack(filename: str) -> FileResponse:
    """Serve one uploaded voice pack directly to a mower."""
    if Path(filename).name != filename or not _VOICE_PACK_SAFE_PART.fullmatch(filename):
        raise HTTPException(status_code=404, detail="voice pack not found")
    path = _voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="voice pack not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get(
    "/api/anthbot/admin/voice-packs",
    dependencies=[Depends(require_admin)],
)
def admin_voice_packs(request: Request) -> dict[str, Any]:
    """Return the effective registry and persistent uploaded metadata."""
    return {
        "effective": _voice_pack_registry(request),
        "uploaded": _uploaded_voice_pack_registry(),
    }


@app.post(
    "/api/anthbot/admin/voice-packs",
    dependencies=[Depends(require_admin)],
    status_code=201,
)
async def upload_voice_pack(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form(..., min_length=1, max_length=64),
    language_code: str = Form(..., min_length=2, max_length=16),
    version: str = Form(default="", max_length=64),
    community_id: str = Form(default="", max_length=64),
    variant_id: str = Form(default="default", min_length=1, max_length=64),
    variant_name: str = Form(default="", max_length=128),
    voice_gender: str = Form(default="unknown", max_length=32),
    technical_slot: str = Form(default=COMMUNITY_TECHNICAL_SLOT, max_length=64),
    english_name: str = Form(default=COMMUNITY_TECHNICAL_LANGUAGE, min_length=1, max_length=64),
    sex: str = Form(default=COMMUNITY_TECHNICAL_SEX, min_length=1, max_length=32),
    music_package: int = Form(default=COMMUNITY_MUSIC_PACKAGE, ge=0, le=9999),
    models: str = Form(default="Anthbot Genie 1000", max_length=2048),
) -> dict[str, Any]:
    """Persist a custom voice pack and publish it in the community registry."""
    language = language.strip()
    if not language:
        raise HTTPException(status_code=422, detail="language is required")
    language_code = _voice_pack_safe_part(
        language_code.strip().lower(), field="language_code"
    )
    requested_version = version.strip()
    if requested_version:
        requested_version = _voice_pack_safe_part(
            requested_version, field="version"
        )
    variant_id = _voice_pack_safe_part(variant_id.lower(), field="variant_id")
    variant_name = variant_name.strip()
    voice_gender = (voice_gender or "unknown").strip().lower()
    if voice_gender:
        voice_gender = _voice_pack_safe_part(voice_gender, field="voice_gender")

    if community_id.strip():
        community_id = _voice_pack_safe_part(
            community_id.strip().lower(), field="community_id"
        )
    else:
        community_id = _voice_pack_safe_part(
            f"{language_code}_{variant_id}", field="community_id"
        )

    technical_slot = _voice_pack_safe_part(
        technical_slot.strip(), field="technical_slot"
    )
    if technical_slot.casefold() != COMMUNITY_TECHNICAL_SLOT.casefold():
        raise HTTPException(
            status_code=422,
            detail=f"community voice technical_slot must be {COMMUNITY_TECHNICAL_SLOT}",
        )

    # Community packs always occupy the proven Genie German_girl / slot-3 path.
    # Human speaker gender is carried separately in voice_gender.
    if english_name.strip().casefold() != COMMUNITY_TECHNICAL_LANGUAGE.casefold():
        raise HTTPException(
            status_code=422,
            detail=f"community voice english_name must be {COMMUNITY_TECHNICAL_LANGUAGE}",
        )
    if sex.strip().casefold() != COMMUNITY_TECHNICAL_SEX.casefold():
        raise HTTPException(
            status_code=422,
            detail=f"community voice sex must be {COMMUNITY_TECHNICAL_SEX}",
        )
    if music_package != COMMUNITY_MUSIC_PACKAGE:
        raise HTTPException(
            status_code=422,
            detail=f"community voice music_package must be {COMMUNITY_MUSIC_PACKAGE}",
        )
    english_name = COMMUNITY_TECHNICAL_LANGUAGE
    sex = COMMUNITY_TECHNICAL_SEX
    music_package = COMMUNITY_MUSIC_PACKAGE
    model_list = _voice_pack_models(models)

    directory = _voice_pack_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".voice-pack.{secrets.token_hex(8)}.upload"
    digest = hashlib.md5(usedforsecurity=False)
    size = 0

    try:
        try:
            with temporary.open("wb") as output:
                while True:
                    chunk = await file.read(VOICE_PACK_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_VOICE_PACK_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"voice pack exceeds {MAX_VOICE_PACK_BYTES} bytes",
                        )
                    digest.update(chunk)
                    output.write(chunk)
        finally:
            await file.close()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    if size == 0:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="voice pack is empty")

    local_md5 = digest.hexdigest()
    bundled = [
        item
        for item in _bundled_voice_pack_registry().get("packs", [])
        if isinstance(item, dict)
    ]
    registry = _uploaded_voice_pack_registry()
    existing = [
        item for item in registry.get("packs", []) if isinstance(item, dict)
    ]

    stable_id = community_id.casefold()
    effective_match = next(
        (
            item
            for item in existing + bundled
            if _voice_pack_community_id(item) == stable_id
        ),
        None,
    )
    effective_version = (
        str(effective_match.get("version", "")).strip()
        if isinstance(effective_match, dict)
        else ""
    )
    same_payload = (
        isinstance(effective_match, dict)
        and str(effective_match.get("music_md5", "")).strip().casefold()
        == local_md5.casefold()
        and _voice_pack_technical_slot(effective_match).casefold()
        == COMMUNITY_TECHNICAL_SLOT.casefold()
        and _COMMUNITY_VERSION_RE.fullmatch(effective_version) is not None
    )
    if same_payload:
        assigned_version = effective_version
    else:
        assigned_version = _next_community_version(bundled + existing)
    if not assigned_version:
        temporary.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail="could not allocate community voice version"
        )

    replaced = [
        item
        for item in existing
        if _voice_pack_community_id(item) == stable_id
    ]
    kept = [item for item in existing if item not in replaced]

    package_id = f"{community_id}-v{assigned_version}"
    filename = package_id
    original_suffix = Path(file.filename or "").suffix
    if (
        original_suffix
        and len(original_suffix) <= 12
        and re.fullmatch(r"\.[A-Za-z0-9]+", original_suffix)
    ):
        filename += original_suffix.lower()
    target = directory / filename

    record = {
        "id": package_id,
        "community_id": community_id,
        "language": language,
        "language_code": language_code,
        "variant_id": variant_id,
        "variant_name": variant_name,
        "voice_gender": voice_gender or "unknown",
        "technical_slot": COMMUNITY_TECHNICAL_SLOT,
        "english_name": english_name,
        "sex": sex,
        "music_package": music_package,
        "version": assigned_version,
        "requested_version": requested_version,
        "version_source": "reporting_server",
        "filename": filename,
        "music_md5": local_md5,
        "size": size,
        "models": model_list,
        "source": "community",
        "uploaded_at": _iso(),
    }

    backup: Path | None = None
    if target.exists():
        backup = directory / f".{filename}.{secrets.token_hex(4)}.backup"
        os.replace(target, backup)

    try:
        os.replace(temporary, target)
        _write_uploaded_voice_registry(
            {"schema": VOICE_PACKS_SCHEMA, "packs": kept + [record]}
        )
    except BaseException:
        target.unlink(missing_ok=True)
        if backup is not None and backup.exists():
            os.replace(backup, target)
        raise
    else:
        if backup is not None:
            backup.unlink(missing_ok=True)

    for old in replaced:
        old_filename = old.get("filename")
        if (
            isinstance(old_filename, str)
            and old_filename != filename
            and Path(old_filename).name == old_filename
        ):
            (_voice_pack_dir() / old_filename).unlink(missing_ok=True)

    public_record = _public_voice_pack(record, request)
    return {
        "uploaded": True,
        "version_assigned_by_server": True,
        "assigned_version": assigned_version,
        "pack": public_record,
    }


@app.delete(
    "/api/anthbot/admin/voice-packs/{pack_id}",
    dependencies=[Depends(require_admin)],
)
def delete_voice_pack(pack_id: str) -> dict[str, Any]:
    """Delete one persistently uploaded community voice pack."""
    registry = _uploaded_voice_pack_registry()
    packs = [item for item in registry.get("packs", []) if isinstance(item, dict)]
    match = next((item for item in packs if item.get("id") == pack_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="uploaded voice pack not found")

    remaining = [item for item in packs if item is not match]
    _write_uploaded_voice_registry(
        {"schema": VOICE_PACKS_SCHEMA, "packs": remaining}
    )
    filename = match.get("filename")
    if (
        isinstance(filename, str)
        and filename
        and Path(filename).name == filename
    ):
        (_voice_pack_dir() / filename).unlink(missing_ok=True)
    return {"deleted": True, "pack_id": pack_id}


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with _db() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"ok": True, "schema": "anthbot-reporting-server-v1"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    if not _admin_token():
        html = _dashboard_file("dashboard_login.html").replace(
            "__ERROR__",
            '<div class="error">Admin access is not configured. Set admin_token in the Home Assistant app configuration.</div>',
        )
        return HTMLResponse(html, status_code=503)
    if _request_is_admin(request):
        return HTMLResponse(_dashboard_file("dashboard.html"))
    return HTMLResponse(_dashboard_file("dashboard_login.html").replace("__ERROR__", ""))


@app.post("/dashboard/login")
async def dashboard_login(request: Request):
    if not _admin_token():
        return HTMLResponse(
            _dashboard_file("dashboard_login.html").replace(
                "__ERROR__",
                '<div class="error">Admin access is not configured.</div>',
            ),
            status_code=503,
        )
    raw = (await request.body()).decode("utf-8", errors="replace")
    supplied = parse_qs(raw, keep_blank_values=True).get("token", [""])[0]
    if not supplied or not secrets.compare_digest(supplied, _admin_token()):
        return HTMLResponse(
            _dashboard_file("dashboard_login.html").replace(
                "__ERROR__", '<div class="error">Invalid admin token.</div>'
            ),
            status_code=401,
        )
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        _DASHBOARD_COOKIE,
        _dashboard_session_value(),
        max_age=_DASHBOARD_SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/dashboard/logout")
def dashboard_logout() -> RedirectResponse:
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.delete_cookie(_DASHBOARD_COOKIE, path="/")
    return response


@app.post("/api/anthbot/telemetry", status_code=202)
def ingest_telemetry(payload: UsagePayload) -> dict[str, Any]:
    received_at = _iso()
    installation_id = str(payload.installation_id)
    generated_at = _iso(payload.generated_at)
    models_json = _canonical_json(sorted(set(payload.models)))
    model_counts_json = _canonical_json(dict(sorted(payload.model_counts.items())))

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO installations (
                installation_id, first_seen, last_seen, last_event, country,
                integration_version, home_assistant_version, device_count,
                models_json, model_counts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(installation_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                last_event=excluded.last_event,
                country=excluded.country,
                integration_version=excluded.integration_version,
                home_assistant_version=excluded.home_assistant_version,
                device_count=excluded.device_count,
                models_json=excluded.models_json,
                model_counts_json=excluded.model_counts_json
            """,
            (
                installation_id,
                received_at,
                received_at,
                payload.event,
                payload.country,
                payload.integration_version,
                payload.home_assistant_version,
                payload.device_count,
                models_json,
                model_counts_json,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO telemetry_events (
                installation_id, event, generated_at, received_at
            ) VALUES (?, ?, ?, ?)
            """,
            (installation_id, payload.event, generated_at, received_at),
        )

    return {"accepted": True}


@app.post("/api/anthbot/diagnostics", status_code=202)
def ingest_diagnostics(payload: DiagnosticsPayload) -> dict[str, Any]:
    if _contains_sensitive_key(payload.report):
        raise HTTPException(
            status_code=422,
            detail="diagnostics report contains a credential-like field name",
        )

    report_json = _canonical_json(payload.report)
    if len(report_json.encode("utf-8")) > MAX_DIAGNOSTICS_BYTES:
        raise HTTPException(status_code=413, detail="diagnostics report is too large")

    report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
    now = _utcnow()
    report_id = f"AB-{now:%Y%m%d}-{secrets.token_hex(4).upper()}"

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO diagnostics (
                report_id, installation_id, trigger, generated_at,
                received_at, report_sha256, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                str(payload.installation_id),
                payload.trigger,
                _iso(payload.generated_at),
                _iso(now),
                report_sha256,
                report_json,
            ),
        )

    return {"accepted": True, "report_id": report_id}


@app.get("/api/anthbot/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats() -> dict[str, Any]:
    now = _utcnow()
    since_7d = _iso(now - timedelta(days=7))
    since_30d = _iso(now - timedelta(days=30))
    since_24h = _iso(now - timedelta(hours=24))

    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM installations").fetchone()[0]
        total_devices = conn.execute(
            "SELECT COALESCE(SUM(device_count), 0) FROM installations"
        ).fetchone()[0]
        active_7d = conn.execute(
            "SELECT COUNT(*) FROM installations WHERE last_seen >= ?", (since_7d,)
        ).fetchone()[0]
        active_30d = conn.execute(
            "SELECT COUNT(*) FROM installations WHERE last_seen >= ?", (since_30d,)
        ).fetchone()[0]
        country_rows = conn.execute(
            """
            SELECT COALESCE(country, 'Unknown') AS name, COUNT(*) AS count
            FROM installations GROUP BY COALESCE(country, 'Unknown')
            ORDER BY count DESC, name ASC
            """
        ).fetchall()
        version_rows = conn.execute(
            """
            SELECT COALESCE(integration_version, 'Unknown') AS name, COUNT(*) AS count
            FROM installations GROUP BY COALESCE(integration_version, 'Unknown')
            ORDER BY count DESC, name ASC
            """
        ).fetchall()
        install_rows = conn.execute("SELECT model_counts_json FROM installations").fetchall()
        diag_24h = conn.execute(
            "SELECT COUNT(*) FROM diagnostics WHERE received_at >= ?", (since_24h,)
        ).fetchone()[0]
        diag_7d = conn.execute(
            "SELECT COUNT(*) FROM diagnostics WHERE received_at >= ?", (since_7d,)
        ).fetchone()[0]
        trigger_rows = conn.execute(
            """
            SELECT trigger AS name, COUNT(*) AS count
            FROM diagnostics WHERE received_at >= ?
            GROUP BY trigger ORDER BY count DESC, name ASC
            """,
            (since_30d,),
        ).fetchall()

    model_counts: dict[str, int] = {}
    for row in install_rows:
        try:
            counts = json.loads(row["model_counts_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(counts, dict):
            continue
        for model, count in counts.items():
            if isinstance(model, str) and isinstance(count, int):
                model_counts[model] = model_counts.get(model, 0) + count

    return {
        "generated_at": _iso(now),
        "installations": {
            "total": total,
            "active_7d": active_7d,
            "active_30d": active_30d,
        },
        "total_devices": total_devices,
        "by_country": [dict(row) for row in country_rows],
        "by_integration_version": [dict(row) for row in version_rows],
        "by_model": [
            {"name": name, "count": count}
            for name, count in sorted(
                model_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "diagnostics": {
            "last_24h": diag_24h,
            "last_7d": diag_7d,
            "by_trigger_30d": [dict(row) for row in trigger_rows],
        },
    }


@app.get("/api/anthbot/admin/installations", dependencies=[Depends(require_admin)])
def admin_installations(
    q: str | None = Query(default=None, max_length=160),
    country: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=128),
    version: str | None = Query(default=None, max_length=64),
    active_days: int | None = Query(default=None, ge=1, le=3650),
    sort: str = Query(default="last_seen", max_length=32),
    order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT installation_id, first_seen, last_seen, last_event, country,
                   integration_version, home_assistant_version, device_count,
                   model_counts_json
            FROM installations
            """
        ).fetchall()

    items = [_installation_from_row(row) for row in rows]
    if country:
        items = [item for item in items if (item.get("country") or "Unknown") == country]
    if version:
        items = [
            item
            for item in items
            if (item.get("integration_version") or "Unknown") == version
        ]
    if model:
        items = [item for item in items if model in item.get("model_counts", {})]
    if active_days is not None:
        cutoff = _iso(_utcnow() - timedelta(days=active_days))
        items = [item for item in items if str(item.get("last_seen") or "") >= cutoff]
    if q:
        needle = q.casefold()
        filtered: list[dict[str, Any]] = []
        for item in items:
            haystack = " ".join(
                [
                    str(item.get("installation_id") or ""),
                    str(item.get("country") or ""),
                    str(item.get("integration_version") or ""),
                    str(item.get("home_assistant_version") or ""),
                    str(item.get("last_event") or ""),
                    " ".join(item.get("model_counts", {}).keys()),
                ]
            ).casefold()
            if needle in haystack:
                filtered.append(item)
        items = filtered

    allowed_sort = {
        "installation_id",
        "country",
        "device_count",
        "integration_version",
        "home_assistant_version",
        "first_seen",
        "last_seen",
        "last_event",
    }
    sort_key = sort if sort in allowed_sort else "last_seen"

    def _sort_value(item: dict[str, Any]):
        value = item.get(sort_key)
        if sort_key == "device_count":
            return int(value or 0)
        return str(value or "").casefold()

    items.sort(key=_sort_value, reverse=order == "desc")
    total_matching = len(items)
    return {"count": total_matching, "items": items[:limit]}


@app.get("/api/anthbot/admin/diagnostics", dependencies=[Depends(require_admin)])
def admin_diagnostics(
    limit: int = Query(default=50, ge=1, le=500),
    include_report: bool = Query(default=False),
) -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT report_id, installation_id, trigger, generated_at,
                   received_at, report_sha256, report_json
            FROM diagnostics ORDER BY received_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "report_id": row["report_id"],
            "installation_id": row["installation_id"],
            "trigger": row["trigger"],
            "generated_at": row["generated_at"],
            "received_at": row["received_at"],
            "report_sha256": row["report_sha256"],
        }
        if include_report:
            item["report"] = json.loads(row["report_json"])
        items.append(item)
    return {"items": items}


@app.delete(
    "/api/anthbot/admin/diagnostics/{report_id}",
    dependencies=[Depends(require_admin)],
)
def delete_diagnostic(report_id: str) -> dict[str, Any]:
    with _db() as conn:
        cursor = conn.execute("DELETE FROM diagnostics WHERE report_id = ?", (report_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="report not found")
    return {"deleted": True, "report_id": report_id}


@app.delete(
    "/api/anthbot/admin/installations/{installation_id}",
    dependencies=[Depends(require_admin)],
)
def delete_installation(installation_id: UUID) -> dict[str, Any]:
    value = str(installation_id)
    with _db() as conn:
        conn.execute("DELETE FROM telemetry_events WHERE installation_id = ?", (value,))
        conn.execute("DELETE FROM diagnostics WHERE installation_id = ?", (value,))
        cursor = conn.execute("DELETE FROM installations WHERE installation_id = ?", (value,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="installation not found")
    return {"deleted": True, "installation_id": value}
