"""Thinking: sends a question (and the newest camera frame) to the language
model and hands back what Rocky should say and feel.

Each model round is read to the end first. A round that calls a tool is not
spoken; the next round may call another tool or become the reply. A spoken
round is then handed back one sentence at a time.

The request is the OpenAI-style chat API, which OpenRouter, Anthropic and
OpenAI all serve, so the provider is a base URL and the model is a string
(config.LLM_BASE_URL / config.MODEL; the key is LLM_API_KEY in server/.env).

Rocky has real abilities the model can call (tool use): `look` moves the
head and comes back with a fresh camera frame from the new angle, and
`track_face` starts/stops following the human. `get_home_state` is added
when Home Assistant readings are configured. `control_light` is added when
authorised lights are configured. Those names come from the adapters, not
from this prompt. main.py supplies the functions that actually do those
things. Live house readings are not part of this prompt.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import openai

from . import config
from . import personality

_EMOTION_TAG = re.compile(r"^\s*\[(\w+)\]\s*", re.S)
# A sentence ends at . ! or ? followed by a space — but not at an ellipsis:
# "just... clear answer." is one sentence, not two.
_SENTENCE_END = re.compile(r"(?<=[!?])\s+|(?<=[^.]\.)\s+")
# Known face tags only. Other bracketed text ("[pin 3]", "[note]") is speech.
_KNOWN_EMOTION_TAG = re.compile(
    r"\[\s*(?:" + "|".join(re.escape(name) for name in sorted(config.EMOTIONS, key=len, reverse=True)) + r")\s*\]",
    re.IGNORECASE,
)


def strip_known_emotion_tags(text: str) -> str:
    """Remove Rocky's face tags so they are never spoken. Other brackets stay."""
    cleaned = _KNOWN_EMOTION_TAG.sub(" ", text)
    cleaned = re.sub(r"[^\S\n]{2,}", " ", cleaned)
    return cleaned.strip()


def limited_sentences(text: str, limit: int) -> tuple[list[str], bool]:
    """The sentences to speak, and whether the reply was cut at `limit`."""
    parts = _SENTENCE_END.split(text)
    sentences: list[str] = []
    while len(parts) > 1:
        piece = parts.pop(0).strip()
        if piece:
            sentences.append(piece)
    tail = parts[-1].strip() if parts else ""
    if tail:
        sentences.append(tail)
    if len(sentences) > limit:
        return sentences[:limit], True
    return sentences, False


# An action returns (text for the model, optional fresh camera JPEG).
Action = Callable[[dict], tuple[str, bytes | None]]

_LOOK = {
        "type": "function",
        "function": {
            "name": "look",
            "description": (
                "Move your head and look with the camera. Use it when you are "
                "explicitly asked to look, turn, inspect, or check visually, or "
                "when the answer genuinely needs visual information that is not "
                "in the current image. If that image is not enough, call it again "
                "on a later turn. Do not call it merely because a person, physical "
                "object, room, or past event was mentioned. When explicitly asked "
                "to look in a direction, always call it, even if you think your "
                "head is already there: the result tells you where your head really "
                "is and whether it is at a limit. left and right turn only; down "
                "and level nod only; center is straight ahead and level. Describe "
                "only what the image shows. Up is as high as 'level': your neck "
                "cannot tilt above eye level."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["left", "right", "down", "level", "center"],
                        "description": (
                            "left/right turn the head only (no nod); down/level nod only (no turn); "
                            "center = straight ahead and level"
                        ),
                    },
                    "degrees": {
                        "type": "number",
                        "description": "How far: 5-60 for left/right, 5-60 for down. Omit for a normal look.",
                    },
                },
                "required": ["direction"],
            },
        },
}

_TRACK = {
        "type": "function",
        "function": {
            "name": "track_face",
            "description": "Start or stop following the human's face with your head.",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
        },
}


def _spoken_name(name: str) -> str:
    return name.replace("_", " ")


def _identity_counterexamples(shown: str) -> str:
    """Short phrases that omit part of one listed name. Not a matcher."""
    words = shown.split("_")
    spoken = _spoken_name(shown)
    if len(words) < 2:
        return "A shorter or different phrase does not identify it. "
    phrases = [" ".join(words[1:])]
    if len(words) >= 3:
        skipped = " ".join((words[0], *words[2:]))
        if skipped not in phrases and skipped != spoken:
            phrases.append(skipped)
        last = words[-1]
        if last not in phrases and last != spoken:
            phrases.append(last)
    sentences = "".join(f'"{phrase}" does not identify {shown}. ' for phrase in phrases)
    return sentences + "A phrase that uses only some of the name's words does not identify it. "


def _shared_ending_sentence(names: list[str]) -> str:
    """A concrete shared ending when the catalogue has one, otherwise the general rule."""
    lists = [name.split("_") for name in names]
    best: tuple[str, ...] = ()
    longest = max(len(item) for item in lists)
    for length in range(2, longest + 1):
        counts: dict[tuple[str, ...], int] = {}
        for item in lists:
            if len(item) >= length:
                suffix = tuple(item[-length:])
                counts[suffix] = counts.get(suffix, 0) + 1
        for suffix, count in counts.items():
            if count >= 2 and len(suffix) > len(best):
                best = suffix
    if best:
        spoken = " ".join(best)
        return (
            f"A shared ending such as {spoken} does not choose among listed names that end that way. "
        )
    return "A shared ending does not choose among listed names that end the same way. "


def _device_identity(names: list[str]) -> str:
    """How exact a listed name must be. Guidance for the model, not a matcher."""
    shown = names[0]
    spoken = _spoken_name(shown)
    return (
        "Use a listed name only when the human has identified that exact device. "
        "The full listed name, spoken with spaces instead of underscores, is exact. "
        f'"{spoken}" identifies {shown}. '
        "All meaningful words from the listed name, in order, are required to identify it. "
        "Words such as the, my, please, can you, Rocky, turn, and switch may sit around that name. "
        f"{_identity_counterexamples(shown)}"
        f"{_shared_ending_sentence(names)}"
        "One listed name is not a default. "
        "Do not choose a name because it is the only one, the closest, or a partial overlap. "
        "Do not invent an alias. "
        "If several listed names fit the words they used, ask which of those names they mean. "
        "If none fit, say that device is not one you can use. Do not substitute another listed name. "
        "If the device is not identified exactly, ask in one short question and do not call this tool yet. "
        "A direct reply to that question, such as yes or the listed name, identifies the device "
        "for the request still waiting in the conversation."
    )


def _home_description(names: list[str]) -> str:
    """Behaviour for any house, plus the names this process may read."""
    available = ", ".join(names)
    return (
        f"Read one current value from {config.HUMAN_NAME}'s home. "
        "This is how you learn a live temperature, how far something has progressed, "
        "or whether a device is on. You can only read. You cannot change a device with this tool. "
        "Call this tool when your human asks what is true now for a listed name they have identified exactly. "
        "That includes whether a device is on, a temperature, progress, or any other current value. "
        "Call it again when they ask again. "
        "One semantic name per call. When more than one reading is needed, "
        "call this tool again for each name. "
        f"{_device_identity(names)} "
        "If that waiting request asks what is true now, call this tool for that name before you answer. "
        "An earlier result of this tool, an earlier control result, and anything already said "
        "in the conversation are what was true then. They are not what is true now. "
        "The device may have changed since that result. "
        "Call this tool again before you state the current value. "
        "You may say that an earlier command happened. Do not use its result as the current state. "
        "Do not call this tool merely because a place, device, or topic was mentioned, "
        "because the room seems dark, because a device is annoying, "
        "or because you were told not to change something. "
        "How something works, a usual or recommended value, a fact your human states, "
        "and a question about what was said or what you did earlier are not requests for a live reading. "
        "Pass only a semantic name from the list. Never pass a Home Assistant "
        "entity id. "
        "The result is JSON. When ok is true, say the value and unit, and do not "
        "invent a different number or anything the result does not contain. "
        "When ok is false, error says why (not configured, unavailable, "
        "invalid state, authentication failure, or connection failure). "
        "Say you could not read it. Never invent a value. "
        f"Available states: {available}."
    )


def _home_tool(names: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_home_state",
            "description": _home_description(names),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": list(names),
                        "description": (
                            "The listed name the human identified exactly. One name only. "
                            "Never a Home Assistant entity id."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    }


def _light_description(names: list[str]) -> str:
    """When to change a light, and how to read the result."""
    available = ", ".join(names)
    spoken = _spoken_name(names[0])
    return (
        f"Turn one authorised light on or off in {config.HUMAN_NAME}'s home. "
        "This is the only way to change one of those lights. "
        "Call this tool only when your human has identified one authorised light exactly and asked for on or off. "
        f'"Turn the {spoken} on", "switch the {spoken} off", and '
        f'"Can you turn the {spoken} on?" are requests to call this tool now. '
        "Politeness is part of the request. Do not ask whether to proceed. "
        "One name and one action per call. "
        "Pass only a listed name and on or off. "
        "Never pass an entity id, a service, or a domain. "
        f"{_device_identity(names)} "
        "If that waiting request is to turn that light on or off, call this tool now. "
        "Do not call get_home_state first. Do not ask whether to proceed. "
        "Do not call this tool because a light was mentioned, because the room seems dark, "
        "because a light is annoying, or because you were told not to change it. "
        '"It\'s dark in here", "the light is annoying", and "don\'t turn it on" '
        "do not call this tool. "
        "Do not ask which light to leave alone. You may offer help. An offer is not a tool call. "
        "Do not say a light is on or off now from an earlier result. "
        "Do not call this tool for every light, all lights, or a light that is not listed. "
        'One authorised light is not "every light". '
        "If your human asks for more than one device action and any part cannot be done "
        "with this tool, do not call this tool. "
        "Explain that the complete requested operation cannot be performed. "
        "Do not turn an authorised light on or off as a partial substitute, "
        "and do not ask which light they meant in order to perform the part this tool can do. "
        f"Turning the {spoken} on and the printer off is one request: do not turn the {spoken} on. "
        "A question about what is true now uses get_home_state when that reading is listed there. "
        "This tool does not answer that question. "
        "The result is JSON and describes this command only. "
        "When ok is true, outcome confirmed means the light was observed in state immediately after this command. "
        "Say that state as the result of this command. "
        "It does not mean the light is still in that state later. "
        "When ok is false, say you could not confirm it. "
        "state_unconfirmed means the command was accepted but the light was not confirmed. "
        "If state is present on a failure, say that observed state, and do not say the light changed. "
        "For every other error, say you could not do it. Never invent success. "
        f"Authorised lights: {available}."
    )


def _light_tool(names: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "control_light",
            "description": _light_description(names),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": list(names),
                        "description": (
                            "The authorised light the human identified exactly. One name only. "
                            "Never a Home Assistant entity id."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "on or off. Nothing else.",
                    },
                },
                "required": ["name", "action"],
                "additionalProperties": False,
            },
        },
    }


def _semantic_catalogue(names) -> list[str]:
    """Sorted semantic names. A dotted name is an entity id and is left out."""
    if not names:
        return []
    return sorted({
        name for name in names
        if isinstance(name, str) and name and "." not in name
    })


def build_tools(names: list[str], lights: list[str] | None = None) -> list:
    """look, track_face, then a read tool and a light tool when each catalogue has names.

    `names` is the read catalogue. `lights` is the control catalogue. An empty
    catalogue omits its tool. Neither list is read from the environment.
    """
    tools = [_LOOK, _TRACK]
    ordered = _semantic_catalogue(names)
    if ordered:
        tools.append(_home_tool(ordered))
    controlled = _semantic_catalogue(() if lights is None else lights)
    if controlled:
        tools.append(_light_tool(controlled))
    return tools


# Tests that do not pass a tool list advertise look and track_face only.
# Production passes build_tools(adapter.names, control.names) at startup.
TOOLS = build_tools(())


@dataclass
class Reply:
    text: str
    emotion: str


class Interrupted(Exception):
    """The human started talking again; drop this reply."""


def _image_part(jpeg: bytes) -> dict:
    data = base64.standard_b64encode(jpeg).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


class RobotBrain:
    def __init__(self, actions: dict[str, Action] | None = None, tools: list | None = None) -> None:
        self.client = openai.OpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=os.environ.get("LLM_API_KEY", "missing"),
            default_headers={"X-Title": "desk-robot"},  # shows up in OpenRouter's usage page; others ignore it
        )
        self.history: list[dict] = []
        self.actions = actions or {}
        self.tools = TOOLS if tools is None else tools
        self.emotion = "neutral"        # emotion of the reply in progress
        self._inflight: tuple[int, dict] | None = None  # (index, user message) being answered

    def ask(self, question: str, jpeg: bytes | None = None, camera_wanted: bool = False) -> Reply:
        """The whole reply at once. See reply() for the streaming form."""
        sentences = list(self.reply(question, jpeg, camera_wanted=camera_wanted))
        return Reply(" ".join(sentences), self.emotion)

    def reply(
        self,
        question: str,
        jpeg: bytes | None = None,
        on_emotion: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
        camera_wanted: bool = False,
    ) -> Iterator[str]:
        """Rocky's reply, one sentence at a time as the model writes it.

        on_emotion(name) is called as soon as the emotion tag at the start of
        the reply is known — before the first sentence — so the face can
        change while he's still composing. Set `cancelled`, or close the
        generator early, and the question is dropped from his memory as if it
        was never asked. `camera_wanted` with no `jpeg` means the question
        was about seeing but the camera had no fresh picture; he's told so,
        otherwise he answers from memory and claims to see things.
        """
        content: list[dict] = [{"type": "text", "text": question}]
        if jpeg is not None:
            content.append({"type": "text", "text": "(Live picture from your camera, taken just now, because the question seems to be about what you can see. If it isn't, ignore the picture.)"})
            content.append(_image_part(jpeg))
        elif camera_wanted:
            content.append({"type": "text", "text": "(Your camera has no fresh picture right now, so you cannot see anything at the moment.)"})
        user_msg = {"role": "user", "content": content}
        self.history.append(user_msg)
        mark = len(self.history) - 1
        self._inflight = (mark, user_msg)
        self.emotion = "neutral"
        spoken: list[str] = []
        error: tuple[str, str] | None = None

        gen = self._converse(on_emotion, cancelled or threading.Event())
        try:
            for sentence in gen:
                spoken.append(sentence)
                yield sentence
        except (Interrupted, GeneratorExit):
            gen.close()
            self._forget(mark, user_msg)
            raise
        except openai.APIConnectionError:
            error = ("Brain cannot reach internet. Bad bad bad.", "sad")
        except openai.AuthenticationError:
            error = ("Brain has no key. Set LLM_API_KEY, human.", "sad")
        except openai.APIStatusError as e:
            error = (f"Ow. Brain hurts. API error {e.status_code}.", "sad")
        finally:
            gen.close()

        if error is not None:
            self._forget(mark, user_msg)
            self.emotion = error[1]
            if on_emotion is not None:
                on_emotion(self.emotion)
            yield error[0]
            return

        if spoken:
            # Remember what was actually said, so he can't refer back to a
            # part that got cut.
            self.history.append({"role": "assistant", "content": f"[{self.emotion}] {' '.join(spoken)}"})
        self._inflight = None
        self._strip_images()
        self._trim_history()

    def abandon(self) -> None:
        """Forget the question being answered right now (the human kept talking)."""
        if self._inflight is not None:
            self._forget(*self._inflight)

    def _forget(self, mark: int, user_msg: dict) -> None:
        # Only cut if that question is still where we left it: a newer one may
        # already have taken its place.
        if mark < len(self.history) and self.history[mark] is user_msg:
            del self.history[mark:]
        self._inflight = None

    def _converse(self, on_emotion: Callable[[str], None] | None, cancelled: threading.Event) -> Iterator[str]:
        """One question, possibly several model calls if it uses its abilities.

        The whole round is consumed before any of it is spoken. Tool calls in
        that round run in index order and the lead-in is not spoken; a later
        round may call tools again. A round with no tool call is spoken, and
        the sentence cap is applied only then.
        """
        nudged = False
        limit = config.REPLY_MAX_SENTENCES
        for _ in range(5):
            if cancelled.is_set():
                raise Interrupted()
            stream = self.client.chat.completions.create(
                model=config.MODEL,
                max_tokens=200,  # backstop; the sentence limit does the real work
                messages=[{"role": "system", "content": personality.SYSTEM_PROMPT}, *self.history],
                tools=self.tools if self.actions else openai.NOT_GIVEN,
                stream=True,
            )
            raw: list[str] = []      # everything the model wrote this round
            calls: dict[int, dict] = {}
            try:
                for chunk in stream:
                    if cancelled.is_set():
                        raise Interrupted()
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    for tc in getattr(delta, "tool_calls", None) or []:
                        entry = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                entry["name"] = tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments
                    content = getattr(delta, "content", None)
                    if content:
                        raw.append(content)
            finally:
                stream.close()

            emotion_before = self.emotion
            body, _ = self._take_emotion_tag("".join(raw), final=True)
            # The first valid leading tag sets the face, including on a silent
            # tool round. A spoken round reports its emotion before any audio.
            if on_emotion is not None and (self.emotion != emotion_before or not calls):
                on_emotion(self.emotion)

            if calls:
                # The lead-in stays in the transcript for the model and is not spoken.
                self.history.append({
                    "role": "assistant",
                    "content": "".join(raw),
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for _, c in sorted(calls.items())
                    ],
                })
                fresh: bytes | None = None
                for _, c in sorted(calls.items()):
                    if cancelled.is_set():
                        raise Interrupted()
                    try:
                        args = json.loads(c["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    action = self.actions.get(c["name"])
                    if action is None:
                        text = f"unknown ability {c['name']}"
                    else:
                        try:
                            text, img = action(args)
                        except Exception as e:  # the robot didn't cooperate; say so
                            text, img = f"could not do that: {e}", None
                        if img:
                            fresh = img
                    print(f"  [{c['name']} {args} -> {text}]")
                    self.history.append({"role": "tool", "tool_call_id": c["id"], "content": text})
                if fresh is not None:
                    self.history.append({
                        "role": "user",
                        "content": [{"type": "text", "text": "Camera view after moving:"}, _image_part(fresh)],
                    })
                continue

            if not "".join(raw).strip() and not nudged:
                # Some models go quiet right after using an ability. Ask once.
                nudged = True
                self.history.append({"role": "user", "content": "(Tell your human what you just did, in one short line.)"})
                continue
            if nudged and self.history and self.history[-1].get("role") == "user":
                self.history.pop()  # don't keep the nudge in the transcript
            spoken_text = strip_known_emotion_tags(body)
            sentences, trimmed = limited_sentences(spoken_text, limit)
            if trimmed:
                print(f"  (trimmed reply to {limit} sentences)")
            if sentences:
                yield from sentences
            else:
                self.emotion = "thinking"
                if on_emotion is not None:
                    on_emotion(self.emotion)
                yield "Hmm. Words did not come. Ask again."
            return

        self.emotion = "thinking"
        if on_emotion is not None:
            on_emotion(self.emotion)
        yield "Too many things at once. Ask again, human."

    def _take_emotion_tag(self, buf: str, final: bool) -> tuple[str, bool]:
        """Look for "[happy] " at the start of the reply. Returns (text with the
        tag removed, decided). Not decided means: need more text to know."""
        lead = buf.lstrip()
        if not lead:
            return buf, final
        if not lead.startswith("["):
            return buf, True
        if "]" in lead:
            m = _EMOTION_TAG.match(lead)
            if m:
                candidate = m.group(1).lower()
                if candidate in config.EMOTIONS:
                    self.emotion = candidate
                return _EMOTION_TAG.sub("", lead, count=1), True
            return buf, True
        if final or len(lead) > 24:
            return buf, True  # a "[" with no "]" in sight: not a tag
        return buf, False

    def _strip_images(self) -> None:
        """Replace old camera frames with a note. Keeping every image in the
        history would make each later question cost far more."""
        for m in self.history:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                parts = [p for p in m["content"] if p.get("type") == "text"]
                if len(parts) != len(m["content"]):
                    parts.append({"type": "text", "text": "(a camera image was attached here)"})
                m["content"] = parts

    def _trim_history(self) -> None:
        max_msgs = config.MAX_HISTORY_TURNS * 2
        if len(self.history) > max_msgs:
            cut = len(self.history) - max_msgs
            # Never start the history on a tool result: back up to a user turn.
            while cut < len(self.history) and self.history[cut].get("role") != "user":
                cut += 1
            del self.history[:cut]
