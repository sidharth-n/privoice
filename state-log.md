# Session log (older handoffs, newest first)

> Everything below predates the fork to venice-voice-agent on 2026-08-12,
> when this project was `uncensored-local-voice`.

_Last updated: 2026-07-30_

## Now
- Branch **`voice-stack-upgrade-2026-07`** (12 commits, all pushed, worktree clean). `main` untouched.
- **Known-good config is `TURN_DETECTOR=off`** — Sid confirmed it works. Run it that way.
- Stack: **Parakeet TDT v3** STT · Kokoro TTS · Silero VAD (min_silence 500 ms with detector
  off) · WebRTC AEC · SuperGemma4 LLM via Ollama. Unchanged this session — **no runtime code
  was touched**, only docs, the issue tracker, and a new bench script.
- `issues/` has **8 open** (3 urgent). `0003` is measured out and should be **set aside**.
- Last verified: `engines.py` imports and still builds the Parakeet default; the LLM A/B ran
  clean across three configurations (rows in `bench_results.jsonl`).

## Next
1. **`issues/0002` — inter-sentence gap.** Now the top item, since 0003 is spent. Callback-driven
   output stream + ring buffer so TTS generates ahead of playback. Must move the AEC
   reverse-stream feed carefully.
2. **`issues/0001` — barge-in.** Do after 0002; they share the playback mechanism.
3. `issues/0004` (STT slower in-pipeline than isolated — may be free latency), then 0007 / 0005 / 0006 / 0008.
4. Optional, Sid deferred it ("later"): upgrade Ollama 0.21.2 → 0.32.5 for its own MLX engine.
   Only remaining LLM lever; see the ceiling estimate below before spending on it.

## Blockers
- None. The one open decision — whether to upgrade Ollama — Sid deferred.

## Latest handoff — 2026-07-30 (evening: issue 0003 measured to a dead end, docs corrected)

### What shipped
Three commits, all pushed. **No change to `voice_agent.py` — the agent behaves exactly as it
did at `ddb6369`.**

- `5ea6a74` — `CLAUDE.md` corrected to the shipped stack; `engines.py` docstring fixed; new
  `scripts/bench_llm_mlx.py`.
- `dbf9b17` — `issues/0003` rewritten: the premise was wrong.
- `1c77efe` — `issues/0003` final: MLX measured, both levers dead.

### The finding: issue 0003's premise was false, and both fixes it implied are dead
0003 said the LLM is slow because it is dense, so swap in a sparse 3B-active MoE. **The model
we already run is a sparse MoE** — `gemma4.expert_count = 128`, `expert_used_count = 8`, at
`100% GPU`, 19 GB — and still decodes at dense speed. So the premise did not hold, and the two
candidate fixes both measured out:

| | decode | TTFT | prefill |
|---|---|---|---|
| **current GGUF / llama.cpp** | 15.2 tok/s | **276 ms** | fast |
| second GGUF (`HammerAI/gemma-4-26b-a4b-heretic`) | 13.0–16.6 tok/s | 266 ms | fast |
| MLX / mlx-lm (`mlx-community/gemma-4-26B-A4B-it-heretic-4bit`) | **18.3 tok/s** | 529–1077 ms | **51–60 tok/s** |

- **GGUF→GGUF: no change.** Inside the noise. Dead.
- **MLX: +20% decode, but prefill is disqualifying.** With `HISTORY_MAX_TURNS=8` the prompt
  grows every turn, so a ~55 tok/s prefill means the agent gets *slower the longer you talk to
  it*. Confirmed warm, 17-token prompt, 3 trials — not cold start.
- **That MLX model is separately unusable:** it opens a `<|channel>thought` block on every
  generation, even for a bare "Hi" with no system prompt, despite its template correctly
  pre-filling an empty closed thought block when thinking is off. mlx-lm has no `think: false`.

### Three errors in 0003's original research, caught before they cost anything
1. `huihui_ai/Qwen3.6-abliterated` **has no `latest` tag** — the pull command in the issue
   fails outright. The "verified HTTP 200" check hit the model page, not a manifest.
2. Its "17 GB" is wrong: every `35b*` tag is **23.94 GB**. On this 32 GB machine (default
   `iogpu.wired_limit_mb=0` → ~24 GiB) that spills past the GPU wired limit — it would have run
   **slower**, not faster.
3. The 17 GB figure belongs to the `27b` tag, which is **dense** and defeats the premise.

### What to do first next session
Start `issues/0002`. **Do not reopen 0003** — read its "Verdict" section first; the numbers and
both dead ends are written up there so nobody re-runs this. The only untested lever is the
Ollama 0.21.2 → 0.32.5 upgrade (its bundled MLX engine has custom small-batch matmul kernels and
a v0.31.1 fix for "Gemma 4 MoE model loading", so it may not repeat mlx-lm's prefill weakness) —
but the measured ceiling is ~20% of one stage in exchange for replacing the engine the whole
agent depends on. Sid said "later". 0002 and 0001 are smaller but certain, and they are what
make the agent *feel* natural.

### Gotchas discovered this session (they will bite again)
- **`ollama stop <model>` before any MLX benchmark**, or it OOMs the GPU at 19 GB resident
  (`kIOGPUCommandBufferCallbackErrorOutOfMemory`).
- **`apply_chat_template(tokenize=False)` → `stream_generate(str)` double-prepends BOS.** Pass
  token ids. The first MLX run was invalid because of this.
- **`ollama pull` can stall silently at byte-complete** — it sat 25 min with the client at 0%
  CPU and the blob still `-partial`. Kill and re-run; blobs resume.
- Benchmarks run while a large download is in flight read **~20% low**. Re-run clean before
  believing any A/B.


---

Older session handoffs, newest first. Read on demand — `/start` only reads `state.md`.

## 2026-07-30 — Research + 9 commits (agent materially better but not "natural" yet)

### What shipped
Full research pass (agent-reach ×5 + monid/tikhub X pulls, $0.0165 spend) then a staged
rebuild. Every number below was measured on **this M5**, not taken from a vendor page.

| Slot | Before | After |
|---|---|---|
| STT | Moonshine, 396 ms, 17.8% WER | **Parakeet TDT v3, 74 ms isolated, 13.3% WER** |
| Turn-taking | silence timeout only | Smart Turn v3.2 available (19 ms) — **default off** |
| Threading | thread per turn | **one persistent worker** |
| Barge-in | fixed RMS gate, no VAD check | VAD-gated + output-scaled gate (still wrong, see 0001) |

Commits: `08c87ea` mlx-audio added · `abbfc17` engine slots · `e0874ce` bench harness ·
`a04e245` Smart Turn · `de96969` fragmentation fix + STT engines · `71efaef` Parakeet
default · `b8be6e1` GIL crash fix + multi-turn test · `20dfa9e` self-interruption /
history / utterance cap · `0df0154` continuation bound + audio-length logging.

### Key decisions and the evidence
- **`mlx-audio` (⭐7650) is the single runtime for STT+TTS+VAD** — replaced what would
  have been four separate packages. Installs clean on Python 3.13, so no venv rebuild.
- **Parakeet over Nemotron/Whisper/Moonshine**, decided on a hard 45-word passage read
  by Sid: nemotron 8.9% WER / 2174 ms, **parakeet 13.3% / 228 ms**, whisper 15.6% /
  1183 ms, moonshine 17.8% / 1682 ms. Nemotron's margin is partly a scoring artifact
  (it split the place name into two words = 2 errors for one mistake) and its 2.1 s
  exceeds the whole latency budget. Parakeet was also the only engine to get "just buy
  it" right. `STT_ENGINE=nemotron` remains available.
- **The metric that matters is time-to-first-sentence, not TTFT** — TTS is driven per
  sentence, so nothing is audible until a terminator arrives. TTFT 319 ms vs
  first-sentence 977 ms; the gap is decode rate (16.1 tok/s). This reordered the plan.
- **Malayalam deliberately parked** by Sid — findings preserved in `issues/0006`.

### Corrections to earlier project claims (all were wrong in `CLAUDE.md`/`state.md`)
- STT "~75 ms warm" → measured **351–396 ms** (Moonshine).
- Kokoro "17× realtime" → measured **8.9–14.7×**, TTFA ~300 ms.
- "MTP reaches ~70 tok/s" → **MTP is a net loss on Metal** (35B self-MTP collapses to
  1.93 tok/s).
- HauhauCS "~4× KL drift" → **6.5×**, and its tool is plagiarised from Heretic.
- `<1 s warm` target → actual ~1.6 s isolated, 2.2–3.6 s TTFA live.
- `CLAUDE.md` still carried the wrong STT/TTS/MTP numbers (fixed 2026-07-30 in `5ea6a74`).

### Process lesson (this cost Sid five test sessions)
Three of my changes shipped clean through scripted tests and broke in live conversation:
utterance fragmentation, a fatal GIL crash, and runaway buffers. Cause: component tests
(`smoke_pipeline.py` = one turn, main thread) cannot catch interaction bugs.
`scripts/smoke_multiturn.py` now covers multi-turn threading, but the real fix is
**record conversations and replay offline** before enabling anything conversational —
see `issues/0005`. Do not develop turn-taking by shipping to Sid and reading logs.

## 2026-07-27 — Research session (no code)
- **Uncensored model landscape (researched via agent-reach):** Heretic
  (github.com/p-e-w/heretic, v1.4.0) is now the standard abliteration tool; community
  forensic benchmark ("Abliterlitics", r/LocalLLaMA) showed Heretic builds stay closest
  to base (KL ≈ 0.06) while HauhauCS "aggressive" drifts ~4×. Mac sweet spot for
  32–48 GB is **Qwen3.6-35B-A3B** (MoE, 3B active, 256K ctx, vision, tool calling);
  uncensored GGUFs pullable from Ollama. OMLX + MTP reaches ~70 tok/s.
  — *Corrected 2026-07-30: HauhauCS drift is 6.5× not 4×, and MTP is a net LOSS on
  Metal. See `learning.md`.*
- **Local capability map for this Mac (32 GB M5):** agents (Hermes Agent on local
  Ollama), local coding agents (context is the wall, ~64K working budget), overnight
  batch (~1.4K summarizations/8 h for ~$0.14 power), private RAG, image gen (Draw
  Things / Flux), music gen (ACE-Step 1.5), speech-to-speech (Moshi MLX). Video gen is
  the weak slot (Wan 2.2 5B ≈ 47–97 min per 5 s clip on 32 GB).
- **Spun off a new project: `livefunAI`** (~/Developer/Personal/livefunAI, private
  GitHub) — live AI event entertainment (Decart realtime restyle + fal.ai I2V clips on
  a video wall). Scaffolded, registered in the brain, MVP plan approved, Stage 0
  (operator+wall shell, camera passthrough) built/verified/pushed. Work continues in
  its own iTerm tab/session — not in this repo.
- Planned next: model A/B swap (pure `ollama pull` + env change + smoke test), TTS slot
  upgrade to Qwen3-TTS, tool calling for the voice agent.

## 2026-04-27 — Open-sourced on GitHub
- Wrote public-facing `README.md` (setup, modes, env-var reference, hardware notes, gotchas, contribution asks).
- Wrote `.gitignore` excluding `.venv/`, `__pycache__/`, `.voice_history.json` (private convo memory), audio test artifacts, logs.
- `git init -b main`, staged 10 files, initial commit with co-author trailer.
- Created public GitHub repo via `gh repo create`: **https://github.com/sidharth-n/uncensored-local-voice**
- Pushed `main`. Repo description set, branch tracks origin.
- Convention going forward: commit + push regularly during future sessions; `/end` should also push state.md updates.

## 2026-04-27 — Voice agent v1: AEC, barge-in, conversational replies
- Researched + selected **SuperGemma4-26B-Uncensored** (Apr 2026 MoE, ~4B active) as the LLM. Pulled `0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M` via Ollama. Smoke-tested: ~40 tok/s, no refusals.
- Discovered Gemma 4 ships with **thinking mode ON** — first chat returned 80 tokens of empty `content`, all CoT in `thinking` field. Hard requirement to send `"think": false` in every `/api/chat` request (Modelfile param not supported yet, see [ollama/ollama#14809](https://github.com/ollama/ollama/issues/14809)).
- Built `voice_agent.py` (~430 LOC): mic → Silero VAD → Moonshine STT → Ollama (think=false, stream=true, keep_alive=30m) → sentence-level split → KokoroTTS (MLX) → speakers. Sentence-level streaming gives sub-second TTFA.
- Added `scripts/smoke_pipeline.py` (E2E without mic via macOS `say`) and `scripts/smoke_vad.py` (Silero accuracy check). Warm baseline: STT 75 ms / LLM TTFT 724 ms / TTS TTFA 363 ms / total 1.16 s.
- Hit acoustic feedback loop on first live test (agent's TTS → speakers → mic → STT → loop). Implemented half-duplex (mic dropped during TTS + 250 ms grace) — fixed echo but killed barge-in.
- Researched 2026 best practice: **WebRTC AEC3** via `livekit.rtc.AudioProcessingModule` is the standard for full-duplex local agents (Pipecat, LiveKit, RealtimeSTT all use it). Wired it in: 10 ms frames at 16 kHz int16, Kokoro 24 kHz output resampled to 16 kHz so reverse stream matches speaker output, `set_stream_delay_ms(80)`.
- Tuned the AEC pipeline iteratively against MacBook Air built-in mic+speaker geometry:
  - First with NS+AGC: AEC residual fully suppressed but user voice was eaten too — Moonshine returned empty for real speech.
  - AEC-only: user voice intact but residual triggered VAD as false barge-ins.
  - Final: **AEC + HPF, no NS, no AGC**, plus an **energy gate** (`BARGE_IN_RMS_GATE=0.05`) and **sustain-frame check** (`BARGE_IN_SUSTAIN_FRAMES=4`, ~128 ms of continuous voice required) before firing barge-in. `barge_fired` flag disarms further fires until the next reply starts.
- Upgraded Moonshine from `BASE` → `MEDIUM_STREAMING` for accuracy. Disabling NS in APM also visibly cleaned up STT garbling.
- Capped history (`HISTORY_MAX_TURNS=8`, env-tunable) and enforced cap at load too — TTFA was hitting 5–8 s once history grew past 20 turns. Now stays under 1.5 s warm regardless of session length.
- Persistent context memory: `.voice_history.json` saves user/assistant turns; rolling-window trim. Each session resumes prior turns automatically.
- Live conversation tested for ~30 turns: real barge-ins fire correctly (RMS 0.07–0.15, sustained), Malayalam reply played through cleanly, conversational personality good ("Hmm", "Haha", contractions, no disclaimers).
- Wrote `CLAUDE.md` with full architecture, run instructions, env-var reference, and gotchas.
