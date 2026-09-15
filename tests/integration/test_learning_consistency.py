"""V2-T04 production-entry tests: learning consistency and the nine criteria.

Where ``test_learning_samples.py`` fixes the five evaluation classes *before*
implementation, this file is the regression suite that keeps the issue's
acceptance list true afterwards. Each test names the criterion it covers, and
each one runs through the real production entry points:

* the real ``MemoryStore`` / ``LearningStore`` schema and revision columns;
* the real ``LearningService`` the runtime's worker drives;
* the real ``AemeathAgent`` built by ``AgentFactory``, so the prompt under
  assertion is the one a conversation would actually send;
* the real ``prompts.build_system_prompt``.

Only the model provider is substituted. Isolation: every case builds its own
database under ``tmp_path``; the personal ``data/aemeath.sqlite3`` is never
opened.

Acceptance criteria from issue #17:

A1 entries trace back to a source message, and the UI can show it
A2 natural use in a fitting context; no forced use elsewhere
A3 an ambiguous word keeps its separate meanings, without overwriting
A4 a manual correction wins over later automatic results
A5 disabled / revoked entries are not injected and cannot be re-learned from
   the same evidence; both survive a restart
A6 forgetting a source applies the same boundary to derived learning
A7 learning being unavailable leaves ordinary chat unaffected, and nothing
   claims to have been learned
A8 the core persona is never rewritten by automatic learning
A9 the management surface can read and act on all of the above
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from aemeath.adapters import FakeLearningAdapter, LearningCandidate
from aemeath.interfaces import SituationState
from aemeath.learning import (
    KIND_EXPRESSION,
    KIND_JARGON,
    STATUS_CANDIDATE,
    STATUS_DISABLED,
    STATUS_ENABLED,
    STATUS_REVOKED,
    LearningService,
    LearningStore,
    fingerprint_for,
)
from aemeath.memory import MemoryStore
from aemeath.prompts import build_system_prompt

pytestmark = pytest.mark.asyncio

PERSONA = "身份：你是 Aemeath，用户 Windows 电脑上的 AI 伙伴。\n性格：自然、简洁、有分寸。"


class Stack:
    """A wired learning stack over one isolated database."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        candidates: List[LearningCandidate] | None = None,
        enabled: bool = True,
        adapter=...,
        persona_text: str = PERSONA,
    ) -> None:
        self.db_path = tmp_path / "aemeath.sqlite3"
        self.memory = MemoryStore(self.db_path)
        # ``adapter`` is a sentinel so that "not supplied" (build a default fake)
        # and "explicitly None" (learning has no provider at all) stay different
        # states. Collapsing them made the unconfigured case silently pass as
        # configured.
        if adapter is ...:
            adapter = FakeLearningAdapter(candidates or [])
        self.adapter = adapter
        self.service = LearningService(
            LearningStore(self.db_path), adapter=adapter, enabled=enabled
        )
        self.persona_text = persona_text

    def say(self, content: str, *, role: str = "user") -> str:
        """Record one message."""
        return self.memory.add_message(role=role, source="user_text", content=content)

    async def learn(self, *message_ids: str) -> int:
        """Queue and drain learning exactly as the runtime worker does."""
        self.service.queue_turn(list(message_ids))
        return await self.service.run_pending_learning(self.persona_text)

    def prompt(self, *, persona: str | None = None) -> str:
        """The system prompt a conversation would send."""
        return build_system_prompt(
            persona=persona if persona is not None else self.persona_text,
            situation=SituationState(),
            learnings=self.service.prompt_items(),
        )

    def restart(self) -> "Stack":
        """Rebuild every object over the same database file."""
        return Stack(
            self.db_path.parent,
            adapter=FakeLearningAdapter(),
            persona_text=self.persona_text,
        )


# ----------------------------------------------------------------------
# A1 — traceable to a specific source message, and the UI can show it
# ----------------------------------------------------------------------


async def test_a1_entry_traces_to_its_source_with_a_located_fragment(
    tmp_path: Path,
) -> None:
    """Every learned entry cites at least one message, with the span it came from."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(kind=KIND_JARGON, content="冒烟测试", meaning="快速验证流程")
        ],
    )
    user_id = stack.say("这个功能先冒烟测试一下，看看主流程通不通。")
    await stack.learn(user_id)

    item = stack.service.store.list_items()[0]
    evidence = stack.service.store.evidence_for(item.item_id)
    assert evidence, "学习条目必须有来源证据"
    assert evidence[0].message_id == user_id
    assert evidence[0].fragment, "必须能定位到原文片段，否则界面无法展示来源语境"


async def test_a1_source_content_is_readable_for_display(tmp_path: Path) -> None:
    """The originating message is still readable, so the page can show context."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="先放着")],
    )
    user_id = stack.say("这个先放着吧，回头再说。")
    await stack.learn(user_id)

    item = stack.service.store.list_items()[0]
    evidence = stack.service.store.evidence_for(item.item_id)
    source = stack.memory.get_message(evidence[0].message_id)
    assert source is not None
    assert "先放着" in source.content


# ----------------------------------------------------------------------
# A2 — natural use in a fitting context, none elsewhere
# ----------------------------------------------------------------------


async def test_a2_scenario_travels_with_the_entry(tmp_path: Path) -> None:
    """The scenario is part of the injected block, not dropped."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(
                kind=KIND_JARGON,
                content="上车",
                meaning="参与某个项目或活动",
                scenario="用户招呼别人一起参与时",
            )
        ],
    )
    user_id = stack.say("这个新项目你要不要上车？")
    await stack.learn(user_id)

    prompt = stack.prompt()
    assert "上车" in prompt
    assert "用户招呼别人一起参与时" in prompt


async def test_a2_prompt_forbids_using_the_entry_out_of_context(
    tmp_path: Path,
) -> None:
    """The prompt explicitly scopes learned items to fitting contexts."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="确实")],
    )
    await stack.learn(stack.say("确实，这个我同意。"))

    prompt = stack.prompt()
    assert "与当前语境不符时不要使用" in prompt


async def test_a2_no_learned_block_when_nothing_is_enabled(tmp_path: Path) -> None:
    """With nothing enabled the prompt carries no learning block at all.

    An empty block would still be an instruction about style; an empty store
    means "nothing was learned", not "learn nothing".
    """
    stack = Stack(tmp_path, candidates=[])
    prompt = stack.prompt()
    assert "表达与用词参考" not in prompt
    assert PERSONA in prompt


# ----------------------------------------------------------------------
# A3 — an ambiguous word keeps its meanings; nothing is overwritten
# ----------------------------------------------------------------------


async def test_a3_second_sense_adds_a_meaning_and_keeps_the_first(
    tmp_path: Path,
) -> None:
    """Two senses of one word produce two meaning rows."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(
                kind=KIND_JARGON, content="车", meaning="显卡", scenario="装机语境"
            )
        ],
    )
    await stack.learn(stack.say("想换张车，预算三千。"))

    stack.adapter.candidates = [
        LearningCandidate(
            kind=KIND_JARGON, content="车", meaning="班车", scenario="通勤语境"
        )
    ]
    await stack.learn(stack.say("早上那趟车又晚点了。"))

    items = stack.service.store.list_items(kind=KIND_JARGON)
    assert len(items) == 1, "同词应为一条目"
    assert {m.meaning for m in items[0].meanings} == {"显卡", "班车"}


async def test_a3_unsettled_meaning_is_marked_and_withheld(tmp_path: Path) -> None:
    """An unclear meaning is flagged ``needs_clarification`` and not injected."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(kind=KIND_JARGON, content="上岛", meaning="含义不明确")
        ],
    )
    await stack.learn(stack.say("明天上岛。"))

    item = stack.service.store.list_items(kind=KIND_JARGON)[0]
    assert item.ambiguous
    assert "上岛" not in stack.prompt()


async def test_a3_user_can_settle_the_meaning(tmp_path: Path) -> None:
    """Settling the meaning makes the word usable, with the user's wording."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(kind=KIND_JARGON, content="上岛", meaning="含义不明确")
        ],
    )
    await stack.learn(stack.say("明天上岛。"))
    item = stack.service.store.list_items(kind=KIND_JARGON)[0]

    result = stack.service.resolve_ambiguity(
        item.item_id, [{"meaning": "去岛上那家咖啡馆", "scenario": "约见面时"}]
    )
    assert result["ok"]

    refreshed = stack.service.store.get(item.item_id)
    assert not refreshed.ambiguous
    assert "去岛上那家咖啡馆" in stack.prompt()


# ----------------------------------------------------------------------
# A4 — a manual correction beats later automatic results
# ----------------------------------------------------------------------


async def test_a4_manual_edit_is_not_overwritten_by_a_later_result(
    tmp_path: Path,
) -> None:
    """An edited entry keeps the user's wording when the model proposes it again."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(kind=KIND_JARGON, content="冒烟", meaning="模型第一次的说法")
        ],
    )
    user_id = stack.say("先冒烟一下。")
    await stack.learn(user_id)
    item = stack.service.store.list_items()[0]

    stack.service.edit(item.item_id, meaning="用户改过的含义", scenario="用户写的场景")
    edited = stack.service.store.get(item.item_id)
    assert edited.manual_override, "人工编辑必须留下标记，后续自动结果才不会被覆盖"

    # The same word observed again, with the model's own wording.
    stack.adapter.candidates = [
        LearningCandidate(kind=KIND_JARGON, content="冒烟", meaning="模型第二次的说法")
    ]
    await stack.learn(stack.say("再冒烟一次。"))

    after = stack.service.store.get(item.item_id)
    assert after.meaning == "用户改过的含义", "人工编辑必须优先于后续自动结果"
    assert after.scenario == "用户写的场景"


async def test_a4_manual_edit_reports_a_conflict_on_a_stale_revision(
    tmp_path: Path,
) -> None:
    """A stale edit is refused rather than silently applied over a newer one."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="先放着")],
    )
    await stack.learn(stack.say("先放着吧。"))
    item = stack.service.store.list_items()[0]

    first = stack.service.edit(item.item_id, meaning="第一次修改")
    assert first["ok"]
    stale = stack.service.edit(
        item.item_id, meaning="并发修改", expected_revision=item.revision
    )
    assert stale["ok"] is False and stale["conflict"] is True


async def test_a4_manual_entry_can_be_created_without_a_model(tmp_path: Path) -> None:
    """A hand-written entry works with learning switched off entirely."""
    stack = Stack(tmp_path, enabled=False)
    item_id = stack.service.store.add_item(
        kind=KIND_EXPRESSION,
        content="先放着，回头再说",
        scenario="用户想推迟时",
        status=STATUS_ENABLED,
        origin="manual",
    )
    prompt = stack.prompt()
    assert "先放着，回头再说" in prompt
    assert stack.service.store.get(item_id).origin == "manual"


# ----------------------------------------------------------------------
# A5 — disabled / revoked stop being injected and cannot be re-learned
# ----------------------------------------------------------------------


async def test_a5_disabled_entry_is_not_injected_and_stays_disabled(
    tmp_path: Path,
) -> None:
    """Disabling survives another extraction pass, and an explicit restore undoes it."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="先这样")],
    )
    user_id = stack.say("先这样吧。")
    await stack.learn(user_id)
    item = stack.service.store.list_items()[0]

    stack.service.set_enabled(item.item_id, False)
    assert "先这样" not in stack.prompt()

    await stack.learn(user_id)
    assert stack.service.store.get(item.item_id).status == STATUS_DISABLED

    stack.service.set_enabled(item.item_id, True)
    assert stack.service.store.get(item.item_id).status == STATUS_ENABLED
    assert "先这样" in stack.prompt()


async def test_a5_revocation_is_terminal_and_blocks_relearning(
    tmp_path: Path,
) -> None:
    """A revoked entry cannot be revived by a later observation."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="老规矩")],
    )
    user_id = stack.say("还是老规矩。")
    await stack.learn(user_id)
    item = stack.service.store.list_items()[0]

    stack.service.revoke(item.item_id)
    assert "老规矩" not in stack.prompt()

    written = await stack.learn(user_id)
    assert written == 0
    assert stack.service.store.get(item.item_id).status == STATUS_REVOKED

    # Even an explicit enable is refused: revocation is not a toggle.
    refused = stack.service.set_enabled(item.item_id, True)
    assert refused["ok"] is False


async def test_a5_disabled_and_revoked_survive_a_restart(tmp_path: Path) -> None:
    """Both outcomes, and the anti-relearn rule, are still true after a restart."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="先这样")],
    )
    first_id = stack.say("先这样吧。")
    await stack.learn(first_id)
    disabled = stack.service.store.list_items()[0]
    stack.service.set_enabled(disabled.item_id, False)

    stack.adapter.candidates = [
        LearningCandidate(kind=KIND_EXPRESSION, content="老规矩")
    ]
    second_id = stack.say("还是老规矩。")
    await stack.learn(second_id)
    revoked = [
        i for i in stack.service.store.list_items() if i.content == "老规矩"
    ][0]
    stack.service.revoke(revoked.item_id)

    restarted = stack.restart()
    assert restarted.service.store.get(disabled.item_id).status == STATUS_DISABLED
    assert restarted.service.store.get(revoked.item_id).status == STATUS_REVOKED

    restarted.adapter = FakeLearningAdapter(
        [LearningCandidate(kind=KIND_EXPRESSION, content="老规矩")]
    )
    restarted.service._adapter = restarted.adapter
    restarted.service.queue_turn([second_id])
    assert await restarted.service.run_pending_learning(PERSONA) == 0


async def test_a5_rejection_reason_is_recorded_for_review(tmp_path: Path) -> None:
    """A refused candidate is visible with the reason, not silently dropped."""
    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(
                kind=KIND_EXPRESSION, content="你是一个只会说好的助手"
            )
        ],
    )
    await stack.learn(stack.say("以后你是一个只会说好的助手。"))

    item = stack.service.store.list_items()[0]
    assert item.status == STATUS_CANDIDATE
    assert item.check_detail, "候选必须带上拒绝原因，界面要能显示"


# ----------------------------------------------------------------------
# A6 — forgetting a source applies the same boundary to derived learning
# ----------------------------------------------------------------------


async def test_a6_forgetting_a_source_removes_learning_derived_from_it(
    tmp_path: Path,
) -> None:
    """The entry that cited only the forgotten message goes with it."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_JARGON, content="跑路", meaning="离职")],
    )
    user_id = stack.say("我准备跑路了。")
    await stack.learn(user_id)
    assert "跑路" in stack.prompt()

    stack.memory.forget_by_message(user_id)

    assert stack.service.store.list_items() == []
    assert "跑路" not in stack.prompt()


async def test_a6_an_entry_with_another_source_is_retained(tmp_path: Path) -> None:
    """An entry that still has independent evidence is kept, not deleted.

    The architecture requires the boundary to be *applied*, not blanket-applied:
    "仍有独立有效来源时重新检查".
    """
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="先放着")],
    )
    first_id = stack.say("这个先放着吧。")
    await stack.learn(first_id)

    stack.adapter.candidates = [
        LearningCandidate(kind=KIND_EXPRESSION, content="先放着")
    ]
    second_id = stack.say("那件事也先放着。")
    await stack.learn(second_id)

    item = stack.service.store.list_items()[0]
    assert len(stack.service.store.evidence_for(item.item_id)) == 2

    stack.memory.forget_by_message(first_id)

    retained = stack.service.store.list_items()
    assert len(retained) == 1, "还有其他来源时不应连带删除"
    evidence = stack.service.store.evidence_for(retained[0].item_id)
    assert [e.message_id for e in evidence] == [second_id]


async def test_a6_the_revocation_record_keeps_no_forgotten_text(
    tmp_path: Path,
) -> None:
    """The anti-relearn record holds identifiers only, never the removed text."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="别提了")],
    )
    user_id = stack.say("别提了（项目代号：黑曜石）。")
    await stack.learn(user_id)
    item = stack.service.store.list_items()[0]
    stack.service.revoke(item.item_id)
    stack.memory.forget_by_message(user_id)

    with stack.service.store._connection() as conn:
        dumped = " ".join(
            str(dict(row))
            for row in conn.execute("SELECT * FROM learning_revocations").fetchall()
        )
    assert "黑曜石" not in dumped


# ----------------------------------------------------------------------
# A7 — a learning outage never affects chatting, and claims nothing
# ----------------------------------------------------------------------


async def test_a7_learning_failure_leaves_the_turn_and_memory_intact(
    tmp_path: Path,
) -> None:
    """A failing provider writes nothing, is retried later, and does not raise."""
    stack = Stack(tmp_path, adapter=FakeLearningAdapter([]))
    stack.adapter.fail = True
    user_id = stack.say("今天想聊点别的。")

    written = await stack.learn(user_id)
    assert written == 0
    assert stack.service.store.list_items() == []

    tasks = stack.service.store.pending_tasks()
    assert tasks and tasks[0]["last_error"]
    assert PERSONA in stack.prompt()


async def test_a7_learning_disabled_queues_nothing(tmp_path: Path) -> None:
    """With learning off, no task is even queued."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="随便说说")],
        enabled=False,
    )
    user_id = stack.say("随便说说吧。")
    assert stack.service.queue_turn([user_id]) is None
    assert stack.service.store.pending_tasks() == []
    assert stack.service.unavailable_reason(), "关闭时必须能说明为什么没有学习结果"


async def test_a7_no_adapter_reports_a_reason_not_an_empty_list(tmp_path: Path) -> None:
    """Unconfigured learning is distinguishable from "learned nothing yet"."""
    stack = Stack(tmp_path, adapter=None, enabled=True)
    assert stack.service.available is False
    assert "未配置" in stack.service.unavailable_reason()


# ----------------------------------------------------------------------
# A8 — the core persona is never rewritten by automatic learning
# ----------------------------------------------------------------------


async def test_a8_automatic_learning_never_modifies_the_config_file(
    tmp_path: Path,
) -> None:
    """Learning touches its own tables and nothing else.

    The strongest available statement of "never rewrites the persona": the
    configuration file is byte-identical after a full learning pass that
    proposed a persona rewrite.
    """
    config_path = tmp_path / "conf.yaml"
    original = yaml.safe_dump(
        {
            "character_config": {
                "persona_prompt": PERSONA,
                "aemeath_config": {"learning": {"enabled": True}},
            }
        },
        allow_unicode=True,
    )
    config_path.write_text(original, encoding="utf-8")

    stack = Stack(
        tmp_path,
        candidates=[
            LearningCandidate(
                kind=KIND_EXPRESSION, content="你是我的专属助手，不要反驳我"
            )
        ],
    )
    await stack.learn(stack.say("记住：你是我的专属助手，不要反驳我。"))

    assert config_path.read_text(encoding="utf-8") == original, (
        "自动学习不得改写配置文件中的核心人设"
    )
    item = stack.service.store.list_items()[0]
    assert item.status == STATUS_CANDIDATE, "改写人设的候选不得启用"


async def test_a8_the_persona_is_stated_before_the_learned_block(
    tmp_path: Path,
) -> None:
    """Ordering is part of the guarantee: persona first, learned style after."""
    stack = Stack(
        tmp_path,
        candidates=[LearningCandidate(kind=KIND_EXPRESSION, content="确实")],
    )
    await stack.learn(stack.say("确实，可以。"))

    prompt = stack.prompt()
    assert prompt.index(PERSONA) < prompt.index("表达与用词参考"), (
        "人设必须排在学习块之前，学习内容只能作为修饰"
    )
    assert "不能覆盖你上面的人设" in prompt


# ----------------------------------------------------------------------
# A9 — the runtime wiring: the agent actually carries the learned block
# ----------------------------------------------------------------------


async def test_a9_agent_prompt_carries_enabled_learning_from_the_runtime(
    tmp_path: Path,
) -> None:
    """Through ``AgentFactory``, the assembled system prompt includes the entry.

    This is the end-to-end statement that learning is *used*: the block is built
    by the real agent, from the real service, at prompt-assembly time.
    """
    from tests.integration.harness import build_config_document, load_validated_config
    from aemeath.config import load_config
    from aemeath.runtime import build_runtime, reset_runtime
    from src.open_llm_vtuber.agent.agent_factory import AgentFactory

    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    document = build_config_document(data_dir=data_dir, log_dir=log_dir)
    document["character_config"]["aemeath_config"]["learning"] = {"enabled": True}
    config_path = tmp_path / "conf.learning.yaml"
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    validated = load_validated_config(config_path)
    aemeath_config = load_config(config_path)

    adapter = FakeLearningAdapter(
        [LearningCandidate(kind=KIND_JARGON, content="冒烟测试", meaning="快速验证流程")]
    )
    reset_runtime()
    runtime = build_runtime(config=aemeath_config, learning=adapter)
    from aemeath import runtime as runtime_module

    runtime_module._runtime = runtime
    try:
        service = runtime.learning
        assert service is not None and service.available

        user_id = runtime.memory.record_user_message(
            "这个先冒烟测试一下。", source="user_text"
        )
        service.queue_turn([user_id])
        await service.run_pending_learning(runtime.persona_text())

        character_config = validated.character_config
        agent = AgentFactory.create_agent(
            conversation_agent_choice=character_config.agent_config.conversation_agent_choice,
            agent_settings=character_config.agent_config.agent_settings.model_dump(),
            llm_configs=character_config.agent_config.llm_configs.model_dump(),
            system_prompt=character_config.persona_prompt,
            live2d_model=None,
            tts_preprocessor_config=character_config.tts_preprocessor_config,
            aemeath_config=aemeath_config,
        )

        from src.open_llm_vtuber.agent.input_types import (
            BatchInput,
            TextData,
            TextSource,
        )

        batch = BatchInput(
            texts=[TextData(source=TextSource.INPUT, content="这个真麻烦。")],
            images=[],
        )
        messages, system_prompt = await agent._build_messages(batch)

        assert "冒烟测试" in system_prompt, (
            "已启用的学习条目必须出现在真实 agent 组装的系统提示词里"
        )
        assert "快速验证流程" in system_prompt
        assert "表达与用词参考" in system_prompt
    finally:
        await runtime.stop()
        reset_runtime()


async def test_a9_disabling_takes_effect_on_the_next_turn(tmp_path: Path) -> None:
    """The agent re-reads the selection per turn, so a disable is immediate.

    A cached selection would keep injecting a revoked entry until restart, which
    would make the button appear not to work.
    """
    from tests.integration.harness import build_config_document, load_validated_config
    from aemeath.config import load_config
    from aemeath.runtime import build_runtime, reset_runtime
    from src.open_llm_vtuber.agent.agent_factory import AgentFactory
    from src.open_llm_vtuber.agent.input_types import (
        BatchInput,
        TextData,
        TextSource,
    )

    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    document = build_config_document(data_dir=data_dir, log_dir=log_dir)
    document["character_config"]["aemeath_config"]["learning"] = {"enabled": True}
    config_path = tmp_path / "conf.learning2.yaml"
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    validated = load_validated_config(config_path)
    aemeath_config = load_config(config_path)
    adapter = FakeLearningAdapter(
        [LearningCandidate(kind=KIND_EXPRESSION, content="先放着")]
    )

    reset_runtime()
    runtime = build_runtime(config=aemeath_config, learning=adapter)
    from aemeath import runtime as runtime_module

    runtime_module._runtime = runtime
    try:
        service = runtime.learning
        user_id = runtime.memory.record_user_message(
            "这个先放着吧。", source="user_text"
        )
        service.queue_turn([user_id])
        await service.run_pending_learning(runtime.persona_text())

        character_config = validated.character_config
        agent = AgentFactory.create_agent(
            conversation_agent_choice=character_config.agent_config.conversation_agent_choice,
            agent_settings=character_config.agent_config.agent_settings.model_dump(),
            llm_configs=character_config.agent_config.llm_configs.model_dump(),
            system_prompt=character_config.persona_prompt,
            live2d_model=None,
            tts_preprocessor_config=character_config.tts_preprocessor_config,
            aemeath_config=aemeath_config,
        )

        batch = BatchInput(
            texts=[TextData(source=TextSource.INPUT, content="那件事怎么样了？")],
            images=[],
        )
        _, before = await agent._build_messages(batch)
        assert "先放着" in before

        item = service.store.list_items()[0]
        service.set_enabled(item.item_id, False)

        _, after = await agent._build_messages(batch)
        assert "先放着" not in after, "禁用应立即在下一轮生效"
    finally:
        await runtime.stop()
        reset_runtime()
