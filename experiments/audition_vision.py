"""Compare one webcam JPEG on Haiku and a local MLX VLM.

Does not change Rocky. The user message is the same shape thinking.py sends:
a text question, the live-camera note, then an image_url data URL.

  server/.venv/bin/python experiments/audition_vision.py --server-pid PID

The local server is started separately:

  experiments/.venv-vision/bin/mlx_vlm.server \\
    --host 127.0.0.1 --port 8091 \\
    --model mlx-community/Qwen3-VL-4B-Instruct-4bit
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import openai

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from brain import config  # noqa: E402

OUT = ROOT / "experiments" / "out" / "vision"
JPEG = OUT / "frame.jpg"
LOCAL_URL = "http://127.0.0.1:8091/v1"
LOCAL_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"

# Same sentence thinking.py appends when a fresh JPEG is attached.
CAMERA_NOTE = (
    "(Live picture from your camera, taken just now, because the question "
    "seems to be about what you can see. If it isn't, ignore the picture.)"
)
SYSTEM = (
    "Answer the question about the attached camera picture. "
    "Be brief and literal. If the picture does not show it, say that."
)
QUESTIONS = [
    "What can you see?",
    "What am I holding?",
    "What colour is it?",
    "How many fingers am I holding up?",
    "Read this.",
]


def rss_mb(pid: int) -> float | None:
    try:
        raw = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    text = raw.strip()
    if not text:
        return None
    return round(int(text) / 1024, 1)


def image_part(jpeg: bytes) -> dict:
    data = base64.standard_b64encode(jpeg).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


def user_message(question: str, jpeg: bytes) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "text", "text": CAMERA_NOTE},
            image_part(jpeg),
        ],
    }


def ask(client: openai.OpenAI, model: str, question: str, jpeg: bytes, server_pid: int | None) -> dict:
    started = time.perf_counter()
    first = None
    parts: list[str] = []
    error = None
    try:
        stream = client.chat.completions.create(
            model=model,
            max_tokens=200,
            messages=[
                {"role": "system", "content": SYSTEM},
                user_message(question, jpeg),
            ],
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = delta.content or ""
            if text and first is None:
                first = time.perf_counter()
            if text:
                parts.append(text)
    except openai.APIStatusError as exc:
        error = f"HTTP {exc.status_code}: {exc.message}"
    except openai.APIConnectionError as exc:
        error = f"connection: {exc}"
    finished = time.perf_counter()
    reply = "".join(parts).strip()
    return {
        "question": question,
        "reply": reply,
        "error": error,
        "ttft_s": round(first - started, 3) if first else None,
        "total_s": round(finished - started, 3),
        "chars": len(reply),
        "server_rss_mb": rss_mb(server_pid) if server_pid else None,
    }


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-pid", type=int, default=None)
    parser.add_argument("--local-url", default=LOCAL_URL)
    parser.add_argument("--skip-haiku", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    args = parser.parse_args()

    jpeg = JPEG.read_bytes()
    report: dict = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine(),
        "jpeg": str(JPEG.relative_to(ROOT)),
        "jpeg_bytes": len(jpeg),
        "note": (
            "Each question is a fresh chat with the same JPEG. "
            "The user message matches thinking.py: question, camera note, "
            "image_url data URL. max_tokens=200, stream=True. "
            "Personality and tools are not part of this run."
        ),
        "system": SYSTEM,
        "camera_note": CAMERA_NOTE,
    }

    if not args.skip_haiku:
        client = openai.OpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=os.environ.get("LLM_API_KEY", "missing"),
            default_headers={"X-Title": "desk-robot-vision"},
        )
        rows = []
        print(f"haiku {config.MODEL}")
        for question in QUESTIONS:
            row = ask(client, config.MODEL, question, jpeg, None)
            rows.append(row)
            print(f"  ttft {row['ttft_s']}  total {row['total_s']}  {row['error'] or row['reply'][:180]}")
        report["haiku"] = {
            "model": config.MODEL,
            "base_url": config.LLM_BASE_URL,
            "lines": rows,
        }

    if not args.skip_local:
        client = openai.OpenAI(base_url=args.local_url, api_key="not-needed")
        rows = []
        print(f"qwen {LOCAL_MODEL} rss {rss_mb(args.server_pid) if args.server_pid else '?'}")
        for question in QUESTIONS:
            row = ask(client, LOCAL_MODEL, question, jpeg, args.server_pid)
            rows.append(row)
            print(f"  ttft {row['ttft_s']}  total {row['total_s']}  rss {row['server_rss_mb']}  {row['error'] or row['reply'][:180]}")
        report["qwen"] = {
            "model": LOCAL_MODEL,
            "base_url": args.local_url,
            "server_pid": args.server_pid,
            "rss_after_mb": rss_mb(args.server_pid) if args.server_pid else None,
            "lines": rows,
        }

    dest = OUT / "report.json"
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
