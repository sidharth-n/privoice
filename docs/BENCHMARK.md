# Local vs hosted, measured

Every number here came off one machine on 2026-08-12. Nothing is quoted from a
vendor blog, and nothing is estimated. The harness that produced it is
`scripts/bench_stack.py`, in this repo, and it appends every run to
`bench_results.jsonl` so results accumulate instead of being overwritten.

## Why this comparison is worth anything

Venice serves **the same checkpoints this project runs locally**:

| Slot | Local | Venice | Same model? |
|---|---|---|---|
| STT | `mlx-community/parakeet-tdt-0.6b-v3` | `nvidia/parakeet-tdt-0.6b-v3` | **yes** |
| TTS | `kokoro` via `kokoro-mlx` | `tts-kokoro` | **yes** |
| LLM | `supergemma4-26b-uncensored` Q4_K_M | `venice-uncensored` | **no** |

So for STT and TTS the model is held constant and only the substrate changes.
That is a controlled comparison.

**The LLM row is not controlled and should not be read as one.** A 26B Q4 GGUF
on an M5 and whatever `venice-uncensored` runs on are different models on
different hardware. It is "the best local option available here" against "the
equivalent hosted option", which is the choice a builder actually faces — but
it is not a like-for-like swap, and any conclusion that depends on it being one
is wrong.

## Conditions

- Apple M5, macOS 26.4, 2026-08-12.
- Client in India; Venice reached over ordinary residential broadband.
- Prompt, every run: `"Hey, what should I make for dinner tonight?"`
- STT probe: the same sentence rendered by macOS `say -v Samantha`, 16 kHz mono.
  Synthetic on purpose — anyone can regenerate it and get the same audio.
- 5 timed samples per slot. Each slot is warmed first and the warm-up sample is
  discarded, so these are steady-state numbers unless labelled cold.
- Medians reported. Min/max live in `bench_results.jsonl`.
- Venice's injected system prompt is **disabled**
  (`venice_parameters.include_venice_system_prompt: false`). Left on it adds
  ~1,568 cached prompt tokens against a local run carrying only this project's
  own ~60-token system prompt, which is not the same workload.

## Warm steady state

| Slot | Local (M5) | Venice (hosted) | Ratio |
|---|---|---|---|
| STT — Parakeet TDT 0.6b v3 | **63 ms** | 1,515 ms | 24× slower hosted |
| LLM — time to first sentence | 1,250 ms | **990 ms** | 1.26× faster hosted |
| LLM — decode rate | 16.6 tok/s | **115 tok/s** | 6.9× faster hosted |
| TTS — Kokoro, time to first audio | **329 ms** | 1,168 ms | 3.5× slower hosted |
| TTS — realtime factor | **8.3×** | 0.3× | hosted is slower than playback |

End to end (STT + first sentence + TTS first audio):

| Configuration | Total |
|---|---|
| All local | 1,642 ms |
| All Venice | 3,672 ms |
| **Hybrid — local STT/TTS, Venice LLM** | **1,381 ms** |

All-hosted is **2.2× worse than a laptop**. That is the result that needs
explaining, because it is not what "our GPUs are faster than your Mac" predicts.

## Time to first sentence, not time to first token

TTFT is the wrong metric for a voice agent and it is the one everybody quotes.
Nothing can be spoken until a whole sentence exists, because TTS is driven per
sentence. On Venice the two differ by more than a factor of four in the worst
case measured here:

| Model | TTFT | First sentence | Decode |
|---|---|---|---|
| `venice-uncensored` | 861 ms | **990 ms** | 115 tok/s |
| `qwen-3-8-max` | 4,345 ms | **4,702 ms** | 288 tok/s |

`qwen-3-8-max` decodes 2.5× faster and still loses badly, because its prefill
dominates. Picking a Venice model on decode rate alone would make a voice agent
worse. **Model choice inside Venice swings first-sentence latency more than
hosted-vs-local does.**

## Why hosted loses when it should win: a fixed ~400 ms per request

Venice's GPUs decode 6.9× faster than the M5 and still only gain 260 ms end to
end. The reason is a fixed per-request cost that has nothing to do with
inference.

Timing a trivial `GET /models` — a static list, no inference, no auth required:

```
dns 2 ms · tcp 37 ms · tls 82 ms · ttfb 490 ms
```

Three controls rule out the obvious explanations:

- **Not authentication.** `/models` returns 200 with no key at all, same timing.
- **Not TLS or connection setup.** On a reused keep-alive connection
  (`time_appconnect` = 0), TTFB is still **406 ms**.
- **Not distance.** TCP connect completes in 37 ms.

So roughly **400 ms of server-side time attaches to every request**, before any
model runs. A voice turn is three sequential calls — STT, then LLM, then TTS —
so an all-hosted turn pays about **1.2 s of overhead** before generating
anything. That single number accounts for nearly the whole gap between the
all-hosted row and the local row.

This is the one finding here that is actionable for Venice rather than for
builders, and it is why the TTS realtime factor lands at 0.3×: the audio cannot
stream faster than it plays, so hosted TTS cannot drive continuous speech.

## Cold start, which is the case that actually matters

The warm numbers assume the local model is already resident in RAM. For a
personal assistant it usually is not: Ollama evicts on its keep-alive timer, so
the first thing you say after a gap pays a full load from disk.

Measured with the model explicitly evicted before each trial, 2 trials:

| | Trial 1 | Trial 2 |
|---|---|---|
| Local TTFT | 6,445 ms | 6,201 ms |
| Local first sentence | 7,282 ms | 7,582 ms |

| Configuration | First turn after idle |
|---|---|
| All local | **~7,824 ms** |
| Hybrid | **~1,381 ms** |

Hosted has no cold start to pay, so the hybrid number is unchanged. On the first
turn — the one a user notices most — **hybrid is 5.7× faster than all-local**.

The warm comparison flatters local. The cold comparison is the honest one for
anything that isn't a benchmark, and it reverses the conclusion.

## What a real conversation measures — and why it is 1.6× the synthetic number

Everything above comes from `scripts/bench_stack.py`: one fixed short prompt,
one `say`-rendered utterance, each slot timed on its own. That is the right
shape for "how fast is Kokoro" and the wrong shape for "how long does a turn
take". So every live turn now writes its own stage breakdown to
`turn_log.jsonl` (`turnlog.py`), and a conversation is also a benchmark run.

**34 turns of ordinary conversation, hybrid config, one sitting, 2026-08-12.**
Real speech from a person, not `say`. 31 produced audio; 3 were no-speech,
error or cut short and are excluded from the stage medians but not from the
count. Measured from end-of-speech — which is when the human starts waiting,
and is *not* where the old `[ttfa]` print started its clock.

| Stage | Real conversation (n=31) | Synthetic bench | Ratio |
|---|---|---|---|
| STT | 269 ms | 63 ms | 4.3× |
| LLM — first token | 993 ms | ~990 ms* | 1.0× |
| LLM — first sentence | 1,076 ms | 990 ms | 1.1× |
| TTS — first audio | 388 ms | 329 ms | 1.2× |
| **→ first audio out** | **1,699 ms** | **1,381 ms** | **1.23×** |

p90 first-audio was 2,757 ms; worst turn 5,492 ms; best 1,086 ms.

\* first-sentence and TTFT are close on Venice for short replies; see the
section above for where they diverge.

### The local slots warm up over a session; the hosted one does not

The session median hides two regimes. Splitting it in half:

| | First 15 turns | Later 16 turns |
|---|---|---|
| STT | 382 ms (103 ms/s of speech) | 178 ms (77 ms/s) |
| LLM first sentence | 1,071 ms | 1,089 ms |
| TTS first audio | 546 ms | 303 ms |
| **First audio** | **2,196 ms** | **1,497 ms** |

The hosted LLM is flat to within 2% — it has no local state to warm. Both local
slots roughly halve. Some of the TTS improvement is shorter first sentences
(37 → 28 chars at the median), but normalised it still falls from 14.8 to
10.8 ms/char, so the warming is real and not only a change in what was said.

This is a slower and longer warm-up than "load the model" — Ollama's keep-alive
and MLX's lazy Metal compilation are both already done well before turn 15.
Unexplained, and worth its own investigation.

Quote **~1.7 s median / ~2.8 s p90** for a session, or **~1.5 s** for warm
steady state. The synthetic 1,381 ms is a fair description of the warm case and
a poor description of the first few minutes.

### STT degrades with real speech, and not only because it is longer

Real utterances ran 2.5 s at the median against the 2.2 s synthetic probe, so
some of the increase is simply more audio. But normalised, local Parakeet cost
**89 ms per second of speech** across the session, and 103 ms/s over the first
15 turns, against ~29 ms/s in isolation. That is `issues/0004` reproduced with a
proper sample instead of an anecdote: the same model, the same machine, ~3×
slower once it is one stage of a running pipeline rather than the only thing
happening.

### TTS time-to-first-audio is a linear function of sentence length

Kokoro yields nothing until it has synthesized the **whole sentence**. Across
31 turns, time-to-first-audio against the character count of the reply's first
sentence fits:

```
tts_first_ms ≈ 118 ms + 7.9 ms per character        (R² = 0.86, n = 30)
```

One turn sat far off the line at 3,776 ms and is excluded from the fit; with it
the slope is 11.0 ms/char at R² = 0.50. The relationship is otherwise tight
enough to treat as mechanical. Note the slope drifts with session warmth
(9.1 ms/char over the first 15 turns), so treat it as ~8–11 ms/char rather than
a constant.

The consequence is that **the model's writing style sets the floor on
perceived latency**. A reply opening with "Yeah, totally." speaks in 131 ms; one
opening with a 165-character sentence takes 1,610 ms — a 12× spread on the
same slot, same machine, same config, decided entirely by how the LLM chose to
start the sentence. That is a bigger lever than any of the local-vs-hosted
differences on this page, and no isolated benchmark can see it.

### The decode-rate readings are contaminated by playback, and so is the pipeline

The log initially showed LLM decode at 5.8 chunks/s — ~20× worse than the
115 tok/s measured synthetically. Splitting by reply shape explains it:

| Reply | Measured decode | n |
|---|---|---|
| Single sentence | **122 chunks/s** | 19 |
| Two or more sentences | **5.8 chunks/s** | 12 |

Venice did not get slower. The pipeline stops reading. Tokens are pulled
lazily through `sentence_stream`, and `player.speak_sentence()` blocks for the
entire duration of playback, so while sentence one is being spoken **nothing is
draining the LLM stream**. Single-sentence replies are fully drained before
playback begins and report the true rate; multi-sentence replies report the
rate of speech instead.

This is a measurement artifact worth knowing about — `llm_chunk_s` and
`llm_total_ms` are only meaningful on single-sentence turns — but it is also a
real defect. `issues/0002` describes the gap before sentence two as TTS
synthesizing synchronously. That is only half of it: at the moment sentence one
finishes playing, sentence two's tokens **have not been requested yet**. The
measured gap before later sentences was 293 ms at the median, and closing it
means draining the LLM concurrently with playback, not just pre-synthesizing
audio. Filed as `issues/0009` and `issues/0010`.

### What this does not overturn

The ranking is unchanged: hybrid is still the configuration to ship, the hosted
LLM still carries its weight, and local STT/TTS are still far cheaper than a
round trip. What changes is the **headline number**. `1,381 ms` is a real
measurement of a synthetic workload and should be quoted as one. The number to
quote for a conversation is **~1.7 s median, ~2.8 s p90**, or ~1.5 s once the
local slots are warm.

## What this means if you are building a voice agent

1. **Put the LLM on Venice, keep STT and TTS on the device.** Small models are
   milliseconds of local compute; shipping them over a network to save those
   milliseconds costs a round trip you can never win back. The LLM is the only
   slot where the compute is large enough for hosting to pay.
2. **Choose the hosted model on first-sentence latency, not decode rate or
   TTFT.** See the table above; the ranking changes depending on which you use.
3. **Do not benchmark warm and ship cold.** A 26B local model is fast on the
   second turn and slow on the first, and users mostly experience the first.
4. **Instrument the live turn, not just the slots.** Every finding in the
   section above — STT degrading in-pipeline, TTS latency tracking sentence
   length, the LLM stream stalling behind playback — is invisible to a
   per-slot benchmark, and two of the three are larger effects than anything
   the per-slot benchmark did find.
5. **Constrain the first sentence.** Time-to-first-audio is ~8–11 ms per
   character of it. Prompting the model to open briefly is worth more than any
   slot swap available here.
6. **Report the warm-up, not just the steady state.** The local slots took
   ~15 turns to reach their best numbers, long after model loading was done.
   A five-turn sample would have described a system nobody experiences for long.

## Reproducing this

```bash
cp .env.example .env          # add your Venice key
uv sync

# hosted slots
STT_ENGINE=venice uv run python scripts/bench_stack.py --slot stt --repeat 5
TTS_ENGINE=venice uv run python scripts/bench_stack.py --slot tts --repeat 5
uv run python scripts/bench_stack.py --slot llm --repeat 5 --no-local-llm \
    --venice-llm venice-uncensored --venice-llm qwen-3-8-max

# local slots
uv run python scripts/bench_stack.py --slot all --repeat 5

# real-conversation numbers: talk to it, then read the log
LLM_BACKEND=venice uv run python voice_agent.py
uv run python scripts/analyze_turns.py
```

Total Venice spend to produce every number on this page: **under $0.05**.
`venice-uncensored` costs $0.000048 per call at this prompt size.

## What this does not measure

- One network, one city, one day. Latency from a datacentre next to Venice's
  edge would look very different, and this says nothing about it.
- One prompt, short replies. Longer generations shift the balance toward the
  faster decoder — Venice — and this benchmark does not capture that.
- The 34-turn conversational sample is **hybrid only**. The equivalent
  all-local and all-hosted conversations have not been run, so the
  local-vs-hosted ranking still rests on the synthetic bench above. The
  conversational section corrects the *magnitude* of the hybrid number, not
  the comparison.
- Quality is not measured anywhere here. Every number is latency. `venice-uncensored`
  and the local 26B were not compared on output quality at all, and nothing here
  should be read as a claim that either writes better replies.
- No concurrency. Single-client, sequential. A hosted API's advantage under load
  is real and is not visible in any of this.
