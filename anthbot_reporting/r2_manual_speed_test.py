"""Private R2 single-voice upload and GET speed check for an SSH session.

Usage from the HOME ASSISTANT HOST:
    docker cp r2_manual_speed_test.py <reporting-container>:/tmp/r2_manual_speed_test.py
    docker exec -it <reporting-container> python3 /tmp/r2_manual_speed_test.py

Credentials are prompted without terminal echo, live in process memory only,
and are NOT saved to /data, the addon options, logs or a file.
No HA restart, registry modification or voice-set command is performed.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config

BUCKET = "anthbot-voice-packs"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ACCOUNT_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
EXPECTED_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
MAX_SIZE = 32 * 1024 * 1024


def available_paid_packs(root: Path) -> list[dict]:
    registry_path = root / "registry.json"
    if not registry_path.is_file():
        raise ValueError("Nem talalhato a hangcsomag registry.json")
    records = json.loads(registry_path.read_text(encoding="utf-8")).get("packs", [])
    if not isinstance(records, list):
        raise ValueError("Ervenytelen registry formatum")
    result = []
    for item in records:
        if not isinstance(item, dict) or str(item.get("access", "")).lower() != "paid":
            continue
        filename = str(item.get("filename", ""))
        if not SAFE_NAME.fullmatch(filename):
            continue
        if not (root / filename).is_file():
            continue
        if not SAFE_NAME.fullmatch(str(item.get("community_id", ""))):
            continue
        if not EXPECTED_MD5.fullmatch(str(item.get("music_md5", ""))):
            continue
        result.append(item)
    return result


def verified_local_file(root: Path, pack: dict) -> tuple[Path, int, str]:
    filename = str(pack["filename"])
    path = root / filename
    if not SAFE_NAME.fullmatch(filename) or path.is_symlink() or not path.is_file():
        raise ValueError("Hibas vagy hianyzo helyi hangfajl")
    size = path.stat().st_size
    if not (0 < size <= MAX_SIZE) or size != int(pack["size"]):
        raise ValueError("Hibas helyi fajlmeret")
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        while True:
            block = source.read(256 * 1024)
            if not block:
                break
            digest.update(block)
    md5 = digest.hexdigest()
    if md5 != str(pack["music_md5"]).lower():
        raise ValueError("Helyi MD5 elteres: feltoltes leallitva")
    return path, size, md5


def ask_voice(packs: list[dict]) -> dict:
    query = input("Keresett hang neve [lilla]: ").strip().casefold() or "lilla"
    found = [
        item for item in packs
        if query in " ".join(str(item.get(key, "")) for key in (
            "language", "variant_name", "community_id", "voice_display_name"
        )).casefold()
    ]
    if not found:
        raise ValueError("Nincs ilyen fizetos, helyileg elerheto hang")
    print("Talalatok:")
    for index, pack in enumerate(found[:30], start=1):
        print("%d: %s | %s | %s | %.2f MiB" % (
            index,
            pack.get("community_id"),
            pack.get("variant_name", ""),
            pack.get("version", ""),
            int(pack.get("size", 0)) / 1048576,
        ))
    if len(found) > 30:
        print("Tul sok talalat. Futtasd ujra pontosabb keresessel.")
        raise ValueError("Tul sok talalat")
    choice = input("Melyiket teszteljuk? [1]: ").strip() or "1"
    if not choice.isdecimal() or not 1 <= int(choice) <= len(found):
        raise ValueError("Ervenytelen sorszam")
    return found[int(choice) - 1]


def main() -> None:
    root = Path(os.environ.get("ANTHBOT_VOICE_PACK_DIR", "/data/voice_packs"))
    voices = available_paid_packs(root)
    if not voices:
        raise ValueError("Nem talalhato helyileg tarolt fizetos hang")
    print("ANTHBOT R2 sebessegproba; eredeti Reporting Server valtozatlan.")
    selected = ask_voice(voices)
    path, size, md5 = verified_local_file(root, selected)
    print("Helyi fajl ellenorizve: %.2f MiB, MD5 egyezik." % (size / 1048576,))
    account_id = input("Cloudflare Account ID (32 hex karakter): ").strip()
    if not ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("Ervenytelen Cloudflare Account ID")
    access_key = getpass.getpass("R2 Access Key ID (rejtett): ").strip()
    secret_key = getpass.getpass("R2 Secret Access Key (rejtett): ").strip()
    if not access_key or not secret_key:
        raise ValueError("Hianyzo API kulcs")
    print("Cel: privát %s bucket" % BUCKET)
    if input("Feltoltes es visszaolvasasos sebessegproba? [igen/N]: ").strip().lower() != "igen":
        print("Megszakitva. Semmi nem lett feltoltve.")
        return

    s3 = boto3.client(
        "s3",
        region_name="auto",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % account_id,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2},
        ),
    )
    key = "pilot/%s/%s/%s" % (selected["community_id"], md5, path.name)
    t0 = time.monotonic()
    s3.upload_file(
        str(path), BUCKET, key,
        ExtraArgs={
            "ContentType": "application/octet-stream",
            "Metadata": {"anthbot-md5": md5},
        },
    )
    upload_time = max(time.monotonic() - t0, 0.001)
    info = s3.head_object(Bucket=BUCKET, Key=key)
    if (
        int(info.get("ContentLength", -1)) != size
        or str(info.get("Metadata", {}).get("anthbot-md5", "")).lower() != md5
    ):
        raise ValueError("R2 metaadat vagy meret nem egyezik")

    signed_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=900,
        HttpMethod="GET",
    )
    t0 = time.monotonic()
    digest = hashlib.md5(usedforsecurity=False)
    received = 0
    request = Request(signed_url, headers={"User-Agent": "ANTHBOT-R2-Pilot/1.0"})
    with urlopen(request, timeout=60) as response:
        status = response.status
        first_byte_ms = None
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            if first_byte_ms is None:
                first_byte_ms = int((time.monotonic() - t0) * 1000)
            digest.update(block)
            received += len(block)
    download_time = max(time.monotonic() - t0, 0.001)
    if status != 200 or received != size or digest.hexdigest() != md5:
        raise ValueError("R2 GET nem egyezik a helyi hangfajllal")

    print("\n=== R2 TESZT SIKERES ===")
    print("Hang community ID:", selected["community_id"])
    print("R2 meret: %.2f MiB | MD5 egyezik: IGEN" % (size / 1048576,))
    print("Feltoltes: %.2f mp | %.2f Mbit/s" % (
        upload_time, (size * 8 / 1_000_000) / upload_time))
    print("R2 GET: HTTP %s | %.2f mp | %.2f Mbit/s" % (
        status, download_time, (size * 8 / 1_000_000) / download_time))
    print("Elso adathoz: %s ms" % first_byte_ms)
    print("Hangfajl es registry a Reporting Serveren VALTOZATLAN.")
    print("Az R2 letoltesi token titkos es nem kerult kiirasra.")
    print("A robot sebessege csak a kesobbi Genie teszttel merheto.")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as exc:
        print("R2 HTTP hiba:", exc.code, "A kulcsot es bucket jogokat ellenorizd.")
        sys.exit(1)
    except Exception as exc:
        # Do not expose exception messages, which may include signed URLs.
        if isinstance(exc, (ValueError, FileNotFoundError)):
            print("Hiba:", str(exc))
        else:
            print("Hiba tipusa:", type(exc).__name__)
        sys.exit(1)
