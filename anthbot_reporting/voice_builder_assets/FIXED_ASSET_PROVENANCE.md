# Fixed Genie asset provenance

Builder version: `0.1.0-alpha.7.20.11`

A `base-assets/genie_slot3_community/` mappában lévő három fix, nem beszélt fájl a felhasználó által az eredeti Genie hangcsomagból kinyert példány. A Builder ezeket byte-ra változatlanul, pass-through módon csomagolja; nem normalizálja és nem kódolja át őket.

## Fájlok és ellenőrzőösszegek

- `A001.mp3` — SHA-256 `33c93e4390da546d77d745f0d1c0119184101837dc5902c996a3d29e15b58b0f`
- `A003.mp3` — SHA-256 `676e4d10dfdcf4c74a7db9a2de099c3f3e979f67a37cf44634efccb28603f218`
- `A030.mp3` — SHA-256 `c077b272367dd010189f47e994dc8cb03b30a6613715fc4922467b77643d6d58`

## Mért audioformátum

Mindhárom fájl: MPEG Layer III (MP3), 16 000 Hz, mono, 48 kbit/s.

A `Test-FixedMp3` nem egy mesterségesen előírt bitrate-profilt kér számon: a három konkrét beépített fájl SHA-256 azonosságát és azt ellenőrzi, hogy ffprobe szerint valódi MP3 audio stream olvasható belőlük. Így a fix fájlok sem véletlenül nem cserélődhetnek ki, sem nem kerülnek újrakódolásra.
