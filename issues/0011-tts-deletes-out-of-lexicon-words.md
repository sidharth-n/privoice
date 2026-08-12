---
id: 0011
title: TTS silently deletes words outside Kokoro's lexicon, including "Privoice" and the user's name
status: closed
priority: urgent
area: tts
opened: 2026-08-12
updated: 2026-08-12
closed: 2026-08-12
---

## What

`kokoro-mlx` 0.1.1 constructs misaki's G2P with **no espeak fallback**. Any word
outside Kokoro's built-in lexicon phonemizes to the empty string and is then
synthesized as *silence*. The word is not mispronounced — it is removed from the
sentence, with nothing raised and nothing logged where a user would see it.

The agent therefore could not say its own name, or its user's.

## Why it mattered

It is the first thing a person cloning the repo hits, because the first thing
they do is say their own name. It also affected the launch video, whose
narration silently omitted "Privoice" from every line.

## Evidence

Synthesizing a carrier sentence with the word swapped in, `kokoro-mlx` 0.1.1:

```
Say Privoice now.  -> 33600 samples
Say Sid now.       -> 33600 samples     <- identical, to the sample
Say Kokoro now.    -> 33600 samples     <- identical
Say John now.      -> 39000 samples     <- in lexicon, spoken
Say Venice now.    -> 42000 samples     <- in lexicon, spoken
Say now.           -> 33600 samples     <- the baseline they all collapsed to
```

Three different words producing byte-identical output equal to the sentence with
no word in it at all. Confirmed at the cause:

```python
misaki.en.G2P(fallback=None)("Privoice")  -> ''
misaki.en.G2P(fallback=EspeakFallback())("Privoice")  -> 'pɹˈɪvYs'
```

`mlx_audio`'s `KokoroPipeline` builds `misaki.espeak.EspeakFallback` itself, and
logs `"EspeakFallback not Enabled: OOD words will be skipped"` when it cannot —
a warning that fires at construction, into `logging`, behind model-download
progress bars, and that nobody has ever seen.

## Fix

`engines.py`'s `KokoroTts` now drives `mlx_audio.tts.models.kokoro.KokoroPipeline`
instead of `kokoro_mlx.KokoroTTS`.

- Time-to-first-audio is unchanged: 170 / 264 / 446 ms at 13 / 34 / 72
  characters, against 190 / 217 / 442 ms for the old engine — inside noise.
- The old path stays reachable as `TTS_ENGINE=kokoro-legacy`.
- `_check_g2p_fallback()` turns the silent warning into a **startup error**,
  with `KOKORO_ALLOW_NO_ESPEAK=1` as a deliberate override. A failure this quiet
  has to be made loud at the one moment it is cheap to fix.
- `espeakng-loader`, `phonemizer-fork` and `misaki` are now declared directly in
  `pyproject.toml`. They were only ever present transitively, which is precisely
  how a fresh clone would have reproduced the bug.

## Regression test

`scripts/smoke_tts_lexicon.py`, which checks the cause and the symptom
separately: that the G2P returns real phonemes, and that the word measurably
lengthens the audio it sits in. It passes on `kokoro` and fails on
`kokoro-legacy`, which is what makes it worth having.

Note the test compares against the carrier sentence *with the word removed*, not
against the other words: Kokoro quantizes output to 600-sample boundaries, so
two genuinely-spoken one-syllable words legitimately collide on length. An
earlier draft flagged that as deletion and was wrong.

## Related

`issues/0007` is the STT-side twin — proper nouns the recognizer cannot hear.
This was the TTS side: proper nouns the synthesizer refuses to say.
