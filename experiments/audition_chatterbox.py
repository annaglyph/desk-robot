"""Audition Chatterbox Turbo on Rocky's lines. Does not touch the brain.

Uses the model's own default voice, not a clone of the Fish recording.
Writes experiments/out/chatterbox/report.json and one wav per line.

  experiments/.venv/bin/python experiments/audition_chatterbox.py

The venv is separate from Rocky's. Create it with:
  python3.13 -m venv experiments/.venv
  experiments/.venv/bin/pip install mlx-audio
"""

from __future__ import annotations

import json
import os
import platform
import resource
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.rocky_lines import voice_lines  # noqa: E402

MODEL_ID = "mlx-community/chatterbox-turbo-fp16"
OUT = ROOT / "experiments" / "out" / "chatterbox"


def rss_mb() -> float:
    raw = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)
    return int(raw.strip() or "0") / 1024


def peak_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value / (1024 * 1024)
    return value / 1024


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def main() -> None:
    import mlx.core as mx
    from huggingface_hub import snapshot_download
    from mlx_audio.tts.utils import load

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"fetching {MODEL_ID}")
    started = time.perf_counter()
    path = snapshot_download(MODEL_ID)
    fetch_s = time.perf_counter() - started
    print(f"weights on disk in {fetch_s:.1f}s  ({path})")

    rss_before = rss_mb()
    started = time.perf_counter()
    model = load(path)
    load_s = time.perf_counter() - started
    rss_loaded = rss_mb()
    print(f"cold load {load_s:.2f}s  rss {rss_before:.0f} -> {rss_loaded:.0f} MB")

    rows = []
    rate = int(model.sample_rate)
    for i, line in enumerate(voice_lines(), start=1):
        text = line["text"]
        started = time.perf_counter()
        first = None
        pieces: list[np.ndarray] = []
        # cfg/exaggeration are ignored by Turbo and would only log a warning.
        for result in model.stream_generate(text, cfg_weight=0.0, exaggeration=0.0, min_p=0.0):
            mx.eval(result.audio)
            if first is None:
                first = time.perf_counter()
            pieces.append(np.array(result.audio, dtype=np.float32).reshape(-1))
        finished = time.perf_counter()
        audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        audio_s = len(audio) / rate if rate else 0.0
        total = finished - started
        wav_path = OUT / f"{i:02d}-{line['id']}.wav"
        write_wav(wav_path, audio, rate)
        row = {
            "n": i,
            "id": line["id"],
            "kind": line["kind"],
            "text": text,
            "ttfa_s": round(first - started, 3) if first else None,
            "total_s": round(total, 3),
            "audio_s": round(audio_s, 3),
            "rtf": round(total / audio_s, 3) if audio_s else None,
            "rss_mb": round(rss_mb(), 1),
            "wav": str(wav_path.relative_to(ROOT)),
        }
        rows.append(row)
        print(f"chatterbox {i:02d}  ttfa {row['ttfa_s']}s  total {row['total_s']}s  rtf {row['rtf']}  {text}")

    mem = 0
    try:
        mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    except (OSError, subprocess.CalledProcessError):
        pass
    report = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "memory_gb": round(mem / (1024 ** 3), 1) if mem else None,
        },
        "model": MODEL_ID,
        "voice": "built-in Chatterbox default (not a clone of the Fish voice)",
        "note": "RTF is generation_seconds / audio_seconds; below 1 is faster than realtime. Cold load is weights already on disk into memory, not the download.",
        "fetch_s": round(fetch_s, 1),
        "cold_load_s": round(load_s, 2),
        "rss_before_load_mb": round(rss_before, 1),
        "rss_after_load_mb": round(rss_loaded, 1),
        "sample_rate": rate,
        "mlx_peak_gb": round(mx.get_peak_memory() / 1e9, 2),
        "process_peak_mb": round(peak_mb(), 1),
        "lines": rows,
    }
    dest = OUT / "report.json"
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
