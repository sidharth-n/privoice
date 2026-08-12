"""Per-turn latency log for real conversations.

Why this exists: `scripts/bench_stack.py` measures each slot in isolation
against a fixed prompt. That is the right shape for "how fast is Kokoro" and
the wrong shape for the question this repo actually cares about — how long a
turn takes when a person is talking to the thing, with real speech of variable
length and a conversation history that grows in the prompt. Those two differ:
`issues/0004` exists precisely because STT measured 5-10x slower in the live
pipeline than in isolation, and no isolated benchmark could have shown it.

Every live turn already computes these numbers on its way past. This writes
them down, so holding a conversation *is* a benchmark run.

One JSON object per turn, appended to `turn_log.jsonl`. Read it with
`scripts/analyze_turns.py`.

Env:
    TURN_LOG        path to the log, or `off` to disable. Default turn_log.jsonl
    TURN_LOG_TEXT   `0` logs lengths instead of transcripts. Default `1`.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _log_path() -> Path | None:
    raw = os.environ.get("TURN_LOG", "turn_log.jsonl").strip()
    if not raw or raw.lower() in {"off", "0", "none", "false"}:
        return None
    p = Path(raw)
    return p if p.is_absolute() else _ROOT / p


def _keep_text() -> bool:
    return os.environ.get("TURN_LOG_TEXT", "1").strip() not in {"0", "false", "no"}


@dataclass
class TurnMetrics:
    """One conversational turn, from end-of-speech to end-of-reply.

    Every duration is milliseconds and measured from the same origin — the
    moment Silero declared the user's utterance over — because that is the
    instant the human starts waiting. Stage times that start anywhere else
    (the old `[ttfa]` print began its clock *after* STT) flatter the pipeline
    by hiding whatever ran before them.
    """

    # what produced this turn — without it, rows from different configurations
    # pool together and the medians are meaningless
    llm_backend: str = ""
    llm_model: str = ""
    stt_engine: str = ""
    tts_engine: str = ""

    ts: str = ""
    audio_in_s: float = 0.0

    # stages
    dispatch_ms: float = 0.0            # end-of-speech -> worker picked the turn up
    # Work already done when end-of-speech was declared. Silero only reports the
    # end of an utterance after a fixed silence timeout has elapsed, so that
    # window is dead time the pipeline can spend on STT and on the model's first
    # token. When it does, the stages no longer sum to first-audio — they sum to
    # first-audio *plus* this. 0 on the unspeculated path.
    spec_lead_ms: float = 0.0
    stt_ms: float = 0.0
    llm_ttft_ms: float | None = None    # first token out of the model
    llm_first_sentence_ms: float | None = None   # first sentence handed to TTS
    llm_total_ms: float | None = None   # last token
    llm_chunks: int = 0                 # stream deltas, not tokenizer tokens
    llm_chunk_s: float | None = None    # chunks per second, decode-rate proxy
    tts_first_ms: float | None = None   # first sentence: text in -> first audio out
    tts_synth_ms: float = 0.0           # synthesis compute across the whole reply
    tts_audio_s: float = 0.0            # audio actually produced
    tts_rtf: float | None = None        # realtime factor (audio_s / synth_s)

    # the two numbers a listener perceives
    first_audio_ms: float | None = None  # end-of-speech -> first sample at speaker
    turn_total_ms: float = 0.0           # end-of-speech -> reply finished playing

    interrupted: bool = False
    ok: bool = True
    error: str = ""
    sentences: list[dict] = field(default_factory=list)
    user_text: str = ""
    reply_text: str = ""


def log_turn(m: TurnMetrics) -> None:
    """Append one row. Never raises — a logging fault must not kill a turn."""
    path = _log_path()
    if path is None:
        return
    try:
        row = asdict(m)
        row["ts"] = row["ts"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not _keep_text():
            # Transcripts are the same class of private data as
            # .voice_history.json. Keep the shape, drop the words.
            row["user_chars"] = len(row.pop("user_text", ""))
            row["reply_chars"] = len(row.pop("reply_text", ""))
            for s in row.get("sentences", []):
                s.pop("text", None)
        for k, v in row.items():
            if isinstance(v, float):
                row[k] = round(v, 2)
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:  # noqa: BLE001 — logging must never be fatal
        print(f"[turnlog] not written: {type(e).__name__}: {e}", flush=True)


def summary_line(m: TurnMetrics) -> str:
    """The one-line terminal readout for a finished turn.

    Deliberately prints every stage on the critical path to first audio, and
    prints them as a sum that reconciles:

        dispatch + stt + llm-first-sentence + tts-first - spec_lead == first_audio

    When it doesn't, the missing milliseconds are real and worth chasing. The
    `spec_lead` term is what a turn got done before the clock started; without
    it a speculated turn looks like it broke the arithmetic.
    """
    def ms(v: float | None) -> str:
        return "—" if v is None else f"{v:.0f}"

    lead = f" - {ms(m.spec_lead_ms)} early" if m.spec_lead_ms > 1 else ""
    parts = [
        f"[turn] first-audio {ms(m.first_audio_ms)}ms "
        f"= stt {ms(m.stt_ms)} + llm {ms(m.llm_first_sentence_ms)} "
        f"+ tts {ms(m.tts_first_ms)}{lead}",
        f"ttft {ms(m.llm_ttft_ms)}ms · {m.llm_chunks} chunks"
        + (f" @ {m.llm_chunk_s:.1f}/s" if m.llm_chunk_s else ""),
    ]
    if m.tts_rtf is not None:
        parts.append(f"tts {m.tts_rtf:.1f}x rt")
    parts.append(f"total {ms(m.turn_total_ms)}ms")
    return "   | ".join(parts)


def now() -> float:
    return time.perf_counter()
