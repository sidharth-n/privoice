# venice-voice-agent — State

_Last updated: 2026-08-12_

Forked from `uncensored-local-voice` (the fully on-device build) to add hosted
slots and measure the two against each other. Everything before the fork is in
`state-log.md`.

## Now

**2026-08-12 (later): live turns are now instrumented, and the real numbers
are worse than the synthetic ones.** Every turn appends a full stage breakdown
to `turn_log.jsonl` (`turnlog.py`); `scripts/analyze_turns.py` reports medians
and p90 per stage grouped by config; `scripts/smoke_turnlog.py` is the
multi-turn regression test for the path.

16 real conversational turns on hybrid: **2,143 ms median / 3,007 ms p90** to
first audio, against the 1,381 ms the per-slot benchmark predicted. Ranking
unchanged, magnitude 1.55× optimistic. Three findings, all invisible to
`bench_stack.py`:

- STT costs **106 ms per second of speech** in-pipeline vs ~29 ms/s isolated
  (`issues/0004`, now with real numbers).
- TTS time-to-first-audio ≈ **101 ms + 9.1 ms per character** of the reply's
  first sentence, R²=0.94 — so the model's opening phrase is the biggest
  latency lever in the system (`issues/0010`, new).
- The LLM stream is **not drained during playback**, so sentence two's tokens
  are not even requested until sentence one finishes speaking (`issues/0009`,
  new — the larger half of `issues/0002`, and must land first).

Written up in `docs/BENCHMARK.md` → "What a real conversation measures".
Still outstanding from this: the all-local and all-hosted *conversational*
runs have not been done, so only the hybrid magnitude is corrected.

## Earlier today

The Venice port is done and measured. Every slot (STT, LLM, TTS) swaps between
on-device and Venice's hosted API by env var. **The local path is still the
default and is unchanged** — the port is purely additive, so nothing regressed.

The benchmark is the point of the repo, and it is finished:

| Configuration | Warm | First turn after idle |
|---|---|---|
| All local | 1,642 ms | 7,824 ms |
| All Venice | 3,672 ms | 3,672 ms |
| Hybrid (local STT/TTS + Venice LLM) | **1,381 ms** | **1,381 ms** |

Headline result: all-hosted is 2.2× slower than the laptop, and the cause is a
fixed **~400 ms of server-side latency per API request** — isolated on a static
`GET /models` that needs no auth, survives keep-alive connection reuse, and sits
behind a 37 ms TCP connect. A voice turn is three sequential calls, so ~1.2 s of
pure overhead before any token is generated. Cold start reverses the warm
conclusion: local pays a measured ~7.4 s model load on the first turn after
idle, hosted pays none.

Venice serves the same Parakeet and Kokoro checkpoints, so STT and TTS are
controlled comparisons. The LLM row is not — see Known-soft claims.

Last verified: `scripts/bench_stack.py` run end to end on the M5 today against
both backends, 5 samples per slot; `smoke_pipeline.py` green in all three
configurations and producing real reply audio.

## Next

1. **`issues/0010` — prompt for a short first sentence.** A `SYSTEM_PROMPT`
   line, no code. On the measured 9.1 ms/char it is worth several hundred ms
   off every reply. Verify with `analyze_turns.py` before/after, not by ear.
2. **`issues/0009` — drain the LLM stream concurrently with playback.** Then
   `issues/0002`. Constraints in the issue: barge-in cancel must reach the
   producer, and no new thread per turn.
3. **Conversational runs for all-local and all-hosted**, so the local-vs-hosted
   comparison rests on the same kind of data the hybrid number now does.
4. **Record a demo.** A voice project with no audible artifact is unpersuasive.
   `out_reply.wav` exists, but a screen recording of a live barge-in turn in
   hybrid mode is what the README actually needs.
5. **Rotate the Venice API key** — the one in `.env` was pasted into a chat
   transcript during development.
6. Longer-generation benchmark. Every current number uses one short prompt,
   which structurally favours the slower decoder (local). Longer replies should
   shift the balance toward Venice; untested.
4. Concurrency. All measurements are single-client and sequential, so the main
   advantage of a hosted API is invisible in them.
5. A streaming/WebSocket path if Venice ever ships one — the per-request tax is
   what makes REST unusable for continuous voice.

## Blockers

- None technical. The repo runs in all three configurations.

## Known-soft claims

- The LLM row is **not** a controlled comparison (local 26B Q4 GGUF vs
  `venice-uncensored` — different models on different hardware). Stated plainly
  in both the README and `docs/BENCHMARK.md`; do not let it drift into being
  quoted as like-for-like.
- All numbers come from one machine, one network, one city, one day.
- Nothing here measures output quality. Latency only.
