"""Pick a Mac camera or microphone by name, favourite first.

MIC_DEVICE and CAMERA_DEVICE are comma-separated names. The first one that
is actually present is used, matched as a case-insensitive substring so a
short unique piece ("PowerConf", "FaceTime") is enough. Indexes are avoided
on purpose: plugging a camera in changes them.

When none of the named devices are present, the computer's own built-in
camera or microphone is the next choice. An empty setting means "no
preference" and the caller keeps its existing default.
"""

from __future__ import annotations


def preferences(spec: str | None) -> list[str]:
    if not spec:
        return []
    return [part.strip() for part in spec.split(",") if part.strip()]


def match_name(names: list[str], wanted: str) -> int | None:
    """Index of the one device `wanted` names, or None.

    A decimal string is an index into `names`. Anything else is a
    case-insensitive substring and must match exactly one name.
    """
    if wanted.isdigit():
        index = int(wanted)
        if names and index >= len(names):
            return None
        return index
    needle = wanted.lower()
    hits = [i for i, name in enumerate(names) if needle in name.lower()]
    if len(hits) == 1:
        return hits[0]
    exact = [i for i, name in enumerate(names) if name.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    return None


def builtin_camera(names: list[str]) -> int | None:
    """The computer's own camera, ignoring virtual, phone and desk-view cameras."""
    best: tuple[int, int] | None = None
    for i, name in enumerate(names):
        low = name.lower()
        if "desk view" in low or "virtual" in low or low.startswith("obs"):
            continue
        if "facetime" in low:
            score = 0
        elif "macbook" in low and "camera" in low:
            score = 1
        elif "built-in" in low and "camera" in low:
            score = 2
        else:
            continue
        if best is None or score < best[0]:
            best = (score, i)
    return None if best is None else best[1]


def builtin_mic(names: list[str]) -> int | None:
    """The computer's own microphone, ignoring phones and virtual inputs."""
    for i, name in enumerate(names):
        low = name.lower()
        if "microphone" not in low:
            continue
        if any(hint in low for hint in ("macbook", "built-in", "imac", "mac studio", "mac mini", "mac pro")):
            return i
    return None


def choose(names: list[str], spec: str | None, fallback) -> tuple[int | None, str | None]:
    """(index, preferences that were named but absent).

    The index is into `names`. None means the caller should use its own
    default: either nothing was configured, or a configured name was absent
    and `fallback` could not find a built-in device either. The second value
    is those missing names joined with ", ", so the server can say what it
    skipped.
    """
    prefs = preferences(spec)
    for i, pref in enumerate(prefs):
        hit = match_name(names, pref)
        if hit is not None:
            missed = ", ".join(prefs[:i]) or None
            return hit, missed
    if not prefs:
        return None, None
    found = fallback(names) if names else None
    return found, ", ".join(prefs)
