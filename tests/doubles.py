"""Test doubles for models, clock, screen and audio.

Each double records what it was asked to do so tests can assert on behaviour
rather than on internal state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from src.open_llm_vtuber.agent.stateless_llm.stateless_llm_interface import (
    StatelessLLMInterface,
)


class FakeLLM(StatelessLLMInterface):
    """Deterministic streaming LLM stand-in.

    Args:
        chunks: Text chunks to emit for each call, in order.
        delay: Optional per-chunk delay, used to create interruptible windows.
        fail_with: Optional exception raised instead of streaming.
    """

    def __init__(
        self,
        chunks: Optional[Sequence[str]] = None,
        *,
        delay: float = 0.0,
        fail_with: Optional[BaseException] = None,
    ) -> None:
        self.chunks = list(chunks) if chunks is not None else ["你好。"]
        self.delay = delay
        self.fail_with = fail_with

        self.calls: List[Dict[str, Any]] = []
        self.completed_streams = 0
        self.aborted_streams = 0

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream the configured chunks, honouring cancellation."""
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        try:
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.fail_with is not None:
                    raise self.fail_with
                yield chunk
            self.completed_streams += 1
        except asyncio.CancelledError:
            # Mirrors a real streaming client being cancelled mid-flight.
            self.aborted_streams += 1
            raise


class FakeMemory:
    """In-memory stand-in for the local memory store."""

    def __init__(self, items: Optional[List[Any]] = None) -> None:
        self.items = list(items or [])
        self.recall_calls: List[str] = []
        self.fail_recall = False
        self.recall_delay = 0.0

    async def recall(self, query: str, limit: int = 5) -> List[Any]:
        """Return the configured items, or raise if configured to fail."""
        self.recall_calls.append(query)
        if self.recall_delay:
            await asyncio.sleep(self.recall_delay)
        if self.fail_recall:
            raise RuntimeError("embedding provider unavailable")
        return self.items[:limit]


class FakeClock:
    """Manually advanced clock, so cooldowns are testable without sleeping."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        """Current fake time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


@dataclass
class PlaybackRecord:
    """One recorded playback attempt."""

    turn_id: str
    text: str
    started_at: float
    completed: bool = False
    cancelled: bool = False


class FakeAudioPlayer:
    """Records playback instead of emitting sound.

    Used to assert that output for a cancelled turn is never played and that
    classroom mode suppresses audio.
    """

    def __init__(self) -> None:
        self.played: List[PlaybackRecord] = []
        self.pending: Dict[str, PlaybackRecord] = {}

    async def play(self, turn_id: str, text: str) -> PlaybackRecord:
        """Record a playback and return its record."""
        record = PlaybackRecord(turn_id=turn_id, text=text, started_at=time.time())
        self.played.append(record)
        self.pending[turn_id] = record
        return record

    def cancel(self, turn_id: str) -> None:
        """Mark playback for a turn as cancelled."""
        record = self.pending.get(turn_id)
        if record is not None and not record.completed:
            record.cancelled = True

    def cancel_all(self) -> None:
        """Cancel every pending playback."""
        for record in self.pending.values():
            if not record.completed:
                record.cancelled = True


@dataclass
class FakeWindow:
    """A pretend foreground window."""

    title: str
    rect: tuple[int, int, int, int] = (0, 0, 1280, 720)


class FakeScreenCapture:
    """Screen capture substitute.

    Args:
        window: Window to report, or ``None`` to simulate capture failure.
    """

    def __init__(self, window: Optional[FakeWindow] = None) -> None:
        self.window = window
        self.capture_count = 0
        self.fail = False
        self.locked = False

    def is_locked(self) -> bool:
        """Whether the session is locked."""
        return self.locked

    def foreground_window(self) -> Optional[FakeWindow]:
        """Return the current foreground window, or ``None`` if unavailable."""
        if self.fail:
            return None
        return self.window

    def grab(self, window) -> bytes:
        """Return a fake image payload for the given window."""
        self.capture_count += 1
        if self.fail:
            raise RuntimeError("capture failed")
        return b"\x89PNG\r\n\x1a\nfake-image-bytes"


class FakeVision:
    """Vision model substitute."""

    def __init__(self, summary: str = "用户正在编辑一份文档。") -> None:
        self.summary = summary
        self.fail = False
        self.calls: List[bytes] = []

    async def describe(self, image: bytes, window_title: str = "") -> str:
        """Return a canned description of the image."""
        self.calls.append(image)
        if self.fail:
            raise RuntimeError("vision provider unavailable")
        return self.summary


class FakeTTS:
    """TTS substitute that can be told to fail."""

    def __init__(self) -> None:
        self.synthesised: List[str] = []
        self.fail = False

    async def synthesize(self, text: str) -> bytes:
        """Return fake audio, or raise if configured to fail."""
        if self.fail:
            raise RuntimeError("tts provider unavailable")
        self.synthesised.append(text)
        return b"fake-audio"


class FakeASR:
    """ASR substitute that can be told to fail."""

    def __init__(self, transcript: str = "你好") -> None:
        self.transcript = transcript
        self.fail = False
        self.calls: List[bytes] = []

    async def transcribe(self, audio: bytes) -> str:
        """Return a canned transcript, or raise if configured to fail."""
        self.calls.append(audio)
        if self.fail:
            raise RuntimeError("asr provider unavailable")
        return self.transcript
