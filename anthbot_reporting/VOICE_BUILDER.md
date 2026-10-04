# Server Voice Builder

Reporting Server **1.0.52** provides **Dashboard → Voice Builder** for administrators.
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
   the full bundled Cloud catalog contains 489 voices across 150 locales.
3. Generate **A004/A005 previews**, then start a full batch. The full builder
   reuses the preview TTS cache. Generated output is validated as MP3,
   16 kHz, mono, 32 kbit/s. The three original non-speech assets A001/A003/A030
   remain byte-identical; their original bitrate is preserved.
4. Successful full builds automatically upload to Hangbolt through the existing
   community pack/version allocation path, even after the browser closes.
   Previews are not uploaded. Download the complete TAR.GZ, technical manifest
   and public catalog at any time after completion. Newly created packs start
   hidden. Review the two samples and set price and
   visibility in **Hangbolt**. Republish of the same identity preserves its
   price, visibility and existing access rules.
   If upload fails, the completed pack stays downloadable and the UI offers
   **Feltöltés újrapróbálása**; no new speech generation is needed. Restarting the
   add-on recovers completed, unuploaded builds whose upload has not failed.
   Automatic and manual retries use the same idempotent publisher.
   Older completed builds lacking final text approval show **Ellenőrzés és
   feltöltés**, including those that were never uploaded. Listing jobs only
   checks local proof and never calls a provider. The recovery backs up the
   original script and archive, checks the saved candidate, reuses unchanged
   audio, rebuilds the validated artifact and follows the batch's upload
   setting. Already verified text needs only a local rebuild. A missing styled
   QA pass or genuinely corrected speech may incur a new provider charge;
   translation and full-script rewriting are not repeated. Ordinary upload
   failures continue to offer the upload-only retry.
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
Explicit rejected requests release their local budget reservation; ambiguous requests keep it. The displayed
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
settings. The 104 other lines can be rewritten. The full desktop 7.20.11 linguistic,
semantic and character QA runs before TTS, followed by targeted repairs and
re-verification of failed rows only (five rounds and three internal draft alternatives
by default, adjustable in the rule editor). E* wording stays exact; source review
flags survive rewriting. Freshness rejects punctuation-only changes, source-prefix
appendages and unchanged opening words. Funny/Wild-Funny require 104/104
materially changed non-error rows. A failed gate blocks TTS and upload.
The original desktop instructions are bundled verbatim. Entire 201-row source
context is translated with the Responses API in one saved request. English
variants reuse the corrected master; exact trusted scripts take precedence.
Translation is shared by target code (including the Chinese variants), and
regional target overrides are supported. Existing drafts receive QA without
repeating translation or whole-script rewriting.
The rule editor changes future text generation and invalidates affected style
cache. A started job's saved script remains stable.
Translation and style caches are shared across voices and character effects
for the same target/base, locale, text style, delivery and rules. Resume
keeps valid finished MP3s and generates only missing audio; upload retries run
only publishing. The log explicitly identifies reused scripts and text chunks,
instead of displaying cached chunks as new text processing.
Paid text/TTS calls also persist a request intent before sending and save their
responses before further processing. Timeouts, server errors and interrupted
calls with unknown outcomes are never sent again on automatic retry or resume:
the job stops for provider-side checking or result import. Explicit rejection
responses allow correction and retry; only HTTP 429 is retried automatically.
This prevents the builder from blindly paying for the same uncertain request
twice; it cannot control the provider's accounting or retrieve a lost response
without provider support. Preserve the entire state directory, including
`paid-requests` and `paid-text-results`, when backing up or upgrading.

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
Valid finished speech is copied without re-encoding. Other valid raw MP3s are
normalized locally without a provider request. Partial speech archives
are supported; only missing files need generation. The three fixed assets, if
included, must match the originals. The import never extracts arbitrary paths,
links or executables.

## Additional desktop operations

The UI supports per-locale voice selections and translation target overrides,
selected/first/all voices, saved selections, 4096-recipes batches, skip-completed,
continue-on-error and optional upload. The default remains automatic upload.
Text-only and audio-only stages are available independently; validate, build from
finished audio and export MP3 ZIP do not call TTS. The classic themes and the
six original Funny override scripts are retained separately from the full
104-row character pipeline. Model and manual voice/profile entries remain editable.
ElevenLabs voice discovery uses paginated v2 results with descriptive metadata.
Fish lists owned and licensed public trained TTS models, with search/filtering.
Cloud native style variants and local delivery fallback remain available.
The style-rule editor can restore the desktop defaults. Logs follow only when
already at the bottom. Desktop `selection_preset.json` imports locale/voice/model/
style/batch choices while retaining encrypted server credentials. Cached aliases
remain stable; new ElevenLabs aliases can use once-per-voice OpenAI local-name
generation, with a durable deterministic fallback. Unknown paid alias outcomes
are not retried. Public display names never change entitlement identity.
After upload, `voice_set.json` contains the allocated version and MD5. Paid
packs require a licensed installation URL issued by the Store; the generic
admin template intentionally has no reusable public paid download URL.

## Previously uploaded server packs

At startup and through the admin review list, the migration examines only
server Builder jobs linked to an uploaded current revision. It requires matching
community identity, pack ID and payload MD5. It never infers ownership from a
similar display name, and does not change desktop/bundled packs or newer
independent uploads. Styled server revisions without final QA proof are backed
up before being hidden and marked for review. Files, original metadata, prices,
access and sales records are retained. The backup ZIP is downloadable from the
job. Hidden paid packs remain accessible through existing purchase entitlements.

**Korábbi csomagok ellenőrzése és feltöltése → Ellenőrzés és feltöltés** processes the
saved candidate, not a new translation. Good rows and audio remain intact.
Only corrected text loses its old per-job MP3, after that file is backed up;
unchanged rows never request TTS. A different verified shared script cannot
silently replace an existing candidate. Final QA, MP3/204-file checks and upload
proof must pass before the replacement is published under the same community
identity. Previous Store revisions are backed up before core upload removes
its superseded file. Replacements stay hidden for listening and manual release;
existing purchases resolve through the unchanged community identity.

A new QA pass and genuinely changed speech can incur provider charges; this is
not a replay of an already completed translation/TTS request. Saved QA, repair
and alias responses are also reusable. The server cannot refund earlier provider
charges, infer which packs were installed on individual mowers, or automatically
undo a mower installation. The update provides local review and replacement,
not an assertion that every earlier package was linguistically defective.

The desktop's force-regenerate paid bypasses are intentionally not carried over:
the user's requirement is to reuse every completed result. Refresh/validate/
repair never silently bypass a valid cache or ambiguous-request journal. The
Windows DPAPI and GUI process details use the existing Linux encrypted storage
and durable worker equivalents; no Windows paths are required on the server.

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
