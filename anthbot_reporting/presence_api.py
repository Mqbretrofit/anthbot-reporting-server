"""Minimal ANTHBOT Map installation presence heartbeat.

This endpoint intentionally accepts only three fields: install_id, version and
model. first_seen/last_seen are generated server-side. It is independent from
the existing opt-in developer reports and diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter()
DEFAULT_DB_PATH = "/data/anthbot_reporting.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        CREATE INDEX IF NOT EXISTS idx_installation_presence_last_seen
            ON installation_presence(last_seen);
        CREATE TABLE IF NOT EXISTS installation_presence_models (
            install_id TEXT NOT NULL,
            model TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (install_id, model)
        );
        CREATE INDEX IF NOT EXISTS idx_presence_models_last_seen
            ON installation_presence_models(last_seen);
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
        conn.execute(
            """INSERT INTO installation_presence
               (install_id, version, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(install_id) DO UPDATE SET
                 version=excluded.version,
                 last_seen=excluded.last_seen""",
            (install_id, payload.version, now, now),
        )
        conn.execute(
            """INSERT INTO installation_presence_models
               (install_id, model, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(install_id, model) DO UPDATE SET
                 last_seen=excluded.last_seen""",
            (install_id, payload.model, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
