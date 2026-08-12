"""Turn a conversation log into a benchmark.

Reads `turn_log.jsonl` (written by every live turn — see `turnlog.py`) and
reports per-stage latency grouped by configuration.

Why medians and p90 rather than means: a voice turn's distribution has a tail —
a long utterance, a cold slot, a retry — and one 8-second outlier moves a mean
of ten turns by nearly a second while moving the median not at all. What a user
perceives as "the agent is fast" is the median; what they remember as "it
hung" is the p90. A mean describes neither.

    uv run python scripts/analyze_turns.py
    uv run python scripts/analyze_turns.py --log turn_log.jsonl
    uv run python scripts/analyze_turns.py --since 2026-08-12
    uv run python scripts/analyze_turns.py --raw       # one line per turn
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# stage key -> (label, unit). Ordered as the pipeline runs, so the table reads
# in the order the milliseconds are actually spent.
STAGES = [
    ("dispatch_ms", "queue hop"),
    ("stt_ms", "STT"),
    ("llm_ttft_ms", "LLM first token"),
    ("llm_first_sentence_ms", "LLM first sentence"),
    ("tts_first_ms", "TTS first audio"),
    ("first_audio_ms", "→ FIRST AUDIO"),
    ("llm_total_ms", "LLM last token"),
    ("turn_total_ms", "turn total"),
]


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation — with 8 turns in a log,
    interpolating between two samples invents a number that was never measured."""
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def load(path: Path, since: str | None) -> list[dict]:
    if not path.exists():
        sys.exit(
            f"no log at {path}\n"
            "Hold a conversation first: `LLM_BACKEND=venice uv run python voice_agent.py`"
        )
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and r.get("ts", "") < since:
            continue
        rows.append(r)
    return rows


def config_key(r: dict) -> str:
    llm = r.get("llm_backend", "?")
    model = (r.get("llm_model") or "").split("/")[-1][:34]
    return f"LLM {llm} ({model})  ·  STT {r.get('stt_engine','?')}  ·  TTS {r.get('tts_engine','?')}"


def report(rows: list[dict]) -> None:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(config_key(r), []).append(r)

    for cfg, turns in groups.items():
        spoke = [t for t in turns if t.get("first_audio_ms") is not None]
        dropped = len(turns) - len(spoke)
        print(f"\n\033[1m{cfg}\033[0m")
        print(f"  {len(turns)} turns logged, {len(spoke)} produced audio"
              + (f", {dropped} did not (no speech / error / cut)" if dropped else ""))
        if not spoke:
            continue

        in_s = [t["audio_in_s"] for t in spoke]
        print(f"  input speech: median {statistics.median(in_s):.1f}s "
              f"(range {min(in_s):.1f}–{max(in_s):.1f}s)")

        print(f"\n  {'stage':<22}{'median':>10}{'p90':>10}{'min':>10}{'max':>10}   n")
        print(f"  {'-'*22}{'-'*10}{'-'*10}{'-'*10}{'-'*10}  ---")
        for key, label in STAGES:
            vals = [t[key] for t in spoke if t.get(key) is not None]
            if not vals:
                continue
            bold = key == "first_audio_ms"
            line = (f"  {label:<22}{statistics.median(vals):>9.0f}ms"
                    f"{pct(vals, 0.9):>9.0f}ms{min(vals):>9.0f}ms{max(vals):>9.0f}ms"
                    f"  {len(vals):>3}")
            print(f"\033[1m{line}\033[0m" if bold else line)

        # Throughput figures, which are rates rather than latencies and do not
        # belong in the same column as milliseconds.
        rates = [t["llm_chunk_s"] for t in spoke if t.get("llm_chunk_s")]
        rtfs = [t["tts_rtf"] for t in spoke if t.get("tts_rtf")]
        chunks = [t["llm_chunks"] for t in spoke if t.get("llm_chunks")]
        print()
        if rates:
            print(f"  LLM decode      median {statistics.median(rates):.1f} chunks/s"
                  f"   reply length median {statistics.median(chunks):.0f} chunks")
        if rtfs:
            print(f"  TTS synthesis   median {statistics.median(rtfs):.1f}x realtime")

        # Does the breakdown add up? If the three critical-path stages do not
        # sum to first-audio, the difference is time nothing has claimed —
        # scheduling, thread contention, or a stage nobody is timing.
        gaps = []
        for t in spoke:
            parts = [t.get("dispatch_ms"), t.get("stt_ms"),
                     t.get("llm_first_sentence_ms"), t.get("tts_first_ms")]
            if all(p is not None for p in parts):
                gaps.append(t["first_audio_ms"] - sum(parts))
        if gaps:
            med = statistics.median(gaps)
            flag = "  ← unattributed, worth chasing" if abs(med) > 50 else ""
            print(f"  unaccounted     median {med:+.0f}ms{flag}")

        multi = [t for t in spoke if len(t.get("sentences", [])) > 1]
        if multi:
            # issues/0002. `gap_ms` is silence the listener actually heard
            # between sentences: first frame of sentence N+1 minus last frame of
            # sentence N, both stamped by the playback thread.
            #
            # Rows written before synthesis and playback were split have no
            # `gap_ms`. For those, fall back to the old proxy — the synthesis
            # time of later sentences — which was equal to the gap back when
            # synthesis only started after playback finished. The two are not
            # comparable, so they are reported under different labels rather
            # than pooled: pooling them would show the fix making things worse.
            heard = [s["gap_ms"] for t in multi
                     for s in t["sentences"][1:] if s.get("gap_ms") is not None]
            legacy = [s["synth_first_ms"] for t in multi
                      for s in t["sentences"][1:]
                      if s.get("gap_ms") is None and s.get("synth_first_ms") is not None]
            if heard:
                print(f"  gap heard between sentences (issues/0002): "
                      f"median {statistics.median(heard):.0f}ms over {len(heard)} sentences")
            if legacy:
                print(f"  gap before later sentences, pre-split rows (issues/0002): "
                      f"median {statistics.median(legacy):.0f}ms over {len(legacy)} sentences")

        cut = sum(1 for t in turns if t.get("interrupted"))
        if cut:
            print(f"  barge-ins: {cut} of {len(turns)} turns cut short")


def raw(rows: list[dict]) -> None:
    for r in rows:
        fa = r.get("first_audio_ms")
        ts = r.get("ts", "?")
        if fa is None:
            print(f"{ts}  (no audio: {r.get('error') or 'cut short'})")
            continue
        print(f"{ts}  {r.get('llm_backend','?'):>7}  "
              f"in {r.get('audio_in_s', 0):>4.1f}s  "
              f"stt {r.get('stt_ms', 0):>6.0f}  "
              f"llm1 {(r.get('llm_first_sentence_ms') or 0):>6.0f}  "
              f"tts1 {(r.get('tts_first_ms') or 0):>6.0f}  "
              f"first-audio {fa:>7.0f}ms")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(ROOT / "turn_log.jsonl"))
    ap.add_argument("--since", help="ISO date/time; only rows at or after it")
    ap.add_argument("--raw", action="store_true", help="one line per turn")
    a = ap.parse_args()

    rows = load(Path(a.log), a.since)
    if not rows:
        sys.exit("log is empty (or --since filtered everything out)")
    print(f"\n{len(rows)} turns from {Path(a.log).name}")
    if a.raw:
        raw(rows)
    else:
        report(rows)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
