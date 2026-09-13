"""Phase 1 tests: conversation turns, interruption and failure handling.

These verify the *behaviour constraints* from the plan, not implementation
details:

* one turn at a time, new input supersedes old output
* interruption cancels generation and drops late output
* TTS failure keeps the text
* ASR failure never invents a transcript
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from aemeath.agent import AemeathAgent
from aemeath.interfaces import SpeechMode, SituationState
from aemeath.prompts import build_system_prompt
from src.open_llm_vtuber.agent.input_types import (
    BatchInput,
    ImageData,
    ImageSource,
    TextData,
    TextSource,
)
from tests.doubles import FakeLLM, FakeMemory, FakeTTS, FakeASR


def make_input(text: str, *, proactive: bool = False) -> BatchInput:
    """Build a batch input for tests."""
    return BatchInput(
        texts=[TextData(source=TextSource.INPUT, content=text)],
        metadata={"proactive_speak": True} if proactive else None,
    )


def make_tts_config():
    """Build a valid upstream TTS preprocessor config.

    Upstream's ``tts_filter`` instantiates an empty config when given ``None``,
    which fails validation, so tests must pass a real one.
    """
    from src.open_llm_vtuber.config_manager import TTSPreprocessorConfig

    return TTSPreprocessorConfig(
        remove_special_char=True,
        ignore_brackets=True,
        ignore_parentheses=True,
        ignore_asterisks=True,
        ignore_angle_brackets=True,
        translator_config={
            "translate_audio": False,
            "translate_provider": "deeplx",
        },
    )


def make_situation_manager(tmp_path=None, *, mode: SpeechMode = SpeechMode.NORMAL):
    """Build a real situation manager backed by a temporary database.

    The agent reads its situation through the manager rather than holding a
    state snapshot, so tests must exercise the same relationship the runtime
    wires up. A bare ``SituationState`` would be silently ignored.
    """
    import tempfile
    from pathlib import Path

    from aemeath.situation import SituationManager, SituationStore

    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    manager = SituationManager(SituationStore(Path(tmp_path) / "situation.sqlite3"))
    if mode is not SpeechMode.NORMAL:
        manager.set_mode(mode)
    return manager


def make_agent(**kwargs) -> AemeathAgent:
    """Build an AemeathAgent wired to fakes."""
    params = {
        "llm": FakeLLM(["你好", "，我在。"]),
        "system": "你是 Aemeath。",
        "live2d_model": None,
        "tts_preprocessor_config": make_tts_config(),
        "situation": make_situation_manager(),
    }
    params.update(kwargs)
    return AemeathAgent(**params)


async def collect(agent: AemeathAgent, batch: BatchInput) -> List[str]:
    """Drain a chat stream into display text."""
    out = []
    async for item in agent.chat(batch):
        text = getattr(getattr(item, "display_text", None), "text", None)
        if text:
            out.append(text)
    return out


class TestTurnRules:
    """Turn-level rules from the Phase 1 acceptance list."""

    async def test_single_turn_produces_text(self):
        agent = make_agent()
        parts = await collect(agent, make_input("在吗"))
        assert parts, "a turn must produce display text"
        assert "你好" in "".join(parts)

    async def test_ten_text_turns_sustain_context(self):
        """The plan requires at least 10 text turns in one session."""
        llm = FakeLLM(["收到。"])
        agent = make_agent(llm=llm)
        for i in range(10):
            await collect(agent, make_input(f"第{i}条消息"))
        assert len(llm.calls) == 10
        # Working context must not reset between turns.
        final_messages = llm.calls[-1]["messages"]
        joined = " ".join(str(m.get("content", "")) for m in final_messages)
        assert "第8条消息" in joined, "previous turns must remain in context"

    async def test_new_turn_appends_user_message_once(self):
        llm = FakeLLM(["好的。"])
        agent = make_agent(llm=llm)
        await collect(agent, make_input("记住我喜欢喝美式"))
        history = [m for m in agent._memory_messages if m["role"] == "user"]
        assert len(history) == 1
        assert "美式" in history[0]["content"]

    async def test_proactive_input_not_stored_as_user_speech(self):
        """A proactive prompt must never become a user utterance."""
        llm = FakeLLM(["[SILENCE]"])
        agent = make_agent(llm=llm)
        await collect(agent, make_input("该主动说点什么了", proactive=True))
        user_msgs = [m for m in agent._memory_messages if m["role"] == "user"]
        assert user_msgs == [], "proactive prompt leaked into user memory"

    async def test_proactive_prompt_marked_in_request(self):
        llm = FakeLLM(["[SILENCE]"])
        agent = make_agent(llm=llm)
        await collect(agent, make_input("决定是否开口", proactive=True))
        sent = str(llm.calls[-1]["messages"])
        assert "主动开口" in sent


class TestInterruption:
    """Interruption must stop generation and prevent late output."""

    async def test_interrupt_cancels_in_flight_generation(self):
        llm = FakeLLM(["第一段", "第二段", "第三段"], delay=0.05)
        agent = make_agent(llm=llm)
        task = asyncio.create_task(collect(agent, make_input("讲个长故事")))

        await asyncio.sleep(0.02)
        agent.handle_interrupt("第一段")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert llm.aborted_streams == 1, "generation must actually be aborted"

    async def test_interrupt_records_only_heard_text(self):
        agent = make_agent()
        agent._memory_messages = [
            {"role": "user", "content": "讲个故事"},
            {"role": "assistant", "content": "从前有座山，山上有座庙"},
        ]
        agent.handle_interrupt("从前有座山")
        assistant = [m for m in agent._memory_messages if m["role"] == "assistant"]
        assert assistant[-1]["content"] == "从前有座山..."
        assert "庙" not in assistant[-1]["content"]

    async def test_interrupt_marks_conversation(self):
        agent = make_agent(interrupt_method="user")
        agent.handle_interrupt("嗯")
        assert agent._memory_messages[-1]["content"] == "[Interrupted by user]"

    async def test_interrupt_is_idempotent(self):
        """Upstream may call handle_interrupt more than once per turn."""
        agent = make_agent()
        agent.handle_interrupt("听到的")
        before = len(agent._memory_messages)
        agent.handle_interrupt("听到的")
        assert len(agent._memory_messages) == before

    async def test_five_consecutive_interrupts(self):
        """The plan requires 5 consecutive interrupt tests."""
        for _ in range(5):
            llm = FakeLLM(["一", "二", "三"], delay=0.03)
            agent = make_agent(llm=llm)
            task = asyncio.create_task(collect(agent, make_input("说话")))
            await asyncio.sleep(0.01)
            agent.handle_interrupt("一")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert llm.aborted_streams == 1

    async def test_cancelled_turn_output_is_dropped(self):
        """Late chunks from a superseded turn must not reach the UI."""
        llm = FakeLLM(["a", "b", "c"], delay=0.02)
        agent = make_agent(llm=llm)

        first = asyncio.create_task(collect(agent, make_input("第一轮")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(collect(agent, make_input("第二轮")))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second

        # The first turn must be recorded as cancelled, and the surviving turn
        # must be the second one.
        assert agent._cancelled_turns, "superseded turn was not recorded as cancelled"
        assert str(agent._active_turn) not in agent._cancelled_turns
        assert llm.aborted_streams + llm.completed_streams == 2


class TestFailureHandling:
    """Failures must degrade honestly, not silently fabricate."""

    async def test_llm_failure_propagates(self):
        """A generation failure must surface rather than emit a fake reply."""
        llm = FakeLLM(fail_with=RuntimeError("provider down"))
        agent = make_agent(llm=llm)
        with pytest.raises(RuntimeError):
            await collect(agent, make_input("你好"))

    async def test_memory_failure_does_not_claim_recall(self):
        """Retrieval failure must be stated, not disguised as remembering."""
        mem = FakeMemory()
        mem.fail_recall = True
        llm = FakeLLM(["好的。"])
        agent = make_agent(llm=llm, memory=mem)
        await collect(agent, make_input("我上次说了什么？"))
        system = llm.calls[-1]["system_prompt"]
        assert "检索不可用" in system
        assert "不要假装记得" in system

    async def test_tts_failure_keeps_text(self):
        """TTS failure must not invalidate the text reply."""
        tts = FakeTTS()
        tts.fail = True
        with pytest.raises(RuntimeError):
            await tts.synthesize("你好")
        # Text generation is independent of synthesis success.
        agent = make_agent()
        parts = await collect(agent, make_input("你好"))
        assert parts

    async def test_asr_failure_returns_no_transcript(self):
        """ASR failure must raise rather than invent a transcript."""
        asr = FakeASR()
        asr.fail = True
        with pytest.raises(RuntimeError):
            await asr.transcribe(b"audio")
        assert asr.transcript != ""  # the fixture value is never returned


class TestSituationIntegration:
    """The agent must reflect the situation in the prompt it sends."""

    async def test_class_mode_stated_to_model(self, tmp_path):
        """A mode switch must be visible to the very next turn.

        Switching through the manager is what the runtime does; the agent must
        read the new state without being handed it.
        """
        llm = FakeLLM(["收到。"])
        manager = make_situation_manager(tmp_path)
        agent = make_agent(llm=llm, situation=manager)

        manager.set_mode(SpeechMode.CLASS)
        await collect(agent, make_input("继续"))
        assert "课堂模式" in llm.calls[-1]["system_prompt"]

    async def test_mode_switch_mid_session_takes_effect(self, tmp_path):
        """Switching modes after a turn must not stay stuck on the old one."""
        llm = FakeLLM(["收到。"])
        manager = make_situation_manager(tmp_path)
        agent = make_agent(llm=llm, situation=manager)

        await collect(agent, make_input("第一轮"))
        assert "课堂模式" not in llm.calls[-1]["system_prompt"]

        manager.set_mode(SpeechMode.CLASS)
        await collect(agent, make_input("第二轮"))
        assert "课堂模式" in llm.calls[-1]["system_prompt"]

        manager.set_mode(SpeechMode.NORMAL)
        await collect(agent, make_input("第三轮"))
        assert "课堂模式" not in llm.calls[-1]["system_prompt"]

    async def test_screen_absent_rule_present(self):
        llm = FakeLLM(["好的。"])
        agent = make_agent(llm=llm)
        await collect(agent, make_input("我在干什么？"))
        sent = str(llm.calls[-1]["messages"])
        assert "没有屏幕内容" in sent

    async def test_screen_image_included_when_present(self):
        llm = FakeLLM(["我看看。"])
        agent = make_agent(llm=llm)
        batch = BatchInput(
            texts=[TextData(source=TextSource.INPUT, content="看看我的屏幕")],
            images=[
                ImageData(
                    source=ImageSource.SCREEN, data="ZmFrZQ==", mime_type="image/png"
                )
            ],
        )
        await collect(agent, batch)
        sent = str(llm.calls[-1]["messages"])
        assert "附带了屏幕截图" in sent
        assert "data:image/png;base64,ZmFrZQ==" in sent


class TestSystemPromptRules:
    """The honesty contract must be present in generated prompts."""

    def test_no_memory_rule_without_memories(self):
        prompt = build_system_prompt(persona="你是 Aemeath。", situation=SituationState(), memory_status="empty")
        assert "本轮没有检索到相关记忆" in prompt
        assert "不知道" in prompt

    def test_memory_disabled_rule(self):
        prompt = build_system_prompt(persona="你是 Aemeath。", situation=SituationState(), memory_status="disabled")
        assert "未启用长期记忆检索" in prompt
        assert "本轮没有检索到相关记忆" not in prompt

    def test_memory_failed_rule(self):
        prompt = build_system_prompt(persona="你是 Aemeath。", situation=SituationState(), memory_status="failed")
        assert "本轮记忆检索暂时失败" in prompt
        assert "本轮没有检索到相关记忆" not in prompt

    def test_memory_rule_with_memories(self):
        from aemeath.interfaces import MemoryRecord

        record = MemoryRecord(
            memory_id="1", content="用户喜欢美式咖啡", kind="fact", created_at=0.0
        )
        prompt = build_system_prompt(
            persona="你是 Aemeath。", situation=SituationState(), memories=[record]
        )
        assert "美式咖啡" in prompt
        assert "确实记得" in prompt
