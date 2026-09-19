from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException


API_BASE = "https://api.anthbot.com/api/v1"
USER_AGENT = "LdMower/1581 CFNetwork/3860.400.51 Darwin/25.3.0"
COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "version": "v2",
    "language": "en",
    "User-Agent": USER_AGENT,
}
IOT_ENDPOINT_RE = re.compile(
    r"^[A-Za-z0-9-]+\.iot\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$"
)


def _recursive_value(value: Any, names: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 12:
        return None
    wanted = {name.casefold() for name in names}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in wanted and child not in (None, ""):
                return child
        for child in value.values():
            found = _recursive_value(child, names, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recursive_value(child, names, depth + 1)
            if found not in (None, ""):
                return found
    return None


def anthbot_json(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = dict(COMMON_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = UrlRequest(
        f"{API_BASE}{path}",
        data=payload,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024)
    except HTTPError as err:
        raise HTTPException(
            status_code=502,
            detail=f"ANTHBOT cloud request failed (HTTP {err.code}).",
        ) from None
    except (URLError, TimeoutError, OSError):
        raise HTTPException(
            status_code=502,
            detail="ANTHBOT cloud is temporarily unavailable.",
        ) from None

    try:
        decoded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="ANTHBOT cloud returned an unreadable response.",
        ) from None
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=502, detail="Unexpected ANTHBOT cloud response.")

    code = decoded.get("code")
    if str(code) != "0":
        message = str(decoded.get("msg") or decoded.get("message") or "").strip()
        if method.upper() == "POST" and path == "/login":
            raise HTTPException(
                status_code=401,
                detail=message[:240] or "ANTHBOT sign-in failed.",
            )
        raise HTTPException(
            status_code=502,
            detail=message[:240] or f"ANTHBOT cloud rejected the request (code {code}).",
        )
    return decoded


def _owner_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value).strip().casefold() in {"1", "true", "yes"}


def is_genie(category: str) -> bool:
    """Match the beta.15 fail-closed Genie family gate."""
    value = category.strip()
    if not value:
        return False
    return (
        re.match(
            r"^(?:anthbot[\s_-]+)?genie(?=$|[\s_-]|\d)",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def mask_serial(serial: str) -> str:
    if len(serial) <= 8:
        return "***"
    return f"{serial[:4]}…{serial[-4:]}"


def login_and_devices(
    username: str,
    password: str,
    area_code: str,
) -> tuple[str, list[dict[str, Any]]]:
    login = anthbot_json(
        "POST",
        "/login",
        body={"username": username, "password": password, "areaCode": area_code},
    )
    access_token = _recursive_value(login, ("access_token", "accessToken"))
    if not isinstance(access_token, str) or not access_token.strip():
        raise HTTPException(
            status_code=502,
            detail="ANTHBOT sign-in succeeded but no access token was returned.",
        )
    access_token = access_token.strip()

    bound = anthbot_json("GET", "/device/bind/list", token=access_token)
    data = bound.get("data")
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="ANTHBOT device list is unavailable.")

    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("sn") or "").strip()
        if not serial or serial in seen or not _owner_value(item.get("is_owner")):
            continue
        seen.add(serial)
        alias = str(
            item.get("alias")
            or item.get("name")
            or item.get("device_name")
            or item.get("nickname")
            or "ANTHBOT robot"
        ).strip()
        category = str(item.get("category_id") or item.get("model") or "").strip()
        devices.append(
            {
                "serial": serial,
                "serial_masked": mask_serial(serial),
                "alias": alias[:128] or "ANTHBOT robot",
                "category": category[:128],
                "supported": is_genie(category),
            }
        )
    if not devices:
        raise HTTPException(
            status_code=404,
            detail="No owner ANTHBOT mower was found on this account.",
        )
    return access_token, devices


def verification_token(serial: str) -> str:
    epoch = int(time.time())
    digest = hashlib.md5(f"{serial}{epoch}".encode("utf-8")).hexdigest()
    return f"{digest}{epoch}"


def iot_credentials(serial: str, access_token: str) -> dict[str, str]:
    response = anthbot_json(
        "POST",
        "/device/v2/iot/sts/arn",
        token=access_token,
        body={"sn": serial, "verification_token": verification_token(serial)},
    )
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("ANTHBOT did not return temporary robot credentials.")

    result = {
        "access_key_id": str(data.get("access_key_id") or "").strip(),
        "secret_access_key": str(data.get("secret_access_key") or "").strip(),
        "session_token": str(data.get("session_token") or "").strip(),
        "region": str(data.get("region_name") or "").strip(),
        "endpoint": str(data.get("endpoint") or "").strip(),
    }
    if not all(result.values()):
        raise RuntimeError("ANTHBOT returned incomplete temporary robot credentials.")

    endpoint = re.sub(r"^https?://", "", result["endpoint"], flags=re.I).rstrip("/")
    if not IOT_ENDPOINT_RE.fullmatch(endpoint):
        raise RuntimeError("ANTHBOT returned an unexpected robot endpoint.")
    match = re.search(r"\.iot\.([a-z0-9-]+)\.", endpoint)
    if match:
        result["region"] = match.group(1)
    result["endpoint"] = endpoint
    return result


def iot_client(credentials: dict[str, str]):
    return boto3.client(
        "iot-data",
        region_name=credentials["region"],
        endpoint_url=f"https://{credentials['endpoint']}",
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
        aws_session_token=credentials["session_token"],
        config=Config(
            connect_timeout=10,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def get_shadow(iot: Any, serial: str) -> dict[str, Any]:
    response = iot.get_thing_shadow(thingName=serial, shadowName="property")
    payload = response.get("payload")
    raw = payload.read() if hasattr(payload, "read") else payload
    if not isinstance(raw, (bytes, bytearray)):
        raise RuntimeError("Robot status response is unreadable.")
    decoded = json.loads(bytes(raw).decode("utf-8-sig"))
    if not isinstance(decoded, dict):
        raise RuntimeError("Robot status response is invalid.")
    return decoded


def _reported(shadow: dict[str, Any]) -> dict[str, Any]:
    state = shadow.get("state")
    if not isinstance(state, dict):
        return {}
    reported = state.get("reported")
    return reported if isinstance(reported, dict) else {}


def voice_status(shadow: dict[str, Any]) -> dict[str, Any] | None:
    reported = _reported(shadow)
    music_cfg = reported.get("music_cfg")
    if isinstance(music_cfg, dict) and isinstance(music_cfg.get("voice_status"), dict):
        return music_cfg["voice_status"]
    value = reported.get("voice_status")
    return value if isinstance(value, dict) else None


def music_package(shadow: dict[str, Any]) -> str:
    reported = _reported(shadow)
    music_cfg = reported.get("music_cfg")
    value = music_cfg.get("music_package") if isinstance(music_cfg, dict) else None
    if value is None:
        value = reported.get("music_package")
    return str(value or "")


def status_summary(status: dict[str, Any] | None) -> str:
    if not status:
        return "No robot voice status yet."
    return (
        f"{str(status.get('name') or 'voice')} · "
        f"{str(status.get('state') or 'unknown')} · "
        f"{str(status.get('progress') if status.get('progress') is not None else '—')}%"
    )


def install_voice(
    *,
    access_token: str,
    serial: str,
    pack: dict[str, Any],
    update,
) -> None:
    def stage(progress: int, message: str) -> None:
        update(progress=progress, message=message)

    stage(8, "Creating a temporary secure robot connection…")
    credentials = iot_credentials(serial, access_token)
    iot = iot_client(credentials)

    baseline = None
    try:
        baseline = voice_status(get_shadow(iot, serial))
    except Exception:
        baseline = None
    baseline_summary = status_summary(baseline)

    stage(18, "Preparing the mower for the voice download…")
    marker = json.dumps(
        {"state": {"desired": {"voice_status": "ip"}}},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        iot.update_thing_shadow(thingName=serial, shadowName="property", payload=marker)
    except (ClientError, BotoCoreError):
        pass

    voice_data = {
        "music_package": pack["music_package"],
        "english_name": pack["english_name"],
        "sex": pack["sex"],
        "music_url": pack["music_url"],
        "music_md5": pack["music_md5"],
        "category": "voice_pack",
        "version": pack["version"],
    }
    command = json.dumps(
        {"state": {"desired": {"cmd": "voice_set", "data": voice_data}}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    topic = f"$aws/things/{serial}/shadow/name/service/update"

    stage(28, "Sending the voice installation command…")
    try:
        iot.publish(topic=topic, qos=0, payload=command)
    except ClientError as err:
        status = int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if status not in {401, 403}:
            raise
        credentials = iot_credentials(serial, access_token)
        iot = iot_client(credentials)
        iot.publish(topic=topic, qos=0, payload=command)

    stage(35, "The mower accepted the command. Waiting for download progress…")
    started = time.time()
    deadline = started + 5 * 60
    last_summary = ""
    saw_active = False
    target_package = str(pack["music_package"])
    stable_since: float | None = None

    time.sleep(3)
    while time.time() < deadline:
        shadow = get_shadow(iot, serial)
        status = voice_status(shadow)
        summary = status_summary(status)
        if summary != last_summary:
            last_summary = summary
            stage(min(92, max(38, int(38 + (time.time() - started) / 3.8))), summary)

        if status:
            state = str(status.get("state") or "").casefold()
            name = str(status.get("name") or "")
            raw_progress = status.get("progress")
            progress_text = "" if raw_progress is None else str(raw_progress)
            try:
                progress_value = int(float(progress_text)) if progress_text else 0
                has_progress = bool(progress_text)
            except ValueError:
                progress_value = 0
                has_progress = False

            if 0 < progress_value < 100 or state in {
                "downloading", "download", "installing", "processing", "start", "running", "ip"
            }:
                saw_active = True
            if state in {"failed", "failure", "error"}:
                raise RuntimeError("The mower reported a voice installation error.")

            status_id = str(status.get("id") or "")
            name_matches = bool(
                re.search(r"(?i)(girl[-_]?de|de[-_]?girl|german|deutsch)", name)
            )
            target_matches = (
                name_matches
                or status_id == target_package
                or (music_package(shadow) == target_package and not name)
            )
            complete = (not has_progress) or progress_value >= 100
            changed = summary != baseline_summary
            fresh_time = False
            raw_time = status.get("time", status.get("timestamp"))
            try:
                epoch = int(float(str(raw_time)))
                if epoch > 99_999_999_999:
                    epoch //= 1000
                fresh_time = epoch >= int(started) - 120
            except (TypeError, ValueError):
                pass

            success = state == "success" and complete and target_matches
            if success and (saw_active or changed or fresh_time):
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= 5:
                    break
            else:
                stable_since = None
        time.sleep(4)
    else:
        raise RuntimeError(
            "The mower did not confirm the voice installation within 5 minutes."
        )

    stage(95, "Voice pack installed. Sending the audible test signal…")
    signal = json.dumps(
        {"state": {"desired": {"cmd": "find_robot", "data": 1}}},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        iot.publish(topic=topic, qos=0, payload=signal)
    except Exception:
        pass
    update(
        status="success",
        progress=100,
        message="Installation complete. The mower should play its find-robot voice now.",
    )
