"""Aemeath internal interfaces.

These are the stable boundaries the architecture document requires. They are
deliberately small: only the types that cross module borders live here.

Invariants enforced by these types:

* A user message and a proactive event carry different sources
  (:class:`EventSource`). A proactive prompt must never be recorded as
  something the user said.
* Every generation turn has an id (:class:`TurnId`). Output produced for a
  cancelled turn is rejected rather than played.
* Speaking is governed by :class:`SpeechMode`; the classroom mode suppresses
  voice output while text keeps flowing.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EventSource(str, Enum):
    """Where an input event came from.

    Kept distinct so that proactive events are never attributed to the user.
    """

    USER_TEXT = "user_text"
    USER_VOICE = "user_voice"
    PROACTIVE = "proactive"
    SCREEN = "screen"
    TIMER = "timer"
    SYSTEM = "system"


class SpeechMode(str, Enum):
    """Conversation mode controlling whether replies may be spoken."""

    NORMAL = "normal"
    CLASS = "class"


class TurnId(str):
    """Identifier for one generation turn.

    A plain ``str`` subclass so it serialises directly into WebSocket payloads
    while still being a distinct type at call sites.
    """

    @classmethod
    def new(cls) -> "TurnId":
        """Create a fresh, unique turn id."""
        return cls(uuid.uuid4().hex)


@dataclass(frozen=True)
class InputEvent:
    """One input event entering the coordination layer.

    Attributes:
        source: Origin of the event.
        text: Text content, if any.
        created_at: Unix timestamp of the event.
        event_id: Unique id for this event.
        screen_ref: Optional reference to a screen observation.
    """

    source: EventSource
    text: str = ""
    created_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    screen_ref: Optional[str] = None

    @property
    def is_user(self) -> bool:
        """Whether this event represents the user actually speaking/typing."""
        return self.source in (EventSource.USER_TEXT, EventSource.USER_VOICE)


@dataclass(frozen=True)
class SituationState:
    """Current situation governing how Aemeath may respond.

    Attributes:
        mode: Normal or classroom conversation mode.
        microphone_enabled: Whether audio capture is on.
        screen_observation_enabled: Whether automatic screen observation is on.
        proactive_enabled: Whether Aemeath may start a topic on her own.
        user_paused: Set when the user asked not to be talked to for now.
    """

    mode: SpeechMode = SpeechMode.NORMAL
    microphone_enabled: bool = False
    screen_observation_enabled: bool = False
    proactive_enabled: bool = False
    user_paused: bool = False

    @property
    def voice_allowed(self) -> bool:
        """Whether spoken replies are permitted in this state.

        Classroom mode suppresses ordinary reply audio independently of how the
        user provided input.
        """
        return self.mode is SpeechMode.NORMAL and not self.user_paused


@dataclass(frozen=True)
class TurnOutput:
    """One piece of output belonging to a specific turn.

    Attributes:
        turn_id: Turn this output belongs to.
        display_text: Text for the UI.
        speech_allowed: Whether this output may be synthesised to audio.
        expression: Optional Live2D expression keyword.
    """

    turn_id: TurnId
    display_text: str
    speech_allowed: bool = True
    expression: Optional[str] = None


@dataclass(frozen=True)
class MemoryRecord:
    """A retrieved memory item with provenance.

    Attributes:
        memory_id: Stable local id.
        content: The remembered text.
        kind: ``fact`` or ``experience``.
        created_at: Unix timestamp when recorded.
        source_message_ids: Evidence messages supporting this memory.
    """

    memory_id: str
    content: str
    kind: str
    created_at: float
    source_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenObservation:
    """A screen observation summary with a timestamp.

    Attributes:
        observation_id: Stable id for referencing from prompts.
        summary: What the visual model reported.
        window_title: Foreground window title at capture time.
        captured_at: Unix timestamp of capture.
    """

    observation_id: str
    summary: str
    window_title: str
    captured_at: float

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Age of this observation in seconds."""
        return (now if now is not None else time.time()) - self.captured_at
