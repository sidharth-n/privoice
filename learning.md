# Learning

Durable lessons for this project. Newest first. Append before continuing work — this is
how the project gets smarter rather than repeating itself.

---

## 2026-08-12 · Snapshot the dataset before you publish statistics from it

**Context:** The first conversational write-up quoted 2,143 ms median from 16 turns.
The agent was still running in the background and the log kept growing; by the time
the session ended it held 34 turns and the true median was 1,699 ms. The published
correction ("the synthetic figure is 1.55× optimistic") was itself wrong by a third —
it was 1.23× — and had already gone into the README, `CLAUDE.md`, two issues and
`learning.md` before anyone noticed.

**Rule:** If a data source can still be appended to, copy it to a fixed path and
compute, cite and re-verify every figure from that copy. Note the sample size next to
every statistic so a stale number is visibly stale. And before publishing, check
whether the source grew while the analysis was being written.

**Example:** The drift was not noise — the local slots warm up over ~15 turns, so the
early sample was systematically slow. A half-session sample was measuring a transient
and reporting it as the steady state, which is a worse error than the arithmetic one.

---

## 2026-08-12 · Measuring a producer through a lazy consumer measures the consumer

**Context:** The new per-turn log reported the hosted LLM decoding at 5.8 chunks/s —
24× worse than the 115 tok/s `bench_stack.py` measured for the same model minutes
earlier. Nothing had changed on Venice's side. Tokens are pulled lazily through
`sentence_stream`, and `speak_sentence()` blocks for the whole duration of playback,
so while sentence one was being spoken nothing was reading the stream. The "decode
rate" was the rate of speech.

**Rule:** Before believing a throughput number, ask what is pulling the iterator and
whether it ever stops pulling. A generator's timing reflects the slowest thing in the
consumption chain, not the producer. Splitting the sample by a structural property of
the consumer — here, one-sentence vs multi-sentence replies — separates the two in one
step: single-sentence replies drained before playback began and reported 122 chunks/s,
matching the isolated benchmark and proving the 5.8 was backpressure.

**Example:** The artifact was also a real defect. `issues/0002` blamed the gap before
sentence two on synchronous TTS; the log showed sentence two's *tokens* had not been
requested either. Filed as `issues/0009`, and it has to land before `0002` or fixing
`0002` closes half a gap.

---

## 2026-08-12 · Per-slot benchmarks cannot see the effects that dominate a real turn

**Context:** `bench_stack.py` timed each slot in isolation against one fixed short
prompt and produced the headline 1,381 ms hybrid figure. Thirty-four turns of actual
conversation measured 1,699 ms median / 2,757 ms p90 — and the biggest contributors
were all invisible to the per-slot harness: STT costing 89 ms per second of speech
in-pipeline vs ~29 ms/s isolated, TTS time-to-first-audio scaling at ~8 ms per
character of the reply's first sentence (R²=0.86), the LLM stalling behind playback,
and the local slots taking ~15 turns to warm up long after model loading finished.

**Rule:** A benchmark that holds the workload fixed measures the slot, not the system.
If the deliverable is a latency claim about a *pipeline*, instrument the live path and
report from it — the cheap version is one log line per turn, which turns every real use
into a sample. Keep the isolated numbers, but label which is which and never let the
synthetic one be quoted as the product's latency.

**Example:** The largest single lever found was not a slot at all — the model's choice
of opening phrase. Identical hardware and config produced 131 ms to first audio on
"Yeah, totally." and 1,610 ms on a 165-character opener. That is a bigger spread than
local-vs-hosted TTS, and no fixed-prompt benchmark can produce it.

---

## 2026-07-30 · Check the premise of a plan before executing it, not just the steps

**Context:** `issues/0003` said "the LLM is slow because it's dense — swap in a sparse
3B-active MoE, expect ~3.7x". The plan was detailed, evidenced, and marked urgent. The
model we were already running turned out to *be* a sparse MoE (`expert_count = 128`,
`expert_used_count = 8`, 100% GPU) that simply decodes at dense speed. The whole issue
was "replace X with Y" where we already had Y. A control run confirmed it: a second
same-class GGUF landed at 13.0 tok/s vs 12.5 — inside the noise.

**Rule:** Before executing a written plan — including one this project wrote itself —
spend the five minutes to verify its central factual claim against the artifact in
front of you. A plan's steps get scrutiny because they're what you're about to type;
its premise sits above them and gets read as given. Ask "is the thing this says is
true, actually true here?" `ollama show` / model metadata / `ollama ps` answer it
faster than any benchmark.

**Example:** Three claims in 0003 were also false — the recommended tag has no
`latest` and cannot be pulled at all, its real size is 23.94 GB not 17 GB (on this
32 GB machine that exceeds the ~24 GiB GPU wired limit and would have run *slower*),
and the 17 GB figure belonged to a dense variant. This is the same failure the
"verify subagent research findings" lesson below describes, surviving into a written,
prioritised issue — so the check has to happen at execution time too, not only when
the research lands.

---

## 2026-07-30 · A faster number in one column can still be a slower agent

**Context:** MLX decoded 20% faster than llama.cpp (18.3 vs 15.2 tok/s) — the metric
0003 was explicitly optimising. It was still the wrong choice: its prompt *prefill*
ran at 51–60 tok/s. With `HISTORY_MAX_TURNS=8` the prompt grows every turn, so the
agent would have got slower the longer you talked to it. The win column was real and
irrelevant.

**Rule:** When benchmarking a swap, measure every stage the change touches, including
the ones the current setup is silently good at. A regression hides in the column you
didn't think to print — especially costs that scale with conversation length, since a
fresh-prompt benchmark never sees them.

**Example:** `bench_llm_mlx.py` originally reported TTFT / first-sentence / decode. It
took a separate probe with a 17-token prompt to expose `prompt_tps`, which is what
actually killed the option.

---

## 2026-07-30 · Component tests cannot catch conversation bugs

**Context:** Three changes shipped clean through every scripted test and broke immediately
in live use — utterance fragmentation (buffer wiped on resume), a fatal GIL crash on the
second turn, and runaway 20-second STT buffers. Sid found all three by talking to the
agent, across five test sessions.

**Rule:** A voice agent's bugs live in the *interaction* between stages and threads, not
in the stages. Before shipping anything that touches turn-taking, buffering, or
threading, it must be exercised by (a) **multiple turns** and (b) **a worker thread** —
`scripts/smoke_multiturn.py` exists for exactly this. `smoke_pipeline.py` runs one turn
on the main thread and is structurally incapable of catching this class of bug.

**Example:** `voice_agent.py` spawned a fresh thread per turn. MLX keeps thread-local
Metal state, and destroying it on thread exit crashes the interpreter
(`PyThreadState_Get: ... the GIL is released`). Kokoro alone tolerated it; adding a
second MLX model made it fatal on turn 2. Fix: one persistent worker that never exits.

---

## 2026-07-30 · Never bound one dimension of an unbounded wait

**Context:** Semantic turn detection added a "keep listening if the thought isn't
finished" path. Fixing its first bug (buffer reset) removed a ceiling without adding one,
producing an 11.5 s transcription. Adding a *duration* cap still left the loop appending
silence while it waited, so every turn simply grew until it hit the cap — 18–21 s STT.

**Rule:** When adding a wait-for-more-input path, bound **every** dimension at once:
iteration count, total duration, and what accumulates during the wait. Bounding one at a
time produces a new pathology per fix.

**Example:** `MAX_UTTERANCE_S` alone was insufficient; it needed `MAX_TURN_CONTINUATIONS`
too, and the buffer still accumulates silence frames (unresolved — see `issues/0005`).

---

## 2026-07-30 · Measure on this machine; published latency numbers are near-useless

**Context:** Every latency figure found in research came from an M4 Max, an H100, or a
vendor blog. The project's own `CLAUDE.md` carried numbers that were wrong by 4–5×.

**Rule:** Before choosing a model, measure it here. `scripts/bench_stack.py` (per-slot)
and `scripts/compare_stt.py` (real speech, WER-scored) exist for this. Also verify the
*right* metric: for a per-sentence TTS pipeline the gate is **time-to-first-sentence**,
not TTFT — they differed 3× on the same model.

**Example:** Documented "STT ~75 ms" measured 351–396 ms. "Kokoro 17× realtime" measured
8.9×. Whisper's claimed accuracy crown lost to Parakeet on Sid's actual voice.

---

## 2026-07-30 · Verify subagent research findings before acting on them

**Context:** A research subagent's top-ranked STT recommendation (`nemotron-asr-mlx`) was
a **1-star repo, last pushed 5 months earlier**, wrapping an English-only model while the
agent claimed it gave 40 language-locales. Another agent reported a WER table that a web
AI summary had printed backwards.

**Rule:** Treat subagent output as leads, not conclusions. Check repo health (stars,
last push, release count), confirm the model behind a wrapper is the one claimed, and
prefer primary sources (HF model cards, GitHub releases, the project's own benchmark
files) over blog summaries. Programmatic-SEO benchmark sites read authoritative and are
not.

**Example:** `mlx-audio` (⭐7650, pushed daily) turned out to already contain the
Nemotron MLX port the agent said had "no Apple Silicon path".

---

## 2026-07-30 · A comment that describes a condition the code doesn't implement

**Context:** Barge-in fired on the agent's own voice, cutting 7 of 13 replies. The
condition's comment read *"if user has been loud for N consecutive frames AND VAD
currently agrees there's speech"* — the code checked only energy. The VAD term had never
existed.

**Rule:** When debugging, read the condition, not its comment. And when a comment
describes a safeguard, grep for the safeguard.
