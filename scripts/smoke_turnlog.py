"""Regression test for the per-turn latency log — real `respond()`, real worker thread.

Why this and not `smoke_pipeline.py`: that script calls the engines directly and
never touches `respond()` or `TTSPlayer`, which is where every timing in
`turn_log.jsonl` is actually computed. Per `learning.md`, anything on the turn
path has to be exercised over *multiple turns* on a *persistent worker thread*
or the test is structurally blind to the bugs that live there.

Drives the same code the live agent drives, with `say`-generated speech instead
of a mic, then checks that each row's stages reconcile against its measured
first-audio time.

    uv run python scripts/smoke_turnlog.py
    LLM_BACKEND=venice uv run python scripts/smoke_turnlog.py
    LLM_BACKEND=venice uv run python scripts/smoke_turnlog.py --turns 4

Audio plays out loud — it runs the real playback path, which is the only way
to time when sound actually reached the speaker.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Default to a throwaway log: a test must not pollute the benchmark data it
# exists to protect. An explicit TURN_LOG still wins, so this script doubles as
# a way to generate comparable A/B rows without a mic.
_TMP_LOG = Path(
    os.environ.setdefault(
        "TURN_LOG",
        str(Path(tempfile.mkdtemp(prefix="turnlog-smoke-")) / "turn_log.jsonl"),
    )
)

from voice_agent import (  # noqa: E402
    SR,
    SYSTEM_PROMPT,
    TTSPlayer,
    load_models,
    respond,
)

PROMPTS = [
    "Hello there, can you hear me clearly?",
    "What is the fastest way to boil an egg?",
    "Tell me something surprising about the ocean.",
    "Thanks, that was helpful. Goodbye.",
]


def synth(text: str) -> np.ndarray:
    """macOS `say` -> 16 kHz mono float32, the same shape the mic loop produces."""
    with tempfile.TemporaryDirectory() as d:
        aiff = Path(d) / "u.aiff"
        subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True)
        data, sr = sf.read(str(aiff))
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        n = int(len(data) * SR / sr)
        data = np.interp(
            np.linspace(0, len(data) - 1, n), np.arange(len(data)), data
        )
    return data.astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=3)
    a = ap.parse_args()
    n_turns = max(1, min(a.turns, len(PROMPTS)))

    print(f"log -> {_TMP_LOG}")
    print(f"backend: LLM={os.environ.get('LLM_BACKEND', 'ollama')} "
          f"STT={os.environ.get('STT_ENGINE', 'parakeet')} "
          f"TTS={os.environ.get('TTS_ENGINE', 'kokoro')}\n")

    models = load_models()
    player = TTSPlayer(models.tts, models.apm)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    state = {"speaking": False, "resume_at": 0.0,
             "user_just_ended": 0.0, "loud_streak": 0, "barge_fired": False}

    # The persistent-worker shape from voice_agent.main(). A thread per turn
    # crashes the interpreter on turn 2 once MLX state is thread-local, so the
    # test has to reproduce the real threading model to be worth anything.
    turn_q: queue.Queue = queue.Queue()
    sem = threading.Semaphore(0)  # released when a turn finishes

    def worker():
        # Never returns, exactly as voice_agent.turn_worker never returns.
        # An earlier version of this test used a None sentinel to shut the
        # worker down cleanly and died with the same fatal GIL error the
        # thread-per-turn bug produced: MLX's thread-local Metal state cannot
        # survive its thread exiting. The daemon thread is left parked on
        # `get()` and the process leaves via os._exit below.
        while True:
            audio, cancel, t_end = turn_q.get()
            try:
                respond(audio, models, history, player, cancel, t_end)
            except Exception as e:
                print(f"[turn failed] {type(e).__name__}: {e}", flush=True)
            finally:
                state["speaking"] = False
                sem.release()

    threading.Thread(target=worker, daemon=True).start()

    for i in range(n_turns):
        text = PROMPTS[i]
        print(f"--- turn {i+1}/{n_turns}: {text!r}")
        audio = synth(text)
        turn_q.put((audio, threading.Event(), time.perf_counter()))
        sem.acquire()  # serialize turns, as the live agent does

    # Verification runs BEFORE teardown on purpose. Closing the audio device
    # trips `issues/0008` — a fatal GIL error during interpreter finalization —
    # which killed this script after the turns had all succeeded but before it
    # could report. A test that cannot print its verdict is not a test.
    code = verify(n_turns)

    for closer in (player.close, models.stt.close, models.tts.close, models.turn.close):
        try:
            closer()
        except Exception:
            pass
    sys.stdout.flush()
    # Skip finalization for the same reason: MLX thread-local Metal state makes
    # a clean exit unreliable here, and issues/0008 is cosmetic, not ours to fix
    # in a test harness.
    os._exit(code)


def verify(n_turns: int) -> int:
    # ---- verify the log ----
    if not _TMP_LOG.exists():
        print("\nFAIL: no log written")
        return 1
    all_rows = [json.loads(x) for x in _TMP_LOG.read_text().splitlines() if x.strip()]
    failures = []
    if len(all_rows) < n_turns:
        failures.append(f"expected {n_turns} rows, got {len(all_rows)}")
    # Only this run's rows: the log may be shared deliberately, to collect A/B
    # rows across backends into one file for scripts/analyze_turns.py.
    rows = all_rows[-n_turns:]
    print(f"\n{len(rows)} rows written (log now holds {len(all_rows)})")

    for i, r in enumerate(rows, 1):
        fa = r.get("first_audio_ms")
        if fa is None:
            failures.append(f"turn {i}: no first_audio_ms (error={r.get('error')!r})")
            continue
        parts = {k: r.get(k) for k in
                 ("dispatch_ms", "stt_ms", "llm_first_sentence_ms", "tts_first_ms")}
        missing = [k for k, v in parts.items() if v is None]
        if missing:
            failures.append(f"turn {i}: missing stage(s) {missing}")
            continue
        # Work done before end-of-speech was declared does not count towards the
        # wait, so it comes back off the sum. It is 0 unless the turn was
        # speculated; leaving it out would make every speculated turn read as
        # broken arithmetic rather than as a faster turn.
        lead = r.get("spec_lead_ms") or 0.0
        expected = sum(parts.values()) - lead
        gap = fa - expected
        # The stages are measured on the same clock and cover the whole path,
        # so they must sum to first-audio. A drift beyond a few frames means a
        # stage is being double-counted or one is unmeasured.
        status = "ok" if abs(gap) < 100 else "DRIFT"
        if status == "DRIFT":
            failures.append(f"turn {i}: stages sum to {expected:.0f}ms "
                            f"but first audio was {fa:.0f}ms (gap {gap:+.0f}ms)")
        print(f"  turn {i}: first-audio {fa:>6.0f}ms = "
              f"dispatch {parts['dispatch_ms']:.0f} + stt {parts['stt_ms']:.0f} + "
              f"llm {parts['llm_first_sentence_ms']:.0f} + "
              f"tts {parts['tts_first_ms']:.0f}"
              + (f" - {lead:.0f} early" if lead > 1 else "")
              + f"  [{status}]")
        for f in ("llm_chunks", "llm_chunk_s", "tts_rtf", "tts_audio_s", "turn_total_ms"):
            if r.get(f) in (None, 0):
                failures.append(f"turn {i}: {f} is {r.get(f)!r}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — every turn logged, every stage reconciles to first audio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
