"""Time the current cloud brain: Haiku replies and Fish Audio on Rocky's lines.

Does not change how Rocky runs. Writes experiments/out/baseline/report.json
and one wav per spoken line.

  server/.venv/bin/python experiments/baseline_cloud.py
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

from brain import config  # noqa: E402
from brain import mouth, personality  # noqa: E402
from experiments.rocky_lines import HAIKU_PROMPTS, voice_lines  # noqa: E402

OUT = ROOT / "experiments" / "out" / "baseline"
FISH_RATE = 16_000


def rss_mb() -> float:
    """Current resident set, megabytes. macOS ps reports kilobytes."""
    raw = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)
    return int(raw.strip() or "0") / 1024


def peak_mb() -> float:
    # macOS: ru_maxrss is bytes. Linux: kilobytes.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value / (1024 * 1024)
    return value / 1024


def machine() -> dict:
    mem = 0
    try:
        mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    except (OSError, subprocess.CalledProcessError):
        pass
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "memory_gb": round(mem / (1024 ** 3), 1) if mem else None,
    }


def write_pcm(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def time_haiku() -> list[dict]:
    import openai

    client = openai.OpenAI(
        base_url=config.LLM_BASE_URL,
        api_key=os.environ.get("LLM_API_KEY", "missing"),
        default_headers={"X-Title": "desk-robot-baseline"},
    )
    rows = []
    for i, prompt in enumerate(HAIKU_PROMPTS, start=1):
        started = time.perf_counter()
        first = None
        parts: list[str] = []
        stream = client.chat.completions.create(
            model=config.MODEL,
            max_tokens=200,
            messages=[
                {"role": "system", "content": personality.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
        )
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content
                if not piece:
                    continue
                if first is None:
                    first = time.perf_counter()
                parts.append(piece)
        finally:
            stream.close()
        finished = time.perf_counter()
        reply = "".join(parts)
        row = {
            "n": i,
            "prompt": prompt,
            "reply": reply,
            "ttft_s": round(first - started, 3) if first else None,
            "total_s": round(finished - started, 3),
            "chars": len(reply),
            "rss_mb": round(rss_mb(), 1),
        }
        rows.append(row)
        print(f"haiku {i:02d}  ttft {row['ttft_s']}s  total {row['total_s']}s")
        print(f"         {reply[:180]}")
    return rows


def time_fish(lines: list[dict]) -> list[dict]:
    rows = []
    for i, line in enumerate(lines, start=1):
        text = line["text"]
        started = time.perf_counter()
        first = None
        chunks: list[bytes] = []
        for chunk in mouth._fish_stream(text):
            if first is None:
                first = time.perf_counter()
            chunks.append(chunk)
        finished = time.perf_counter()
        pcm = b"".join(chunks)
        audio_s = len(pcm) / 2 / FISH_RATE
        total = finished - started
        path = OUT / "fish" / f"{i:02d}-{line['id']}.wav"
        write_pcm(path, pcm, FISH_RATE)
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
            "wav": str(path.relative_to(ROOT)),
        }
        rows.append(row)
        print(f"fish  {i:02d}  ttfa {row['ttfa_s']}s  total {row['total_s']}s  rtf {row['rtf']}  {text}")
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("LLM_API_KEY"):
        sys.exit("LLM_API_KEY is missing from server/.env")
    if not mouth.fish_available():
        sys.exit("Fish Audio is not configured (FISH_AUDIO_API_KEY / TTS_VOICE_ID)")
    print(f"model {config.MODEL}  voice {config.TTS_VOICE_ID}")
    report = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine(),
        "note": "Text only. No camera frame. RTF is generation_seconds / audio_seconds; below 1 is faster than realtime.",
        "haiku": {"model": config.MODEL, "base_url": config.LLM_BASE_URL, "prompts": time_haiku()},
        "fish": {"voice_id": config.TTS_VOICE_ID, "sample_rate": FISH_RATE, "lines": time_fish(voice_lines())},
        "process_peak_mb": round(peak_mb(), 1),
    }
    dest = OUT / "report.json"
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
