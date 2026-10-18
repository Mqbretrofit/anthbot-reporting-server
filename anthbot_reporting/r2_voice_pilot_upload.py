"""One-time, explicit upload for a single paid-voice R2 pilot.

Run INSIDE the Reporting Server add-on after configuring the private R2
bucket and its scoped access keys. Never prints credentials or signed URLs.
This does not change local registry or delete local files.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import app as core
from r2_voice_pilot import (
    client_for,
    object_is_verified,
    object_key,
    settings_from_env,
)


def main() -> None:
    settings = settings_from_env()
    if settings is None:
        raise SystemExit("R2 pilot is disabled; no upload performed")
    records = core._uploaded_voice_pack_registry().get("packs", [])
    candidates = [
        pack for pack in records
        if isinstance(pack, dict)
        and pack.get("community_id") == settings.community_id
        and str(pack.get("access", "")).lower() == "paid"
    ]
    if len(candidates) != 1:
        raise SystemExit("Exactly one paid pilot voice must match in the registry")

    pack = candidates[0]
    filename = str(pack.get("filename", ""))
    key = object_key(pack)
    # Avoid arbitrary paths supplied through a corrupted registry.
    if Path(filename).name != filename or not filename:
        raise SystemExit("Invalid source filename")
    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise SystemExit("Voice file missing; nothing uploaded")

    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != str(pack.get("music_md5", "")).lower():
        raise SystemExit("Local MD5 does not match registered voice; upload refused")
    if path.stat().st_size != int(pack.get("size", 0)):
        raise SystemExit("Local size does not match registered voice; upload refused")

    s3 = client_for(settings)
    s3.upload_file(
        str(path),
        settings.bucket,
        key,
        ExtraArgs={
            "ContentType": "application/octet-stream",
            "Metadata": {"anthbot-md5": digest.hexdigest()},
        },
    )
    if not object_is_verified(s3, settings, pack):
        raise SystemExit("R2 object metadata verification failed")
    print("R2 PILOT UPLOAD VERIFIED")
    print(f"Voice community ID: {settings.community_id}")
    print(f"Size: {path.stat().st_size} bytes; MD5 match: yes")
    print("Original local file and legacy download endpoint unchanged.")


if __name__ == "__main__":
    main()
