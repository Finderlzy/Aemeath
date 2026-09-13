"""Proactive conversation scheduling.

Two-stage design, as the plan requires: rules decide whether Aemeath is
*eligible* to speak, and the model then decides whether anything is worth
saying. A timer firing is never by itself a reason to talk.

Eligibility rules:

* Do not speak while the microphone is capturing, while the user is typing, or
  while a reply is still being produced.
* Do not speak when locked, disconnected, paused, or with proactive turned off.
* At most one startup greeting, and no immediate follow-up questions.
* At least ``cooldown_seconds`` since the last proactive message.
* At most ``max_per_hour`` proactive messages in any rolling hour.
* If the last proactive message went unanswered, stay quiet until the user
  speaks again.
* In classroom mode proactive output is text only.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from loguru import logger

from .interfaces import SituationState, SpeechMode


@dataclass(frozen=True)
class ProactiveDecision:
    """Outcome of an eligibility check."""

    eligible: bool
    reason: str

    @classmethod
    def allow(cls, reason: str = "eligible") -> "ProactiveDecision":
        """Build an allowing decision."""
        return cls(True, reason)

    @classmethod
    def deny(cls, reason: str) -> "ProactiveDecision":
        """Build a denying decision."""
        return cls(False, reason)


class ProactiveScheduler:
    """Tracks eligibility for proactive messages."""

    def __init__(
        self,
        *,
        cooldown_seconds: int = 900,
        max_per_hour: int = 2,
        startup_greeting_enabled: bool = True,
    ) -> None:
        """Configure the rate limits.

        Args:
            cooldown_seconds: Minimum gap between proactive messages.
            max_per_hour: Maximum proactive messages per rolling hour.
            startup_greeting_enabled: Whether a startup greeting is allowed.
        """
        self.cooldown_seconds = cooldown_seconds
        self.max_per_hour = max_per_hour
        self.startup_greeting_enabled = startup_greeting_enabled

        self._history: Deque[float] = deque()
        self._last_proactive_at: Optional[float] = None
        self._awaiting_response = False
        self._greeted = False

    # -- state updates -------------------------------------------------

    def mark_spoken(self, now: Optional[float] = None) -> None:
        """Record that a proactive message was sent."""
        timestamp = now if now is not None else time.time()
        self._history.append(timestamp)
        self._last_proactive_at = timestamp
        self._awaiting_response = True

    def mark_user_spoke(self) -> None:
        """Record user activity, which clears the unanswered state."""
        self._awaiting_response = False

    def mark_greeted(self) -> None:
        """Record that the startup greeting has been used."""
        self._greeted = True

    @property
    def awaiting_response(self) -> bool:
        """Whether the last proactive message is still unanswered."""
        return self._awaiting_response

    # -- eligibility ---------------------------------------------------

    def _prune(self, now: float) -> None:
        """Drop history entries older than one hour."""
        cutoff = now - 3600.0
        while self._history and self._history[0] < cutoff:
            self._history.popleft()

    def check(
        self,
        *,
        situation: SituationState,
        mic_active: bool = False,
        user_typing: bool = False,
        generation_active: bool = False,
        client_connected: bool = True,
        screen_locked: bool = False,
        is_startup: bool = False,
        now: Optional[float] = None,
    ) -> ProactiveDecision:
        """Decide whether Aemeath may start a topic right now.

        Args:
            situation: Current situation state.
            mic_active: Whether audio is being captured.
            user_typing: Whether the user is typing in the input box.
            generation_active: Whether a reply is still being produced.
            client_connected: Whether the desktop client is connected.
            screen_locked: Whether the session is locked.
            is_startup: Whether this check is the startup greeting.
            now: Optional timestamp override for tests.

        Returns:
            A :class:`ProactiveDecision`.
        """
        current = now if now is not None else time.time()

        if not situation.proactive_enabled:
            return ProactiveDecision.deny("proactive disabled")
        if situation.user_paused:
            return ProactiveDecision.deny("user asked not to talk")
        if not client_connected:
            return ProactiveDecision.deny("client disconnected")
        if screen_locked:
            return ProactiveDecision.deny("session locked")

        # User activity always wins over a self-initiated topic.
        if mic_active:
            return ProactiveDecision.deny("microphone is capturing")
        if user_typing:
            return ProactiveDecision.deny("user is typing")
        if generation_active:
            return ProactiveDecision.deny("a reply is in progress")

        if self._last_proactive_at is not None:
            elapsed = current - self._last_proactive_at
            if elapsed < self.cooldown_seconds:
                remaining = int(self.cooldown_seconds - elapsed)
                return ProactiveDecision.deny(f"cooldown active ({remaining}s left)")

        # An unanswered message means stay quiet even once the cooldown has
        # passed; checked after the cooldown so the nearer reason is reported.
        if self._awaiting_response:
            return ProactiveDecision.deny("previous proactive message unanswered")

        if is_startup:
            if not self.startup_greeting_enabled:
                return ProactiveDecision.deny("startup greeting disabled")
            if self._greeted:
                return ProactiveDecision.deny("already greeted")
            return ProactiveDecision.allow("startup greeting")

        self._prune(current)
        if len(self._history) >= self.max_per_hour:
            return ProactiveDecision.deny("hourly limit reached")

        return ProactiveDecision.allow()

    def may_speak(self, situation: SituationState) -> bool:
        """Whether proactive output may be voiced in this situation.

        Classroom mode allows proactive *text* but never speech.
        """
        return situation.mode is SpeechMode.NORMAL and not situation.user_paused

    def next_eligible_at(self, now: Optional[float] = None) -> Optional[float]:
        """Earliest timestamp at which a proactive message could be sent."""
        if self._awaiting_response:
            return None
        if self._last_proactive_at is None:
            return now if now is not None else time.time()
        return self._last_proactive_at + self.cooldown_seconds

    def reset(self) -> None:
        """Clear all scheduling state (used when the client reconnects)."""
        self._history.clear()
        self._last_proactive_at = None
        self._awaiting_response = False
        self._greeted = False
        logger.debug("Proactive scheduler reset.")
