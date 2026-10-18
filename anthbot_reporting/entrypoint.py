from __future__ import annotations

import logging
import time

from app import app as fastapi_app
from diagnostics_dedupe import DiagnosticsDedupeMiddleware
from migration_api import router as migration_router
from store_api import router as store_router
from custom_voice_admin import router as custom_voice_admin_router
from custom_voice_ai import router as custom_voice_ai_router
from custom_voice_inbox import router as custom_voice_inbox_router
from store_notifications import router as store_notifications_router
from store_accounts import router as store_accounts_router
from web_voice_installer import router as web_voice_installer_router

fastapi_app.include_router(migration_router)
fastapi_app.include_router(store_router)
fastapi_app.include_router(custom_voice_admin_router)
fastapi_app.include_router(custom_voice_ai_router)
fastapi_app.include_router(custom_voice_inbox_router)
fastapi_app.include_router(store_notifications_router)
fastapi_app.include_router(store_accounts_router)
fastapi_app.include_router(web_voice_installer_router)

from asgi import app as dashboard_app  # noqa: E402

_MAX_MIGRATION_BYTES = 16 * 1024 * 1024
_MIGRATION_PREFIX = "/api/anthbot/admin/migration/"


class MigrationUploadMiddleware:
    """Allow larger admin-only migration requests without relaxing public limits."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        is_migration = (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() == "POST"
            and path.startswith(_MIGRATION_PREFIX)
        )
        if not is_migration:
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers", []))
        content_length = None
        filtered_headers = []
        for name, value in headers:
            if name.lower() == b"content-length":
                try:
                    content_length = int(value.decode("ascii"))
                except (ValueError, UnicodeDecodeError):
                    content_length = None
                continue
            filtered_headers.append((name, value))

        if content_length is not None and content_length > _MAX_MIGRATION_BYTES:
            body = (
                '{"detail":"migration request body exceeds '
                + str(_MAX_MIGRATION_BYTES)
                + ' bytes"}'
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        # The core FastAPI middleware intentionally caps normal diagnostics at
        # 2 MiB. Migration is authenticated separately and needs to preserve
        # historical reports that can be larger, so hide Content-Length only
        # for these admin-only endpoints after applying the 16 MiB cap above.
        migrated_scope = {**scope, "headers": filtered_headers}
        await self.app(migrated_scope, receive, send)


_VOICE_DOWNLOAD_LOG = logging.getLogger("uvicorn.error")


class VoiceDownloadTimingMiddleware:
    """Compare factory and paid voice transfer timing without recording secrets.

    ASGI send timings reflect how quickly the application passes bytes to its
    downstream peer (usually a reverse proxy), NOT mower download completion.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        if not (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() in {"GET", "HEAD"}
            and (
                (path.startswith("/api/anthbot/store/voice-packs/") and path.endswith("/download"))
                or path.startswith("/voice-packs/")
            )
        ):
            await self.app(scope, receive, send)
            return

        voice_type = "paid_community" if path.startswith("/api/") else "factory_cached"
        started = time.monotonic()
        status = 0
        sent_bytes = 0
        first_body_ms = None
        range_requested = any(
            name.lower() == b"range" for name, _ in scope.get("headers", [])
        )
        completed = False

        async def measured_send(message):
            nonlocal status, sent_bytes, first_body_ms, completed
            if message["type"] == "http.response.start":
                status = int(message.get("status", 0))
            elif message["type"] == "http.response.body":
                if first_body_ms is None:
                    first_body_ms = round((time.monotonic() - started) * 1000)
                sent_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    completed = True
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            elapsed = max(time.monotonic() - started, 0.001)
            _VOICE_DOWNLOAD_LOG.info(
                "voice_download type=%s status=%s bytes=%s elapsed_ms=%s "
                "first_body_ms=%s throughput_mib_s=%.2f range=%s completed=%s",
                voice_type,
                status,
                sent_bytes,
                round(elapsed * 1000),
                first_body_ms,
                sent_bytes / (1024 * 1024) / elapsed,
                range_requested,
                completed,
            )


# Defense in depth: clients deduplicate automatic diagnostics themselves, but
# production must also protect its persistent store from older/buggy clients.
app = VoiceDownloadTimingMiddleware(
    DiagnosticsDedupeMiddleware(MigrationUploadMiddleware(dashboard_app))
)
