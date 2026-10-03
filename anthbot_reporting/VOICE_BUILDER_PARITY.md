# Desktop alpha.7.20.11 → Reporting Server 1.0.51

This is the functional mapping used to close the incomplete 1.0.48–1.0.50 port.
The source is the supplied desktop 7.20.11 package. Windows UI, DPAPI and tar.exe
are replaced by the server UI, encrypted persistent storage and Linux worker/tools.

| Desktop function / feature | Server equivalent |
|---|---|
| `Ensure-PromptFile`, `New-TranslationInstruction` | `voice_builder_text.base_document`, exact trusted sources, English master, target-shared cache, full 201-row contextual Responses request |
| `New-StyleRewriteInstruction` | Verbatim bundled instruction, cached draft, full source context |
| `New-StyleVerifyInstruction` | Independent 201-row grammar, meaning and character QA before any TTS |
| `New-StyleRepairInstruction`, `New-StyleRepairVerifyInstruction` | Failed rows only, configurable 1–10 rounds / 1–5 alternatives, saved repair and QA responses |
| `Test-CriticalPromptName`, `Test-StyledDocumentAgainstBase` | Original critical-message asset, exact E* text, source review flags, exact filenames/order |
| `Test-FreshStyleRewrite`, `Get-MinimumCharacterRewriteRatio` | Punctuation/symbol normalization, source-prefix/opening-word rejection, character strength gate, Funny/Wild-Funny 104/104 |
| `Ensure-StyledPromptFile`, rule hash/defaults editor | Desktop-compatible schema-6 proof/hash validation, voice-independent verified cache, editable rules/reset |
| Alias generation/cache/reserved names | Once-per-locale/voice AI alias, journaled response, deterministic fallback, imported aliases preserved |
| Full Nabu Casa catalog | 150 locales / 489 voices, native style variants, per-locale choice and target overrides |
| ElevenLabs voice/model catalogs | v2 paginated search and metadata; live model/quota checks before paid generation |
| Fish owned/public model search | Pagination, trained TTS filtering, search and licensed-only public filter, manual reference IDs |
| Four TTS providers / hybrid / delivery / character | Existing equivalent provider signatures and filters; E* neutral settings; original cache schema/order |
| Classic profiles / themes / Funny overrides | Bundled original profile/theme assets and six override scripts, separate classic selector |
| Persistent presets / batch scope / skip / error options | Server autosave, desktop v5 preset import, selected/first/all/per-locale voices, 4096 recipes, completed skip, pause-on-error option |
| Translation-only / preview / generate / import / validate / build | Independent text/audio stages, A004/A005 samples, partial/raw import with free normalization, diagnostics, build from ready MP3s |
| MP3/fixed assets/204-file/hash/manifest checks | FFprobe/FFmpeg, 16 kHz mono 32 kbit/s speech, three original SHA-256 assets, deterministic TAR.GZ, MD5/SHA-256, public catalog and allocated `voice_set.json` |
| Server upload / skip / version assignment | Shared automatic/manual idempotent publisher, upload-only retry, same stable identity and current Store price/access rules |
| Log follow and durable output | Follow only at bottom, resumable process-owned queue, saved provider results before processing |
| Existing defective server uploads | Added review migration: exact current revision evidence, backup-before-hide, QA existing draft, replace only changed audio, same entitlement identity, downloadable original backup |

The user's no-repeat-payment instruction overrides the desktop's force-paid-
regenerate toggles and blind retries after unknown provider outcomes. The server
keeps completed responses and stops ambiguous requests instead. A fresh QA or a
genuinely changed line may incur a new charge; an existing successful response
is reused. No production provider calls or live server pack mutations were
performed during development. The local migration runs after installing this
update; affected packs remain hidden until the administrator releases them.
