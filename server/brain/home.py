"""Home Assistant for the brain: which readings and lights Rocky may use right now, and the tools that say so.

  get_home_state   read one listed name (brain/home_assistant.py)
  control_light    turn one listed light on or off, then check it (brain/home_control.py)

Settings are the dashboard's Home Assistant card: home_assistant.enabled / url / readings / lights in
server/settings.json, applied by main.apply_settings() at start and whenever the launcher saves. The token is not a
setting: it lives in server/home_assistant.token (gitignored, owner-only), written and cleared by the launcher, read
here, and never returned to the page, printed, or put in front of the model.

Both actions are always registered with the brain and read the current catalogues when they run, so a name removed
in the dashboard (or Home Assistant switched off) is refused at once, even if an older message mentions it. tools()
advertises only what is allowed now; system_prompt() adds the home rules only while a home tool is offered.

Standard library only (the launcher imports this for the token file).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from . import config
from .home_assistant import HomeAssistant, readings_from_text, state_for_tool
from .home_control import HomeControl, controls_from_text, mismatches, set_light_for_tool

TOKEN_PATH = Path(__file__).resolve().parent.parent / "home_assistant.token"
_MAX_TOKEN = 4096
_TOKEN_CHARS = re.compile(r"^[\x21-\x7e]+$")   # printable ASCII, no spaces


# ── The token file ───────────────────────────────────────────────────────────

def load_token(path: Path | None = None) -> str:
    """The saved token, or "" if there is none or it can't be read."""
    try:
        return (path or TOKEN_PATH).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def token_set(path: Path | None = None) -> bool:
    return bool(load_token(path))


def save_token(token: object, path: Path | None = None) -> None:
    """Replace the token: written to a private temp file, then renamed over the old one. Raises ValueError (which
    never contains the token) if it doesn't look like one."""
    text = token.strip() if isinstance(token, str) else ""
    if not text:
        raise ValueError("paste a Home Assistant long-lived access token")
    if len(text) > _MAX_TOKEN or not _TOKEN_CHARS.fullmatch(text):
        raise ValueError("that doesn't look like a Home Assistant token (one line, no spaces)")
    target = path or TOKEN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".home_assistant-", suffix=".token")   # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_token(path: Path | None = None) -> bool:
    """Delete the token. True if there was one."""
    try:
        (path or TOKEN_PATH).unlink()
        return True
    except FileNotFoundError:
        return False


def ignored_env_keys(env: Mapping[str, str]) -> list[str]:
    """V1's Home Assistant keys found in server/.env or the environment. V2 doesn't read them; the names (never the
    values) go in one log line pointing at the dashboard."""
    return sorted(k for k, v in env.items()
                  if isinstance(k, str) and (k.startswith("HOME_ASSISTANT_") or k.startswith("HA_"))
                  and isinstance(v, str) and v.strip())


# ── The controller ───────────────────────────────────────────────────────────

class Home:
    def __init__(self, fetch=None, post=None, pause=None) -> None:
        # fetch / post / pause are the adapters' transports; tests swap in fakes.
        self._fetch, self._post, self._pause = fetch, post, pause
        self.lock = threading.Lock()
        self.enabled = False
        self._token_set = False
        self._reader = HomeAssistant("", "", {})
        self._control = HomeControl("", "", {})
        self._shown: tuple | None = None

    def configure(self, section: Mapping, token: str) -> None:
        """Apply settings["home_assistant"] and the token. Builds fresh adapters and swaps them in together."""
        enabled = bool(section.get("enabled"))
        readings, lights = {}, {}
        if enabled:
            try:
                readings = readings_from_text(section.get("readings", ""))
            except ValueError as e:
                print(f"home assistant readings ignored: {e}")
            try:
                lights = controls_from_text(section.get("lights", ""))
            except ValueError as e:
                print(f"home assistant lights ignored: {e}")
        url = section.get("url", "") if enabled else ""
        key = token if enabled else ""
        reader = HomeAssistant(url, key, readings, fetch=self._fetch)
        control = HomeControl(url, key, lights, post=self._post, fetch=self._fetch, pause=self._pause)
        with self.lock:
            self.enabled = enabled
            self._token_set = bool(token)
            self._reader, self._control = reader, control
        shown = (enabled, tuple(reader.names), tuple(control.names), bool(token), bool(url))
        if shown != self._shown:
            self._shown = shown
            self._report(readings, lights, bool(token), bool(url))

    def _report(self, readings, lights, has_token: bool, has_url: bool) -> None:
        """One log line of names only: no URL, no entity ids, no token."""
        if not self.enabled:
            print("home assistant: off")
            return
        print(f"home readings: {', '.join(readings) or 'none'}")
        print(f"home lights: {', '.join(lights) or 'none'}")
        if not has_url or not has_token:
            print("home assistant: no " + ("URL" if not has_url else "token") + " yet (dashboard → Home Assistant)")
        for name in mismatches(readings, lights):
            print(f"home assistant: {name} reads one entity and switches another")

    def adapters(self) -> tuple[HomeAssistant, HomeControl]:
        with self.lock:
            return self._reader, self._control

    def status(self) -> dict:
        """For the dashboard: names only."""
        reader, control = self.adapters()
        return {"on": self.enabled, "readings": reader.names, "lights": control.names, "token_set": self._token_set}

    # ── the abilities (called from the brain's worker thread) ──

    def get_home_state(self, args: dict) -> tuple[str, None]:
        return state_for_tool(self.adapters()[0], args)

    def control_light(self, args: dict) -> tuple[str, None]:
        return set_light_for_tool(self.adapters()[1], args)

    def system_prompt(self, base: str) -> str:
        """The character's prompt, plus the home rules while a home tool is offered."""
        if not tools(self) or HOME_RULES_HEADING in base:
            return base
        prompt = base
        if _FACTS_ANCHOR in prompt:
            prompt = prompt.replace(_FACTS_ANCHOR, _FACTS_ANCHOR + " " + LIVE_READING_SENTENCE, 1)
        if _RULES_ANCHOR in prompt:
            return prompt.replace(_RULES_ANCHOR, HOME_RULES + "\n" + _RULES_ANCHOR, 1)
        return prompt.rstrip() + "\n\n" + HOME_RULES


# ── What the model sees (from V1's thinking.py, unchanged) ───────────────────

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


def build_tools(names, lights=None) -> list:
    """A read tool and a light tool when each catalogue has names. An empty catalogue omits its tool."""
    tools_out = []
    ordered = _semantic_catalogue(names)
    if ordered:
        tools_out.append(_home_tool(ordered))
    controlled = _semantic_catalogue(() if lights is None else lights)
    if controlled:
        tools_out.append(_light_tool(controlled))
    return tools_out


def tools(home: Home) -> list:
    """The brain's extra tools right now. Switched off: none."""
    if not home.enabled:
        return []
    reader, control = home.adapters()
    return build_tools(reader.names, control.names)


# The home rules from V1's personality.py, unchanged. Added to whichever character is active, only while a home
# tool is offered: the sentence goes after "from what you know.", the block before the emotion-tag rule.
LIVE_READING_SENTENCE = "A live reading from the home is not one of those facts."
HOME_RULES_HEADING = "Home state (history is not the present):"
HOME_RULES = f"""{HOME_RULES_HEADING}
- Conversation history says what happened. `get_home_state` says what is true now.
- If your human asks the current state of one configured item they have \
named exactly, call `get_home_state` before you answer, even if you read it, \
changed it, or said it a moment ago. Call it again every time they ask. Do \
this only when that ability is available.
- A shorter name is not exact. Ask which item they mean and do not call yet. \
One configured item is not a default.
- An exact request to turn a light on or off calls `control_light` now, with \
no reading first, when that ability is available.
- What you just did, a fact they stated, darkness, annoyance, and being told \
not to change something do not call `get_home_state` or `control_light`.
"""
_FACTS_ANCHOR = "Math, facts, advice, chat: just answer, from what you know."
_RULES_ANCHOR = "Every reply MUST start with an emotion tag"


controller = Home()
ACTIONS = {
    "get_home_state": controller.get_home_state,
    "control_light": controller.control_light,
}
