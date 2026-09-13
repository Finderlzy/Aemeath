"""Event coordination and single-turn arbitration.

Every input — typed text, voice, a screen observation, a timer, or a proactive
check — enters through this module. It exists so that Aemeath can never talk
over herself or over the user: exactly one generation turn is active at a time,
user input always outranks a self-initiated topic, and a superseded proactive
candidate is discarded rather than queued.

Output arbitration is separate from generation. A turn produces text always;
whether that text is *spoken* depends on the situation at two moments: when
synthesis starts, and again immediately before playback. Checking twice is what
makes "I'm in class" take effect on audio that was already being generated.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from loguru import logger

from .interfaces import EventSource, InputEvent, SituationState, TurnId
from .proactive import ProactiveDecision, ProactiveScheduler
from .situation import SituationManager


@dataclass
class TurnState:
    """Bookkeeping for the active turn."""

    turn_id: TurnId
    source: EventSource
    cancelled: bool = False
    text: str = ""
    speech_allowed: bool = True


@dataclass
class CoordinatorHooks:
    """Callbacks the coordinator invokes.

    Kept as plain callables so the coordinator stays testable without a running
    WebSocket server.
    """

    on_display_text: Optional[Callable[[TurnId, str], Awaitable[None]]] = None
    #: ``on_speak(turn_id, text, source)`` — synthesis callback. The source
    #: tells the callee whether this is a user reply or a self-initiated
    #: message, which are delivered and remembered differently.
    on_speak: Optional[Callable[..., Awaitable[None]]] = None
    on_cancel_audio: Optional[Callable[[TurnId], Awaitable[None]]] = None
    on_cancel_all_audio: Optional[Callable[[], Awaitable[None]]] = None
    on_proactive_decided: Optional[
        Callable[[ProactiveDecision, str], Awaitable[None]]
    ] = None


class EventCoordinator:
    """Arbitrates turns and output for one desktop session."""

    def __init__(
        self,
        *,
        situation: SituationManager,
        scheduler: ProactiveScheduler,
        hooks: Optional[CoordinatorHooks] = None,
    ) -> None:
        """Wire the coordinator to situation and scheduling state."""
        self._situation = situation
        self._scheduler = scheduler
        self._hooks = hooks or CoordinatorHooks()

        self._active: Optional[TurnState] = None
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

        #: Cancellation work started by a synchronous entry point (interrupt,
        #: begin_turn) but not yet awaited by the caller. Callers that must
        #: observe ordering await :meth:`drain_cancellations` instead of leaving
        #: a detached task to race the rest of the turn; see :meth:`_schedule`.
        self._pending_cancels: List[asyncio.Task] = []

        # Signals the proactive rules consult.
        self.mic_active = False
        self.user_typing = False
        self.client_connected = True
        self.screen_locked = False

    # ------------------------------------------------------------------
    # Situation
    # ------------------------------------------------------------------

    @property
    def situation(self) -> SituationState:
        """Current situation state."""
        return self._situation.state

    @property
    def scheduler(self) -> ProactiveScheduler:
        """The proactive scheduler.

        Exposed so the bridge can record that a proactive message was actually
        displayed; the coordinator remains the only thing that decides whether
        a proactive turn may run.
        """
        return self._scheduler

    def observe_user_text(self, text: str) -> Optional[str]:
        """Apply mode/pause changes implied by a user message.

        Cancellation triggered here is tracked but not awaited; use
        :meth:`observe_user_text_ordered` when the caller needs the queue to be
        genuinely cleared before it proceeds.

        Returns:
            A short acknowledgement when the state changed.
        """
        ack = self._situation.observe_user_text(text)
        if ack is not None and self.situation.mode.value == "class":
            # Switching into class must silence anything already queued. The
            # work is tracked rather than detached so a caller that needs the
            # ordering (the bridge) can await drain_cancellations().
            self._schedule(self._cancel_all_audio())
        return ack

    async def observe_user_text_ordered(self, text: str) -> Optional[str]:
        """Apply implied state changes and await any cancellation they start.

        This is the awaitable form the plan requires: entering class mode by
        message must not leave the audio cancellation running as a detached
        task racing the rest of the turn.

        Args:
            text: The user's message.

        Returns:
            A short acknowledgement when the state changed.
        """
        ack = self.observe_user_text(text)
        await self.drain_cancellations()
        return ack

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    @property
    def current_turn(self) -> Optional[TurnState]:
        """The active turn, if any."""
        return self._active

    def is_generation_active(self) -> bool:
        """Whether a generation turn is currently running."""
        return self._active is not None and not self._active.cancelled

    def begin_turn(self, source: EventSource) -> TurnState:
        """Start a new turn, superseding any previous one.

        Args:
            source: Where this turn came from.

        Returns:
            The new turn state.
        """
        if self._active is not None:
            self._active.cancelled = True
            self._cancelled.add(str(self._active.turn_id))
            logger.info(
                "Turn {} superseded by a new turn.", self._active.turn_id
            )
            self._schedule(self._cancel_audio(self._active.turn_id))

        state = TurnState(turn_id=TurnId.new(), source=source)
        # Speech is decided per turn, from the situation at that moment.
        state.speech_allowed = self._situation.should_speak()
        self._active = state
        return state

    def adopt_turn(self, source: EventSource, turn_id: TurnId) -> TurnState:
        """Open a turn under a caller-supplied id.

        Used when the id has to exist before the coordinator is entered — the
        bridge mints the proactive turn id up front so the same id can tag the
        output frames and be matched against the client's display receipt.
        Adoption still supersedes whatever was running, so the one-turn-at-a-
        time rule and the cancellation bookkeeping are unchanged.

        Args:
            source: Where this turn came from.
            turn_id: The id the caller already minted.

        Returns:
            The new turn state.
        """
        if self._active is not None and self._active.turn_id != turn_id:
            self._active.cancelled = True
            self._cancelled.add(str(self._active.turn_id))
            logger.info("Turn {} superseded by a new turn.", self._active.turn_id)
            self._schedule(self._cancel_audio(self._active.turn_id))

        # Re-opening the same id must not inherit its own retirement.
        self._cancelled.discard(str(turn_id))
        state = TurnState(turn_id=turn_id, source=source)
        state.speech_allowed = self._situation.should_speak()
        self._active = state
        return state

    def end_turn(self, turn_id: TurnId) -> None:
        """Mark a turn finished and retire it from further output.

        Retiring matters as much as clearing the active slot. A turn that has
        already been delivered must not be able to emit a second, late slice:
        once the user starts typing, that queued audio is stale by definition,
        and ``may_send_audio`` has to say so. Leaking audio out of a finished
        turn is how a proactive message could be heard *after* the user had
        already replied to something else.

        No cancellation is scheduled here: this method is synchronous and is
        also called from contexts with no running loop. Retiring the turn id is
        what makes late output fail the ``may_send_audio`` check, and the
        caller that knows about a live queue (a mode switch, an interrupt)
        clears it explicitly.
        """
        if self._active is not None and self._active.turn_id == turn_id:
            self._active = None
        self._cancelled.add(str(turn_id))

    def is_cancelled(self, turn_id: TurnId) -> bool:
        """Whether a turn has been superseded or interrupted."""
        return str(turn_id) in self._cancelled

    def interrupt(self, heard_response: str = "") -> Optional[TurnId]:
        """Cancel the active turn.

        Args:
            heard_response: Text the user actually heard; the rest is dropped.

        Returns:
            The cancelled turn id, or ``None`` when nothing was running.
        """
        if self._active is None:
            return None
        turn = self._active
        turn.cancelled = True
        self._cancelled.add(str(turn.turn_id))
        # Keep only what was heard; later output must not be treated as spoken.
        turn.text = heard_response
        self._schedule(self._cancel_audio(turn.turn_id))
        logger.info("Turn {} interrupted by user.", turn.turn_id)
        self._active = None
        return turn.turn_id

    # ------------------------------------------------------------------
    # Output gating
    # ------------------------------------------------------------------

    async def emit_text(self, turn_id: TurnId, text: str) -> bool:
        """Deliver display text for a turn.

        Returns:
            ``True`` when the text was delivered; ``False`` when the turn had
            already been cancelled and the text was dropped.
        """
        if self.is_cancelled(turn_id):
            logger.debug("Dropping text for cancelled turn {}.", turn_id)
            return False
        if self._active is not None and self._active.turn_id == turn_id:
            self._active.text += text
        if self._hooks.on_display_text is not None:
            await self._hooks.on_display_text(turn_id, text)
        return True

    async def emit_speech(self, turn_id: TurnId, text: str) -> bool:
        """Synthesise and play speech for a turn, if currently permitted.

        The situation is re-checked here, immediately before playback, so a
        mode change during generation still suppresses the audio.

        Returns:
            ``True`` when speech was produced, ``False`` when suppressed.
        """
        if self.is_cancelled(turn_id):
            logger.debug("Dropping speech for cancelled turn {}.", turn_id)
            return False
        if not self._situation.should_speak():
            logger.debug("Speech suppressed for turn {} by situation.", turn_id)
            return False
        if self._hooks.on_speak is not None:
            await self._hooks.on_speak(turn_id, text, EventSource.USER_TEXT)
        return True
    async def emit_proactive(
        self, turn_id: TurnId, text: str, *, source: EventSource = EventSource.PROACTIVE
    ) -> bool:
        """Deliver one generated message's text and, when allowed, its audio.

        Text always goes out — classroom mode mutes audio, never display — and
        speech additionally requires that the turn is still current and the
        situation still permits voice. Both conditions are re-checked here,
        immediately before synthesis, because a mode switch, an interrupt or a
        user turn can land while the model is still generating.

        This is shared by ordinary replies and proactive candidates on purpose:
        A01 existed because the proactive path had its own, audio-less send.

        Args:
            turn_id: Turn this output belongs to.
            text: The message to display and, when allowed, to speak.
            source: Origin of the turn, recorded in the hook.

        Returns:
            ``True`` when the turn was current and the text was sent, ``False``
            when the turn had already been cancelled or superseded.
        """
        if self.is_cancelled(turn_id):
            logger.debug("Dropping output for cancelled turn {}.", turn_id)
            return False
        if self._active is not None and self._active.turn_id == turn_id:
            self._active.text += text
        if self._hooks.on_display_text is not None:
            await self._hooks.on_display_text(turn_id, text)
        if not self._situation.should_speak():
            logger.debug("Speech suppressed for turn {} by situation.", turn_id)
            return True
        if self._hooks.on_speak is not None:
            await self._hooks.on_speak(turn_id, text, source)
        return True

    async def _cancel_audio(self, turn_id: TurnId) -> None:
        """Cancel pending playback for a turn."""
        if self._hooks.on_cancel_audio is not None:
            await self._hooks.on_cancel_audio(turn_id)

    async def _cancel_all_audio(self) -> None:
        """Cancel all pending playback."""
        if self._hooks.on_cancel_all_audio is not None:
            await self._hooks.on_cancel_all_audio()

    def _schedule(self, coro: Awaitable[None]) -> asyncio.Task:
        """Start cancellation work that a synchronous caller cannot await.

        ``interrupt`` and ``begin_turn`` are synchronous, but cancelling audio
        is asynchronous. Detaching the task would let it run at an arbitrary
        point relative to the caller's subsequent sends, which is exactly the
        unordered behaviour the plan forbids. The task is therefore tracked so
        :meth:`drain_cancellations` can await it at a defined point, and a
        caller that cannot await still gets the work done rather than lost.

        Args:
            coro: The cancellation coroutine to run.

        Returns:
            The scheduled task.
        """
        task = asyncio.create_task(coro)
        self._pending_cancels.append(task)
        task.add_done_callback(self._forget_cancel)
        return task

    def _forget_cancel(self, task: asyncio.Task) -> None:
        """Drop a finished cancellation task, surfacing any failure."""
        try:
            self._pending_cancels.remove(task)
        except ValueError:  # pragma: no cover - already drained
            return
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Audio cancellation failed: {}", exc)

    async def drain_cancellations(self) -> int:
        """Await every cancellation started by a synchronous entry point.

        This is the awaitable counterpart the plan calls for: a mode switch or
        an interrupt can be followed by ``await drain_cancellations()`` so the
        client hears about a cleared queue *after* the queue was actually
        cleared, instead of racing a detached task.

        Returns:
            How many cancellation tasks were awaited.
        """
        pending = list(self._pending_cancels)
        if not pending:
            return 0
        await asyncio.gather(*pending, return_exceptions=True)
        return len(pending)

    # ------------------------------------------------------------------
    # Proactive
    # ------------------------------------------------------------------

    async def consider_proactive(
        self,
        *,
        is_startup: bool = False,
        now: Optional[float] = None,
    ) -> ProactiveDecision:
        """Check whether a proactive message is allowed right now."""
        decision = self._scheduler.check(
            situation=self.situation,
            mic_active=self.mic_active,
            user_typing=self.user_typing,
            generation_active=self.is_generation_active(),
            client_connected=self.client_connected,
            screen_locked=self.screen_locked,
            is_startup=is_startup,
            now=now,
        )
        return decision

    async def run_proactive(
        self,
        generate: Callable[[], Awaitable[str]],
        *,
        is_startup: bool = False,
        now: Optional[float] = None,
        turn_id: Optional[TurnId] = None,
    ) -> Optional[str]:
        """Attempt a proactive turn.

        The user may speak at any point; the state is re-checked after
        generation, and a candidate produced for a superseded situation is
        discarded instead of sent.

        Sending is deliberately *not* done here. The message is only counted as
        delivered when the client confirms it was displayed, and that receipt
        is handled by the bridge, so the coordinator returns the candidate and
        lets the bridge own delivery. Counting here as well is precisely the
        double count A02 described.

        Args:
            generate: Callable producing the candidate message.
            is_startup: Whether this is the startup greeting.
            now: Optional timestamp override.
            turn_id: Turn to use, when the caller already minted one. The
                bridge does, because the id has to be on the frames before the
                candidate is produced and must stay the same one its receipt
                arrives on. When omitted a fresh turn is created.

        Returns:
            The candidate that was sent, or ``None`` when nothing was said.
        """
        decision = await self.consider_proactive(is_startup=is_startup, now=now)
        if not decision.eligible:
            logger.debug("Proactive declined: {}", decision.reason)
            if self._hooks.on_proactive_decided is not None:
                await self._hooks.on_proactive_decided(decision, "")
            return None

        turn = (
            self.begin_turn(EventSource.PROACTIVE)
            if turn_id is None
            else self.adopt_turn(EventSource.PROACTIVE, turn_id)
        )
        try:
            candidate = (await generate()).strip()
        except Exception as exc:
            logger.error("Proactive generation failed: {}", exc)
            self.end_turn(turn.turn_id)
            return None

        # Re-check before sending: the user may have started typing, and the
        # switch may have been turned off while the model was generating. The
        # situation is re-read rather than trusted from the earlier decision,
        # because that decision was made before an arbitrary-length await.
        if self.is_cancelled(turn.turn_id):
            logger.info("Proactive candidate discarded: turn superseded.")
            self.end_turn(turn.turn_id)
            return None
        if self.user_typing or self.mic_active:
            logger.info("Proactive candidate discarded: user became active.")
            self.end_turn(turn.turn_id)
            return None
        if not self.situation.proactive_enabled:
            logger.info("Proactive candidate discarded: reactive switch turned off.")
            self.end_turn(turn.turn_id)
            if self._hooks.on_proactive_decided is not None:
                await self._hooks.on_proactive_decided(decision, "")
            return None
        if not candidate or "[SILENCE]" in candidate:
            logger.debug("Proactive declined by model.")
            self.end_turn(turn.turn_id)
            if self._hooks.on_proactive_decided is not None:
                await self._hooks.on_proactive_decided(decision, "")
            return None

        # The candidate is *returned*, not sent or counted: the bridge delivers
        # it and only the client's display receipt records it as spoken. The
        # turn therefore stays open and current so the bridge can still emit its
        # text and audio; the bridge closes it once delivery is done.
        if self._hooks.on_proactive_decided is not None:
            await self._hooks.on_proactive_decided(decision, candidate)
        return candidate

    def mark_proactive_greeted(self, *, is_startup: bool) -> None:
        """Record that a startup greeting has been used.

        Separate from :meth:`mark_spoken` because the greeting is one-shot per
        session whether or not the client ever confirmed showing it: repeating
        it on every reconnect would be worse than a missed receipt.
        """
        if is_startup:
            self._scheduler.mark_greeted()

    def notify_user_activity(self) -> None:
        """Record that the user did something, clearing the unanswered state."""
        self._scheduler.mark_user_spoke()
