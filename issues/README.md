# Issues

Local issue tracker. One file per issue: `NNNN-slug.md`, frontmatter tags
(`id`, `title`, `status`, `priority`, `area`, `opened`, `updated`, `closed`).
New issues start from `_template.md`.

**Open: 10** — Urgent 4 · Moderate 4 · Low 2

## Urgent

| id | title | area |
|---|---|---|
| [0001](0001-barge-in-ignores-overlapping-speech.md) | Barge-in ignores overlapping speech, and misfires at end of replies | audio/aec |
| [0002](0002-gap-before-second-sentence.md) | Audible gap before the second sentence (TTS generates synchronously) | tts/playback |
| [0003](0003-llm-decode-rate-dominates-latency.md) | LLM decode rate dominates latency — swap to a 3B-active MoE | llm |
| [0009](0009-llm-stream-stalls-behind-playback.md) | LLM stream is not drained while audio plays — sentence 2 is never requested early | llm/playback |

## Moderate

| id | title | area |
|---|---|---|
| [0004](0004-stt-slower-in-pipeline-than-isolation.md) | STT is 5-10x slower in the live pipeline than in isolation | stt/threading |
| [0005](0005-semantic-turn-detection-parked.md) | Semantic turn detection parked — needs offline evaluation, not live debugging | vad/turn-taking |
| [0007](0007-proper-nouns-need-decoder-biasing.md) | Proper nouns unrecognisable by every STT engine — needs biasing | stt |
| [0010](0010-tts-latency-scales-with-sentence-length.md) | TTS time-to-first-audio scales linearly with first-sentence length (~9 ms/char) | tts/latency |

## Low

| id | title | area |
|---|---|---|
| [0006](0006-malayalam-support-parked.md) | Malayalam support parked until the English path is solid | multilingual |
| [0008](0008-fatal-gil-error-on-shutdown.md) | Fatal GIL error printed on Ctrl+C shutdown (cosmetic) | threading |

## Suggested order

Revised 2026-08-12 after the first real-conversation measurements
(`docs/BENCHMARK.md`, "What a real conversation measures"):

`0010` first — it is a prompt change, not a code change, and on the measured
~9 ms/char it buys several hundred milliseconds off time-to-first-audio for
nothing. Then `0009`, which is the larger half of `0002` and must land before
it: pre-synthesizing sentence two is pointless while its tokens have not been
requested. Then `0002`, then `0001` (which depends on `0002`).

`0003` is **not** next despite being marked urgent — `learning.md` records that
its central premise was disproven, and it needs rewriting rather than
executing. `0004` now has real numbers behind it (106 ms/s of speech in-pipeline
vs ~29 ms/s isolated) and is worth investigating once the playback path settles.
`0005`/`0006` remain deliberately deferred.
