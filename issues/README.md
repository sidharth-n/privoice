# Issues

Local issue tracker. One file per issue: `NNNN-slug.md`, frontmatter tags
(`id`, `title`, `status`, `priority`, `area`, `opened`, `updated`, `closed`).
New issues start from `_template.md`.

**Open: 6** — Urgent 1 · Moderate 3 · Low 2

## Urgent

| id | title | area |
|---|---|---|
| [0001](0001-barge-in-ignores-overlapping-speech.md) | Barge-in ignores overlapping speech, and misfires at end of replies | audio/aec |

## Moderate

| id | title | area |
|---|---|---|
| [0003](0003-llm-decode-rate-dominates-latency.md) | LLM time-to-first-token is the pipeline's floor, and it is not a decode-rate problem | llm |
| [0005](0005-semantic-turn-detection-parked.md) | Semantic turn detection parked — needs offline evaluation, not live debugging | vad/turn-taking |
| [0007](0007-proper-nouns-need-decoder-biasing.md) | Proper nouns unrecognisable by every STT engine — needs biasing | stt |

## Low

| id | title | area |
|---|---|---|
| [0004](0004-stt-slower-in-pipeline-than-isolation.md) | STT occasionally spikes in the live pipeline (mostly not reproducible) | stt/threading |
| [0006](0006-malayalam-support-parked.md) | Malayalam support parked until the English path is solid | multilingual |

## Closed

| id | title | closed |
|---|---|---|
| [0011](0011-tts-deletes-out-of-lexicon-words.md) | TTS silently deleted words outside Kokoro's lexicon, including "Privoice" and the user's name | 2026-08-12 |
| [0010](0010-tts-latency-scales-with-sentence-length.md) | TTS time-to-first-audio scales linearly with first-sentence length | 2026-08-12 |
| [0009](0009-llm-stream-stalls-behind-playback.md) | LLM stream not drained while audio plays | 2026-08-12 |
| [0002](0002-gap-before-second-sentence.md) | Audible gap before the second sentence | 2026-08-12 |
| [0008](0008-fatal-gil-error-on-shutdown.md) | Fatal GIL error printed on Ctrl+C shutdown (cosmetic) | 2026-08-12 |

## Suggested order

Revised 2026-08-12, after the latency work took first-audio from 1,699 ms median
to 1,112 ms (see `docs/BENCHMARK.md`).

`0001` is the only urgent one left and is now the most valuable thing in here:
barge-in is the last part of the turn-taking path that has never been properly
measured, and `issues/0002` — which it was waiting on — has landed. It also
finally has a test rig: `scripts/replay_conversation.py` drives the real loop
without a mic, so overlapping speech can be scripted instead of performed.
Note that its two symptoms are opposite failures (missing real interruptions,
firing on its own tail) and a single threshold cannot fix both.

`0003` and `0004` are both open mainly as records of what was measured and found
*not* to be the problem. Read them before re-attempting either.

`0007` is untouched and unchanged: it is the STT twin of the TTS defect closed
as `0011`, and the harder half — a synthesizer can be given phonemes, a
recognizer has to be biased.

`0005`/`0006` remain deliberately deferred.
