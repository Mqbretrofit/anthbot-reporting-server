"""Remote ANTHBOT Map announcements and admin publishing API."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app import _dashboard_file, _db, _iso, _request_is_admin, _utcnow, require_admin
from presence_api import init_presence_tables

router = APIRouter()

ANNOUNCEMENTS_SCHEMA = "anthbot-map-announcements-v1"
SUPPORTED_LANGUAGES = {
    "en", "hu", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
    "ro", "da", "sv", "no", "fi", "zh-CN", "zh-TW", "tr", "th", "vi",
    "ko", "km",
}
_ANNOUNCEMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


def init_announcement_tables() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                announcement_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                show_popup INTEGER NOT NULL DEFAULT 0,
                published INTEGER NOT NULL DEFAULT 0,
                published_at TEXT,
                expires_at TEXT,
                min_version TEXT,
                max_version TEXT,
                target_models_json TEXT NOT NULL,
                target_installations_json TEXT NOT NULL DEFAULT '[]',
                translations_json TEXT NOT NULL,
                link_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_announcements_publication
                ON announcements(published, published_at, expires_at);
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(announcements)").fetchall()
        }
        if "target_installations_json" not in columns:
            conn.execute(
                "ALTER TABLE announcements ADD COLUMN "
                "target_installations_json TEXT NOT NULL DEFAULT '[]'"
            )


class AnnouncementTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    link_label: str | None = Field(default=None, max_length=100)

    @field_validator("title", "body", "link_label")
    @classmethod
    def _strip_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized and info.field_name in {"title", "body"}:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized or None


class AnnouncementWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    announcement_id: str | None = Field(default=None, max_length=80)
    category: Literal["news", "release", "maintenance", "outage", "voice"] = "news"
    priority: Literal["normal", "important", "critical"] = "normal"
    show_popup: bool = False
    published: bool = False
    published_at: datetime | None = None
    expires_at: datetime | None = None
    min_version: str | None = Field(default=None, max_length=64)
    max_version: str | None = Field(default=None, max_length=64)
    target_models: list[str] = Field(default_factory=list, max_length=100)
    target_installations: list[str] = Field(default_factory=list, max_length=500)
    translations: dict[str, AnnouncementTranslation]
    link_url: str | None = Field(default=None, max_length=2048)

    @field_validator("announcement_id")
    @classmethod
    def _validate_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if not _ANNOUNCEMENT_ID_RE.fullmatch(normalized):
            raise ValueError("announcement_id must use lowercase letters, digits, dots, dashes or underscores")
        return normalized

    @field_validator("min_version", "max_version")
    @classmethod
    def _strip_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("target_models")
    @classmethod
    def _clean_models(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            model = str(raw).strip()
            if not model:
                continue
            if len(model) > 128:
                raise ValueError("target model names must be at most 128 characters")
            if model.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(model)
        return cleaned

    @field_validator("target_installations")
    @classmethod
    def _clean_installations(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            try:
                install_id = str(UUID(str(raw).strip()))
            except (ValueError, TypeError, AttributeError) as err:
                raise ValueError("target installation IDs must be UUID values") from err
            if install_id not in cleaned:
                cleaned.append(install_id)
        return cleaned

    @field_validator("translations")
    @classmethod
    def _validate_translations(
        cls, value: dict[str, AnnouncementTranslation]
    ) -> dict[str, AnnouncementTranslation]:
        if not value:
            raise ValueError("at least one translation is required")
        unknown = set(value) - SUPPORTED_LANGUAGES
        if unknown:
            raise ValueError(f"unsupported languages: {', '.join(sorted(unknown))}")
        return value

    @field_validator("link_url")
    @classmethod
    def _validate_link(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("link_url must be an absolute HTTPS URL")
        return normalized

    @model_validator(mode="after")
    def _validate_dates(self) -> "AnnouncementWrite":
        if self.published_at and self.expires_at and self.expires_at <= self.published_at:
            raise ValueError("expires_at must be later than published_at")
        return self


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts: list[int] = []
    for chunk in value.strip().lstrip("vV").split("."):
        match = re.match(r"^(\d+)", chunk)
        if match is None:
            break
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else None


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_payload(row: Any) -> dict[str, Any]:
    try:
        translations = json.loads(row["translations_json"])
    except (TypeError, ValueError):
        translations = {}
    try:
        target_models = json.loads(row["target_models_json"])
    except (TypeError, ValueError):
        target_models = []
    try:
        target_installations = json.loads(row["target_installations_json"])
    except (TypeError, ValueError, IndexError):
        target_installations = []
    return {
        "announcement_id": row["announcement_id"],
        "category": row["category"],
        "priority": row["priority"],
        "show_popup": bool(row["show_popup"]),
        "published": bool(row["published"]),
        "published_at": row["published_at"],
        "expires_at": row["expires_at"],
        "min_version": row["min_version"],
        "max_version": row["max_version"],
        "target_models": target_models if isinstance(target_models, list) else [],
        "target_installations": (
            target_installations if isinstance(target_installations, list) else []
        ),
        "translations": translations if isinstance(translations, dict) else {},
        "link_url": row["link_url"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_language(value: str | None) -> str:
    raw = str(value or "en").replace("_", "-").strip()
    if raw in SUPPORTED_LANGUAGES:
        return raw
    lowered = raw.lower()
    if lowered.startswith("zh"):
        return "zh-TW" if any(part in lowered for part in ("-tw", "-hk", "-mo")) else "zh-CN"
    base = lowered.split("-", 1)[0]
    return base if base in SUPPORTED_LANGUAGES else "en"


def _localized_translation(translations: dict[str, Any], language: str) -> dict[str, Any] | None:
    for key in (language, language.split("-", 1)[0], "en", "hu"):
        candidate = translations.get(key)
        if isinstance(candidate, dict) and candidate.get("title") and candidate.get("body"):
            return candidate
    for candidate in translations.values():
        if isinstance(candidate, dict) and candidate.get("title") and candidate.get("body"):
            return candidate
    return None


def _matches_target(
    item: dict[str, Any], *, version: str | None, models: set[str],
    installation_id: str | None, now: datetime
) -> bool:
    if not item["published"]:
        return False
    published_at = _parse_iso(item.get("published_at"))
    expires_at = _parse_iso(item.get("expires_at"))
    if published_at and published_at > now:
        return False
    if expires_at and expires_at <= now:
        return False

    client_version = _version_tuple(version)
    minimum = _version_tuple(item.get("min_version"))
    maximum = _version_tuple(item.get("max_version"))
    if minimum and (client_version is None or _compare_versions(client_version, minimum) < 0):
        return False
    if maximum and (client_version is None or _compare_versions(client_version, maximum) > 0):
        return False

    target_installations = {
        str(value).strip().lower()
        for value in item.get("target_installations", [])
        if str(value).strip()
    }
    if target_installations and str(installation_id or "").strip().lower() not in target_installations:
        return False

    targets = {
        str(model).strip().casefold()
        for model in item.get("target_models", [])
        if str(model).strip()
    }
    return not targets or bool(targets & models)


@router.get("/api/anthbot/announcements")
def public_announcements(
    response: Response,
    version: str | None = Query(default=None, max_length=64),
    language: str | None = Query(default="en", max_length=32),
    models: str | None = Query(default=None, max_length=4096),
    installation_id: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    init_announcement_tables()
    normalized_language = _normalize_language(language)
    requested_models = {
        item.strip().casefold()
        for item in str(models or "").split(",")
        if item.strip()
    }
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM announcements ORDER BY COALESCE(published_at, created_at) DESC, created_at DESC"
        ).fetchall()

    now = _utcnow()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _row_payload(row)
        if not _matches_target(
            item,
            version=version,
            models=requested_models,
            installation_id=installation_id,
            now=now,
        ):
            continue
        translation = _localized_translation(item["translations"], normalized_language)
        if translation is None:
            continue
        items.append(
            {
                "id": item["announcement_id"],
                "category": item["category"],
                "priority": item["priority"],
                "show_popup": item["show_popup"],
                "title": translation["title"],
                "body": translation["body"],
                "link": item["link_url"],
                "link_label": translation.get("link_label"),
                "published_at": item["published_at"] or item["created_at"],
                "expires_at": item["expires_at"],
            }
        )

    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "schema": ANNOUNCEMENTS_SCHEMA,
        "generated_at": _iso(now),
        "language": normalized_language,
        "items": items[:50],
    }


@router.get(
    "/api/anthbot/admin/announcements", dependencies=[Depends(require_admin)]
)
def admin_announcements() -> dict[str, Any]:
    init_announcement_tables()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM announcements ORDER BY updated_at DESC"
        ).fetchall()
    return {"items": [_row_payload(row) for row in rows]}


@router.get(
    "/api/anthbot/admin/announcement-targets",
    dependencies=[Depends(require_admin)],
)
def announcement_targets() -> dict[str, Any]:
    """Return live target choices from the privacy-minimal presence database."""
    init_presence_tables()
    with _db() as conn:
        installation_rows = conn.execute(
            """
            SELECT install_id, version, first_seen, last_seen
            FROM installation_presence
            ORDER BY last_seen DESC
            LIMIT 1000
            """
        ).fetchall()
        model_rows = conn.execute(
            """
            SELECT install_id, model, last_seen
            FROM installation_presence_models
            ORDER BY last_seen DESC, model ASC
            """
        ).fetchall()

    models_by_installation: dict[str, list[str]] = {}
    model_installations: dict[str, set[str]] = {}
    model_last_seen: dict[str, str] = {}
    for row in model_rows:
        install_id = str(row["install_id"])
        model = str(row["model"]).strip()
        if not model:
            continue
        models_by_installation.setdefault(install_id, []).append(model)
        model_installations.setdefault(model, set()).add(install_id)
        model_last_seen[model] = max(
            model_last_seen.get(model, ""), str(row["last_seen"] or "")
        )

    installations = [
        {
            "installation_id": str(row["install_id"]),
            "version": str(row["version"] or ""),
            "models": sorted(set(models_by_installation.get(str(row["install_id"]), []))),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }
        for row in installation_rows
    ]
    models = [
        {
            "name": name,
            "installation_count": len(model_installations[name]),
            "last_seen": model_last_seen.get(name),
        }
        for name in sorted(
            model_installations,
            key=lambda value: (-len(model_installations[value]), value.casefold()),
        )
    ]
    return {"models": models, "installations": installations}


@router.post(
    "/api/anthbot/admin/announcements", dependencies=[Depends(require_admin)]
)
def save_announcement(payload: AnnouncementWrite) -> dict[str, Any]:
    init_announcement_tables()
    now = _iso()
    announcement_id = payload.announcement_id or f"notice-{secrets.token_hex(6)}"
    published_at = payload.published_at
    if payload.published and published_at is None:
        published_at = _utcnow()
    translations = {
        language: translation.model_dump(exclude_none=True)
        for language, translation in payload.translations.items()
    }
    with _db() as conn:
        existing = conn.execute(
            "SELECT created_at FROM announcements WHERE announcement_id = ?",
            (announcement_id,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """
            INSERT INTO announcements (
                announcement_id, category, priority, show_popup, published,
                published_at, expires_at, min_version, max_version,
                target_models_json, target_installations_json,
                translations_json, link_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(announcement_id) DO UPDATE SET
                category=excluded.category,
                priority=excluded.priority,
                show_popup=excluded.show_popup,
                published=excluded.published,
                published_at=excluded.published_at,
                expires_at=excluded.expires_at,
                min_version=excluded.min_version,
                max_version=excluded.max_version,
                target_models_json=excluded.target_models_json,
                target_installations_json=excluded.target_installations_json,
                translations_json=excluded.translations_json,
                link_url=excluded.link_url,
                updated_at=excluded.updated_at
            """,
            (
                announcement_id,
                payload.category,
                payload.priority,
                int(payload.show_popup),
                int(payload.published),
                _iso(published_at) if published_at else None,
                _iso(payload.expires_at) if payload.expires_at else None,
                payload.min_version,
                payload.max_version,
                json.dumps(payload.target_models, ensure_ascii=False, separators=(",", ":")),
                json.dumps(payload.target_installations, ensure_ascii=False, separators=(",", ":")),
                json.dumps(translations, ensure_ascii=False, separators=(",", ":")),
                payload.link_url,
                created_at,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM announcements WHERE announcement_id = ?",
            (announcement_id,),
        ).fetchone()
    return _row_payload(row)


@router.delete(
    "/api/anthbot/admin/announcements/{announcement_id}",
    dependencies=[Depends(require_admin)],
)
def delete_announcement(announcement_id: str) -> dict[str, Any]:
    normalized = announcement_id.strip().lower()
    if not _ANNOUNCEMENT_ID_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="invalid announcement id")
    init_announcement_tables()
    with _db() as conn:
        cursor = conn.execute(
            "DELETE FROM announcements WHERE announcement_id = ?", (normalized,)
        )
    if cursor.rowcount < 1:
        raise HTTPException(status_code=404, detail="announcement not found")
    return {"deleted": True, "announcement_id": normalized}


@router.get("/dashboard/announcements", response_class=HTMLResponse)
def announcements_dashboard(request: Request):
    if not _request_is_admin(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(_dashboard_file("announcements_dashboard.html"))
