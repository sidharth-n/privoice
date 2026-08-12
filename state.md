# Privoice — State

_Last updated: 2026-08-12_

Forked from `uncensored-local-voice` (the fully on-device build) to add hosted
slots and measure the two against each other, then renamed **Privoice** for a
public launch. Earlier handoffs are in `state-log.md`.

## Now

Branch `main`, clean, pushed. **The repo is public**:
https://github.com/sidharth-n/privoice — description and topics set.

The launch is assembled and waiting on Sid, not on code:

- **Launch video is rendered and approved** — `Work/video-engine/apps/privoice/
  video/out/privoice-9x16.mp4` (primary, for X) and `privoice-16x9.mp4`. 40.4s,
  Venice palette, dark bed, SFX, Kokoro narration.
- **X post is drafted, not posted.** Three variants under 280 chars; final copy
  in the conversation. Attach the 9:16 file, tag `@AskVenice` mid-sentence, and
  put the demo disclosure in a reply-tweet.
- **Telegram to Venice is blocked** on one question: Sid asked to "send Erika",
  but there is no thread or message history with that name in his Telegram (only
  `@erikabot`, `@IN999ErikaBot`, `@Erika6`, `@ErikaM`, none ever messaged). Best
  guess is **Erik Voorhees**, Venice's founder. Do not guess a handle — ask.

Last verified: repo visibility, description and topics read back from `gh`; git
history scanned for the Venice key and secret-shaped strings before going public
(clean — only `.env.example`, and the two tracked `.jsonl` files contain just the
synthetic benchmark prompt); `voice_agent`/`engines`/`turnlog` all import cleanly
after the directory move.

## Next

1. **Post to X** with `privoice-9x16.mp4`, then the disclosure reply.
2. **Get the Telegram handle** for Erik/Erika and send the repo + video +
   benchmark writeup.
3. **Fix the TTS proper-noun bug** — deliberately deferred by Sid until after
   the post, but it is the first thing a cloner will hit. `engines.py`'s
   `KokoroTts` uses `kokoro_mlx` 0.1.1, whose misaki G2P is built with **no
   espeak fallback**, so any out-of-lexicon word (including "Privoice" and the
   user's own name) phonemizes to `❓` and is synthesized as *silence*. Fix is to
   drive `mlx_audio.tts.models.kokoro.KokoroPipeline`, which constructs
   `EspeakFallback` itself. Working reference implementation already exists:
   `Work/video-engine/apps/privoice/video/scripts/generate-vo-kokoro.py`. Needs
   an issue filed alongside `issues/0007`.
4. **Rotate the Venice API key** — pasted into a chat transcript during
   development, and the repo is now public (the key was never committed, but
   rotate anyway).
5. `issues/0010` — prompt for a short first sentence. A `SYSTEM_PROMPT` line, no
   code; worth several hundred ms on the measured ~8 ms/char.
6. `issues/0009` — drain the LLM stream concurrently with playback, then
   `issues/0002`.
7. Conversational runs for all-local and all-hosted, so the local-vs-hosted
   comparison rests on the same kind of data the hybrid number now does.

## Blockers

- **Telegram recipient unknown** (see Now). Everything else is unblocked.

## Known-soft claims

- **"World's first fully private, uncensored voice agent"** is Sid's positioning
  call, not a verified claim. Local voice assistants are a crowded genre; at
  least one markets itself as uncensored while routing TTS through ElevenLabs.
  Flagged to him; he chose to keep it.
- **The X post quotes 2.4–3s local → 1.3s hosted.** Neither figure is in
  `docs/BENCHMARK.md`: our recorded all-local numbers are 1,642 ms warm and
  7,824 ms cold, and the hosted-path conversational number is 1,699 ms median /
  1,497 ms warm, with the best turns at 1.1–1.3s. Raised twice; Sid chose it.
  If the numbers are challenged publicly, the repo is the source of truth.
- The cold-open reply in the video is a **scripted line**, not live model output.
  The `DEMO · SCRIPTED REPLY` stamp was removed at Sid's request; the disclosure
  now depends on him posting it in the reply-tweet.
- The LLM row of the benchmark is **not** a controlled comparison (local 26B Q4
  GGUF vs `venice-uncensored`). STT and TTS are.
- All numbers come from one machine, one network, one city, one day. Nothing
  measures output quality.

## Latest handoff — 2026-08-12 (Privoice launch prep)

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
