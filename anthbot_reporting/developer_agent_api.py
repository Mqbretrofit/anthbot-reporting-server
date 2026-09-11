from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter()

DEFAULT_DB_PATH = "/data/anthbot_reporting.sqlite3"
_DASHBOARD_COOKIE = "anthbot_admin_session"
_AGENT_SCHEMA_POLL = "anthbot-developer-agent-poll-v1"
_AGENT_SCHEMA_RESULT = "anthbot-developer-agent-result-v1"
_AGENT_REQUEUE_MINUTES = 30
_AGENT_ACTIVE_POLL_SECONDS = 120
_AGENT_DISABLED_POLL_SECONDS = 21_600
_MAX_ASSEMBLED_RESULT_BYTES = 64 * 1024 * 1024
_MAX_JOB_PARAMS_BYTES = 64 * 1024

LEGACY_PROBES = frozenset(
    {
        "state_schema",
        "firmware_diagnostics",
        "area_definition",
        "ridable_area_definition",
        "map_definition",
        "map_archive",
        "path_definition",
        "task_events",
        "refresh_properties",
    }
)

ALLOWED_PROBES = frozenset(
    set(LEGACY_PROBES)
    | {
        "full_state",
        "full_diagnostics",
        "state_inspector",
        "state_diff",
        "refresh_diagnostics",
    }
)

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
    "agent_key",
    "api_key",
    "private_key",
    "serial_number",
    "username",
    "email",
    "url",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    result = value or _utcnow()
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc).isoformat()


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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_developer_agent_tables() -> None:
    """Create/migrate developer-agent tables without touching reporting data."""
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS developer_agent_installations (
                installation_id TEXT PRIMARY KEY,
                agent_key_sha256 TEXT NOT NULL,
                server_enabled INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                integration_version TEXT,
                models_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS developer_agent_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                installation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_model TEXT,
                params_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT,
                result_json TEXT,
                error TEXT,
                FOREIGN KEY(installation_id)
                    REFERENCES developer_agent_installations(installation_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_developer_agent_jobs_installation_status
                ON developer_agent_jobs(installation_id, status, created_at);

            CREATE TABLE IF NOT EXISTS developer_agent_result_chunks (
                job_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                chunk TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(job_id, chunk_index),
                FOREIGN KEY(job_id)
                    REFERENCES developer_agent_jobs(job_id)
                    ON DELETE CASCADE
            );
            """
        )
        _ensure_column(
            conn,
            "developer_agent_installations",
            "capabilities_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        _ensure_column(
            conn,
            "developer_agent_jobs",
            "params_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )


def _agent_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contains_sensitive_key(value: Any, *, depth: int = 0) -> bool:
    if depth > 64:
        return True
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if any(part in key for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_key(child, depth=depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_key(child, depth=depth + 1) for child in value)
    return False


def _safe_json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decode_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return _safe_json_object(decoded)


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


def _request_is_admin(request: Request, authorization: str | None) -> bool:
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


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    probe_actions: list[str] = Field(default_factory=list, max_length=200)
    job_params: bool = False
    state_inspector_paths: bool = False
    full_state: bool = False
    state_diff: bool = False

    @field_validator("probe_actions")
    @classmethod
    def _validate_probe_actions(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if normalized and len(normalized) <= 64:
                result.append(normalized)
        return sorted(set(result))


class AgentPoll(BaseModel):
    # Ignore unknown capability-era fields so a future client can add metadata
    # without breaking older reporting-server deployments.
    model_config = ConfigDict(extra="ignore")

    schema_name: Literal[_AGENT_SCHEMA_POLL] = Field(alias="schema")
    installation_id: UUID
    agent_key: str = Field(min_length=32, max_length=256)
    integration_version: str | None = Field(default=None, max_length=64)
    models: list[str] = Field(default_factory=list, max_length=100)
    capabilities: AgentCapabilities | None = None

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if not normalized or len(normalized) > 128:
                raise ValueError("model names must be 1..128 characters")
            result.append(normalized)
        return sorted(set(result))


class AgentResultChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal[_AGENT_SCHEMA_RESULT] = Field(alias="schema")
    installation_id: UUID
    agent_key: str = Field(min_length=32, max_length=256)
    job_id: int = Field(ge=1)
    chunk_index: int = Field(ge=0, le=4095)
    chunk_count: int = Field(ge=1, le=4096)
    chunk: str = Field(max_length=32_000)


class AgentEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AgentJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    action: str = Field(min_length=1, max_length=64)
    target_model: str | None = Field(default=None, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        if value not in ALLOWED_PROBES:
            raise ValueError("action is not in the server whitelist")
        return value

    @field_validator("params")
    @classmethod
    def _validate_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _contains_sensitive_key(value):
            raise ValueError("params contain a credential-like field name")
        try:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as err:
            raise ValueError("params must be JSON serializable") from err
        if len(raw.encode("utf-8")) > _MAX_JOB_PARAMS_BYTES:
            raise ValueError("params are too large")
        return value


def _authenticate_agent(
    conn: sqlite3.Connection,
    installation_id: str,
    agent_key: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM developer_agent_installations WHERE installation_id = ?",
        (installation_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="developer agent is not registered")
    supplied_hash = _agent_key_hash(agent_key)
    if not secrets.compare_digest(supplied_hash, row["agent_key_sha256"]):
        raise HTTPException(status_code=401, detail="invalid developer agent key")
    return row


def _capabilities_payload(payload: AgentPoll) -> dict[str, Any]:
    if payload.capabilities is None:
        return {}
    return payload.capabilities.model_dump(mode="json")


def _register_or_authenticate_poll(
    conn: sqlite3.Connection,
    payload: AgentPoll,
) -> sqlite3.Row:
    installation_id = str(payload.installation_id)
    now = _iso()
    models_json = json.dumps(payload.models, ensure_ascii=False, separators=(",", ":"))
    capabilities_json = json.dumps(
        _capabilities_payload(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    row = conn.execute(
        "SELECT * FROM developer_agent_installations WHERE installation_id = ?",
        (installation_id,),
    ).fetchone()
    supplied_hash = _agent_key_hash(payload.agent_key)
    if row is None:
        conn.execute(
            """
            INSERT INTO developer_agent_installations (
                installation_id, agent_key_sha256, server_enabled,
                first_seen, last_seen, integration_version, models_json,
                capabilities_json
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                installation_id,
                supplied_hash,
                now,
                now,
                payload.integration_version,
                models_json,
                capabilities_json,
            ),
        )
    else:
        if not secrets.compare_digest(supplied_hash, row["agent_key_sha256"]):
            raise HTTPException(status_code=401, detail="invalid developer agent key")
        conn.execute(
            """
            UPDATE developer_agent_installations
            SET last_seen = ?, integration_version = ?, models_json = ?,
                capabilities_json = ?
            WHERE installation_id = ?
            """,
            (
                now,
                payload.integration_version,
                models_json,
                capabilities_json,
                installation_id,
            ),
        )
    return conn.execute(
        "SELECT * FROM developer_agent_installations WHERE installation_id = ?",
        (installation_id,),
    ).fetchone()


def _client_allowed_actions(installation: sqlite3.Row) -> set[str]:
    capabilities = _decode_json_object(installation["capabilities_json"])
    advertised = capabilities.get("probe_actions")
    if isinstance(advertised, list):
        return {
            str(item)
            for item in advertised
            if isinstance(item, str) and item in ALLOWED_PROBES
        }
    return set(LEGACY_PROBES)


def _client_supports_params(installation: sqlite3.Row) -> bool:
    capabilities = _decode_json_object(installation["capabilities_json"])
    return bool(capabilities.get("job_params"))


@router.post("/api/anthbot/developer-agent/poll")
def developer_agent_poll(payload: AgentPoll) -> dict[str, Any]:
    installation_id = str(payload.installation_id)
    with _db() as conn:
        installation = _register_or_authenticate_poll(conn, payload)
        server_enabled = bool(installation["server_enabled"])
        if not server_enabled:
            return {
                "server_enabled": False,
                "next_poll_seconds": _AGENT_DISABLED_POLL_SECONDS,
                "job": None,
            }

        # If a client disappeared after claiming a job, make it available again.
        stale_before = _iso(_utcnow() - timedelta(minutes=_AGENT_REQUEUE_MINUTES))
        conn.execute(
            """
            UPDATE developer_agent_jobs
            SET status = 'queued', claimed_at = NULL
            WHERE installation_id = ? AND status = 'running'
              AND claimed_at IS NOT NULL AND claimed_at < ?
            """,
            (installation_id, stale_before),
        )

        job = conn.execute(
            """
            SELECT job_id, action, target_model, params_json
            FROM developer_agent_jobs
            WHERE installation_id = ? AND status = 'queued'
            ORDER BY created_at ASC, job_id ASC
            LIMIT 1
            """,
            (installation_id,),
        ).fetchone()
        if job is None:
            return {
                "server_enabled": True,
                "next_poll_seconds": _AGENT_ACTIVE_POLL_SECONDS,
                "job": None,
            }

        allowed_actions = _client_allowed_actions(installation)
        if job["action"] not in allowed_actions:
            conn.execute(
                """
                UPDATE developer_agent_jobs
                SET status='failed', completed_at=?, error=?
                WHERE job_id=?
                """,
                (
                    _iso(),
                    "installed client does not advertise this diagnostic probe",
                    job["job_id"],
                ),
            )
            return {
                "server_enabled": True,
                "next_poll_seconds": 5,
                "job": None,
            }

        params = _decode_json_object(job["params_json"])
        if params and not _client_supports_params(installation):
            conn.execute(
                """
                UPDATE developer_agent_jobs
                SET status='failed', completed_at=?, error=?
                WHERE job_id=?
                """,
                (
                    _iso(),
                    "installed client does not support diagnostic job parameters",
                    job["job_id"],
                ),
            )
            return {
                "server_enabled": True,
                "next_poll_seconds": 5,
                "job": None,
            }

        claimed_at = _iso()
        conn.execute(
            """
            UPDATE developer_agent_jobs
            SET status = 'running', claimed_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (claimed_at, job["job_id"]),
        )
        return {
            "server_enabled": True,
            "next_poll_seconds": 5,
            "job": {
                "job_id": job["job_id"],
                "action": job["action"],
                "target_model": job["target_model"],
                "params": params,
            },
        }


@router.post("/api/anthbot/developer-agent/result", status_code=202)
def developer_agent_result(payload: AgentResultChunk) -> dict[str, Any]:
    installation_id = str(payload.installation_id)
    with _db() as conn:
        _authenticate_agent(conn, installation_id, payload.agent_key)
        job = conn.execute(
            """
            SELECT job_id, installation_id, status
            FROM developer_agent_jobs WHERE job_id = ?
            """,
            (payload.job_id,),
        ).fetchone()
        if job is None or job["installation_id"] != installation_id:
            raise HTTPException(status_code=404, detail="developer-agent job not found")
        if job["status"] in {"completed", "failed"}:
            return {"accepted": True, "completed": True, "job_id": payload.job_id}

        conn.execute(
            """
            INSERT INTO developer_agent_result_chunks (
                job_id, chunk_index, chunk_count, chunk, received_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id, chunk_index) DO UPDATE SET
                chunk_count=excluded.chunk_count,
                chunk=excluded.chunk,
                received_at=excluded.received_at
            """,
            (
                payload.job_id,
                payload.chunk_index,
                payload.chunk_count,
                payload.chunk,
                _iso(),
            ),
        )
        rows = conn.execute(
            """
            SELECT chunk_index, chunk_count, chunk
            FROM developer_agent_result_chunks
            WHERE job_id = ? ORDER BY chunk_index ASC
            """,
            (payload.job_id,),
        ).fetchall()
        if len(rows) < payload.chunk_count:
            return {
                "accepted": True,
                "completed": False,
                "job_id": payload.job_id,
                "received_chunks": len(rows),
            }
        if any(row["chunk_count"] != payload.chunk_count for row in rows):
            raise HTTPException(status_code=409, detail="result chunk-count mismatch")
        expected_indices = list(range(payload.chunk_count))
        if [row["chunk_index"] for row in rows] != expected_indices:
            return {
                "accepted": True,
                "completed": False,
                "job_id": payload.job_id,
                "received_chunks": len(rows),
            }

        assembled = "".join(row["chunk"] for row in rows)
        if len(assembled.encode("utf-8")) > _MAX_ASSEMBLED_RESULT_BYTES:
            conn.execute(
                "UPDATE developer_agent_jobs SET status='failed', completed_at=?, error=? WHERE job_id=?",
                (_iso(), "assembled result exceeds server limit", payload.job_id),
            )
            conn.execute(
                "DELETE FROM developer_agent_result_chunks WHERE job_id = ?",
                (payload.job_id,),
            )
            raise HTTPException(status_code=413, detail="assembled result is too large")
        try:
            result = json.loads(assembled)
        except json.JSONDecodeError as err:
            conn.execute(
                "UPDATE developer_agent_jobs SET status='failed', completed_at=?, error=? WHERE job_id=?",
                (_iso(), f"invalid result JSON: {err}", payload.job_id),
            )
            raise HTTPException(status_code=422, detail="invalid assembled result JSON") from err
        if _contains_sensitive_key(result):
            conn.execute(
                "UPDATE developer_agent_jobs SET status='failed', completed_at=?, error=? WHERE job_id=?",
                (_iso(), "result contains a credential-like field name", payload.job_id),
            )
            conn.execute(
                "DELETE FROM developer_agent_result_chunks WHERE job_id = ?",
                (payload.job_id,),
            )
            raise HTTPException(status_code=422, detail="result contains a credential-like field name")

        canonical = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result_status = result.get("status") if isinstance(result, dict) else None
        final_status = "completed" if result_status == "ok" else "failed"
        error = None
        if final_status == "failed" and isinstance(result, dict):
            error = str(result.get("error") or result_status or "probe failed")[:1000]
        conn.execute(
            """
            UPDATE developer_agent_jobs
            SET status=?, completed_at=?, result_json=?, error=?
            WHERE job_id=?
            """,
            (final_status, _iso(), canonical, error, payload.job_id),
        )
        conn.execute(
            "DELETE FROM developer_agent_result_chunks WHERE job_id = ?",
            (payload.job_id,),
        )

    return {"accepted": True, "completed": True, "job_id": payload.job_id}


@router.get(
    "/api/anthbot/admin/developer-agent/installations",
    dependencies=[Depends(require_admin)],
)
def admin_agent_installations() -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT installation_id, server_enabled, first_seen, last_seen,
                   integration_version, models_json, capabilities_json
            FROM developer_agent_installations
            ORDER BY last_seen DESC
            """
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            models = json.loads(row["models_json"])
        except (TypeError, ValueError):
            models = []
        capabilities = _decode_json_object(row["capabilities_json"])
        items.append(
            {
                "installation_id": row["installation_id"],
                "server_enabled": bool(row["server_enabled"]),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "integration_version": row["integration_version"],
                "models": models if isinstance(models, list) else [],
                "capabilities": capabilities,
                "legacy_client": not bool(capabilities),
            }
        )
    return {
        "items": items,
        "allowed_probes": sorted(ALLOWED_PROBES),
        "legacy_probes": sorted(LEGACY_PROBES),
    }


@router.post(
    "/api/anthbot/admin/developer-agent/installations/{installation_id}/enabled",
    dependencies=[Depends(require_admin)],
)
def admin_agent_set_enabled(
    installation_id: UUID,
    payload: AgentEnabledUpdate,
) -> dict[str, Any]:
    value = str(installation_id)
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE developer_agent_installations SET server_enabled = ? WHERE installation_id = ?",
            (1 if payload.enabled else 0, value),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="developer agent installation not found")
    return {"installation_id": value, "server_enabled": payload.enabled}


@router.post(
    "/api/anthbot/admin/developer-agent/jobs",
    dependencies=[Depends(require_admin)],
    status_code=201,
)
def admin_agent_create_job(payload: AgentJobCreate) -> dict[str, Any]:
    installation_id = str(payload.installation_id)
    target_model = (
        payload.target_model.strip()
        if isinstance(payload.target_model, str) and payload.target_model.strip()
        else None
    )
    params_json = json.dumps(
        payload.params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with _db() as conn:
        installation = conn.execute(
            "SELECT * FROM developer_agent_installations WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
        if installation is None:
            raise HTTPException(status_code=404, detail="developer agent installation not found")

        allowed_actions = _client_allowed_actions(installation)
        if payload.action not in allowed_actions:
            raise HTTPException(
                status_code=409,
                detail="installed client does not advertise this diagnostic probe",
            )
        if payload.params and not _client_supports_params(installation):
            raise HTTPException(
                status_code=409,
                detail="installed client does not support diagnostic job parameters",
            )

        cursor = conn.execute(
            """
            INSERT INTO developer_agent_jobs (
                installation_id, action, target_model, params_json,
                status, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (
                installation_id,
                payload.action,
                target_model,
                params_json,
                _iso(),
            ),
        )
        job_id = int(cursor.lastrowid)
    return {
        "job_id": job_id,
        "installation_id": installation_id,
        "action": payload.action,
        "target_model": target_model,
        "params": payload.params,
        "status": "queued",
    }


@router.get(
    "/api/anthbot/admin/developer-agent/jobs",
    dependencies=[Depends(require_admin)],
)
def admin_agent_jobs(
    installation_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    include_result: bool = Query(default=False),
) -> dict[str, Any]:
    params: list[Any] = []
    where = ""
    if installation_id is not None:
        where = "WHERE installation_id = ?"
        params.append(str(installation_id))
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, installation_id, action, target_model, params_json,
                   status, created_at, claimed_at, completed_at,
                   result_json, error
            FROM developer_agent_jobs
            {where}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "job_id": row["job_id"],
            "installation_id": row["installation_id"],
            "action": row["action"],
            "target_model": row["target_model"],
            "params": _decode_json_object(row["params_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "claimed_at": row["claimed_at"],
            "completed_at": row["completed_at"],
            "error": row["error"],
        }
        if include_result and row["result_json"]:
            item["result"] = json.loads(row["result_json"])
        items.append(item)
    return {
        "items": items,
        "allowed_probes": sorted(ALLOWED_PROBES),
        "legacy_probes": sorted(LEGACY_PROBES),
    }
