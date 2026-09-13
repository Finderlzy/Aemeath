"""Aemeath bridge: the single seam between upstream and Aemeath.

Upstream owns transport, ASR, TTS and character rendering. Aemeath owns turns,
situation, memory and output permission. This module is the only place the two
meet, so that no upstream code path can bypass Aemeath's own input, output and
history rules.

Flow (see docs/architecture.md):

    desktop client
      -> upstream connection / input handling
      -> AemeathBridge            (this module)
      -> coordinator
      -> agent / memory / screen / scheduler
      -> output bridging + TTS
      -> client display, playback and receipts

Responsibilities fixed here:

* The coordinator alone manages turns, cancellation and output permission.
* Every inbound event (typed text, ASR transcript, interrupt, mode switch,
  screen request, proactive signal, client receipt) enters through a method on
  :class:`AemeathBridge`.
* Every outbound frame is emitted through the bridge, so it always carries the
  connection generation, turn id and audio slice id the client needs to reject
  stale output.
* Only one client may be active. A new connection takes over, cancels the old
  connection's turns, and the old connection's frames are dropped.

The bridge is deliberately independent of FastAPI: it talks to a *sender*
coroutine, which the tests supply as a fake socket.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from src.open_llm_vtuber.utils.stream_audio import prepare_audio_payload

from .interfaces import EventSource, SituationState, SpeechMode, TurnId

#: Protocol version this bridge speaks. A client that does not negotiate at
#: least this version is not treated as a full Aemeath client.
PROTOCOL_VERSION = 2

Sender = Callable[[str], Awaitable[None]]


@dataclass
class ClientSession:
    """One connected desktop client."""

    client_uid: str
    generation: int
    sender: Sender
    protocol_version: int = 0
    connected_at: float = field(default_factory=time.time)

    #: Set when the client reports it is showing text for a turn.
    displayed_turns: set[str] = field(default_factory=set)

    #: Set when the client reports playback finished for an audio slice.
    played_slices: set[str] = field(default_factory=set)

    @property
    def is_full_client(self) -> bool:
        """Whether this client negotiated the Aemeath protocol."""
        return self.protocol_version >= PROTOCOL_VERSION


@dataclass
class PendingProactive:
    """A proactive message awaiting the client's display receipt.

    Held so a second candidate cannot be generated while the first is still
    unacknowledged; released when the receipt arrives or the turn is dropped.
    """

    turn_id: str
    text: str
    sent_at: float


class AemeathBridge:
    """Routes every desktop event into Aemeath and every output back out."""

    def __init__(self, *, coordinator, situation, memory=None, screen=None,
                 metrics=None, config=None, tts_engine=None) -> None:
        """Wire the bridge to the Aemeath modules it fronts.

        Args:
            coordinator: Turn and output arbitration.
            situation: Situation state.
            memory: Local history and memory service.
            screen: Screen observer, when capture and vision are configured.
            metrics: Per-turn metrics recorder.
            config: Resolved Aemeath configuration.
            tts_engine: Upstream TTS engine used to voice proactive messages.
                Attached later by ``ServiceContext``, because the bridge is
                built before the engine is resolved.
        """
        self._coordinator = coordinator
        self._situation = situation
        self._memory = memory
        self._screen = screen
        self._metrics = metrics
        self._config = config
        self._tts_engine = tts_engine

        self._session: Optional[ClientSession] = None
        self._generation = 0
        self._lock = asyncio.Lock()

        #: Turn currently being displayed/played, used for receipt validation.
        self._active_output_turn: Optional[str] = None

        #: Monotonic counter for audio slices within a turn.
        self._slice_counters: Dict[str, int] = {}

        #: Proactive candidate awaiting a display receipt.
        self.pending_proactive: Optional[PendingProactive] = None

        #: Callable that produces a proactive candidate; installed by upstream.
        self._proactive_generate: Optional[Callable[..., Awaitable[str]]] = None

        #: Whether the startup greeting has already been attempted this session.
        self._startup_attempted = False

        #: Screen observation generation; bumped whenever observation stops.
        self._observation_generation = 0

        #: Conversation currently selected by the client.
        self._history_uid: Optional[str] = None

        #: Late-output counters, exposed for tests and diagnostics.
        self.dropped_late_audio = 0
        self.dropped_late_text = 0

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def session(self) -> Optional[ClientSession]:
        """The active client session, if any."""
        return self._session

    @property
    def generation(self) -> int:
        """Current connection generation."""
        return self._generation

    @property
    def observation_generation(self) -> int:
        """Current screen observation generation."""
        return self._observation_generation

    def attach_client(
        self,
        sender: Sender,
        *,
        client_uid: str = "client",
        protocol_version: int = PROTOCOL_VERSION,
    ) -> ClientSession:
        """Register a client, taking over from any previous connection.

        A new connection supersedes the old one: the previous session's turns
        are cancelled and its frames are no longer accepted, because a stale
        window must not keep driving the character.

        Args:
            sender: Coroutine sending one JSON string to this client.
            client_uid: Identifier for diagnostics.
            protocol_version: Version the client negotiated (0 when unknown).

        Returns:
            The new session.
        """
        self._generation += 1
        previous = self._session
        session = ClientSession(
            client_uid=client_uid,
            generation=self._generation,
            sender=sender,
            protocol_version=protocol_version,
        )
        self._session = session
        self._coordinator.client_connected = True

        if previous is not None:
            logger.info(
                "Client {} superseded by {} (generation {}).",
                previous.client_uid,
                client_uid,
                session.generation,
            )
            # Cancel whatever the old connection was doing; its sender is now
            # detached so late output is dropped rather than delivered twice.
            self._coordinator.interrupt()

        logger.info(
            "Client attached: uid={} generation={} protocol={} full_client={}",
            client_uid,
            session.generation,
            protocol_version,
            session.is_full_client,
        )
        return session

    def detach_client(self, *, client_uid: Optional[str] = None) -> None:
        """Drop the active client, e.g. on disconnect.

        Args:
            client_uid: When given, only detach if it matches the active client.
        """
        if self._session is None:
            return
        if client_uid is not None and self._session.client_uid != client_uid:
            return
        self._coordinator.client_connected = False
        self._coordinator.interrupt()
        self._observe_off()
        self.pending_proactive = None
        self._session = None
        logger.info("Client detached; output stopped.")

    def _is_current(self, generation: int) -> bool:
        """Whether a generation still belongs to the active connection."""
        return self._session is not None and self._session.generation == generation

    # ------------------------------------------------------------------
    # Outbound frames
    # ------------------------------------------------------------------

    async def _send(self, payload: Dict[str, Any]) -> bool:
        """Send one frame to the active client.

        Returns:
            ``True`` when the frame was delivered, ``False`` when there is no
            client. Late frames from a superseded generation are never sent
            because every sender closure captures its own session.
        """
        session = self._session
        if session is None:
            return False
        payload.setdefault("generation", session.generation)
        try:
            import json

            await session.sender(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:  # pragma: no cover - transport dependent
            logger.warning("Failed to send {}: {}", payload.get("type"), exc)
            return False

    async def send_state(self, *, reason: str = "") -> None:
        """Send the current situation state and its version to the client."""
        await self._send(
            {
                "type": "aemeath-state",
                "protocol_version": PROTOCOL_VERSION,
                "state": self.state_payload(),
                "reason": reason,
            }
        )

    def state_payload(self) -> Dict[str, Any]:
        """Serialise the situation state for the client."""
        state = self._situation.state
        return {
            "mode": state.mode.value,
            "state_version": self._situation.state_version,
            "microphone_enabled": state.microphone_enabled,
            "screen_observation_enabled": state.screen_observation_enabled,
            "proactive_enabled": state.proactive_enabled,
            "user_paused": state.user_paused,
            "voice_allowed": state.voice_allowed,
        }

    async def send_clear_audio(self, *, reason: str) -> None:
        """Tell the client to stop playback and discard queued audio."""
        await self._send(
            {
                "type": "aemeath-clear-audio",
                "reason": reason,
                "state_version": self._situation.state_version,
            }
        )

    async def send_display_text(self, turn_id: str, text: str) -> None:
        """Send display text, independent of whether audio is produced.

        Classroom mode suppresses audio but must never suppress text, so this
        path does not consult ``should_speak``.
        """
        await self._send(
            {
                "type": "aemeath-text",
                "turn_id": turn_id,
                "text": text,
                "mode": self._situation.state.mode.value,
                "state_version": self._situation.state_version,
            }
        )

    async def send_audio(self, turn_id: str, audio_base64: str, **extra: Any) -> Optional[str]:
        """Send one audio slice tagged with turn and slice ids.

        Returns:
            The slice id, or ``None`` when the audio was dropped because the
            turn was cancelled or speech is no longer permitted.
        """
        if not self.may_send_audio(turn_id):
            self.dropped_late_audio += 1
            return None

        slice_id = uuid.uuid4().hex[:12]
        self._slice_counters[turn_id] = self._slice_counters.get(turn_id, 0) + 1
        payload = {
            "type": "audio",
            "turn_id": turn_id,
            "audio_slice_id": slice_id,
            "slice_index": self._slice_counters[turn_id] - 1,
            "audio": audio_base64,
            "state_version": self._situation.state_version,
            "mode": self._situation.state.mode.value,
        }
        payload.update(extra)
        if not await self._send(payload):
            return None
        return slice_id

    def may_send_audio(self, turn_id: str) -> bool:
        """Whether audio for this turn may still go out.

        Checked at three points — before synthesis, before sending, and on the
        client before playback — because a mode change or an interrupt can land
        in between any two of them.
        """
        if self._coordinator.is_cancelled(TurnId(turn_id)):
            return False
        return self._situation.should_speak()

    def speech_allowed_for(self, turn_id: str) -> bool:
        """Alias used by the conversation layer before synthesis starts."""
        return self.may_send_audio(turn_id)

    async def send_audio_payload(self, turn_id: str, payload: Dict[str, Any]) -> Optional[str]:
        """Send an upstream-prepared audio payload, re-tagged with turn ids.

        The payload is reused so upstream's audio encoding (base64 plus the
        volume array used for lip sync) is preserved, while the Aemeath fields
        the client needs for rejection are added on top.

        Returns:
            The slice id, or ``None`` when the payload was dropped.
        """
        if not self.may_send_audio(turn_id):
            self.dropped_late_audio += 1
            logger.info("Dropping late audio payload for turn {}.", turn_id)
            return None

        slice_id = uuid.uuid4().hex[:12]
        self._slice_counters[turn_id] = self._slice_counters.get(turn_id, 0) + 1

        enriched = dict(payload)
        enriched.update(
            {
                "turn_id": turn_id,
                "audio_slice_id": slice_id,
                "slice_index": self._slice_counters[turn_id] - 1,
                "state_version": self._situation.state_version,
                "mode": self._situation.state.mode.value,
            }
        )
        if self._slice_counters[turn_id] == 1:
            self.mark_first_audio(turn_id)

        if not await self._send(enriched):
            return None
        return slice_id

    def mark_first_text(self, turn_id: str) -> None:
        """Record backend time-to-first-text for a turn."""
        if self._metrics is not None:
            self._metrics.mark_first_text(turn_id)

    def mark_first_audio(self, turn_id: str) -> None:
        """Record backend time-to-first-audio for a turn."""
        if self._metrics is not None:
            self._metrics.mark_first_audio(turn_id)

    # ------------------------------------------------------------------
    # Inbound events
    # ------------------------------------------------------------------

    async def begin_user_turn(self, *, source: EventSource) -> TurnId:
        """Open a turn for user input (typed text or ASR transcript).

        The turn is registered with the metrics recorder here, so the backend
        stage timings and the client receipts have somewhere to land.

        Returns:
            The new turn id.
        """
        self._coordinator.notify_user_activity()
        self._invalidate_pending_proactive("user input arrived")
        turn = self._coordinator.begin_turn(source)
        if self._metrics is not None:
            self._metrics.start_turn(str(turn.turn_id), source.value)
        return turn.turn_id

    def _invalidate_pending_proactive(self, reason: str) -> None:
        """Drop a proactive candidate that user activity made stale."""
        if self.pending_proactive is not None:
            logger.info("Proactive candidate invalidated: {}.", reason)
            self.pending_proactive = None

    def observe_user_text(self, text: str) -> Optional[str]:
        """Apply mode/pause changes implied by a user message.

        Returns:
            An acknowledgement string when the state changed.
        """
        return self._coordinator.observe_user_text(text)

    async def on_user_message(self, text: str, *, source: EventSource,
                              turn_id: Optional[TurnId] = None) -> Optional[str]:
        """Record the user message and apply implied state changes.

        The user message is persisted *before* generation so a crash mid-turn
        does not lose what was said, and it is attached to the conversation the
        bridge currently points at so it shows up in the client's history.

        Returns:
            A short acknowledgement to display, when the mode/pause changed.
        """
        if self._memory is not None and text.strip():
            self._memory.record_user_message(
                text,
                source=source.value,
                turn_id=str(turn_id) if turn_id else None,
                conversation_id=self._history_uid,
            )
        ack = await self._coordinator.observe_user_text_ordered(text)
        return ack

    async def switch_mode(self, mode: SpeechMode) -> SituationState:
        """Switch conversation mode as one ordered, awaitable operation.

        Ordering is the whole point (see the plan's classroom section):

        1. persist the state and bump the state version,
        2. cancel the old turn and any pending synthesis,
        3. tell the client the new state and to clear audio,
        4. the client stops playback, clears its queue and rejects old audio,
        5. send the confirmation text.
        """
        async with self._lock:
            state = self._situation.set_mode(mode)

            # 2. Cancel the running turn; pending synthesis is dropped by the
            #    output path because the turn id is now cancelled. The audio
            #    cancellation is awaited here so the queue is genuinely cleared
            #    before the client is told it was.
            cancelled = self._coordinator.interrupt()
            if cancelled is not None:
                logger.info("Turn {} cancelled by mode switch.", cancelled)
            await self._coordinator.drain_cancellations()

            self._invalidate_pending_proactive("mode switched")

            # 3. New state + clear-audio instruction, in that order.
            await self.send_state(reason=f"mode switched to {mode.value}")
            if mode is SpeechMode.CLASS:
                await self.send_clear_audio(reason="class mode entered")

            # 5. Confirmation text; display is independent of audio.
            await self._send(
                {
                    "type": "full-text",
                    "text": (
                        "好，我改用文字。" if mode is SpeechMode.CLASS else "好，我说话了。"
                    ),
                    "reason": "mode-changed",
                }
            )
            return state

    async def set_switch(self, name: str, enabled: bool) -> SituationState:
        """Toggle one situation switch and notify the client.

        Args:
            name: ``microphone``, ``screen`` or ``proactive``.
            enabled: Desired state.

        Returns:
            The stored state.
        """
        if name == "microphone":
            state = self._situation.set_microphone(enabled)
        elif name == "screen":
            state = self._situation.set_screen_observation(enabled)
            if enabled:
                self._observe_on()
            else:
                self._observe_off()
        elif name == "proactive":
            state = self._situation.set_proactive(enabled)
        else:
            raise ValueError(f"unknown switch: {name}")

        await self.send_state(reason=f"{name}={'on' if enabled else 'off'}")
        return state

    def _observe_on(self) -> None:
        """Mark screen observation as active."""
        self._observation_generation += 1
        logger.info(
            "Screen observation enabled (generation {}).", self._observation_generation
        )

    def _observe_off(self) -> None:
        """Invalidate in-flight observations and drop the current summary."""
        self._observation_generation += 1
        if self._screen is not None:
            self._screen.reset()
        logger.info(
            "Screen observation disabled (generation {}).", self._observation_generation
        )

    # ------------------------------------------------------------------
    # Proactive
    # ------------------------------------------------------------------

    async def request_proactive(self, *, is_startup: bool = False) -> Optional[str]:
        """Handle a client's request to consider a proactive message.

        The client signal only *asks for an eligibility check*; it never starts
        a conversation directly. The backend scheduler is the only source of
        proactive turns.
        """
        if self.pending_proactive is not None:
            logger.debug("Proactive request ignored: an earlier candidate is pending.")
            return None

        decision = await self._coordinator.consider_proactive(is_startup=is_startup)
        if not decision.eligible:
            logger.debug("Proactive declined: {}", decision.reason)
            await self._send(
                {"type": "aemeath-proactive-decision", "eligible": False,
                 "reason": decision.reason}
            )
            return None

        return await self.run_proactive(is_startup=is_startup)

    def set_proactive_generator(self, generate) -> None:
        """Install the callable that produces a proactive candidate.

        The bridge owns *whether* Aemeath may speak; the generator owns *what*
        she says. Upstream supplies the generator (it holds the model and the
        prompt), and the runtime's worker drives the timing, so both the client
        signal and the timer funnel through one arbitration path.

        Args:
            generate: An async callable returning the candidate text.
        """
        self._proactive_generate = generate

    def attach_coordinator(self, coordinator) -> None:
        """Complete the bridge/coordinator wiring.

        The two are mutually dependent: the coordinator owns *whether* output
        may go out, and its output hooks are the bridge's own methods. The
        bridge is therefore constructed first and given its coordinator here,
        rather than reaching into the attribute from outside.

        Args:
            coordinator: The :class:`EventCoordinator` for this process.
        """
        self._coordinator = coordinator

    def attach_tts_engine(self, engine) -> None:
        """Attach the TTS engine used to voice self-initiated messages.

        Upstream's own synthesis path is driven by the conversation loop, which
        only runs for a user turn. A proactive message has no such loop, so the
        bridge drives the same engine itself; without this the proactive path
        could only send display text and an empty audio frame.

        Args:
            engine: An upstream ``TTSInterface`` implementation, or ``None``.
        """
        self._tts_engine = engine
        logger.info(
            "TTS engine attached to the Aemeath bridge ({})",
            type(engine).__name__ if engine is not None else "none",
        )

    async def run_proactive(self, *, is_startup: bool = False) -> Optional[str]:
        """Let the scheduler decide, then generate and deliver one message.

        This is the single entry point for proactive output. Both the client's
        ``ai-speak-signal`` and the runtime's timer call it, so the eligibility
        rules are applied identically no matter what triggered the attempt.

        Returns:
            The message that was delivered, or ``None`` when nothing was said.
        """
        generate = self._proactive_generate
        if generate is None:
            logger.debug("Proactive skipped: no generator installed yet.")
            return None

        turn_id = str(TurnId.new())

        async def _candidate() -> str:
            """Produce the candidate text for this attempt."""
            return await generate(is_startup)

        message = await self._coordinator.run_proactive(
            _candidate, is_startup=is_startup, turn_id=TurnId(turn_id)
        )
        if not message:
            return None

        # Delivery re-checks the situation and holds the receipt placeholder.
        delivered = await self.deliver_proactive(message, turn_id=turn_id)
        if not delivered:
            return None
        # The greeting is one-shot for the session once it has actually gone
        # out; it is not repeated on every reconnect.
        self._coordinator.mark_proactive_greeted(is_startup=is_startup)
        return message

    async def maybe_startup_greeting(self) -> Optional[str]:
        """Attempt the one-shot startup greeting.

        Called once when a client connects. The scheduler still decides: if the
        greeting is disabled, already used, or the situation forbids speech
        right now, nothing is sent and the attempt is simply skipped.

        Returns:
            The greeting that was delivered, or ``None``.
        """
        if self._startup_attempted:
            return None
        self._startup_attempted = True
        return await self.run_proactive(is_startup=True)

    async def deliver_proactive(self, text: str, *, turn_id: str) -> bool:
        """Send a proactive candidate and hold a placeholder for its receipt.

        The text goes out first and is independent of audio, so classroom mode
        still shows the message. Audio then reuses the ordinary reply path —
        the coordinator re-checks that the turn is current and that the
        situation still permits voice, and :meth:`on_speak` synthesises it with
        the configured engine. Synthesising here rather than sending an empty
        audio frame is what makes a proactive message audible (A01): the old
        code called ``send_audio(turn_id, "")`` and the client received a
        silent placeholder.

        Delivery is *attempted* here but never *counted* here. The message is
        recorded as sent only when the client confirms it was displayed, so an
        unshown message cannot consume the proactive budget.

        Args:
            text: The candidate to display and, when allowed, speak.
            turn_id: The turn this candidate belongs to.

        Returns:
            ``True`` when the message was produced, ``False`` when it was
            declined or the turn had already been superseded.
        """
        if not text or "[SILENCE]" in text:
            return False

        self.pending_proactive = PendingProactive(
            turn_id=turn_id, text=text, sent_at=time.time()
        )
        # A proactive turn is registered with the recorder like any other, so
        # its backend timings and receipts have somewhere to land instead of
        # being an untracked side channel.
        if self._metrics is not None:
            self._metrics.start_turn(turn_id, EventSource.PROACTIVE.value)

        sent = await self._coordinator.emit_proactive(
            TurnId(turn_id), text, source=EventSource.PROACTIVE
        )

        # The coordinator left the turn open so it could carry this output;
        # retiring it now is what makes any later slice for the same turn fail
        # ``may_send_audio``. A user turn or an interrupt that arrived in the
        # meantime has already retired it, which end_turn treats as a no-op.
        self._coordinator.end_turn(TurnId(turn_id))

        if sent:
            self.mark_generation_finished(turn_id)
            self.finish_turn(turn_id, cancelled=False)
            return True

        # The turn was superseded or cancelled before anything went out; do not
        # leave a placeholder waiting for a receipt that cannot come, and close
        # the metrics entry rather than leaking an unfinished turn.
        self._invalidate_pending_proactive("candidate superseded before delivery")
        self.finish_turn(turn_id, cancelled=True)
        return False

    async def on_speak(self, turn_id: TurnId, text: str, source: EventSource) -> None:
        """Synthesise one generated message through the same pipeline replies use.

        The coordinator has already confirmed that this turn is current and that
        the situation permits voice, so this only has to run the synthesis.

        Ordinary replies do **not** come through here: upstream's conversation
        loop drives ``TTSTaskManager`` for a user turn, and that loop is also
        what writes the assistant message to history. This path exists for the
        turns that have no such loop, which is exactly the proactive case — and
        a proactive message is not yet confirmed as displayed, so it is not
        remembered here either; the display receipt decides that.
        """
        await self._synthesise(str(turn_id), text)

    async def _synthesise(self, turn_id: str, text: str) -> None:
        """Run one sentence through upstream's real TTS and send the result.

        This is the bridge's own synthesis path, used when there is no upstream
        conversation loop to drive ``TTSTaskManager`` — which is exactly the
        proactive case. The engine, the payload builder and the outbound frame
        are all upstream's; only the caller differs.
        """
        engine = self._tts_engine
        if engine is None:
            logger.warning(
                "No TTS engine attached; turn {} produced no proactive audio.",
                turn_id,
            )
            return

        if not self.may_send_audio(turn_id):
            self.dropped_late_audio += 1
            logger.info("Speech suppressed for turn {} before synthesis.", turn_id)
            return

        try:
            audio_path = await engine.async_generate_audio(
                text, file_name_no_ext=turn_id
            )
        except Exception as exc:
            logger.error("Proactive synthesis failed for turn {}: {}", turn_id, exc)
            return

        try:
            if not self.may_send_audio(turn_id):
                self.dropped_late_audio += 1
                logger.info(
                    "Discarding proactive audio for turn {} (cancelled or muted).",
                    turn_id,
                )
                return
            payload = prepare_audio_payload(
                audio_path=audio_path,
                display_text={"text": text, "name": "Aemeath", "avatar": ""},
                actions=None,
            )
            await self.send_audio_payload(turn_id, payload)
        except Exception as exc:
            logger.error("Could not send proactive audio for turn {}: {}", turn_id, exc)
        finally:
            try:
                engine.remove_file(audio_path)
            except Exception:  # pragma: no cover - cleanup is best effort
                logger.debug("Could not remove cached audio for turn {}.", turn_id)

    async def on_display_receipt(self, *, turn_id: str) -> None:
        """Handle the client confirming that a proactive message was shown.

        This is the **only** place a proactive message is recorded as sent: the
        coordinator deliberately does not count at generation time. Counting in
        both places was A02 — one message consumed two slots of the hourly
        budget, and a message the client never displayed still counted as
        delivered. A repeated receipt finds nothing pending and is a no-op.
        """
        pending = self.pending_proactive
        if pending is None or pending.turn_id != turn_id:
            return
        self.pending_proactive = None
        self._coordinator.scheduler.mark_spoken(pending.sent_at)
        logger.info("Proactive message {} confirmed displayed.", turn_id)

    # ------------------------------------------------------------------
    # Receipts from the client
    # ------------------------------------------------------------------

    async def on_playback_started(self, *, turn_id: str, audio_slice_id: str,
                                  client_elapsed_ms: Optional[float] = None) -> None:
        """Record that the client actually began playing an audio slice."""
        session = self._session
        if session is not None:
            session.played_slices.add(audio_slice_id)
        if self._metrics is not None:
            self._metrics.mark_playback_started(
                turn_id, audio_slice_id, client_elapsed_ms=client_elapsed_ms
            )

    async def on_playback_finished(self, *, turn_id: str, audio_slice_id: str,
                                   client_elapsed_ms: Optional[float] = None) -> None:
        """Record that a slice finished playing on the client."""
        if self._metrics is not None:
            self._metrics.mark_playback_finished(
                turn_id, audio_slice_id, client_elapsed_ms=client_elapsed_ms
            )

    async def on_display_text_shown(self, *, turn_id: str,
                                    client_elapsed_ms: Optional[float] = None) -> None:
        """Record that display text actually reached the screen."""
        if self._metrics is not None:
            self._metrics.mark_text_displayed(turn_id, client_elapsed_ms=client_elapsed_ms)

    async def on_cancel_complete(self, *, turn_id: str,
                                 client_elapsed_ms: Optional[float] = None) -> None:
        """Record that the client finished stopping playback for a cancel."""
        if self._metrics is not None:
            self._metrics.mark_cancel_complete(turn_id, client_elapsed_ms=client_elapsed_ms)

    async def on_client_activity(self, *, typing: bool = False,
                                 voice_active: bool = False) -> None:
        """Update the signals the proactive rules consult."""
        self._coordinator.user_typing = typing
        self._coordinator.mic_active = voice_active
        if typing or voice_active:
            self._invalidate_pending_proactive("client reports user activity")

    async def on_screen_request(self, *, force: bool = True):
        """Handle a manual screen observation request.

        Returns the observation, or ``None`` when observation is off, the
        session is locked, or capture failed. The generation is captured before
        awaiting so a result that arrives after observation was switched off is
        discarded rather than written back.
        """
        if self._screen is None:
            await self._send(
                {"type": "aemeath-screen-unavailable",
                 "reason": "vision or capture backend not configured"}
            )
            return None

        generation = self._observation_generation
        observation = await self._screen.observe(force=force)

        if generation != self._observation_generation:
            logger.info("Discarding screen observation from stale generation.")
            return None

        if observation is None:
            return None

        await self._send(
            {
                "type": "aemeath-screen-summary",
                "observation_id": observation.observation_id,
                "summary": observation.summary,
                "window_title": observation.window_title,
                "captured_at": observation.captured_at,
                "observation_generation": generation,
            }
        )
        return observation

    # ------------------------------------------------------------------
    # History: SQLite is the single source of truth
    # ------------------------------------------------------------------

    @property
    def memory_store(self) -> Any:
        """The authoritative local store."""
        return self._memory.store if self._memory is not None else None

    def set_history_uid(self, history_uid: Optional[str]) -> None:
        """Point the bridge at the conversation the client selected."""
        self._history_uid = history_uid or None

    @property
    def history_uid(self) -> Optional[str]:
        """Currently selected conversation id."""
        return self._history_uid

    def list_histories(self) -> List[Dict[str, Any]]:
        """List conversations from the authoritative database."""
        if self._memory is None:
            return []
        return self._memory.store.list_conversations()

    def create_history(self) -> str:
        """Create a conversation and return its id."""
        if self._memory is None:
            return ""
        conversation_id = self._memory.store.create_conversation()
        self._history_uid = conversation_id
        return conversation_id

    def delete_history(self, history_uid: str) -> bool:
        """Delete a conversation and its messages."""
        if self._memory is None:
            return False
        deleted = self._memory.store.delete_conversation(history_uid)
        if deleted and self._history_uid == history_uid:
            self._history_uid = None
        return deleted

    def load_history(self, history_uid: str) -> List[Dict[str, Any]]:
        """Read a conversation's messages from SQLite, in display form."""
        if self._memory is None:
            return []
        self._history_uid = history_uid
        messages = self._memory.store.conversation_messages(history_uid)
        return [
            {
                "role": "human" if message.role == "user" else "ai",
                "content": message.content,
                "name": (
                    "你" if message.role == "user" else "Aemeath"
                ),
            }
            for message in messages
            if message.status != "deleted"
        ]

    def record_assistant_message(
        self, content: str, *, turn_id: Optional[str], interrupted: bool = False
    ) -> Optional[str]:
        """Record the character's reply in the authoritative database."""
        if self._memory is None or not content.strip():
            return None
        return self._memory.record_assistant_message(
            content,
            turn_id=str(turn_id) if turn_id else None,
            interrupted=interrupted,
            conversation_id=self._history_uid,
        )

    def queue_extraction(self, *, turn_id: str) -> None:
        """Queue memory extraction for a finished turn.

        A proactive turn is not a user statement, so it is never queued as one.
        """
        if self._memory is None:
            return
        messages = self._memory.store.turn_messages(str(turn_id))
        user_messages = [m.message_id for m in messages if m.role == "user"]
        if not user_messages:
            return
        self._memory.store.enqueue_extraction(user_messages)

    def finish_turn(
        self,
        turn_id: str,
        *,
        cancelled: bool = False,
        error: Optional[str] = None,
    ) -> None:
        """Close out a turn's metrics exactly once."""
        if self._metrics is not None:
            self._metrics.finish_turn(turn_id, cancelled=cancelled, error=error)

    def mark_generation_finished(self, turn_id: str) -> None:
        """Record that generation ended for a turn.

        Distinct from playback ending: after generation finishes there may
        still be queued audio that a cancel must be able to discard.
        """
        if self._metrics is not None:
            self._metrics.mark_generation_finished(turn_id)

    async def import_legacy_history(self) -> Dict[str, Any]:
        """Run the explicit, idempotent import of upstream JSON history.

        The legacy files are imported once per source file and then deleted.
        If a file cannot be deleted the migration is reported as incomplete
        rather than being described as a completed unification.
        """
        if self._memory is None:
            return {"success": False, "reason": "memory not configured"}

        from .legacy import import_legacy_histories

        root = self.config.data_dir if self.config else None
        return await import_legacy_histories(self._memory.store, root=root)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel active work and release the client."""
        self._coordinator.interrupt()
        # Let any cancellation started by the interrupt finish before the
        # session is dropped, so nothing keeps writing to a dead client.
        await self._coordinator.drain_cancellations()
        self.pending_proactive = None
        self._session = None
        logger.info("Aemeath bridge shut down.")
