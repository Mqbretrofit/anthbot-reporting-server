"""Minimal ANTHBOT Map installation presence heartbeat and admin statistics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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
            version=excluded.version, last_seen=excluded.last_seen""",
            (install_id, payload.version, now, now))
        conn.execute("""INSERT INTO installation_presence_models (install_id, model, first_seen, last_seen)
            VALUES (?, ?, ?, ?) ON CONFLICT(install_id, model) DO UPDATE SET last_seen=excluded.last_seen""",
            (install_id, payload.model, now, now))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/anthbot/admin/presence-stats", dependencies=[Depends(require_admin)])
def presence_stats() -> dict:
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


@router.get("/dashboard/presence", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def presence_dashboard(request: Request) -> HTMLResponse:
    path = Path(__file__).with_name("presence_dashboard.html")
    return HTMLResponse(path.read_text(encoding="utf-8"))
