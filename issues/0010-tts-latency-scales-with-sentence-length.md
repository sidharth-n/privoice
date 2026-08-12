---
id: 0010
title: TTS time-to-first-audio scales linearly with first-sentence length (~9 ms/char)
status: open
priority: moderate
area: tts/latency
opened: 2026-08-12
updated: 2026-08-12
closed:
---

## What

Kokoro yields no audio until it has synthesized the **entire sentence**. So
time-to-first-audio is a linear function of how long the reply's first sentence
happens to be, and the LLM's choice of opening phrasing sets the floor on
perceived latency.

## Why it matters

It is the largest single lever on this pipeline's felt latency, and it is not a
slot-swap decision — it is a prompting decision.

Across a real conversation the same configuration produced **131 ms** to first
audio on a reply opening "Yeah, totally." and **1,610 ms** on one opening with a
165-character sentence. A 12× spread on identical hardware, identical config,
decided entirely by how the model started writing.

For comparison, the entire local-vs-hosted TTS difference documented in
`docs/BENCHMARK.md` is 329 ms vs 1,168 ms. Sentence length moves first-audio by
more than switching substrate does.

## Evidence

16 turns, hybrid config, 2026-08-12. Time-to-first-audio against character
count of the first sentence:

```
tts_first_ms ≈ 101 ms + 9.1 ms per character      (R² = 0.94, n = 15)
```

One turn sat far off the line at 3,776 ms and is excluded; including it the fit
is 13.3 ms/char at R² = 0.52. That outlier is unexplained and may be a separate
contention problem — worth a look before assuming the model is clean.

The near-linear fit with a small intercept is itself the finding: there is no
meaningful fixed startup cost and no intra-sentence streaming. Cost is
proportional to text, which is what "synthesize the whole sentence, then emit"
predicts.

Raw rows: `turn_log.jsonl`, `sentences[0].synth_first_ms` against
`len(sentences[0].text)`.

## Approach

Cheapest first, and the first one may be enough:

1. **Prompt for a short opener.** Add a line to `SYSTEM_PROMPT` asking the
   first sentence of every reply to be brief. Costs nothing, changes no code,
   and on these numbers a 40-character opener instead of 120 saves ~700 ms.
   Verify with `scripts/analyze_turns.py` before and after rather than by ear.
2. **Split the first sentence further for synthesis** — synthesize the first
   clause, start playing, synthesize the rest underneath. Prosody across the
   join is the risk and needs listening to, not measuring.
3. **Check whether kokoro-mlx can stream within a sentence** at all. If it can
   emit per-phoneme-chunk, most of this disappears; `engines.py` currently
   consumes `tts.stream()` as whatever granularity it offers.

Interacts with `issues/0009` and `issues/0002` — all three are about audio
starting later than the data allows.
