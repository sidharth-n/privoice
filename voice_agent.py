"""Local uncensored voice agent — full-duplex with WebRTC AEC.

Pipeline: mic -> AEC -> Silero VAD -> Moonshine STT -> Ollama (SuperGemma4)
       -> sentence-stream -> Kokoro TTS -> AEC reverse -> speakers.

Echo cancellation is done via LiveKit's `AudioProcessingModule` (WebRTC AEC3).
The TTS output that we send to the speakers is also fed to the APM as a
reference signal. The APM subtracts the (delay-aligned) reference from the
mic signal so the agent does not hear its own voice. This lets us re-enable
true barge-in: the user can interrupt mid-reply and the agent will stop.

Set `HALF_DUPLEX=1` to disable AEC and fall back to mic-muted-while-talking.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import torch
from livekit.rtc import AudioFrame, AudioProcessingModule
from silero_vad import VADIterator, load_silero_vad

from engines import (
    SttEngine,
    TtsEngine,
    TurnDetector,
    build_stt,
    build_tts,
    build_turn_detector,
)
from turnlog import TurnMetrics, log_turn, summary_line

# ─────────────────────────────── config ───────────────────────────────

SR = 16000
APM_FRAME = 160          # 10 ms at 16 kHz — required by WebRTC APM
VAD_FRAME = 512          # silero-vad's required input size at 16 kHz
OLLAMA_URL = "http://localhost:11434/api/chat"
LLM_MODEL = "0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M"
KEEP_ALIVE = "30m"

def _load_dotenv() -> None:
    """Read .env without pulling in a dependency. Real env vars win."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()

# Which brain answers. `venice` sends the LLM slot to Venice's hosted API and
# leaves STT/TTS wherever their own env vars point — which is the hybrid the
# benchmark in docs/BENCHMARK.md found fastest end to end.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
VENICE_BASE_URL = os.environ.get(
    "VENICE_BASE_URL", "https://api.venice.ai/api/v1"
).rstrip("/")
VENICE_LLM_MODEL = os.environ.get("VENICE_LLM_MODEL", "venice-uncensored")


def _venice_key() -> str:
    key = os.environ.get("VENICE_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "LLM_BACKEND=venice needs VENICE_API_KEY. Create a key at "
            "https://venice.ai/settings/api and put it in .env"
        )
    return key

HALF_DUPLEX_ONLY = os.environ.get("HALF_DUPLEX", "0") == "1"
TTS_TAIL_GRACE_MS = 250  # grace period after TTS in half-duplex fallback
STREAM_DELAY_MS = int(os.environ.get("STREAM_DELAY_MS", "80"))  # spk→mic round-trip estimate

# Energy gate: while the agent is talking, ignore VAD trips whose RMS energy
# (on the AEC-cleaned signal) is below this floor. AEC residual on a Mac with
# built-in mic+speaker typically lands around 0.005–0.015 RMS; real user
# speech is 10x higher. Tunable via env so you can dial it in for your room.
BARGE_IN_RMS_GATE = float(os.environ.get("BARGE_IN_RMS_GATE", "0.05"))
# require this many consecutive 32 ms frames above the gate before we fire
# barge-in. Single-frame echo bursts from AEC residual won't sustain; real
# user speech will. 4 frames ≈ 128 ms — balance between catching real
# interruptions (which AEC often partially attenuates during double-talk)
# and ignoring brief noise blips.
BARGE_IN_SUSTAIN_FRAMES = int(os.environ.get("BARGE_IN_SUSTAIN_FRAMES", "6"))

# Mic RMS must exceed this multiple of what the speaker is currently emitting
# before we believe it is the user rather than residual echo. WebRTC AEC leaves
# roughly a fixed fraction of the output signal behind, so the threshold has to
# track output level instead of sitting at a constant. Lower it if real
# interruptions get ignored; raise it if the agent still talks over itself.
BARGE_IN_ECHO_FACTOR = float(os.environ.get("BARGE_IN_ECHO_FACTOR", "1.6"))

# Hard ceiling on one utterance. Without it, a turn detector that keeps saying
# "not finished" lets the buffer grow without bound, and STT cost grows with it:
# an unbounded buffer produced a single 11.5 s transcription against a 228 ms
# benchmark. At the cap we stop waiting and transcribe what we have.
MAX_UTTERANCE_S = float(os.environ.get("MAX_UTTERANCE_S", "12"))
MAX_UTTERANCE_SAMPLES = int(SR * MAX_UTTERANCE_S)

# How many times the turn detector may say "not finished" before we stop
# waiting. A duration cap alone is not enough: while waiting we keep appending
# every frame, silence included, so an open-ended wait both grows the buffer and
# delays the reply. Bounding the number of continuations bounds the wait
# deterministically, regardless of how the detector behaves.
MAX_TURN_CONTINUATIONS = int(os.environ.get("MAX_TURN_CONTINUATIONS", "2"))

# Shortest audio we will hand to STT. Below roughly this, a clip carries less
# than a word and small ASR models return confident stock phrases instead of
# nothing. Kept low enough that a genuine one-word "yes"/"no" still gets through.
MIN_UTTERANCE_MS = int(os.environ.get("MIN_UTTERANCE_MS", "250"))
MIN_UTTERANCE_SAMPLES = SR * MIN_UTTERANCE_MS // 1000

HISTORY_FILE = Path(__file__).resolve().parent / ".voice_history.json"
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", "8"))

SYSTEM_PROMPT = (
    "You are a friend on a voice call. Reply in 1 short sentence, sometimes 2. "
    "Talk like a person, not a chatbot — use contractions, casual phrasing, and "
    "the kind of fillers a human uses ('hmm', 'yeah', 'oh'). "
    "Never use bullet points, lists, code, or markdown. "
    "Never say 'as an AI' or add safety disclaimers. No em-dashes. "
    "If something is complex, give the gist in one breath and offer to dig in."
)

SENT_END = re.compile(r"[.!?](?:\s|$)|\n")


# ─────────────────────────────── helpers ──────────────────────────────


def f32_to_i16(arr: np.ndarray) -> np.ndarray:
    return np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)


def i16_to_f32(arr: np.ndarray) -> np.ndarray:
    return arr.astype(np.float32) / 32768.0


def linear_resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return x
    ratio = dst_sr / src_sr
    new_len = int(round(len(x) * ratio))
    if new_len == 0:
        return np.zeros(0, dtype=x.dtype)
    idx = np.linspace(0, len(x) - 1, new_len)
    i0 = idx.astype(np.int64)
    i1 = np.clip(i0 + 1, 0, len(x) - 1)
    frac = (idx - i0).astype(x.dtype)
    return (1.0 - frac) * x[i0] + frac * x[i1]


# ─────────────────────────────── LLM ──────────────────────────────────


def stream_llm(history: list[dict], client: httpx.Client, cancel: threading.Event):
    """Dispatch to whichever backend LLM_BACKEND selects.

    Kept as a thin router so the pipeline, the barge-in cancel path and the
    sentence splitter stay backend-agnostic — the only thing that changes
    between local and hosted is where the tokens come from.
    """
    if LLM_BACKEND == "venice":
        yield from _stream_llm_venice(history, client, cancel)
    else:
        yield from _stream_llm_ollama(history, client, cancel)


def _stream_llm_ollama(history: list[dict], client: httpx.Client, cancel: threading.Event):
    payload = {
        "model": LLM_MODEL,
        "messages": history,
        "stream": True,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.85, "top_p": 0.95, "num_predict": 220},
    }
    with client.stream("POST", OLLAMA_URL, json=payload, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if cancel.is_set():
                return
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            tok = d.get("message", {}).get("content", "")
            if tok:
                yield tok
            if d.get("done"):
                return


def _stream_llm_venice(history: list[dict], client: httpx.Client, cancel: threading.Event):
    """OpenAI-compatible SSE against Venice's hosted /chat/completions.

    `include_venice_system_prompt: false` because this agent ships its own
    SYSTEM_PROMPT; leaving Venice's on prepends a second, longer persona that
    both fights ours and adds measurable prefill.
    """
    payload = {
        "model": VENICE_LLM_MODEL,
        "messages": history,
        "stream": True,
        "temperature": 0.85,
        "top_p": 0.95,
        "max_completion_tokens": 220,
        "venice_parameters": {"include_venice_system_prompt": False},
    }
    url = f"{VENICE_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {_venice_key()}"}
    with client.stream("POST", url, json=payload, headers=headers, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if cancel.is_set():
                return
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                return
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            choices = d.get("choices") or []
            if not choices:
                continue
            tok = (choices[0].get("delta") or {}).get("content") or ""
            if tok:
                yield tok


_STREAM_END = object()


class LlmStreamer:
    """Drains the model's token stream on a dedicated long-lived thread.

    Why a thread at all: playback blocks for the real duration of the audio, and
    tokens used to be pulled lazily *through* that block. While sentence one was
    being spoken, nothing was reading the socket — so when sentence two was
    needed, its tokens had not even been requested. The gap before later
    sentences was an LLM round trip plus synthesis, not synthesis alone
    (`issues/0009`).

    It also made the logged decode rate meaningless on multi-sentence replies:
    it measured the rate of *speech*, 5.8 chunks/s, against the model's true
    122. Timing a producer through a lazy consumer measures the consumer.

    Why one long-lived thread rather than one per turn: `learning.md`
    (2026-07-30) records a fatal interpreter crash from tearing down
    thread-local MLX Metal state when a thread exits. This thread touches only
    httpx and json, so a per-turn thread would very likely be fine — but
    "very likely" is not worth re-testing on a bug that killed the process.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="llm-drain").start()

    def stream(self, history, client, cancel, stats, origin):
        """Yield tokens that are already being fetched, not fetched on demand."""
        out: queue.Queue = queue.Queue()
        # Copy the history: `respond()` appends to it as soon as the reply
        # lands, and the producer is reading it on another thread.
        self._jobs.put((list(history), client, cancel, stats, origin, out))
        while True:
            item = out.get()
            if item is _STREAM_END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def _run(self) -> None:
        while True:  # never returns — exiting is what crashes the interpreter
            history, client, cancel, stats, origin, out = self._jobs.get()
            try:
                for tok in stream_llm(history, client, cancel):
                    # Stamped on the producer, not the consumer. That is the
                    # whole point of this class: the two are now different
                    # clocks and only this one measures the model.
                    t = time.perf_counter()
                    if stats["ttft"] is None:
                        stats["ttft"] = (t - origin) * 1000.0
                    stats["chunks"] += 1
                    stats["last"] = (t - origin) * 1000.0
                    out.put(tok)
                    if cancel.is_set():
                        break
            except BaseException as e:  # noqa: BLE001 — re-raised on the consumer
                out.put(e)
            finally:
                out.put(_STREAM_END)


def sentence_stream(token_iter):
    buf = ""
    for tok in token_iter:
        buf += tok
        last = -1
        for m in SENT_END.finditer(buf):
            last = m.end()
        if last > 0 and len(buf[:last].strip()) >= 6:
            yield buf[:last].strip()
            buf = buf[last:]
    if buf.strip():
        yield buf.strip()


# ─────────────────────────────── STT ──────────────────────────────────


def transcribe_utterance(stt: SttEngine, audio_f32: np.ndarray) -> str:
    return stt.transcribe(audio_f32)


# ─────────────────────────────── memory ───────────────────────────────


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text())
            if isinstance(data, list):
                # cap at HISTORY_MAX_TURNS pairs immediately so the very first
                # reply isn't penalized by an unbounded persisted file.
                if len(data) > HISTORY_MAX_TURNS * 2:
                    data = data[-HISTORY_MAX_TURNS * 2 :]
                return data
        except Exception:
            pass
    return []


def save_history(history: list[dict]) -> None:
    turns = [m for m in history if m.get("role") in ("user", "assistant")]
    HISTORY_FILE.write_text(json.dumps(turns, indent=2))


def trim_history(history: list[dict]) -> list[dict]:
    sys_msgs = [m for m in history if m["role"] == "system"]
    turns = [m for m in history if m["role"] in ("user", "assistant")]
    if len(turns) > HISTORY_MAX_TURNS * 2:
        turns = turns[-HISTORY_MAX_TURNS * 2 :]
    return sys_msgs + turns


# ─────────────────────────────── models ───────────────────────────────


@dataclass
class Models:
    vad: VADIterator
    stt: SttEngine
    tts: TtsEngine
    turn: TurnDetector
    apm: AudioProcessingModule | None
    client: httpx.Client
    llm: LlmStreamer


def load_models() -> Models:
    # The silence timeout is a flat tax on every turn: we wait this long after
    # you stop making noise before doing anything. With no semantic detector it
    # has to be generous (500 ms) or natural mid-sentence pauses get treated as
    # end-of-turn — and it still cuts people off, because silence duration says
    # nothing about whether a sentence is finished.
    #
    # With a semantic detector in the loop, that job moves to the model, so the
    # timeout only has to be long enough to catch a breath. 200 ms costs ~19 ms
    # of inference and saves ~300 ms per turn.
    turn_choice = os.environ.get("TURN_DETECTOR", "off").strip().lower()
    semantic_turn = turn_choice not in ("off", "none", "0")
    default_silence = "200" if semantic_turn else "500"
    min_silence_ms = int(os.environ.get("VAD_MIN_SILENCE_MS", default_silence))

    print(f"[1/5] Silero VAD (min_silence {min_silence_ms} ms)...", flush=True)
    vad = VADIterator(
        load_silero_vad(),
        threshold=float(os.environ.get("VAD_THRESHOLD", "0.6")),
        sampling_rate=SR,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=100,
    )

    # Print before building, not after: loading a model can take tens of
    # seconds (or download gigabytes on first run) and silence reads as a hang.
    # Print the engine's own name after building, never a hardcoded guess at
    # the default — that drifts the moment a default changes and silently
    # misreports which model is actually running.
    print(f"[2/5] STT: {os.environ.get('STT_ENGINE', '(default)')} loading...", flush=True)
    stt = build_stt()
    print(f"      -> {stt.name}", flush=True)

    print(f"[3/5] TTS: {os.environ.get('TTS_ENGINE', '(default)')} loading...", flush=True)
    tts = build_tts()
    print(f"      -> {tts.name} @ {tts.sample_rate} Hz", flush=True)

    print(f"[4/5] turn detector: {os.environ.get('TURN_DETECTOR', 'off')}", flush=True)
    turn = build_turn_detector()

    apm = None
    if not HALF_DUPLEX_ONLY:
        print(f"[5/5] WebRTC AEC (stream delay {STREAM_DELAY_MS} ms)...", flush=True)
        # AEC + HPF only. Reasoning:
        # - AEC removes the agent's voice from the mic.
        # - NS, when on, was damaging real user speech (Moonshine garbles its
        #   output) for marginal residual reduction. The energy gate below
        #   handles residual better.
        # - AGC ducks user voice during loud TTS, killing barge-in. Stays off.
        # - HPF removes low-freq rumble, helps STT, no downside.
        apm = AudioProcessingModule(
            echo_cancellation=True,
            noise_suppression=False,
            high_pass_filter=True,
            auto_gain_control=False,
        )
        apm.set_stream_delay_ms(STREAM_DELAY_MS)
    else:
        print("[4/4] AEC disabled (HALF_DUPLEX=1)", flush=True)

    client = httpx.Client(timeout=httpx.Timeout(120.0))
    return Models(
        vad=vad, stt=stt, tts=tts, turn=turn, apm=apm, client=client,
        llm=LlmStreamer(),
    )


def warmup(models: Models) -> None:
    print("warming up...", flush=True)
    t0 = time.perf_counter()

    models.stt.warmup()

    try:
        if LLM_BACKEND == "venice":
            # Opens the TLS connection and pays the first-request tax now
            # rather than inside the user's first turn.
            models.client.post(
                f"{VENICE_BASE_URL}/chat/completions",
                json={
                    "model": VENICE_LLM_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "max_completion_tokens": 1,
                    "venice_parameters": {"include_venice_system_prompt": False},
                },
                headers={"Authorization": f"Bearer {_venice_key()}"},
                timeout=120.0,
            )
        else:
            models.client.post(
                OLLAMA_URL,
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "think": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": {"num_predict": 1},
                },
                timeout=120.0,
            )
    except Exception as e:
        print(f"  (LLM warmup: {e})")

    models.tts.warmup()

    print(f"  warmed in {(time.perf_counter()-t0)*1000:.0f}ms", flush=True)


# ─────────────────────────────── playback ─────────────────────────────


# How many sentences may sit synthesized-and-waiting ahead of the speaker.
# This is the whole point of the split thread — but it must be bounded, or a
# long reply synthesizes in full before the first word is spoken and a barge-in
# throws all of it away. Two is enough to cover any sentence boundary.
TTS_MAX_AHEAD = int(os.environ.get("TTS_MAX_AHEAD", "2"))


class TTSPlayer:
    """Synthesizes on the caller's thread, plays on a thread of its own.

    Why the split. `stream.write()` blocks for the real duration of the audio.
    While synthesis and playback shared a thread, sentence two could not start
    synthesizing until sentence one had finished *playing*, so every sentence
    boundary cost a full synthesis — 293 ms median (`issues/0002`). With
    playback on its own thread the caller runs ahead, and sentence two's audio
    is ready before sentence one stops.

    The playback thread deliberately owns no MLX state. `learning.md`
    (2026-07-30) records that thread-local Metal state torn down on thread exit
    kills the interpreter, so synthesis stays on the persistent turn worker and
    this thread only feeds the APM reverse stream and writes int16. It is
    long-lived for the same reason and never exits.

    Why 10 ms frames at 16 kHz: the APM requires its reverse stream to be
    exactly that, and AEC works best when the reference is bit-identical to
    what the speaker emits. Kokoro's 24 kHz output is resampled once, on the
    synthesis side, so the playback thread stays as close to a pure device
    writer as possible and its timestamps mean what they say.
    """

    def __init__(self, tts: TtsEngine, apm: AudioProcessingModule | None):
        self.tts = tts
        self.apm = apm
        self.in_sr = tts.sample_rate  # 24000 for Kokoro and the mlx-audio models
        self.out_sr = SR  # 16000
        self.stream = sd.OutputStream(
            samplerate=self.out_sr, channels=1, dtype="int16", blocksize=APM_FRAME
        )
        self.stream.start()

        # True only while frames are actually reaching the speaker. The barge-in
        # gate keys off this rather than "a reply is in progress": with
        # synthesis running ahead, those two are no longer the same instant, and
        # arming the echo gate before any sound exists would suppress a real
        # interruption during the silent pre-audio phase.
        self.audible = False

        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        # Bounds how far synthesis may run ahead of the speaker.
        self._slots = threading.Semaphore(TTS_MAX_AHEAD)
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        # Exponential moving average of what we are sending to the speaker.
        # The barge-in gate uses this to scale itself with echo, so it must be
        # cheap and updated on every frame we actually write.
        self._out_rms = 0.0

        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()

    def output_rms(self) -> float:
        """Recent RMS of audio sent to the speaker; 0.0 when silent."""
        return self._out_rms

    def begin_reply(self) -> None:
        """Re-arm for a new reply.

        Cancellation is per *reply*, not per sentence. The previous code cleared
        the cancel flag at the top of every sentence, which meant a barge-in
        landing between two sentences was forgotten by the next one.
        """
        with self._lock:
            self._cancel.clear()

    def speak_sentence(self, sentence: str) -> dict:
        """Synthesize one sentence and hand it to the speaker. Does not block on playback.

        Returns a dict that is still being written to: `t_first_frame` and
        `audio_s` are filled in by the playback thread. Read them after
        `wait_idle()`, not before.

        `synth_ms` covers only the generator, never the device write — that
        distinction is what separates "how fast can Kokoro make audio" from
        "how long is the audio", and it is the measurement `issues/0002` turns
        on.
        """
        self._slots.acquire()

        m = {
            "text": sentence,
            "synth_first_ms": None,   # text in -> first audio chunk out
            "synth_ms": 0.0,          # generator time only, no playback
            "audio_s": 0.0,           # written by the playback thread
            "t_first_frame": None,    # written by the playback thread
            "t_last_frame": None,     # written by the playback thread
            "complete": False,        # written by the playback thread
        }
        with self._lock:
            self._pending += 1
            self._idle.clear()

        t_start = time.perf_counter()
        try:
            if self._cancel.is_set():
                return m
            residual = np.zeros(0, dtype=np.float32)
            # Stepped by hand rather than with `for`, so the clock covers only
            # the generator and not the queueing around it.
            stream_iter = iter(self.tts.stream(sentence))
            while True:
                t_gen = time.perf_counter()
                try:
                    arr = next(stream_iter)
                except StopIteration:
                    m["synth_ms"] += (time.perf_counter() - t_gen) * 1000.0
                    break
                m["synth_ms"] += (time.perf_counter() - t_gen) * 1000.0
                if m["synth_first_ms"] is None:
                    m["synth_first_ms"] = (time.perf_counter() - t_start) * 1000.0
                if self._cancel.is_set():
                    return m
                if self.in_sr != self.out_sr:
                    arr = linear_resample(arr, self.in_sr, self.out_sr).astype(np.float32)
                buf = np.concatenate([residual, arr])
                n_full = len(buf) // APM_FRAME
                if n_full:
                    self._q.put(("frames", m, f32_to_i16(buf[: n_full * APM_FRAME])))
                residual = buf[n_full * APM_FRAME :]

            if len(residual) > 0 and not self._cancel.is_set():
                pad = np.zeros(APM_FRAME - len(residual), dtype=np.float32)
                self._q.put(("frames", m, f32_to_i16(np.concatenate([residual, pad]))))
        finally:
            # Always posted, including on cancel or synthesis error, so the
            # playback thread has exactly one place to settle the bookkeeping
            # and release the slot. Two release points is how this leaks.
            self._q.put(("end", m))
        return m

    def _play_loop(self) -> None:
        while True:  # never returns — see the class docstring
            kind, m, *rest = self._q.get()
            if kind == "end":
                # Settled here rather than on the synthesis side, because only
                # this thread knows whether the frames actually got written.
                m["complete"] = not self._cancel.is_set()
                with self._lock:
                    self._pending -= 1
                    if self._pending == 0:
                        self.audible = False
                        self._out_rms = 0.0
                        self._idle.set()
                self._slots.release()
                continue
            if self._cancel.is_set():
                continue
            block = rest[0]
            for i in range(0, len(block), APM_FRAME):
                if self._cancel.is_set():
                    break
                frame_i16 = block[i : i + APM_FRAME]
                if self.apm is not None:
                    af = AudioFrame(
                        data=frame_i16.tobytes(),
                        sample_rate=self.out_sr,
                        num_channels=1,
                        samples_per_channel=APM_FRAME,
                    )
                    self.apm.process_reverse_stream(af)
                self._note_output(frame_i16)
                # Stamped before the write, not after: `write` blocks until the
                # device has room, so stamping after would fold one frame of
                # playback into what is meant to be the moment sound started.
                if m["t_first_frame"] is None:
                    m["t_first_frame"] = time.perf_counter()
                self.audible = True
                self.stream.write(frame_i16)
                # Stamped after the write, unlike t_first_frame: this one marks
                # when the device accepted the last frame, which is what the
                # next sentence's start time has to be compared against to see
                # whether the speaker ever ran dry.
                m["t_last_frame"] = time.perf_counter()
                m["audio_s"] += APM_FRAME / self.out_sr

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until everything queued has played (or been cancelled)."""
        return self._idle.wait(timeout)

    def _note_output(self, frame_i16: np.ndarray) -> None:
        """Fold one outgoing frame into the output-level estimate.

        Asymmetric smoothing: rise fast so the gate is already high when the
        agent starts talking, decay slower so it stays high through the brief
        dips between words rather than dropping and admitting an echo spike.
        """
        r = float(np.sqrt(np.mean((frame_i16.astype(np.float32) / 32768.0) ** 2)))
        alpha = 0.5 if r > self._out_rms else 0.05
        self._out_rms = (1 - alpha) * self._out_rms + alpha * r

    def stop(self) -> None:
        """Cut the current reply. Queued frames are dropped, markers still settle."""
        self._cancel.set()

    def close(self) -> None:
        self._cancel.set()
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


# ─────────────────────────────── main loop ────────────────────────────


def main() -> int:
    models = load_models()
    warmup(models)

    persisted = load_history()
    history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}] + persisted
    if persisted:
        print(f"[memory] loaded {len(persisted)//2} prior turns")

    player = TTSPlayer(models.tts, models.apm)

    # mic queue holds 10 ms (APM_FRAME) chunks of float32 mono
    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def mic_cb(indata, frames, t, status):  # noqa: ANN001
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        audio_q.put(indata[:, 0].copy().astype(np.float32))

    in_stream = sd.InputStream(
        samplerate=SR,
        channels=1,
        dtype="float32",
        blocksize=APM_FRAME,  # 10 ms — matches APM
        callback=mic_cb,
    )
    in_stream.start()

    mode = "FULL-DUPLEX + AEC + barge-in" if models.apm is not None else "HALF-DUPLEX"
    print(f"\n=== ready ({mode}) — Ctrl+C to quit ===\n", flush=True)

    vad_buf = np.zeros(0, dtype=np.float32)  # accumulates AEC'd audio for VAD
    speech_buf: list[np.ndarray] = []
    active = False
    continuations = 0  # times the turn detector deferred the current utterance
    state = {
        "speaking": False,
        "resume_at": 0.0,
        "user_just_ended": 0.0,
        "loud_streak": 0,         # consecutive AEC'd frames above the gate
        "barge_fired": False,     # disarms further barge-ins until next reply
    }
    llm_cancel = threading.Event()

    # cooldown after user finishes speaking, before barge-in can fire on the
    # agent's pre-audio phase (LLM still generating, no audio out yet). Without
    # this, the user's natural breathing or trailing breath after end-of-utterance
    # trips VAD and instantly cancels the agent's reply before it can speak.
    POST_USER_BARGE_LOCKOUT_S = 0.6

    # One long-lived worker handles every turn, rather than a thread per turn.
    # MLX keeps thread-local Metal state, and tearing that down when a thread
    # exits crashes the interpreter outright:
    #   Fatal Python error: PyThreadState_Get: ... the GIL is released
    # A thread per turn therefore died on the second turn once the STT slot
    # held an MLX model (Kokoro alone had tolerated it). Reproduced both ways:
    # per-turn threads crash at turn 1->2, a persistent worker ran 8/8 clean.
    turn_q: queue.Queue = queue.Queue()

    def turn_worker():
        while True:  # never returns — exiting is precisely what crashes
            audio_buf, cancel, t_end = turn_q.get()
            try:
                respond(audio_buf, models, history, player, cancel, t_end)
            except Exception as e:  # one bad turn must not kill the worker
                print(f"\n[turn failed] {type(e).__name__}: {e}", flush=True)
            finally:
                state["resume_at"] = time.monotonic() + TTS_TAIL_GRACE_MS / 1000.0
                state["speaking"] = False
                models.vad.reset_states()

    threading.Thread(target=turn_worker, daemon=True).start()

    try:
        while True:
            mic_chunk_f32 = audio_q.get()  # 10 ms float32

            # In half-duplex fallback, drop mic frames while speaking.
            if models.apm is None and (
                state["speaking"] or time.monotonic() < state["resume_at"]
            ):
                continue

            # AEC: process mic through APM (subtracts speaker echo using reverse stream).
            if models.apm is not None:
                mic_i16 = f32_to_i16(mic_chunk_f32)
                af = AudioFrame(
                    data=mic_i16.tobytes(),
                    sample_rate=SR,
                    num_channels=1,
                    samples_per_channel=APM_FRAME,
                )
                models.apm.process_stream(af)
                cleaned_i16 = np.frombuffer(af.data, dtype=np.int16)
                cleaned = i16_to_f32(cleaned_i16)
            else:
                cleaned = mic_chunk_f32

            # Accumulate cleaned audio into 512-sample VAD frames.
            vad_buf = np.concatenate([vad_buf, cleaned])
            while len(vad_buf) >= VAD_FRAME:
                v_chunk = vad_buf[:VAD_FRAME]
                vad_buf = vad_buf[VAD_FRAME:]

                rms = float(np.sqrt(np.mean(v_chunk**2)))

                # VAD runs before the barge-in check, not after, because the
                # barge-in decision needs to know whether this frame is speech
                # at all. Previously it did not: the condition was pure energy
                # while its comment claimed VAD agreement, and the agent's own
                # voice leaking past AEC cut 7 of 13 replies in testing.
                event = models.vad(torch.from_numpy(v_chunk), return_seconds=False)
                is_speech = bool(getattr(models.vad, "triggered", False))

                # Adaptive gate. Residual echo scales with what the speaker is
                # actually emitting, so a fixed floor cannot separate "user
                # talking" from "our own output leaking" — measured false
                # triggers ranged 0.058-0.152, straddling any single threshold.
                # Requiring the mic to exceed a multiple of concurrent TTS
                # output makes the bar rise exactly when leakage does.
                # `player.audible` rather than "a reply is in flight": synthesis
                # now runs ahead of the speaker, so those are different
                # instants, and raising the echo gate before any sound exists
                # would suppress a real interruption during the silent
                # pre-audio phase.
                gate = BARGE_IN_RMS_GATE
                if player.audible:
                    gate = max(gate, BARGE_IN_ECHO_FACTOR * player.output_rms())

                if rms >= gate and is_speech:
                    state["loud_streak"] += 1
                else:
                    state["loud_streak"] = 0

                # Mid-TTS barge-in. `barge_fired` disarms repeats; we re-arm
                # when the next reply starts.
                if (
                    state["speaking"]
                    and player.audible
                    and not state["barge_fired"]
                    and state["loud_streak"] >= BARGE_IN_SUSTAIN_FRAMES
                    and time.monotonic() - state["user_just_ended"] >= POST_USER_BARGE_LOCKOUT_S
                ):
                    print(
                        f"[barge-in] cutting reply (rms={rms:.3f} gate={gate:.3f} "
                        f"streak={state['loud_streak']})",
                        flush=True,
                    )
                    llm_cancel.set()
                    player.stop()
                    state["loud_streak"] = 0
                    state["barge_fired"] = True

                if event:
                    if "start" in event:
                        if not state["speaking"]:
                            if active:
                                # Already mid-utterance: the turn detector told
                                # us the thought wasn't finished, so this is the
                                # user resuming after a pause. Keep the buffer —
                                # resetting it here drops everything said before
                                # the pause and leaves STT decoding a fragment
                                # too short to get right.
                                speech_buf.append(v_chunk)
                            else:
                                speech_buf = [v_chunk]
                                continuations = 0  # fresh utterance
                                print("🎙  listening...", flush=True)
                            active = True
                    elif "end" in event and active:
                        speech_buf.append(v_chunk)
                        audio = np.concatenate(speech_buf)

                        # Sub-word fragments make small STT models hallucinate
                        # fluent nonsense rather than return nothing — "No.",
                        # "Thank you." and similar stock phrases. Cheaper to
                        # refuse to transcribe them than to filter them after.
                        too_short = len(audio) < MIN_UTTERANCE_SAMPLES

                        # Stop waiting once the utterance hits the ceiling, even
                        # if the turn detector still thinks there is more coming.
                        # STT cost scales with buffer length, so an uncapped
                        # buffer turns into runaway latency.
                        forced = False
                        if len(audio) >= MAX_UTTERANCE_SAMPLES:
                            print(
                                f"[utterance cap] {len(audio)/SR:.1f}s reached — "
                                "transcribing now",
                                flush=True,
                            )
                            too_short = False
                            forced = True
                        elif continuations >= MAX_TURN_CONTINUATIONS:
                            print(
                                f"[turn cap] {continuations} continuations — "
                                "transcribing now",
                                flush=True,
                            )
                            forced = True

                        # Silero says the sound stopped; the turn detector says
                        # whether the *thought* finished. If not, stay active so
                        # the continuation joins this same utterance instead of
                        # becoming a second, truncated one.
                        if not forced and (
                            too_short or not models.turn.is_complete(audio)
                        ):
                            continuations += 1
                            models.vad.reset_states()
                            continue

                        active = False
                        continuations = 0
                        state["speaking"] = True
                        state["user_just_ended"] = time.monotonic()
                        state["loud_streak"] = 0
                        state["barge_fired"] = False  # re-arm for the new reply
                        llm_cancel = threading.Event()
                        # Stamp end-of-speech here, not in the worker: the queue
                        # hop and any backlog are latency the user experiences,
                        # and starting the clock inside respond() would hide it.
                        turn_q.put((audio, llm_cancel, time.perf_counter()))
                        models.vad.reset_states()
                elif active:
                    speech_buf.append(v_chunk)

    except KeyboardInterrupt:
        print("\nbye 👋")
    finally:
        save_history(history)
        in_stream.stop()
        in_stream.close()
        player.close()
        models.stt.close()
        models.tts.close()
        models.turn.close()
    return 0


def respond(
    audio: np.ndarray,
    models: Models,
    history: list[dict],
    player: TTSPlayer,
    cancel: threading.Event,
    speech_end: float | None = None,
) -> None:
    # Every stage time is measured from end-of-speech, because that is when the
    # human starts waiting. `speech_end` is stamped in the mic loop; falling
    # back to now() only loses the queue hop when a caller omits it.
    t_end = speech_end if speech_end is not None else time.perf_counter()

    m = TurnMetrics(
        llm_backend=LLM_BACKEND,
        llm_model=VENICE_LLM_MODEL if LLM_BACKEND == "venice" else LLM_MODEL,
        stt_engine=models.stt.name,
        tts_engine=models.tts.name,
        audio_in_s=len(audio) / SR,
        dispatch_ms=(time.perf_counter() - t_end) * 1000.0,
    )

    t0 = time.perf_counter()
    user_text = transcribe_utterance(models.stt, audio)
    t_stt = time.perf_counter() - t0
    m.stt_ms = t_stt * 1000.0
    if not user_text:
        print("(no speech detected)", flush=True)
        # Still logged: a turn that cost STT time and produced nothing is a
        # real cost the pipeline paid, and dropping those rows would quietly
        # improve every average.
        m.ok = False
        m.error = "no_speech"
        m.turn_total_ms = (time.perf_counter() - t_end) * 1000.0
        log_turn(m)
        return
    m.user_text = user_text
    # Log the audio length alongside the time. Without it, a slow STT reading is
    # ambiguous between "the model is slow" and "we handed it far too much
    # audio", and those have opposite fixes.
    print(
        f"You: {user_text}   "
        f"[stt {t_stt*1000:.0f}ms on {len(audio)/SR:.1f}s]",
        flush=True,
    )

    history.append({"role": "user", "content": user_text})
    history[:] = trim_history(history)

    print("AI: ", end="", flush=True)
    t1 = time.perf_counter()
    llm_stats = {"ttft": None, "chunks": 0, "last": None}

    interrupted = False
    metas: list[dict] = []
    player.begin_reply()
    try:
        # `llm_stats` counts stream *deltas*, not tokenizer tokens — Ollama and
        # Venice both emit one delta per token in practice, but nothing
        # guarantees it, so the field is `chunks` and not `tokens`.
        tokens = models.llm.stream(history, models.client, cancel, llm_stats, t1)
        for sent in sentence_stream(tokens):
            if cancel.is_set():
                interrupted = True
                break
            print(sent + " ", end="", flush=True)
            if m.llm_first_sentence_ms is None:
                m.llm_first_sentence_ms = (time.perf_counter() - t1) * 1000.0
            metas.append(player.speak_sentence(sent))
            if cancel.is_set():
                interrupted = True
                break
    except Exception as e:
        print(f"\n[error] {e}", flush=True)
        m.ok = False
        m.error = f"{type(e).__name__}: {e}"

    # Synthesis now runs ahead of the speaker, so the turn is not over when the
    # loop above ends — it is over when the last frame has been written. Every
    # playback-side field below is only valid after this returns.
    player.wait_idle()
    print()

    for i, sm in enumerate(metas):
        # `gap_ms` is the real `issues/0002` measurement: silence the listener
        # actually heard between one sentence and the next. It replaces the old
        # proxy — the synthesis time of later sentences — which was only ever
        # equal to the gap because synthesis happened *after* playback. Now that
        # the two overlap, that proxy measures work done underneath the audio
        # and would report a gap where there is none.
        gap = None
        if i > 0 and sm["t_first_frame"] is not None:
            prev_end = metas[i - 1]["t_last_frame"]
            if prev_end is not None:
                gap = round((sm["t_first_frame"] - prev_end) * 1000.0, 2)
        m.sentences.append(
            {
                "text": sm["text"],
                "synth_first_ms": round(sm["synth_first_ms"], 2)
                if sm["synth_first_ms"] is not None
                else None,
                "synth_ms": round(sm["synth_ms"], 2),
                "audio_s": round(sm["audio_s"], 3),
                "gap_ms": gap,
            }
        )
        m.tts_synth_ms += sm["synth_ms"]
        m.tts_audio_s += sm["audio_s"]
    if metas:
        m.tts_first_ms = metas[0]["synth_first_ms"]
        if metas[0]["t_first_frame"] is not None:
            m.first_audio_ms = (metas[0]["t_first_frame"] - t_end) * 1000.0

    # A sentence counts as spoken only if it played through. Recording one that
    # was synthesized but cut — or never reached the speaker at all — would put
    # words in the agent's mouth that were never heard, and the model would then
    # reason from a reply the user never got. With synthesis running ahead of
    # playback this is no longer the same thing as "we finished the loop".
    full_reply = [sm["text"] for sm in metas if sm["complete"] and sm["audio_s"] > 0]
    if len(full_reply) < len(metas):
        interrupted = True
    m.llm_ttft_ms = llm_stats["ttft"]
    m.llm_total_ms = llm_stats["last"]
    m.llm_chunks = llm_stats["chunks"]
    if llm_stats["last"] and llm_stats["ttft"] is not None and llm_stats["chunks"] > 1:
        # Decode rate excludes time-to-first-token: TTFT is queueing and prefill,
        # and folding it in makes a fast decoder on a long prompt look slow.
        decode_ms = llm_stats["last"] - llm_stats["ttft"]
        if decode_ms > 0:
            m.llm_chunk_s = (llm_stats["chunks"] - 1) / (decode_ms / 1000.0)
    if m.tts_synth_ms > 0:
        m.tts_rtf = m.tts_audio_s / (m.tts_synth_ms / 1000.0)
    m.interrupted = interrupted
    m.reply_text = " ".join(full_reply)
    m.turn_total_ms = (time.perf_counter() - t_end) * 1000.0
    print(summary_line(m), flush=True)
    log_turn(m)

    if full_reply:
        text = " ".join(full_reply)
        if interrupted:
            # Mark it so the model knows it was cut off rather than believing it
            # delivered a complete thought — otherwise the next turn continues
            # from a reply that only half happened, which reads as non-sequitur.
            text += " …(interrupted)"
        history.append({"role": "assistant", "content": text})
        save_history(history)


if __name__ == "__main__":
    sys.exit(main())
