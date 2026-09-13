"""Aemeath conversation agent.

Owns character context for one desktop session: it assembles the persona, the
current situation, recent turns and retrieved memories into the prompt, streams
the reply, and honours interruption and classroom muting.

Relationship to upstream: this implements
``open_llm_vtuber.agent.agents.agent_interface.AgentInterface`` and reuses
upstream's sentence-splitting transformers rather than reimplementing them. It
replaces upstream's ``BasicMemoryAgent`` *only* for conversation, because
upstream's long-term memory was removed and Aemeath supplies its own.

Turn rules enforced here (see docs/architecture.md and the Phase 1 plan):

* One generation turn at a time; a new turn supersedes the previous one.
* Interruption cancels generation and marks the turn so late output is dropped.
* Text is always produced; speech is additionally gated on the situation state.
* A proactive event never enters memory as something the user said.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

from loguru import logger

# Import upstream through the *same* module path the server itself uses.
#
# The server is launched as a script from the upstream checkout, so its own
# modules load as ``src.open_llm_vtuber.*``. Importing ``open_llm_vtuber.*``
# here (which also resolves, because ``src/`` is on sys.path) loads the same
# files a *second* time under a different name, producing two distinct
# ``SentenceOutput`` classes. ``isinstance`` then fails in the upstream
# conversation loop and every reply is silently dropped with only
# "Received unexpected item type from agent chat stream" as a symptom.
#
# ``src`` is a package with an ``__init__.py``, so this resolves whenever the
# upstream checkout root is importable — which every entry point guarantees.
from src.open_llm_vtuber.agent.agents.agent_interface import AgentInterface
from src.open_llm_vtuber.agent.input_types import BatchInput, ImageSource
from src.open_llm_vtuber.agent.output_types import DisplayText, SentenceOutput
from src.open_llm_vtuber.agent.stateless_llm.stateless_llm_interface import (
    StatelessLLMInterface,
)
from src.open_llm_vtuber.agent.transformers import (
    actions_extractor,
    display_processor,
    sentence_divider,
    tts_filter,
)
from src.open_llm_vtuber.config_manager import TTSPreprocessorConfig

from .interfaces import EventSource, SituationState, TurnId
from .prompts import build_system_prompt, build_user_prompt

# Upstream sends this marker into the conversation stream when the user
# interrupts; the agent must expect it and not treat it as model output.
INTERRUPT_MARKER = "[Interrupted by user]"


class _NullLive2DModel:
    """Placeholder used when no Live2D model is available.

    Upstream's ``actions_extractor`` unconditionally calls
    ``live2d_model.extract_emotion``, so a headless context (tests, or a client
    running without an avatar) needs a stand-in that simply reports no emotion
    rather than raising.
    """

    @staticmethod
    def extract_emotion(text: str):  # noqa: ARG004 - signature must match upstream
        """Report that no expression was detected."""
        return None


class AemeathAgent(AgentInterface):
    """Conversation agent carrying Aemeath's persona, situation and memory."""

    def __init__(
        self,
        llm: StatelessLLMInterface,
        system: str,
        live2d_model=None,
        tts_preprocessor_config: Optional[TTSPreprocessorConfig] = None,
        faster_first_response: bool = True,
        segment_method: str = "pysbd",
        interrupt_method: str = "user",
        *,
        situation=None,
        memory=None,
        config=None,
        screen_summary_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Initialise the agent.

        Args:
            llm: Stateless LLM used for generation.
            system: Base persona prompt from the character configuration.
            live2d_model: Model used for expression extraction.
            tts_preprocessor_config: Text cleanup applied before synthesis.
            faster_first_response: Split on the first comma for lower latency.
            segment_method: Sentence segmentation strategy.
            interrupt_method: ``"user"`` or ``"system"`` interrupt marker role.
            situation: Shared situation manager; the current state is read from
                it on every turn rather than being cached here.
            memory: Optional local memory store implementing the memory API.
            config: Optional resolved Aemeath configuration.
            screen_summary_provider: Optional zero-argument callable returning
                the currently usable screen observation. When omitted it is
                resolved from the process runtime.
        """
        super().__init__()
        self._llm = llm
        self._persona = system or "You are Aemeath, a desktop AI companion."
        self._live2d_model = live2d_model or _NullLive2DModel()
        self._tts_preprocessor_config = tts_preprocessor_config
        self._faster_first_response = faster_first_response
        self._segment_method = segment_method
        self.interrupt_method = interrupt_method

        # The situation manager is held by reference and re-read per turn.
        # Caching the *state object* here would keep serving a stale mode after
        # a switch, because every update replaces the object.
        self._situation_manager = situation
        self._memory = memory
        self._config = config
        #: Optional screen-summary provider. Held as a callable rather than as
        #: the observation itself, because validity (switch, age, source
        #: window) has to be decided at prompt-assembly time, not at wiring
        #: time: the user can switch observation off while the model is
        #: generating. The bridge owns that judgement.
        self._screen_summary_provider: Optional[Callable[[], Any]] = None
        if screen_summary_provider is None:
            screen_summary_provider = self._resolve_screen_provider()
        self._screen_summary_provider = screen_summary_provider

        # Conversation working set, mirroring upstream's short-term memory.
        self._memory_messages: List[Dict[str, Any]] = []
        self._interrupt_handled = False

        # Turn tracking so late output from a cancelled turn is dropped.
        self._active_turn: Optional[TurnId] = None
        self._cancelled_turns: set[str] = set()

        logger.info("AemeathAgent initialised.")

    @staticmethod
    def _resolve_screen_provider() -> Optional[Callable[[], Any]]:
        """Fall back to the process-wide runtime's screen provider.

        This is only a safety net for agents constructed without an explicit
        provider. The supported path is :meth:`set_screen_summary_provider`,
        because the runtime is not necessarily registered as the module global
        when the agent is built (``build_runtime`` returns a runtime without
        claiming the singleton), and reading a *different* runtime here would
        attach the agent to a screen observer nobody is driving.
        """
        try:
            from .runtime import _runtime

            if _runtime is None or _runtime.bridge is None:
                return None
            return _runtime.bridge.current_screen_summary
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("No screen summary provider available: {}", exc)
            return None

    def set_screen_summary_provider(self, provider: Optional[Callable[[], Any]]) -> None:
        """Install the callable that reports the currently usable summary.

        The provider returns a :class:`~aemeath.interfaces.ScreenObservation`
        only when that observation is genuinely usable right now, and ``None``
        otherwise. The agent never decides validity itself and never caches the
        result: a cached summary would survive the user switching observation
        off, which is exactly what the acceptance criteria forbid.

        Args:
            provider: Zero-argument callable, or ``None`` to disable screen
                context for this agent.
        """
        self._screen_summary_provider = provider

    def _usable_screen_summary(self):
        """The screen observation this turn may cite, if any.

        Returns:
            A ``ScreenObservation``, or ``None`` when there is no usable one.
            A provider failure is reported as "no summary" rather than being
            allowed to break generation.
        """
        if self._screen_summary_provider is None:
            return None
        try:
            return self._screen_summary_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Screen summary lookup failed; continuing without it: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Situation state
    # ------------------------------------------------------------------

    @property
    def situation(self) -> SituationState:
        """The current situation state, read fresh on every access.

        Reading through to the manager on each call is what makes a mode
        switch during generation take effect on the very next check.
        """
        if self._situation_manager is not None:
            return self._situation_manager.state
        return SituationState()

    def update_situation(self, situation: SituationState) -> None:
        """Replace the current situation state.

        Kept for compatibility with callers that push a state object; when a
        manager is present the manager remains authoritative.
        """
        if self._situation_manager is not None:
            logger.debug(
                "Ignoring pushed situation state; the manager is authoritative."
            )
            return
        self._situation = situation
        logger.debug(
            "Situation updated: mode={} voice_allowed={}",
            situation.mode.value,
            situation.voice_allowed,
        )

    # ------------------------------------------------------------------
    # AgentInterface
    # ------------------------------------------------------------------

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        """Load working context from Aemeath's authoritative local history.

        Upstream's JSON history is deliberately **not** read: it is no longer a
        source of truth, and reading it would resurrect deleted content. Other
        agents keep their original behaviour.
        """
        self._memory_messages = []

        store = self._store()
        if store is None:
            logger.warning("No local store available; working context is empty.")
            return

        try:
            messages = store.conversation_messages(history_uid)
        except Exception as exc:  # pragma: no cover - depends on local database
            logger.error("Failed to load local history: {}", exc)
            return

        for message in messages:
            if message.status == "deleted":
                # A forgotten message must not return through the prompt.
                continue
            role = "user" if message.role == "user" else "assistant"
            if message.content:
                self._memory_messages.append({"role": role, "content": message.content})

        logger.info(
            "Loaded {} messages of working context from local history.",
            len(self._memory_messages),
        )

    def _store(self):
        """The local message store, when one is wired in."""
        if self._memory is None:
            return None
        return getattr(self._memory, "store", None)

    def clear_working_context(self) -> None:
        """Drop the in-memory working context.

        Called after a deletion or correction so text that no longer exists in
        the database cannot keep being sent to the model.
        """
        self._memory_messages = []
        logger.info("Working context cleared.")

    def handle_interrupt(self, heard_response: str) -> None:
        """Record that the user interrupted, and close the spoken text.

        Only the portion the user actually heard is kept as the assistant turn;
        the rest must not be remembered as if it had been said.
        """
        if self._interrupt_handled:
            return
        self._interrupt_handled = True

        # Any in-flight turn is now cancelled; its remaining output is dropped.
        if self._active_turn is not None:
            self._cancelled_turns.add(self._active_turn)
            logger.info("Turn {} cancelled by interruption.", self._active_turn)

        heard = (heard_response or "").strip()
        if self._memory_messages and self._memory_messages[-1]["role"] == "assistant":
            # Replace what was not heard with only the heard prefix.
            self._memory_messages[-1]["content"] = f"{heard}..." if heard else "..."
        elif heard:
            self._memory_messages.append(
                {"role": "assistant", "content": f"{heard}..."}
            )

        marker_role = "system" if self.interrupt_method == "system" else "user"
        self._memory_messages.append(
            {"role": marker_role, "content": INTERRUPT_MARKER}
        )
        logger.info("Interrupt handled with role '{}'.", marker_role)

    def reset_interrupt(self) -> None:
        """Clear the interrupt flag so the next turn is processed normally."""
        self._interrupt_handled = False

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    async def _build_messages(
        self, input_data: BatchInput
    ) -> tuple[List[Dict[str, Any]], str]:
        """Assemble the message list and system prompt for this turn.

        Returns:
            A tuple of (messages, system_prompt).
        """
        source = self._event_source(input_data)
        user_text = self._extract_text(input_data)
        has_image = bool(input_data.images)

        memories = []
        memory_available = True
        # A greeting or a mode switch does not need an embedding round-trip.
        if self._memory is not None and user_text and self._is_user_source(source):
            try:
                memories = await self._memory.recall(user_text)
            except Exception as exc:
                # Retrieval failure must be visible, and must not be papered
                # over by pretending we remembered something.
                memory_available = False
                logger.error("Memory recall failed; continuing without it: {}", exc)

        # Read the situation once per turn, at prompt-assembly time. Holding a
        # cached state object would keep an old mode after a switch, because
        # every update replaces the object rather than mutating it.
        situation = self.situation

        # Screen context is resolved here, at the same moment, for the same
        # reason: a summary is only valid while observation is on and it has
        # not aged out. This is the connection A04 was missing — the prompt
        # previously had no way to learn what was on screen, so a proactive
        # message could not refer to it.
        screen_summary = self._usable_screen_summary()

        situational = build_user_prompt(
            event=source,
            user_text=user_text,
            memories=memories,
            situation=situation,
            has_screen_image=has_image,
            memory_available=memory_available,
            screen_summary=screen_summary,
        )

        system_prompt = build_system_prompt(
            persona=self._persona,
            situation=situation,
            memories=memories,
            memory_available=memory_available,
            recent_turns=self._recent_turn_limit(),
        )

        messages: List[Dict[str, Any]] = list(self._memory_messages)
        # Attach any screen image to this turn's message so the vision-capable
        # model actually receives it; the system prompt alone is not enough.
        content = self._to_multimodal_content(input_data, situational)
        messages.append({"role": "user", "content": content})
        return messages, system_prompt

    def _recent_turn_limit(self) -> int:
        """Number of recent turns to keep in working context."""
        if self._config is not None:
            return self._config.memory.recent_turns
        return 12

    @staticmethod
    def _event_source(input_data: BatchInput) -> EventSource:
        """Determine whether this input is a user turn or a proactive one."""
        metadata = input_data.metadata or {}
        if metadata.get("proactive_speak"):
            return EventSource.PROACTIVE
        return EventSource.USER_TEXT

    @staticmethod
    def _is_user_source(source: EventSource) -> bool:
        """Whether an event source represents the user actually communicating."""
        return source in (EventSource.USER_TEXT, EventSource.USER_VOICE)

    @staticmethod
    def _extract_text(input_data: BatchInput) -> str:
        """Flatten text parts of a batch input."""
        parts = [t.content for t in (input_data.texts or []) if t.content]
        return "\n".join(parts).strip()

    @staticmethod
    def _to_multimodal_content(
        input_data: BatchInput, text: str
    ) -> Union[str, List[Dict[str, Any]]]:
        """Build OpenAI-style message content, including images when present."""
        images = input_data.images or []
        if not images:
            return text

        content: List[Dict[str, Any]] = []
        for image in images:
            if image.source is ImageSource.SCREEN and image.data:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,{image.data}"
                        },
                    }
                )
        content.append({"type": "text", "text": text})
        return content

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def _chat_function_factory(
        self,
    ) -> Callable[[BatchInput], AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]]:
        """Create the streaming chat pipeline.

        Reuses upstream's transformers so segmentation, expression extraction
        and TTS filtering behave consistently with the rest of the client.
        """

        @tts_filter(self._tts_preprocessor_config)
        @display_processor()
        @actions_extractor(self._live2d_model)
        @sentence_divider(
            faster_first_response=self._faster_first_response,
            segment_method=self._segment_method,
            valid_tags=["think"],
        )
        async def chat_with_memory(
            input_data: BatchInput,
        ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
            """Yield raw text chunks from the LLM for this turn."""
            messages, system_prompt = await self._build_messages(input_data)
            user_text = self._extract_text(input_data)
            source = self._event_source(input_data)

            # Only real user turns enter working context; a proactive prompt is
            # Aemeath's own initiative and must not be stored as user speech.
            if self._is_user_source(source) and user_text:
                self._memory_messages.append(
                    {"role": "user", "content": user_text}
                )

            turn_id = self._active_turn
            token_stream = self._llm.chat_completion(messages, system_prompt)
            complete = ""

            async for event in token_stream:
                # Drop output belonging to a superseded or cancelled turn.
                if turn_id is not None and str(turn_id) in self._cancelled_turns:
                    logger.info("Dropping output for cancelled turn {}.", turn_id)
                    return

                chunk = ""
                if isinstance(event, dict) and event.get("type") == "text_delta":
                    chunk = event.get("text", "")
                elif isinstance(event, str):
                    chunk = event
                if chunk:
                    complete += chunk
                    yield chunk

            if complete:
                self._memory_messages.append(
                    {"role": "assistant", "content": complete}
                )

        return chat_with_memory

    async def chat(
        self, input_data: BatchInput
    ) -> AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]:
        """Run one generation turn.

        Yields upstream ``SentenceOutput`` objects (display text, TTS text and
        actions) for the conversation handler to dispatch.
        """
        # A new turn supersedes any previous one.
        if self._active_turn is not None:
            self._cancelled_turns.add(str(self._active_turn))
        self._active_turn = TurnId.new()
        self._interrupt_handled = False

        logger.debug(
            "Starting turn {} (mode={}, voice_allowed={})",
            self._active_turn,
            self.situation.mode.value,
            self.situation.voice_allowed,
        )

        try:
            pipeline = self._chat_function_factory()
            async for output in pipeline(input_data):
                yield output
        except Exception as exc:
            # Surface generation failures instead of emitting a fake reply.
            logger.exception("Generation failed for turn {}: {}", self._active_turn, exc)
            raise

    def display_name(self) -> str:
        """Name used when emitting output."""
        return "Aemeath"
