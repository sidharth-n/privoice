---
id: 0003
title: LLM time-to-first-token is the pipeline's floor, and it is not a decode-rate problem
status: open
priority: moderate
area: llm
opened: 2026-07-30
updated: 2026-08-12
closed:
---

## History — this issue was wrong twice, in different ways

**As originally written (2026-07-30)** it said: "the LLM is slow because it's
dense — swap in a sparse 3B-active MoE, expect ~3.7x", and was marked urgent.
`learning.md` records the disproof: the model already in use *was* a sparse MoE
(`expert_count 128`, `expert_used_count 8`, 100% GPU) that simply decodes at
dense speed, and three of the issue's supporting claims were false — the
recommended tag has no `latest` and cannot be pulled, its real size is 23.94 GB
not 17 GB (above this machine's ~24 GiB wired limit, so it would have run
*slower*), and the 17 GB figure belonged to a dense variant.

**The framing was also wrong.** The title said decode rate dominates latency.
It does not, and never did on the hosted path. Rewritten 2026-08-12 against
measurements rather than a plan.

## What is actually true

On the hybrid path the pipeline ships (`LLM_BACKEND=venice`), time-to-first-audio
breaks down, over 20 replayed conversational turns, as:

```
STT                   74 ms median
LLM first sentence  1011 ms median      <- 84% of the critical path
TTS first audio      194 ms median
```

and of that LLM figure, **952 ms is time-to-first-token** — queueing and prefill,
not decoding. Decode is 106 chunks/s and produces the rest of the first sentence
in ~60 ms.

## It is a floor, not a model choice

Ten Venice models, four prompts each, same client and network:

| model | TTFT | first sentence |
|---|---|---|
| nvidia-nemotron-3-nano-30b-a3b | 880 ms | 917 ms |
| venice-uncensored-role-play | 888 ms | 944 ms |
| **venice-uncensored** (ours) | 851 ms | 971 ms |
| venice-uncensored-1-2 | 885 ms | 978 ms |
| mistral-small-3-2-24b-instruct | 912 ms | 1094 ms |
| gemma-4-uncensored | 1014 ms | 1103 ms |
| qwen3-235b-a22b-instruct-2507 | 928 ms | 1460 ms |
| llama-3.3-70b | 1014 ms | 1491 ms |
| e2ee-gemma-4-26b-a4b-uncensored-p | 1112 ms | 1573 ms |

Every model lands in an 850–1100 ms TTFT band regardless of size, from a 30B-A3B
MoE to a 405B. A 3B-active model is not faster to first token than a 235B one,
which is the tell that this is gateway and queueing time rather than compute.

It is not the network either, and not connection setup:

```
raw TCP connect to api.venice.ai      39 ms
raw TLS handshake                     65 ms
TTFT on a brand-new connection       869 ms
TTFT on a warm reused connection     881 ms
TTFT after 12 s idle, expiry 5 s     925 ms
TTFT after 12 s idle, expiry 300 s   902 ms
```

Connection reuse buys nothing — httpx's default 5 s keepalive expiry looked like
an obvious win and measured as noise. ~810 ms of the 850 is Venice-side.

Prompt size is the one lever that does move it, and only slightly: 849 ms with no
history, 978 ms at 4 turns, 1022 ms at 8. `HISTORY_MAX_TURNS=8` therefore costs
about 173 ms. It is deliberately *not* being cut: 8 turns of memory is a product
feature and 44 ms (8 -> 4) is not worth half of it.

## What was done instead

Since the LLM cannot be made faster, the work moved off the critical path:

- **`issues/0010`** cut TTS first-audio by shortening the model's opening
  sentence — 481 ms projected to 268 ms.
- **Speculative dispatch** starts STT and the LLM call during Silero's
  end-of-speech window, removing a median 342 ms from the measured wait.
- **`issues/0009`/`0002`** overlapped decode and synthesis with playback.

Net: 1,699 ms median first-audio to 1,112 ms, without the LLM getting any faster.

## What is left

Only two things would actually move the remaining ~950 ms, and both are choices
rather than fixes:

1. **A local LLM with a warm KV cache** has no gateway in front of it. The
   earlier local measurements are not encouraging (first sentence ~1.0–1.3 s at
   14–16 tok/s) but they were taken before any of this year's work, and a small
   local model used *only* for the opening clause — with the hosted model taking
   over from sentence two — would hide the entire hosted TTFT. That is a real
   design, not a tweak.
2. **Ask Venice** whether the 850 ms floor is expected for this account/region.
   It is uniform across model sizes, which usually means routing rather than
   inference, and it may simply be answerable.

Neither is urgent. The published latency claim is met without them.

## Do not

Re-open this as a model swap. It has been tested across nine alternatives and
the spread in TTFT is 260 ms, most of which is paid back in longer first
sentences. Swapping models also costs the uncensored positioning, which is the
product.
