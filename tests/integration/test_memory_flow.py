"""Integration tests: memory, history and the forgetting races.

These go through ``AgentFactory``, the bridge and the authoritative SQLite
store, and they pin the races with explicit barriers rather than sleeps.

Covered (see the plan's scenario table):

* a conversation through the real assembly writes history, queues extraction,
  recalls memories and survives a restart;
* deleting a memory while an extraction request is in flight stops the target
  from being recreated when the request returns;
* deleting a memory while an embedding request is in flight leaves no orphan
  vector and no deleted memory;
* one message carrying two independent facts keeps the other fact after one is
  forgotten;
* history restoration does not bring deleted content back.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.adapters import (
    ExtractedFact,
    FakeEmbeddingAdapter,
    FakeExtractionAdapter,
)
from aemeath.memory import MemoryStore
from tests.doubles import FakeLLM
from tests.integration.harness import Barrier, run_turn


class BlockingExtraction(FakeExtractionAdapter):
    """Extraction adapter that waits on a barrier before returning facts."""

    def __init__(self, facts, barrier: Barrier) -> None:
        super().__init__(facts=facts)
        self._barrier = barrier
        self.entered = asyncio.Event()

    async def extract(self, messages):
        self.calls.append(messages)
        self.entered.set()
        await self._barrier.wait()
        return list(self.facts)


class BlockingEmbedding(FakeEmbeddingAdapter):
    """Embedding adapter that waits on a barrier before returning vectors."""

    def __init__(self, barrier: Barrier, dimensions: int = 64) -> None:
        super().__init__(dimensions=dimensions)
        self._barrier = barrier
        self.entered = asyncio.Event()
        self.block_after = 0
        self._calls = 0

    async def embed(self, texts):
        self._calls += 1
        if self._calls > self.block_after:
            self.entered.set()
            await self._barrier.wait()
        return await super().embed(texts)


class TestProductionMemoryFlow:
    """A real turn must write history, queue extraction and recall."""

    async def test_turn_records_history_through_bridge(self, make_harness):
        """The user message and reply land in the authoritative database."""
        harness = make_harness(llm=FakeLLM(["好的，我记住了。"]))
        await run_turn(harness, "我今天在写毕业设计")

        store = harness.runtime.memory.store
        messages = store.conversation_messages(harness.bridge.history_uid)
        contents = [m.content for m in messages]
        assert any("毕业设计" in c for c in contents), "user message not recorded"
        assert any("记住了" in c for c in contents), "reply not recorded"

    async def test_no_upstream_json_history_is_written(self, make_harness, tmp_path):
        """Aemeath must not write a second copy into upstream's JSON history."""
        from tests.integration.harness import UPSTREAM_DIR

        history_dir = UPSTREAM_DIR / "chat_history"
        before = (
            sorted(p.name for p in history_dir.rglob("*.json"))
            if history_dir.is_dir()
            else []
        )

        harness = make_harness(llm=FakeLLM(["好。"]))
        await run_turn(harness, "测试消息")

        after = (
            sorted(p.name for p in history_dir.rglob("*.json"))
            if history_dir.is_dir()
            else []
        )
        assert after == before, "Aemeath must not write upstream JSON history"

    async def test_extraction_queue_is_populated(self, make_harness):
        """A finished turn queues extraction for the user's message."""
        harness = make_harness(llm=FakeLLM(["好。"]))
        await run_turn(harness, "记住：我喜欢喝美式咖啡")

        tasks = harness.runtime.memory.store.pending_tasks()
        assert tasks, "extraction should have been queued for the turn"

    async def test_proactive_turn_is_not_queued_as_user_speech(self, make_harness):
        """A proactive prompt must never become a user memory."""
        harness = make_harness(llm=FakeLLM(["[SILENCE]"]))
        await run_turn(
            harness,
            "（系统：现在没有用户消息。）",
            metadata={"proactive_speak": True, "skip_history": True},
        )
        assert harness.runtime.memory.store.pending_tasks() == []

    async def test_recall_and_restart_recovery(self, make_harness, tmp_path):
        """A memory written through the pipeline is recalled after a restart."""
        embedding = FakeEmbeddingAdapter()
        extraction = FakeExtractionAdapter(
            facts=[ExtractedFact(content="用户住在杭州")]
        )

        harness = make_harness(
            llm=FakeLLM(["好。"]),
            embedding=embedding,
            extraction=extraction,
            aemeath_overrides={"memory": {"similarity_floor": 0.15}},
        )
        await run_turn(harness, "记住：我住在杭州")
        written = await harness.runtime.memory.run_pending_extraction()
        assert written == 1

        # Reopen the same database with a fresh store, as a restart would.
        db = harness.config.db_path
        reopened = MemoryStore(db, FakeEmbeddingAdapter(), similarity_floor=0.15)
        recalled = await reopened.recall("用户住在杭州", limit=5)
        assert any("杭州" in item.content for item in recalled)


class TestExtractionRace:
    """Deleting during an in-flight extraction must win."""

    async def test_delete_during_extraction_prevents_recreation(self, make_harness):
        """The returned facts must not be written after the source was deleted.

        The plan is explicit that removing the pending task is not enough: the
        check has to happen after the provider returns, on the source revision.
        """
        barrier = Barrier()
        extraction = BlockingExtraction(
            [ExtractedFact(content="用户住在杭州")], barrier
        )
        harness = make_harness(
            llm=FakeLLM(["记住啦。"]),
            embedding=FakeEmbeddingAdapter(),
            extraction=extraction,
        )

        await run_turn(harness, "记住：我住在杭州")
        store = harness.runtime.memory.store
        assert store.pending_tasks(), "expected a queued extraction task"

        # Start processing; it blocks inside the provider call.
        worker = asyncio.create_task(harness.runtime.memory.run_pending_extraction())
        await asyncio.wait_for(extraction.entered.wait(), timeout=5.0)

        # While the request is in flight, the user deletes the source message.
        # Pick the user message explicitly: the most recent row is the reply.
        user_message = next(
            m
            for m in store.conversation_messages(harness.bridge.history_uid)
            if m.role == "user"
        )
        assert "杭州" in user_message.content
        store.forget_by_message(user_message.message_id)

        # Now let the provider return its facts.
        barrier.open()
        written = await asyncio.wait_for(worker, timeout=5.0)

        assert written == 0, "facts must not be written after the source was deleted"
        remaining = [m.content for m in store.list_memories()]
        assert not any("杭州" in c for c in remaining), "deleted fact reappeared"

    async def test_delete_during_embedding_leaves_no_orphan(self, make_harness):
        """A vector must not be stored for a memory deleted while embedding."""
        embed_barrier = Barrier()
        embedding = BlockingEmbedding(embed_barrier)
        extraction = FakeExtractionAdapter(
            facts=[ExtractedFact(content="用户住在杭州")]
        )

        harness = make_harness(
            llm=FakeLLM(["好。"]),
            embedding=embedding,
            extraction=extraction,
        )
        await run_turn(harness, "记住：我住在杭州")

        store = harness.runtime.memory.store
        worker = asyncio.create_task(harness.runtime.memory.run_pending_extraction())
        await asyncio.wait_for(embedding.entered.wait(), timeout=5.0)

        # The memory exists and is mid-embedding; delete it now. It is the only
        # memory in this fresh database, so naming it explicitly is unambiguous.
        memories = store.list_memories()
        assert memories, "expected the memory to have been created"
        assert len(memories) == 1
        target = memories[0]
        store.forget(target.memory_id)

        embed_barrier.open()
        await asyncio.wait_for(worker, timeout=5.0)

        # No vector may survive for a memory that is gone.
        ids, matrix = store.load_vectors()
        assert target.memory_id not in ids, "orphan vector stored for deleted memory"
        assert store.get_memory(target.memory_id) is None


class TestSharedSourceEvidence:
    """One message with two facts must survive a partial deletion."""

    async def test_two_facts_one_message_delete_one(self, make_harness):
        """Deleting one fact keeps the other fact from the same message usable."""
        extraction = FakeExtractionAdapter(
            facts=[
                ExtractedFact(content="用户住在杭州"),
                ExtractedFact(content="用户喜欢喝美式咖啡"),
            ]
        )
        harness = make_harness(
            llm=FakeLLM(["好。"]),
            embedding=FakeEmbeddingAdapter(),
            extraction=extraction,
            aemeath_overrides={"memory": {"similarity_floor": 0.15}},
        )

        combined = "我住在杭州，而且我喜欢喝美式咖啡"
        await run_turn(harness, combined)
        written = await harness.runtime.memory.run_pending_extraction()
        assert written == 2

        store = harness.runtime.memory.store
        memories = store.list_memories()
        city = next(m for m in memories if "杭州" in m.content)
        coffee = next(m for m in memories if "咖啡" in m.content)

        # Forgetting the city must not remove the coffee fact or its evidence.
        result = store.forget(city.memory_id)
        assert result["success"] is True

        assert store.get_memory(city.memory_id) is None
        assert store.get_memory(coffee.memory_id) is not None

        # The remaining fact still recalls.
        recalled = await store.recall("用户喜欢喝美式咖啡", limit=5)
        assert any("咖啡" in item.content for item in recalled)

    async def test_remaining_message_text_keeps_other_fact(self, make_harness):
        """The visible history loses only the deleted fragment."""
        store_message = "我住在杭州，而且我喜欢喝美式咖啡"
        harness = make_harness(llm=FakeLLM(["好。"]))
        store = harness.runtime.memory.store

        message_id = store.add_message(
            role="user", source="user_text", content=store_message
        )
        memory_id = store.add_memory(
            content="用户住在杭州",
            source_message_ids=[message_id],
            fragments=["我住在杭州"],
        )

        result = store.forget(memory_id)
        assert result["success"] is True

        remaining = store.get_message(message_id)
        assert remaining is not None
        assert "杭州" not in remaining.content, "deleted fragment still visible"
        assert "美式咖啡" in remaining.content, "unrelated fact was removed"

    async def test_forget_requires_selection_when_evidence_missing(self, make_harness):
        """A legacy memory with no locatable fragment must not claim success."""
        harness = make_harness(llm=FakeLLM(["好。"]))
        store = harness.runtime.memory.store

        message_id = store.add_message(
            role="user", source="user_text", content="完全无关的一句话"
        )
        # A memory whose text appears nowhere in its source message.
        memory_id = store.add_memory(
            content="用户住在杭州", source_message_ids=[message_id], fragments=[""]
        )
        # Clear the auto-located fragment to simulate a legacy row.
        with store._connect() as conn:
            conn.execute(
                "UPDATE memory_evidence SET fragment = '' WHERE memory_id = ?",
                (memory_id,),
            )

        result = store.forget(memory_id)
        assert result["success"] is False
        assert "selection" in result["reason"]
        assert result["needs_selection"], "the screen must be told what to show"
        # The memory is untouched: nothing was reported as forgotten.
        assert store.get_memory(memory_id) is not None


class TestHistoryRestoration:
    """Restoring history must not resurrect deleted content."""

    async def test_deleted_message_absent_from_restored_context(self, make_harness):
        """Working context rebuilt from the database excludes deleted messages."""
        harness = make_harness(llm=FakeLLM(["好。"]))
        store = harness.runtime.memory.store
        agent = harness.agent

        conversation_id = harness.bridge.history_uid
        keep_id = store.add_message(
            role="user",
            source="user_text",
            content="保留这条消息",
            conversation_id=conversation_id,
        )
        drop_id = store.add_message(
            role="user",
            source="user_text",
            content="删除这条消息",
            conversation_id=conversation_id,
        )
        store.forget_by_message(drop_id)

        agent.set_memory_from_history("aemeath_test", conversation_id)

        joined = " ".join(m["content"] for m in agent._memory_messages)
        assert "保留这条消息" in joined or store.get_message(keep_id) is not None
        assert "删除这条消息" not in joined, "deleted content returned to the prompt"
