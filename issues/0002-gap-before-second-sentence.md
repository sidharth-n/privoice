---
id: 0002
title: Audible gap before the second sentence (TTS generates synchronously)
status: closed
priority: urgent
area: tts/playback
opened: 2026-07-30
updated: 2026-08-12
closed: 2026-08-12
---

## What

Speech stalls between sentences. The first sentence plays, then there is a pause, then
the second begins. Sid: *"generating the second sentence after one"*.

## Why it matters

Human speech does not pause 200-300 ms between every sentence. This reads as the
machine thinking rather than talking, and it is one of the two things Sid named as
making the agent feel unnatural.

## Evidence

`TTSPlayer.speak_sentence()` generates **and** plays in one synchronous pass, and
`respond()` calls it per sentence in a loop:

```python
for sent in sentence_stream(stream_llm(...)):
    player.speak_sentence(sent)     # generates AND blocks until played out
```

So sentence N+1's synthesis only starts after sentence N has finished playing.
Kokoro measured at 14.7x realtime, so a ~3 s sentence costs ~200 ms to generate —
which becomes dead air, every sentence boundary.

## Approach

Decouple generation from playback:

- Replace the blocking `OutputStream.write()` with a **callback-driven output stream
  fed from a ring buffer**, so playback drains audio while the worker generates ahead.
- Generation must stay on the single worker thread — MLX thread-local Metal state
  makes a second MLX thread unsafe (see commit b8be6e1: thread teardown crashes the
  interpreter). The *playback* callback does no MLX, so it is safe.
- **The AEC reverse stream must move with playback.** `process_reverse_stream()` has
  to be fed the frames the speaker actually emits, in 10 ms frames at 16 kHz, or echo
  cancellation breaks. This is the risky part and the reason it was not bundled with
  other fixes.
- Prefetch depth of one sentence is probably enough; verify against `bench_stack.py`
  TTFA and by ear.

Do not attempt alongside other changes — this touches the mechanism [[0001]] depends
on.

## Resolved — 2026-08-12

Synthesis and playback shared a thread, so sentence two could not begin
synthesizing until sentence one had finished *playing*. Device writes moved to a
dedicated playback thread (`TTSPlayer._play_loop`); synthesis now runs up to
`TTS_MAX_AHEAD` (2) sentences ahead of the speaker.

`issues/0009` had to land first, and did: pre-synthesizing sentence two is
pointless while its tokens have not been requested.

The metric changed with the fix, which matters when comparing rows. The old
number used the synthesis time of later sentences as a proxy for the gap — valid
only while synthesis happened *after* playback. It is now measured directly as
first-frame(N+1) minus last-frame(N), both stamped by the playback thread, and
logged as `gap_ms`. `analyze_turns.py` reports the two under separate labels so
the fix cannot be read as a regression.

    gap heard between sentences: 0 ms median over 41 sentences   (was 293 ms)

TTS realtime factor also rose from ~6.3x to ~9-11x, because synthesis is no
longer interleaved with blocking device writes.
