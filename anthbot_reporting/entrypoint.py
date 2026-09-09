from __future__ import annotations

from app import app as fastapi_app
from migration_api import router as migration_router

fastapi_app.include_router(migration_router)

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


app = MigrationUploadMiddleware(dashboard_app)
