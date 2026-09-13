"""Integration tests: proactive chat and screen observation.

Both must enter through the bridge so the coordinator stays the single
arbitration point. The plan's scenarios covered here:

* repeated proactive signals are governed by the cooldown and hourly limit, and
  an unanswered message stops further proactive speech;
* user activity invalidates a proactive candidate that is still generating;
* a proactive message is not counted as sent until the client confirms display;
* a vision result that arrives after observation was switched off is discarded,
  and a failed observation does not block retrying the same screen.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.interfaces import SpeechMode
from tests.doubles import FakeScreenCapture, FakeVision, FakeWindow
from tests.integration.harness import Barrier


class SlowExtraction:
    """Extraction adapter that blocks, used to hold a proactive candidate."""

    def __init__(self, barrier: Barrier, text: str = "主动说的话。") -> None:
        self._barrier = barrier
        self._text = text
        self.entered = asyncio.Event()

    async def extract(self, messages):
        self.entered.set()
        await self._barrier.wait()
        return []


class TestProactiveGating:
    """The scheduler, not the client signal, decides eligibility."""

    async def test_proactive_denied_when_disabled(self, make_harness):
        """Proactive must be refused while the switch is off."""
        harness = make_harness()
        decision = await harness.bridge._coordinator.consider_proactive()
        assert decision.eligible is False
        assert "disabled" in decision.reason

    async def test_client_signal_only_requests_check(self, make_harness):
        """An ai-speak-signal must not start a conversation by itself."""
        harness = make_harness()
        result = await harness.bridge.request_proactive()
        # The bridge answers with an eligibility result and never generates.
        assert result is None
        decisions = harness.websocket.frames_of("aemeath-proactive-decision")
        assert decisions, "the client must be told the eligibility outcome"
        assert decisions[-1]["eligible"] is False

    async def test_ai_speak_signal_does_not_start_conversation(self, make_harness):
        """The upstream handler must not be reached for the Aemeath agent.

        Before this was wired, ``ai-speak-signal`` still routed to upstream's
        ``_handle_conversation_trigger``, which generates a reply directly.
        That made the client the source of proactive turns and bypassed every
        scheduler rule.
        """
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        harness.websocket.clear()

        from src.open_llm_vtuber.websocket_handler import WebSocketHandler

        sent: list = []

        async def _handle_conversation_trigger(*args, **kwargs):
            """Record that the bypass path was taken."""
            sent.append("conversation-trigger")

        handler = WebSocketHandler.__new__(WebSocketHandler)
        handler.client_contexts = {"test-client": harness.context}
        handler._handle_conversation_trigger = _handle_conversation_trigger

        await handler._handle_ai_speak_signal(
            harness.websocket, "test-client", {"type": "ai-speak-signal"}
        )

        assert sent == [], (
            "ai-speak-signal must not reach upstream's conversation trigger"
        )

    async def test_scheduler_drives_proactive_without_a_client_signal(self, make_harness):
        """The backend scheduler alone can produce a proactive message.

        Nothing calls ``run_proactive`` from a test here: the bridge is given a
        generator and asked the way the runtime's timer asks, which is the only
        path that exists in production.
        """
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        harness.websocket.clear()

        calls: list = []

        async def _generate(is_startup: bool) -> str:
            """Stand in for the upstream model call."""
            calls.append(is_startup)
            return "主动来一句。"

        harness.bridge.set_proactive_generator(_generate)
        message = await harness.bridge.run_proactive()

        assert message == "主动来一句。", "the scheduler must be able to speak"
        assert calls == [False]
        texts = harness.websocket.frames_of("aemeath-text")
        assert texts and "主动来一句" in texts[-1]["text"]

    async def test_run_proactive_without_generator_is_a_noop(self, make_harness):
        """A missing generator must not raise or claim success."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        assert await harness.bridge.run_proactive() is None

    async def test_startup_greeting_is_attempted_once(self, make_harness):
        """The startup greeting must not repeat within a session."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)

        async def _generate(is_startup: bool) -> str:
            """Return a greeting candidate."""
            return "早上好。"

        harness.bridge.set_proactive_generator(_generate)

        first = await harness.bridge.maybe_startup_greeting()
        second = await harness.bridge.maybe_startup_greeting()

        assert first == "早上好。"
        assert second is None, "the greeting must be one-shot"

    async def test_cooldown_blocks_second_message(self, make_harness):
        """Two immediate proactive messages are not allowed."""
        harness = make_harness(
            aemeath_overrides={"proactive": {"cooldown_seconds": 900, "max_per_hour": 5}}
        )
        await harness.bridge.set_switch("proactive", True)

        coordinator = harness.bridge._coordinator
        first = await coordinator.consider_proactive()
        assert first.eligible is True

        harness.bridge._coordinator.scheduler.mark_spoken()
        second = await coordinator.consider_proactive()
        assert second.eligible is False
        assert "cooldown" in second.reason

    async def test_hourly_limit_blocks_repeated_messages(self, make_harness):
        """The hourly cap holds even after the cooldown has passed."""
        harness = make_harness(
            aemeath_overrides={"proactive": {"cooldown_seconds": 1, "max_per_hour": 2}}
        )
        await harness.bridge.set_switch("proactive", True)
        scheduler = harness.bridge._coordinator.scheduler

        import time

        now = time.time()
        # Two messages inside the rolling hour, each far enough apart to clear
        # the one-second cooldown. Both must still be inside the hour window,
        # otherwise they age out of the rolling count.
        scheduler.mark_spoken(now - 1800)
        scheduler.mark_user_spoke()
        scheduler.mark_spoken(now - 600)
        scheduler.mark_user_spoke()

        decision = scheduler.check(situation=harness.runtime.situation.state, now=now)
        assert decision.eligible is False
        assert "limit" in decision.reason

    async def test_unanswered_message_stops_further_proactive(self, make_harness):
        """After an unanswered message, Aemeath stays quiet."""
        harness = make_harness(
            aemeath_overrides={"proactive": {"cooldown_seconds": 1, "max_per_hour": 5}}
        )
        await harness.bridge.set_switch("proactive", True)
        scheduler = harness.bridge._coordinator.scheduler

        import time

        now = time.time()
        scheduler.mark_spoken(now - 10)  # spoken, then five minutes pass

        decision = scheduler.check(
            situation=harness.runtime.situation.state, now=now
        )
        assert decision.eligible is False
        assert "unanswered" in decision.reason

        # The user replying clears the block.
        scheduler.mark_user_spoke()
        assert scheduler.awaiting_response is False

    async def test_user_typing_invalidates_candidate(self, make_harness):
        """Typing while a proactive candidate is pending discards it."""
        harness = make_harness()
        harness.bridge.pending_proactive = type(
            "P", (), {"turn_id": "t1", "text": "候选", "sent_at": 0.0}
        )()

        await harness.bridge.on_client_activity(typing=True)
        assert harness.bridge.pending_proactive is None

    async def test_voice_activity_invalidates_candidate(self, make_harness):
        """Speaking while a proactive candidate is pending discards it."""
        harness = make_harness()
        harness.bridge.pending_proactive = type(
            "P", (), {"turn_id": "t1", "text": "候选", "sent_at": 0.0}
        )()

        await harness.bridge.on_client_activity(voice_active=True)
        assert harness.bridge.pending_proactive is None

    async def test_candidate_held_until_display_receipt(self, make_harness):
        """A delivered candidate is not counted as sent before the receipt."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        scheduler = harness.bridge._coordinator.scheduler
        assert scheduler.awaiting_response is False

        delivered = await harness.bridge.deliver_proactive("你好呀。", turn_id="turn-1")
        assert delivered is True
        assert harness.bridge.pending_proactive is not None
        # Not yet counted: the client has not confirmed it appeared.
        assert scheduler.awaiting_response is False

        await harness.bridge.on_display_receipt(turn_id="turn-1")
        assert harness.bridge.pending_proactive is None
        assert scheduler.awaiting_response is True

    async def test_second_candidate_blocked_while_pending(self, make_harness):
        """A pending candidate prevents a duplicate from being generated."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)

        await harness.bridge.deliver_proactive("第一句。", turn_id="turn-1")
        repeated = await harness.bridge.request_proactive()
        assert repeated is None

    async def test_no_retry_after_disconnect(self, make_harness):
        """A dropped connection must not auto-resend a pending candidate."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        await harness.bridge.deliver_proactive("你好呀。", turn_id="turn-1")

        harness.bridge.detach_client()
        assert harness.bridge.pending_proactive is None
        assert harness.bridge.session is None

    async def test_proactive_text_only_in_class_mode(self, make_harness):
        """Class mode allows proactive text but never proactive audio."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        await harness.bridge.deliver_proactive("上课时的小提示。", turn_id="turn-class")

        assert harness.websocket.frames_of("aemeath-text"), "proactive text is allowed"
        assert harness.websocket.frames_of("audio") == [], "proactive audio is not"


class TestScreenObservation:
    """Observation must respect the generation and the switches."""

    def _screen_harness(self, make_harness, *, vision, capture):
        return make_harness(
            vision=vision,
            capture=capture,
            aemeath_overrides={"screen": {"min_interval_seconds": 0}},
        )

    async def test_manual_request_returns_summary(self, make_harness):
        """A manual request captures and reports a summary.

        Observation is switched on first: ``force`` skips the throttling gates,
        not the on/off switch, and a request with observation off must produce
        nothing at all (see ``test_screen_lifecycle.py``).
        """
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        observation = await harness.bridge.on_screen_request(force=True)

        assert observation is not None
        summary = harness.websocket.last_of("aemeath-screen-summary")
        assert summary is not None
        assert "写代码" in summary["summary"]
        assert summary["window_title"] == "编辑器"

    async def test_vision_result_discarded_after_observation_disabled(
        self, make_harness
    ):
        """A summary that returns after the switch was turned off is dropped.

        Asserts the observer cache as well as the outbound frame. Refusing to
        send the frame is not enough on its own: the defect this guards was
        exactly that the bridge declined to forward the summary while the
        observer still cached it, so a frame-only assertion stayed green.
        """
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        vision = SlowVision("迟到的摘要。")
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        request = asyncio.create_task(harness.bridge.on_screen_request(force=True))
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)

        # The user turns observation off while the vision call is in flight.
        await harness.bridge.set_switch("screen", False)
        vision.release()

        result = await asyncio.wait_for(request, timeout=5.0)
        assert result is None, "a stale observation must not be written back"
        assert harness.websocket.frames_of("aemeath-screen-summary") == []
        assert harness.runtime.screen.current_summary() is None, (
            "the observer must not cache a summary from a period the user left"
        )
        assert harness.runtime.screen.latest is None

    async def test_disabling_clears_current_summary(self, make_harness):
        """Turning observation off forgets the cached summary."""
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)
        assert harness.runtime.screen.current_summary() is not None

        await harness.bridge.set_switch("screen", False)
        assert harness.runtime.screen.current_summary() is None

    async def test_failed_vision_does_not_block_retry(self, make_harness):
        """A failed observation must not poison the dedup cache."""
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("第二次成功了。")
        vision.fail = True
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        first = await harness.bridge.on_screen_request(force=True)
        assert first is None, "the first attempt fails"

        # The same picture is retried; it must not be skipped as a duplicate.
        vision.fail = False
        second = await harness.bridge.on_screen_request(force=True)
        assert second is not None, "the same screen must be retryable after a failure"
        assert "第二次成功了" in second.summary

    async def test_unavailable_screen_reports_reason(self, make_harness):
        """With no capture backend the client is told why, not left guessing."""
        harness = make_harness()
        harness.runtime.screen = None
        harness.bridge._screen = None
        harness.websocket.clear()

        await harness.bridge.on_screen_request(force=True)

        unavailable = harness.websocket.last_of("aemeath-screen-unavailable")
        assert unavailable is not None
        assert unavailable["reason"]

    async def test_locked_session_refuses_observation(self, make_harness):
        """A locked session produces no summary."""
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        capture.locked = True
        vision = FakeVision("不该出现。")
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)
        harness.websocket.clear()

        observation = await harness.bridge.on_screen_request(force=True)
        assert observation is None
        assert harness.websocket.frames_of("aemeath-screen-summary") == []

    async def test_summary_not_saved_as_user_fact(self, make_harness):
        """An ordinary screen summary never becomes a permanent memory."""
        capture = FakeScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = self._screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)

        assert harness.runtime.memory.store.list_memories() == []


class SlowVision(FakeVision):
    """Vision double that waits until released."""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.entered = asyncio.Event()
        self._release = asyncio.Event()

    def release(self) -> None:
        """Allow the pending describe call to finish."""
        self._release.set()

    async def describe(self, image: bytes, window_title: str = "") -> str:
        self.calls.append(image)
        self.entered.set()
        await self._release.wait()
        return self.summary
