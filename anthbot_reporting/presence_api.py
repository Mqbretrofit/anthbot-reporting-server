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


def _presence_stats_data() -> dict:
    init_presence_tables()
    now = _now_dt()
    cut24 = (now - timedelta(hours=24)).isoformat()
    cut7 = (now - timedelta(days=7)).isoformat()
    cut30 = (now - timedelta(days=30)).isoformat()
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM installation_presence").fetchone()[0]
        a24 = conn.execute("SELECT COUNT(*) FROM installation_presence WHERE last_seen>=?", (cut24,)).fetchone()[0]
        a7 = conn.execute("SELECT COUNT(*) FROM installation_presence WHERE last_seen>=?", (cut7,)).fetchone()[0]
        a30 = conn.execute("SELECT COUNT(*) FROM installation_presence WHERE last_seen>=?", (cut30,)).fetchone()[0]
        versions = [dict(r) for r in conn.execute("SELECT version name, COUNT(*) count FROM installation_presence GROUP BY version ORDER BY count DESC, name").fetchall()]
        models = [dict(r) for r in conn.execute("SELECT model name, COUNT(DISTINCT install_id) count FROM installation_presence_models GROUP BY model ORDER BY count DESC, name").fetchall()]
        items = [dict(r) for r in conn.execute("""SELECT p.install_id,p.version,p.first_seen,p.last_seen,GROUP_CONCAT(m.model,' | ') models
            FROM installation_presence p LEFT JOIN installation_presence_models m ON m.install_id=p.install_id
            GROUP BY p.install_id,p.version,p.first_seen,p.last_seen ORDER BY p.last_seen DESC LIMIT 500""").fetchall()]
    finally:
        conn.close()
    return {"generated_at": now.isoformat(), "total": total, "active_24h": a24, "active_7d": a7,
            "active_30d": a30, "by_version": versions, "by_model": models, "items": items}


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
