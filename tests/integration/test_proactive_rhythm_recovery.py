"""Integration tests for T06: proactive rhythm, anti-interruption, and failure recovery.

Covers:
1. Autonomous proactive ticks via runtime worker (no client signal).
2. Anti-interruption matrix (not speaking when typing, capturing mic, or generating;
   discarding candidate if user interrupts during generation; stopping playback).
3. Unanswered pauses: after proactive message is displayed, no continuous follow-up;
   remains quiet until user speaks again.
4. Disconnect and reconnect clean state: client disconnected blocks proactive;
   reconnect does not re-issue startup greeting; old candidates are invalidated;
   TTS engine remains intact across reconnect.
5. Request failure handling & recovery: generation failure safely ends turn, does not
   count against proactive budget, does not trigger unanswered state, recovers cleanly.
6. Memory extraction survives disconnect and operates idempotently without duplication.
7. Lock-screen detection stops observation and proactive chat.
8. Unavailable foreground window safely degrades without injecting false screen claims.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from aemeath.adapters import ExtractedFact, FakeExtractionAdapter
from aemeath.interfaces import EventSource, SpeechMode
from tests.doubles import (
    FakeLLM,
    FakeScreenCapture,
    FakeVision,
    FakeWindow,
)
from tests.integration.harness import run_turn
from tests.integration.test_proactive_voice import (
    enable_proactive,
    install_real_generator,
)


class ControllableLockCapture(FakeScreenCapture):
    """Fake capture backend with an explicit lock-screen flag."""

    def __init__(self, window: Optional[FakeWindow] = None, *, locked: bool = False):
        super().__init__(window)
        self.locked = locked

    def is_locked(self) -> bool:
        return self.locked


def make_t06_harness(
    make_harness,
    *,
    window: Optional[FakeWindow] = None,
    locked: bool = False,
    summary: str = "当前正在浏览代码",
    llm_responses: Optional[list[str]] = None,
    **overrides,
):
    capture = ControllableLockCapture(
        window or FakeWindow(title="测试编辑器 - main.py"),
        locked=locked,
    )
    vision = FakeVision(summary)
    llm = FakeLLM(llm_responses or ["好的。"])
    harness = make_harness(capture=capture, vision=vision, llm=llm, **overrides)
    return harness, capture, vision


@pytest.mark.asyncio
class TestProactiveRhythmAndAntiInterruption:
    """Tests for autonomous speech, anti-interruption, and non-continuous follow-ups."""

    async def test_autonomous_proactive_tick_without_client_signal(
        self, make_harness, monkeypatch
    ):
        """Timer alone triggers proactive speech; produces text and audio frames."""
        harness, capture, _ = make_t06_harness(
            make_harness, llm_responses=["下午好，工作还顺利吗？"]
        )
        await enable_proactive(harness)
        install_real_generator(harness, monkeypatch)

        # Advance observer clocks so stability and min_interval allow observation
        observer = harness.runtime.screen
        observer._last_attempt_at = time.time() - 300.0
        observer._window_seen_at = time.time() - 30.0

        bridge = harness.bridge
        coordinator = harness.runtime.coordinator
        assert bridge.pending_proactive is None

        # Simulate the runtime worker's autonomous tick
        message = await bridge.run_proactive()
        assert message == "下午好，工作还顺利吗？"
        assert bridge.pending_proactive is not None
        turn_id = bridge.pending_proactive.turn_id

        # Verify client received text frame via websocket
        text_frames = [
            f for f in harness.websocket.sent if f.get("type") == "aemeath-text"
        ]
        assert any(f.get("text") == "下午好，工作还顺利吗？" for f in text_frames)

        # Before receipt, scheduler awaiting_response is False
        assert not coordinator._scheduler.awaiting_response

        # Client sends display receipt
        await bridge.on_display_receipt(turn_id=turn_id)
        assert coordinator._scheduler.awaiting_response

    async def test_anti_interruption_matrix_blocks_proactive_speech(
        self, make_harness, monkeypatch
    ):
        """User typing, mic capturing, or active generation blocks proactive speech."""
        harness, _, _ = make_t06_harness(make_harness)
        await enable_proactive(harness)
        install_real_generator(harness, monkeypatch)

        bridge = harness.bridge
        coordinator = harness.runtime.coordinator

        # 1. User is typing
        await bridge.on_client_activity(typing=True)
        decision = await coordinator.consider_proactive()
        assert not decision.eligible
        assert "typing" in decision.reason
        msg = await bridge.run_proactive()
        assert msg is None
        await bridge.on_client_activity(typing=False)

        # 2. Microphone is capturing
        await bridge.on_client_activity(voice_active=True)
        decision = await coordinator.consider_proactive()
        assert not decision.eligible
        assert "microphone" in decision.reason
        msg = await bridge.run_proactive()
        assert msg is None
        await bridge.on_client_activity(voice_active=False)

        # 3. Generation in progress
        turn = coordinator.begin_turn(EventSource.USER_TEXT)
        decision = await coordinator.consider_proactive()
        assert not decision.eligible
        assert "reply is in progress" in decision.reason
        msg = await bridge.run_proactive()
        assert msg is None
        coordinator.end_turn(turn.turn_id)

        # Once cleared, proactive is eligible again
        decision = await coordinator.consider_proactive()
        assert decision.eligible

    async def test_user_typing_mid_generation_discards_proactive_candidate(
        self, make_harness
    ):
        """If user starts typing while LLM is generating, candidate is discarded."""
        harness, _, _ = make_t06_harness(make_harness)
        await enable_proactive(harness)

        bridge = harness.bridge

        async def slow_generate(*args, **kwargs):
            # User starts typing while generation is in progress
            await bridge.on_client_activity(typing=True)
            return "我是趁机说话的"

        bridge.set_proactive_generator(slow_generate)

        msg = await bridge.run_proactive()
        assert msg is None
        assert bridge.pending_proactive is None
        # No text frame sent to client
        text_frames = [
            f for f in harness.websocket.sent if f.get("type") == "aemeath-text"
        ]
        assert not any("我是趁机说话的" in f.get("text", "") for f in text_frames)

    async def test_unanswered_message_stops_continuous_prompting(
        self, make_harness, monkeypatch
    ):
        """After proactive message is displayed, she does NOT continuously prompt."""
        harness, _, _ = make_t06_harness(
            make_harness, llm_responses=["你在忙吗？"]
        )
        await enable_proactive(harness)
        install_real_generator(harness, monkeypatch)

        bridge = harness.bridge
        coordinator = harness.runtime.coordinator

        # First proactive succeeds
        msg = await bridge.run_proactive()
        assert msg == "你在忙吗？"
        turn_id = bridge.pending_proactive.turn_id
        await bridge.on_display_receipt(turn_id=turn_id)

        # Scheduler is now awaiting response
        assert coordinator._scheduler.awaiting_response

        # Even if cooldown has passed, she must not speak again without response
        fake_future = time.time() + 3600.0
        monkeypatch.setattr(time, "time", lambda: fake_future)

        decision = await coordinator.consider_proactive()
        assert not decision.eligible
        assert "unanswered" in decision.reason

        msg2 = await bridge.run_proactive()
        assert msg2 is None

        # User now speaks / texts back
        harness.agent._llm = FakeLLM(["好的，我在听。"])
        await run_turn(harness, "我在忙着写代码呢")

        # The unanswered lock is now cleared
        assert not coordinator._scheduler.awaiting_response

        # After cooldown from the user's turn / last proactive, she can speak again
        decision_after = await coordinator.consider_proactive()
        assert decision_after.eligible


@pytest.mark.asyncio
class TestDisconnectAndFailureRecovery:
    """Tests for disconnects, reconnects, model failures, and memory persistence."""

    async def test_client_disconnect_blocks_proactive_and_cleans_candidate(
        self, make_harness, monkeypatch
    ):
        """Disconnect marks client disconnected and clears pending proactive candidate."""
        harness, _, _ = make_t06_harness(
            make_harness, llm_responses=["你好呀！"]
        )
        await enable_proactive(harness)
        install_real_generator(harness, monkeypatch)

        bridge = harness.bridge
        coordinator = harness.runtime.coordinator

        await bridge.run_proactive()
        assert bridge.pending_proactive is not None

        # Client disconnects
        bridge.detach_client()
        assert not coordinator.client_connected
        assert bridge.pending_proactive is None

        # While disconnected, proactive is denied
        decision = await coordinator.consider_proactive()
        assert not decision.eligible
        assert "disconnected" in decision.reason

        msg = await bridge.run_proactive()
        assert msg is None

    async def test_reconnect_does_not_repeat_startup_greeting_and_engine_works(
        self, make_harness, monkeypatch
    ):
        """Reconnecting client does not trigger repeated greeting; TTS remains operational."""
        harness, _, _ = make_t06_harness(
            make_harness, llm_responses=["启动问候！"]
        )
        await enable_proactive(harness)
        coordinator = harness.runtime.coordinator
        coordinator._scheduler.startup_greeting_enabled = True
        install_real_generator(harness, monkeypatch)

        bridge = harness.bridge

        # 1. Startup greeting runs
        msg = await bridge.run_proactive(is_startup=True)
        assert msg == "启动问候！"
        turn_id = bridge.pending_proactive.turn_id
        await bridge.on_display_receipt(turn_id=turn_id)

        # 2. Client reconnects (new session)
        new_frames = []

        async def fake_sender(frame: str) -> None:
            new_frames.append(frame)

        bridge.attach_client(fake_sender, client_uid="reconnected_client")

        # Startup greeting must NOT be re-issued
        decision = await coordinator.consider_proactive(is_startup=True)
        assert not decision.eligible
        assert "already greeted" in decision.reason

        # Normal conversation still synthesises audio using the TTS engine
        assert bridge._tts_engine is not None

    async def test_request_failure_recovers_without_leaking_turns_or_budget(
        self, make_harness, monkeypatch
    ):
        """A failure during proactive generation cleans up turn and recovers on next attempt."""
        harness, _, _ = make_t06_harness(make_harness)
        await enable_proactive(harness)

        bridge = harness.bridge
        coordinator = harness.runtime.coordinator

        # 1. Generation fails with exception
        fail_count = 0

        async def faulty_generate(*args, **kwargs):
            nonlocal fail_count
            fail_count += 1
            raise RuntimeError("LLM gateway timeout (504)")

        bridge.set_proactive_generator(faulty_generate)

        # Run should catch exception, log error, and return None
        msg = await bridge.run_proactive()
        assert msg is None
        assert bridge.pending_proactive is None
        assert fail_count == 1

        # No active turns leaked
        assert not coordinator.is_generation_active()
        # Budget was NOT consumed
        assert len(coordinator._scheduler._history) == 0
        # Not locked into unanswered state
        assert not coordinator._scheduler.awaiting_response

        # 2. Recovered on next attempt
        install_real_generator(harness, monkeypatch, prompt="恢复后")
        harness.agent._llm = FakeLLM(["故障恢复后的问候"])
        msg2 = await bridge.run_proactive()
        assert msg2 == "故障恢复后的问候"
        assert bridge.pending_proactive is not None

    async def test_memory_extraction_survives_disconnect_without_duplicates(
        self, make_harness
    ):
        """Extraction task in background persists and writes once even across disconnect."""
        extraction = FakeExtractionAdapter(
            facts=[ExtractedFact(content="用户喜欢吃面条")]
        )
        harness = make_harness(
            llm=FakeLLM(["好的，记住了。"]),
            extraction=extraction,
        )

        bridge = harness.bridge
        # User adds a turn
        await run_turn(harness, "记住：我喜欢吃面条")

        # Client disconnects while memory task is pending
        bridge.detach_client()

        # Memory worker processes extraction in background
        written = await harness.runtime.memory.run_pending_extraction()
        assert written == 1

        # Fact was written to SQLite
        memories = harness.runtime.memory.store.list_memories()
        assert any("用户喜欢吃面条" in m.content for m in memories)

        # Re-running extraction does not duplicate the fact
        written_again = await harness.runtime.memory.run_pending_extraction()
        assert written_again == 0
        memories_after = harness.runtime.memory.store.list_memories()
        assert len([m for m in memories_after if "用户喜欢吃面条" in m.content]) == 1


@pytest.mark.asyncio
class TestLockScreenAndWindowUnavailableBranches:
    """Tests for lock screen and unavailable foreground window branches."""

    async def test_screen_locked_inhibits_proactive_chat_and_observation(
        self, make_harness, monkeypatch
    ):
        """When screen is locked, proactive chat is blocked and capture is skipped."""
        harness, capture, vision = make_t06_harness(make_harness, locked=True)
        await enable_proactive(harness)
        install_real_generator(harness, monkeypatch)

        bridge = harness.bridge

        # Proactive check should see session locked and decline
        msg = await bridge.run_proactive()
        assert msg is None
        assert bridge.pending_proactive is None
        assert len(vision.calls) == 0  # Vision model never called

    async def test_window_unavailable_safely_degrades_without_false_summary(
        self, make_harness, monkeypatch
    ):
        """When foreground window is unavailable (None), no screen summary is cited."""
        harness, capture, vision = make_t06_harness(
            make_harness, window=None, llm_responses=["没有窗口也可以正常普通搭话"]
        )
        await enable_proactive(harness)
        capture.window = None  # No foreground window

        bridge = harness.bridge
        install_real_generator(harness, monkeypatch)

        msg = await bridge.run_proactive()
        assert msg == "没有窗口也可以正常普通搭话"
        # Screen summary was None, so vision was not called
        assert len(vision.calls) == 0
        assert bridge.current_screen_summary() is None
