"""Read-only Home Assistant states.

Asks Home Assistant for one configured entity and always returns a
structured result. Rocky's get_home_state ability is a thin call to
get_state (see state_for_tool). Nothing here can call a service or
change a device. An HA_* entry permits a read only.

Configuration is server/.env (see .env.example). HOME_ASSISTANT_URL and
HOME_ASSISTANT_TOKEN are the hub. Each HA_<NAME> line is one semantic
name Rocky may read. A name that was not configured returns unknown_name.
A hub that is not configured returns not_configured.

From the server/ directory:

  python -m brain.home_assistant pool_temperature office_temperature
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import certifi

# How many HA_* readings one process will offer the model.
MAX_READINGS = 32
# HA_POOL_TEMPERATURE -> pool_temperature. Uppercase words, no empty gaps.
_HA_KEY = re.compile(r"^HA_[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
_MAX_NAME = 64

ERRORS = frozenset({
    "not_configured",
    "unknown_name",
    "entity_not_configured",
    "entity_not_found",
    "entity_unavailable",
    "invalid_state",
    "authentication_failed",
    "connection_failed",
    "timeout",
    "request_failed",
})

TIMEOUT = 5.0
_MAX_BODY = 65_536
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_ENTITY_ID = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")

Fetch = Callable[[str, str, float], tuple[int, bytes]]


@dataclass(frozen=True)
class Reading:
    """One readable name. An empty entity_id is not configured.

    numeric is True unless the setting explicitly says text:. There is
    no write permission on this object.
    """

    entity_id: str
    numeric: bool


def _fail(name: object, error: str) -> dict:
    """A failure result. Never blank: ok is false and error is a known code."""
    if error not in ERRORS:
        error = "invalid_state"
    shown = name[:80] if isinstance(name, str) else ""
    return {"ok": False, "name": shown, "error": error}


def _ok(name: str, value: object, unit: str | None) -> dict:
    """A success result. ok true only with a real value, never a blank one."""
    if isinstance(value, bool) or value is None or value == "":
        return _fail(name, "invalid_state")
    if isinstance(value, str) and not value.strip():
        return _fail(name, "invalid_state")
    if not isinstance(value, (str, int, float)):
        return _fail(name, "invalid_state")
    if isinstance(value, float) and not math.isfinite(value):
        return _fail(name, "invalid_state")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        unit = None
    return {"ok": True, "name": name, "value": value, "unit": unit}


def _remember_url(url: object, token: str) -> str:
    """Keep an origin only. Drop anything that carries credentials."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url or (token and token in url):
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    return url.rstrip("/")


def _origin_ok(url: str, token: str) -> bool:
    """True when url is an http(s) origin and does not carry the token."""
    if not url or not token:
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.path not in ("", "/"):
        return False
    return True


def _state_url(url: str, entity_id: str) -> str:
    quoted = urllib.parse.quote(entity_id, safe=".")
    return url.rstrip("/") + "/api/states/" + quoted


def _unit(payload: dict) -> str | None:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get("unit_of_measurement")
    if not isinstance(raw, str):
        return None
    unit = raw.strip()
    if not unit or len(unit) > 32 or any(ord(ch) < 32 for ch in unit):
        return None
    return unit


def _number(state: str) -> int | float | None:
    """A plain decimal, or None. No hex, no exponents, no trailing text."""
    if not _NUMBER.fullmatch(state):
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    if value.is_integer() and abs(value) <= 2**53:
        return int(value)
    return value


def _interpret(name: str, body: bytes, numeric: bool) -> dict:
    if len(body) > _MAX_BODY:
        return _fail(name, "invalid_state")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail(name, "invalid_state")
    if not isinstance(payload, dict) or "state" not in payload:
        return _fail(name, "invalid_state")
    state = payload.get("state")
    if not isinstance(state, str):
        return _fail(name, "invalid_state")
    state = state.strip()
    if not state:
        return _fail(name, "invalid_state")
    folded = state.casefold()
    if folded == "unavailable":
        return _fail(name, "entity_unavailable")
    if folded == "unknown":
        return _fail(name, "invalid_state")
    unit = _unit(payload)
    if numeric:
        value = _number(state)
        if value is None:
            return _fail(name, "invalid_state")
        return _ok(name, value, unit)
    if len(state) > 200:
        return _fail(name, "invalid_state")
    return _ok(name, state, unit)


def urllib_get(url: str, token: str, timeout: float) -> tuple[int, bytes]:
    """GET one URL. The token is a header, never part of the URL.

    An HTTP error status is returned as a code. The body of an error is
    discarded so it cannot become the result. Other failures propagate
    and are mapped by HomeAssistant.get_state.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "desk-robot",
        },
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        try:
            exc.read(_MAX_BODY + 1)
        finally:
            exc.close()
        return exc.code, b""


def _timeout(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)


def _semantic_name(key: str) -> str | None:
    """pool_temperature from HA_POOL_TEMPERATURE, or None when the key is unfit."""
    if not _HA_KEY.fullmatch(key):
        return None
    name = key[3:].lower()
    if len(name) > _MAX_NAME:
        return None
    return name


def _parse_reading(value: str) -> tuple[str, Reading | None]:
    """('ok', reading), ('blank', None), or ('bad', None).

    A bare entity id is numeric. number: is the same. text: is a short
    string. The type is never guessed from the entity id's domain.
    """
    text = value.strip()
    if not text:
        return "blank", None
    numeric = True
    entity = text
    if text.startswith("text:"):
        numeric = False
        entity = text[5:].strip()
    elif text.startswith("number:"):
        entity = text[7:].strip()
    elif ":" in text:
        return "bad", None
    if not entity or not _ENTITY_ID.fullmatch(entity):
        return "bad", None
    return "ok", Reading(entity, numeric)


def _warn_obsolete(key: str) -> str:
    return (
        f"Ignoring {key}. Home readings use HA_<NAME> in server/.env "
        "(see .env.example)."
    )


def _warn_bad_key(key: str) -> str:
    return f"Ignoring {key}. A Home reading name is HA_ followed by uppercase words."


def _warn_bad_value(key: str) -> str:
    return f"Ignoring {key}. It is not a readable Home Assistant entity."


def _warn_cap(key: str) -> str:
    return f"Ignoring {key}. At most {MAX_READINGS} Home Assistant readings are allowed."


def readings_from_env(env: Mapping[str, str]) -> tuple[dict[str, Reading], list[str]]:
    """Semantic names from HA_* keys, and warnings that name keys only.

    The mapping is used alone. Blank values are unset. Old
    HOME_ASSISTANT_ENTITY_* keys are ignored. HA_CONTROL_* keys are not
    readings. At most MAX_READINGS names are kept, in alphabetical order.
    Warning text never includes a value.
    """
    warnings: list[str] = []
    valid: list[tuple[str, str, Reading]] = []
    for key in sorted(k for k in env if isinstance(k, str)):
        raw = env.get(key, "")
        if key.startswith("HOME_ASSISTANT_ENTITY_"):
            if isinstance(raw, str) and raw.strip():
                warnings.append(_warn_obsolete(key))
            continue
        if key.startswith("HA_CONTROL_"):
            continue
        if not key.startswith("HA_"):
            continue
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = _semantic_name(key)
        if name is None:
            warnings.append(_warn_bad_key(key))
            continue
        status, reading = _parse_reading(raw)
        if status != "ok" or reading is None:
            warnings.append(_warn_bad_value(key))
            continue
        valid.append((name, key, reading))
    valid.sort(key=lambda item: item[0])
    kept = valid[:MAX_READINGS]
    for _name, key, _reading in valid[MAX_READINGS:]:
        warnings.append(_warn_cap(key))
    return {name: reading for name, _key, reading in kept}, warnings


class HomeAssistant:
    """GET /api/states/<entity_id> for names in the allow-list."""

    def __init__(
        self,
        url: str,
        token: str,
        entities: Mapping[str, Reading],
        *,
        timeout: float = TIMEOUT,
        fetch: Fetch | None = None,
    ) -> None:
        self._token = token.strip() if isinstance(token, str) else ""
        self._url = _remember_url(url, self._token)
        self._entities = dict(entities)
        self._timeout = timeout
        self._fetch = fetch or urllib_get

    def __repr__(self) -> str:
        return f"HomeAssistant(url={self._url!r})"

    @property
    def names(self) -> list[str]:
        """Semantic names Rocky may read, in catalogue order."""
        return list(self._entities)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HomeAssistant":
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
        entities, warnings = readings_from_env(env)
        for line in warnings:
            print(line, file=sys.stderr)
        return cls(
            env.get("HOME_ASSISTANT_URL", ""),
            env.get("HOME_ASSISTANT_TOKEN", ""),
            entities,
        )

    def get_state(self, name: object) -> dict:
        """One reading. Always a dict with ok, and either value or error."""
        if not isinstance(name, str) or name not in self._entities:
            return _fail(name, "unknown_name")
        if not _origin_ok(self._url, self._token):
            return _fail(name, "not_configured")
        reading = self._entities[name]
        entity_id = reading.entity_id.strip()
        if not entity_id or not _ENTITY_ID.fullmatch(entity_id):
            return _fail(name, "entity_not_configured")
        url = _state_url(self._url, entity_id)
        if self._token in url:
            return _fail(name, "not_configured")
        try:
            status, body = self._fetch(url, self._token, self._timeout)
        except TimeoutError:
            return _fail(name, "timeout")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
        except urllib.error.URLError as exc:
            return _fail(name, "timeout" if _timeout(exc) else "connection_failed")
        except OSError:
            return _fail(name, "connection_failed")
        if status in (401, 403):
            return _fail(name, "authentication_failed")
        if status == 404:
            return _fail(name, "entity_not_found")
        if status != 200:
            return _fail(name, "request_failed")
        return _interpret(name, body, reading.numeric)


def state_for_tool(client: HomeAssistant, args: object) -> tuple[str, None]:
    """JSON text of one adapter result, and no camera image.

    The model passes one semantic name. This does not interpret Home
    Assistant itself: get_state does, and its dict is serialized as-is.
    A failure is that error object, never a blank string and never a
    guessed reading.
    """
    name = args.get("name") if isinstance(args, dict) else None
    return json.dumps(client.get_state(name), ensure_ascii=False), None


def _hints(results: list[dict], url: str) -> None:
    """Tell a person which settings are missing. Never the token."""
    if any(item.get("error") == "not_configured" for item in results):
        print(
            "Home Assistant is not configured. Set HOME_ASSISTANT_URL and "
            "HOME_ASSISTANT_TOKEN in server/.env (see .env.example).",
            file=sys.stderr,
        )
    for item in results:
        if item.get("error") != "entity_not_configured":
            continue
        name = item.get("name") or "that name"
        if isinstance(name, str) and name:
            env_name = "HA_" + name.upper()
        else:
            env_name = "HA_<NAME>"
        print(
            f"{name} has no entity id. Set {env_name} in server/.env.",
            file=sys.stderr,
        )
    if any(item.get("error") == "authentication_failed" for item in results):
        print(
            "Home Assistant rejected HOME_ASSISTANT_TOKEN (HTTP 401 or 403).",
            file=sys.stderr,
        )
    if any(item.get("error") == "connection_failed" for item in results):
        print(f"Could not reach {url}.", file=sys.stderr)
    if any(item.get("error") == "timeout" for item in results):
        print("Home Assistant did not answer in time.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Print structured results for the named readings. Exit 1 if any failed."""
    args = list(sys.argv[1:] if argv is None else argv)
    client = HomeAssistant.from_env()
    if args in (["-h"], ["--help"]):
        print(
            "usage: python -m brain.home_assistant [name ...]",
            file=sys.stderr,
        )
        shown = ", ".join(client.names) if client.names else "(none)"
        print("names: " + shown, file=sys.stderr)
        return 0
    names = args or list(client.names)
    if not names:
        print(
            "No Home Assistant readings are configured. Set HOME_ASSISTANT_URL, "
            "HOME_ASSISTANT_TOKEN, and HA_<NAME> in server/.env (see .env.example).",
            file=sys.stderr,
        )
        json.dump([], sys.stdout)
        sys.stdout.write("\n")
        return 1
    results = [client.get_state(name) for name in names]
    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    _hints(results, client._url)
    return 0 if all(item.get("ok") is True for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
