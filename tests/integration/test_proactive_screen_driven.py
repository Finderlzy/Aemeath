"""Integration tests: screen-driven proactive conversation (T03).

Closes architecture-review finding **A04**: automatic screen observation and the
connection into proactive conversation were missing. ``_observe_on()`` only
bumped an observation generation, ``service_context._generate()`` sent only a
proactive prompt text, and ``AemeathAgent._build_messages()`` never read
``current_summary()``. "Screen understanding passes" and "she can open a topic
about the screen" were therefore two different things, and R05 was unmet: a
successful *manual* observation proves nothing about the automatic path.

The entry point under test is production's own chain, not a stand-in:

    runtime._proactive_worker  ->  bridge.run_proactive
      -> upstream ServiceContext._install_aemeath_proactive_generator()
      -> AemeathAgent._build_messages()  ->  the model request

Only the model (``FakeLLM``), the TTS engine and the capture/vision backends are
replaced, exactly as ``tests/integration/harness.py`` documents. The vision
double records every call, so "observation was switched off" can be asserted as
"the vision model was never asked" rather than merely "no frame was sent".

No client signal is sent anywhere in this module: nothing calls
``request_proactive`` or ``ai-speak-signal``. The runtime's own timer is what
asks, which is the only proactive path that exists in production.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.interfaces import SpeechMode
from tests.doubles import FakeLLM, FakeScreenCapture, FakeVision, FakeWindow
from tests.integration.test_proactive_voice import (
    decode_wav,
    enable_proactive,
    install_real_generator,
)

#: What the isolated window shows. The tests assert on this *specific* content,
#: because a generic "she said something" would also pass if the summary never
#: reached the model.
WINDOW_TITLE = "季度预算表.xlsx"
WINDOW_SUMMARY = "用户正在编辑一份季度预算表格，第三列是差旅费用，标题栏写着 2026 Q3。"


def screen_harness(make_harness, *, summary: str = WINDOW_SUMMARY, title: str = WINDOW_TITLE,
                   **overrides):
    """Build a harness with a controllable isolated window.

    No throttling override is applied here. ``_positive_int`` in
    ``aemeath/config.py`` treats ``0`` as invalid and falls back to the default,
    so a zero interval cannot be configured — the gates are real. Tests satisfy
    them the way production does: by ticking until the window counts as stable
    (:func:`prime_observation`) and by advancing the observer's own clocks when
    a specific gate is under test.

    Returns:
        A tuple of (harness, capture, vision) so tests can assert on how many
        times capture and the vision model were actually invoked.
    """
    capture = FakeScreenCapture(FakeWindow(title=title))
    vision = FakeVision(summary)
    aemeath_overrides = {}
    aemeath_overrides.update(overrides.pop("screen", {}))
    harness = make_harness(
        vision=vision, capture=capture, aemeath_overrides=aemeath_overrides, **overrides
    )
    return harness, capture, vision


def model_prompts(llm) -> str:
    """Every message body the model was actually asked with, as one string."""
    chunks = []
    for call in llm.calls:
        for message in call.get("messages", []):
            content = message.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        chunks.append(str(part.get("text", "")))
        chunks.append(str(call.get("system_prompt", "")))
    return "\n".join(chunks)


async def tick(harness, *, ticks: int = 1, interval: float = 0.01):
    """Run the runtime's real proactive worker for a bounded number of ticks.

    The worker sleeps before each attempt, so the interval is shortened and the
    task is cancelled once the requested number of attempts has happened. This
    drives the same code path production's timer drives; it does not call
    ``run_proactive`` directly.

    Note that a single tick can legitimately produce no observation: the
    observer requires the foreground window to be seen twice before it counts as
    stable, so the first check only records the window signature. Tests that
    need an observation therefore warm the observer up first (see
    :func:`prime_observation`) rather than treating the first tick as a failure.
    """
    harness.runtime.config = _with_interval(harness.runtime.config, interval)

    attempts = 0
    original = harness.bridge.run_proactive

    async def _counting(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return await original(*args, **kwargs)

    harness.bridge.run_proactive = _counting
    task = asyncio.create_task(harness.runtime._proactive_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(interval)
            if attempts >= ticks:
                break
        await asyncio.sleep(interval * 2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            harness.bridge.run_proactive = original
    return attempts


async def prime_observation(harness, *, interval: float = 0.01):
    """Make the observer willing to capture, the way production gets there.

    Two gates stand between a proactive attempt and a vision call: the
    foreground window must have been seen for ``min_stable_seconds``, and at
    least ``min_interval_seconds`` must have passed since the last attempt.
    Neither can be configured down to zero (``_positive_int`` rejects it), so
    the gates are satisfied by their own state rather than by disabling them:
    the window signature is registered and backdated, which is exactly the state
    a foreground window reaches after being watched for that long, and the
    attempt clock is backdated the same way.

    This observes nothing itself. It only supplies the elapsed time a real
    desktop would have supplied, so the *next* tick is the one that captures —
    which is what the tests then assert on. No gate is removed: the following
    ``observe()`` still runs every check.
    """
    allow_next_observation(harness)
    await asyncio.sleep(interval)


def allow_next_observation(harness):
    """Backdate the observer's clocks so its gates permit one more capture.

    Used between measured ticks where the previous attempt would otherwise sit
    inside the interval window. The gates themselves are untouched.
    """
    import time as _time

    screen = harness.runtime.screen
    if screen is None:
        return
    now = _time.time()
    window = screen._backend.foreground_window()
    if window is not None:
        screen._window_signature = f"{window.title}|{window.rect}"
        screen._window_since = now - screen.min_stable_seconds - 1
    screen._last_attempt_at = now - screen.min_interval_seconds - 1


def _with_interval(config, interval: float):
    """Return a copy of the config with a shorter proactive tick.

    ``check_interval_seconds`` is the only timer knob; whether Aemeath may speak
    is decided by the situation switch and the scheduler, not by the config, so
    nothing else has to be overridden to make the worker attempt a message.
    """
    import dataclasses

    proactive = dataclasses.replace(config.proactive, check_interval_seconds=interval)
    return dataclasses.replace(config, proactive=proactive)


# ----------------------------------------------------------------------
# Acceptance 1: no client signal, yet the specific window content is used
# ----------------------------------------------------------------------


class TestAutomaticScreenObservation:
    """The runtime timer alone must produce a screen-aware proactive message."""

    async def test_runtime_timer_observes_and_speaks_about_the_window(
        self, make_harness, monkeypatch
    ):
        """The production timer path observes the window and cites its content.

        This is the A04 regression. Before the fix the worker called
        ``bridge.run_proactive()`` which never touched the screen observer, so
        the model was asked to open a topic with no idea what was on screen.
        """
        harness, capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM([f"你还在改那份预算表呀。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        # Warm-up ticks satisfy the window-stability gate the way production
        # does; the tick under measurement is the one that must observe and
        # then ask the model about it.
        await prime_observation(harness)
        capture.capture_count = 0
        harness.agent._llm.calls.clear()

        attempts = await tick(harness, ticks=1)

        assert attempts >= 1, "the runtime worker must attempt a proactive message"
        assert capture.capture_count >= 1, (
            "the runtime must observe on demand before asking the model"
        )
        assert vision.calls, "the vision model must be asked about the window"

        prompt = model_prompts(harness.agent._llm)
        assert WINDOW_SUMMARY in prompt, (
            "the observed summary must reach the model request; otherwise she "
            "cannot refer to what is actually on screen"
        )

    async def test_model_request_carries_the_source_window_title(
        self, make_harness, monkeypatch
    ):
        """The request names the source window, so provenance is not decorative."""
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["看到你在忙。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        install_real_generator(harness, monkeypatch)

        await prime_observation(harness)
        harness.agent._llm.calls.clear()
        await tick(harness, ticks=1)

        prompt = model_prompts(harness.agent._llm)
        assert WINDOW_TITLE in prompt, (
            "the source window title must be part of the prompt, so an "
            "observation cannot be cited without its provenance"
        )

    async def test_no_client_observation_or_speak_signal_is_sent(
        self, make_harness, monkeypatch
    ):
        """Nothing in this path is client-initiated.

        The client only ever *asks* for an eligibility check, and here it is
        never asked at all: the frames the client receives are one-way output.
        """
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["主动来一句。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        await prime_observation(harness)
        await tick(harness, ticks=1)

        types = harness.websocket.types_sent()
        assert "aemeath-screen-summary" not in types, (
            "the automatic path must not depend on the manual screen request"
        )
        assert harness.bridge.pending_proactive is not None, (
            "a candidate must have been produced by the timer alone"
        )


# ----------------------------------------------------------------------
# Acceptance 2: text in normal mode, voice; class mode, text only
# ----------------------------------------------------------------------


class TestScreenDrivenOutputModes:
    """Screen-driven output obeys the same situation rules as any proactive turn."""

    async def test_normal_mode_produces_audio(self, make_harness, monkeypatch):
        """In normal mode the screen-driven message is spoken."""
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["你在改预算表。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        await prime_observation(harness)
        await tick(harness, ticks=1)

        audio = harness.websocket.frames_of("audio")
        assert audio, "a normal-mode screen-driven message must carry audio"
        for frame in audio:
            assert frame.get("audio"), "the audio frame must not be an empty string"
            decode_wav(frame["audio"])

    async def test_class_mode_is_text_only(self, make_harness, monkeypatch):
        """In class mode the same path stays silent but still shows text."""
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["上课呢，先记着。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        await prime_observation(harness)
        # Entering class mode last, so the screen-driven attempt is the one
        # under test rather than the mode-switch confirmation.
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        await tick(harness, ticks=1)

        assert harness.websocket.frames_of("aemeath-text"), (
            "class mode mutes audio, never display"
        )
        assert harness.websocket.frames_of("audio") == [], (
            "class mode must never speak, even for a screen-driven topic"
        )


# ----------------------------------------------------------------------
# Acceptance 3: the switches and validity are checked before and after
# ----------------------------------------------------------------------


class TestValidityChecksAroundGeneration:
    """Observation and summary validity are hard gates, not decoration."""

    async def test_observation_off_means_no_capture_and_no_vision(
        self, make_harness, monkeypatch
    ):
        """With observation off the vision model is never asked."""
        harness, capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["不该出现。"])
        await enable_proactive(harness)
        # Observation is left off on purpose.
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        await tick(harness, ticks=1)

        assert capture.capture_count == 0, (
            "observation off must not capture anything"
        )
        assert vision.calls == [], (
            "observation off must not call the vision model at all"
        )

    async def test_off_switch_means_no_old_summary_is_cited(
        self, make_harness, monkeypatch
    ):
        """A summary cached while on must not be reused after it is off.

        This is the "no stale summary" half of the acceptance criterion: the
        switch is turned off *after* a real observation exists.
        """
        harness, _capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["不该出现。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)
        assert harness.runtime.screen.current_summary() is not None, (
            "precondition: a real summary exists while observation is on"
        )

        await harness.bridge.set_switch("screen", False)
        vision.calls.clear()
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        await tick(harness, ticks=1)

        prompt = model_prompts(harness.agent._llm)
        assert WINDOW_SUMMARY not in prompt, (
            "a summary from before observation was switched off must never "
            "reach the model request"
        )
        assert vision.calls == [], "no new vision call may happen while off"

    async def test_summary_provider_refuses_a_cached_summary_when_off(
        self, make_harness
    ):
        """The provider refuses on its own, not only because the cache was cleared.

        ``set_switch("screen", False)`` also clears the observer's cache, so a
        test that only flips the switch cannot tell *which* of the two
        protections is doing the work. This pins the provider's own guard by
        leaving a summary cached and flipping only the persisted situation
        state — the state the user's switch actually writes.

        Without the provider checking the switch, a summary captured before the
        user turned observation off would be handed to the model as current.
        """
        harness, _capture, _vision = screen_harness(make_harness)
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)

        screen = harness.runtime.screen
        assert screen.current_summary() is not None
        assert harness.bridge.current_screen_summary() is not None, (
            "precondition: the provider offers the summary while observation is on"
        )

        # Flip only the persisted situation state, leaving the observer's cache
        # intact so the provider's own guard is the only thing that can refuse.
        harness.runtime.situation.set_screen_observation(False)

        assert screen.current_summary() is not None, (
            "precondition: the cached summary is still present in the observer"
        )
        assert harness.bridge.current_screen_summary() is None, (
            "the provider must refuse a cached summary once observation is off, "
            "independently of the observer clearing its cache"
        )
        assert harness.bridge.observe_enabled() is False

    async def test_observer_guard_alone_refuses_even_with_switch_on(
        self, make_harness
    ):
        """The observer's own enabled flag is checked too, not just the state.

        The bridge mirrors the switch onto the observer; if the two ever
        disagree, the safer reading (not observing) must win.
        """
        harness, _capture, _vision = screen_harness(make_harness)
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)
        assert harness.bridge.current_screen_summary() is not None

        # The situation still says "on", but the observer says otherwise.
        harness.runtime.screen.set_enabled(False)

        assert harness.bridge.observe_enabled() is False, (
            "both halves of the switch must agree before observation counts as on"
        )
        assert harness.bridge.current_screen_summary() is None

    async def test_summary_without_a_source_window_is_refused(
        self, make_harness, monkeypatch
    ):
        """An observation with no source window must not enter the prompt.

        The requirement names provenance, not just recency: a summary the model
        cannot attribute to a window must not be presented as "what the user is
        doing". This pins that guard directly, because an empty title is
        otherwise indistinguishable from a valid summary at the prompt layer.
        """
        harness, _capture, vision = screen_harness(make_harness, title="")
        harness.agent._llm = FakeLLM(["不该出现。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)

        assert harness.runtime.screen.current_summary() is not None, (
            "precondition: the observer did record a summary"
        )
        assert harness.bridge.current_screen_summary() is None, (
            "a summary with no source window must be refused as unusable"
        )

        install_real_generator(harness, monkeypatch)
        await prime_observation(harness)
        harness.agent._llm.calls.clear()
        await tick(harness, ticks=1)

        prompt = model_prompts(harness.agent._llm)
        assert WINDOW_SUMMARY not in prompt, (
            "an unprovenanced summary must never reach the model request"
        )

    async def test_generation_is_suppressed_when_user_types_midway(
        self, make_harness, monkeypatch
    ):
        """Typing while the model is generating discards the whole candidate.

        The check must happen after generation too, because the user can act
        during an arbitrary-length model call.
        """
        harness, _capture, _vision = screen_harness(make_harness)
        llm = FakeLLM(["我在说这个。"], delay=0.05)
        harness.agent._llm = llm
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        from tests.integration.harness import Barrier

        install_real_generator(harness, monkeypatch)

        started = asyncio.Event()
        real_chat = type(harness.agent).chat

        async def _chat(self, input_data):
            """Signal that generation started, then run the real pipeline."""
            started.set()
            async for item in real_chat(self, input_data):
                yield item

        monkeypatch.setattr(type(harness.agent), "chat", _chat)

        task = asyncio.create_task(harness.bridge.run_proactive())
        await asyncio.wait_for(started.wait(), timeout=5.0)
        # The user starts typing while the model is still generating.
        await harness.bridge.on_client_activity(typing=True)
        result = await asyncio.wait_for(task, timeout=10.0)

        assert result is None, "a candidate must not survive the user's activity"
        assert harness.websocket.frames_of("aemeath-text") == []

    async def test_switching_windows_does_not_cite_the_previous_one(
        self, make_harness, monkeypatch
    ):
        """A new observation replaces the old one; pictures never mix."""
        harness, capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["收到。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)

        await harness.bridge.on_screen_request(force=True)
        first = harness.runtime.screen.current_summary()
        assert first is not None and WINDOW_SUMMARY in first.summary

        # The user switches to a different window.
        capture.window = FakeWindow(title="会议纪要.docx")
        vision.summary = "用户正在读一份会议纪要，提到了周五的评审。"

        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)
        # Two ticks: the first notices the window changed (so it is not yet
        # stable), the second observes it. This is the production behaviour that
        # stops a mid-switch window from being captured.
        await prime_observation(harness)
        harness.agent._llm.calls.clear()
        await tick(harness, ticks=1)

        prompt = model_prompts(harness.agent._llm)
        assert "会议纪要" in prompt, "the current window must be cited"
        assert WINDOW_SUMMARY not in prompt, (
            "the previous window's summary must not leak into the new request"
        )


# ----------------------------------------------------------------------
# Acceptance 4: throttling and no repeated pestering
# ----------------------------------------------------------------------


class TestThrottlingAndPersistence:
    """An unchanged picture is not re-analysed, and silence is respected."""

    async def test_unchanged_screen_is_not_re_analysed(self, make_harness, monkeypatch):
        """Repeated ticks on an identical picture do not re-call vision."""
        harness, capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["看了两次。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        install_real_generator(harness, monkeypatch)

        # Cooldown and hourly cap are opened up so both attempts are genuinely
        # allowed to speak; the gate under test here is the image dedup.
        harness.bridge._coordinator.scheduler.cooldown_seconds = 0
        harness.bridge._coordinator.scheduler.max_per_hour = 10

        # First measured attempt: the window is primed as stable, so this tick
        # observes and therefore calls vision exactly once.
        await prime_observation(harness)
        await tick(harness, ticks=1)
        calls_after_first = len(vision.calls)
        assert calls_after_first == 1, "the first attempt walks the picture once"

        # Second attempt, same picture. The observer's dedup must skip it even
        # though the interval gate is opened again — an unchanged screen is not
        # worth a second vision call, and must not become a second topic.
        allow_next_observation(harness)
        await tick(harness, ticks=1)

        assert len(vision.calls) == calls_after_first, (
            "an unchanged screen must not be re-analysed on the next attempt"
        )

    async def test_unanswered_message_stops_a_second_screen_topic(
        self, make_harness, monkeypatch
    ):
        """After an unanswered topic she stays quiet instead of pestering."""
        harness, _capture, vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["你在忙吗？"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.bridge._coordinator.scheduler.cooldown_seconds = 0
        harness.bridge._coordinator.scheduler.max_per_hour = 10
        install_real_generator(harness, monkeypatch)

        await prime_observation(harness)
        await tick(harness, ticks=1)
        assert harness.bridge.pending_proactive is not None, (
            "precondition: a first topic was produced"
        )

        # The client confirms display; the user never replies.
        await harness.bridge.on_display_receipt(
            turn_id=harness.bridge.pending_proactive.turn_id
        )
        vision.calls.clear()

        await tick(harness, ticks=1)

        assert harness.bridge.pending_proactive is None, (
            "an unanswered message must stop further proactive topics"
        )
        assert vision.calls == [], (
            "she must not even look again while the previous topic is unanswered"
        )


# ----------------------------------------------------------------------
# Acceptance 5: T01/T02 behaviour is preserved on the new path
# ----------------------------------------------------------------------


class TestPreservedBehaviour:
    """The new path must not regress the T01/T02 invariants."""

    async def test_late_vision_result_after_the_switch_off_is_discarded(
        self, make_harness, monkeypatch
    ):
        """A slow vision call that lands after the switch goes off is dropped.

        T02's guarantee: refusing to send is not enough, the observer must not
        cache it either.
        """
        from tests.integration.test_proactive_screen import SlowVision

        capture = FakeScreenCapture(FakeWindow(title=WINDOW_TITLE))
        vision = SlowVision(WINDOW_SUMMARY)
        harness = make_harness(vision=vision, capture=capture)
        # The model is asked to stay silent, so anything that does reach the
        # client would have to come from the discarded summary rather than from
        # an unrelated reply.
        harness.agent._llm = FakeLLM(["[SILENCE]"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        # Satisfy the observer's gates without consuming an observation, so the
        # attempt below is the one that reaches the (blocking) vision call.
        allow_next_observation(harness)

        task = asyncio.create_task(harness.bridge.run_proactive())
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)

        # The user switches observation off while the vision call is in flight.
        await harness.bridge.set_switch("screen", False)
        vision.release()
        result = await asyncio.wait_for(task, timeout=10.0)

        assert harness.runtime.screen.current_summary() is None, (
            "a vision response from the previous generation must not be cached"
        )
        prompt = model_prompts(harness.agent._llm)
        assert WINDOW_SUMMARY not in prompt, (
            "the discarded summary must not reach the model either"
        )
        assert harness.websocket.frames_of("aemeath-text") == []

    async def test_delivery_is_counted_only_on_the_receipt(
        self, make_harness, monkeypatch
    ):
        """T01's counting rule still holds on the screen-driven path."""
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["搭话一句。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        install_real_generator(harness, monkeypatch)

        scheduler = harness.bridge._coordinator.scheduler
        assert scheduler.awaiting_response is False

        await tick(harness, ticks=1)

        assert harness.bridge.pending_proactive is not None
        assert scheduler.awaiting_response is False, (
            "a candidate is not counted as delivered before the display receipt"
        )

        await harness.bridge.on_display_receipt(
            turn_id=harness.bridge.pending_proactive.turn_id
        )
        assert scheduler.awaiting_response is True

    async def test_user_turn_cancels_a_screen_driven_candidate(
        self, make_harness, monkeypatch
    ):
        """A real user turn retires the proactive turn so its audio cannot land."""
        harness, _capture, _vision = screen_harness(make_harness)
        harness.agent._llm = FakeLLM(["我先说。"])
        await enable_proactive(harness)
        await harness.bridge.set_switch("screen", True)
        install_real_generator(harness, monkeypatch)

        await tick(harness, ticks=1)
        pending = harness.bridge.pending_proactive
        assert pending is not None

        turn_id = pending.turn_id
        harness.bridge._coordinator.end_turn(
            __import__("aemeath.interfaces", fromlist=["TurnId"]).TurnId(turn_id)
        )

        assert harness.bridge.may_send_audio(turn_id) is False, (
            "a retired turn must not be able to emit late audio"
        )
