---
id: 0010
title: TTS time-to-first-audio scales linearly with first-sentence length (~9 ms/char)
status: closed
priority: moderate
area: tts/latency
opened: 2026-08-12
updated: 2026-08-12
closed: 2026-08-12
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

34 turns, hybrid config, 2026-08-12. Time-to-first-audio against character
count of the first sentence:

```
tts_first_ms ≈ 118 ms + 7.9 ms per character      (R² = 0.86, n = 30)
```

One turn sat far off the line at 3,776 ms and is excluded; including it the fit
is 11.0 ms/char at R² = 0.50. That outlier is unexplained and may be a separate
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
   and on these numbers a 40-character opener instead of 120 saves ~630 ms.
   Verify with `scripts/analyze_turns.py` before and after rather than by ear.
2. **Split the first sentence further for synthesis** — synthesize the first
   clause, start playing, synthesize the rest underneath. Prosody across the
   join is the risk and needs listening to, not measuring.
3. **Check whether kokoro-mlx can stream within a sentence** at all. If it can
   emit per-phoneme-chunk, most of this disappears; `engines.py` currently
   consumes `tts.stream()` as whatever granularity it offers.

Interacts with `issues/0009` and `issues/0002` — all three are about audio
starting later than the data allows.

## Resolved — 2026-08-12

Approach 1 was enough. The old prompt constrained the *reply* ("1 short
sentence, sometimes 2") and left the opener free; the new one names the first
sentence specifically and pushes detail into later sentences.

Measured over 10 varied prompts against `venice-uncensored`:

| prompt | opener median | p90 | max | projected TTS first-audio |
|---|---|---|---|---|
| old | 46 chars | 108 | 142 | 481 ms |
| new | **19 chars** | **31** | **35** | **268 ms** |

The tail matters more than the median here: the p90 of 108 characters is what
produced the pipeline's 2,757 ms p90 first-audio, and it is gone.

Measured end to end, TTS first-audio fell from 302/813/1115 ms to 140/159/179 ms
on the same smoke prompts, and to 194 ms median over 20 replayed turns.

This is only affordable because `issues/0002` landed first. Later sentences are
now synthesized underneath audio that is already playing, so their length costs
nothing — before that change, "put the detail later" would have moved the cost
rather than removed it.

Approaches 2 and 3 (splitting the first sentence at a clause boundary, and
checking whether kokoro can stream within a sentence) were not needed and remain
available if the opener ever has to get shorter still.
