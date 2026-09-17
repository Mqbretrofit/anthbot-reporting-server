# ANTHBOT Reporting Server

This Home Assistant app runs the opt-in ANTHBOT Map telemetry and diagnostics
backend locally on port 8080.

This repository is maintainer infrastructure. It is not part of the public
Home Assistant integration.

## Configuration

Set `admin_token`. It protects the admin JSON API and dashboard with
`Authorization: Bearer <token>`.

Telemetry and diagnostics ingest stay separate from the admin API.

The SQLite database is stored in the app `/data` volume as
`anthbot_reporting.sqlite3` and is included in app backups.

## Tunnel

## Endpoints

- `POST /api/anthbot/telemetry`
- `POST /api/anthbot/diagnostics`
- `GET /api/anthbot/admin/stats`
- `GET /api/anthbot/admin/diagnostics`
Publish local port 8080 through your own tunnel. Do not commit the public
hostname or LAN address.

After start, `GET /health` should return:

```json
{"ok":true,"schema":"anthbot-reporting-server-v1"}
