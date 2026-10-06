"""Fixed lines for cloud-vs-local comparisons.

Spoken lines are the example replies and canned lines in personality.py, with
the emotion tag removed the way the brain does before it calls the voice.
The Haiku prompts are the matching questions, with no picture attached.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from brain.personality import LINES, SYSTEM_PROMPT  # noqa: E402

_EXAMPLE = re.compile(r"^\[(\w+)\]\s*(.+)$")
# Same cleanup mouth.clean_for_tts applies, kept here so a voice test does not
# need to import the speech stack.
_MARKUP = re.compile(r"[*_`#~<>\[\]{}|\\]")


def spoken(text: str) -> str:
    text = _MARKUP.sub("", text)
    text = re.sub(r"([!?.,])\1+", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def voice_lines() -> list[dict]:
    """What Fish is asked to say today."""
    lines = []
    for raw in SYSTEM_PROMPT.splitlines():
        raw = raw.strip()
        match = _EXAMPLE.match(raw)
        if not match:
            continue
        emotion, words = match.group(1), spoken(match.group(2))
        lines.append({"id": f"example-{emotion}-{len(lines) + 1:02d}", "kind": "example", "text": words})
    for key, words in LINES.items():
        lines.append({"id": f"canned-{key}", "kind": "canned", "text": spoken(words)})
    return lines


# Questions that produced those kinds of replies. No image: this is the
# text baseline. Vision is a later test.
HAIKU_PROMPTS = [
    "I finished a new circuit board. It is sitting on the desk.",
    "The error says the pin number is wrong. What now?",
    "I have been awake since five.",
    "It works!",
    "There is a solder bridge on pin three.",
    "What is a weekend?",
    "I made coffee.",
    "What am I holding?",
]
