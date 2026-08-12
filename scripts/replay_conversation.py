"""Drive a whole conversation through the real turn-taking loop, without a mic.

`bench_stack.py` measures slots in isolation and `smoke_turnlog.py` calls
`respond()` directly. Neither one runs `run_conversation()` — the loop that owns
VAD, end-of-speech detection, barge-in and speculative dispatch — so neither can
measure what a turn actually costs, and `learning.md` (2026-07-30) records that
this is exactly where the bugs and the milliseconds both live.

This feeds `say`-generated speech into that loop as 10 ms frames, with real
silence between utterances, so every stage runs the way it does with a person
talking: Silero decides when the utterance ended, speculation fires during the
silence window, and `turn_log.jsonl` gets a row per turn.

What it is NOT: a substitute for talking to the thing. Synthesized speech is
cleaner than a room, there is no echo path to cancel, and one voice is not a
sample of voices. Numbers from here are a floor, not a promise.

    LLM_BACKEND=venice uv run python scripts/replay_conversation.py
    LLM_BACKEND=venice SPECULATE=0 uv run python scripts/replay_conversation.py
    ... --turns 12 --log /tmp/spec-on.jsonl

Audio plays out loud — it runs the real playback path.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Utterances chosen to look like a conversation rather than a quiz: a mix of
# lengths, some questions, some statements, a couple of very short turns. The
# reply's opening phrase drives time-to-first-audio (issues/0010), so a prompt
# set that only invites long explanations measures a system nobody uses.
UTTERANCES = [
    "Hey, are you there?",
    "I was thinking about getting into running, what do you reckon?",
    "How far should I go on the first day?",
    "That sounds about right honestly.",
    "What about shoes, do I need the expensive ones?",
    "Okay. And how often per week?",
    "Hmm, that's more than I expected.",
    "What if it rains?",
    "Fair enough. Anything else I should know before I start?",
    "Alright, thanks. I'll give it a go tomorrow morning.",
    "Actually wait, should I eat before or after?",
    "Perfect, that's really helpful.",
]


def say(text: str, voice: str) -> np.ndarray:
    from voice_agent import SR

    with tempfile.TemporaryDirectory() as d:
        aiff = Path(d) / "u.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        data, sr = sf.read(str(aiff))
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        n = int(len(data) * SR / sr)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return data.astype(np.float32)


def frame_source(clips, gap_s: float, tail_s: float, started, quiet):
    """Yield 10 ms frames in real time: speech, then silence, then the next clip.

    Paced with `sleep` rather than dumped at once, deliberately. The loop under
    test makes timing decisions — how long silence has lasted, when to
    speculate — and feeding it faster than real time would let it "hear" a
    two-second pause in a few milliseconds and speculate on turns a person
    would never have finished.

    Between utterances it emits silence until `quiet()` says the agent has
    stopped talking, then `gap_s` more. Without that wait the next utterance
    lands on top of the reply and fires barge-in — which is correct behaviour
    from the pipeline and useless for measurement: the first version of this
    script cut four of six replies short and fed the agent fragments like
    "About getting it." A person waits for the answer.
    """
    from voice_agent import APM_FRAME, SR

    def emit(samples: np.ndarray):
        for i in range(0, len(samples) - APM_FRAME, APM_FRAME):
            t0 = time.perf_counter()
            yield samples[i : i + APM_FRAME].copy()
            # Sleep the remainder of the frame's wall-clock duration. The
            # consumer does real work per frame, so this is not a flat 10 ms.
            slack = APM_FRAME / SR - (time.perf_counter() - t0)
            if slack > 0:
                time.sleep(slack)

    def silence(seconds: float):
        return np.zeros(int(SR * seconds), dtype=np.float32)

    # Lead-in silence so the VAD settles before the first word.
    yield from emit(silence(0.5))
    for clip in clips:
        yield from emit(clip)
        # Two waits, and the order matters. Immediately after the clip the
        # agent has not started yet — Silero has not even declared the turn
        # over — so "is it quiet?" is trivially true. Waiting on that alone
        # started the next utterance on top of the reply and fired barge-in on
        # most turns, which is correct pipeline behaviour and useless data.
        waited = 0.0
        while not started() and waited < 5.0:
            yield from emit(silence(0.1))
            waited += 0.1
        while not quiet() and waited < 60.0:
            yield from emit(silence(0.1))
            waited += 0.1
        # Then a real pause, which also has to outlast VAD_MIN_SILENCE_MS or
        # the next utterance joins this one and there is no end-of-turn.
        yield from emit(silence(gap_s))
    yield from emit(silence(tail_s))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=len(UTTERANCES))
    ap.add_argument("--voice", default="Samantha")
    ap.add_argument("--gap", type=float, default=2.5,
                    help="seconds of silence after each utterance")
    ap.add_argument("--log", help="TURN_LOG path (default: the repo's turn_log.jsonl)")
    a = ap.parse_args()
    if a.log:
        os.environ["TURN_LOG"] = a.log

    from voice_agent import (
        SYSTEM_PROMPT,
        TTSPlayer,
        load_models,
        run_conversation,
        warmup,
    )

    texts = UTTERANCES[: max(1, a.turns)]
    print(f"replaying {len(texts)} utterances · voice {a.voice} · gap {a.gap}s")
    print(f"backend: LLM={os.environ.get('LLM_BACKEND', 'ollama')} "
          f"SPECULATE={os.environ.get('SPECULATE', '1')} "
          f"log={os.environ.get('TURN_LOG', 'turn_log.jsonl')}\n")

    # Synthesize everything up front: `say` is a subprocess and doing it inline
    # would inject seconds of dead air into the timing under test.
    clips = [say(t, a.voice) for t in texts]

    models = load_models()
    warmup(models)
    player = TTSPlayer(models.tts, models.apm)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(f"\n=== replaying ({len(clips)} utterances) ===\n", flush=True)
    t0 = time.perf_counter()

    # "The agent has finished": no turn in flight and nothing left for the
    # speaker. `player.audible` alone is not enough — it is False during the
    # second between end-of-speech and the first synthesized frame, which is
    # exactly when the next utterance must not start.
    state: dict = {"speaking": False}

    def started() -> bool:
        return bool(state["speaking"])

    def quiet() -> bool:
        return not state["speaking"] and player.wait_idle(0.0)

    run_conversation(
        models, player, history,
        frame_source(clips, a.gap, 1.5, started, quiet), state,
    )
    print(f"\nreplay finished in {time.perf_counter()-t0:.0f}s")

    for closer in (player.close, models.stt.close, models.tts.close, models.turn.close):
        try:
            closer()
        except Exception:
            pass
    sys.stdout.flush()
    # MLX thread-local Metal state makes a clean exit unreliable (issues/0008).
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
