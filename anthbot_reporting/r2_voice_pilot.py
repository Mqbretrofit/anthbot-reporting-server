"""Optional single-voice Cloudflare R2 delivery pilot.

Only used after the Reporting Server has already validated a user's paid
entitlement. Disabled by default. Other voices retain their original route.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

_LOG = logging.getLogger(__name__)
_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ACCOUNT_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PRESIGNED_TTL = 15 * 60


@dataclass(frozen=True)
class PilotSettings:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    community_id: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def settings_from_env() -> PilotSettings | None:
    if os.environ.get("ANTHBOT_R2_PILOT_ENABLED", "").lower() != "true":
        return None

    config = PilotSettings(
        account_id=os.environ.get("ANTHBOT_R2_ACCOUNT_ID", "").strip(),
        bucket=os.environ.get("ANTHBOT_R2_BUCKET", "").strip(),
        access_key_id=os.environ.get("ANTHBOT_R2_ACCESS_KEY_ID", "").strip(),
        secret_access_key=os.environ.get("ANTHBOT_R2_SECRET_ACCESS_KEY", "").strip(),
        community_id=os.environ.get("ANTHBOT_R2_PILOT_COMMUNITY_ID", "").strip(),
    )
    if not (
        _ACCOUNT_ID.fullmatch(config.account_id)
        and _BUCKET.fullmatch(config.bucket)
        and config.access_key_id
        and config.secret_access_key
        and _SAFE_PART.fullmatch(config.community_id)
    ):
        raise ValueError("incomplete or invalid R2 pilot configuration")
    return config


def client_for(settings: PilotSettings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    )


def object_key(pack: dict[str, Any]) -> str:
    community_id = str(pack.get("community_id", "")).strip()
    filename = str(pack.get("filename", "")).strip()
    md5 = str(pack.get("music_md5", "")).strip().lower()
    if not (
        _SAFE_PART.fullmatch(community_id)
        and _SAFE_PART.fullmatch(filename)
        and _MD5.fullmatch(md5)
    ):
        raise ValueError("invalid voice pack identity for R2")
    # Immutable content-addressed key; reuploaded voices cannot serve stale bytes.
    return f"pilot/{community_id}/{md5}/{filename}"


def _expected_size(pack: dict[str, Any]) -> int:
    size = int(pack.get("size", 0))
    if size <= 0 or size > 32 * 1024 * 1024:
        raise ValueError("invalid voice pack size for R2")
    return size


def object_is_verified(s3: Any, settings: PilotSettings, pack: dict[str, Any]) -> bool:
    info = s3.head_object(Bucket=settings.bucket, Key=object_key(pack))
    md5 = str(pack["music_md5"]).lower()
    return (
        int(info.get("ContentLength", -1)) == _expected_size(pack)
        and str(info.get("Metadata", {}).get("anthbot-md5", "")).lower() == md5
    )


def pilot_download_url(pack: dict[str, Any]) -> str | None:
    """Return an R2 URL for the configured paid voice, otherwise legacy fallback."""
    if str(pack.get("access", "free")).lower() != "paid":
        return None
    if not os.environ.get("ANTHBOT_R2_PILOT_ENABLED", "").lower() == "true":
        return None
    try:
        settings = settings_from_env()
        if settings is None or pack.get("community_id") != settings.community_id:
            return None
        s3 = client_for(settings)
        if not object_is_verified(s3, settings, pack):
            _LOG.warning("R2 pilot object missing expected size or MD5 metadata")
            return None

        result = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.bucket, "Key": object_key(pack)},
            ExpiresIn=_PRESIGNED_TTL,
            HttpMethod="GET",
        )
        parts = urlsplit(result)
        if parts.scheme != "https" or parts.hostname != (
            f"{settings.account_id}.r2.cloudflarestorage.com"
        ):
            raise ValueError("unexpected R2 presigned URL host")
        return result
    except Exception as err:
        # Never include URLs, credentials, token fragments or exception strings.
        _LOG.warning("R2 pilot unavailable (%s); using existing download route",
                     type(err).__name__)
        return None
