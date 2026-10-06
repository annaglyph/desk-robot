"""Personality audition: Haiku 4.5 vs Qwen3-VL-8B-Instruct 4-bit.

Reuses the Haiku replies from experiments/out/personality/. Does not call
Haiku again, does not change Rocky, and does not overwrite the 4B results.

  # server already listening on 127.0.0.1:8091 with the 8B model
  server/.venv/bin/python experiments/audition_personality_8b.py --server-pid PID

Writes experiments/out/personality-8b/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import openai

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "experiments"))

from brain import config  # noqa: E402
from brain import personality  # noqa: E402
import audition_personality as base  # noqa: E402

OUT = ROOT / "experiments" / "out" / "personality-8b"
PREVIOUS = ROOT / "experiments" / "out" / "personality" / "responses.json"
LOCAL_URL = "http://127.0.0.1:8091/v1"
LOCAL_MODEL = "mlx-community/Qwen3-VL-8B-Instruct-4bit"
LOCAL_NAME = "qwen8b"
CLOUD_NAME = "haiku"

# A line this long, repeated, is the 4B failure mode (example lines and
# whole clauses copied again). Short catchphrases stay under it.
_LINE_MIN = 30
_GRAM = 8


def repetition(text: str) -> dict:
    """Mechanical loop check. No model is asked to judge it."""
    lines = []
    for raw in re.split(r"\n+", text):
        line = re.sub(r"\s+", " ", raw).strip()
        line = re.sub(r"^\[\w+\]\s*", "", line).strip().lower()
        if len(line) >= _LINE_MIN:
            lines.append(line)
    counts = Counter(lines)
    repeated = {line: n for line, n in counts.items() if n >= 2}
    words = re.findall(r"[a-z0-9']+", text.lower())
    grams: Counter[tuple[str, ...]] = Counter()
    for i in range(0, max(0, len(words) - _GRAM + 1)):
        grams[tuple(words[i : i + _GRAM])] += 1
    max_gram = max(grams.values()) if grams else 1
    return {
        "repeated_lines": len(repeated),
        "max_line_repeats": max(repeated.values()) if repeated else 1,
        "max_8gram_repeats": max_gram,
        "loop": bool(repeated) or max_gram >= 3,
    }


def annotate(row: dict) -> dict:
    """Re-score a saved or fresh reply with the shared checks plus loops."""
    reply = row["reply"]
    emotion = base.emotion_of(reply)
    sentences = base.sentence_count(reply)
    info = base.structure(reply)
    loops = repetition(reply)
    flags = base.mechanical_flags(reply, emotion, sentences)
    if loops["loop"] and "repetition_loop" not in flags:
        flags.append("repetition_loop")
    row = dict(row)
    row.update({
        "chars": len(reply),
        "words": len(reply.split()),
        "sentences": sentences,
        "emotion_tag": emotion["tag"],
        "emotion_valid": emotion["valid"],
        "emotion_reason": emotion["reason"],
        "tag_count": info["tag_count"],
        "later_tags": info["later_tags"],
        "invalid_tags": info["invalid_tags"],
        "echoed_examples": info["echoed_examples"],
        "ends_with_punct": info["ends_with_punct"],
        "repeated_lines": loops["repeated_lines"],
        "max_line_repeats": loops["max_line_repeats"],
        "max_8gram_repeats": loops["max_8gram_repeats"],
        "flags": flags,
    })
    return row


def load_haiku() -> dict:
    previous = json.loads(PREVIOUS.read_text())
    digest = hashlib.sha256(personality.SYSTEM_PROMPT.encode()).hexdigest()
    if previous["system_prompt_sha256"] != digest:
        sys.exit("SYSTEM_PROMPT changed since the Haiku replies were saved")
    if [item["prompt"] for item in previous["isolated"]] != [item["prompt"] for item in base.ISOLATED]:
        sys.exit("isolated prompts do not match the saved Haiku audition")
    if [turn["prompt"] for turn in previous["soup"]] != list(base.CONVERSATION):
        sys.exit("soup prompts do not match the saved Haiku audition")
    haiku = {
        "source": str(PREVIOUS.relative_to(ROOT)),
        "measured_at": previous["measured_at"],
        "model": previous["cloud"]["model"],
        "base_url": previous["cloud"]["base_url"],
        "warmup": annotate(previous["warmup"]["haiku"]),
        "isolated": [],
        "soup": [],
    }
    by_id = {item["id"]: item for item in previous["isolated"]}
    for item in base.ISOLATED:
        saved = by_id[item["id"]]
        haiku["isolated"].append({
            "id": item["id"],
            "title": item["title"],
            "prompt": item["prompt"],
            "row": annotate(saved["haiku"]),
        })
    for turn in previous["soup"]:
        haiku["soup"].append({
            "turn": turn["turn"],
            "prompt": turn["prompt"],
            "row": annotate(turn["haiku"]),
        })
    return haiku


def complete(client: openai.OpenAI, model: str, messages: list[dict], server_pid: int | None) -> dict:
    started = time.perf_counter()
    first = None
    parts: list[str] = []
    error = None
    finish = None
    stream = None
    try:
        stream = client.chat.completions.create(
            model=model,
            max_tokens=200,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish = choice.finish_reason
            piece = choice.delta.content
            if not piece:
                continue
            if first is None:
                first = time.perf_counter()
            parts.append(piece)
    except openai.APIStatusError as exc:
        error = f"HTTP {exc.status_code}: {exc.message}"
    except openai.APIConnectionError as exc:
        error = f"connection: {exc}"
    finally:
        if stream is not None:
            stream.close()
    finished = time.perf_counter()
    row = annotate({
        "reply": "".join(parts),
        "error": error,
        "ttft_s": round(first - started, 3) if first else None,
        "total_s": round(finished - started, 3),
        "finish_reason": finish,
        "server_rss_mb": base.rss_mb(server_pid) if server_pid else None,
    })
    if finish == "length" and "cut_off" not in row["flags"]:
        row["flags"].append("token_cap")
    elif finish == "length":
        row["flags"].append("token_cap")
    return row


def mentions_thirteen(row: dict) -> bool:
    return "thirteen" in row["reply"].lower() or re.search(r"\b13\b", row["reply"]) is not None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-pid", type=int, default=None)
    parser.add_argument("--local-url", default=LOCAL_URL)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    haiku = load_haiku()
    seed = random.SystemRandom().randrange(1, 2**63)
    rng = random.Random(seed)
    local = openai.OpenAI(base_url=args.local_url, api_key="not-needed")

    def call(messages: list[dict]) -> dict:
        row = complete(local, LOCAL_MODEL, messages, args.server_pid)
        print(
            f"  qwen8b ttft {row['ttft_s']} total {row['total_s']} "
            f"chars {row['chars']} finish {row['finish_reason']} rss {row['server_rss_mb']} "
            f"{row['error'] or ''}"
        )
        return row

    print("warmup")
    warmup = call([
        {"role": "system", "content": personality.SYSTEM_PROMPT},
        base.user_message(base.WARMUP),
    ])

    isolated_raw = []
    audition_pairs = []
    key_isolated = []
    for item, saved in zip(base.ISOLATED, haiku["isolated"]):
        print(item["id"])
        row = call([
            {"role": "system", "content": personality.SYSTEM_PROMPT},
            base.user_message(item["prompt"]),
        ])
        order = [CLOUD_NAME, LOCAL_NAME]
        rng.shuffle(order)
        letters = {"A": order[0], "B": order[1]}
        replies = {CLOUD_NAME: saved["row"]["reply"], LOCAL_NAME: row["reply"]}
        key_isolated.append({"id": item["id"], "A": letters["A"], "B": letters["B"]})
        audition_pairs.append({
            "title": item["title"],
            "prompt": item["prompt"],
            "A": replies[letters["A"]],
            "B": replies[letters["B"]],
        })
        isolated_raw.append({
            "id": item["id"],
            "title": item["title"],
            "prompt": item["prompt"],
            CLOUD_NAME: saved["row"],
            LOCAL_NAME: row,
        })

    print("soup")
    history: list[dict] = []
    turns_raw = []
    audition_turns = []
    for saved in haiku["soup"]:
        print(f"  turn {saved['turn']}")
        messages = [{"role": "system", "content": personality.SYSTEM_PROMPT}, *history]
        messages.append(base.user_message(saved["prompt"]))
        row = call(messages)
        history.append(base.user_message(saved["prompt"]))
        history.append({"role": "assistant", "content": row["reply"]})
        turns_raw.append({
            "turn": saved["turn"],
            "prompt": saved["prompt"],
            CLOUD_NAME: saved["row"],
            LOCAL_NAME: row,
        })
        audition_turns.append({"prompt": saved["prompt"]})
    soup_order = [CLOUD_NAME, LOCAL_NAME]
    rng.shuffle(soup_order)
    soup_letters = {"A": soup_order[0], "B": soup_order[1]}
    for turn, raw in zip(audition_turns, turns_raw):
        turn["A"] = raw[soup_letters["A"]]["reply"]
        turn["B"] = raw[soup_letters["B"]]["reply"]

    audition = base.write_audition(audition_pairs, {"turns": audition_turns})
    leaked = [word for word in base.BANNED_IN_AUDITION if word in audition.lower()]
    if leaked:
        sys.exit(f"audition would name a model: {leaked}")

    def pack(name: str) -> dict:
        rows = [item[name] for item in isolated_raw]
        soup_rows = [turn[name] for turn in turns_raw]
        return {
            "isolated": base.summarize(rows),
            "soup": base.summarize(soup_rows),
            "all_scored": base.summarize(rows + soup_rows),
        }

    responses = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": base.machine(),
        "note": (
            "Text only. No image. No tools. personality.SYSTEM_PROMPT passed through unchanged. "
            "max_tokens=200, stream=True, no temperature or other sampling overrides. "
            "Thinking left off (--enable-thinking not set). "
            "User turns are one text content part, matching thinking.py when no camera frame is attached. "
            "Isolated prompts are fresh chats. The soup conversation appends each model's own raw reply "
            "before the next turn. Haiku replies and timings are copied from the 4B audition and were not "
            "regenerated. Qwen 8B had one discarded warmup, stored here and not in the audition. "
            "Letters are only in key.json. "
            "Sentence counts use the same splitter as thinking.py. The live brain would speak at most "
            "3 sentences; these files keep the full generation. "
            "repetition_loop means a line of 30 or more characters appears twice, or the same 8 words "
            "appear three times. echoed_example means a distinctive line from the system prompt's "
            "example replies appeared. Catchphrases the prompt asks for are not counted as echoes."
        ),
        "system_prompt_sha256": hashlib.sha256(personality.SYSTEM_PROMPT.encode()).hexdigest(),
        "system_prompt": personality.SYSTEM_PROMPT,
        "max_tokens": 200,
        "reply_max_sentences": config.REPLY_MAX_SENTENCES,
        "emotions": list(config.EMOTIONS),
        "prompts": {
            "isolated": [{"id": item["id"], "prompt": item["prompt"]} for item in base.ISOLATED],
            "soup": list(base.CONVERSATION),
        },
        "cloud": {
            "name": CLOUD_NAME,
            "model": haiku["model"],
            "base_url": haiku["base_url"],
            "reused_from": haiku["source"],
            "reused_measured_at": haiku["measured_at"],
        },
        "local": {
            "name": LOCAL_NAME,
            "model": LOCAL_MODEL,
            "base_url": args.local_url,
            "server": "mlx-vlm 0.7.6, mlx_vlm.server, --enable-thinking not set",
            "server_pid": args.server_pid,
        },
        "warmup_prompt": base.WARMUP,
        "warmup": {CLOUD_NAME: haiku["warmup"], LOCAL_NAME: warmup},
        "isolated": isolated_raw,
        "soup": turns_raw,
        "bean_memory": {
            "note": "Objective string check only. Turn 3 is 'I ate four.' Turn 4 asks how many beans are left.",
            CLOUD_NAME: {
                "turn3_mentions_thirteen": mentions_thirteen(turns_raw[2][CLOUD_NAME]),
                "turn4_mentions_thirteen": mentions_thirteen(turns_raw[3][CLOUD_NAME]),
            },
            LOCAL_NAME: {
                "turn3_mentions_thirteen": mentions_thirteen(turns_raw[2][LOCAL_NAME]),
                "turn4_mentions_thirteen": mentions_thirteen(turns_raw[3][LOCAL_NAME]),
            },
        },
        "measurements": {CLOUD_NAME: pack(CLOUD_NAME), LOCAL_NAME: pack(LOCAL_NAME)},
    }

    key = {
        "seed": seed,
        "note": "A and B in audition.md. Isolated items were shuffled separately. The soup conversation uses one shuffle for every turn. haiku replies were reused from the 4B audition.",
        "isolated": key_isolated,
        "soup": soup_letters,
    }
    (OUT / "audition.md").write_text(audition)
    (OUT / "key.json").write_text(json.dumps(key, indent=2) + "\n")
    (OUT / "responses.json").write_text(json.dumps(responses, indent=2) + "\n")
    print(f"wrote {OUT / 'audition.md'}")
    print(f"wrote {OUT / 'key.json'}")
    print(f"wrote {OUT / 'responses.json'}")


if __name__ == "__main__":
    main()
