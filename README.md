# Privoice

**A private, uncensored voice agent. No refusal layer, no logging, no retention.**

Talk to it and it talks back in about a second and a half, full duplex — cut it
off mid-sentence and it stops. It answers what other assistants refuse, and it
keeps no record that you asked.

```bash
git clone https://github.com/sidharth-n/privoice && cd privoice
uv sync
cp .env.example .env                        # add a Venice key for the hosted slots
LLM_BACKEND=venice uv run python voice_agent.py
```

Everything below is the measurement behind those claims. Every figure came off
one machine, is labelled as warm or cold, and is reproducible with the scripts
in this repo — nothing here is quoted from a vendor blog.

---

A real-time voice agent for Apple Silicon whose every slot — speech-to-text,
LLM, text-to-speech — swaps by env var between **on-device** and
**[Venice AI](https://venice.ai)'s hosted API**. Speak to it, it speaks back,
interrupt it and it stops.

```
mic ─► AEC ─► VAD ─► STT ─► LLM ─► TTS ─► speakers
                      │      │      │
                   local or venice, per slot
```

Venice serves the **same Parakeet and Kokoro checkpoints** this project already
ran locally. That is what makes the comparison below controlled: for those two
slots the model is held constant and only the substrate changes.

## The finding

Apple M5 MacBook Air (32 GB), client in India, 2026-08-12, 5 samples per slot,
medians. Method, controls and limits: **[docs/BENCHMARK.md](docs/BENCHMARK.md)**.

| Configuration | Warm | First turn after idle |
|---|---|---|
| All local | 1,642 ms | 7,824 ms |
| All Venice | 3,672 ms | 3,672 ms |
| **Hybrid — local STT/TTS, Venice LLM** | **1,381 ms** | **1,381 ms** |

Those are per-slot measurements on a fixed short prompt. **A real conversation
is slower**: 34 live turns on the hybrid config measured **1,699 ms median /
2,757 ms p90** to first audio. The ranking holds, the magnitude does not — and
the gap is almost entirely in the two local slots, which take ~15 turns to warm
up (2,196 ms over the first half of the session, 1,497 ms over the second)
while the hosted LLM is flat from turn one. Every live turn now logs its own
breakdown, so this is measured rather than assumed; see
[docs/BENCHMARK.md](docs/BENCHMARK.md#what-a-real-conversation-measures--and-why-it-is-16-the-synthetic-number).

Three results, and the first two were not what I expected.

**1. All-hosted is 2.2× slower than a laptop** — but not because the GPUs are
slow. Venice decodes 6.9× faster than the M5 (115 vs 16.6 tok/s). It loses
because roughly **400 ms of fixed server-side latency attaches to every API
request**. Measured on a static `GET /models` that runs no inference: it
survives connection reuse (TTFB still 406 ms with TLS already established) and
needs no auth. A voice turn is three sequential calls, so all-hosted pays ~1.2 s
of overhead before generating a single token.

**2. Cold start reverses the answer.** Warm, local wins. But Ollama evicts the
model on a timer, so the first thing you say after a gap costs a measured
**~7.4 s** to first sentence. Hosted has no cold start. For anything that isn't
a benchmark loop, **hybrid is 5.7× faster on the turn users notice most.**

**3. Inside Venice, model choice matters more than hosting does.**
`venice-uncensored` reaches its first sentence in 990 ms; `qwen-3-8-max` decodes
2.5× faster and takes 4,702 ms, because prefill dominates. Choosing on decode
rate or TTFT picks the wrong model.

Practical answer: **keep the ears and mouth on the device, put the brain on
Venice.**

### Why time to first sentence, not time to first token

TTS is driven per sentence, so nothing can be spoken until a complete sentence
exists. TTFT — the number almost everyone publishes — is not a proxy for it, and
here they differ by up to 4×. Every LLM figure in this repo is time to first
*sentence*, measured through the same splitter the live agent uses.

An earlier revision of the upstream README claimed "sub-second". That was never
measured and never met; see
[`issues/0003`](issues/0003-llm-decode-rate-dominates-latency.md).

## What it actually does

- **You speak.** Silero VAD detects start/end of your utterance.
- **It hears you cleanly.** WebRTC AEC (via LiveKit) cancels its own voice from
  the mic, so it doesn't feed back even with built-in mic + speaker.
- **STT transcribes** — Parakeet TDT v3 locally (~63 ms warm), or hosted.
- **The LLM streams a reply** — a local uncensored 26B via Ollama, or Venice.
- Tokens are split into sentences and each sentence goes to **TTS** immediately,
  so the first audio plays while the LLM is still writing sentence two.
- **Barge-in:** start talking and it shuts up. An energy gate plus a
  sustain-frame check filters residual echo.
- **Memory** persists across runs in `.voice_history.json` (rolling 8 turns).

## Quickstart

```bash
# 1. Install uv, then deps
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12       # kokoro-mlx requires 3.10–3.12
uv sync

# 2. For the Venice slots: key from venice.ai/settings/api
cp .env.example .env

# 3. For the local LLM slot
brew install ollama
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve &
ollama pull 0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M   # ~16 GB
```

Run it:

```bash
# hybrid — local ears and mouth, Venice brain (fastest measured)
LLM_BACKEND=venice uv run python voice_agent.py

# fully local, no network, no API key
uv run python voice_agent.py

# fully hosted — no 16 GB download, no Ollama
LLM_BACKEND=venice STT_ENGINE=venice TTS_ENGINE=venice uv run python voice_agent.py
```

No mic handy? `scripts/smoke_pipeline.py` runs the whole path from a synthesized
utterance and prints per-stage latency.

## Configuration

| Variable | Default | Options |
|---|---|---|
| `LLM_BACKEND` | `ollama` | `ollama`, `venice` |
| `STT_ENGINE` | `parakeet` | `parakeet`, `venice`, `moonshine`, `nemotron`, `whisper`, any mlx-audio repo id |
| `TTS_ENGINE` | `kokoro` | `kokoro`, `venice` |
| `TURN_DETECTOR` | `off` | `off`, `smartturn` |
| `VENICE_LLM_MODEL` | `venice-uncensored` | any Venice text model id |
| `VENICE_STT_MODEL` | `nvidia/parakeet-tdt-0.6b-v3` | any Venice ASR model id |
| `VENICE_TTS_MODEL` | `tts-kokoro` | any Venice TTS model id |

Room tuning (unchanged from the local build):

| Var | Default | Effect |
|---|---|---|
| `HALF_DUPLEX` | `0` | `1` disables AEC; mic muted while TTS plays |
| `STREAM_DELAY_MS` | `80` | AEC speaker→mic estimate. Raise (100–150) if echo bleeds; lower (40–60) if first words clip |
| `BARGE_IN_RMS_GATE` | `0.05` | RMS floor on cleaned mic to count as user voice |
| `BARGE_IN_SUSTAIN_FRAMES` | `6` | Consecutive 32 ms frames above gate before barge-in fires |
| `VAD_THRESHOLD` | `0.6` | Silero VAD speech probability |
| `HISTORY_MAX_TURNS` | `8` | Rolling user/assistant pair window |

## Stack

| Component | Local | Hosted (Venice) |
|---|---|---|
| LLM | [SuperGemma4-26B-Uncensored](https://huggingface.co/Jiunsong/supergemma4-26b-uncensored-gguf-v2) via Ollama | `venice-uncensored` |
| STT | [Parakeet TDT v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) via mlx-audio | `nvidia/parakeet-tdt-0.6b-v3` |
| TTS | [`kokoro-mlx`](https://pypi.org/project/kokoro-mlx/) | `tts-kokoro` |
| VAD | [Silero VAD](https://github.com/snakers4/silero-vad) | local only |
| AEC | [LiveKit WebRTC APM](https://docs.livekit.io/reference/python/v1/livekit/rtc/apm.html) | local only |

Parakeet was chosen on measured real speech, not reputation: 13.3% WER / 69 ms
against Moonshine's 17.8% / 351 ms on the same clips.

## Cost

Every number in `docs/BENCHMARK.md` cost **under $0.05** of Venice credit in
total. `venice-uncensored` is $0.000048 per turn at this prompt size; hosted
Parakeet is $0.0001 per audio-second.

## Critical gotchas

1. **`think: false` is non-negotiable** on the local model. SuperGemma4 ships
   thinking-mode ON; with it on the response field stays empty for 5–30 s while
   reasoning streams to a separate field, which no voice agent can tolerate. The
   Modelfile cannot disable it ([ollama#14809](https://github.com/ollama/ollama/issues/14809)).
2. **Venice injects its own system prompt by default** — measured at ~1,568
   cached prompt tokens. This repo sets
   `venice_parameters.include_venice_system_prompt: false`, both because the
   agent ships its own persona and because leaving it on quietly changes what
   you are benchmarking.
3. **AEC frames must be exactly 10 ms at 16 kHz.** WebRTC APM is strict and
   fails silently on the wrong frame size.
4. **Built-in MacBook mic+speaker geometry is hard for software AEC.** Expect to
   tune for your room; headphones remove the problem.
5. **History is a latency multiplier.** Every retained turn lengthens prefill.
   `HISTORY_MAX_TURNS=8` is the cap that keeps latency in the measured range.

## Files

| Path | What it is |
|---|---|
| `voice_agent.py` | The live agent: VAD, barge-in, AEC, playback |
| `engines.py` | Swappable STT/TTS/turn-detector slots, one adapter per backend |
| `scripts/bench_stack.py` | The benchmark that produced every published number |
| `scripts/smoke_pipeline.py` | Full path without a mic, per-stage timing |
| `scripts/compare_stt.py` | STT comparison on real speech (WER + latency) |
| `docs/BENCHMARK.md` | Method, controls, and what the numbers do not cover |
| `bench_results.jsonl` | Every run, appended, never overwritten |
| `baseline_local_m5.jsonl` | Earlier local-only runs from this machine |
| `CLAUDE.md`, `state.md`, `learning.md`, `issues/` | How it was actually built |

### Why the context files are in the repo

`CLAUDE.md`, `state.md`, `learning.md` and `issues/` are checked in
deliberately. This project is built with coding agents doing most of the typing,
and those files are what makes that work: the agent reads `state.md` to know
where things stand, appends to `learning.md` when something breaks, and files an
issue instead of derailing.

They are also the honest record.
[`issues/0003`](issues/0003-llm-decode-rate-dominates-latency.md) is where the
sub-second claim died, and `learning.md` says why. If you want to know what this
codebase does and where it falls short, those files will tell you faster than
the source will.

## Honest limits

- One machine, one network, one city, one day. Latency from a datacentre next to
  Venice's edge would look completely different.
- STT and TTS are like-for-like. **The LLM comparison is not** — a local 26B Q4
  GGUF and `venice-uncensored` are different models on different hardware. It
  reflects the choice a builder faces, not a controlled swap, and no conclusion
  here should rest on it being one.
- Latency only. No quality evaluation of any kind was run.
- Single client, sequential requests. Says nothing about behaviour under load,
  where a hosted API's advantages are real and invisible here.

## Why uncensored

Hardcoded "I cannot…" disclaimers and safety preambles ruin natural voice
conversation. A friend on a phone call doesn't say "as an AI I cannot" — they
just talk. That is the design goal, on both the local and hosted paths.

Use responsibly. You own anything it says; don't deploy it public-facing.

## Credit

Extended from [uncensored-local-voice](https://github.com/sidharth-n/uncensored-local-voice),
the fully on-device build, with hosted slots added so the two could be measured
against each other.

Built by [Sidharth N](https://sidharthn.in). Not affiliated with Venice AI.

MIT — see [`LICENSE`](LICENSE). Built on open-source components (Parakeet TDT,
Silero VAD, Kokoro, LiveKit RTC, Ollama, SuperGemma4); see their licenses.
