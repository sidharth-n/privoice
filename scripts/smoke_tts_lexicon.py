"""Regression test: TTS must not silently delete words it doesn't know.

Why this test is shaped the way it is. kokoro-mlx 0.1.1 built misaki's G2P with
no espeak fallback, so out-of-lexicon words phonemized to `''` and were
synthesized as *silence*. Nothing raised, nothing logged where anyone would see
it, and the audio sounded completely fine — it was simply missing a word. Hours
went into "fixing the pronunciation" of a word that was never being pronounced.

So this test does not listen and it does not check for errors. It measures
**duration**, which is the signal that actually exposed the bug: `Say Privoice
now.` and `Say Sid now.` produced byte-identical output (33,600 samples each),
which no pronunciation difference could explain, while the in-lexicon control
`John` produced 39,000.

Two independent checks, because either alone can be fooled:
  1. the G2P returns real phonemes for the word (the cause)
  2. the word measurably lengthens the audio it is embedded in (the symptom)

    uv run python scripts/smoke_tts_lexicon.py
    TTS_ENGINE=kokoro-legacy uv run python scripts/smoke_tts_lexicon.py   # fails, by design
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engines import build_tts  # noqa: E402

# Words the product actually has to say. "John"/"Venice" are in Kokoro's
# lexicon and act as controls: if THEY fail, the test rig is wrong, not the TTS.
OUT_OF_LEXICON = ["Privoice", "Sid", "Kokoro", "Parakeet"]
CONTROLS = ["John", "Venice"]

CARRIER = "Say {} now."
BASELINE = "Say now."

# A syllable of speech is ~0.15 s = 3,600 samples at 24 kHz. Requiring 0.1 s of
# growth is comfortably above synthesis jitter and far below one short word, so
# it separates "spoken" from "deleted" without being sensitive to prosody.
MIN_GROWTH_SAMPLES = 2400


def audio_len(tts, text: str) -> int:
    return sum(len(chunk) for chunk in tts.stream(text))


def main() -> int:
    tts = build_tts()
    print(f"engine: {tts.name} @ {tts.sample_rate} Hz\n")
    tts.warmup()

    failures: list[str] = []

    # ---- check 1: the cause. Does the G2P produce phonemes at all? ----
    # Read through the engine's own pipeline where possible, so the test checks
    # the object the agent will actually speak with, not a fresh one built with
    # different arguments that might happen to be wired correctly.
    g2p = getattr(getattr(tts, "_pipe", None), "g2p", None)
    if g2p is None:
        print("g2p: not introspectable on this engine — skipping phoneme check")
    else:
        print("phonemes:")
        for word in OUT_OF_LEXICON + CONTROLS:
            try:
                ps, _ = g2p(word)
            except Exception as e:  # noqa: BLE001
                failures.append(f"g2p({word!r}) raised {type(e).__name__}: {e}")
                continue
            ok = bool((ps or "").strip()) and "❓" not in (ps or "")
            print(f"  {word:<10} -> {ps!r:<16} {'ok' if ok else 'EMPTY — will be deleted'}")
            if not ok:
                failures.append(f"{word!r} phonemizes to {ps!r}; it will be silent")

    # ---- check 2: the symptom. Does the word lengthen the audio? ----
    # The comparison is against the carrier sentence with the word removed, not
    # against the other words. Kokoro quantizes output to 600-sample boundaries,
    # so two genuinely-spoken one-syllable words ("John", "Sid") legitimately
    # land on the same length — an earlier version of this test flagged that as
    # deletion and was wrong. The real fingerprint is narrower and exact: audio
    # that matches the sentence with the word taken out.
    base = audio_len(tts, BASELINE)
    print(f"\ndurations (baseline {BASELINE!r} = {base} samples):")
    for word in OUT_OF_LEXICON + CONTROLS:
        n = audio_len(tts, CARRIER.format(word))
        growth = n - base
        ok = growth >= MIN_GROWTH_SAMPLES
        print(f"  {word:<10} {n:>6} samples  (+{growth:>5} vs baseline) "
              f"{'ok' if ok else 'NOT SPOKEN'}")
        if not ok:
            failures.append(
                f"{word!r} added only {growth} samples over {BASELINE!r} — "
                "the word is being dropped, not mispronounced"
            )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — every word reaches the synthesizer as real phonemes and "
          "measurably lengthens the audio.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        sys.stdout.flush()
    # MLX keeps thread-local Metal state; a clean interpreter exit is unreliable
    # here (issues/0008). The verdict is already printed.
    os._exit(code)
