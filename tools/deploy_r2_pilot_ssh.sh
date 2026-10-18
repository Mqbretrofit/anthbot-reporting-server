#!/bin/sh
# Safe, reversible SSH overlay for ONE owner's private R2-paid voice test.
set -eu

REPO="https://raw.githubusercontent.com/Mqbretrofit/anthbot-reporting-server"
PIN="07acd497eb0288c17f71f16686267d932c82b87b"
C="$(docker ps --format '{{.Names}}' | grep 'anthbot_reporting' | head -n 1)"
if [ -z "$C" ]; then
  echo "HIBA: nem talalhato a futo ANTHBOT Reporting Server kontener."
  exit 1
fi

echo "Reporting Server: $C"
echo "SSH-s tulajdonosi R2 probatelepitest keszitunk elo."
echo "Az API kulcsokat csak a kontenerben kerjuk be, rejtett modon."

curl -fLsS "$REPO/$PIN/tools/install_r2_pilot_ssh.py" \
  -o /tmp/anthbot_r2_pilot_install.py
curl -fLsS "$REPO/$PIN/anthbot_reporting/r2_voice_pilot.py" \
  -o /tmp/anthbot_r2_voice_pilot.py
python3 -m py_compile /tmp/anthbot_r2_pilot_install.py /tmp/anthbot_r2_voice_pilot.py
docker cp /tmp/anthbot_r2_pilot_install.py "$C:/tmp/r2_pilot_install.py"
docker cp /tmp/anthbot_r2_voice_pilot.py "$C:/tmp/r2_voice_pilot_stage.py"

rollback() {
  echo "HIBA: R2 pilot visszaallitasa a mentett eredetire..."
  docker exec "$C" python3 /tmp/r2_pilot_install.py --rollback || {
    echo "Automatikus visszaallitas sikertelen, NE frissitsd vagy torold az add-ont."
    return 1
  }
  docker restart "$C" >/dev/null || return 1
}

# Interactive confirmation; decline is a nonzero exit and does not restart HA.
docker exec -it "$C" python3 /tmp/r2_pilot_install.py

if ! docker exec "$C" python3 -m py_compile \
    /app/store_api.py /app/r2_voice_pilot.py; then
  rollback
  exit 1
fi

if ! docker exec "$C" bash -n /run.sh; then
  rollback
  exit 1
fi

echo "Syntax OK. Reporting Server ujrainditasa..."
if ! docker restart "$C" >/dev/null; then
  echo "A kontener nem indult ujra. Manualis visszaallitas szukseges."
  exit 1
fi

ok=0
i=0
while [ "$i" -lt 12 ]; do
  if docker exec "$C" python3 -c \
    'import urllib.request; import json; r=urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3); assert r.status==200 and json.load(r).get("ok") is True' \
    >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
  i=$((i + 1))
done

if [ "$ok" -ne 1 ]; then
  echo "HIBA: a szerver health ellenorzese nem sikerult."
  rollback || true
  exit 1
fi

echo "=========================================="
echo "R2 PILOT AKTIV - /health HTTP 200 OK."
echo "Egy kivalasztott fizetos hang: csak owner entitlement."
echo "Normal vasarloi hangletoltesek valtozatlanok."
echo "=========================================="
echo "Visszaallitas, ha szukseges:"
echo "docker exec $C python3 /tmp/r2_pilot_install.py --rollback"
echo "docker restart $C"
