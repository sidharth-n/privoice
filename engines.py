"""Swappable engine slots for the voice pipeline.

Why this exists: every component in the pipeline (STT, TTS, end-of-turn) is a
model choice we want to A/B on real hardware, not a permanent decision. Each
slot is a small protocol plus one adapter per backend, selected by an env var.

The defaults are the known-good shipped config. STT moved to Parakeet on
measurement (see build_stt); Moonshine, the original pick, stays one env var
away. Turn detection defaults off deliberately — see issues/0005.

    STT_ENGINE=parakeet    (default; also venice, moonshine, nemotron,
                            nemotron-8bit, whisper, or any mlx-audio repo id)
    TTS_ENGINE=kokoro      (default; also kokoro-legacy, venice)
    TURN_DETECTOR=off      (default; also smartturn)

`venice` runs the same checkpoints through Venice's hosted API instead of on
this machine, which is what makes the local-vs-hosted benchmark a controlled
comparison rather than two unrelated stacks. Needs VENICE_API_KEY.
"""

from __future__ import annotations

import os
from typing import Iterator, Protocol, runtime_checkable

import numpy as np

SR = 16000  # pipeline-wide sample rate; TTS adapters resample to this upstream


# ─────────────────────────────── protocols ────────────────────────────


@runtime_checkable
class SttEngine(Protocol):
    """Utterance-at-a-time speech to text.

    The pipeline buffers a whole utterance behind VAD before transcribing, so
    engines only need a batch call — not incremental partial hypotheses.
    """

    name: str

    def transcribe(self, audio_f32: np.ndarray) -> str: ...
    def warmup(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class TtsEngine(Protocol):
    """Sentence-at-a-time speech synthesis.

    `stream` yields float32 mono chunks at `sample_rate`. The player resamples
    to 16 kHz for both the speaker and the AEC reverse stream, so adapters
    report their native rate rather than resampling themselves.
    """

    name: str
    sample_rate: int

    def stream(self, sentence: str) -> Iterator[np.ndarray]: ...
    def warmup(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class TurnDetector(Protocol):
    """Decides whether a VAD-delimited utterance is actually finished.

    Silero tells us the user stopped making sound; it cannot tell us whether
    they finished their thought. A semantic detector answers the second
    question, which is what stops the agent cutting people off mid-sentence.
    """

    name: str

    def is_complete(self, audio_f32: np.ndarray) -> bool: ...
    def close(self) -> None: ...


# ─────────────────────────────── STT adapters ─────────────────────────


class MoonshineStt:
    """Moonshine via the `moonshine_voice` package. English only.

    MEDIUM_STREAMING is more accurate than BASE and STT sits well inside our
    latency budget, so we spend the extra compute on quality. The _STREAMING
    variant works fine for one-shot `transcribe_without_streaming` calls.
    """

    name = "moonshine"

    def __init__(self, language: str = "en", arch: str | None = None) -> None:
        from moonshine_voice import get_model_for_language
        from moonshine_voice.moonshine_api import ModelArch
        from moonshine_voice.transcriber import Transcriber

        resolved = getattr(ModelArch, arch) if arch else ModelArch.MEDIUM_STREAMING
        path, model_arch = get_model_for_language(language, resolved)
        self._t = Transcriber(model_path=str(path), model_arch=model_arch)
        self._t.start()

    def transcribe(self, audio_f32: np.ndarray) -> str:
        transcript = self._t.transcribe_without_streaming(
            audio_f32.tolist(), sample_rate=SR
        )
        parts = []
        for line in getattr(transcript, "lines", []) or []:
            text = getattr(line, "text", "") or ""
            if text.strip():
                parts.append(text.strip())
        return " ".join(parts).strip()

    def warmup(self) -> None:
        self.transcribe(np.zeros(SR, dtype=np.float32))

    def close(self) -> None:
        try:
            self._t.stop()
            self._t.close()
        except Exception:
            pass


class MlxAudioStt:
    """Any STT model in mlx-audio's registry, MLX-native on Apple Silicon.

    Gives us Parakeet TDT v3 (25 European languages), Nemotron 3.5 ASR
    (40 locales, language-ID prompting), Whisper, Canary and MMS behind one
    adapter — all substantially larger and better-trained than Moonshine,
    which is English-only and small enough to hallucinate on short clips.

    mlx-audio's generate() takes a file path, so we spill the utterance to a
    temp wav. At utterance rate that write is microseconds against hundreds of
    milliseconds of inference, and it keeps us on the library's supported path
    instead of reaching into its internals.
    """

    name = "mlxaudio"

    def __init__(self, model_id: str, language: str | None = None) -> None:
        from mlx_audio.stt import load

        self.model_id = model_id
        self.language = language
        self.name = f"mlxaudio:{model_id.split('/')[-1]}"
        self._model = load(model_id)

    def transcribe(self, audio_f32: np.ndarray) -> str:
        import tempfile
        import wave

        audio = np.asarray(audio_f32, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:  # keep within [-1, 1] before int16 conversion clips it
            audio = audio / peak
        pcm = (audio * 32767.0).astype(np.int16)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(pcm.tobytes())
            kwargs = {"language": self.language} if self.language else {}
            try:
                result = self._model.generate(tmp.name, **kwargs)
            except TypeError:
                # not every model in the registry accepts a language kwarg
                result = self._model.generate(tmp.name)

        text = getattr(result, "text", None)
        if text is None:  # some models yield segments instead of a flat string
            segs = getattr(result, "segments", None) or []
            text = " ".join(getattr(s, "text", "") or "" for s in segs)
        return (text or "").strip()

    def warmup(self) -> None:
        self.transcribe(np.zeros(SR, dtype=np.float32))

    def close(self) -> None:
        self._model = None


# ─────────────────────────── Venice (hosted) ──────────────────────────
#
# Venice serves the *same* Parakeet and Kokoro checkpoints this project runs
# locally, which is the whole reason a hosted/local A/B is worth running: the
# model is held constant and only the substrate changes.


def _venice_key() -> str:
    key = os.environ.get("VENICE_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "VENICE_API_KEY is not set. Create a key at "
            "https://venice.ai/settings/api and put it in .env"
        )
    return key


def _venice_base_url() -> str:
    return os.environ.get("VENICE_BASE_URL", "https://api.venice.ai/api/v1").rstrip("/")


class VeniceStt:
    """Hosted ASR via Venice's `/audio/transcriptions`.

    Defaults to `nvidia/parakeet-tdt-0.6b-v3` — the same checkpoint MlxAudioStt
    loads locally — so a difference in the measured number is a difference in
    substrate, not in model.

    The endpoint is multipart and wants a real file, so we spill the utterance
    to a temp wav exactly as the local mlx-audio path does. That write is
    microseconds against a network round trip and keeps both adapters honest
    about what they are timing.
    """

    name = "venice-stt"

    def __init__(self, model: str | None = None, language: str | None = None) -> None:
        import httpx

        self.model = model or os.environ.get(
            "VENICE_STT_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
        )
        self.language = language
        self.name = f"venice:{self.model.split('/')[-1]}"
        self._client = httpx.Client(
            base_url=_venice_base_url(),
            headers={"Authorization": f"Bearer {_venice_key()}"},
            timeout=httpx.Timeout(120.0),
        )

    def transcribe(self, audio_f32: np.ndarray) -> str:
        import io
        import wave

        audio = np.asarray(audio_f32, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / peak
        pcm = (audio * 32767.0).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        buf.seek(0)

        data = {"model": self.model}
        if self.language:
            data["language"] = self.language
        r = self._client.post(
            "/audio/transcriptions",
            files={"file": ("utterance.wav", buf, "audio/wav")},
            data=data,
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    def warmup(self) -> None:
        # A full second of digital silence still costs a round trip, which is
        # exactly the thing we want warmed before the timed samples start.
        self.transcribe(np.zeros(SR, dtype=np.float32))

    def close(self) -> None:
        self._client.close()


# ─────────────────────────────── TTS adapters ─────────────────────────


class KokoroTts:
    """Kokoro 82M via mlx-audio's `KokoroPipeline`. English-centric, 24 kHz out.

    Drives mlx_audio rather than `kokoro_mlx` because of a silent, product-level
    defect in the latter. kokoro-mlx 0.1.1 builds misaki's G2P with **no espeak
    fallback**, so any word outside Kokoro's lexicon phonemizes to the empty
    string and is synthesized as *silence*. Not mispronounced — deleted, with no
    warning. That removed "Privoice" and the user's own name from every reply.

    The tell was duration, not sound: "Say Privoice now." and "Say Sid now."
    both produced exactly 33,600 samples, which no pronunciation difference
    could explain, while "John" (in the lexicon) produced 39,000. Verified
    directly against the G2P — `misaki.en.G2P(fallback=None)("Privoice")`
    returns `''`, and with the fallback returns `pɹˈɪvYs`.

    `KokoroPipeline` constructs `misaki.espeak.EspeakFallback` itself. Measured
    time-to-first-audio is unchanged by the swap (170/264/446 ms at 13/34/72
    characters, against 190/217/442 ms for kokoro_mlx — inside run-to-run noise).

    The old engine stays reachable as `TTS_ENGINE=kokoro-legacy` so the
    previously shipping path is still one env var away.
    """

    name = "kokoro"
    REPO = "prince-canuma/Kokoro-82M"

    def __init__(self, voice: str | None = None, repo: str | None = None) -> None:
        from mlx_audio.tts.models.kokoro import KokoroPipeline
        from mlx_audio.tts.utils import load_model

        self._repo = repo or os.environ.get("KOKORO_REPO", self.REPO)
        self._voice = voice or "af_heart"
        self._speed = float(os.environ.get("TTS_SPEED", "1.0"))
        self.sample_rate = 24000
        self._pipe = KokoroPipeline(
            lang_code="a", model=load_model(self._repo), repo_id=self._repo
        )
        _check_g2p_fallback(self._pipe)

    def stream(self, sentence: str) -> Iterator[np.ndarray]:
        for result in self._pipe(sentence, voice=self._voice, speed=self._speed):
            audio = getattr(result, "audio", None)
            if audio is None:
                continue
            yield np.asarray(audio, dtype=np.float32).flatten()

    def warmup(self) -> None:
        for _ in self.stream("Hi."):
            pass

    def close(self) -> None:
        pass


def _check_g2p_fallback(pipeline) -> None:  # noqa: ANN001
    """Refuse to run with a G2P that will silently delete words.

    `KokoroPipeline` logs "EspeakFallback not Enabled: OOD words will be
    skipped" and carries on when espeak cannot be loaded. Nobody sees that —
    it fires at construction, into the logging module, behind model-download
    progress bars — and the symptom arrives hours later as proper nouns
    missing from speech. Converting it into a startup error is the entire
    lesson of that bug: a failure this quiet has to be made loud at the only
    moment it is cheap to fix.

    `espeakng-loader` ships the library as a wheel, so the usual cause is a
    missing dependency rather than a missing system package.
    """
    if getattr(getattr(pipeline, "g2p", None), "fallback", None) is not None:
        return
    if os.environ.get("KOKORO_ALLOW_NO_ESPEAK", "0") == "1":
        print(
            "[tts] WARNING: no espeak fallback — words outside Kokoro's lexicon "
            "(names, 'Privoice') will be SILENTLY DROPPED from speech.",
            flush=True,
        )
        return
    raise SystemExit(
        "Kokoro's espeak fallback failed to load. Without it any word outside\n"
        "Kokoro's lexicon — including names and 'Privoice' — is not\n"
        "mispronounced but silently deleted from the audio.\n\n"
        "Fix:  uv sync        (installs espeakng-loader + phonemizer-fork)\n"
        "Override, accepting dropped words:  KOKORO_ALLOW_NO_ESPEAK=1"
    )


class KokoroLegacyTts:
    """The previously shipping Kokoro path, via `kokoro_mlx`.

    Kept so the old behaviour stays reproducible and the A/B is still runnable,
    but it is not the default: it drops out-of-lexicon words (see `KokoroTts`).
    Pinned to kokoro-mlx 0.1.1 by the interpreter — 0.1.2 requires Python <3.13.
    """

    name = "kokoro-legacy"

    def __init__(self, voice: str | None = None) -> None:
        from kokoro_mlx import DEFAULT_VOICE, KokoroTTS

        self._tts = KokoroTTS.from_pretrained()
        self._voice = voice or DEFAULT_VOICE
        self.sample_rate = int(self._tts.SAMPLE_RATE)  # 24000

    def stream(self, sentence: str) -> Iterator[np.ndarray]:
        for chunk in self._tts.generate_stream(sentence, voice=self._voice):
            yield np.asarray(chunk, dtype=np.float32).flatten()

    def warmup(self) -> None:
        for _ in self.stream("Hi."):
            pass

    def close(self) -> None:
        pass


class VeniceTts:
    """Kokoro via Venice's hosted `/audio/speech`.

    Same model as KokoroTts (`tts-kokoro`), so the A/B isolates substrate.

    We ask for raw `pcm` rather than the default mp3: mp3 would need decoding
    before playback, putting a decoder inside the latency we are measuring.
    Venice's PCM for kokoro is 24 kHz signed 16-bit mono, matching the local
    engine's native rate, so the downstream player path is unchanged.

    Chunks are yielded as they come off the socket, so callers measure true
    time-to-first-audio rather than time-to-complete-file.
    """

    name = "venice-kokoro"

    def __init__(self, voice: str | None = None, model: str | None = None) -> None:
        import httpx

        self.model = model or os.environ.get("VENICE_TTS_MODEL", "tts-kokoro")
        self._voice = voice or os.environ.get("VENICE_TTS_VOICE", "af_heart")
        self.sample_rate = 24000
        self.name = f"venice:{self.model}"
        self._client = httpx.Client(
            base_url=_venice_base_url(),
            headers={"Authorization": f"Bearer {_venice_key()}"},
            timeout=httpx.Timeout(120.0),
        )

    def stream(self, sentence: str) -> Iterator[np.ndarray]:
        body = {
            "model": self.model,
            "input": sentence,
            "voice": self._voice,
            "response_format": "pcm",
            "streaming": True,
        }
        tail = b""
        with self._client.stream("POST", "/audio/speech", json=body) as r:
            r.raise_for_status()
            for raw in r.iter_bytes():
                if not raw:
                    continue
                buf = tail + raw
                # int16 frames are 2 bytes wide; carry an odd trailing byte
                # into the next chunk instead of dropping it, which would
                # desync every sample after it.
                usable = len(buf) - (len(buf) % 2)
                if usable <= 0:
                    tail = buf
                    continue
                tail = buf[usable:]
                yield np.frombuffer(buf[:usable], dtype=np.int16).astype(
                    np.float32
                ) / 32768.0

    def warmup(self) -> None:
        for _ in self.stream("Hi."):
            pass

    def close(self) -> None:
        self._client.close()


# ─────────────────────────── turn detectors ───────────────────────────


class NoTurnDetector:
    """The original behaviour: trust Silero's end-of-speech and reply.

    Kept as a named engine rather than a None check so the pipeline has one
    code path, and so `TURN_DETECTOR=off` stays a first-class, testable choice.
    """

    name = "off"

    def is_complete(self, audio_f32: np.ndarray) -> bool:
        return True

    def close(self) -> None:
        pass


class SmartTurnDetector:
    """Semantic end-of-turn detection via pipecat-ai/smart-turn v3.2 (ONNX).

    An 8M-param Whisper-Tiny backbone with a classifier head, run on CPU
    through onnxruntime. It answers "did this person finish their thought",
    which Silero cannot: Silero only knows sound stopped, so the pipeline had
    to wait out a fixed silence timeout and still cut people off on pauses.

    We deliberately depend on the raw .onnx weights plus transformers'
    WhisperFeatureExtractor rather than pulling in pipecat as a framework —
    the model is BSD-2-Clause and the preprocessing is ~10 lines.

    Cost: one inference per end-of-speech event, not per audio frame, so it
    sits outside the 32 ms VAD loop entirely.

    Note it covers 23 languages and Malayalam is not among them; worse, it
    reads lexical content rather than pure prosody, so it should not be
    assumed to generalize. `TURN_LANGUAGES` gates which languages use it.
    """

    name = "smartturn"

    HF_REPO = "pipecat-ai/smart-turn-v3"
    DEFAULT_FILE = "smart-turn-v3.2-cpu.onnx"
    WINDOW_S = 8  # the model is trained on a fixed 8 s window at 16 kHz

    def __init__(self, threshold: float = 0.5, filename: str | None = None) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import WhisperFeatureExtractor

        self.threshold = threshold
        path = hf_hub_download(
            repo_id=self.HF_REPO, filename=filename or self.DEFAULT_FILE
        )

        # Single-threaded sequential execution: this runs on the audio thread
        # between turns, and letting ORT spin up a thread pool per call costs
        # more than the inference itself at this model size.
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(path, sess_options=so)
        self._fx = WhisperFeatureExtractor(chunk_length=self.WINDOW_S)
        self.last_probability: float | None = None

    def _window(self, audio_f32: np.ndarray) -> np.ndarray:
        """Keep the last 8 s, or left-pad with silence to reach 8 s.

        Left-padding matters: the decision is about how the utterance *ended*,
        so the tail must stay anchored to the end of the window.
        """
        n = self.WINDOW_S * SR
        if len(audio_f32) > n:
            return audio_f32[-n:]
        if len(audio_f32) < n:
            return np.pad(audio_f32, (n - len(audio_f32), 0), mode="constant")
        return audio_f32

    def is_complete(self, audio_f32: np.ndarray) -> bool:
        audio = self._window(np.asarray(audio_f32, dtype=np.float32))
        inputs = self._fx(
            audio,
            sampling_rate=SR,
            return_tensors="np",
            padding="max_length",
            max_length=self.WINDOW_S * SR,
            truncation=True,
            do_normalize=True,
        )
        feats = np.expand_dims(inputs.input_features.squeeze(0).astype(np.float32), 0)
        prob = float(self._session.run(None, {"input_features": feats})[0][0].item())
        self.last_probability = prob
        return prob > self.threshold

    def close(self) -> None:
        self._session = None


# ─────────────────────────────── registry ─────────────────────────────


# Shorthands so callers say STT_ENGINE=parakeet rather than pasting a repo id.
STT_MODELS = {
    "parakeet": "mlx-community/parakeet-tdt-0.6b-v3",
    "nemotron": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
    "nemotron-8bit": "mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit",
    # -asr-fp16 rather than the more popular plain -turbo repo: that one ships
    # only config.json + weights, and mlx-audio's whisper loader needs
    # preprocessor_config.json + tokenizer to build a processor, so it fails
    # with "Processor not found". This variant carries all 14 files.
    "whisper": "mlx-community/whisper-large-v3-turbo-asr-fp16",
}


def build_stt() -> SttEngine:
    # Default is parakeet, chosen on measurement rather than reputation. On an
    # 8 s clip of the actual speaker, 5 runs each:
    #     parakeet    74 ms median (73-76)   transcript correct
    #     moonshine  396 ms median (385-399) transcript correct
    #     nemotron   702 ms median (700-715) transcript correct
    # Identical accuracy on the full utterance at 5.4x Moonshine's speed, and
    # better on truncated audio (kept "tomorrow" where Moonshine substituted
    # "today"). Moonshine stays one env var away.
    choice = os.environ.get("STT_ENGINE", "parakeet").strip().lower()
    if choice == "venice":
        return VeniceStt(
            model=os.environ.get("VENICE_STT_MODEL") or None,
            language=os.environ.get("STT_LANGUAGE") or None,
        )
    if choice == "moonshine":
        return MoonshineStt(
            language=os.environ.get("STT_LANGUAGE", "en"),
            arch=os.environ.get("MOONSHINE_ARCH") or None,
        )
    if choice in STT_MODELS or "/" in choice:
        return MlxAudioStt(
            model_id=STT_MODELS.get(choice, os.environ.get("STT_ENGINE", "")),
            language=os.environ.get("STT_LANGUAGE") or None,
        )
    raise SystemExit(
        f"unknown STT_ENGINE={choice!r}. "
        f"available: moonshine, {', '.join(STT_MODELS)}, or an mlx-audio repo id"
    )


def build_tts() -> TtsEngine:
    choice = os.environ.get("TTS_ENGINE", "kokoro").strip().lower()
    if choice == "kokoro":
        return KokoroTts(voice=os.environ.get("TTS_VOICE") or None)
    if choice in ("kokoro-legacy", "kokoro_legacy"):
        return KokoroLegacyTts(voice=os.environ.get("TTS_VOICE") or None)
    if choice == "venice":
        return VeniceTts(
            voice=os.environ.get("VENICE_TTS_VOICE") or None,
            model=os.environ.get("VENICE_TTS_MODEL") or None,
        )
    raise SystemExit(
        f"unknown TTS_ENGINE={choice!r}. available: kokoro, kokoro-legacy, venice"
    )


def build_turn_detector() -> TurnDetector:
    choice = os.environ.get("TURN_DETECTOR", "off").strip().lower()
    if choice in ("off", "none", "0"):
        return NoTurnDetector()
    if choice in ("smartturn", "smart_turn", "smart-turn"):
        # Default 0.6 rather than the model's own 0.5. Two reasons, one solid
        # and one weak, so treat it as a starting point and not a tuned value:
        #
        #  - Solid: the costs are asymmetric. Declaring "complete" too eagerly
        #    talks over the user; declaring "incomplete" too eagerly just waits
        #    a beat longer. Requiring more confidence to interrupt is right.
        #  - Weak: on scripts/smoke_turn.py's 8 cases, 0.5 misses one trailing
        #    "...I really want to" at p=0.530 and 0.6 gets all eight. That is 8
        #    samples of synthetic speech, not evidence — the vendor calibrated
        #    0.5 on 31,527 real samples. Revisit against your own voice.
        return SmartTurnDetector(
            threshold=float(os.environ.get("TURN_THRESHOLD", "0.6")),
            filename=os.environ.get("SMART_TURN_FILE") or None,
        )
    raise SystemExit(
        f"unknown TURN_DETECTOR={choice!r}. available: off, smartturn"
    )
