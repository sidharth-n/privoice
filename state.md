# Privoice — State

_Last updated: 2026-08-12_

Forked from `uncensored-local-voice` (the fully on-device build) to add hosted
slots and measure the two against each other, then renamed **Privoice** for a
public launch. Earlier handoffs are in `state-log.md`.

## Now

**Latency work is done and the 1.3 s claim is met.** 20 turns through the real
turn-taking loop: **1,112 ms median / 1,463 ms p90** to first audio, down from
1,699 / 2,757. 13 of 20 turns under 1,300 ms. Nothing got a faster model — see
the handoff below.

**Still needs a real mic session from Sid.** Every after-number comes from
`say`-synthesized speech through `scripts/replay_conversation.py`: no room
noise, no echo path for AEC, one voice. It is a floor, not a promise, and
barge-in in particular (`issues/0001`) cannot be judged from it.

```bash
cd ~/Developer/Personal/privoice && LLM_BACKEND=venice uv run python voice_agent.py
uv run python scripts/analyze_turns.py     # after talking to it
```

**Still open from the launch:** rotate the Venice API key (below), and the
Telegram/WhatsApp messages to Erik are sent.

---

## Previous context

Branch `main`, clean, pushed. **The repo is public**:
https://github.com/sidharth-n/privoice. The launch shipped: X post is out, and
the messages to Erik went via Telegram and WhatsApp. The launch video lives at
`Work/video-engine/apps/privoice/video/out/privoice-9x16.mp4`.

## Next

1. **Talk to it.** Everything below was verified headlessly; a real mic session
   is the only thing that can confirm the latency numbers hold with a human
   voice in a real room, and the only way to judge barge-in.
2. **Rotate the Venice API key** — pasted into a chat transcript during
   development, and the repo is public. The key was never committed; rotate
   anyway. Only Sid can do this.
3. `issues/0001` — barge-in. Now the only urgent issue, and the last part of the
   turn-taking path never properly measured. It was waiting on `0002`, which has
   landed, and it finally has a rig: `replay_conversation.py` can script
   overlapping speech instead of requiring it to be performed. Note its two
   symptoms are opposite failures and one threshold cannot fix both.
4. Conversational runs for all-local and all-hosted, so the local-vs-hosted
   comparison rests on the same kind of data the hybrid number now does.
   `replay_conversation.py` makes this cheap — it did not exist before.
5. `issues/0007` — proper nouns the recognizer cannot hear. The STT twin of the
   TTS bug fixed as `0011`, and the harder half.

## Blockers

- None.

## Known-soft claims

- **"World's first fully private, uncensored voice agent"** is Sid's positioning
  call, not a verified claim. Local voice assistants are a crowded genre; at
  least one markets itself as uncensored while routing TTS through ElevenLabs.
  Flagged to him; he chose to keep it.
- **The X post quotes 2.4–3s local → 1.3s hosted.** ~~Neither figure is in
  `docs/BENCHMARK.md`.~~ **Resolved 2026-08-12:** the hosted figure is now true.
  The conversational number is **1,112 ms median / 1,463 ms p90**, so 1.3 s is
  if anything conservative, and `docs/BENCHMARK.md` shows the work. Two caveats
  stand: the after-figures are `say`-synthesized speech through the real loop,
  not a mic in a room, and the **local** side of the comparison has still never
  been measured conversationally — the 2.4–3 s figure remains unsourced. If the
  numbers are challenged publicly, the repo is the source of truth.
- The cold-open reply in the video is a **scripted line**, not live model output.
  The `DEMO · SCRIPTED REPLY` stamp was removed at Sid's request; the disclosure
  now depends on him posting it in the reply-tweet.
- The LLM row of the benchmark is **not** a controlled comparison (local 26B Q4
  GGUF vs `venice-uncensored`). STT and TTS are.
- All numbers come from one machine, one network, one city, one day. Nothing
  measures output quality.

## Latest handoff — 2026-08-12 (latency: 1,699 ms -> 1,112 ms)

Sid's brief: the launch says 1.3 s, so the repo has to actually be 1.3-1.5 s.
It is. 20 turns through the real loop measure **1,112 ms median / 1,463 ms p90**
to first audio, 13 of 20 under 1,300 ms.

**The thing worth remembering: no model got faster.** The LLM was 63% of the
budget and turned out to be a floor — nine Venice models from 30B-A3B to 405B
all return their first token in 850-1,100 ms, raw RTT is 39 ms, and HTTP
connection reuse (an obvious suspect: httpx expires keepalives after 5 s, and
turns are further apart than that) measured as pure noise. Every millisecond won
came from changing the workload instead. `issues/0003` is rewritten around this
and says plainly: do not re-open it as a model swap.

**What shipped, in dependency order:**

1. **`0011` (new, closed) — TTS was deleting words, not mispronouncing them.**
   `kokoro_mlx` 0.1.1 builds misaki's G2P with no espeak fallback, so anything
   outside Kokoro's lexicon phonemizes to `''` and synthesizes as silence. "Say
   Privoice now.", "Say Sid now." and "Say Kokoro now." all produced *exactly*
   33,600 samples — the same as "Say now." Now drives mlx_audio's
   `KokoroPipeline`; TTFA unchanged. The old path is `TTS_ENGINE=kokoro-legacy`.
   Missing espeak is now a **startup error**, not a logging warning nobody sees.
2. **`0009` (closed)** — LlmStreamer drains tokens on its own thread. Decode
   rate on multi-sentence replies 5.8 -> 106 chunks/s.
3. **`0002` (closed)** — playback moved to its own thread, synthesis runs up to
   2 sentences ahead. Gap heard between sentences 293 ms -> 0 ms.
4. **`0010` (closed)** — the prompt is a latency control. "Your FIRST sentence
   must be under 8 words" took the opener from 46 to 19 chars median, p90 from
   108 to 31. Only affordable because (3) made later sentences free.
5. **Speculative dispatch** — STT and the LLM start 200 ms into Silero's 500 ms
   end-of-speech window. Median 342 ms recovered, 20/20 turns. `SPECULATE=0`
   restores the old path exactly.
6. **`0008` (closed)** — `os._exit(0)` after cleanup, as the issue itself
   recommended.

**The most useful thing built is not a fix.** `run_conversation()` now takes its
frames from an iterator, so `scripts/replay_conversation.py` can drive the whole
turn-taking loop off `say`-generated speech. That loop had never been runnable
without a microphone. It immediately found that **speculative dispatch was a
complete no-op**: silero clears `temp_end` both when speech resumes *and* when
it emits `end`, so every guess was discarded one frame before the commit that
would have used it. The code read as correct. Three lessons in `learning.md`.

**Two things I did not do, deliberately:**

- **`issues/0001` (barge-in)** — needs double-talk with a real mic and an echo
  path. Left urgent. The replay harness now makes it testable.
- **`issues/0004`** — chased every hypothesis and the 5-10x slowdown does not
  reproduce: STT is 48-90 ms isolated *and* under the full real-loop load, which
  was the fastest configuration of the five I built. Dropped to low with the
  measurements written down, rather than inventing a fix for it.

**Watch out for:** the after-numbers are `say`-synthesized speech — no room
noise, no echo path, one voice. Labelled as a floor everywhere they appear.
`first_audio_ms` also still starts when silero *reports* end-of-speech, which is
500 ms after the user stopped; speculation spends that window rather than
shortening it, so felt latency is better too, but the two are not the same
number and `CLAUDE.md` gotcha #10 says so.

**Start next session by** asking Sid how it felt on a real mic, then `0001`.
