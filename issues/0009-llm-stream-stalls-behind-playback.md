---
id: 0009
title: LLM stream is not drained while audio plays — sentence 2 is never requested early
status: open
priority: urgent
area: llm/playback
opened: 2026-08-12
updated: 2026-08-12
closed:
---

## What

Tokens are pulled from the LLM lazily through `sentence_stream`, and
`TTSPlayer.speak_sentence()` blocks for the full duration of playback. So
while sentence one is being spoken, nothing is reading the LLM stream. The
model may well have finished generating; we have not asked for the bytes.

The generation and the speaking of a reply are strictly serialised when they
could overlap almost completely.

## Why it matters

Two consequences, one measurement and one real.

**Real:** this is the larger half of `issues/0002`. That issue attributes the
gap before sentence two to TTS synthesizing synchronously. But at the instant
sentence one stops playing, sentence two's *tokens* have not been requested
either — so the gap is LLM round-trip plus TTS synthesis, not TTS alone.
Pre-synthesizing audio without also draining the LLM concurrently will only
close part of it. Measured gap before later sentences: **376 ms median** over
9 sentences.

**Measurement:** `llm_chunk_s` and `llm_total_ms` in `turn_log.jsonl` are only
meaningful on single-sentence turns. On multi-sentence turns they measure the
rate of speech, not the rate of the model, and understate it by ~24×.

## Evidence

16-turn hybrid conversation, 2026-08-12, split by reply shape:

| Reply | Measured decode rate | n |
|---|---|---|
| Single sentence (drained before playback starts) | **110 chunks/s** | 7 |
| Two or more sentences (drained between sentences) | **4.8 chunks/s** | 9 |

110 chunks/s agrees with the 115 tok/s that `scripts/bench_stack.py` measured
for `venice-uncensored` with no playback in the loop, which is what confirms
the 4.8 figure is backpressure rather than the model.

Reproduce: `uv run python scripts/analyze_turns.py` after any conversation with
multi-sentence replies; compare the decode rate against `bench_stack.py --slot llm`.

## Approach

Drain the token stream on its own thread into a queue, and let the sentence
splitter consume from that queue rather than directly from the HTTP response.
Playback then blocks the consumer without blocking the producer.

Constraints that must survive the change:

- Barge-in must still cancel generation promptly — the cancel `threading.Event`
  has to reach the producer thread, not just stop the consumer.
- No new thread per turn. `learning.md` (2026-07-30) documents the fatal GIL
  crash from tearing down thread-local MLX state; the producer must be a
  long-lived worker or a plain `httpx` read loop that never owns MLX state.
- Interrupted replies must still record only what was actually heard, so the
  buffer cannot be treated as "spoken" merely because it was received.

Land this before `issues/0002`: pre-synthesizing sentence two is pointless
while its tokens are still unrequested.
