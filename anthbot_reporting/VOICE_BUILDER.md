# Server Voice Builder

Reporting Server **1.0.48** adds **Dashboard → Voice Builder** for administrators.
The generator runs in a separate Python process on the server. Closing the
browser does not stop it. One process owns the durable queue; restarting the
add-on recovers interrupted jobs and reuses finished audio.

## Use

1. Save the API keys once. Existing saved keys are retained when the input is
   empty; the explicit delete checkbox removes a key. Secrets are encrypted
   with a private persistent key and are never returned to the browser.
2. Select a provider, languages, voices, text style, delivery and character
   effect. **Add to batch** saves that selection; different recipes can coexist
   in the same batch. Cloud voices apply only to their matching locale.
   The searchable language picker offers all 150 language/region entries from
   the desktop Builder's Nabu Casa catalog, including separate German, Austrian
   and Swiss German choices. Search by native name, Hungarian name or locale.
   Existing selections and manually added languages remain available. Locales
   without a prepared script use the existing cached OpenAI translation flow;
   an OpenAI key is required unless a matching script has already been imported
   or cached. Speech language coverage depends on the selected provider/model;
   the existing Cloud voice presets remain unchanged.
3. Generate **A004/A005 previews**, then start a full batch. The full builder
   reuses the preview TTS cache. Generated output is validated as MP3,
   16 kHz, mono, 32 kbit/s. The three original non-speech assets A001/A003/A030
   remain byte-identical; their original bitrate is preserved.
4. Download the complete TAR.GZ, technical manifest and public catalog, or
   upload it using the existing community pack/version allocation path.
   Newly created packs start hidden. Review the two samples and set price and
   visibility in **Hangbolt**. Republish of the same identity preserves its
   price, visibility and existing access rules.
5. Pause stops at a checkpoint after the current provider request finishes.
   Resume uses completed output and cache. A failed job can be resumed after
   correcting its credentials or batch credit limit. To switch TTS models,
   create a new recipe/job; the original output and shared cache remain intact.

The TTS providers from desktop **alpha.7.20.11** are supported: ElevenLabs,
OpenAI, Home Assistant Cloud and Fish Audio. ElevenLabs voice/model lists are
read from the configured account. The model field is editable; a removed or
unavailable model fails before a TTS request. The batch-wide ElevenLabs credit
limit uses provider model rate information when available, and a conservative
1-credit/character fallback otherwise. The account quota is also checked.
Retries reserve budget again, as a conservative safeguard. The displayed
reserved credit amount is an estimate, not an invoice. Translation and text
rewriting use OpenAI separately and are not covered by the ElevenLabs limit.

ElevenLabs keys need the permissions used by this builder: Text to Speech,
Voices Read, Models Read, and User Read for the subscription/quota check.
Before new ElevenLabs-job translation or rewriting, the builder verifies the
subscription endpoint; an access failure stops before OpenAI processing.
Saved job scripts and translation/style caches remain reusable. The final
budget and model checks still run before speech generation. Provider errors
identify the operation and translate known reason codes into actionable hints;
raw provider messages, URLs, headers and credentials are never displayed.

All 97 E* error messages keep their localized wording and natural provider
settings. The 104 other lines can be rewritten; unchanged or source-plus-joke
outputs are rejected and repaired. Text chunks survive interrupted translation.
The rule editor changes future text generation and invalidates affected style
cache. A started job's saved script remains stable.

## Bring existing work across

For desktop cache migration, ZIP `prompt-cache`, `tts-cache`, and
`voice-aliases.json` from `%LOCALAPPDATA%\ANTHBOT Voice Pack Builder` while
preserving those directory names. Use **Korábbi Builder cache átvétele**.
Each ZIP may contain up to 64 MB of compressed and uncompressed content; split
large caches into several ZIPs. Windows-encrypted keys are ignored and must be
entered once in the web UI. Valid existing cache entries are retained.
ElevenLabs schema-2 cache request signatures preserve desktop property order.
Supported translated/styled 201-row scripts and local voice aliases migrate.

To import finished speech, add the matching recipe and choose **Korábbi csomag
átvétele**. This creates paused jobs. Import its exact 201-row script JSON,
then a ZIP/TAR containing the corresponding MP3s directly at the archive root.
Valid finished speech is copied without re-encoding. Partial speech archives
are supported; only missing files need generation. The three fixed assets, if
included, must match the originals. The import never extracts arbitrary paths,
links or executables.

## Deployment and storage

The add-on image installs FFmpeg/FFprobe and copies the new engine, worker, API,
page and original data assets. The default private state directory is
`/data/voice_builder`, alongside the reporting database; it contains encrypted
credentials, the encryption key, settings, the separate queue database, logs,
text/audio caches and job artifacts. Back up this complete directory together
with the add-on's existing data. No existing reporting tables, community pack
registry format, payment logic or public Store endpoints are replaced.

For standalone use, override `ANTHBOT_VOICE_BUILDER_DIR` if desired.
`ANTHBOT_VOICE_BUILDER_NO_WORKER=1` disables automatic process launch for tests.
The admin API lives under `/api/anthbot/admin/voice-builder`; writes require
`X-ANTHBOT-Builder: 1` and reject a foreign Origin. Provider redirects are not
followed, and HA download URLs must have the configured HA origin.

Validation includes the existing reporting/Store suite, real FFmpeg format and
fixed-asset checks, mocked-provider full 204-file build, cache reuse, pause/
resume, worker restart ownership, migration and idempotent Store publishing.
Live paid provider calls require the owner's keys and were not run as part of
the automated tests.
