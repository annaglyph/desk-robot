"""When Rocky's head may move on its own.

The firmware runs the idle choreography. This only decides whether it should
be on. Any blocking state, or a moment of activity, restarts the quiet clock;
idle is wanted once nothing has blocked it for the whole quiet period. A held
look is separate: it waits out its own hold without restarting that clock, so
idle can resume as soon as the hold ends.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdleInputs:
    connected: bool
    awake: bool
    thinking: bool
    speaking: bool
    hearing: bool
    tracking: bool
    tracking_enabled: bool
    held: bool


def blocker(state: IdleInputs) -> str | None:
    """The first thing that rules idle out right now, or None."""
    if not state.connected:
        return "robot not connected"
    if not state.awake:
        return "asleep"
    if state.thinking:
        return "thinking"
    if state.speaking:
        return "speaking"
    if state.hearing:
        return "listening"
    if state.tracking or state.tracking_enabled:
        return "tracking"
    return None


class IdleController:
    def __init__(self, quiet_seconds: float) -> None:
        self.quiet_seconds = quiet_seconds
        self.reason = "starting"
        self._busy_at: float | None = None

    def note_activity(self, now: float) -> None:
        """Something deliberate happened: wait a full quiet period again."""
        self._busy_at = now

    def wanted(self, now: float, state: IdleInputs) -> bool:
        why = blocker(state)
        if why is not None:
            self._busy_at = now
            self.reason = why
            return False
        if self._busy_at is None:
            self._busy_at = now
        if state.held:
            self.reason = "holding a look"
            return False
        if now - self._busy_at < self.quiet_seconds:
            self.reason = "quiet period"
            return False
        self.reason = "idle"
        return True
