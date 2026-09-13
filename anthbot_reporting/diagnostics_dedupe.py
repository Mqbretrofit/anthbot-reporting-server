"""Persistent semantic deduplication for incoming ANTHBOT diagnostics.

This is a server-side safety net for older integration versions.  The client is
expected to deduplicate reports itself, but a malformed/unstable cloud error
must never be able to flood the reporting database again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any


DEFAULT_DB_PATH = "/data/anthbot_reporting.sqlite3"
MAX_DIAGNOSTICS_BYTES = 2 * 1024 * 1024
DEDUPE_WINDOW = timedelta(hours=1)
DEDUPE_RETENTION = timedelta(days=7)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_REQUEST_ID_RE = re.compile(
    r"<RequestId>[^<]*(?:</RequestId>)?",
    re.IGNORECASE | re.DOTALL,
)
_HOST_ID_RE = re.compile(
    r"<HostId>[^<]*(?:</HostId>)?",
    re.IGNORECASE | re.DOTALL,
)

_ERROR_PATHS: dict[str, tuple[str, ...]] = {
    "map_definition_error": ("definitions", "map", "error"),
    "path_definition_error": ("definitions", "path", "error"),
    "live_shadow_error": ("connection", "live_shadow_error"),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _db_path() -> Path:
    return Path(os.environ.get("ANTHBOT_DB_PATH", DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_cloud_error(value: object) -> str:
    """Remove request-specific cloud metadata from a diagnostic error."""
    text = str(value or "")
    text = _URL_RE.sub("<url>", text)
    text = _REQUEST_ID_RE.sub("<RequestId>", text)
    text = _HOST_ID_RE.sub("<HostId>", text)
    return " ".join(text.split())[:4096]


def _nested_value(value: object, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _device_identity(report: dict[str, Any]) -> str:
    device = report.get("device")
    if isinstance(device, dict):
        for key in ("serial_sha256", "serial_suffix", "robot_id"):
            value = device.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        model = device.get("model")
        if model not in (None, ""):
            return f"model:{model}"
    return "device:unknown"


def semantic_dedupe_key(payload: dict[str, Any]) -> str | None:
    """Return a conservative semantic key for triggers safe to aggregate."""
    installation_id = payload.get("installation_id")
    trigger = payload.get("trigger")
    report = payload.get("report")
    if not isinstance(installation_id, str) or not installation_id:
        return None
    if not isinstance(trigger, str) or not trigger:
        return None
    if not isinstance(report, dict):
        return None

    identity = _device_identity(report)
    semantic: Any = None

    error_path = _ERROR_PATHS.get(trigger)
    if error_path is not None:
        error = _nested_value(report, error_path)
        if error in (None, "", False):
            return None
        semantic = {"error": normalize_cloud_error(error)}
    elif trigger == "mower_error_code":
        error_code = _nested_value(report, ("telemetry", "err_code"))
        if error_code in (None, "", 0, "0", False):
            return None
        semantic = {"err_code": str(error_code)}
    elif trigger == "no_go_path_crossing":
        check = _nested_value(report, ("no_go", "check"))
        if not isinstance(check, dict):
            return None
        semantic = {
            "path_id": check.get("path_id"),
            "zone_ids": sorted(str(item) for item in check.get("zone_ids", []) or []),
        }
    else:
        # Unknown/future report types keep their existing one-request/one-row
        # behavior rather than risking false-positive deduplication.
        return None

    basis = {
        "installation_id": installation_id,
        "trigger": trigger,
        "device": identity,
        "semantic": semantic,
    }
    return hashlib.sha256(_canonical_json(basis).encode("utf-8")).hexdigest()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS diagnostic_dedupe (
            dedupe_key TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            trigger TEXT NOT NULL,
            report_id TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            duplicate_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_diagnostic_dedupe_last_seen
            ON diagnostic_dedupe(last_seen);
        """
    )


def reserve_or_get_duplicate(
    dedupe_key: str,
    *,
    installation_id: str,
    trigger: str,
    now: datetime | None = None,
) -> tuple[bool, str | None, str]:
    """Atomically reserve a key or return an existing recent report.

    Returns ``(is_duplicate, report_id, reservation_time)``.  A duplicate may
    temporarily have no report id when it arrives concurrently with the first
    request; it is still safe to acknowledge because the first request owns the
    persistent reservation.
    """
    current = now or _utcnow()
    current_iso = _iso(current)
    cutoff = current - DEDUPE_WINDOW
    retention_cutoff = _iso(current - DEDUPE_RETENTION)

    conn = _connect()
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM diagnostic_dedupe WHERE last_seen < ?",
            (retention_cutoff,),
        )
        row = conn.execute(
            """
            SELECT report_id, first_seen, last_seen, duplicate_count
            FROM diagnostic_dedupe WHERE dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()
        if row is not None:
            last_seen = _parse_iso(row["last_seen"])
            if last_seen is not None and last_seen >= cutoff:
                conn.execute(
                    """
                    UPDATE diagnostic_dedupe
                    SET last_seen = ?, duplicate_count = duplicate_count + 1
                    WHERE dedupe_key = ?
                    """,
                    (current_iso, dedupe_key),
                )
                conn.commit()
                report_id = row["report_id"]
                return True, str(report_id) if report_id else None, str(row["first_seen"])

        conn.execute(
            """
            INSERT INTO diagnostic_dedupe (
                dedupe_key, installation_id, trigger, report_id,
                first_seen, last_seen, duplicate_count
            ) VALUES (?, ?, ?, NULL, ?, ?, 0)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                installation_id=excluded.installation_id,
                trigger=excluded.trigger,
                report_id=NULL,
                first_seen=excluded.first_seen,
                last_seen=excluded.last_seen,
                duplicate_count=0
            """,
            (dedupe_key, installation_id, trigger, current_iso, current_iso),
        )
        conn.commit()
        return False, None, current_iso
    finally:
        conn.close()


def finalize_reservation(
    dedupe_key: str,
    *,
    reservation_time: str,
    installation_id: str,
    trigger: str,
    report_sha256: str,
    success: bool,
) -> None:
    """Attach the inserted report id, or release a failed reservation."""
    conn = _connect()
    try:
        _ensure_table(conn)
        if not success:
            conn.execute(
                """
                DELETE FROM diagnostic_dedupe
                WHERE dedupe_key = ? AND first_seen = ? AND report_id IS NULL
                """,
                (dedupe_key, reservation_time),
            )
            conn.commit()
            return

        row = conn.execute(
            """
            SELECT report_id FROM diagnostics
            WHERE installation_id = ? AND trigger = ? AND report_sha256 = ?
            ORDER BY received_at DESC LIMIT 1
            """,
            (installation_id, trigger, report_sha256),
        ).fetchone()
        if row is not None:
            conn.execute(
                """
                UPDATE diagnostic_dedupe SET report_id = ?
                WHERE dedupe_key = ? AND first_seen = ?
                """,
                (row["report_id"], dedupe_key, reservation_time),
            )
        conn.commit()
    finally:
        conn.close()


def _content_length(scope: dict[str, Any]) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return None
    return None


class DiagnosticsDedupeMiddleware:
    """ASGI middleware that deduplicates known diagnostic episodes server-side."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() == "POST"
            and str(scope.get("path", "")) == "/api/anthbot/diagnostics"
        ):
            await self.app(scope, receive, send)
            return

        length = _content_length(scope)
        if length is not None and length > MAX_DIAGNOSTICS_BYTES:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") != "http.request":
                await self.app(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            more_body = bool(message.get("more_body", False))
        body = b"".join(chunks)

        replayed = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self.app(scope, replay_receive, send)
            return
        if not isinstance(payload, dict):
            await self.app(scope, replay_receive, send)
            return

        dedupe_key = semantic_dedupe_key(payload)
        if dedupe_key is None:
            await self.app(scope, replay_receive, send)
            return

        installation_id = str(payload.get("installation_id") or "")
        trigger = str(payload.get("trigger") or "")
        is_duplicate, report_id, reservation_time = reserve_or_get_duplicate(
            dedupe_key,
            installation_id=installation_id,
            trigger=trigger,
        )
        if is_duplicate:
            response: dict[str, Any] = {"accepted": True, "deduplicated": True}
            if report_id:
                response["report_id"] = report_id
            raw = json.dumps(response, separators=(",", ":")).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(raw)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": raw})
            return

        report = payload.get("report")
        report_json = _canonical_json(report) if isinstance(report, dict) else ""
        report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        response_status = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_status
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
        finally:
            finalize_reservation(
                dedupe_key,
                reservation_time=reservation_time,
                installation_id=installation_id,
                trigger=trigger,
                report_sha256=report_sha256,
                success=200 <= response_status < 300,
            )


__all__ = [
    "DiagnosticsDedupeMiddleware",
    "normalize_cloud_error",
    "semantic_dedupe_key",
]
