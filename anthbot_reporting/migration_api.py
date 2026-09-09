from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import _canonical_json, _contains_sensitive_key, _db, _iso, require_admin

router = APIRouter()

MIGRATION_SCHEMA = "anthbot-reporting-migration-v1"


class MigrationInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    first_seen: str = Field(min_length=1, max_length=80)
    last_seen: str = Field(min_length=1, max_length=80)
    last_event: str = Field(min_length=1, max_length=64)
    country: str | None = Field(default=None, max_length=128)
    integration_version: str | None = Field(default=None, max_length=64)
    home_assistant_version: str | None = Field(default=None, max_length=64)
    device_count: int = Field(ge=0, le=100)
    model_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("model_counts")
    @classmethod
    def _validate_model_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > 100:
            raise ValueError("too many model count entries")
        normalized: dict[str, int] = {}
        for raw_name, raw_count in value.items():
            name = str(raw_name).strip()
            if not name or len(name) > 128:
                raise ValueError("invalid model name")
            if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0 or raw_count > 100:
                raise ValueError("invalid model count")
            normalized[name] = raw_count
        return normalized


class MigrationDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1, max_length=128)
    installation_id: UUID
    trigger: str = Field(min_length=1, max_length=128)
    generated_at: str = Field(min_length=1, max_length=80)
    received_at: str = Field(min_length=1, max_length=80)
    report_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    report: dict[str, Any]


class MigrationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal[MIGRATION_SCHEMA] = Field(alias="schema")
    installations: list[MigrationInstallation] = Field(default_factory=list, max_length=500)
    diagnostics: list[MigrationDiagnostic] = Field(default_factory=list, max_length=500)


@router.post(
    "/api/anthbot/admin/migration/import",
    dependencies=[Depends(require_admin)],
)
def import_reporting_data(payload: MigrationPayload) -> dict[str, Any]:
    """Import data exported from the previous reporting-server installation.

    This endpoint is intentionally admin-only and idempotent. It preserves the
    original installation timestamps and diagnostic report IDs so links and
    report history remain stable after moving the Home Assistant app to a new
    repository slug.
    """

    prepared_diagnostics: list[tuple[MigrationDiagnostic, str, str]] = []
    for item in payload.diagnostics:
        if _contains_sensitive_key(item.report):
            raise HTTPException(
                status_code=422,
                detail=f"diagnostic {item.report_id} contains a credential-like field name",
            )
        report_json = _canonical_json(item.report)
        calculated_sha = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        if item.report_sha256 and item.report_sha256.lower() != calculated_sha:
            raise HTTPException(
                status_code=422,
                detail=f"diagnostic {item.report_id} SHA-256 mismatch",
            )
        prepared_diagnostics.append((item, report_json, calculated_sha))

    installations_written = 0
    diagnostics_inserted = 0
    diagnostics_existing = 0

    with _db() as conn:
        for item in payload.installations:
            model_counts = dict(sorted(item.model_counts.items()))
            models = sorted(model_counts)
            conn.execute(
                """
                INSERT INTO installations (
                    installation_id, first_seen, last_seen, last_event, country,
                    integration_version, home_assistant_version, device_count,
                    models_json, model_counts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    first_seen=excluded.first_seen,
                    last_seen=excluded.last_seen,
                    last_event=excluded.last_event,
                    country=excluded.country,
                    integration_version=excluded.integration_version,
                    home_assistant_version=excluded.home_assistant_version,
                    device_count=excluded.device_count,
                    models_json=excluded.models_json,
                    model_counts_json=excluded.model_counts_json
                """,
                (
                    str(item.installation_id),
                    item.first_seen,
                    item.last_seen,
                    item.last_event,
                    item.country,
                    item.integration_version,
                    item.home_assistant_version,
                    item.device_count,
                    _canonical_json(models),
                    _canonical_json(model_counts),
                ),
            )
            installations_written += 1

        for item, report_json, calculated_sha in prepared_diagnostics:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO diagnostics (
                    report_id, installation_id, trigger, generated_at,
                    received_at, report_sha256, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.report_id,
                    str(item.installation_id),
                    item.trigger,
                    item.generated_at,
                    item.received_at,
                    calculated_sha,
                    report_json,
                ),
            )
            if cursor.rowcount:
                diagnostics_inserted += 1
            else:
                diagnostics_existing += 1

    return {
        "imported": True,
        "schema": MIGRATION_SCHEMA,
        "installations_written": installations_written,
        "diagnostics_inserted": diagnostics_inserted,
        "diagnostics_existing": diagnostics_existing,
    }
