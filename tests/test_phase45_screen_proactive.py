"""Phase 4 and 5 tests: screen observation and proactive scheduling.

Screen checks (plan):
* observation off means no capture requests at all
* capture is throttled and requires a stable window
* a stale summary is not presented as current
* failure reports honestly and never reuses an old image
* window minimized / unavailable is reported rather than widened to desktop

Proactive checks (plan):
* cooldown and hourly limits
* no speaking while the user is active
* an unanswered message pauses proactive chat
* user input beats a proactive candidate
* classroom mode never voices a proactive message
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.coordinator import CoordinatorHooks, EventCoordinator
from aemeath.interfaces import EventSource, ScreenObservation, SpeechMode, SituationState
from aemeath.proactive import ProactiveScheduler
from aemeath.screen import ScreenObserver, WindowInfo
from aemeath.situation import SituationManager, SituationStore
from tests.doubles import FakeClock, FakeScreenCapture, FakeWindow, FakeVision


@pytest.fixture
def manager(tmp_path):
    """Situation manager over a temporary database."""
    return SituationManager(SituationStore(tmp_path / "aemeath.sqlite3"))


def make_observer(vision=None, backend=None, **kwargs):
    """Build a ScreenObserver with doubles, observation switched on.

    The observer refuses to capture while it is off, so a unit test about
    capture, throttling or caching has to enable it first. The switch itself is
    exercised through the bridge, which is where it belongs.
    """
    params = {"max_edge_px": 1600, "min_interval_seconds": 60, "min_stable_seconds": 10}
    params.update(kwargs)
    observer = ScreenObserver(
        backend or FakeScreenCapture(FakeWindow("main.py - VS Code")),
        vision or FakeVision(),
        **params,
    )
    observer.set_enabled(True)
    return observer


class TestScreenObservation:
    """Screen awareness bounds."""

    async def test_forced_observation_succeeds(self):
        vision = FakeVision("用户正在编辑 main.py。")
        observer = make_observer(vision=vision)
        observation = await observer.observe(force=True)
        assert observation is not None
        assert "main.py" in observation.summary
        assert observation.window_title == "main.py - VS Code"

    async def test_observation_is_throttled(self):
        backend = FakeScreenCapture(FakeWindow("doc.txt"))
        observer = make_observer(backend=backend)
        assert await observer.observe(force=True) is not None
        # A second automatic attempt within the interval is skipped.
        assert await observer.observe() is None
        assert backend.capture_count == 1

    async def test_window_must_be_stable(self):
        observer = make_observer(backend=FakeScreenCapture(FakeWindow("a")))
        # First automatic attempt only records the window, not a capture.
        assert await observer.observe() is None

    async def test_no_foreground_window_reports_failure(self):
        backend = FakeScreenCapture(None)
        observer = make_observer(backend=backend)
        assert await observer.observe(force=True) is None
        assert backend.capture_count == 0, "must not capture without a window"

    async def test_capture_failure_does_not_reuse_old_image(self):
        vision = FakeVision()
        backend = FakeScreenCapture(FakeWindow("editor"))
        observer = make_observer(vision=vision, backend=backend)

        first = await observer.observe(force=True)
        assert first is not None

        backend.fail = True
        second = await observer.observe(force=True)
        assert second is None, "a failed capture must not return the old summary"
        assert len(vision.calls) == 1, "vision must not be called on a failed capture"

    async def test_vision_failure_returns_none(self):
        vision = FakeVision()
        vision.fail = True
        observer = make_observer(vision=vision)
        assert await observer.observe(force=True) is None
        assert observer.latest is None

    async def test_locked_session_skips_capture(self):
        backend = FakeScreenCapture(FakeWindow("desktop"))
        backend.is_locked = lambda: True
        observer = make_observer(backend=backend)
        assert await observer.observe(force=True) is None
        assert backend.capture_count == 0

    async def test_identical_image_not_reanalysed(self):
        vision = FakeVision()
        observer = make_observer(vision=vision)
        await observer.observe(force=True)
        # Same bytes again: the automatic path should skip the vision call.
        await observer.observe()
        assert len(vision.calls) == 1

    async def test_stale_summary_not_current(self):
        observer = make_observer(summary_max_age_seconds=120)
        observation = await observer.observe(force=True)
        assert observation is not None

        # Still fresh.
        assert observer.current_summary(observation.captured_at + 60) is not None
        # Older than the limit.
        assert observer.current_summary(observation.captured_at + 300) is None

    async def test_reset_clears_cached_image(self):
        observer = make_observer()
        await observer.observe(force=True)
        observer.reset()
        assert observer._last_image is None
        assert observer._last_image_hash == ""

    async def test_downscale_bounds_longest_edge(self):
        """Images larger than the limit are shrunk; smaller ones pass through."""
        try:
            import io

            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")

        buffer = io.BytesIO()
        Image.new("RGB", (4000, 1000)).save(buffer, format="PNG")
        big = buffer.getvalue()

        scaled = ScreenObserver._downscale(big, 1600)
        with Image.open(io.BytesIO(scaled)) as img:
            assert max(img.size) <= 1600

        buffer = io.BytesIO()
        Image.new("RGB", (800, 600)).save(buffer, format="PNG")
        small = buffer.getvalue()
        assert ScreenObserver._downscale(small, 1600) == small


class TestScreenObservationOff:
    """Observation off means no capture happens."""

    async def test_no_capture_when_disabled(self, manager):
        backend = FakeScreenCapture(FakeWindow("anything"))
        observer = make_observer(backend=backend)

        manager.set_screen_observation(False)
        if manager.state.screen_observation_enabled:
            await observer.observe(force=True)

        assert backend.capture_count == 0

    async def test_state_reflects_switch(self, manager):
        assert not manager.state.screen_observation_enabled
        manager.set_screen_observation(True)
        assert manager.state.screen_observation_enabled


class TestProactiveRules:
    """Eligibility rules for starting a topic."""

    def make_scheduler(self, **kwargs):
        """Scheduler with test-friendly limits."""
        params = {"cooldown_seconds": 900, "max_per_hour": 2}
        params.update(kwargs)
        return ProactiveScheduler(**params)

    def enabled_state(self) -> SituationState:
        """A situation in which proactive chat is switched on."""
        return SituationState(proactive_enabled=True)

    def test_disabled_by_default(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(situation=SituationState())
        assert not decision.eligible
        assert "disabled" in decision.reason

    def test_eligible_when_enabled(self):
        scheduler = self.make_scheduler()
        assert scheduler.check(situation=self.enabled_state()).eligible

    def test_blocked_while_microphone_active(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(situation=self.enabled_state(), mic_active=True)
        assert not decision.eligible

    def test_blocked_while_user_typing(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(situation=self.enabled_state(), user_typing=True)
        assert not decision.eligible

    def test_blocked_during_generation(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(
            situation=self.enabled_state(), generation_active=True
        )
        assert not decision.eligible

    def test_blocked_when_disconnected(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(
            situation=self.enabled_state(), client_connected=False
        )
        assert not decision.eligible

    def test_blocked_when_locked(self):
        scheduler = self.make_scheduler()
        decision = scheduler.check(situation=self.enabled_state(), screen_locked=True)
        assert not decision.eligible

    def test_blocked_when_user_paused(self):
        scheduler = self.make_scheduler()
        state = SituationState(proactive_enabled=True, user_paused=True)
        assert not scheduler.check(situation=state).eligible

    def test_cooldown_enforced(self):
        scheduler = self.make_scheduler(cooldown_seconds=900)
        clock = FakeClock()
        scheduler.mark_spoken(clock.now())
        # The user answers, which clears the unanswered rule; only the cooldown
        # should then hold proactive chat back.
        scheduler.mark_user_spoke()

        early = scheduler.check(situation=self.enabled_state(), now=clock.now() + 60)
        assert not early.eligible
        assert "cooldown" in early.reason

        clock.advance(1000)
        assert scheduler.check(
            situation=self.enabled_state(), now=clock.now()
        ).eligible

    def test_cooldown_then_unanswered_still_blocks(self):
        """Even past the cooldown, an unanswered message keeps Aemeath quiet."""
        scheduler = self.make_scheduler(cooldown_seconds=900)
        clock = FakeClock()
        scheduler.mark_spoken(clock.now())

        clock.advance(1000)
        decision = scheduler.check(situation=self.enabled_state(), now=clock.now())
        assert not decision.eligible
        assert "unanswered" in decision.reason

    def test_hourly_limit_enforced(self):
        scheduler = self.make_scheduler(cooldown_seconds=1, max_per_hour=2)
        base = 1_000_000.0
        scheduler.mark_spoken(base)
        scheduler.mark_user_spoke()
        scheduler.mark_spoken(base + 10)
        scheduler.mark_user_spoke()

        decision = scheduler.check(
            situation=self.enabled_state(), now=base + 20
        )
        assert not decision.eligible
        assert "hourly" in decision.reason

    def test_hourly_limit_expires(self):
        scheduler = self.make_scheduler(cooldown_seconds=1, max_per_hour=2)
        base = 1_000_000.0
        scheduler.mark_spoken(base)
        scheduler.mark_user_spoke()
        scheduler.mark_spoken(base + 10)
        scheduler.mark_user_spoke()

        later = base + 4000.0
        assert scheduler.check(situation=self.enabled_state(), now=later).eligible

    def test_unanswered_pauses_proactive(self):
        """The plan: after an unanswered message, stay quiet until the user returns."""
        scheduler = self.make_scheduler(cooldown_seconds=1)
        scheduler.mark_spoken(1_000_000.0)

        decision = scheduler.check(situation=self.enabled_state(), now=1_000_100.0)
        assert not decision.eligible
        assert "unanswered" in decision.reason

        # The user comes back.
        scheduler.mark_user_spoke()
        assert scheduler.check(
            situation=self.enabled_state(), now=1_000_200.0
        ).eligible

    def test_startup_greeting_only_once(self):
        scheduler = self.make_scheduler()
        first = scheduler.check(situation=self.enabled_state(), is_startup=True)
        assert first.eligible
        scheduler.mark_greeted()
        second = scheduler.check(situation=self.enabled_state(), is_startup=True)
        assert not second.eligible

    def test_startup_greeting_can_be_disabled(self):
        scheduler = self.make_scheduler(startup_greeting_enabled=False)
        assert not scheduler.check(
            situation=self.enabled_state(), is_startup=True
        ).eligible

    def test_class_mode_forbids_voice_but_allows_text(self):
        scheduler = self.make_scheduler()
        class_state = SituationState(
            proactive_enabled=True, mode=SpeechMode.CLASS
        )
        assert not scheduler.may_speak(class_state)
        assert scheduler.check(situation=class_state).eligible

    def test_next_eligible_at_none_while_unanswered(self):
        scheduler = self.make_scheduler()
        scheduler.mark_spoken(1_000_000.0)
        assert scheduler.next_eligible_at(1_000_100.0) is None


class TestCoordinator:
    """Single-turn arbitration and output gating."""

    def make_coordinator(self, manager, **kwargs):
        """Coordinator with recording hooks."""
        self.events = []
        hooks = CoordinatorHooks(
            on_display_text=lambda tid, text: self._record("text", tid, text),
            # The speak hook carries the event source as well, so the bridge can
            # tell an ordinary reply from a self-initiated message.
            on_speak=lambda tid, text, source=None: self._record("speak", tid, text),
            on_cancel_audio=lambda tid: self._record("cancel", tid, ""),
            on_cancel_all_audio=lambda: self._record("cancel_all", "", ""),
        )
        scheduler = ProactiveScheduler(cooldown_seconds=1, max_per_hour=5)
        scheduler._history.clear()
        # start from an eligible state
        manager.set_proactive(True)
        return EventCoordinator(situation=manager, scheduler=scheduler, hooks=hooks)

    async def _record(self, kind, turn_id, text):
        self.events.append((kind, str(turn_id), text))

    async def test_emit_text_delivered(self, manager):
        coord = self.make_coordinator(manager)
        turn = coord.begin_turn(EventSource.USER_TEXT)
        assert await coord.emit_text(turn.turn_id, "你好") is True

    async def test_cancelled_turn_text_dropped(self, manager):
        coord = self.make_coordinator(manager)
        first = coord.begin_turn(EventSource.USER_TEXT)
        coord.interrupt("听到的")
        assert await coord.emit_text(first.turn_id, "迟到的内容") is False

    async def test_new_turn_supersedes_old(self, manager):
        coord = self.make_coordinator(manager)
        first = coord.begin_turn(EventSource.USER_TEXT)
        second = coord.begin_turn(EventSource.USER_TEXT)
        assert coord.is_cancelled(first.turn_id)
        assert not coord.is_cancelled(second.turn_id)

    async def test_speech_suppressed_in_class_mode(self, manager):
        coord = self.make_coordinator(manager)
        manager.set_mode(SpeechMode.CLASS)
        turn = coord.begin_turn(EventSource.USER_TEXT)
        assert await coord.emit_speech(turn.turn_id, "这段话不该被朗读") is False
        assert not any(e[0] == "speak" for e in self.events)

    async def test_speech_allowed_in_normal_mode(self, manager):
        coord = self.make_coordinator(manager)
        turn = coord.begin_turn(EventSource.USER_TEXT)
        assert await coord.emit_speech(turn.turn_id, "可以朗读") is True

    async def test_speech_rechecked_at_playback(self, manager):
        """Mode changed after generation must still suppress audio."""
        coord = self.make_coordinator(manager)
        turn = coord.begin_turn(EventSource.USER_TEXT)
        # Generation produced text while still in normal mode.
        await coord.emit_text(turn.turn_id, "生成中的文本")
        # User switches to class before playback.
        manager.set_mode(SpeechMode.CLASS)
        assert await coord.emit_speech(turn.turn_id, "生成中的文本") is False

    async def test_interrupt_cancels_audio(self, manager):
        """Cancellation is awaitable, so it is ordered rather than raced.

        This previously asserted after ``await asyncio.sleep(0.01)`` and so
        only proved that *some* task eventually ran. Awaiting the drain makes
        the cancellation a completed fact before the assertion.
        """
        coord = self.make_coordinator(manager)
        turn = coord.begin_turn(EventSource.USER_TEXT)
        coord.interrupt("听到一半")
        drained = await coord.drain_cancellations()
        assert drained >= 1, "the interrupt must have scheduled a cancellation"
        assert any(e[0] == "cancel" for e in self.events)
        assert coord._pending_cancels == [], "no cancellation may be left detached"

    async def test_class_switch_awaits_cancellation_of_queued_audio(self, manager):
        """A message that switches to class mode clears audio before returning."""
        coord = self.make_coordinator(manager)
        coord.begin_turn(EventSource.USER_TEXT)

        ack = await coord.observe_user_text_ordered("我在上课")

        assert ack is not None, "the mode switch should be acknowledged"
        assert manager.state.mode is SpeechMode.CLASS
        assert any(e[0] == "cancel_all" for e in self.events), (
            "queued audio must be cleared as part of the mode switch"
        )
        assert coord._pending_cancels == [], (
            "the cancellation must not be left as a detached task"
        )

    async def test_drain_cancellations_is_idempotent(self, manager):
        """Draining twice must not re-run or fail on already-finished work."""
        coord = self.make_coordinator(manager)
        coord.begin_turn(EventSource.USER_TEXT)
        coord.interrupt("听到一半")

        assert await coord.drain_cancellations() >= 1
        assert await coord.drain_cancellations() == 0

    async def test_user_activity_clears_unanswered(self, manager):
        coord = self.make_coordinator(manager)
        coord._scheduler.mark_spoken()
        assert coord._scheduler.awaiting_response
        coord.notify_user_activity()
        assert not coord._scheduler.awaiting_response


class TestProactiveTurn:
    """Proactive turns through the coordinator."""

    def make_coordinator(self, manager):
        """Coordinator with no-op hooks."""
        manager.set_proactive(True)
        scheduler = ProactiveScheduler(cooldown_seconds=1, max_per_hour=5)
        return EventCoordinator(situation=manager, scheduler=scheduler)

    async def test_proactive_message_emitted(self, manager):
        coord = self.make_coordinator(manager)
        spoken = []

        async def generate():
            return "在看论文？"

        coord._hooks.on_display_text = lambda t, x: _noop()
        coord._hooks.on_speak = lambda t, x, source=None: _record_speak(spoken, x)
        result = await coord.run_proactive(generate)
        assert result == "在看论文？"

    async def test_silence_marker_means_no_message(self, manager):
        coord = self.make_coordinator(manager)

        async def generate():
            return "[SILENCE]"

        assert await coord.run_proactive(generate) is None

    async def test_user_typing_during_generation_discards_candidate(self, manager):
        """User input arriving during generation wins."""
        coord = self.make_coordinator(manager)

        async def generate():
            # The user starts typing while the model is thinking.
            coord.user_typing = True
            return "打扰一下"

        assert await coord.run_proactive(generate) is None

    async def test_not_eligible_when_user_typing(self, manager):
        coord = self.make_coordinator(manager)
        coord.user_typing = True

        async def generate():
            raise AssertionError("must not generate when user is typing")

        assert await coord.run_proactive(generate) is None

    async def test_class_mode_proactive_not_spoken(self, manager):
        coord = self.make_coordinator(manager)
        manager.set_mode(SpeechMode.CLASS)
        spoken = []
        coord._hooks.on_display_text = lambda t, x: _noop()
        coord._hooks.on_speak = lambda t, x, source=None: _record_speak(spoken, x)

        async def generate():
            return "上课时的一条文字"

        result = await coord.run_proactive(generate)
        assert result == "上课时的一条文字", "text must still be sent"
        assert spoken == [], "but it must not be spoken"

    async def test_generation_failure_is_contained(self, manager):
        coord = self.make_coordinator(manager)

        async def generate():
            raise RuntimeError("provider down")

        assert await coord.run_proactive(generate) is None


async def _noop():
    """Async no-op used as a hook."""
    return None


async def _record_speak(sink, text):
    """Record spoken text into a list."""
    sink.append(text)
