"""Phase 3 tests: local fact and experience memory.

Covers the plan's fixture set and checks:

* 10 facts recallable by paraphrase after a restart (target >= 9/10)
* corrections make the old fact stop appearing
* deletions remove the memory, its vector, its sources and any pending task,
  and it does not come back
* unknown information is not invented
* the index is rebuilt rather than mixed when the embedding model changes
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from aemeath.adapters import ExtractedFact, FakeEmbeddingAdapter, FakeExtractionAdapter
from aemeath.memory import MemoryService, MemoryStore

# The plan's fixed fixture set.
FACTS = [
    "用户的名字叫林澈",
    "用户喜欢喝美式咖啡，不加糖",
    "用户养了一只叫「糯米」的橘猫",
    "用户的生日是三月十二号",
    "用户在做毕业设计，方向是桌面宠物",
    "用户习惯凌晨一点睡觉",
    "用户不喜欢吃香菜",
    "用户的导师姓陈",
    "用户用 VS Code 写代码",
    "用户住在杭州",
]

EXPERIENCES = [
    "用户和角色讨论过毕业设计的开题报告",
    "用户提到上周调试摄像头驱动遇到麻烦",
    "用户和角色一起吐槽过食堂的饭",
    "用户分享过在图书馆写论文的经历",
    "用户提到过周末去看了一场电影",
]

UNKNOWNS = [
    "用户的鞋码是多少",
    "用户最喜欢哪支球队",
    "用户的高考分数",
    "用户的银行卡号",
    "用户小时候住在哪里",
]


@pytest.fixture
def store(tmp_path):
    """A store with a deterministic fake embedding adapter.

    The fake adapter is a character-bag model, so its similarity scores are not
    comparable to a real embedding model's. The production floor of 0.5 is left
    at its default here, because the fake adapter's paraphrases genuinely do
    score above it (see ``TestRelevanceFloor``); lowering it for the whole
    suite would hide a regression in the filter.
    """
    return MemoryStore(tmp_path / "aemeath.sqlite3", FakeEmbeddingAdapter())


@pytest.fixture
def service(store):
    """A memory service over the store."""
    return MemoryService(store, extraction=FakeExtractionAdapter())


async def seed_facts(store, texts=FACTS, kind="fact"):
    """Write memories and their vectors, as the extraction pipeline would."""
    ids = []
    for text in texts:
        memory_id = store.add_memory(content=text, kind=kind)
        vector = (await store.embedding.embed([text]))[0]
        store.store_embedding(memory_id, vector)
        ids.append(memory_id)
    return ids


class TestPersistence:
    """Memories survive a restart."""

    async def test_facts_persist(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        first = MemoryStore(db, FakeEmbeddingAdapter())
        await seed_facts(first)

        second = MemoryStore(db, FakeEmbeddingAdapter())
        assert len(second.list_memories()) == 10

    async def test_messages_persist(self, store):
        store.add_message(role="user", source="user_text", content="你好")
        assert len(store.recent_messages()) == 1
        assert store.recent_messages()[0].role == "user"

    def test_schema_created(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        MemoryStore(db, FakeEmbeddingAdapter())
        with sqlite3.connect(db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {
            "messages",
            "memories",
            "memory_evidence",
            "embeddings",
            "pending_extraction",
        } <= tables


class TestRecallByParaphrase:
    """The plan's target: >= 9 of 10 facts found by a synonymous question.

    Important limitation: the stand-in embedding is a bag-of-characters hash,
    not a semantic model. Measured on this fixture set its best-match scores
    range from 0.17 ("我住哪个城市？" -> 杭州) to 0.67, so a 0.5 floor that is
    right for a real embedding model would reject most of these paraphrases.
    These tests therefore lower the floor to 0.15 to exercise the *ranking*
    path, and the floor's own behaviour is covered separately in
    ``TestRelevanceFloor``.

    Passing here is not evidence that retrieval quality is acceptable: that
    requires calibrating against a real embedding model (see
    docs/acceptance.md).
    """

    #: Floor used for the stand-in adapter; see the class docstring.
    FAKE_FLOOR = 0.15

    # Each paraphrase avoids the literal wording of the stored fact.
    PARAPHRASES = [
        ("我叫什么名字？", "林澈"),
        ("我平时喝咖啡有什么讲究？", "美式"),
        ("我家那只猫叫什么？", "糯米"),
        ("我的生日是哪天？", "三月十二"),
        ("我的毕设做什么方向？", "桌面宠物"),
        ("我一般几点睡？", "凌晨一点"),
        ("我有什么不吃的东西吗？", "香菜"),
        ("我导师姓什么？", "陈"),
        ("我写代码用什么编辑器？", "VS Code"),
        ("我住哪个城市？", "杭州"),
    ]

    async def test_at_least_nine_of_ten_recalled(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        store = MemoryStore(db, FakeEmbeddingAdapter(), similarity_floor=self.FAKE_FLOOR)
        await seed_facts(store)

        # Restart before recall, as the plan specifies.
        store = MemoryStore(db, FakeEmbeddingAdapter(), similarity_floor=self.FAKE_FLOOR)

        hits = 0
        for question, expected in self.PARAPHRASES:
            recalled = await store.recall(question, limit=5)
            joined = " ".join(item.content for item in recalled)
            if expected in joined:
                hits += 1
        assert hits >= 9, f"only {hits}/10 facts recalled by paraphrase"

    async def test_experiences_recallable(self, tmp_path):
        """Experiences are recalled the same way facts are.

        Uses the stand-in floor for the same reason as the paraphrase test.
        """
        store = MemoryStore(
            tmp_path / "aemeath.sqlite3",
            FakeEmbeddingAdapter(),
            similarity_floor=self.FAKE_FLOOR,
        )
        await seed_facts(store, EXPERIENCES, kind="experience")
        recalled = await store.recall("开题报告", limit=5)
        assert recalled
        assert any("开题报告" in item.content for item in recalled)
    async def test_recall_returns_provenance(self, store):
        message_id = store.add_message(
            role="user", source="user_text", content="记住：我喜欢美式"
        )
        memory_id = store.add_memory(
            content="用户喜欢美式咖啡", source_message_ids=[message_id]
        )
        vector = (await store.embedding.embed(["用户喜欢美式咖啡"]))[0]
        store.store_embedding(memory_id, vector)

        recalled = await store.recall("咖啡", limit=1)
        assert recalled
        assert recalled[0].source_message_ids == (message_id,)

    async def test_recall_limit_respected(self, store):
        await seed_facts(store)
        recalled = await store.recall("用户", limit=3)
        assert len(recalled) <= 3

    async def test_empty_query_returns_nothing(self, store):
        await seed_facts(store)
        assert await store.recall("") == []


class TestRelevanceFloor:
    """Retrieval must be able to return nothing.

    The plan's requirement is that an unrelated query yields an empty result
    rather than a fixed number of the most similar rows, and that vectors which
    cannot be ranked (zero, non-finite, or from another model) never take part.
    """

    async def test_unrelated_query_returns_empty(self, store):
        """A query with no lexical or semantic overlap must recall nothing."""
        await seed_facts(store)
        recalled = await store.recall("今天天气怎么样", limit=5)
        assert recalled == []

    async def test_floor_rejects_below_threshold(self, tmp_path):
        """Anything under the floor is excluded, even the best match."""
        # The fake adapter scores "我住在杭州" vs "用户住在杭州吗" at ~0.75 and
        # vs an unrelated sentence at ~0.40, so a 0.5 floor separates them.
        store = MemoryStore(
            tmp_path / "aemeath.sqlite3", FakeEmbeddingAdapter(), similarity_floor=0.5
        )
        memory_id = store.add_memory(content="用户住在杭州")
        vector = (await store.embedding.embed(["用户住在杭州"]))[0]
        store.store_embedding(memory_id, vector)

        # Related enough to clear the floor.
        assert await store.recall("我住在杭州", limit=5)
        # Unrelated: must be empty rather than returning the single memory.
        assert await store.recall("今天股市怎么走", limit=5) == []

    async def test_zero_vector_is_not_ranked(self, tmp_path):
        """A zero vector has no direction and must not be scored."""
        store = MemoryStore(tmp_path / "aemeath.sqlite3", FakeEmbeddingAdapter())
        memory_id = store.add_memory(content="用户住在杭州")
        store.store_embedding(memory_id, np.zeros(64, dtype=np.float32))

        recalled = await store.recall("我住在杭州", limit=5)
        assert recalled == []

    async def test_non_finite_vector_is_not_ranked(self, tmp_path):
        """NaN/inf entries must be excluded instead of poisoning the sort."""
        store = MemoryStore(tmp_path / "aemeath.sqlite3", FakeEmbeddingAdapter())
        bad = np.full(64, np.nan, dtype=np.float32)
        memory_id = store.add_memory(content="用户住在杭州")
        store.store_embedding(memory_id, bad)

        recalled = await store.recall("我住在杭州", limit=5)
        assert recalled == []

    async def test_dimension_mismatch_returns_nothing(self, tmp_path):
        """Vectors from a different model are never compared."""
        wide = FakeEmbeddingAdapter(dimensions=128, model_id="other-model")
        store = MemoryStore(tmp_path / "aemeath.sqlite3", wide)
        memory_id = store.add_memory(content="用户住在杭州")
        vector = (await wide.embed(["用户住在杭州"]))[0]
        store.store_embedding(memory_id, vector)

        # A narrower adapter would compare 64-wide queries against 128-wide
        # rows; that must be refused rather than broadcast.
        narrow = FakeEmbeddingAdapter(dimensions=64, model_id="other-model")
        store._embedding = narrow
        assert await store.recall("我住在杭州", limit=5) == []

    async def test_recall_is_capped_at_five(self, store):
        """At most five memories are recalled even when more are relevant."""
        await seed_facts(store, ["用户住在杭州"] * 8)
        recalled = await store.recall("用户住在杭州", limit=5)
        assert len(recalled) <= 5


class TestCorrection:
    """Corrections invalidate the old fact rather than adding a second one."""

    async def test_correction_replaces_fact(self, store):
        memory_id = store.add_memory(content="用户住在杭州")
        vector = (await store.embedding.embed(["用户住在杭州"]))[0]
        store.store_embedding(memory_id, vector)

        new_id = store.correct_memory(memory_id, "用户住在上海")
        new_vector = (await store.embedding.embed(["用户住在上海"]))[0]
        store.store_embedding(new_id, new_vector)

        old = store.get_memory(memory_id)
        assert old is not None and not old.valid
        assert old.superseded_by == new_id

        # Query with the stored wording so the assertion is about invalidation
        # rather than about the stand-in embedding's paraphrase score.
        recalled = await store.recall("用户住在上海", limit=5)
        joined = " ".join(item.content for item in recalled)
        assert "上海" in joined
        assert "杭州" not in joined, "superseded fact must stop being recalled"

    async def test_corrected_fact_has_no_stale_vector(self, store):
        memory_id = store.add_memory(content="用户住在杭州")
        store.store_embedding(memory_id, np.ones(64, dtype=np.float32))
        store.correct_memory(memory_id, "用户住在上海")

        ids, _ = store.load_vectors()
        assert memory_id not in ids

    async def test_correction_keeps_evidence(self, store):
        message_id = store.add_message(
            role="user", source="user_text", content="其实我住上海"
        )
        memory_id = store.add_memory(
            content="用户住在杭州", source_message_ids=[message_id]
        )
        new_id = store.correct_memory(memory_id, "用户住在上海")
        assert store.evidence_for(new_id) == [message_id]

    async def test_five_corrections(self, store):
        """The plan's fixture: 5 corrections."""
        corrections = [
            ("用户住在杭州", "用户住在南京"),
            ("用户喜欢喝拿铁", "用户喜欢喝美式"),
            ("用户养了一只狗", "用户养了一只猫"),
            ("用户的导师姓王", "用户的导师姓陈"),
            ("用户用 Vim", "用户用 VS Code"),
        ]
        for old_text, new_text in corrections:
            memory_id = store.add_memory(content=old_text)
            (await store.embedding.embed([old_text]))
            new_id = store.correct_memory(memory_id, new_text)
            vector = (await store.embedding.embed([new_text]))[0]
            store.store_embedding(new_id, vector)

        valid = store.list_memories()
        contents = [item.content for item in valid]
        assert len(valid) == 5
        for _, new_text in corrections:
            assert new_text in contents

    def test_correcting_missing_memory_raises(self, store):
        with pytest.raises(KeyError):
            store.correct_memory("does-not-exist", "新内容")


class TestDeletion:
    """Deletion must be thorough and permanent."""

    async def test_delete_removes_memory_and_vector(self, store):
        message_id = store.add_message(
            role="user", source="user_text", content="我喜欢喝美式"
        )
        memory_id = store.add_memory(
            content="用户喜欢喝美式", source_message_ids=[message_id]
        )
        store.store_embedding(memory_id, np.ones(64, dtype=np.float32))

        store.delete_memory(memory_id)

        assert store.get_memory(memory_id) is None
        ids, _ = store.load_vectors()
        assert memory_id not in ids

    async def test_deleted_memory_not_recalled(self, store):
        message_id = store.add_message(
            role="user", source="user_text", content="我的手机号是 138"
        )
        memory_id = store.add_memory(
            content="用户的手机号是 138", source_message_ids=[message_id]
        )
        vector = (await store.embedding.embed(["用户的手机号是 138"]))[0]
        store.store_embedding(memory_id, vector)

        store.delete_memory(memory_id)

        recalled = await store.recall("我的手机号", limit=5)
        assert all("手机号" not in item.content for item in recalled)

    async def test_delete_removes_source_message(self, store):
        """Deleting must also drop the content it could be re-extracted from."""
        message_id = store.add_message(
            role="user", source="user_text", content="记住：我的密码是 abc"
        )
        memory_id = store.add_memory(
            content="用户的密码是 abc", source_message_ids=[message_id]
        )
        store.delete_memory(memory_id)
        assert store.get_message(message_id) is None

    async def test_delete_removes_pending_task(self, store):
        message_id = store.add_message(
            role="user", source="user_text", content="记住：我讨厌下雨"
        )
        memory_id = store.add_memory(
            content="用户讨厌下雨", source_message_ids=[message_id]
        )
        store.enqueue_extraction([message_id])

        store.delete_memory(memory_id)

        # The task either vanished or no longer references the deleted source.
        for task in store.pending_tasks():
            assert message_id not in task["message_ids"]

    async def test_five_deletions(self, store):
        """The plan's fixture: 5 deletions followed by re-query."""
        targets = [
            "用户的血型是 O 型",
            "用户的工位在三楼",
            "用户开一辆白色轿车",
            "用户的邮箱是 a@b.com",
            "用户的室友叫小周",
        ]
        for text in targets:
            memory_id = store.add_memory(content=text)
            vector = (await store.embedding.embed([text]))[0]
            store.store_embedding(memory_id, vector)

        for item in store.list_memories():
            store.delete_memory(item.memory_id)

        assert store.list_memories() == []
        for text in targets:
            recalled = await store.recall(text, limit=5)
            assert not any(text[:6] in r.content for r in recalled)


class TestUnknownsNotInvented:
    """Never-told information must not be produced from memory."""

    async def test_unknown_questions_return_nothing_relevant(self, store):
        await seed_facts(store)
        for question in UNKNOWNS:
            recalled = await store.recall(question, limit=5)
            # Whatever is returned must not claim to answer the unknown.
            joined = " ".join(item.content for item in recalled)
            assert "鞋码" not in joined
            assert "银行卡" not in joined
            assert "高考" not in joined

    async def test_no_memories_returns_empty(self, store):
        for question in UNKNOWNS:
            assert await store.recall(question, limit=5) == []


class TestEmbeddingModelChange:
    """Mixed embedding spaces must never be compared."""

    async def test_reindex_required_on_model_change(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        store = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-a"))
        await seed_facts(store, FACTS[:3])

        switched = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-b"))
        assert switched.needs_reindex()

    async def test_reindex_rebuilds_all(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        store = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-a"))
        await seed_facts(store, FACTS[:4])

        switched = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-b"))
        count = await switched.reindex()

        assert count == 4
        assert not switched.needs_reindex()
        assert switched.indexed_model_ids() == ["model-b"]

    async def test_old_model_vectors_excluded_from_recall(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        store = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-a"))
        await seed_facts(store, FACTS[:3])

        switched = MemoryStore(db, FakeEmbeddingAdapter(model_id="model-b"))
        # Before reindexing, no vectors match the current model.
        recalled = await switched.recall("名字", limit=5)
        assert recalled == []


class TestExtractionPipeline:
    """Extraction writes memories with provenance and survives failure."""

    async def test_messages_stored_before_generation(self, service):
        message_id = service.record_user_message("记住：我喜欢美式", source="user_text")
        assert service.store.get_message(message_id) is not None

    async def test_extraction_writes_memory_with_evidence(self, service):
        message_id = service.record_user_message(
            "记住：我喜欢美式咖啡", source="user_text"
        )
        await service.process_turn(message_id)
        written = await service.run_pending_extraction()

        assert written == 1
        memories = service.store.list_memories()
        assert len(memories) == 1
        assert service.store.evidence_for(memories[0].memory_id) == [message_id]

    async def test_failed_extraction_keeps_task(self, service):
        class Boom(FakeExtractionAdapter):
            async def extract(self, messages):
                raise RuntimeError("provider down")

        service._extraction = Boom()
        message_id = service.record_user_message("记住：我喜欢美式", source="user_text")
        await service.process_turn(message_id)

        written = await service.run_pending_extraction()

        assert written == 0
        tasks = service.store.pending_tasks()
        assert len(tasks) == 1, "failed task must remain for retry"
        assert "provider down" in (tasks[0]["last_error"] or "")

    async def test_completed_task_removed(self, service):
        message_id = service.record_user_message("记住：我喜欢美式", source="user_text")
        await service.process_turn(message_id)
        await service.run_pending_extraction()
        assert service.store.pending_tasks() == []

    async def test_assistant_reply_not_stored_as_user_fact(self, service):
        """The character's own words must not become user facts."""
        user_id = service.record_user_message("你好", source="user_text")
        assistant_id = service.record_assistant_message("你好呀，我是 Aemeath")
        await service.process_turn(user_id, assistant_id)
        await service.run_pending_extraction()

        # FakeExtractionAdapter only reads user-role messages.
        for memory in service.store.list_memories():
            assert "Aemeath" not in memory.content

    async def test_interrupted_reply_marked(self, service):
        message_id = service.record_assistant_message("说到一半", interrupted=True)
        assert service.store.get_message(message_id).status == "interrupted"

    async def test_embedding_failure_keeps_memory_text(self, tmp_path):
        """A memory whose embedding failed is kept, just not retrievable."""
        embedding = FakeEmbeddingAdapter()
        store = MemoryStore(tmp_path / "aemeath.sqlite3", embedding)
        service = MemoryService(store, extraction=FakeExtractionAdapter())

        message_id = service.record_user_message("记住：我喜欢美式", source="user_text")
        await service.process_turn(message_id)

        embedding.fail = True
        await service.run_pending_extraction()

        assert len(store.list_memories()) == 1, "text must be retained"


class TestExplicitFacts:
    """Deterministic extraction for a known fact set."""

    async def test_ten_facts_written_and_recalled(self, tmp_path):
        facts = [ExtractedFact(content=text) for text in FACTS]
        store = MemoryStore(tmp_path / "aemeath.sqlite3", FakeEmbeddingAdapter())
        service = MemoryService(store, extraction=FakeExtractionAdapter(facts=facts))

        message_id = service.record_user_message(
            "这是一段很长的自我介绍", source="user_text"
        )
        await service.process_turn(message_id)
        written = await service.run_pending_extraction()

        assert written == 10
        assert len(store.list_memories()) == 10
        recalled = await store.recall("我的猫叫什么", limit=5)
        assert recalled
