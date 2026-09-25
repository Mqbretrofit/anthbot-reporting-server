"""Minimal ANTHBOT Map presence heartbeat and admin statistics."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import require_admin

router = APIRouter()
DEFAULT_DB_PATH = "/data/anthbot_reporting.sqlite3"


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(os.environ.get("ANTHBOT_DB_PATH", DEFAULT_DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_presence_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS installation_presence (
            install_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_installation_presence_last_seen ON installation_presence(last_seen);
        CREATE TABLE IF NOT EXISTS installation_presence_models (
            install_id TEXT NOT NULL,
            model TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (install_id, model)
        );
        CREATE INDEX IF NOT EXISTS idx_presence_models_last_seen ON installation_presence_models(last_seen);
        """)
        conn.commit()
    finally:
        conn.close()


class PresencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    install_id: UUID
    version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)

    @field_validator("version", "model")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


@router.post("/api/anthbot/presence")
def presence_heartbeat(payload: PresencePayload) -> dict[str, bool]:
    init_presence_tables()
    now = _now()
    install_id = str(payload.install_id)
    conn = _connect()
    try:
        conn.execute("""INSERT INTO installation_presence (install_id, version, first_seen, last_seen)
            VALUES (?, ?, ?, ?) ON CONFLICT(install_id) DO UPDATE SET
            version=excluded.version, last_seen=excluded.last_seen""", (install_id, payload.version, now, now))
        conn.execute("""INSERT INTO installation_presence_models (install_id, model, first_seen, last_seen)
            VALUES (?, ?, ?, ?) ON CONFLICT(install_id, model) DO UPDATE SET last_seen=excluded.last_seen""", (install_id, payload.model, now, now))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


_PRESENCE_FALLBACK_MIN_VERSION = (2, 4, 9, 2)
_PRESENCE_RECONCILE_WINDOW = timedelta(minutes=2)


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    """Return the leading numeric version components used for feature gating."""
    if not isinstance(value, str):
        return None
    parts: list[int] = []
    for chunk in value.strip().split("."):
        match = __import__("re").match(r"^(\\d+)", chunk)
        if match is None:
            break
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else None


def _supports_presence_fallback(version: str | None) -> bool:
    parsed = _version_tuple(version)
    if parsed is None:
        return False
    padded = parsed + (0,) * max(0, len(_PRESENCE_FALLBACK_MIN_VERSION) - len(parsed))
    return padded[: len(_PRESENCE_FALLBACK_MIN_VERSION)] >= _PRESENCE_FALLBACK_MIN_VERSION


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _telemetry_models(raw: object) -> set[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    return {
        str(model).strip()
        for model, count in parsed.items()
        if isinstance(model, str)
        and model.strip()
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    }


def _presence_stats_data() -> dict:
    """Return presence rows reconciled with same-server 2.4.9.2+ telemetry.

    ANTHBOT Map 2.4.9.2 sends the minimal presence heartbeat independently from
    the opt-in usage heartbeat, and the two payloads intentionally use separate
    random installation IDs. If a minimal heartbeat is missed but the reporting
    server did receive the opt-in heartbeat from the same integration load, keep
    the Presence dashboard complete by using that telemetry row as a fallback.

    To avoid double counting, a telemetry row is collapsed into a presence row
    only when version + model set match and there is exactly one presence
    heartbeat within a tight two-minute window. Older integration versions are
    never synthesized because they did not implement the minimal heartbeat.
    """
    init_presence_tables()
    now = _now_dt()
    cut24 = now - timedelta(hours=24)
    cut7 = now - timedelta(days=7)
    cut30 = now - timedelta(days=30)

    conn = _connect()
    try:
        presence_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT install_id, version, first_seen, last_seen "
                "FROM installation_presence"
            ).fetchall()
        ]
        presence_model_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT install_id, model FROM installation_presence_models"
            ).fetchall()
        ]
        telemetry_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT installation_id, integration_version, first_seen, last_seen,
                       model_counts_json
                FROM installations
                WHERE integration_version IS NOT NULL
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    models_by_presence: dict[str, set[str]] = defaultdict(set)
    for row in presence_model_rows:
        install_id = str(row.get("install_id") or "")
        model = str(row.get("model") or "").strip()
        if install_id and model:
            models_by_presence[install_id].add(model)

    merged: list[dict] = []
    by_id: dict[str, dict] = {}
    for row in presence_rows:
        install_id = str(row.get("install_id") or "")
        if not install_id:
            continue
        item = {
            "install_id": install_id,
            "version": str(row.get("version") or ""),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "_models": set(models_by_presence.get(install_id, set())),
            "_source": "presence",
        }
        merged.append(item)
        by_id[install_id] = item

    matched_presence_ids: set[str] = set()
    for row in telemetry_rows:
        version = str(row.get("integration_version") or "").strip()
        if not _supports_presence_fallback(version):
            continue

        telemetry_id = str(row.get("installation_id") or "")
        telemetry_models = _telemetry_models(row.get("model_counts_json"))
        if not telemetry_id or not telemetry_models:
            continue

        telemetry_last = _parse_timestamp(row.get("last_seen"))
        exact = by_id.get(telemetry_id)
        if exact is not None:
            exact["_models"].update(telemetry_models)
            exact["first_seen"] = min(
                value for value in (exact.get("first_seen"), row.get("first_seen")) if value
            )
            exact["last_seen"] = max(
                value for value in (exact.get("last_seen"), row.get("last_seen")) if value
            )
            matched_presence_ids.add(telemetry_id)
            continue

        candidates: list[dict] = []
        if telemetry_last is not None:
            for item in merged:
                if item["_source"] != "presence":
                    continue
                if item["install_id"] in matched_presence_ids:
                    continue
                if item["version"] != version or item["_models"] != telemetry_models:
                    continue
                presence_last = _parse_timestamp(item.get("last_seen"))
                if presence_last is None:
                    continue
                if abs(presence_last - telemetry_last) <= _PRESENCE_RECONCILE_WINDOW:
                    candidates.append(item)

        if len(candidates) == 1:
            candidate = candidates[0]
            matched_presence_ids.add(candidate["install_id"])
            candidate["first_seen"] = min(
                value
                for value in (candidate.get("first_seen"), row.get("first_seen"))
                if value
            )
            candidate["last_seen"] = max(
                value
                for value in (candidate.get("last_seen"), row.get("last_seen"))
                if value
            )
            continue

        # No unambiguous same-load presence heartbeat exists. The server did
        # still receive this 2.4.9.2+ installation through its telemetry API,
        # so expose it as a fallback instead of silently dropping it.
        fallback = {
            "install_id": telemetry_id,
            "version": version,
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "_models": telemetry_models,
            "_source": "telemetry_fallback",
        }
        merged.append(fallback)
        by_id[telemetry_id] = fallback

    merged.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)

    version_counts = Counter(
        str(item.get("version") or "Unknown") for item in merged
    )
    model_counts = Counter()
    for item in merged:
        for model in item["_models"]:
            model_counts[model] += 1

    def _is_active(item: dict, cutoff: datetime) -> bool:
        last_seen = _parse_timestamp(item.get("last_seen"))
        return last_seen is not None and last_seen >= cutoff

    items = [
        {
            "install_id": item["install_id"],
            "version": item["version"],
            "first_seen": item["first_seen"],
            "last_seen": item["last_seen"],
            "models": " | ".join(sorted(item["_models"])) or None,
        }
        for item in merged[:500]
    ]

    return {
        "generated_at": now.isoformat(),
        "total": len(merged),
        "active_24h": sum(1 for item in merged if _is_active(item, cut24)),
        "active_7d": sum(1 for item in merged if _is_active(item, cut7)),
        "active_30d": sum(1 for item in merged if _is_active(item, cut30)),
        "by_version": [
            {"name": name, "count": count}
            for name, count in sorted(
                version_counts.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        "by_model": [
            {"name": name, "count": count}
            for name, count in sorted(
                model_counts.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        "items": items,
    }


@router.get("/api/anthbot/admin/presence-stats", dependencies=[Depends(require_admin)])
def presence_stats() -> dict:
    return _presence_stats_data()


def _robot_identity(report: dict) -> str | None:
    device = report.get("device") if isinstance(report.get("device"), dict) else {}
    value = device.get("serial_sha256") or device.get("serial_suffix")
    return str(value) if value else None


def _robot_model(report: dict) -> str:
    device = report.get("device") if isinstance(report.get("device"), dict) else {}
    model = device.get("model")
    return str(model).strip() if model else "Ismeretlen"


def _error_stats_data() -> dict:
    """Aggregate existing opt-in diagnostics; this enables no new collection."""
    now = _now_dt()
    cut24 = now - timedelta(hours=24)
    cut7 = now - timedelta(days=7)
    cut30 = now - timedelta(days=30)
    conn = _connect()
    try:
        rows = conn.execute("SELECT installation_id, trigger, received_at, report_json FROM diagnostics ORDER BY received_at DESC").fetchall()
    finally:
        conn.close()

    by_model = Counter(); by_error = Counter(); by_cloud = Counter(); by_status = Counter()
    installations: set[str] = set(); robots: set[str] = set()
    model_error = defaultdict(lambda: {"count": 0, "installations": set(), "robots": set(), "description": None, "message": None, "last_seen": None})
    total_errors = cloud_events = e24 = e7 = e30 = 0
    for row in rows:
        try: report = json.loads(row["report_json"])
        except (TypeError, ValueError): continue
        if not isinstance(report, dict): continue
        model = _robot_model(report); robot_id = _robot_identity(report); installation = str(row["installation_id"] or "")
        try:
            received = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
            if received.tzinfo is None: received = received.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError): received = None
        event = report.get("diagnostic_event") if isinstance(report.get("diagnostic_event"), dict) else None
        cloud = report.get("cloud_api_error") if isinstance(report.get("cloud_api_error"), dict) else None
        if cloud:
            cloud_events += 1; by_cloud[str(cloud.get("category") or cloud.get("operation") or "unknown")] += 1
        if not event: continue
        total_errors += 1; installations.add(installation)
        if robot_id: robots.add(robot_id)
        by_model[model] += 1
        status = event.get("robot_sta") or event.get("mode")
        if status is not None: by_status[str(status)] += 1
        code = event.get("err_code"); task_event = event.get("task_event") if isinstance(event.get("task_event"), dict) else {}
        if code is None: code = task_event.get("code") or event.get("cloud_task_event_code") or event.get("event_code")
        code_name = str(code) if code is not None else "n/a"; description = event.get("err_description")
        message = task_event.get("event_message") or task_event.get("message") or task_event.get("description")
        by_error[f"{code_name} · {description or message or 'hiba'}"] += 1
        item = model_error[(model, code_name)]; item["count"] += 1; item["installations"].add(installation)
        if robot_id: item["robots"].add(robot_id)
        item["description"] = item["description"] or description; item["message"] = item["message"] or message
        if row["received_at"] and (item["last_seen"] is None or str(row["received_at"]) > item["last_seen"]): item["last_seen"] = str(row["received_at"])
        if received:
            if received >= cut24: e24 += 1
            if received >= cut7: e7 += 1
            if received >= cut30: e30 += 1
    detail = []
    for (model, code), item in model_error.items():
        detail.append({"model": model, "error_code": code, "description": item["description"], "message": item["message"], "count": item["count"], "affected_installations": len(item["installations"]), "affected_robots": len(item["robots"]), "last_seen": item["last_seen"]})
    detail.sort(key=lambda x: (-x["count"], x["model"], x["error_code"]))
    named = lambda counter: [{"name": k, "count": v} for k, v in counter.most_common()]
    return {"generated_at": now.isoformat(), "total_error_events": total_errors, "affected_installations": len(installations), "affected_robots": len(robots), "models_with_errors": len(by_model), "cloud_api_events": cloud_events, "error_events_24h": e24, "error_events_7d": e7, "error_events_30d": e30, "by_model": named(by_model), "by_error": named(by_error), "by_cloud_category": named(by_cloud), "by_robot_status": named(by_status), "model_errors": detail[:500]}


@router.get("/api/anthbot/admin/error-stats", dependencies=[Depends(require_admin)])
def error_stats() -> dict:
    return _error_stats_data()


@router.get("/api/anthbot/admin/analysis-export.json", dependencies=[Depends(require_admin)])
def analysis_export() -> Response:
    """Download one analysis-ready JSON snapshot of the anonymous statistics."""
    generated = _now_dt()
    payload = {
        "schema": "anthbot-reporting-analysis-export-v1",
        "generated_at": generated.isoformat(),
        "presence": _presence_stats_data(),
        "error_statistics": _error_stats_data(),
    }
    filename = f"anthbot-reporting-analysis-{generated.strftime('%Y%m%d-%H%M%S')}.json"
    return Response(content=json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/dashboard/presence", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def presence_dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(Path(__file__).with_name("presence_dashboard.html").read_text(encoding="utf-8"))


@router.get("/dashboard/error-stats", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def error_stats_dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(Path(__file__).with_name("error_stats_dashboard.html").read_text(encoding="utf-8"))
