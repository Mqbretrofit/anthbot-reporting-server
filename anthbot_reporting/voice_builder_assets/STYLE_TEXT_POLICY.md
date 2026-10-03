# ANTHBOT Voice Pack Builder — personality text policy

Builder version: `0.1.0-alpha.7.20.4`

## Goal

A character pack is not just an audio filter. For every non-critical message, the selected **Text style + Delivery / emotion** may change the wording, rhythm, punctuation and length so the personality is already obvious before the audio effect is applied.

Examples of the intended behavior for a simple presence/status line:

- Standard + Natural: `Itt vagyok.`
- Standard + Emotional: a warmer, emotionally engaged target-locale sentence.
- Standard + Sensual: a tasteful, clearly flirtatious/sensual target-locale sentence, potentially with a short extra phrase or natural pause.
- Standard + Hard: terse, confident and commanding wording.
- Funny + Natural: genuinely funny robot/garden wording, not a generic “oops” substitution.
- Wild funny: stronger absurd robot humour while the real mower state remains clear.

These are behavior examples, not fixed Hungarian strings to translate literally. Every locale is written natively for that language/culture.

## Combined rewrite

The Builder rewrites text whenever either:
1. the selected Text style is non-Standard, or
2. the selected Delivery / emotion is non-Natural.

The cache key therefore includes **locale + text style + delivery style**. Changing only `Érzéki` to `Kemény`, for example, creates a different verified script.

## Character strength gate

After the first AI rewrite, the Builder measures how many eligible non-critical lines changed materially (punctuation-only changes do not count). Characterful modes have a minimum rewrite ratio. If the first pass is too bland, one automatic **stronger rewrite pass** runs before the independent QA pass.

## Two-pass QA

1. **Creative native-locale rewrite:** meaning is preserved but character is allowed to change wording and length on safe messages.
2. **Independent QA:** grammar, naturalness and semantic equivalence are checked. The QA prompt is explicitly told not to “correct away” safe personality merely because a sentence became longer, funnier, flirtier, warmer or tougher.

Any genuinely doubtful line falls back to the original localized source.

## Locked prompts

- every `E*.mp3` error message;
- every additional filename listed in `critical-messages.json`;
- every base translation line already marked `review_required=true`.

Locked prompts remain character-for-character identical.

## Audio delivery and effects

After the verified script is spoken:
- a native Home Assistant Cloud expressive voice variant is requested where the selected voice actually supports one;
- otherwise the Builder uses a stronger local FFmpeg delivery fallback;
- the independent character effect (Chipmunk, Cartoon Duck, Robot, Deep, etc.) is applied afterward.

Local FFmpeg processing can change cadence/tone, but it is not a substitute for a truly expressive neural TTS. That is why alpha.7.19.2 also changes the wording itself.

## Output

Spoken files remain MP3 / 16 kHz / mono / 32 kbit/s. The three fixed Community non-speech files remain unchanged.


## External editable rules (7.20.4)

Text personality wording policy is no longer hard-coded only in PowerShell. `style-rules.json` is the mutable rule source, with `style-rules.defaults.json` as factory defaults. The GUI can edit the selected style rule directly. Rule SHA-256 participates in styled-prompt cache validation. Active rewrites are required to be fresh formulations, not source sentence + appended personality text; local source-prefix checks and targeted repair enforce this.
