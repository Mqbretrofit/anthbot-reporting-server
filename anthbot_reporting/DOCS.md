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


## Community voice identity

Community uploads carry a stable `community_id`, a separate human `voice_gender`, and the fixed Genie technical slot `German_girl`. The server assigns the public robot-visible `1.2.x` version and guarantees uniqueness for new or changed packages. ANTHBOT Map resolves `German_girl + version` back to the exact `community_id` and display name.


## Optional paid Community voice store

The Reporting Server can sell selected uploaded Community voice packs without
changing the existing free ANTHBOT Map registry. Existing uploads remain free by
default. An administrator explicitly changes a pack to `paid` and sets its
minor-unit price (for example `499` = EUR 4.99).

Paid packs are intentionally removed from the legacy
`GET /api/anthbot/voice-packs` response and their normal
`/voice-packs/{filename}` URL returns 404. This keeps older ANTHBOT Map
versions from accidentally receiving an unlicensed direct download URL. The
store uses a separate licensed download endpoint.

The payment implementation uses Stripe-hosted Checkout. The Reporting Server
never receives or stores card details. A completed Checkout Session is converted
into a signed ANTHBOT voice license. The license can later be stored by ANTHBOT
Map and exchanged for a licensed `music_url`.

Configuration:

- `store_enabled`: enable checkout creation.
- `stripe_secret_key`: Stripe server-side secret key.
- `stripe_webhook_secret`: signing secret for the Reporting Server webhook.
- `store_license_secret`: private random secret used to sign ANTHBOT voice
  licenses. Keep it stable; changing it invalidates previously issued licenses.
- `stripe_automatic_tax`: when enabled, request Stripe automatic tax
  calculation for Checkout Sessions. Enable this only after Stripe Tax is
  configured on the Stripe account.

Checkout sessions explicitly disable Stripe Managed Payments. The ANTHBOT store
uses standard Stripe Checkout with the project's own licensing and delivery
flow, so Managed Payments merchant-of-record eligibility requirements are not
applied. The inline product still carries `txcd_10401100` (Digital Audio Works
- downloaded - non subscription - with permanent rights) for future tax use.

### Automatic ANTHBOT Map entitlement linking

ANTHBOT Map can create a random local store client token that never contains an
ANTHBOT account name, mower serial number, email address, or other account
identifier. The Reporting Server hashes that token to an opaque client ID and
never stores the token itself.

The integration requests a temporary browser pairing URL, opens the store with
that pairing code, and the Checkout Session records only the opaque client ID.
After payment, ANTHBOT Map polls its entitlement endpoint with the local token.
Purchased packs are then returned with licensed download URLs and appear in the
normal voice-pack selector automatically. Manual license copying remains only a
recovery fallback.

Client endpoints:

- `POST /api/anthbot/store/client/pair`
- `POST /api/anthbot/store/client/entitlements`

Pairing codes expire after seven days. They can associate purchases with a Map
installation but cannot read entitlements or download paid packs by themselves.

Public endpoints:

- `GET /store`
- `GET /api/anthbot/store/voice-packs`
- `POST /api/anthbot/store/checkout`
- `POST /api/anthbot/store/webhooks/stripe`
- `GET /api/anthbot/store/orders/{session_id}`
- `POST /api/anthbot/store/entitlements`
- `GET /api/anthbot/store/voice-packs/{pack_id}/download?license=...`

Admin endpoints:

- `GET /dashboard/store`
- `GET /api/anthbot/admin/store/voice-packs`
- `PATCH /api/anthbot/admin/store/voice-packs/{pack_id}`
- `GET /api/anthbot/admin/store/orders`
- `GET /api/anthbot/admin/store/stats`

Stripe should send at least these Checkout events to
`https://<public-reporting-host>/api/anthbot/store/webhooks/stripe`:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`
- `charge.refunded` (revokes the corresponding voice license)

The integration itself remains free; only voice packs explicitly marked as paid
use the store flow.
