"""Reversible owner-only Cloudflare R2 pilot overlay for an existing HA add-on.

Run with 'docker exec -it <reporting> python3 /tmp/r2_pilot_install.py'.
Only changes /app/store_api.py, /app/r2_voice_pilot.py and /run.sh,
with original copies in a private persistent /data backup directory.
Use --rollback to restore; restart container after install/rollback.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timezone
from urllib.request import urlopen

import boto3
from botocore.config import Config

ROOT = Path("/data")
REGISTRY = ROOT / "voice_packs" / "registry.json"
STORE = Path("/app/store_api.py")
RUN = Path("/run.sh")
MODULE = Path("/app/r2_voice_pilot.py")
STAGED_MODULE = Path("/tmp/r2_voice_pilot_stage.py")
ENV = ROOT / ".anthbot_r2_pilot.env"
STATE = ROOT / ".anthbot_r2_pilot_state.json"
BUCKET = "anthbot-voice-packs"
ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MD5_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
ENV_MARKER = "# ANTHBOT R2 owner pilot overlay (reversible)"
HOOK_MARKER = "# ANTHBOT R2 owner pilot overlay (reversible)"
HOOK_ANCHOR = (
    '    pack_id = str(pack.get("id") or "").strip()\n'
    '    base = core._public_base_url(request)\n'
)
RUN_ANCHOR = 'bashio::log.info "Starting ANTHBOT Reporting Server on port 8080"\n'


def _safe_write(target: Path, value: bytes, mode: int) -> None:
    temporary = target.with_name(target.name + ".r2-staging")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_packs() -> list[dict]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    values = data.get("packs", [])
    if not isinstance(values, list):
        raise ValueError("Hibas registry.json szerkezet")
    packs = []
    for item in values:
        if not isinstance(item, dict) or str(item.get("access", "")).lower() != "paid":
            continue
        name = str(item.get("filename", ""))
        community_id = str(item.get("community_id", ""))
        md5 = str(item.get("music_md5", "")).lower()
        if not (PART_RE.fullmatch(name) and PART_RE.fullmatch(community_id)
                and MD5_RE.fullmatch(md5)):
            continue
        file_path = REGISTRY.parent / name
        if file_path.is_file() and not file_path.is_symlink():
            packs.append(item)
    return packs


def _select() -> dict:
    packs = _read_packs()
    query = input("Keresett teszthang [lilla]: ").strip().casefold() or "lilla"
    selected = [
        p for p in packs
        if query in " ".join(str(p.get(k, "")) for k in (
            "language", "community_id", "variant_name", "voice_display_name"
        )).casefold()
    ]
    if not selected:
        raise ValueError("Nincs egyezo fizetos hang a Reporting Serveren")
    if len(selected) > 25:
        raise ValueError("Pontositsd a keresest, tul sok talalat")
    for i, pack in enumerate(selected, 1):
        print("%d. %s | %s | v%s" % (
            i, pack["community_id"], pack.get("variant_name", ""),
            pack.get("version", "")))
    answer = input("Melyiket probaljuk? [1]: ").strip() or "1"
    if not answer.isdecimal() or int(answer) not in range(1, len(selected) + 1):
        raise ValueError("Ervenytelen sorszam")
    return selected[int(answer) - 1]


def _check_object(pack: dict, account: str, key_id: str, secret: str) -> None:
    local = REGISTRY.parent / str(pack["filename"])
    size = local.stat().st_size
    expected_size = int(pack.get("size", 0))
    expected_md5 = str(pack["music_md5"]).lower()
    if size <= 0 or size > 32 * 1024 * 1024 or size != expected_size:
        raise ValueError("Hibas helyi fajlmeret")
    digest = hashlib.md5(usedforsecurity=False)
    with local.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_md5:
        raise ValueError("Helyi MD5 elteres, telepites leallitva")

    s3 = boto3.client(
        "s3", region_name="auto",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % account,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=15,
            retries={"max_attempts": 1},
        ),
    )
    obj_key = "pilot/%s/%s/%s" % (pack["community_id"], expected_md5, pack["filename"])
    result = s3.head_object(Bucket=BUCKET, Key=obj_key)
    if (int(result.get("ContentLength", -1)) != size
            or str(result.get("Metadata", {}).get("anthbot-md5", "")).lower()
            != expected_md5):
        raise ValueError("R2 objektum MD5-metaadatja vagy merete hibas")
    print("R2 objektum ellenorizve. Meret: %.2f MiB, helyi MD5 rendben." %
          (size / 1048576,))


def _patch_store(original: str) -> str:
    start = original.find("def _short_lived_voice_url(")
    if start < 0:
        raise ValueError("Nem talalhato a fizetos hang URL eloallitoja")
    end = original.find("\ndef ", start + 4)
    if end < 0:
        end = len(original)
    body = original[start:end]
    if HOOK_MARKER in original:
        raise ValueError("Az R2 pilot mar telepitve van")
    if body.count(HOOK_ANCHOR) != 1:
        raise ValueError("Eltero Reporting Server kod: automatikus patch leallitva")
    replacement = (
        "    " + HOOK_MARKER + "\n"
        '    if entitlement == "owner":\n'
        "        from r2_voice_pilot import pilot_download_url\n"
        "        pilot_url = pilot_download_url(pack)\n"
        "        if pilot_url:\n"
        "            return pilot_url\n\n"
        + HOOK_ANCHOR
    )
    patched = body.replace(HOOK_ANCHOR, replacement)
    result = original[:start] + patched + original[end:]
    compile(result, str(STORE), "exec")
    return result


def _patch_run(original: str) -> str:
    if ENV_MARKER in original:
        raise ValueError("Az R2 pilot mar telepitve van a run.sh-ban")
    if original.count(RUN_ANCHOR) != 1:
        raise ValueError("Eltero run.sh; telepites megszakitva")
    insertion = (
        ENV_MARKER + "\n"
        'if [ -f /data/.anthbot_r2_pilot.env ]; then\n'
        '  . /data/.anthbot_r2_pilot.env\n'
        'fi\n'
    )
    return original.replace(RUN_ANCHOR, insertion + RUN_ANCHOR)


def _rollback() -> None:
    if not STATE.exists():
        raise ValueError("Nincs telepitett R2 pilot allapotmentes")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    backup = Path(state["backup_dir"])
    if backup.parent != ROOT / "r2_pilot_backups":
        raise ValueError("Hibas mentesi utvonal")
    store_backup = backup / "store_api.py"
    run_backup = backup / "run.sh"
    if not (store_backup.is_file() and run_backup.is_file()):
        raise ValueError("Hianyzik a mentett eredeti kod")
    _safe_write(STORE, store_backup.read_bytes(), STORE.stat().st_mode & 0o777)
    _safe_write(RUN, run_backup.read_bytes(), RUN.stat().st_mode & 0o777)
    if state.get("module_existed"):
        shutil.copy2(backup / "r2_voice_pilot.py", MODULE)
    else:
        MODULE.unlink(missing_ok=True)
    ENV.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)
    print("R2 kod es config visszaallitva. Most inditsd ujra a kontenert.")


def _install() -> None:
    if STATE.exists() or ENV.exists():
        raise ValueError("Mar van R2 telepitesi allapot vagy titkos konfiguracio")
    if not (STORE.is_file() and RUN.is_file() and STAGED_MODULE.is_file()):
        raise ValueError("Hianyzik a Reporting Server vagy az R2 pilot modul")
    print("ANTHBOT R2 pilot: csak SAJAT (owner) kliens, egyetlen hang.")
    pack = _select()
    original_store = STORE.read_text(encoding="utf-8")
    original_run = RUN.read_text(encoding="utf-8")
    new_store = _patch_store(original_store)
    new_run = _patch_run(original_run)
    module_source = STAGED_MODULE.read_bytes()
    compile(module_source, str(MODULE), "exec")

    account = input("Cloudflare Account ID: ").strip()
    if not ID_RE.fullmatch(account):
        raise ValueError("Ervenytelen 32 karakteres Account ID")
    key_id = getpass.getpass("R2 Access Key ID (rejtett): ").strip()
    secret = getpass.getpass("R2 Secret Access Key (rejtett): ").strip()
    if not key_id or not secret:
        raise ValueError("Hianyzik az R2 API-kulcs")
    _check_object(pack, account, key_id, secret)
    print("Telepitendo hang:", pack.get("variant_name", pack["community_id"]))
    if input("Mentest keszitek es aktivalom a SAJAT robot tesztjet. Mehet? [igen/N]: ").strip().lower() != "igen":
        print("Megszakitva. Nem tortent valtoztatas.")
        return

    import shlex
    values = {
        "ANTHBOT_R2_PILOT_ENABLED": "true",
        "ANTHBOT_R2_PILOT_COMMUNITY_ID": str(pack["community_id"]),
        "ANTHBOT_R2_ACCOUNT_ID": account,
        "ANTHBOT_R2_BUCKET": BUCKET,
        "ANTHBOT_R2_ACCESS_KEY_ID": key_id,
        "ANTHBOT_R2_SECRET_ACCESS_KEY": secret,
    }
    secure_env = "".join("export %s=%s\n" % (name, shlex.quote(value))
                         for name, value in values.items())
    backup_parent = ROOT / "r2_pilot_backups"
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_parent, 0o700)
    backup = backup_parent / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(mode=0o700)
    shutil.copy2(STORE, backup / "store_api.py")
    shutil.copy2(RUN, backup / "run.sh")
    module_existed = MODULE.exists()
    if module_existed:
        shutil.copy2(MODULE, backup / "r2_voice_pilot.py")

    state = {"backup_dir": str(backup), "module_existed": module_existed}
    try:
        _safe_write(ENV, secure_env.encode(), 0o600)
        _safe_write(STORE, new_store.encode("utf-8"), STORE.stat().st_mode & 0o777)
        _safe_write(RUN, new_run.encode("utf-8"), RUN.stat().st_mode & 0o777)
        _safe_write(MODULE, module_source, 0o644)
        _safe_write(STATE, json.dumps(state).encode("utf-8"), 0o600)
    except BaseException:
        shutil.copy2(backup / "store_api.py", STORE)
        shutil.copy2(backup / "run.sh", RUN)
        if module_existed:
            shutil.copy2(backup / "r2_voice_pilot.py", MODULE)
        else:
            MODULE.unlink(missing_ok=True)
        ENV.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
        raise
    print("R2 PILOT TELEPITVE (ujrainditas szukseges).")
    print("Visszaallitas: python3 /tmp/r2_pilot_install.py --rollback")
    print("Titkos API-kulcsok csak a kontener /data/.anthbot_r2_pilot.env fajljaban vannak.")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--rollback"]:
            _rollback()
        elif not sys.argv[1:]:
            _install()
        else:
            raise ValueError("Ervenytelen parancs")
    except Exception as exc:
        # Never print exception text: botocore may include signed request details.
        if isinstance(exc, ValueError):
            print("Telepitesi hiba:", str(exc))
        else:
            print("Telepitesi hiba tipusa:", type(exc).__name__)
        sys.exit(1)
