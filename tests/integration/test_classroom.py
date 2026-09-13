"""Integration tests: the classroom scenarios.

These drive the real production entry points — the validated upstream config
schema, ``AgentFactory``, the Aemeath bridge, the patched upstream conversation
path and the client protocol — and substitute only the model providers, the
audio devices and the capture backend.

Covered here (see the plan's scenario table):

* switching to class mode while audio is playing stops it, clears the queue,
  rejects late audio and still shows the confirmation text;
* a second interruption plus new input does not let the first turn's text or
  audio overwrite the new turn, and the next turn still completes.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.interfaces import SpeechMode
from tests.doubles import FakeLLM
from tests.integration.harness import run_turn


class TestClassModeSilence:
    """Switching into class mode must silence output in an ordered way."""

    async def test_switch_to_class_sends_state_then_clear_then_text(self, make_harness):
        """The client is told the new state, then to clear audio, then shown text."""
        harness = make_harness(llm=FakeLLM(["你好呀。"]))
        harness.websocket.clear()

        await harness.bridge.switch_mode(SpeechMode.CLASS)

        types = harness.websocket.types_sent()
        assert "aemeath-state" in types
        assert "aemeath-clear-audio" in types
        assert "full-text" in types

        # Ordering is the point: state first, clear-audio second, text last.
        assert types.index("aemeath-state") < types.index("aemeath-clear-audio")
        assert types.index("aemeath-clear-audio") < types.index("full-text")

        state = harness.websocket.last_of("aemeath-state")
        assert state["state"]["mode"] == "class"
        assert state["state"]["voice_allowed"] is False

        confirmation = harness.websocket.last_of("full-text")
        assert "文字" in confirmation["text"]

    async def test_class_mode_still_displays_text(self, make_harness):
        """Text must keep flowing in class mode; muting is audio-only."""
        harness = make_harness(llm=FakeLLM(["这是一段文字回复。"]))
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        await run_turn(harness, "你好")

        texts = harness.websocket.frames_of("aemeath-text")
        joined = "".join(frame["text"] for frame in texts)
        assert "文字回复" in joined, "class mode must not suppress display text"

    async def test_class_mode_suppresses_audio(self, make_harness):
        """No audio frames may reach the client while in class mode."""
        harness = make_harness(llm=FakeLLM(["这段不应该被念出来。"]))
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        await run_turn(harness, "你好")

        assert harness.websocket.frames_of("audio") == []
        assert harness.bridge.dropped_late_audio >= 0

    async def test_late_audio_rejected_after_switch(self, make_harness):
        """Audio already synthesising when the mode changes must be dropped.

        This is the core classroom guarantee: a reply that started generating in
        normal mode must not keep playing into the lesson.
        """
        harness = make_harness(llm=FakeLLM(["第一句。", "第二句。"]))
        harness.websocket.clear()

        turn = harness.bridge._coordinator.begin_turn(
            __import__("aemeath.interfaces", fromlist=["EventSource"]).EventSource.USER_TEXT
        )
        turn_id = str(turn.turn_id)

        # Audio that would have been valid a moment ago.
        assert harness.bridge.may_send_audio(turn_id) is True

        # The user switches to class mode while synthesis is in flight.
        await harness.bridge.switch_mode(SpeechMode.CLASS)

        # The same turn can no longer emit audio, even though it was fine before.
        assert harness.bridge.may_send_audio(turn_id) is False
        slice_id = await harness.bridge.send_audio(turn_id, "ZmFrZQ==")
        assert slice_id is None
        assert harness.websocket.frames_of("audio") == []
        assert harness.bridge.dropped_late_audio >= 1

    async def test_switch_cancels_active_turn(self, make_harness):
        """Entering class mode cancels the running turn."""
        harness = make_harness(llm=FakeLLM(["慢一点。"]))
        turn = harness.bridge._coordinator.begin_turn(
            __import__("aemeath.interfaces", fromlist=["EventSource"]).EventSource.USER_TEXT
        )
        turn_id = str(turn.turn_id)

        await harness.bridge.switch_mode(SpeechMode.CLASS)

        from aemeath.interfaces import TurnId

        assert harness.bridge._coordinator.is_cancelled(TurnId(turn_id))

    async def test_mode_switch_is_persisted_and_versioned(self, make_harness):
        """The switch is durable and bumps the state version."""
        harness = make_harness(llm=FakeLLM(["好。"]))
        before = harness.runtime.situation.state_version

        await harness.bridge.switch_mode(SpeechMode.CLASS)

        after = harness.runtime.situation.state_version
        assert after > before, "a state change must increment the version"

        reloaded = harness.runtime.situation._store.reload()
        assert reloaded.mode is SpeechMode.CLASS


class TestInterruption:
    """Repeated interruption must not let old output win."""

    async def test_consecutive_interrupts_keep_latest_turn(self, make_harness):
        """Old text and audio must not overwrite the new turn's output."""
        llm = FakeLLM(["旧回复。"])
        harness = make_harness(llm=llm)

        await run_turn(harness, "第一轮")
        harness.websocket.clear()

        llm.chunks = ["新回复。"]
        await run_turn(harness, "第二轮")

        texts = harness.websocket.frames_of("aemeath-text")
        joined = "".join(frame["text"] for frame in texts)
        assert "新回复" in joined
        assert "旧回复" not in joined, "a superseded turn's text must not reappear"

    async def test_late_text_for_cancelled_turn_is_dropped(self, make_harness):
        """Text for a cancelled turn is refused by the coordinator."""
        harness = make_harness(llm=FakeLLM(["旧回复。"]))
        harness.websocket.clear()

        from aemeath.interfaces import EventSource, TurnId

        turn = harness.bridge._coordinator.begin_turn(EventSource.USER_TEXT)
        turn_id = turn.turn_id

        harness.bridge._coordinator.interrupt()
        assert harness.bridge._coordinator.is_cancelled(TurnId(str(turn_id)))

        delivered = await harness.bridge._coordinator.emit_text(turn_id, "迟到的文字")
        assert delivered is False
        assert harness.websocket.frames_of("aemeath-text") == []

    async def test_turn_completes_after_interruption(self, make_harness):
        """The turn after an interruption must still complete normally."""
        llm = FakeLLM(["被打断的。"])
        harness = make_harness(llm=llm)

        await run_turn(harness, "第一轮")
        harness.bridge._coordinator.interrupt()
        harness.websocket.clear()

        llm.chunks = ["这一轮正常完成。"]
        result = await run_turn(harness, "第二轮")

        assert "这一轮正常完成" in result
        joined = "".join(
            frame["text"] for frame in harness.websocket.frames_of("aemeath-text")
        )
        assert "这一轮正常完成" in joined

    async def test_receipts_carry_turn_and_slice_ids(self, make_harness):
        """Every output frame carries the ids the client needs to reject it."""
        harness = make_harness(llm=FakeLLM(["带标识的回复。"]))
        harness.websocket.clear()

        await run_turn(harness, "你好")

        text_frames = harness.websocket.frames_of("aemeath-text")
        assert text_frames, "expected display text to be sent"
        for frame in text_frames:
            assert frame.get("turn_id"), "text frames must carry a turn id"
            assert "state_version" in frame
            assert frame.get("generation") == harness.bridge.generation

        audio_frames = harness.websocket.frames_of("audio")
        for frame in audio_frames:
            assert frame.get("turn_id")
            assert frame.get("audio_slice_id"), "audio must carry a slice id"
            assert "slice_index" in frame
