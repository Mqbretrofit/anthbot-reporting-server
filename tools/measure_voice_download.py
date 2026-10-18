"""Read-only, privacy-safe speed comparison for a freshly started paid voice installation.

Run on Home Assistant host via SSH. Start a Community installation after launching
the script. Prints no download token or authenticated URL.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def wait_for_fresh_voice_url(timeout_seconds=150):
    started = time.time()
    print("Waiting for a NEW Community voice installation (150 seconds)...", flush=True)
    while time.time() - started < timeout_seconds:
        for path in Path("/config/.storage").glob("anthbot_map.voice_pack_request.*"):
            try:
                data = json.loads(path.read_text(encoding="utf-8")).get("data", {})
                timestamp = datetime.fromisoformat(
                    str(data.get("requested_at", ""))
                ).timestamp()
                url = str(data.get("music_url", ""))
                parts = urlsplit(url)
                if (
                    str(data.get("source", "")).lower() == "community"
                    and timestamp >= started - 5
                    and parts.scheme == "https"
                    and parts.path.startswith("/api/anthbot/store/voice-packs/")
                    and parts.path.endswith("/download")
                    and "token=" in parts.query
                ):
                    return url
            except (OSError, ValueError, TypeError):
                continue
        time.sleep(1)
    return None


ORIGIN_MEASURE = """
import sys, time, urllib.request, urllib.error
from urllib.parse import urlsplit, urlunsplit
parts = urlsplit(sys.stdin.read().strip())
url = urlunsplit(('http', '127.0.0.1:8080', parts.path, parts.query, ''))
start = time.monotonic()
try:
    with urllib.request.urlopen(url, timeout=60) as response:
        first = time.monotonic() - start
        size = 0
        while True:
            chunk = response.read(262144)
            if not chunk:
                break
            size += len(chunk)
        elapsed = max(time.monotonic() - start, 0.001)
        print('ORIGIN HTTP=%s size=%.2fMiB first_byte=%.3fs total=%.3fs speed=%.2fMiB/s' %
              (response.status, size/1048576, first, elapsed, size/1048576/elapsed))
except urllib.error.HTTPError as exc:
    print('ORIGIN HTTP=%s; speed not measured' % exc.code)
except Exception as exc:
    print('ORIGIN error=%s: %s' % (type(exc).__name__, str(exc)[:120]))
"""


def main():
    url = wait_for_fresh_voice_url()
    if not url:
        raise SystemExit("No fresh URL detected. An old or expired URL was NOT tested.")

    print("Fresh authorized URL found (token hidden).", flush=True)
    names = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    container = next((name for name in names if "anthbot_reporting" in name), None)
    if not container:
        raise SystemExit("Reporting Server container not found.")

    origin = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", ORIGIN_MEASURE],
        input=url, capture_output=True, text=True, timeout=75,
    )
    print(origin.stdout.strip() or ("ORIGIN error: " + origin.stderr[:160]), flush=True)

    output = (
        "CLOUDFLARE HTTP=%{http_code} size=%{size_download}B "
        "first_byte=%{time_starttransfer}s total=%{time_total}s "
        "speed=%{speed_download}B/s\\n"
    )
    cloudflare = subprocess.run(
        ["curl", "-L", "-sS", "-o", "/dev/null", "--connect-timeout", "15",
         "--max-time", "90", "-A",
         "Mozilla/5.0 (compatible; ANTHBOT-SpeedTest/1.0)",
         "-w", output, url],
        capture_output=True, text=True, timeout=100,
    )
    print(cloudflare.stdout.strip(), flush=True)
    if cloudflare.returncode:
        print("CLOUDFLARE request error:", cloudflare.stderr.strip()[:180], flush=True)
    print("NB: This measures HA-to-Cloudflare and local origin, NOT mower Wi-Fi speed.")


if __name__ == "__main__":
    main()
