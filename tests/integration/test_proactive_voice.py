"""Integration tests: proactive voice output and delivery counting (T01).

These close architecture-review findings **A01** (proactive messages carried an
empty audio frame, so there was no audible proactive speech), **A02** (a
proactive message was counted as delivered once at generation time and again
when the display receipt arrived, so an unshown message consumed the hourly
budget) and **A05** (the upstream proactive generator imported upstream types
under the wrong module name).

The plan forbids substituting the proactive generator: the entry point under
test is upstream's own ``ServiceContext._install_aemeath_proactive_generator``,
installed by the real ``init_agent`` path. Only the model, the TTS engine and
the audio sink are replaced, exactly as the harness documents.

Counting is asserted from ``bridge.run_proactive()`` rather than from
``deliver_proactive()``, because A02 was invisible to a delivery-only probe.
"""

from __future__ import annotations

import asyncio
import base64
import io
import wave

import pytest

from aemeath.interfaces import SpeechMode
from tests.doubles import FakeLLM


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def decode_wav(audio_b64: str) -> bytes:
    """Decode one audio payload and prove it is a real, non-empty WAV.

    Returns:
        The decoded bytes.

    Raises:
        AssertionError: When the payload is empty or is not decodable audio.
    """
    assert audio_b64, "proactive audio must not be an empty string"
    raw = base64.b64decode(audio_b64)
    assert raw, "proactive audio must decode to actual bytes"
    with wave.open(io.BytesIO(raw), "rb") as handle:
        frames = handle.getnframes()
        assert frames > 0, "proactive audio must contain frames, not a bare header"
    return raw


def install_real_generator(harness, monkeypatch, *, prompt: str = "随便说点什么。"):
    """Install upstream's own proactive generator on the real service context.

    This is the production entry point: ``ServiceContext.init_agent`` calls
    ``_install_aemeath_proactive_generator()`` when the Aemeath agent is
    selected. Tests that used a stand-in generator could not observe A01,
    because the defect lived in this function and in what the bridge did with
    its output.

    Args:
        harness: A wired :class:`IntegrationHarness`.
        monkeypatch: pytest's monkeypatch fixture, used to pin the prompt.
        prompt: Text the generator is asked to send to the model.

    Returns:
        The installed generator (the bridge's ``_proactive_generate``).
    """
    async def _prompt(is_startup: bool) -> str:
        """Return a fixed prompt instead of loading one from disk."""
        return prompt

    context = harness.context
    monkeypatch.setattr(
        type(context), "_aemeath_proactive_prompt", staticmethod(_prompt), raising=False
    )
    context._install_aemeath_proactive_generator()
    return harness.bridge._proactive_generate


async def proactive_turn(harness, monkeypatch, *, text: str, is_startup: bool = False,
                         llm=None):
    """Run one proactive attempt through the real generator and the bridge.

    Args:
        harness: A wired :class:`IntegrationHarness`.
        monkeypatch: pytest's monkeypatch fixture.
        text: What the model is made to answer.
        is_startup: Whether this is the startup greeting.
        llm: Optional LLM double; defaults to a fresh one yielding ``text``.

    Returns:
        The delivered message, or ``None``.
    """
    if llm is not None:
        harness.agent._llm = llm
    install_real_generator(harness, monkeypatch)
    return await harness.bridge.run_proactive(is_startup=is_startup)


async def enable_proactive(harness):
    """Turn the proactive switch on, as the settings screen would."""
    await harness.bridge.set_switch("proactive", True)


# ----------------------------------------------------------------------
# A01: proactive output must be audible
# ----------------------------------------------------------------------


class TestProactiveAudioIsProduced:
    """The proactive path must reuse the ordinary synthesis pipeline."""

    async def test_proactive_message_carries_decodable_audio(
        self, make_harness, monkeypatch
    ):
        """A normal-mode proactive message produces real audio, not ``""``.

        This is the A01 regression. Before the fix the bridge called
        ``send_audio(turn_id, "")``, so the client received a payload whose
        audio field was an empty string and nothing was ever heard.
        """
        harness = make_harness(llm=FakeLLM(["主动说一句。"], delay=0.0))
        await enable_proactive(harness)
        harness.websocket.clear()

        message = await proactive_turn(
            harness, monkeypatch, text="主动说一句。", llm=FakeLLM(["主动说一句。"])
        )

        assert message == "主动说一句。", "the proactive message must be delivered"

        audio_frames = harness.websocket.frames_of("audio")
        assert audio_frames, "a proactive message must send an audio frame"

        for frame in audio_frames:
            assert frame.get("audio"), (
                "proactive audio must not be an empty string; the client would "
                "receive a silent placeholder instead of speech"
            )
            decode_wav(frame["audio"])

    async def test_proactive_audio_is_not_a_silent_payload(
        self, make_harness, monkeypatch
    ):
        """The payload must not be upstream's ``audio: None`` silent display.

        ``prepare_audio_payload(audio_path=None)`` yields ``audio: None`` with
        an empty ``volumes`` list. That is a display-only placeholder, and
        treating it as success is how A01 stayed invisible.
        """
        harness = make_harness()
        await enable_proactive(harness)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="非静音的一句。", llm=FakeLLM(["非静音的一句。"])
        )

        frames = harness.websocket.frames_of("audio")
        assert frames, "expected an audio frame"
        frame = frames[0]
        assert frame.get("audio") is not None, "audio must be real, not a null placeholder"
        assert frame.get("volumes"), "a real clip carries lip-sync volumes"

    async def test_proactive_audio_reuses_the_conversation_synthesis_path(
        self, make_harness, monkeypatch
    ):
        """The configured TTS engine must actually be asked to synthesise."""
        harness = make_harness()
        await enable_proactive(harness)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="走真实合成路径。", llm=FakeLLM(["走真实合成路径。"])
        )

        assert harness.context.tts_engine.synthesised, (
            "the proactive path must call the same TTS engine an ordinary reply "
            "uses; an empty send_audio() bypasses synthesis entirely"
        )
        assert any(
            "真实合成路径" in text for text in harness.context.tts_engine.synthesised
        )

    async def test_text_and_audio_share_one_trackable_turn(
        self, make_harness, monkeypatch
    ):
        """Text and audio for one attempt carry the same turn id.

        A single candidate must be one turn; otherwise the client cannot reject
        the audio when the user interrupts the text.
        """
        harness = make_harness()
        await enable_proactive(harness)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="同一轮次。", llm=FakeLLM(["同一轮次。"])
        )

        text_turn = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]
        audio_turn = harness.websocket.frames_of("audio")[-1]["turn_id"]
        assert text_turn == audio_turn, (
            "proactive text and audio must belong to one turn id"
        )

    async def test_proactive_turn_is_registered_with_metrics(
        self, make_harness, monkeypatch
    ):
        """A proactive turn is observable, not an untracked side channel."""
        harness = make_harness()
        await enable_proactive(harness)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="可观测。", llm=FakeLLM(["可观测。"])
        )

        sources = [metric.source for metric in harness.runtime.metrics.turns]
        assert "proactive" in sources, "the proactive turn must be recorded"


class TestProactiveClassroomMode:
    """Class mode allows proactive text but never proactive audio."""

    async def test_class_mode_synthesises_and_plays_nothing(
        self, make_harness, monkeypatch
    ):
        """In class mode the proactive path must not synthesise at all."""
        harness = make_harness()
        await enable_proactive(harness)
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="上课时的小提示。", llm=FakeLLM(["上课时的小提示。"])
        )

        text = harness.websocket.frames_of("aemeath-text")
        assert text, "proactive text is allowed in class mode"
        assert "上课时的小提示" in "".join(frame["text"] for frame in text)

        assert harness.websocket.frames_of("audio") == [], (
            "class mode must suppress proactive audio entirely"
        )
        assert harness.context.tts_engine.synthesised == [], (
            "class mode must not even synthesise proactive speech"
        )

    async def test_switch_to_class_during_generation_suppresses_audio(
        self, make_harness, monkeypatch
    ):
        """Switching to class mode mid-generation still yields text only."""
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowLLM(FakeLLM):
            """LLM double that blocks after its first chunk."""

            async def chat_completion(self, messages, system_prompt, **kwargs):
                self.calls.append({"messages": messages, "system_prompt": system_prompt})
                yield "慢速候选。"
                started.set()
                await release.wait()

        harness = make_harness(llm=SlowLLM(["慢速候选。"]))
        await enable_proactive(harness)
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        attempt = asyncio.create_task(harness.bridge.run_proactive())
        await asyncio.wait_for(started.wait(), timeout=5.0)

        # The user enters class mode while the candidate is being generated.
        await harness.bridge.switch_mode(SpeechMode.CLASS)
        release.set()
        await asyncio.wait_for(attempt, timeout=5.0)

        assert harness.websocket.frames_of("audio") == []
        assert harness.context.tts_engine.synthesised == []


# ----------------------------------------------------------------------
# A02: the display receipt is the single counting entry point
# ----------------------------------------------------------------------


class TestDeliveryCounting:
    """A proactive message is delivered once, and counted once."""

    async def test_not_counted_before_the_receipt(self, make_harness, monkeypatch):
        """Generation alone must not consume the proactive budget."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="尚未显示。", llm=FakeLLM(["尚未显示。"])
        )

        assert scheduler.awaiting_response is False, (
            "a message that was only generated must not be counted as delivered"
        )
        assert len(scheduler._history) == 0, (
            "the hourly budget must not be spent before the client shows the text"
        )

    async def test_first_receipt_counts_exactly_once(self, make_harness, monkeypatch):
        """The first valid display receipt is the only counting entry point.

        A02 was exactly this: ``coordinator.run_proactive`` called
        ``mark_spoken()`` and ``bridge.on_display_receipt`` called it again, so
        one message consumed two slots of the hourly budget.
        """
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="只算一次。", llm=FakeLLM(["只算一次。"])
        )
        turn_id = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]

        await harness.bridge.on_display_receipt(turn_id=turn_id)

        assert len(scheduler._history) == 1, (
            f"one delivered message must be counted once, got {len(scheduler._history)}"
        )
        assert scheduler.awaiting_response is True

    async def test_repeated_receipts_do_not_recount(self, make_harness, monkeypatch):
        """A client that repeats the receipt must not spend more budget."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="重复回执。", llm=FakeLLM(["重复回执。"])
        )
        turn_id = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]

        for _ in range(3):
            await harness.bridge.on_display_receipt(turn_id=turn_id)

        assert len(scheduler._history) == 1, "repeated receipts must not recount"

    async def test_receipt_for_an_unknown_turn_is_ignored(
        self, make_harness, monkeypatch
    ):
        """A receipt for a turn that was never pending changes nothing."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="占位。", llm=FakeLLM(["占位。"])
        )
        await harness.bridge.on_display_receipt(turn_id="not-this-turn")

        assert len(scheduler._history) == 0
        assert harness.bridge.pending_proactive is not None

    async def test_lost_receipt_is_never_counted_as_delivered(
        self, make_harness, monkeypatch
    ):
        """If the client never confirms display, the message is not delivered.

        The hourly budget must survive a client that shows nothing, and the
        unanswered-pause rule must not be armed by a message nobody saw.
        """
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="没有回执。", llm=FakeLLM(["没有回执。"])
        )

        assert len(scheduler._history) == 0
        assert scheduler.awaiting_response is False
        # The placeholder still blocks a duplicate candidate while it is pending.
        assert harness.bridge.pending_proactive is not None

    async def test_hourly_budget_spans_two_delivered_messages(
        self, make_harness, monkeypatch
    ):
        """The cap counts receipts, not generation attempts."""
        harness = make_harness(
            aemeath_overrides={"proactive": {"cooldown_seconds": 1, "max_per_hour": 2}}
        )
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        import time

        # Two messages inside the rolling hour, each far enough back to clear
        # the one-second cooldown but still within the hour window.
        for index, spoken_at in enumerate((time.time() - 60, time.time() - 30)):
            text = f"第{index}条。"
            await proactive_turn(harness, monkeypatch, text=text, llm=FakeLLM([text]))
            turn_id = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]
            # The receipt carries the delivery timestamp, so the history entry
            # lands where a real one would.
            harness.bridge.pending_proactive.sent_at = spoken_at
            await harness.bridge.on_display_receipt(turn_id=turn_id)
            scheduler.mark_user_spoke()

        assert len(scheduler._history) == 2
        decision = await harness.bridge._coordinator.consider_proactive()
        assert decision.eligible is False
        assert "limit" in decision.reason


# ----------------------------------------------------------------------
# Branches: cancellation, disconnect, user interjection, switch off
# ----------------------------------------------------------------------


class TestProactiveFailureBranches:
    """The cancellation and loss branches the plan requires."""

    async def test_send_failure_is_not_counted(self, make_harness, monkeypatch):
        """A transport failure must not be recorded as a delivered message."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler

        calls: list = []
        original = harness.websocket.send_text

        async def failing_send(text: str) -> None:
            """Fail every outbound frame, as a dead socket would."""
            calls.append(text)
            raise RuntimeError("socket is gone")

        harness.bridge._session.sender = failing_send
        harness.websocket.clear()

        delivered = await harness.bridge.deliver_proactive("发不出去。", turn_id="turn-fail")

        assert calls, "the send must have been attempted"
        assert len(scheduler._history) == 0, "a failed send is not a delivery"
        assert delivered is True, (
            "the bridge reports that it attempted delivery; the receipt decides "
            "whether the message counted"
        )
        assert harness.websocket.last_of("aemeath-text") is None

    async def test_disconnect_drops_the_pending_candidate(
        self, make_harness, monkeypatch
    ):
        """A dropped connection must not leave a candidate awaiting a receipt."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="断线前。", llm=FakeLLM(["断线前。"])
        )
        assert harness.bridge.pending_proactive is not None

        harness.bridge.detach_client()

        assert harness.bridge.pending_proactive is None
        assert len(scheduler._history) == 0, "an unshown message is not delivered"
        # A late receipt from the dead client must not resurrect the count.
        await harness.bridge.on_display_receipt(
            turn_id=harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]
        )
        assert len(scheduler._history) == 0

    async def test_late_audio_after_cancel_is_dropped(self, make_harness, monkeypatch):
        """Audio arriving after a cancel must not reach the client."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="会被取消。", llm=FakeLLM(["会被取消。"])
        )
        turn_id = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]

        harness.bridge._coordinator.interrupt()
        before = len(harness.websocket.frames_of("audio"))
        slice_id = await harness.bridge.send_audio(turn_id, "ZmFrZQ==")

        assert slice_id is None, "audio for a cancelled turn must be refused"
        assert len(harness.websocket.frames_of("audio")) == before
        assert harness.bridge.dropped_late_audio >= 1
        assert len(scheduler._history) == 0

    async def test_user_interjection_discards_the_candidate(
        self, make_harness, monkeypatch
    ):
        """A user turn during generation discards the pending candidate."""
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM(FakeLLM):
            """LLM double that holds the candidate until released."""

            async def chat_completion(self, messages, system_prompt, **kwargs):
                self.calls.append({"messages": messages, "system_prompt": system_prompt})
                yield "打断前的候选。"
                started.set()
                await release.wait()

        harness = make_harness(llm=BlockingLLM(["打断前的候选。"]))
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        attempt = asyncio.create_task(harness.bridge.run_proactive())
        await asyncio.wait_for(started.wait(), timeout=5.0)

        # The user starts typing while the candidate is still generating.
        await harness.bridge.on_client_activity(typing=True)
        release.set()
        result = await asyncio.wait_for(attempt, timeout=5.0)

        assert result is None, "a candidate the user pre-empted must not be sent"
        assert harness.bridge.pending_proactive is None
        assert len(scheduler._history) == 0

    async def test_user_turn_cancels_proactive_audio_already_generated(
        self, make_harness, monkeypatch
    ):
        """A user turn arriving between delivery and playback drops the audio."""
        harness = make_harness()
        await enable_proactive(harness)
        harness.websocket.clear()

        await proactive_turn(
            harness, monkeypatch, text="插话前。", llm=FakeLLM(["插话前。"])
        )
        turn_id = harness.websocket.frames_of("aemeath-text")[-1]["turn_id"]

        # The user types: the bridge opens a new turn, superseding the proactive
        # one, and invalidates the pending receipt placeholder.
        from aemeath.interfaces import EventSource

        await harness.bridge.begin_user_turn(source=EventSource.USER_TEXT)

        assert harness.bridge.pending_proactive is None
        assert harness.bridge.may_send_audio(turn_id) is False, (
            "audio for a superseded proactive turn must not be sent"
        )

    async def test_disabling_proactive_mid_generation_stops_output(
        self, make_harness, monkeypatch
    ):
        """Turning the switch off during generation stops the message."""
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM(FakeLLM):
            """LLM double that holds the candidate until released."""

            async def chat_completion(self, messages, system_prompt, **kwargs):
                self.calls.append({"messages": messages, "system_prompt": system_prompt})
                yield "开关关闭前。"
                started.set()
                await release.wait()

        harness = make_harness(llm=BlockingLLM(["开关关闭前。"]))
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        attempt = asyncio.create_task(harness.bridge.run_proactive())
        await asyncio.wait_for(started.wait(), timeout=5.0)

        await harness.bridge.set_switch("proactive", False)
        release.set()
        result = await asyncio.wait_for(attempt, timeout=5.0)

        assert result is None, (
            "the situation must be re-checked after generation; a disabled switch "
            "must stop the message"
        )
        assert harness.websocket.frames_of("aemeath-text") == []
        assert harness.websocket.frames_of("audio") == []
        assert len(scheduler._history) == 0

    async def test_generation_failure_is_not_counted(self, make_harness, monkeypatch):
        """A failing model must not consume budget or produce output."""
        harness = make_harness(llm=FakeLLM(fail_with=RuntimeError("model exploded")))
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()
        install_real_generator(harness, monkeypatch)

        result = await harness.bridge.run_proactive()

        assert result is None
        assert len(scheduler._history) == 0
        assert harness.websocket.frames_of("aemeath-text") == []
        assert harness.websocket.frames_of("audio") == []

    async def test_silence_marker_declines_without_counting(
        self, make_harness, monkeypatch
    ):
        """``[SILENCE]`` means nothing is said, so nothing is counted."""
        harness = make_harness()
        await enable_proactive(harness)
        scheduler = harness.bridge._coordinator.scheduler
        harness.websocket.clear()

        result = await proactive_turn(
            harness, monkeypatch, text="[SILENCE]", llm=FakeLLM(["[SILENCE]"])
        )

        assert result is None
        assert harness.websocket.frames_of("aemeath-text") == []
        assert harness.websocket.frames_of("audio") == []
        assert len(scheduler._history) == 0


# ----------------------------------------------------------------------
# A05: upstream copy and patch must agree
# ----------------------------------------------------------------------


class TestUpstreamGeneratorImport:
    """The generator must import upstream under the one module name in use."""

    def test_generator_does_not_import_upstream_under_a_second_name(self):
        """``service_context`` must use ``src.open_llm_vtuber.*`` like everything else.

        A05: the generator imported ``open_llm_vtuber.agent.input_types``. That
        resolves too — ``src/`` is on ``sys.path`` — but it loads the same file
        a second time under a different name, which is the defect that once made
        every reply silently vanish.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        source = (
            root / "vendor" / "Open-LLM-VTuber" / "src" / "open_llm_vtuber"
            / "service_context.py"
        ).read_text(encoding="utf-8")

        offenders = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"^\s*from\s+open_llm_vtuber\.", line)
            or re.search(r"^\s*import\s+open_llm_vtuber\b", line)
        ]
        assert not offenders, (
            "service_context.py imports upstream under the bare 'open_llm_vtuber' "
            f"name, creating a second copy of every class: {offenders}"
        )

    def test_installed_generator_uses_the_shared_input_types(
        self, make_harness, monkeypatch
    ):
        """The generator's ``BatchInput`` is the class the agent pipeline matches.

        Checked by running it rather than by inspecting imports: the generator
        builds a ``BatchInput`` and hands it to the real agent, so a second copy
        of the class would fail here the same way it once failed in production.
        """
        import sys

        harness = make_harness(llm=FakeLLM(["生成器可用。"]))
        install_real_generator(harness, monkeypatch)

        # Run the installed generator through the agent for real.
        result = asyncio.run(harness.bridge._proactive_generate(False))

        assert result == "生成器可用。", (
            "the generator must drive the agent; a mismatched BatchInput would "
            "make the agent pipeline reject the input"
        )

        bare = sys.modules.get("open_llm_vtuber")
        assert bare is None or bare is sys.modules.get("src.open_llm_vtuber"), (
            "a second upstream module object exists; generator input types would "
            "not match the ones the agent pipeline checks"
        )

    def test_generator_accepts_the_agents_batch_input_type(self, make_harness):
        """The type the generator constructs is the one the agent consumes."""
        from aemeath.agent import AemeathAgent
        from src.open_llm_vtuber.agent.input_types import (
            BatchInput,
            TextData,
            TextSource,
        )

        harness = make_harness()
        batch = BatchInput(
            texts=[TextData(source=TextSource.INPUT, content="随便说点什么。")],
            metadata={"proactive_speak": True},
        )
        # The generator builds exactly this shape; the agent must recognise it
        # as a proactive input rather than treating it as a user statement.
        assert AemeathAgent._event_source(batch).value == "proactive"
        assert isinstance(harness.agent, AemeathAgent)
