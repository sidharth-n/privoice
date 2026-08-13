# Session log (older handoffs, newest first)

## 2026-08-12 — Privoice launch prep (rename, public repo, launch video)

**Shipped this session, in order:**

1. **Renamed the project.** `venice-voice-agent` → `privoice`: GitHub repo via
   `gh repo rename` (old URL redirects), local directory moved, every internal
   reference rewritten, `uv.lock` regenerated so the package name follows.
   Display casing is "Privoice" in prose, `privoice` for repo/dir/package.
   External references in `Work/video-engine/apps/privoice` repointed too — its
   VO generator had the agent path hardcoded. Registered in the brain as a **new**
   card (`brain/projects/privoice.md`), not a rename: `uncensored-local-voice`
   still exists as its own project.

2. **Made the repo public** after scanning history for secrets (clean), with
   description and topics. README rewritten to lead with the product and a
   runnable quick-start, measurement intact underneath.

3. **Built the launch video** as a new `video-engine` tenant
   (`Work/video-engine/apps/privoice/`, brief `0001-launch.md`). Six scenes:
   recorded cold-open exchange → wordmark → spec checklist → terminal with real
   `turn_log.jsonl` rows → three claims that erase themselves → repo + Venice
   lockup. Palette scraped from live venice.ai markup. Music bed picked by
   measurement (spectral centroid, BPM, beat-autocorrelation "pulse") across 32
   candidates; `mixkit-609` chosen. Per-bed `BGM_GAIN`, because a sub-heavy bed
   and a mid-bright one cannot share one number.

4. **Found and worked around a real TTS defect** (see Next #3) — the most
   reusable finding of the session; written up in `learning.md`.

**Decisions that will look arbitrary later:**

- `"Pre-voice"` in the VO script is a **pronunciation control, not a typo**.
  espeak maps the real spelling to `pɹˈɪvYs` ("PRIV-oyss"), which Sid heard as
  "prevaice"; the hyphenated form maps to `pɹˌivˈYs` ("pree-VOICE"). Do not
  "correct" it.
- The silence between question and reply in the video's cold open is **38 frames
  = 1,272 ms**, a real measured turn, with a counter running through it. Cutting
  it shorter would have been trivial and would have been the one lie the film
  argues against.
- Every number in the video's terminal scene is a real row from
  `turn_log.jsonl`, per video-engine's honest-content rule.

**Start next session by** asking whether the post went out and who the Telegram
recipient is. If the launch is done, Next #3 (the TTS bug) is the highest-value
code work and has a working reference implementation to copy.

---


## 2026-08-12 — Venice port, then per-turn instrumentation

_Rolled down by `/end` on 2026-08-12 when the session turned to the Privoice launch._

### Instrumentation (later that day)

**2026-08-12 (later): live turns are now instrumented, and the real numbers
are worse than the synthetic ones.** Every turn appends a full stage breakdown
to `turn_log.jsonl` (`turnlog.py`); `scripts/analyze_turns.py` reports medians
and p90 per stage grouped by config; `scripts/smoke_turnlog.py` is the
multi-turn regression test for the path.

34 real conversational turns on hybrid: **1,699 ms median / 2,757 ms p90** to
first audio, against the 1,381 ms the per-slot benchmark predicted. Ranking
unchanged, magnitude 1.23× optimistic. Four findings, all invisible to
`bench_stack.py`:

- **The local slots warm up over ~15 turns** — STT 382→178 ms, TTS 546→303 ms,
  first-audio 2,196→1,497 ms — while the hosted LLM is flat to within 2%. This
  is well after model loading finishes and is unexplained. It also means any
  short sample (including the 16-turn one this section originally reported)
  overstates the latency.
- STT costs **89 ms per second of speech** in-pipeline (103 ms/s before it
  warms) vs ~29 ms/s isolated (`issues/0004`, now with real numbers).
- TTS time-to-first-audio ≈ **118 ms + 7.9 ms per character** of the reply's
  first sentence, R²=0.86 — so the model's opening phrase is the biggest
  latency lever in the system (`issues/0010`, new).
- The LLM stream is **not drained during playback**, so sentence two's tokens
  are not even requested until sentence one finishes speaking (`issues/0009`,
  new — the larger half of `issues/0002`, and must land first).

Written up in `docs/BENCHMARK.md` → "What a real conversation measures".
Still outstanding from this: the all-local and all-hosted *conversational*
runs have not been done, so only the hybrid magnitude is corrected.

### The Venice port (earlier that day)

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

### Next steps as of that handoff

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

### Blockers

- None technical. The repo runs in all three configurations.

### Known-soft claims

- The LLM row is **not** a controlled comparison (local 26B Q4 GGUF vs
  `venice-uncensored` — different models on different hardware). Stated plainly
  in both the README and `docs/BENCHMARK.md`; do not let it drift into being
  quoted as like-for-like.
- All numbers come from one machine, one network, one city, one day.
- Nothing here measures output quality. Latency only.

---

> Everything below predates the fork to privoice on 2026-08-12,
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
