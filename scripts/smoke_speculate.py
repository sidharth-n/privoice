"""Regression test for speculative dispatch — the worker protocol, without a mic.

Silero only reports end-of-speech after a fixed silence timeout, so the pipeline
starts STT and the model's first token *during* that window and confirms or
discards the result when the real end arrives (`start_reply` / `speak_reply`).
That is turn-taking logic, and `learning.md` (2026-07-30) is explicit that this
class of bug only shows up over multiple turns on a persistent worker thread.

So this drives the same three-message protocol the mic loop drives —
speculate / abort / commit — over a persistent worker, and checks the three
outcomes that matter:

  1. speculate then commit      -> the guess is reused, spec_lead_ms > 0,
                                   and first-audio drops by roughly the lead
  2. speculate then abort       -> the guess is dropped, the next turn is clean,
                                   and no phantom user message is left behind
  3. commit with no speculation -> unchanged behaviour, spec_lead_ms == 0

The conversation history is the thing most likely to break here: a speculative
reply must not appear in it until it is committed, or the model starts reasoning
from turns the user never took.

    LLM_BACKEND=venice uv run python scripts/smoke_speculate.py

Audio plays out loud — it runs the real playback path.
"""
from __future__ import annotations

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

_TMP_LOG = Path(
    os.environ.setdefault(
        "TURN_LOG",
        str(Path(tempfile.mkdtemp(prefix="turnlog-spec-")) / "turn_log.jsonl"),
    )
)

from voice_agent import (  # noqa: E402
    SR,
    SYSTEM_PROMPT,
    PendingReply,
    TTSPlayer,
    load_models,
    speak_reply,
    start_reply,
)


def synth(text: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        aiff = Path(d) / "u.aiff"
        subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True)
        data, sr = sf.read(str(aiff))
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        n = int(len(data) * SR / sr)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return data.astype(np.float32)


def main() -> int:
    print(f"log -> {_TMP_LOG}")
    print(f"backend: LLM={os.environ.get('LLM_BACKEND', 'ollama')}\n")

    models = load_models()
    player = TTSPlayer(models.tts, models.apm)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    # The persistent-worker shape from voice_agent.main(). Reproduced rather
    # than imported because main() owns the mic loop; the protocol below is the
    # part under test.
    turn_q: queue.Queue = queue.Queue()
    done = threading.Semaphore(0)
    failures: list[str] = []

    def worker():
        pending: PendingReply | None = None
        while True:
            msg = turn_q.get()
            kind = msg[0]
            try:
                if kind == "speculate":
                    _, audio, cancel = msg
                    if pending is not None:
                        pending.cancel.set()
                    pending = start_reply(audio, models, history, cancel)
                elif kind == "abort":
                    if pending is not None:
                        pending.cancel.set()
                        pending = None
                elif kind == "commit":
                    _, audio, cancel, t_end = msg
                    p, pending = pending, None
                    if p is None or p.cancel.is_set() or p.cancel is not cancel:
                        if p is not None:
                            p.cancel.set()
                        p = start_reply(audio, models, history, cancel)
                    else:
                        p.audio = audio
                    speak_reply(p, models, history, player, cancel, t_end)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{kind}: {type(e).__name__}: {e}")
            finally:
                if kind in ("commit", "abort"):
                    done.release()

    threading.Thread(target=worker, daemon=True).start()

    # `lead_ms` stands in for the silence window the mic loop would be sitting
    # through. Real speech is padded with silence the way the VAD buffer is, so
    # the committed clip is genuinely longer than the speculated snapshot.
    LEAD_MS = 300
    pad = np.zeros(int(SR * LEAD_MS / 1000), dtype=np.float32)

    # ---- case 1: speculate, then commit. The guess should be reused. ----
    print("--- case 1: speculate -> commit (expect reuse)")
    audio = synth("What is the capital of France?")
    cancel = threading.Event()
    turn_q.put(("speculate", audio, cancel))
    time.sleep(LEAD_MS / 1000.0)
    turn_q.put(("commit", np.concatenate([audio, pad]), cancel, time.perf_counter()))
    done.acquire()

    # ---- case 2: speculate, then abort. Nothing may leak into history. ----
    print("\n--- case 2: speculate -> abort (expect discard, no history entry)")
    hist_before = len(history)
    stale = synth("Actually wait, I meant")
    stale_cancel = threading.Event()
    turn_q.put(("speculate", stale, stale_cancel))
    time.sleep(LEAD_MS / 1000.0)
    turn_q.put(("abort",))
    done.acquire()
    if len(history) != hist_before:
        failures.append(
            f"aborted speculation changed history: {hist_before} -> {len(history)} "
            "messages. A reply the user never received is now in the prompt."
        )
    else:
        print(f"  history unchanged at {len(history)} messages  [ok]")

    # ---- case 3: commit with no speculation. Unchanged behaviour. ----
    print("\n--- case 3: commit with no speculation (expect spec_lead_ms == 0)")
    audio = synth("Thanks, that is all for now.")
    cancel = threading.Event()
    turn_q.put(("commit", audio, cancel, time.perf_counter()))
    done.acquire()

    code = verify(failures)
    for closer in (player.close, models.stt.close, models.tts.close, models.turn.close):
        try:
            closer()
        except Exception:
            pass
    sys.stdout.flush()
    os._exit(code)


def verify(failures: list[str]) -> int:
    rows = [json.loads(x) for x in _TMP_LOG.read_text().splitlines() if x.strip()]
    if len(rows) != 2:
        failures.append(f"expected 2 logged turns (cases 1 and 3), got {len(rows)}")
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    speculated, plain = rows
    print("\nresults:")
    for label, r in (("speculated", speculated), ("plain", plain)):
        lead = r.get("spec_lead_ms") or 0.0
        parts = (r["dispatch_ms"], r["stt_ms"], r["llm_first_sentence_ms"],
                 r["tts_first_ms"])
        drift = r["first_audio_ms"] - (sum(parts) - lead)
        print(f"  {label:<11} first-audio {r['first_audio_ms']:>6.0f}ms  "
              f"lead {lead:>5.0f}ms  stt {r['stt_ms']:.0f}  "
              f"llm {r['llm_first_sentence_ms']:.0f}  tts {r['tts_first_ms']:.0f}  "
              f"drift {drift:+.0f}ms")
        if abs(drift) > 100:
            failures.append(
                f"{label}: stages minus lead give {sum(parts) - lead:.0f}ms but "
                f"first audio was {r['first_audio_ms']:.0f}ms (drift {drift:+.0f}ms)"
            )

    if (speculated.get("spec_lead_ms") or 0) < 50:
        failures.append(
            f"speculated turn reports only {speculated.get('spec_lead_ms')}ms of "
            "lead — the guess was not reused, so speculation bought nothing"
        )
    if (plain.get("spec_lead_ms") or 0) > 1:
        failures.append(
            f"unspeculated turn reports {plain.get('spec_lead_ms')}ms of lead; "
            "it should be exactly 0"
        )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — guess reused when valid, discarded without trace when not, "
          "and the unspeculated path is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
