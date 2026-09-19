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

Publish local port 8080 through your own tunnel. Do not commit the public
hostname or LAN address.

After start, `GET /health` should return:

```json
{"ok":true,"schema":"anthbot-reporting-server-v1"}
```

## Endpoints

- `POST /api/anthbot/telemetry`
- `POST /api/anthbot/diagnostics`
- `GET /api/anthbot/voice-packs`
- `GET /voice-packs/{filename}`
- `GET /api/anthbot/admin/voice-packs`
- `POST /api/anthbot/admin/voice-packs`
- `DELETE /api/anthbot/admin/voice-packs/{pack_id}`
- `GET /api/anthbot/admin/stats`
- `GET /api/anthbot/admin/diagnostics`


## Community voice-pack uploads

Uploaded packs are stored persistently under `/data/voice_packs`. The server
calculates MD5 and size while streaming the upload, publishes the file at
`/voice-packs/{filename}`, and immediately merges its metadata into the public
`/api/anthbot/voice-packs` registry. Uploads replace the previous uploaded pack
with the same `language_code`, so ANTHBOT Map gets one current Community option
per language.

Maximum package size: 32 MiB.

Example:

```bash
curl -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@cs-girl-de-1.0.0" \
  -F "language=Čeština" \
  -F "language_code=cs" \
  -F "version=1.0.0" \
  -F "variant_id=vlasta_standard" \
  -F "variant_name=Vlasta (női) · Standard" \
  -F "english_name=German" \
  -F "sex=girl" \
  -F "music_package=3" \
  -F "models=Anthbot Genie 1000" \
  https://reports.example/api/anthbot/admin/voice-packs
```

Only the admin API accepts uploads/deletes. Public registry and package download
endpoints do not expose the admin token.


Multiple Community packs may share the same language code. Replacement identity
is `language_code + variant_id`, so female/male/theme variants coexist. Re-uploading
the same identity replaces only that exact variant and leaves the others intact.
