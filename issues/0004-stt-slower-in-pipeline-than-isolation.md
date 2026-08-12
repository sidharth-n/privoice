---
id: 0004
title: STT is 5-10x slower in the live pipeline than in isolation
status: open
priority: low
area: stt/threading
opened: 2026-07-30
updated: 2026-08-12
closed:
---

## What

Parakeet transcribes far slower inside the running agent than the same model on the
same audio in a standalone script.

## Why it matters

~700 ms of unexplained per-turn latency. If it is contention, fixing it is free
speed; if it is real, the STT benchmark numbers used to pick the default are
misleading.

## Evidence

Isolated (`compare_stt.py` / direct loop, 5 runs, median):

```
 4s audio ->  76 ms
 8s audio -> 104 ms
20s audio -> 276 ms
45s audio -> 977 ms
```

Live session 2026-07-30, with audio length now logged:

```
[stt 1004ms on 1.1s]
[stt  767ms on 7.0s]
[stt  710ms on 4.5s]
[stt 2569ms on 1.0s]
[stt 1177ms on 3.2s]
```

1.1 s of audio taking 1004 ms is ~13x the isolated figure for that length, and the
cost does not track length — which points at contention rather than workload.

## Approach

Hypotheses, cheapest first:

1. **Main-thread/worker contention.** The main loop runs Silero VAD (torch) on every
   32 ms frame while the worker runs Parakeet (MLX). Both compete for GPU and the GIL.
   Test by timing STT with the mic loop idle vs busy.
2. **Cold Metal kernels per call.** The isolated test warms up once then loops
   immediately; live calls are seconds apart. Check whether a periodic keep-warm
   inference restores the fast path.
3. **Temp-wav overhead.** `MlxAudioStt.transcribe` writes a wav per call because
   mlx-audio's `generate()` takes a path. Should be microseconds, but confirm.
4. Consider whether `mlx-audio` re-runs setup per `generate()` call.

Note the isolated numbers were what justified making Parakeet the default over
Moonshine (396 ms). Even at live cost, Parakeet still wins — Moonshine measured
812-1626 ms in the same live conditions — so the decision stands, but the margin is
smaller than the benchmark implied.

## Re-measured 2026-08-12 — mostly not reproducible

Chased every hypothesis above. The 5–10x systematic slowdown does not reproduce;
what is left is an occasional spike.

**Isolated, on real `say` speech** (not the synthetic tones an earlier probe
used — Parakeet is a transducer and it was worth ruling out that decode cost
tracks what it emits; it does not, silence and speech of equal length cost the
same):

```
 2.0-2.5s clips -> 48-52 ms, tight across 5 runs each
 5.4s clip      -> 84 ms
```

**Contention, added one layer at a time in a single process**, transcribing the
same 2.4 s clip:

```
A. nothing else running                       82 ms
B. + OutputStream open, idle                  89 ms
C. + InputStream running (mic callbacks)      72 ms
D. + mic queue drained on a thread            90 ms
E. + APM + Silero VAD per frame (real loop)   46 ms
```

Hypothesis 1 (main-thread/worker contention) is therefore wrong — the full
real-loop configuration was the *fastest* of the five. Hypothesis 3 (temp-wav
overhead) is wrong: the write is 0.2 ms median. Hypothesis 2 (cold Metal kernels
per call) is wrong: a warmup ladder across five durations made no difference to
first-call cost, and a 6 s idle gap between calls costs ~20 ms.

**In the live loop**, across 30 replayed turns, STT is 74 ms median / 49 ms min.
Two of three runs showed no spikes at all; one run showed five turns at
390–528 ms. That run was initially blamed on speculative dispatch and it was not
— re-running with speculation on reproduced 66 ms median with no spikes, and the
spec-off control ran in between. Machine state, not the pipeline.

## Where that leaves it

The original evidence (1004 ms on 1.1 s of audio) came from a session in
2026-07-30 that also predates the current threading model, and STT was 5–10x
slower then in a way it is not now. Dropped to low priority: the median is
within 30 ms of isolated, STT is 7% of the critical path, and there is no
reproducible defect to fix.

Worth keeping open only for the spikes. If they matter, the thing to capture is
what else the machine was doing — this needs a reproduction before it needs a
hypothesis.
