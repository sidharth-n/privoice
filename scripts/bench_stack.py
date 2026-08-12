"""Per-slot latency benchmark for the voice pipeline.

Why per-slot rather than end-to-end: the end-to-end number tells you the agent
feels slow, not which model to change. This measures STT, LLM and TTS
independently so a swap can be judged on the slot it actually affects.

Every published latency figure we found for these models came from an M4 Max,
an H100, or a vendor blog. This produces numbers for *this* machine.

Usage
-----
    # baseline, all three slots
    uv run python scripts/bench_stack.py

    # compare TTS engines (needs the engine registered in engines.py)
    TTS_ENGINE=kokoro   uv run python scripts/bench_stack.py --slot tts
    TTS_ENGINE=omnivoice uv run python scripts/bench_stack.py --slot tts

    # compare LLMs without touching the rest of the stack
    uv run python scripts/bench_stack.py --slot llm \
        --llm 0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M \
        --llm huihui_ai/Qwen3.6-abliterated:35b

    # Malayalam
    uv run python scripts/bench_stack.py --lang ml

Results append to bench_results.jsonl so runs accumulate and stay comparable.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    """Minimal .env reader so VENICE_API_KEY works without an extra dependency.

    Real environment variables win, so `VENICE_API_KEY=... uv run ...` still
    overrides the file.
    """
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()

from voice_agent import (  # noqa: E402
    KEEP_ALIVE,
    LLM_MODEL,
    OLLAMA_URL,
    SYSTEM_PROMPT,
)
from voice_agent import sentence_stream  # noqa: E402
from engines import build_stt, build_tts  # noqa: E402

RESULTS = ROOT / "bench_results.jsonl"

# One prompt per language. The Malayalam line is here because Malayalam is a
# first-class requirement, and because several models in this stack claim
# support for it without any published Malayalam numbers.
PROMPTS = {
    "en": "Hey, what should I make for dinner tonight?",
    "ml": "നാളെ കാലാവസ്ഥ എങ്ങനെയായിരിക്കും?",  # "What will the weather be like tomorrow?"
}
SAY_VOICE = {"en": "Samantha", "ml": None}  # macOS `say` has no Malayalam voice


# ─────────────────────────────── helpers ──────────────────────────────


def synth_wav(text: str, out_path: Path, voice: str | None) -> np.ndarray:
    """Render text with macOS `say` and return 16 kHz mono float32."""
    aiff = out_path.with_suffix(".aiff")
    cmd = ["say"]
    if voice:
        cmd += ["-v", voice]
    cmd += ["-o", str(aiff), text]
    subprocess.run(cmd, check=True)
    data, sr = sf.read(str(aiff))
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        ratio = 16000 / sr
        idx = (np.arange(int(len(data) * ratio)) / ratio).astype(np.int64)
        data = data[idx]
    aiff.unlink(missing_ok=True)
    return data.astype(np.float32)


def summarize(samples: list[float]) -> dict:
    """Median is the headline; min/max expose variance worth knowing about."""
    return {
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "n": len(samples),
    }


def record(row: dict) -> None:
    row["machine"] = f"{platform.machine()} {platform.mac_ver()[0]}"
    with RESULTS.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─────────────────────────────── slots ────────────────────────────────


def bench_stt(audio: np.ndarray, repeat: int, lang: str) -> dict:
    t0 = time.perf_counter()
    stt = build_stt()
    load_ms = (time.perf_counter() - t0) * 1000

    stt.warmup()  # first call pays lazy init; keep it out of the samples
    samples, texts = [], []
    for _ in range(repeat):
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        samples.append((time.perf_counter() - t0) * 1000)
        texts.append(text)
    stt.close()

    row = {
        "slot": "stt",
        "engine": stt.name,
        "lang": lang,
        "load_ms": round(load_ms, 1),
        "transcribe": summarize(samples),
        "transcript": texts[-1],
        "empty": not texts[-1].strip(),
    }
    record(row)
    return row


def bench_tts(sentences: list[str], repeat: int, lang: str) -> dict:
    t0 = time.perf_counter()
    tts = build_tts()
    load_ms = (time.perf_counter() - t0) * 1000

    tts.warmup()
    ttfa, rtf = [], []
    for _ in range(repeat):
        first = None
        total_samples = 0
        t0 = time.perf_counter()
        for sent in sentences:
            for arr in tts.stream(sent):
                if first is None:
                    first = (time.perf_counter() - t0) * 1000
                total_samples += arr.shape[0]
        elapsed = time.perf_counter() - t0
        if first is None:
            raise SystemExit(f"TTS engine {tts.name} produced no audio")
        ttfa.append(first)
        rtf.append((total_samples / tts.sample_rate) / elapsed)
    tts.close()

    row = {
        "slot": "tts",
        "engine": tts.name,
        "lang": lang,
        "sample_rate": tts.sample_rate,
        "load_ms": round(load_ms, 1),
        "ttfa": summarize(ttfa),
        "realtime_factor": round(statistics.median(rtf), 2),
    }
    record(row)
    return row


def bench_llm(model: str, prompt: str, repeat: int, lang: str) -> dict:
    """Measure TTFT, time-to-first-sentence, and decode rate.

    Time-to-first-sentence is the number that matters and TTFT is not a proxy
    for it. TTS is driven per sentence, so nothing is audible until the first
    sentence terminator arrives — which makes first-sentence latency a function
    of decode rate, not of prefill. On the baseline the two differ by ~6x.

    think=false is mandatory: with thinking on, `response` stays empty for
    5-30 s while the model fills a `thinking` field, which no voice agent can
    tolerate. Measuring with it on would benchmark a config we never ship.
    """
    client = httpx.Client(timeout=httpx.Timeout(180.0))
    ttft, first_sentence, tok_s, replies = [], [], [], []

    for i in range(repeat + 1):  # first iteration warms/loads, then discarded
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "think": False,
            "keep_alive": KEEP_ALIVE,
        }
        t0 = time.perf_counter()
        first = None
        sent_at = None
        text = []
        final = {}

        def tokens():
            """Yield content chunks, timestamping the first one on the way past."""
            nonlocal first, final
            with client.stream("POST", OLLAMA_URL, json=body) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    obj = json.loads(line)
                    chunk = (obj.get("message") or {}).get("content") or ""
                    if chunk and first is None:
                        first = (time.perf_counter() - t0) * 1000
                    if chunk:
                        text.append(chunk)
                        yield chunk
                    if obj.get("done"):
                        final = obj

        # Drive the same sentence splitter the live agent uses, so the measured
        # first-sentence time is the one the pipeline would really see.
        for _ in sentence_stream(tokens()):
            if sent_at is None:
                sent_at = (time.perf_counter() - t0) * 1000

        if i == 0:
            continue  # discard the cold/loading iteration
        if first is not None:
            ttft.append(first)
        if sent_at is not None:
            first_sentence.append(sent_at)
        # eval_duration is nanoseconds of pure decode, excluding prompt prefill
        if final.get("eval_count") and final.get("eval_duration"):
            tok_s.append(final["eval_count"] / (final["eval_duration"] / 1e9))
        replies.append("".join(text).strip())

    client.close()
    if not ttft:
        raise SystemExit(f"model {model} returned no content — is it pulled?")

    row = {
        "slot": "llm",
        "engine": model,
        "lang": lang,
        "ttft": summarize(ttft),
        "first_sentence": summarize(first_sentence) if first_sentence else None,
        "tokens_per_s": round(statistics.median(tok_s), 1) if tok_s else None,
        "reply": replies[-1],
    }
    record(row)
    return row


def bench_llm_venice(model: str, prompt: str, repeat: int, lang: str) -> dict:
    """Same three measurements as bench_llm, against Venice's hosted API.

    Kept as a separate function rather than a branch inside bench_llm because
    the wire formats have nothing in common: Ollama streams bare JSON objects
    per line with `message.content`, OpenAI-compatible endpoints stream SSE
    `data:` frames with `choices[0].delta.content` and a `[DONE]` sentinel.

    Two deliberate fairness choices, both of which change the number:

      * `include_venice_system_prompt: false`. Venice injects its own system
        prompt by default — measured at ~1,568 prompt tokens on a 10-token
        request — which inflates prefill against a local run that only carries
        SYSTEM_PROMPT. We want the same context on both sides.
      * The same temperature/top_p/cap as the live agent's stream_llm, so this
        measures the shipped configuration rather than a benchmark-only one.

    Decode rate comes from usage counts over wall-clock decode time. That is
    not identical to Ollama's `eval_duration`, which excludes prefill exactly;
    here first-token time is subtracted instead, so the two are close but not
    interchangeable. The number to compare across backends is first_sentence.
    """
    base = os.environ.get("VENICE_BASE_URL", "https://api.venice.ai/api/v1").rstrip("/")
    key = os.environ.get("VENICE_API_KEY", "").strip()
    if not key:
        raise SystemExit("VENICE_API_KEY not set — put it in .env")

    client = httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(180.0),
    )
    ttft, first_sentence, tok_s, replies, costs = [], [], [], [], []

    for i in range(repeat + 1):  # first iteration warms the connection, then discarded
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.85,
            "top_p": 0.95,
            "max_completion_tokens": 220,
            "venice_parameters": {"include_venice_system_prompt": False},
        }
        t0 = time.perf_counter()
        first = None
        sent_at = None
        text = []
        usage = {}
        cost = None

        def tokens():
            nonlocal first, usage, cost
            with client.stream("POST", "/chat/completions", json=body) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("cost"):
                        cost = obj["cost"].get("usd")
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    chunk = (choices[0].get("delta") or {}).get("content") or ""
                    if chunk and first is None:
                        first = (time.perf_counter() - t0) * 1000
                    if chunk:
                        text.append(chunk)
                        yield chunk

        for _ in sentence_stream(tokens()):
            if sent_at is None:
                sent_at = (time.perf_counter() - t0) * 1000
        wall_ms = (time.perf_counter() - t0) * 1000

        if i == 0:
            continue
        if first is not None:
            ttft.append(first)
        if sent_at is not None:
            first_sentence.append(sent_at)
        out_tokens = usage.get("completion_tokens")
        if out_tokens and first is not None and wall_ms > first:
            tok_s.append(out_tokens / ((wall_ms - first) / 1000))
        if cost is not None:
            costs.append(cost)
        replies.append("".join(text).strip())

    client.close()
    if not ttft:
        raise SystemExit(f"venice model {model} returned no content")

    row = {
        "slot": "llm",
        "backend": "venice",
        "engine": model,
        "lang": lang,
        "ttft": summarize(ttft),
        "first_sentence": summarize(first_sentence) if first_sentence else None,
        "tokens_per_s": round(statistics.median(tok_s), 1) if tok_s else None,
        "usd_per_call": round(statistics.median(costs), 6) if costs else None,
        "reply": replies[-1],
    }
    record(row)
    return row


# ─────────────────────────────── main ─────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["stt", "tts", "llm", "all"], default="all")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--lang", default="en", choices=sorted(PROMPTS))
    ap.add_argument("--text", default=None, help="override the prompt text")
    ap.add_argument(
        "--llm",
        action="append",
        default=None,
        help="Ollama tag to benchmark; repeatable. Defaults to LLM_MODEL.",
    )
    ap.add_argument(
        "--venice-llm",
        action="append",
        default=None,
        help="Venice model id to benchmark over the hosted API; repeatable.",
    )
    ap.add_argument(
        "--no-local-llm",
        action="store_true",
        help="Skip the Ollama LLM run (useful when only measuring Venice).",
    )
    args = ap.parse_args()

    prompt = args.text or PROMPTS[args.lang]
    models = args.llm or [LLM_MODEL]
    print(f"=== bench: slot={args.slot} lang={args.lang} repeat={args.repeat}")
    print(f"    prompt: {prompt!r}\n")

    if args.slot in ("stt", "all"):
        voice = SAY_VOICE.get(args.lang)
        if args.lang != "en" and voice is None:
            # No macOS voice for this language, so we cannot synthesize a probe.
            # Reporting that honestly beats benchmarking English and mislabeling it.
            print(f"[stt] skipped: macOS `say` has no {args.lang} voice.")
            print("      Record a real clip and pass it via --text is not enough here;")
            print("      supply a wav through bench_stt() directly for this language.\n")
        else:
            with tempfile.TemporaryDirectory() as td:
                audio = synth_wav(prompt, Path(td) / "probe.wav", voice)
            r = bench_stt(audio, args.repeat, args.lang)
            print(f"[stt] {r['engine']}: {r['transcribe']['median_ms']} ms median "
                  f"(load {r['load_ms']} ms)")
            print(f"      -> {r['transcript']!r}\n")

    if args.slot in ("llm", "all"):
        for m in (args.venice_llm or []):
            r = bench_llm_venice(m, prompt, args.repeat, args.lang)
            fs = r["first_sentence"]
            print(f"[llm] venice:{m}")
            print(f"      TTFT           {r['ttft']['median_ms']} ms median "
                  f"(min {r['ttft']['min_ms']}, max {r['ttft']['max_ms']})")
            print(f"      first sentence {fs['median_ms'] if fs else '?'} ms median "
                  f"<- this is what gates audio")
            print(f"      decode         {r['tokens_per_s']} tok/s")
            print(f"      cost           ${r['usd_per_call']} per call")
            print(f"      -> {r['reply'][:160]!r}\n")

        for m in ([] if args.no_local_llm else models):
            r = bench_llm(m, prompt, args.repeat, args.lang)
            fs = r["first_sentence"]
            print(f"[llm] {m}")
            print(f"      TTFT           {r['ttft']['median_ms']} ms median "
                  f"(min {r['ttft']['min_ms']}, max {r['ttft']['max_ms']})")
            print(f"      first sentence {fs['median_ms'] if fs else '?'} ms median "
                  f"<- this is what gates audio")
            print(f"      decode         {r['tokens_per_s']} tok/s")
            print(f"      -> {r['reply'][:160]!r}\n")

    if args.slot in ("tts", "all"):
        sentences = ["Sure, let me think about that for a second.", prompt]
        r = bench_tts(sentences, args.repeat, args.lang)
        print(f"[tts] {r['engine']} @ {r['sample_rate']} Hz: "
              f"TTFA {r['ttfa']['median_ms']} ms median, "
              f"{r['realtime_factor']}x realtime (load {r['load_ms']} ms)\n")

    print(f"appended to {RESULTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
