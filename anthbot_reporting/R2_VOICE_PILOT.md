# Cloudflare R2 paid voice pilot (owner only)

This is a TEST branch, not a production release.

## Safety
- Disabled by default.
- Only an owner entitlement and ONE configured paid community_id use R2.
- Buyers and admin grants continue receiving the existing server URL.
- The R2 uploader never deletes local files or changes the voice registry.
- The R2 URL is issued only after the Reporting Server's existing entitlement check.
- A missing object, wrong size or MD5 metadata, API error, or configuration
  problem falls back to the original paid voice download endpoint.
- Turning the pilot off and restarting reverts new URLs to legacy delivery.

## Cloudflare dashboard setup
1. R2 Object Storage -> Create bucket: anthbot-voice-packs.
2. Location Automatic; storage class Standard. Keep private, without r2.dev.
3. R2 Overview -> API Tokens -> Manage -> Create Account/User API token.
4. Select Object Read & Write and scope to ONLY anthbot-voice-packs.
5. Store Access Key ID and Secret Access Key securely. Never paste credentials,
   full presigned URLs, or secrets into GitHub, chat or screenshots.
6. This pilot expects a standard (non-jurisdiction-restricted) R2 bucket.

## Reporting Server test add-on options
Configure ONLY in a test Reporting Server build:

    r2_pilot_enabled: false
    r2_pilot_community_id: "<one owned paid community_id>"
    r2_account_id: "<32-character Cloudflare account ID>"
    r2_bucket: "anthbot-voice-packs"
    r2_access_key_id: "<R2 access key ID>"
    r2_secret_access_key: "<R2 secret access key>"

The add-on config exposes masked password fields for R2 keys. The live main
branch remains unchanged. Keep a backup of the live add-on and its /data.
Do not enable R2 before the bucket and keys exist.

## Upload and validate one pilot voice
After the test add-on is configured with the pilot voice and keys, enable the
test instance's r2_pilot_enabled setting and restart. Run INSIDE the container:

    python3 /app/r2_voice_pilot_upload.py

The script finds the paid voice in /data/voice_packs, verifies the original
MD5 and size, uploads it under a content-addressed R2 object key, then
downloads the object again and verifies byte count and MD5. No existing
voice data is overwritten or deleted.

On the owner client, refresh entitlements and check that only the selected
voice's URL changes to the Cloudflare R2 S3 API hostname. Then install this
one voice on the Genie 1000, measure time and verify 100% and correct slot.
Non-owner clients must still use the existing Reporting Server URL.

## Rollback
Set r2_pilot_enabled to false in the test add-on and restart. The existing
authenticated Reporting Server download endpoint is retained unchanged.

## Limitations
- Presigned GET URLs last 15 minutes and must be treated as private bearer
  credentials. Revoking purchases does not revoke an already issued URL.
- Cloudflare R2 S3 presigned URLs work on the account's R2 API hostname,
  not on a custom domain.
- Robot compatibility and actual download speed are UNVERIFIED until tested.
- Bulk upload, automated replacement and broad customer cutover are not
  included in this pilot.
