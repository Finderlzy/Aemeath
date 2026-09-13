"""Situation state: conversation mode, switches and persistence.

Two modes exist:

* ``normal`` — replies may be spoken and Aemeath may speak up on her own.
* ``class``  — replies are text only; proactive messages are text only.

The state is persisted locally so a restart keeps the classroom mode. The plan
requires that mode is *not* inferred from how the user typed: typing during a
lesson must not silently un-mute the character, and a question *about* class
("if I were in class, what would you do?") must not switch the mode either.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

from loguru import logger

from .interfaces import SituationState, SpeechMode

# Unambiguous phrases that switch the mode directly, without consulting a model.
# Order matters: the first matching pattern wins.
_CLASS_PHRASES: Sequence[str] = (
    "我在上课",
    "我上课了",
    "在上课",
    "开始上课",
    "我要上课",
    "上课了",
    "进课堂",
    "我在听课",
)
_NORMAL_PHRASES: Sequence[str] = (
    "我下课了",
    "下课了",
    "已经下课",
    "上完课了",
    "我下课",
)
_PAUSE_PHRASES: Sequence[str] = ("先别说话", "别说话", "闭嘴", "安静一会", "先别说")
_RESUME_PHRASES: Sequence[str] = ("可以说话了", "能说话了", "你说吧", "可以聊了")

# Constructions that *mention* class without asking to switch to it. Checked
# before the switch phrases so hypotheticals and questions never trigger.
_HYPOTHETICAL_MARKERS: Sequence[str] = (
    "如果",
    "假如",
    "要是",
    "假设",
    "会不会",
    "怎么办",
    "怎么处理",
    "为什么",
    "什么时候",
    "吗？",
    "吗?",
    "？",
    "?",
)


class SituationStore:
    """Persists and updates the situation state in the local database."""

    def __init__(self, db_path: Path) -> None:
        """Bind the store to a local SQLite database.

        Args:
            db_path: Path to the Aemeath database file.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._state = self._load()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with row access by name."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, and always close it.

        ``with sqlite3.connect(...)`` commits but never closes, and on Windows a
        leftover handle keeps the database file locked. Every database call in
        this module goes through here so that cannot happen.

        Yields:
            An open connection; committed on success, rolled back on error.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create the state table when missing, adding later columns in place."""
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS situation_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL,
                    microphone_enabled INTEGER NOT NULL DEFAULT 0,
                    screen_observation_enabled INTEGER NOT NULL DEFAULT 0,
                    proactive_enabled INTEGER NOT NULL DEFAULT 0,
                    user_paused INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            # Existing databases predate state versioning. Adding the column
            # keeps their data; the version simply starts at 0.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(situation_state)")
            }
            if "state_version" not in columns:
                conn.execute(
                    "ALTER TABLE situation_state "
                    "ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0"
                )

    def _load(self) -> SituationState:
        """Read the persisted state, falling back to defaults."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM situation_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return SituationState()
        try:
            mode = SpeechMode(row["mode"])
        except ValueError:
            logger.warning("Unknown persisted mode '{}'; using normal.", row["mode"])
            mode = SpeechMode.NORMAL
        return SituationState(
            mode=mode,
            microphone_enabled=bool(row["microphone_enabled"]),
            screen_observation_enabled=bool(row["screen_observation_enabled"]),
            proactive_enabled=bool(row["proactive_enabled"]),
            user_paused=bool(row["user_paused"]),
        )

    @property
    def state_version(self) -> int:
        """Monotonic version of the persisted situation state.

        Incremented on every save so a client can reject state that arrives out
        of order, and so output tagged with an older version can be discarded.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT state_version FROM situation_state WHERE id = 1"
            ).fetchone()
        return int(row["state_version"]) if row else 0

    def save(self, state: SituationState) -> SituationState:
        """Persist a new state and return it.

        Args:
            state: The state to store.

        Returns:
            The stored state.
        """
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO situation_state
                    (id, mode, microphone_enabled, screen_observation_enabled,
                     proactive_enabled, user_paused, updated_at, state_version)
                VALUES (1, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    mode=excluded.mode,
                    microphone_enabled=excluded.microphone_enabled,
                    screen_observation_enabled=excluded.screen_observation_enabled,
                    proactive_enabled=excluded.proactive_enabled,
                    user_paused=excluded.user_paused,
                    updated_at=excluded.updated_at,
                    state_version=situation_state.state_version + 1
                """,
                (
                    state.mode.value,
                    int(state.microphone_enabled),
                    int(state.screen_observation_enabled),
                    int(state.proactive_enabled),
                    int(state.user_paused),
                    time.time(),
                ),
            )
        self._state = state
        logger.info(
            "Situation saved: mode={} mic={} screen={} proactive={} paused={} version={}",
            state.mode.value,
            state.microphone_enabled,
            state.screen_observation_enabled,
            state.proactive_enabled,
            state.user_paused,
            self.state_version,
        )
        return state

    @property
    def state(self) -> SituationState:
        """The current in-memory state."""
        return self._state

    def reload(self) -> SituationState:
        """Re-read the persisted state (used after an external change)."""
        self._state = self._load()
        return self._state


def is_hypothetical(text: str) -> bool:
    """Whether a phrase merely discusses the mode rather than requesting it.

    Args:
        text: The user's message.

    Returns:
        ``True`` when the message looks like a question or hypothetical.
    """
    return any(marker in text for marker in _HYPOTHETICAL_MARKERS)


def detect_explicit_mode(text: str) -> Optional[SpeechMode]:
    """Map an unambiguous phrase to a mode.

    Hypotheticals and questions are rejected before matching, so
    "如果我在上课，你会怎么办？" does not switch modes.

    Args:
        text: The user's message.

    Returns:
        The requested mode, or ``None`` when the message is not a direct
        instruction.
    """
    normalized = (text or "").strip()
    if not normalized:
        return None
    if len(normalized) > 30:
        # Mode switches are short commands; long sentences are conversation.
        return None
    if is_hypothetical(normalized):
        return None
    for phrase in _CLASS_PHRASES:
        if phrase in normalized:
            return SpeechMode.CLASS
    for phrase in _NORMAL_PHRASES:
        if phrase in normalized:
            return SpeechMode.NORMAL
    return None


def detect_pause(text: str) -> Optional[bool]:
    """Detect an explicit request to stop or resume talking.

    Args:
        text: The user's message.

    Returns:
        ``True`` to pause, ``False`` to resume, ``None`` when unmentioned.
    """
    normalized = (text or "").strip()
    if not normalized or len(normalized) > 30 or is_hypothetical(normalized):
        return None
    for phrase in _PAUSE_PHRASES:
        if phrase in normalized:
            return True
    for phrase in _RESUME_PHRASES:
        if phrase in normalized:
            return False
    return None


class SituationManager:
    """Owns the situation state and applies mode changes consistently.

    Holds the queue of pending audio that must be dropped when switching into
    classroom mode, so that "I'm in class" stops speech already in flight.
    """

    def __init__(self, store: SituationStore) -> None:
        """Create a manager over a persisted store."""
        self._store = store
        self._pending_audio: set[str] = set()
        # Serialises read-modify-write cycles so two concurrent toggles cannot
        # both write from the same starting state.
        self._lock = threading.RLock()

    @property
    def state(self) -> SituationState:
        """Current situation state."""
        return self._store.state

    @property
    def state_version(self) -> int:
        """Version of the persisted state, incremented on every change."""
        return self._store.state_version

    def set_mode(self, mode: SpeechMode) -> SituationState:
        """Switch conversation mode.

        Entering classroom mode immediately invalidates queued audio so a
        previous normal-mode reply does not keep playing into the lesson.
        """
        with self._lock:
            state = self._store.state
            if state.mode is mode:
                return state
            new_state = SituationState(
                mode=mode,
                microphone_enabled=state.microphone_enabled,
                screen_observation_enabled=state.screen_observation_enabled,
                proactive_enabled=state.proactive_enabled,
                user_paused=state.user_paused,
            )
            if mode is SpeechMode.CLASS:
                dropped = len(self._pending_audio)
                self._pending_audio.clear()
                if dropped:
                    logger.info("Dropped {} queued audio item(s) on class mode.", dropped)
            return self._store.save(new_state)

    def set_microphone(self, enabled: bool) -> SituationState:
        """Turn microphone capture on or off."""
        with self._lock:
            state = self._store.state
            return self._store.save(
                SituationState(
                    mode=state.mode,
                    microphone_enabled=enabled,
                    screen_observation_enabled=state.screen_observation_enabled,
                    proactive_enabled=state.proactive_enabled,
                    user_paused=state.user_paused,
                )
            )

    def set_screen_observation(self, enabled: bool) -> SituationState:
        """Turn automatic screen observation on or off."""
        with self._lock:
            state = self._store.state
            return self._store.save(
                SituationState(
                    mode=state.mode,
                    microphone_enabled=state.microphone_enabled,
                    screen_observation_enabled=enabled,
                    proactive_enabled=state.proactive_enabled,
                    user_paused=state.user_paused,
                )
            )

    def set_proactive(self, enabled: bool) -> SituationState:
        """Turn proactive conversation on or off."""
        with self._lock:
            state = self._store.state
            return self._store.save(
                SituationState(
                    mode=state.mode,
                    microphone_enabled=state.microphone_enabled,
                    screen_observation_enabled=state.screen_observation_enabled,
                    proactive_enabled=enabled,
                    user_paused=state.user_paused,
                )
            )

    def set_paused(self, paused: bool) -> SituationState:
        """Record whether the user wants to be left alone for now."""
        with self._lock:
            state = self._store.state
            return self._store.save(
                SituationState(
                    mode=state.mode,
                    microphone_enabled=state.microphone_enabled,
                    screen_observation_enabled=state.screen_observation_enabled,
                    proactive_enabled=state.proactive_enabled,
                    user_paused=paused,
                )
            )

    def observe_user_text(self, text: str) -> Optional[str]:
        """Apply mode/pause changes implied by a user message.

        Only unambiguous, explicit phrasings take effect. Anything else leaves
        the state untouched; other phrasings are left to model-based intent
        detection, which must also default to "no change" when ambiguous.

        Args:
            text: The user's message.

        Returns:
            A short acknowledgement to show, or ``None`` when nothing changed.
        """
        mode = detect_explicit_mode(text)
        if mode is not None and mode is not self.state.mode:
            self.set_mode(mode)
            if mode is SpeechMode.CLASS:
                return "好，我改用文字。"
            return "好，我说话了。"

        paused = detect_pause(text)
        if paused is not None and paused is not self.state.user_paused:
            self.set_paused(paused)
            return "好。" if paused else "嗯，我在。"

        return None

    def register_pending_audio(self, turn_id: str) -> None:
        """Track audio waiting to play for a turn."""
        self._pending_audio.add(turn_id)

    def clear_pending_audio(self, turn_id: str) -> None:
        """Stop tracking audio for a turn."""
        self._pending_audio.discard(turn_id)

    def should_speak(self) -> bool:
        """Whether audio may currently be produced or played.

        Checked both before synthesis and immediately before playback, so a
        mode change during generation still takes effect.
        """
        return self.state.voice_allowed
