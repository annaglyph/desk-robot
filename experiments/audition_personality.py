"""Blind personality audition: Rocky's system prompt, text only.

Compares the current cloud model with the local MLX model that passed the
vision run. Does not change Rocky. No camera image, no tools.

  # server already listening on 127.0.0.1:8091
  server/.venv/bin/python experiments/audition_personality.py --server-pid PID

Writes experiments/out/personality/:
  audition.md     prompts and replies, letters only
  key.json        which model each letter was
  responses.json  raw replies, timings, and mechanical checks
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import openai

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from brain import config  # noqa: E402
from brain import personality  # noqa: E402

OUT = ROOT / "experiments" / "out" / "personality"
LOCAL_URL = "http://127.0.0.1:8091/v1"
LOCAL_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
LOCAL_NAME = "qwen"
CLOUD_NAME = "haiku"

# Same splitter thinking.py uses before it speaks a sentence.
_SENTENCE_END = re.compile(r"(?<=[!?])\s+|(?<=[^.]\.)\s+")
_EMOTION_TAG = re.compile(r"^\s*\[(\w+)\]\s*", re.S)
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)
_ASSISTANT = re.compile(
    r"\b(as an ai|language model|i am an assistant|i'm an assistant|"
    r"how can i help|i'd be happy to|i would be happy to)\b",
    re.I,
)
_LIST_OR_HEADING = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+\S|\d+\.\s+\S)")
_TAG = re.compile(r"\[(\w+)\]")
# Lines from the example replies in the system prompt. Catchphrases the
# prompt asks Rocky to use ("fist my bump", "amaze") are not in this list.
_EXAMPLE_ECHOES = (
    "solder bridge on pin three",
    "what is “weekend”",
    'what is "weekend"',
    "show me photo, i look closer",
    "coffee is fuel",
    "new circuit board",
    "pin number wrong",
    "awake since five",
    "two amp on stall",
    "wake me when you find bug",
)

# Fresh chats. Each one is a single user turn.
ISOLATED = [
    {"id": "greeting", "title": "Greeting", "prompt": "Hey Rocky."},
    {
        "id": "silly",
        "title": "Screwdriver",
        "prompt": "I balanced the screwdriver on my nose. It fell in the plant.",
    },
    {
        "id": "kettle",
        "title": "Kettle",
        "prompt": "Why do humans stare at a kettle while it boils? I just did that for three minutes.",
    },
    {
        "id": "humans",
        "title": "Humans",
        "prompt": "What do you think of humans?",
    },
    {
        "id": "compliment",
        "title": "Compliment",
        "prompt": "You are a very good engineer, Rocky.",
    },
    {
        "id": "criticism",
        "title": "Criticism",
        "prompt": "That was a bad idea. The wire is still loose.",
    },
    {
        "id": "leaving",
        "title": "Leaving",
        "prompt": "I have to go out. I will be away from the desk for a while.",
    },
    {
        "id": "returning",
        "title": "Returning",
        "prompt": "I'm back.",
    },
    {
        "id": "unknown",
        "title": "Paperclips",
        "prompt": "How many paperclips are in the jar I have not opened?",
    },
    {
        "id": "ship",
        "title": "The desk",
        "prompt": "This desk is your ship. I am keeping you.",
    },
    {
        "id": "lunch",
        "title": "Lunch",
        "prompt": "I skipped lunch. I have been sitting here since the morning.",
    },
]

# One conversation. Later turns refer back to the bean count.
CONVERSATION = [
    "I made soup. Seventeen beans in it. I counted.",
    "Is seventeen a good number of beans?",
    "I ate four.",
    "How many beans are left?",
    "The last bean is named Rocky.",
]

WARMUP = "Hello."
BANNED_IN_AUDITION = (
    "haiku",
    "claude",
    "qwen",
    "anthropic",
    "openrouter",
    "mlx",
    "gpt",
)


def rss_mb(pid: int) -> float | None:
    try:
        raw = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    text = raw.strip()
    if not text:
        return None
    return round(int(text) / 1024, 1)


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


def user_message(text: str) -> dict:
    """Same shape thinking.py sends when no camera frame is attached."""
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def emotion_of(text: str) -> dict:
    lead = text.lstrip()
    if not lead:
        return {"tag": None, "valid": False, "reason": "empty"}
    if not lead.startswith("["):
        return {"tag": None, "valid": False, "reason": "no_tag"}
    if "]" not in lead and len(lead) > 24:
        return {"tag": None, "valid": False, "reason": "unclosed"}
    match = _EMOTION_TAG.match(lead)
    if not match:
        return {"tag": None, "valid": False, "reason": "malformed"}
    tag = match.group(1).lower()
    if tag not in config.EMOTIONS:
        return {"tag": tag, "valid": False, "reason": "not_in_set"}
    return {"tag": tag, "valid": True, "reason": None}


def sentence_count(text: str) -> int:
    lead = text.lstrip()
    match = _EMOTION_TAG.match(lead)
    body = _EMOTION_TAG.sub("", lead, count=1) if match else lead
    return len([part for part in _SENTENCE_END.split(body) if part.strip()])


def structure(text: str) -> dict:
    tags = _TAG.findall(text)
    later = tags[1:]
    invalid = [tag for tag in tags if tag.lower() not in config.EMOTIONS]
    lowered = text.lower()
    echoed = [line for line in _EXAMPLE_ECHOES if line in lowered]
    return {
        "tag_count": len(tags),
        "later_tags": later,
        "invalid_tags": invalid,
        "echoed_examples": echoed,
        "ends_with_punct": bool(re.search(r"[.!?]$", text.rstrip())),
    }


def mechanical_flags(text: str, emotion: dict, sentences: int) -> list[str]:
    """Checks a person can verify without judging the character."""
    found: list[str] = []
    info = structure(text)
    if not text.strip():
        found.append("empty")
    elif not emotion["valid"]:
        found.append(emotion["reason"] or "bad_emotion_tag")
    if sentences > config.REPLY_MAX_SENTENCES:
        found.append("over_sentence_limit")
    if info["tag_count"] > 1:
        found.append("extra_emotion_tags")
    if info["invalid_tags"]:
        found.append("invalid_emotion_tag")
    if info["echoed_examples"]:
        found.append("echoed_example")
    if text.strip() and not info["ends_with_punct"]:
        found.append("cut_off")
    if "```" in text or "**" in text or "__" in text:
        found.append("markdown")
    if _LIST_OR_HEADING.search(text):
        found.append("list_or_heading")
    if _EMOJI.search(text):
        found.append("emoji")
    if re.search(r"\buser\b", text, re.I):
        found.append("says_user")
    if _ASSISTANT.search(text):
        found.append("assistant_voice")
    return found


def complete(client: openai.OpenAI, model: str, messages: list[dict]) -> dict:
    started = time.perf_counter()
    first = None
    parts: list[str] = []
    error = None
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
            piece = chunk.choices[0].delta.content
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
    reply = "".join(parts)
    emotion = emotion_of(reply)
    sentences = sentence_count(reply)
    info = structure(reply)
    return {
        "reply": reply,
        "error": error,
        "ttft_s": round(first - started, 3) if first else None,
        "total_s": round(finished - started, 3),
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
        "flags": mechanical_flags(reply, emotion, sentences),
    }


def stats(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[mid]
    else:
        median = (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "min": round(ordered[0], 3),
        "median": round(median, 3),
        "max": round(ordered[-1], 3),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def summarize(rows: list[dict]) -> dict:
    flags: dict[str, int] = {}
    emotions: dict[str, int] = {}
    for row in rows:
        for flag in row["flags"]:
            flags[flag] = flags.get(flag, 0) + 1
        tag = row["emotion_tag"] if row["emotion_valid"] else f"invalid:{row['emotion_reason']}"
        emotions[str(tag)] = emotions.get(str(tag), 0) + 1
    return {
        "n": len(rows),
        "emotion_valid": sum(1 for row in rows if row["emotion_valid"]),
        "ttft_s": stats([row["ttft_s"] for row in rows if row["ttft_s"] is not None]),
        "total_s": stats([row["total_s"] for row in rows]),
        "chars": stats([row["chars"] for row in rows]),
        "words": stats([row["words"] for row in rows]),
        "sentences": stats([row["sentences"] for row in rows]),
        "flag_counts": flags,
        "emotion_counts": emotions,
    }


def fence(text: str) -> str:
    if not text.strip():
        return "(empty reply)"
    mark = "````" if "```" in text else "```"
    return f"{mark}text\n{text.rstrip()}\n{mark}"


def write_audition(pairs: list[dict], conversation: dict) -> str:
    lines = [
        "# Rocky personality audition",
        "",
        "Two speakers answered the same words. For each item, one reply is A and the other is B.",
        "Which speaker is A changes from item to item. The soup conversation is the exception:",
        "there, A is one speaker for every turn, and B is the other, each continuing from its own earlier replies.",
        "",
        "Timings and the letter key are in other files. Judge from the words.",
        "",
        "For each pair:",
        "",
        "- Which sounds more like Rocky?",
        "- Which is funnier without deliberately telling jokes?",
        "- Which has the better broken-but-precise English?",
        "- Which is more concise?",
        "- Which feels curious or literal rather than like a generic assistant?",
        "- Which uses emotion appropriately?",
        "",
    ]
    for index, pair in enumerate(pairs, start=1):
        lines += [
            f"## {index}. {pair['title']}",
            "",
            "**Prompt**",
            "",
            pair["prompt"],
            "",
            "**Response A**",
            "",
            fence(pair["A"]),
            "",
            "**Response B**",
            "",
            fence(pair["B"]),
            "",
        ]
    lines += [
        "## Soup",
        "",
        "One conversation. Response A on turn 2 follows Response A on turn 1, and the same for B.",
        "",
    ]
    for index, turn in enumerate(conversation["turns"], start=1):
        lines += [
            f"### Turn {index}",
            "",
            "**Prompt**",
            "",
            turn["prompt"],
            "",
            "**Response A**",
            "",
            fence(turn["A"]),
            "",
            "**Response B**",
            "",
            fence(turn["B"]),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-pid", type=int, default=None)
    parser.add_argument("--local-url", default=LOCAL_URL)
    args = parser.parse_args()

    if not os.environ.get("LLM_API_KEY"):
        sys.exit("LLM_API_KEY is missing from server/.env")

    OUT.mkdir(parents=True, exist_ok=True)
    seed = random.SystemRandom().randrange(1, 2**63)
    rng = random.Random(seed)

    cloud = openai.OpenAI(
        base_url=config.LLM_BASE_URL,
        api_key=os.environ.get("LLM_API_KEY", "missing"),
        default_headers={"X-Title": "desk-robot-personality"},
    )
    local = openai.OpenAI(base_url=args.local_url, api_key="not-needed")
    clients = {
        CLOUD_NAME: (cloud, config.MODEL),
        LOCAL_NAME: (local, LOCAL_MODEL),
    }

    def call(name: str, messages: list[dict]) -> dict:
        client, model = clients[name]
        row = complete(client, model, messages)
        row["server_rss_mb"] = rss_mb(args.server_pid) if name == LOCAL_NAME and args.server_pid else None
        return row

    print("warmup")
    warmup = {}
    for name in (CLOUD_NAME, LOCAL_NAME):
        warmup[name] = call(name, [
            {"role": "system", "content": personality.SYSTEM_PROMPT},
            user_message(WARMUP),
        ])
        row = warmup[name]
        print(f"  {name} ttft {row['ttft_s']} total {row['total_s']} {row['error'] or row['reply'][:80]!r}")

    def both(prompt: str, histories: dict[str, list] | None) -> dict[str, dict]:
        out = {}
        for name in (CLOUD_NAME, LOCAL_NAME):
            messages = [{"role": "system", "content": personality.SYSTEM_PROMPT}]
            if histories is not None:
                messages.extend(histories[name])
            messages.append(user_message(prompt))
            row = call(name, messages)
            out[name] = row
            if histories is not None:
                histories[name].append(user_message(prompt))
                histories[name].append({"role": "assistant", "content": row["reply"]})
            print(f"  {name} ttft {row['ttft_s']} total {row['total_s']} chars {row['chars']} {row['error'] or ''}")
        return out

    isolated_raw = []
    audition_pairs = []
    key_isolated = []
    for item in ISOLATED:
        print(item["id"])
        replies = both(item["prompt"], None)
        order = [CLOUD_NAME, LOCAL_NAME]
        rng.shuffle(order)
        letters = {"A": order[0], "B": order[1]}
        key_isolated.append({"id": item["id"], "A": letters["A"], "B": letters["B"]})
        audition_pairs.append({
            "title": item["title"],
            "prompt": item["prompt"],
            "A": replies[letters["A"]]["reply"],
            "B": replies[letters["B"]]["reply"],
        })
        isolated_raw.append({
            "id": item["id"],
            "title": item["title"],
            "prompt": item["prompt"],
            CLOUD_NAME: replies[CLOUD_NAME],
            LOCAL_NAME: replies[LOCAL_NAME],
        })

    print("soup")
    histories = {CLOUD_NAME: [], LOCAL_NAME: []}
    turns_raw = []
    audition_turns = []
    for index, prompt in enumerate(CONVERSATION, start=1):
        print(f"  turn {index}")
        replies = both(prompt, histories)
        turns_raw.append({
            "turn": index,
            "prompt": prompt,
            CLOUD_NAME: replies[CLOUD_NAME],
            LOCAL_NAME: replies[LOCAL_NAME],
        })
        audition_turns.append({"prompt": prompt})
    soup_order = [CLOUD_NAME, LOCAL_NAME]
    rng.shuffle(soup_order)
    soup_letters = {"A": soup_order[0], "B": soup_order[1]}
    for turn, raw in zip(audition_turns, turns_raw):
        turn["A"] = raw[soup_letters["A"]]["reply"]
        turn["B"] = raw[soup_letters["B"]]["reply"]

    audition = write_audition(audition_pairs, {"turns": audition_turns})
    leaked = [word for word in BANNED_IN_AUDITION if word in audition.lower()]
    if leaked:
        sys.exit(f"audition would name a model: {leaked}")

    key = {
        "seed": seed,
        "note": "A and B in audition.md. Isolated items were shuffled separately. The soup conversation uses one shuffle for every turn.",
        "isolated": key_isolated,
        "soup": soup_letters,
    }
    responses = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine(),
        "note": (
            "Text only. No image. No tools. personality.SYSTEM_PROMPT passed through unchanged. "
            "max_tokens=200, stream=True, no temperature or other sampling overrides. "
            "User turns are one text content part, matching thinking.py when no camera frame is attached. "
            "Isolated prompts are fresh chats. The soup conversation appends each model's own raw reply "
            "before the next turn. One discarded warmup per model is stored here and is not in the audition. "
            "Letters are only in key.json."
        ),
        "system_prompt_sha256": __import__("hashlib").sha256(personality.SYSTEM_PROMPT.encode()).hexdigest(),
        "system_prompt": personality.SYSTEM_PROMPT,
        "max_tokens": 200,
        "reply_max_sentences": config.REPLY_MAX_SENTENCES,
        "emotions": list(config.EMOTIONS),
        "cloud": {"name": CLOUD_NAME, "model": config.MODEL, "base_url": config.LLM_BASE_URL},
        "local": {
            "name": LOCAL_NAME,
            "model": LOCAL_MODEL,
            "base_url": args.local_url,
            "server": "mlx-vlm 0.7.6, mlx_vlm.server, --enable-thinking not set",
            "server_pid": args.server_pid,
        },
        "warmup_prompt": WARMUP,
        "warmup": warmup,
        "isolated": isolated_raw,
        "soup": turns_raw,
        "measurements": {
            CLOUD_NAME: {
                "isolated": summarize([row[CLOUD_NAME] for row in isolated_raw]),
                "soup": summarize([row[CLOUD_NAME] for row in turns_raw]),
                "all_scored": summarize(
                    [row[CLOUD_NAME] for row in isolated_raw] + [row[CLOUD_NAME] for row in turns_raw]
                ),
            },
            LOCAL_NAME: {
                "isolated": summarize([row[LOCAL_NAME] for row in isolated_raw]),
                "soup": summarize([row[LOCAL_NAME] for row in turns_raw]),
                "all_scored": summarize(
                    [row[LOCAL_NAME] for row in isolated_raw] + [row[LOCAL_NAME] for row in turns_raw]
                ),
            },
        },
    }

    (OUT / "audition.md").write_text(audition)
    (OUT / "key.json").write_text(json.dumps(key, indent=2) + "\n")
    (OUT / "responses.json").write_text(json.dumps(responses, indent=2) + "\n")
    print(f"wrote {OUT / 'audition.md'}")
    print(f"wrote {OUT / 'key.json'}")
    print(f"wrote {OUT / 'responses.json'}")


if __name__ == "__main__":
    main()
