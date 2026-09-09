from __future__ import annotations

from app import app as fastapi_app
from migration_api import router as migration_router

fastapi_app.include_router(migration_router)

from asgi import app  # noqa: E402,F401
