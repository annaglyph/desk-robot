"""Turn authorised lights on or off.

This is not a Home Assistant service caller. A HA_CONTROL_* line permits
one semantic light, and only the actions on and off. The HA_* catalogue
is not consulted. A readable name grants nothing here.

The model passes a semantic name and on or off. This module maps those
onto one configured entity and the matching turn_on or turn_off service.
HTTP 200 from that service means the request was accepted. It does not
mean the physical state is confirmed. Confirmation is a later GET of the
entity state.

From the server/ directory this module is not a command. The read-only
smoke test stays in brain.home_assistant.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import certifi

from . import home_assistant

MAX_CONTROLS = 8
# Pauses before the second and third state read. This is the whole retry.
VERIFY_PAUSES = (0.4, 0.8)
VERIFY_TIMEOUT = 2.0
_MAX_NAME = 64
_CAPABILITY = "light:"
_ALLOWED_DOMAINS = frozenset({"light", "switch"})
_ACTIONS = frozenset({"on", "off"})
_SERVICE = {"on": "turn_on", "off": "turn_off"}

ERRORS = frozenset({
    "not_configured",
    "unknown_name",
    "unsupported_action",
    "invalid_configuration",
    "entity_not_found",
    "entity_unavailable",
    "authentication_failed",
    "connection_failed",
    "timeout",
    "request_failed",
    "state_unconfirmed",
})

Post = Callable[[str, str, bytes, float], tuple[int, bytes]]
Pause = Callable[[float], None]


@dataclass(frozen=True)
class Light:
    """One controllable light. The entity id stays inside this process."""

    entity_id: str


def _shown(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return name[:80]


def _failure(name: object, error: str, action: object = None, state: str | None = None) -> dict:
    """A failure result. state is kept only for an unconfirmed on/off reading."""
    if error not in ERRORS:
        error = "request_failed"
    result = {"ok": False, "name": _shown(name), "error": error}
    if isinstance(action, str) and action:
        result["action"] = action[:80]
    if error == "state_unconfirmed" and state in ("on", "off"):
        result["state"] = state
    return result


def _confirmed(name: str, action: str) -> dict:
    return {
        "ok": True,
        "name": name,
        "action": action,
        "outcome": "confirmed",
        "state": action,
    }


def _seal(result: dict) -> dict:
    """ok is true only when the observed on/off state equals the action.

    Extra keys are dropped, so a service payload cannot ride along.
    """
    name = _shown(result.get("name"))
    action = result.get("action")
    action_out = action[:80] if isinstance(action, str) and action else None
    state = result.get("state")
    if state not in ("on", "off"):
        state = None
    if (
        result.get("ok") is True
        and result.get("outcome") == "confirmed"
        and action_out in _ACTIONS
        and state == action_out
    ):
        return {
            "ok": True,
            "name": name,
            "action": action_out,
            "outcome": "confirmed",
            "state": state,
        }
    error = result.get("error")
    if error not in ERRORS:
        error = "request_failed"
    sealed = {"ok": False, "name": name, "error": error}
    if action_out is not None:
        sealed["action"] = action_out
    if error == "state_unconfirmed" and state in ("on", "off"):
        sealed["state"] = state
    return sealed


def _timed_out(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)


def _allowed_entity(entity: str) -> bool:
    if not entity or not home_assistant._ENTITY_ID.fullmatch(entity):
        return False
    domain, _, rest = entity.partition(".")
    return bool(rest) and domain in _ALLOWED_DOMAINS


def _semantic_name(key: str) -> str | None:
    """office_desk_lamp from HA_CONTROL_OFFICE_DESK_LAMP, or None."""
    prefix = "HA_CONTROL_"
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix):]
    if not rest or not home_assistant._HA_KEY.fullmatch("HA_" + rest):
        return None
    name = rest.lower()
    if len(name) > _MAX_NAME:
        return None
    return name


def _reading_entity(value: str) -> str | None:
    """Entity id from a HA_* value, for the mismatch warning only."""
    text = value.strip()
    if text.startswith("text:"):
        entity = text[5:].strip()
    elif text.startswith("number:"):
        entity = text[7:].strip()
    elif ":" in text:
        return None
    else:
        entity = text
    if entity and home_assistant._ENTITY_ID.fullmatch(entity):
        return entity
    return None


def _parse_control(value: str) -> tuple[str, Light | None]:
    """('ok', light), ('blank', None), or ('bad', None).

    The capability word is required. A bare entity id is not control.
    The word is not a Home Assistant domain: only light is accepted, and
    the entity itself must be a light or a switch.
    """
    text = value.strip()
    if not text:
        return "blank", None
    if not text.startswith(_CAPABILITY):
        return "bad", None
    entity = text[len(_CAPABILITY):].strip()
    if not _allowed_entity(entity):
        return "bad", None
    return "ok", Light(entity)


def _warn_bad_key(key: str) -> str:
    return f"Ignoring {key}. A controllable light is HA_CONTROL_ followed by uppercase words."


def _warn_bad_value(key: str) -> str:
    return f"Ignoring {key}. It is not an authorised light."


def _warn_cap(key: str) -> str:
    return f"Ignoring {key}. At most {MAX_CONTROLS} controllable lights are allowed."


def _warn_mismatch(control_key: str, read_key: str) -> str:
    return f"{control_key} and {read_key} do not refer to the same entity."


def controls_from_env(env: Mapping[str, str]) -> tuple[dict[str, Light], list[str]]:
    """Semantic lights from HA_CONTROL_* keys, and warnings that name keys only.

    HA_* keys are ignored. Blank values are unset. At most MAX_CONTROLS
    lights are kept, in alphabetical order. Warning text never includes a value.
    """
    warnings: list[str] = []
    valid: list[tuple[str, str, Light]] = []
    for key in sorted(k for k in env if isinstance(k, str)):
        raw = env.get(key, "")
        if not key.startswith("HA_CONTROL_"):
            continue
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = _semantic_name(key)
        if name is None:
            warnings.append(_warn_bad_key(key))
            continue
        status, light = _parse_control(raw)
        if status != "ok" or light is None:
            warnings.append(_warn_bad_value(key))
            continue
        read_key = "HA_" + name.upper()
        read_raw = env.get(read_key, "")
        if isinstance(read_raw, str) and read_raw.strip():
            read_entity = _reading_entity(read_raw)
            if read_entity is not None and read_entity != light.entity_id:
                warnings.append(_warn_mismatch(key, read_key))
        valid.append((name, key, light))
    valid.sort(key=lambda item: item[0])
    kept = valid[:MAX_CONTROLS]
    for _name, key, _light in valid[MAX_CONTROLS:]:
        warnings.append(_warn_cap(key))
    return {name: light for name, _key, light in kept}, warnings


def urllib_post(url: str, token: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST one URL. The token is a header. The response body is discarded.

    An HTTP 200 means the service request was accepted. It does not mean
    the requested physical state has been confirmed. The body lists states
    that changed while the service ran, including unrelated ones, and it
    may be empty. It is not a confirmation, so it is not returned.
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "desk-robot",
        },
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read(home_assistant._MAX_BODY + 1)
            return resp.status, b""
    except urllib.error.HTTPError as exc:
        try:
            exc.read(home_assistant._MAX_BODY + 1)
        finally:
            exc.close()
        return exc.code, b""


def _classify(status: int, body: bytes) -> str:
    """on, off, unavailable, missing, or failed. Attributes are ignored."""
    if status == 404:
        return "missing"
    if status != 200:
        return "failed"
    if len(body) > home_assistant._MAX_BODY:
        return "failed"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "failed"
    if not isinstance(payload, dict):
        return "failed"
    state = payload.get("state")
    if not isinstance(state, str):
        return "failed"
    folded = state.strip().casefold()
    if folded in ("on", "off", "unavailable"):
        return folded
    return "failed"


class HomeControl:
    """POST turn_on or turn_off for names in the control allow-list, then read the state."""

    def __init__(
        self,
        url: str,
        token: str,
        lights: Mapping[str, Light],
        *,
        timeout: float = home_assistant.TIMEOUT,
        verify_timeout: float = VERIFY_TIMEOUT,
        post: Post | None = None,
        fetch: home_assistant.Fetch | None = None,
        pause: Pause | None = None,
    ) -> None:
        self._token = token.strip() if isinstance(token, str) else ""
        self._url = home_assistant._remember_url(url, self._token)
        self._lights = {
            name: light
            for name, light in lights.items()
            if isinstance(name, str) and name and "." not in name
        }
        self._timeout = timeout
        self._verify_timeout = verify_timeout
        self._post = post or urllib_post
        self._fetch = fetch or home_assistant.urllib_get
        self._pause = pause or time.sleep

    def __repr__(self) -> str:
        return f"HomeControl(url={self._url!r})"

    @property
    def names(self) -> list[str]:
        """Semantic lights Rocky may change, in catalogue order."""
        return list(self._lights)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HomeControl":
        """Settings from the environment. A blank value means unset.

        With no mapping, brain.config loads server/.env first, then the
        process environment is read. An explicit mapping, including {},
        is used alone and does not touch that file. Warnings name rejected
        keys and are written to stderr. They do not include values.
        """
        if environ is None:
            from . import config  # loads server/.env into os.environ
            del config
            env = os.environ
        else:
            env = environ
        lights, warnings = controls_from_env(env)
        for line in warnings:
            print(line, file=sys.stderr)
        return cls(
            env.get("HOME_ASSISTANT_URL", ""),
            env.get("HOME_ASSISTANT_TOKEN", ""),
            lights,
        )

    def set_light(self, name: object, action: object) -> dict:
        """One on or off. Always a dict. Does not raise into the caller."""
        try:
            return _seal(self._set_light(name, action))
        except Exception:
            return _seal(_failure(name, "request_failed", action))

    def _set_light(self, name: object, action: object) -> dict:
        if not isinstance(name, str) or "." in name or name not in self._lights:
            return _failure(name, "unknown_name", action)
        if action not in _ACTIONS:
            return _failure(name, "unsupported_action", action)
        light = self._lights[name]
        entity_id = light.entity_id.strip()
        if not _allowed_entity(entity_id):
            return _failure(name, "invalid_configuration", action)
        if not home_assistant._origin_ok(self._url, self._token):
            return _failure(name, "not_configured", action)
        domain = entity_id.split(".", 1)[0]
        url = self._url.rstrip("/") + "/api/services/" + domain + "/" + _SERVICE[action]
        if self._token in url:
            return _failure(name, "not_configured", action)
        body = json.dumps({"entity_id": entity_id}).encode("utf-8")
        try:
            status, accepted_body = self._post(url, self._token, body, self._timeout)
        except TimeoutError:
            return _failure(name, "timeout", action)
        except urllib.error.HTTPError as exc:
            status, accepted_body = exc.code, b""
        except urllib.error.URLError as exc:
            error = "timeout" if _timed_out(exc) else "connection_failed"
            return _failure(name, error, action)
        except OSError:
            return _failure(name, "connection_failed", action)
        # HTTP 200 means the service request was accepted. It does not mean
        # the requested physical state has been confirmed. accepted_body is
        # ignored, including when it claims the light already changed.
        del accepted_body
        if status in (401, 403):
            return _failure(name, "authentication_failed", action)
        if status != 200:
            return _failure(name, "request_failed", action)
        return self._verify(name, action, entity_id)

    def _verify(self, name: str, action: str, entity_id: str) -> dict:
        """GET the state immediately, then twice more after the fixed pauses."""
        observed = "failed"
        for attempt in range(3):
            if attempt:
                self._pause(VERIFY_PAUSES[attempt - 1])
            observed = self._observe(entity_id)
            if observed == "missing":
                return _failure(name, "entity_not_found", action)
            if observed == action:
                return _confirmed(name, action)
        if observed == "unavailable":
            return _failure(name, "entity_unavailable", action)
        if observed in _ACTIONS:
            return _failure(name, "state_unconfirmed", action, observed)
        return _failure(name, "state_unconfirmed", action)

    def _observe(self, entity_id: str) -> str:
        url = home_assistant._state_url(self._url, entity_id)
        if self._token in url:
            return "failed"
        try:
            status, body = self._fetch(url, self._token, self._verify_timeout)
        except TimeoutError:
            return "failed"
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
        except urllib.error.URLError:
            return "failed"
        except OSError:
            return "failed"
        return _classify(status, body)


def set_light_for_tool(client: HomeControl, args: object) -> tuple[str, None]:
    """JSON text of one adapter result, and no camera image.

    Only the semantic name and the action are read. Any other field is
    ignored. A failure is that error object, never an exception string.
    """
    name = args.get("name") if isinstance(args, dict) else None
    action = args.get("action") if isinstance(args, dict) else None
    try:
        result = client.set_light(name, action)
        if not isinstance(result, dict):
            result = _failure(name, "request_failed", action)
        text = json.dumps(_seal(result), ensure_ascii=False)
    except Exception:
        text = json.dumps(_seal(_failure(name, "request_failed", action)), ensure_ascii=False)
    return text, None
