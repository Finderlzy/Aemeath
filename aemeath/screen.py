"""Screen observation.

Captures the *foreground window* rather than the whole desktop, downsizes the
image, and asks a vision model for a short summary with a timestamp.

Bounds required by the plan:

* Never silently widen to the full desktop when a window region is unavailable.
* Summaries older than a configured age are not presented as "what you are
  doing right now".
* The same picture is not re-analysed repeatedly.
* Capture stops when observation is off, the session locks, or the client
  disconnects. A failed capture never reuses a stale image as if it were live.
* Screenshots are held in memory only; they are not written to disk.
* Ordinary screen summaries do not become permanent user facts.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from loguru import logger

from .interfaces import ScreenObservation


class VisionAdapter(Protocol):
    """Vision provider interface."""

    async def describe(self, image: bytes, window_title: str = "") -> str:
        """Describe an image, returning a short summary."""
        ...


@dataclass(frozen=True)
class WindowInfo:
    """Foreground window geometry and title."""

    title: str
    left: int
    top: int
    width: int
    height: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """Window rectangle as (left, top, right, bottom)."""
        return (self.left, self.top, self.left + self.width, self.top + self.height)


class CaptureBackend(Protocol):
    """Platform capture interface."""

    def foreground_window(self) -> Optional[WindowInfo]:
        """Return the foreground window, or ``None`` when unavailable."""
        ...

    def grab(self, window: WindowInfo) -> bytes:
        """Capture the window's visible area as PNG bytes."""
        ...

    def is_locked(self) -> bool:
        """Whether the session is locked."""
        ...


class WindowsCaptureBackend:
    """Windows screen capture via ``pygetwindow`` and ``mss``.

    Imports are deferred so the module stays usable (and testable) on machines
    without those packages installed.
    """

    #: Whether the optional capture dependencies were importable.
    capture_available: bool = False

    #: Concrete reason capture is unavailable, so it is never an unexplained
    #: ``None`` when the user asks why observation does not work.
    unavailable_reason: str = ""

    def __init__(self) -> None:
        """Check that the optional capture dependencies are importable."""
        missing: list[str] = []
        for module in ("mss", "pygetwindow"):
            try:
                __import__(module)
            except Exception as exc:
                missing.append(f"{module} ({exc})")

        if missing:
            self.capture_available = False
            self.unavailable_reason = (
                "screen capture dependencies are not installed: "
                + ", ".join(missing)
            )
            logger.warning("Screen capture unavailable: {}", self.unavailable_reason)
        else:
            self.capture_available = True
            self.unavailable_reason = ""

    def foreground_window(self) -> Optional[WindowInfo]:
        """Return geometry for the currently active window."""
        if not self.capture_available:
            return None
        try:
            import pygetwindow as gw

            window = gw.getActiveWindow()
            if window is None:
                return None
            width = int(window.width)
            height = int(window.height)
            if width <= 0 or height <= 0:
                return None
            return WindowInfo(
                title=str(window.title or ""),
                left=int(window.left),
                top=int(window.top),
                width=width,
                height=height,
            )
        except Exception as exc:
            logger.warning("Failed to read foreground window: {}", exc)
            return None

    def grab(self, window: WindowInfo) -> bytes:
        """Capture the window region as PNG bytes."""
        import mss
        import mss.tools

        left, top, right, bottom = window.rect
        with mss.mss() as sct:
            shot = sct.grab(
                {"left": left, "top": top, "width": right - left, "height": bottom - top}
            )
            return mss.tools.to_png(shot.rgb, shot.size)

    def is_locked(self) -> bool:
        """Best-effort lock detection on Windows.

        Returns ``False`` when the check is unavailable; the caller still
        handles capture failures.
        """
        try:
            import ctypes

            user32 = ctypes.windll.User32
            desktop = user32.OpenInputDesktop(0, False, 0x0100)
            if desktop == 0:
                return True
            user32.CloseDesktop(desktop)
            return False
        except Exception:
            return False


class ScreenObserver:
    """Produces and caches timestamped screen summaries."""

    def __init__(
        self,
        backend: CaptureBackend,
        vision: VisionAdapter,
        *,
        max_edge_px: int = 1600,
        min_interval_seconds: int = 60,
        min_stable_seconds: int = 10,
        summary_max_age_seconds: int = 120,
    ) -> None:
        """Configure capture bounds.

        Args:
            backend: Platform capture implementation.
            vision: Vision model adapter.
            max_edge_px: Longest edge after downscaling.
            min_interval_seconds: Minimum gap between automatic observations.
            min_stable_seconds: How long the foreground window must be stable.
            summary_max_age_seconds: Age after which a summary is no longer
                treated as current.
        """
        self._backend = backend
        self._vision = vision
        self.max_edge_px = max_edge_px
        self.min_interval_seconds = min_interval_seconds
        self.min_stable_seconds = min_stable_seconds
        self.summary_max_age_seconds = summary_max_age_seconds

        self._latest: Optional[ScreenObservation] = None
        self._last_attempt_at: float = 0.0
        self._last_image_hash: str = ""
        self._window_signature: str = ""
        self._window_since: float = 0.0
        # Only ever holds the most recent image; nothing is written to disk.
        self._last_image: Optional[bytes] = None

        #: Whether observation is currently permitted. The observer guards
        #: itself rather than trusting every caller to check the switch: a
        #: ``force=True`` request skips the interval and stability gates, so it
        #: is exactly the path that would otherwise capture with observation
        #: off.
        self._enabled = False

        #: Bumped by :meth:`reset`. A vision response is only written back if
        #: this still matches the value captured before the call, so a reply
        #: that arrives after observation was switched off — or switched off
        #: *and on again* — is discarded instead of cached. Refusing to send
        #: the frame is not the same as refusing to remember it.
        self._generation = 0

    @property
    def generation(self) -> int:
        """Current observation generation, bumped on every reset."""
        return self._generation

    @property
    def enabled(self) -> bool:
        """Whether observation is currently permitted."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable observation, invalidating in-flight work.

        Args:
            enabled: Desired state.
        """
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self.reset()
        logger.info("Screen observation {} (generation {}).",
                    "enabled" if enabled else "disabled", self._generation)

    @property
    def latest(self) -> Optional[ScreenObservation]:
        """The most recent observation, regardless of age."""
        return self._latest

    def current_summary(self, now: Optional[float] = None) -> Optional[ScreenObservation]:
        """Return the latest observation only if it is still current.

        Returns:
            The observation, or ``None`` when missing or expired.
        """
        if self._latest is None:
            return None
        age = self._latest.age_seconds(now)
        if age > self.summary_max_age_seconds:
            return None
        return self._latest

    def _window_stable(self, window: WindowInfo, now: float) -> bool:
        """Whether the foreground window has been stable long enough."""
        signature = f"{window.title}|{window.rect}"
        if signature != self._window_signature:
            self._window_signature = signature
            self._window_since = now
            return False
        return (now - self._window_since) >= self.min_stable_seconds

    @staticmethod
    def _downscale(image: bytes, max_edge: int) -> bytes:
        """Downscale a PNG so its longest edge is at most ``max_edge``."""
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image)) as img:
                width, height = img.size
                longest = max(width, height)
                if longest <= max_edge:
                    return image
                scale = max_edge / float(longest)
                resized = img.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale)))
                )
                buffer = io.BytesIO()
                resized.save(buffer, format="PNG")
                return buffer.getvalue()
        except Exception as exc:
            # A downscale failure is not fatal; the caller still bounds capture.
            logger.warning("Downscale failed, using original size: {}", exc)
            return image

    async def observe(self, *, force: bool = False) -> Optional[ScreenObservation]:
        """Capture and describe the current foreground window.

        Every stage re-checks the observation generation, because the user can
        switch observation off at any point during an await. ``force`` skips the
        interval and stability gates, but never the enabled check: the manual
        request is a way to skip throttling, not a way to observe while off.

        Args:
            force: Skip the interval and stability gates, for the explicit
                "look at my screen" button.

        Returns:
            A new observation, or ``None`` when capture was skipped or failed.
        """
        now = time.time()
        generation = self._generation

        if not self._enabled:
            logger.debug("Screen observation skipped: observation is off.")
            return None

        if self._backend.is_locked():
            logger.info("Screen observation skipped: session is locked.")
            return None

        if not force and (now - self._last_attempt_at) < self.min_interval_seconds:
            logger.debug("Screen observation throttled.")
            return None

        window = self._backend.foreground_window()
        if window is None:
            # Report failure rather than widening to the whole desktop.
            self._last_attempt_at = now
            logger.info("Screen observation unavailable: no foreground window.")
            return None

        if not force and not self._window_stable(window, now):
            logger.debug("Screen observation deferred: window not stable yet.")
            return None

        self._last_attempt_at = now

        try:
            raw = self._backend.grab(window)
        except Exception as exc:
            # Never fall back to a previously captured image.
            logger.warning("Screen capture failed: {}", exc)
            return None

        image = self._downscale(raw, self.max_edge_px)

        digest = hashlib.sha256(image).hexdigest()
        if digest == self._last_image_hash and not force:
            logger.debug("Screen unchanged; skipping vision call.")
            return None
        self._last_image_hash = digest
        self._last_image = image

        try:
            summary = await self._vision.describe(image, window.title)
        except Exception as exc:
            logger.error("Vision model failed: {}", exc)
            return None

        # The vision call is an arbitrary-length await. If observation was
        # switched off (or off and on again) while it ran, this response
        # belongs to a generation nobody is waiting for any more: drop it
        # instead of caching it. Without this the bridge could refuse to send
        # the frame while the observer still kept the summary, which is A03.
        if generation != self._generation:
            logger.info(
                "Discarding vision response from stale generation {} (now {}).",
                generation,
                self._generation,
            )
            return None

        observation = ScreenObservation(
            observation_id=hashlib.sha256(
                f"{digest}{now}".encode("utf-8")
            ).hexdigest()[:16],
            summary=summary,
            window_title=window.title,
            captured_at=now,
        )
        self._latest = observation
        logger.info("Screen observation recorded: {}", window.title)
        return observation

    def reset(self) -> None:
        """Invalidate in-flight observations and forget cached state.

        Bumping the generation is what makes this effective: a vision response
        already in flight compares against the value it captured on entry and
        discards itself rather than writing back a summary from a period the
        user has left.

        The current summary is dropped too: once observation is off, the last
        thing Aemeath saw is no longer something she may present as "what you
        are doing right now".
        """
        self._generation += 1
        self._last_image = None
        self._last_image_hash = ""
        self._window_signature = ""
        self._window_since = 0.0
        self._latest = None
        self._last_attempt_at = 0.0
