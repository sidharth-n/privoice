# Privoice

A real-time voice agent. Mic in → spoken reply out. Every slot (STT, LLM, TTS)
runs either on-device or on Venice's hosted API, selected by env var. Forked
2026-08-12 from `uncensored-local-voice`, which was local-only.

**Measured latency (2026-08-12, this M5 — see `docs/BENCHMARK.md`):**

Current, hybrid, 20 turns through the real turn-taking loop
(`scripts/replay_conversation.py`): **1,112 ms median, 1,463 ms p90** to first
audio. Breakdown: STT 74 ms · LLM first sentence 1,011 ms · TTS first audio
194 ms · minus 342 ms median of work done before the clock starts.

Before this session's latency work it was 1,699 ms median / 2,757 ms p90. What
moved it, none of it by making any model faster:

| change | effect |
|---|---|
| speculative dispatch (`SPECULATE`) | −342 ms median |
| short-opener prompt (`issues/0010`) | TTS first-audio 481 → 268 ms projected |
| overlap decode + synthesis with playback (`0009`, `0002`) | inter-sentence gap 293 → 0 ms |

**The LLM is a floor, not a lever.** Time-to-first-token is ~850–1,000 ms for
*every* Venice model tested, from a 30B-A3B MoE to a 405B, while raw RTT is
39 ms and connection reuse buys nothing. Do not re-open this as a model swap —
`issues/0003` records the nine models measured.

Rules for quoting numbers here:
- `bench_stack.py` figures are **per-slot, synthetic**. Never quote them as
  conversational latency. (Historic: all-local 1,642 ms · all-Venice 3,672 ms ·
  hybrid 1,381 ms; cold local 7,824 ms.)
- Replay figures come from `say`-synthesized speech: cleaner than a room, no
  echo path, one voice. They are a floor, not a promise. A real mic session is
  still the ground truth.
- Local slots warm up over ~15 turns while the hosted LLM is flat, so short
  samples describe a system nobody experiences for long.

**Working rules for this repo:**
- The local path is the default and must stay working. Hosted slots are additive.
- Never quote the LLM row as like-for-like: local 26B Q4 GGUF and
  `venice-uncensored` are different models on different hardware. STT and TTS
  *are* like-for-like (Venice serves the same Parakeet and Kokoro checkpoints).
- Benchmarks warm up and discard the first sample. If you report a cold number,
  label it cold — the two differ by ~6× on the local LLM.
- `venice_parameters.include_venice_system_prompt` stays `false`; leaving it on
  adds ~1,568 cached prompt tokens and silently changes the workload.

## Pipeline

```
mic 16kHz/10ms ─► WebRTC AEC (livekit.rtc) ─► Silero VAD ─► Parakeet TDT v3 STT
                            ▲                       │                │
                            │ reverse-stream        │ 200ms of       ▼
                            │ reference             │ silence ─►  LLM call
                  ┌─ resampled 24→16kHz ──┐         │             (started
                  │                       │         │              early)
              speakers ◄── playback ◄── KokoroTTS    │                │
                            thread      (up to 2   end-of-turn    LlmStreamer
                                       sentences     confirmed    drain thread
                                         ahead)          │             │
                                            ▲            └─► commit ───┤
                                            └──────── sentence_stream ─┘
```

Four threads, and the split is the point:

- **mic loop** — AEC, VAD, barge-in, and deciding when to speculate.
- **turn worker** — owns every MLX model (STT + TTS). Persistent and never
  exits: thread-local Metal state torn down on exit kills the interpreter.
- **LLM drain** — reads the token stream eagerly so playback cannot stall it.
- **playback** — writes 10 ms frames to the device, so synthesis can run ahead.

The two drain/playback threads deliberately touch no MLX state. Every engine
slot is swappable by env var — see `engines.py`.

## Stack

| Component | Pick | Why |
|---|---|---|
| LLM | `0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M` via **Ollama** | Apr 2026, MoE 26B/4B-active, uncensored, ~19 GB resident. Measured **14–16 tok/s**, first sentence ~1.0–1.3 s — the pipeline's biggest remaining cost |
| STT | **Parakeet TDT v3** (`mlx-community/parakeet-tdt-0.6b-v3`) | Chosen on real speech: 13.3% WER / 74 ms isolated, vs Moonshine 17.8% / 396 ms |
| VAD | **Silero VAD** `VADIterator` | Industry standard, 32 ms frames |
| AEC | **`livekit.rtc.AudioProcessingModule`** (WebRTC AEC3) | Production-grade, pip-installable, 10 ms frames @ 16 kHz |
| TTS | **`kokoro-mlx`** (Apple Silicon native MLX) | Per-sentence streaming. Measured **8.9–14.7× realtime**, TTFA ~300 ms |
| Runtime | **`mlx-audio`** | One MLX runtime for STT + TTS + turn detection instead of four packages |
| Audio I/O | **sounddevice** | PortAudio bindings |

Available alternates: `STT_ENGINE` = `parakeet` (default) · `nemotron` · `nemotron-8bit` ·
`whisper` · `moonshine` · `venice` · any mlx-audio repo id.
`TTS_ENGINE` = `kokoro` (default, via mlx-audio's `KokoroPipeline`) ·
`kokoro-legacy` (the old `kokoro_mlx` path — **drops proper nouns**, see
`issues/0011`) · `venice`. `TURN_DETECTOR` = `off` (default) · `smartturn`.

## Run

```bash
cd ~/Developer/Personal/privoice

# default: full-duplex with AEC + barge-in
uv run python voice_agent.py

# headphones / quieter room — more aggressive barge-in
HEADPHONES=1 BARGE_IN_RMS_GATE=0.04 BARGE_IN_SUSTAIN_FRAMES=3 uv run python voice_agent.py

# noisy room or echo-y speakers — fall back to half-duplex (no barge-in, no echo loop)
HALF_DUPLEX=1 uv run python voice_agent.py
```

## Tunable env vars

| Var | Default | Effect |
|---|---|---|
| `HALF_DUPLEX` | `0` | `1` disables AEC; mic muted while TTS plays. No barge-in. |
| `STREAM_DELAY_MS` | `80` | Speaker→mic round-trip estimate for AEC. Raise (100–150) if echo bleeds; lower (40–60) if first words get clipped. |
| `BARGE_IN_RMS_GATE` | `0.05` | RMS floor of cleaned mic audio to count as user voice during TTS. |
| `BARGE_IN_SUSTAIN_FRAMES` | `4` | Consecutive 32 ms frames above gate before barge-in fires. |
| `BARGE_IN_ECHO_FACTOR` | `1.6` | Gate is scaled by this × current output level while TTS plays. |
| `VAD_THRESHOLD` | `0.6` | Silero VAD speech probability cutoff. |
| `VAD_MIN_SILENCE_MS` | `500` (detector off) | Silence before an utterance is considered ended. |
| `MIN_UTTERANCE_MS` | `250` | Shorter trips are discarded as noise, not transcribed. |
| `MAX_UTTERANCE_S` | `12` | Hard ceiling on a single buffered utterance. |
| `MAX_TURN_CONTINUATIONS` | `2` | Times semantic turn detection may extend one turn. |
| `HISTORY_MAX_TURNS` | `8` | Rolling user/assistant pair window. Keep low to keep TTFA fast. |
| `STT_ENGINE` | `parakeet` | See stack table for alternates. |
| `TTS_ENGINE` | `kokoro` | — |
| `TURN_DETECTOR` | `off` | `smartturn` enables Smart Turn v3.2 semantic end-of-turn. **Leave off** — see `issues/0005`. |
| `TURN_LOG` | `turn_log.jsonl` | Where per-turn latencies are appended. `off` disables. |
| `TURN_LOG_TEXT` | `1` | `0` logs stage timings without transcripts. |
| `SPECULATE` | `1` | Start STT + the LLM during the VAD silence window. `0` restores the pre-2026-08-12 path exactly. |
| `SPECULATE_AFTER_MS` | `200` | Silence before a reply is started speculatively. Lead = `VAD_MIN_SILENCE_MS` − this. Lower buys more, and wastes more hosted calls on mid-sentence pauses. |
| `SPEC_DEBUG` | `0` | `1` prints when a guess is made and whether the commit reused it. |
| `TTS_MAX_AHEAD` | `2` | Sentences that may sit synthesized ahead of the speaker. |
| `TTS_SPEED` | `1.0` | Kokoro speaking rate. |
| `KOKORO_ALLOW_NO_ESPEAK` | `0` | `1` runs without the espeak fallback, accepting that out-of-lexicon words are **silently deleted**. See `issues/0011`. |

## Measuring real conversations

Every live turn appends one row to `turn_log.jsonl` (gitignored — it carries
transcripts). A conversation is therefore also a benchmark run, which matters
because `bench_stack.py` measures slots in isolation on a fixed prompt and the
live pipeline behaves differently — `issues/0004` exists because STT is 5–10×
slower in the pipeline than on its own.

```bash
uv run python scripts/analyze_turns.py            # medians + p90 per stage, grouped by config
uv run python scripts/analyze_turns.py --raw      # one line per turn
uv run python scripts/smoke_turnlog.py --turns 4  # regression test, no mic needed

# a whole conversation through the REAL turn-taking loop, no mic — this is the
# only harness that exercises VAD, end-of-turn, barge-in and speculation
LLM_BACKEND=venice uv run python scripts/replay_conversation.py --turns 10
LLM_BACKEND=venice SPECULATE=0 uv run python scripts/replay_conversation.py  # control
```

Every duration is measured from **end-of-speech**, because that is when the
person starts waiting. The old `[ttfa]` print started its clock after STT had
already run and so understated the wait; `first_audio_ms` is the honest number
and equals `dispatch + stt + llm_first_sentence + tts_first` by construction.

## Critical gotchas

1. **`think: false` is non-negotiable.** SuperGemma4 ships thinking-mode ON; with it on, the response field stays empty for 5–30 s while the model dumps reasoning into a `thinking` field. Voice agents cannot tolerate that. The Modelfile cannot disable it (`PARAMETER think false` is unsupported); each `/api/chat` request must include `"think": false`.
2. **WebRTC AEC requires exactly 10 ms frames at 16 kHz** (160 samples, int16). Both mic capture and the TTS reverse stream must conform — any other frame size silently fails.
3. **Kokoro outputs at 24 kHz** — we resample to 16 kHz for both speaker output and AEC reverse stream so they match. Different rates would break AEC.
4. **AEC has trouble with built-in MacBook mic+speaker geometry.** WebRTC's AEC3 isn't perfect during double-talk; some residual leaks. The energy gate (`BARGE_IN_RMS_GATE`) + sustain-frame check is what makes barge-in reliable. Headphones eliminate the problem entirely.
5. **History balloons → TTFA balloons.** 20 turns can push TTFA past 5 s. The cap (`HISTORY_MAX_TURNS=8`) is enforced both at load and after every turn.
6. **One persistent worker thread, never one per turn.** MLX keeps thread-local Metal state; destroying it on thread exit kills the interpreter (`PyThreadState_Get: ... the GIL is released`). This was fatal on turn 2 once a second MLX model was added.
7. **Published latency numbers are useless here — measure on this machine.** `scripts/bench_stack.py` (per slot) and `scripts/compare_stt.py` (real speech, WER-scored) exist for that. The metric that matters is **time-to-first-sentence**, not TTFT: TTS is driven per sentence, and the two differ ~3–4× on the same model.
8. **Component tests cannot catch conversation bugs.** Anything touching turn-taking, buffering or threading must be exercised by `scripts/smoke_multiturn.py` (multi-turn + worker thread) and, if it touches VAD or end-of-turn, by `scripts/replay_conversation.py` (the real `run_conversation` loop). `smoke_pipeline.py` runs one turn on the main thread and is structurally blind to this class of bug. Speculative dispatch was silently a no-op — every guess discarded one frame before it would have been used — until the replay harness existed to show it.
9. **Kokoro deletes words it doesn't know, it does not mispronounce them.** Out-of-lexicon words phonemize to `''` and synthesize as *silence*. The default engine wires espeak as a fallback and refuses to start without it; `scripts/smoke_tts_lexicon.py` guards it. See `issues/0011`.
10. **`first_audio_ms` starts when Silero *reports* end-of-speech**, which is `VAD_MIN_SILENCE_MS` after the user actually stopped. The felt wait is that much longer than the logged number. Speculative dispatch spends that window rather than shortening it, so it improves both — but do not confuse the two when quoting.

## Files

```
voice_agent.py            # main pipeline; run_conversation() is the turn-taking loop
engines.py                # swappable STT / TTS / turn-detector slots
turnlog.py                # per-turn latency record written by every live turn
scripts/analyze_turns.py  # turn_log.jsonl -> per-stage medians/p90 by config
scripts/replay_conversation.py # a whole conversation through the real loop, no mic
scripts/smoke_speculate.py     # speculative dispatch: reuse / discard / off
scripts/smoke_tts_lexicon.py   # TTS must not silently delete unknown words
scripts/smoke_turnlog.py  # multi-turn regression test for the turn path + log
scripts/bench_stack.py    # per-slot latency benchmark (STT / LLM / TTS)
scripts/bench_llm_mlx.py  # same LLM metrics for an MLX-format model
scripts/compare_stt.py    # WER-scored STT comparison on real speech
scripts/smoke_multiturn.py # multi-turn + worker-thread regression test
scripts/smoke_pipeline.py # E2E test without mic (uses macOS `say`)
scripts/smoke_turn.py     # semantic turn-detector test
scripts/smoke_vad.py      # VAD detection accuracy test
bench_results.jsonl       # accumulated benchmark rows, comparable across runs
issues/                   # local issue tracker — read issues/README.md
pyproject.toml            # uv-managed, Python >=3.12
.voice_history.json       # rolling conversation memory (auto-managed)
Modelfile.voice           # earlier Ollama alias (kept for reference, unused by agent)
```

## Quick handoff

For a clean restart: `rm .voice_history.json`. The model stays warm in Ollama for 30 min between runs (`keep_alive: 30m`).

For session continuation: see `state.md` — `/start` reads it, `/end` updates it.
