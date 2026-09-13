"""Integration tests: screen observation lifecycle (T02).

Closes architecture-review finding **A03**: ``ScreenObserver.observe()`` wrote
``_latest`` after awaiting the vision model, while ``reset()`` only cleared the
image cache and changed nothing the return path could check. The isolated probe
showed that resetting *during* a vision call still left ``current_summary()``
non-empty — the bridge refusing to send the frame is not the same as the
observer refusing to cache it.

The plan requires all three of these to be asserted together, because each one
alone can stay green while the defect is live:

* how many capture calls were actually made,
* what the observer's cache holds (``current_summary()``),
* what went out on the wire.

Only the foreground window, the PNG bytes and the vision provider are
substituted; the real ``ScreenObserver``, the real bridge and the real client
protocol are used.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.interfaces import SpeechMode
from tests.doubles import FakeScreenCapture, FakeVision, FakeWindow


class PausableVision(FakeVision):
    """Vision double that blocks inside ``describe`` until released.

    The interval under test is "reset happens while the vision call is in
    flight", so the double has to be held open at exactly that point rather
    than approximated with a sleep.
    """

    def __init__(self, summary: str = "用户正在编辑一份文档。") -> None:
        super().__init__(summary)
        self.entered = asyncio.Event()
        self._release = asyncio.Event()

    def release(self) -> None:
        """Allow the pending describe call to finish."""
        self._release.set()

    async def describe(self, image: bytes, window_title: str = "") -> str:
        """Record the call, signal arrival, then wait to be released."""
        self.calls.append(image)
        self.entered.set()
        await self._release.wait()
        if self.fail:
            raise RuntimeError("vision provider unavailable")
        return self.summary


class CountedScreenCapture(FakeScreenCapture):
    """Capture double that counts window lookups as well as grabs.

    A capture attempt that never reaches ``grab`` is still an attempt, so the
    lookup count is what proves "this request must not even start".
    """

    def __init__(self, window=None) -> None:
        super().__init__(window)
        self.lookups = 0

    def foreground_window(self):
        """Count the lookup before delegating."""
        self.lookups += 1
        return super().foreground_window()


def screen_harness(make_harness, *, vision, capture):
    """Build a harness with a real observer and a fast observation interval."""
    return make_harness(
        vision=vision,
        capture=capture,
        aemeath_overrides={"screen": {"min_interval_seconds": 0, "min_stable_seconds": 0}},
    )


def assert_nothing_observed(harness, capture, vision, *, why: str) -> None:
    """Assert the whole lifecycle stayed inert: capture, cache and wire.

    Args:
        harness: The wired stack.
        capture: The capture double.
        vision: The vision double.
        why: Failure message describing which interleaving this checks.
    """
    assert harness.websocket.frames_of("aemeath-screen-summary") == [], (
        f"{why}: a stale summary must not reach the client"
    )
    assert harness.runtime.screen.current_summary() is None, (
        f"{why}: the observer cache must stay empty, not just the outbound frame"
    )
    assert harness.runtime.screen.latest is None, (
        f"{why}: the raw cache must stay empty too"
    )


# ----------------------------------------------------------------------
# A03: the interleaving that the review reproduced
# ----------------------------------------------------------------------


class TestStaleVisionResponse:
    """A response that returns after the generation moved on is discarded."""

    async def test_reset_during_vision_call_leaves_cache_empty(self, make_harness):
        """Reset while the vision call is in flight must not write back.

        This is the exact interleaving the architecture review reproduced: the
        bridge already refuses to forward the frame, so asserting only on the
        outbound frames passes even while the observer keeps a summary it must
        not have.
        """
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = PausableVision("迟到的摘要。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        request = asyncio.create_task(harness.bridge.on_screen_request(force=True))
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)

        # Observation is switched off while the vision call is in flight.
        await harness.bridge.set_switch("screen", False)
        vision.release()

        result = await asyncio.wait_for(request, timeout=5.0)

        assert result is None, "the request must report that nothing was observed"
        assert_nothing_observed(
            harness, capture, vision, why="reset during the vision call"
        )

    async def test_reopening_before_a_stale_response_returns_discards_it(
        self, make_harness
    ):
        """Off-then-on must not let the old response become the current summary.

        Re-enabling observation advances the generation again, so the response
        that belonged to the *previous* on-period can never be presented as
        what the user is doing now.
        """
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = PausableVision("旧一代的摘要。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        request = asyncio.create_task(harness.bridge.on_screen_request(force=True))
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)

        # Off, then on again, all while the first vision call is still running.
        await harness.bridge.set_switch("screen", False)
        await harness.bridge.set_switch("screen", True)
        vision.release()

        result = await asyncio.wait_for(request, timeout=5.0)

        assert result is None, "a response from the previous on-period is stale"
        assert_nothing_observed(
            harness, capture, vision, why="reopened before the stale response returned"
        )

    async def test_reset_while_waiting_does_not_poison_the_next_observation(
        self, make_harness
    ):
        """After a discarded response, a fresh request still works.

        The generation check must discard exactly the stale result, not wedge
        the observer into a state where nothing is ever accepted again.
        """
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = PausableVision("第二代摘要。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        harness.websocket.clear()

        stale = asyncio.create_task(harness.bridge.on_screen_request(force=True))
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)
        await harness.bridge.set_switch("screen", False)
        vision.release()
        assert await asyncio.wait_for(stale, timeout=5.0) is None

        # The user turns observation back on and asks again.
        await harness.bridge.set_switch("screen", True)
        vision2 = FakeVision("第二代摘要。")
        harness.runtime.screen._vision = vision2
        harness.bridge._screen._vision = vision2
        harness.websocket.clear()

        fresh = await harness.bridge.on_screen_request(force=True)

        assert fresh is not None, "a fresh request after a discard must succeed"
        assert "第二代摘要" in fresh.summary
        summary = harness.websocket.last_of("aemeath-screen-summary")
        assert summary is not None
        assert "第二代摘要" in summary["summary"]
        assert harness.runtime.screen.current_summary() is not None


# ----------------------------------------------------------------------
# OFF means off: no capture, no summary, no frame
# ----------------------------------------------------------------------


class TestObservationDisabled:
    """With observation off, a request must not even attempt a capture."""

    async def test_manual_request_while_disabled_never_captures(self, make_harness):
        """A forced request with the switch off must not start capture.

        ``force`` skips the interval and stability gates, so it is exactly the
        path that could bypass a switch check and capture anyway.
        """
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("不该出现。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        # The switch starts off; assert that rather than assuming it.
        assert harness.runtime.situation.state.screen_observation_enabled is False
        harness.websocket.clear()

        result = await harness.bridge.on_screen_request(force=True)

        assert result is None, "no observation may be produced while off"
        assert capture.lookups == 0, (
            "the foreground window must not even be queried while observation is off"
        )
        assert capture.capture_count == 0, "no screenshot may be taken while off"
        assert vision.calls == [], "the vision model must not be called while off"
        assert_nothing_observed(
            harness, capture, vision, why="request while observation is off"
        )

    async def test_disabling_clears_the_cached_summary(self, make_harness):
        """Turning observation off forgets what was last seen."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        observation = await harness.bridge.on_screen_request(force=True)
        assert observation is not None
        assert harness.runtime.screen.current_summary() is not None

        await harness.bridge.set_switch("screen", False)

        assert harness.runtime.screen.current_summary() is None
        assert harness.runtime.screen.latest is None


# ----------------------------------------------------------------------
# Non-regression: capture semantics and the local-data boundary
# ----------------------------------------------------------------------


class TestCaptureSemantics:
    """Foreground-window framing, no image on disk, no facts from summaries."""

    async def test_summary_is_taken_from_the_foreground_window(self, make_harness):
        """The captured region is the foreground window's rectangle."""
        window = FakeWindow(title="终端", rect=(100, 50, 900, 700))
        capture = CountedScreenCapture(window)
        vision = FakeVision("用户正在运行命令。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        observation = await harness.bridge.on_screen_request(force=True)

        assert observation is not None
        assert observation.window_title == "终端"
        # The capture backend receives the window, not the full desktop.
        assert capture.capture_count == 1
        assert vision.calls, "the vision model must see the captured image"

    async def test_switching_windows_does_not_reuse_the_previous_summary(
        self, make_harness
    ):
        """A different window produces a different summary, not a stale one."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        first = await harness.bridge.on_screen_request(force=True)
        assert first is not None and "写代码" in first.summary

        # The user switches to another window; the image differs.
        capture.window = FakeWindow(title="浏览器")
        vision.summary = "用户正在看网页。"
        harness.websocket.clear()

        second = await harness.bridge.on_screen_request(force=True)

        assert second is not None, "a new window must be observable"
        assert "看网页" in second.summary
        assert "写代码" not in second.summary, (
            "the previous window's summary must not be reused for a new one"
        )
        frame = harness.websocket.last_of("aemeath-screen-summary")
        assert frame is not None and "看网页" in frame["summary"]
        assert frame["window_title"] == "浏览器"

    async def test_capture_failure_does_not_fall_back_to_a_previous_image(
        self, make_harness
    ):
        """A failed capture never reuses the last image as if it were live."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("第一次成功。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        assert await harness.bridge.on_screen_request(force=True) is not None
        harness.websocket.clear()

        # Capture now fails.
        capture.fail = True
        result = await harness.bridge.on_screen_request(force=True)

        assert result is None, "a failed capture must not return a stale summary"
        assert harness.websocket.frames_of("aemeath-screen-summary") == []

    async def test_summary_never_becomes_a_permanent_user_fact(self, make_harness):
        """An ordinary summary is not stored as a durable memory."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)

        assert harness.runtime.memory.store.list_memories() == []

    async def test_no_screenshot_file_is_written(self, make_harness, tmp_path):
        """Capture stays in memory: no image lands on disk.

        Checked over the whole observation lifecycle rather than at one call
        site, because the requirement is about the data boundary, not a
        particular function.
        """
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("用户正在写代码。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        before = {
            path.resolve()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.on_screen_request(force=True)

        after = {
            path.resolve()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        new_files = after - before
        images = [
            str(path)
            for path in new_files
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
        ]
        assert not images, f"a screenshot was written to disk: {images}"


# ----------------------------------------------------------------------
# Class mode must not change screen behaviour
# ----------------------------------------------------------------------


class TestClassModeUnaffected:
    """The classroom mute is about audio; it must not change observation."""

    async def test_screen_request_still_works_in_class_mode(self, make_harness):
        """Class mode suppresses voice, not screen observation."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = FakeVision("上课时看到的窗口。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        observation = await harness.bridge.on_screen_request(force=True)

        assert observation is not None, (
            "class mode must not disable screen observation"
        )
        assert "上课时看到" in observation.summary
        assert harness.websocket.frames_of("aemeath-screen-summary"), (
            "the summary must still reach the client in class mode"
        )

    async def test_turning_observation_off_in_class_mode_still_discards(
        self, make_harness
    ):
        """The generation guard works the same while muted."""
        capture = CountedScreenCapture(FakeWindow(title="编辑器"))
        vision = PausableVision("迟到且上课。")
        harness = screen_harness(make_harness, vision=vision, capture=capture)

        await harness.bridge.set_switch("screen", True)
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        request = asyncio.create_task(harness.bridge.on_screen_request(force=True))
        await asyncio.wait_for(vision.entered.wait(), timeout=5.0)

        await harness.bridge.set_switch("screen", False)
        vision.release()

        assert await asyncio.wait_for(request, timeout=5.0) is None
        assert_nothing_observed(
            harness, capture, vision, why="reset during a vision call in class mode"
        )
