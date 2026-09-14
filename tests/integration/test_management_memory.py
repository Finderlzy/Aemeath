"""V2-T02 production-entry tests: memory management.

The architecture document makes these four classes of isolated case a
**pre-development fixture** for V2-T02 ("开发前须先固化": 纠正、精确遗忘、
来源连带删除、重启一致性), plus a check that a backup restore cannot quietly
bring forgotten content back. They are written first and are the contract the
management memory layer has to satisfy.

Everything goes through the real entry points, as the rest of
``tests/integration/`` does:

* the authoritative SQLite store (``MemoryStore``) with its real schema,
  revision columns and evidence tables — no hand-written SQL shortcuts;
* the real management service object that the FastAPI routes call;
* the real embedding adapter contract, with only the provider substituted.

Isolation: every case builds its own database under ``tmp_path``. The personal
``data/aemeath.sqlite3`` is never opened.

Acceptance criteria covered (issue #15 / implementation-plan V2-T02):

A1 "empty result" and "load failure" are distinguishable
A2 a correction makes the new content recallable and the old content unusable
A3 precise forgetting matches the contract, and survives a restart
A4 a restore warns that forgotten content may come back, and obeys the
   existing restore rules
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from aemeath.adapters import FakeEmbeddingAdapter
from aemeath.memory import MemoryStore

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


class TopicEmbedding(FakeEmbeddingAdapter):
    """Deterministic embedding where similarity follows shared vocabulary.

    Retrieval thresholds are only meaningful if "related" and "unrelated" are
    actually different vectors, so this adapter maps text onto a small
    vocabulary instead of hashing it. Two strings sharing words land close
    together; strings with no shared word land far apart.
    """

    #: Words that carry meaning for these cases; order fixes the dimensions.
    VOCABULARY = (
        "咖啡",
        "不加糖",
        "毕业",
        "设计",
        "导师",
        "猫",
        "过敏",
        "北京",
        "上海",
        "游泳",
        "豆浆",
        "早上",
        "住",
    )

    def __init__(self) -> None:
        super().__init__(dimensions=len(self.VOCABULARY), model_id="topic-embed")

    async def embed(self, texts):
        """Embed texts as normalised vocabulary-frequency vectors."""
        vectors = []
        for text in texts:
            vector = np.array(
                [1.0 if word in text else 0.0 for word in self.VOCABULARY],
                dtype=np.float32,
            )
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector = vector / norm
            vectors.append(vector)
        return vectors


def _store(tmp_path: Path, embedding=None) -> MemoryStore:
    """A real store on an isolated database.

    The threshold is set low so that "related" is decided by the embedding
    geometry under test rather than by a floor that happens to exclude it;
    unrelated text still scores 0.0 and is filtered.
    """
    return MemoryStore(
        tmp_path / "aemeath.sqlite3",
        embedding or TopicEmbedding(),
        similarity_floor=0.3,
    )


def _seed_fact(
    store: MemoryStore,
    *,
    message: str,
    memory: str,
    fragment: str = "",
) -> tuple[str, str]:
    """Record one user message and one memory derived from it.

    Args:
        store: The store to write to.
        message: The source message text.
        memory: The remembered text.
        fragment: Explicit evidence span. Defaults to the memory text, which is
            what the extraction path stores in production.

    Returns:
        ``(message_id, memory_id)``.
    """
    message_id = store.add_message(
        role="user", source="user_text", content=message, status="complete"
    )
    memory_id = store.add_memory(
        content=memory,
        source_message_ids=[message_id],
        fragments=[fragment or memory],
    )
    return message_id, memory_id


@pytest.fixture
def memory_service(tmp_path, monkeypatch):
    """A memory management service on an isolated database.

    Imported lazily so the fixture reports a clean failure while the module
    under test does not exist yet.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    return MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")


# ----------------------------------------------------------------------
# A1 — empty result vs load failure
# ----------------------------------------------------------------------


async def test_no_memories_is_reported_as_an_empty_result_not_a_failure(
    memory_service,
):
    """A store with nothing in it is an empty result, not an error.

    The acceptance criterion exists because the two look identical in a naive
    implementation: both render as an empty list. The distinction has to be
    carried in the payload.
    """
    result = await memory_service.search(query="")

    assert result["ok"] is True
    assert result["available"] is True
    assert result["memories"] == []
    assert result["error"] == ""


async def test_unavailable_retrieval_is_not_reported_as_no_memories(tmp_path):
    """A retrieval failure must not be shown as "you have no memories".

    The store is given memories but **no** embedding adapter, so search cannot
    run. Reporting an empty list here would tell the user their memory is empty
    when it is in fact present but unreachable — precisely the confusion the
    criterion forbids.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    store._embedding = None  # noqa: SLF001 - the unconfigured-provider case
    _seed_fact(store, message="我喜欢咖啡不加糖", memory="喜欢咖啡不加糖")

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.search(query="咖啡")

    assert result["ok"] is False, "an unavailable index must not report success"
    assert result["available"] is False
    assert result["error"], "a failure must carry an explanation"
    assert result["memories"] == []


async def test_a_broken_embedding_provider_is_reported_as_unavailable(tmp_path):
    """An embedding provider that raises is a failure, not an empty result."""
    from aemeath.management.memory_admin import MemoryAdminService

    class BrokenEmbedding(TopicEmbedding):
        async def embed(self, texts):
            raise RuntimeError("embedding service unreachable")

    # Seed the index with a working provider first, then swap in the broken one.
    # Recall returns an empty set without calling the provider when the index
    # holds no vectors at all, so a store that was never indexed would pass this
    # test for the wrong reason.
    working = TopicEmbedding()
    store = _store(tmp_path, embedding=working)
    _, memory_id = _seed_fact(store, message="我喜欢咖啡不加糖", memory="喜欢咖啡不加糖")
    await _index_all(store, embedding=working)
    store._embedding = BrokenEmbedding()  # noqa: SLF001 - the failure under test
    assert memory_id

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.search(query="咖啡")

    assert result["ok"] is False
    assert result["available"] is False
    assert "unreachable" in result["error"]


async def test_an_empty_result_is_still_successful_when_nothing_matches(tmp_path):
    """A working index with no relevant content is a genuine empty result."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    _seed_fact(store, message="我养了一只猫", memory="养了一只猫")

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.search(query="毕业设计导师")

    assert result["ok"] is True
    assert result["available"] is True
    assert result["memories"] == []


# ----------------------------------------------------------------------
# A2 — correction
# ----------------------------------------------------------------------


async def test_correction_makes_the_new_content_recallable_and_the_old_unusable(
    tmp_path,
):
    """After a correction the new wording recalls and the old one is gone.

    The criterion explicitly rejects "only the old vector was deleted" as a
    pass: that would leave the corrected fact unrecallable, which is worse than
    not correcting it.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    _, old_id = _seed_fact(
        store,
        message="我早上喝咖啡",
        memory="早上喝咖啡",
    )
    # Give the old memory a vector so "old content stops recalling" is a real
    # assertion rather than a vacuous one.
    await _index_all(store)

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.correct(memory_id=old_id, content="早上喝豆浆")

    assert result["ok"] is True
    new_id = result["memory_id"]
    assert new_id and new_id != old_id

    # The new memory must be recallable by its own content.
    recalled = await service.search(query="早上")
    recalled_ids = [item["memory_id"] for item in recalled["memories"]]
    assert new_id in recalled_ids, "the corrected fact must be recallable"

    # The old memory keeps its text for provenance but is out of retrieval.
    old = store.get_memory(old_id)
    assert old is not None, "the old memory is superseded, not erased"
    assert old.valid is False
    assert old.superseded_by == new_id

    stale = store.load_vectors()[0]
    assert old_id not in stale, "the stale vector must not remain in the index"


async def test_correcting_an_unknown_memory_fails_without_writing(tmp_path):
    """Correcting a memory that does not exist is refused, not invented."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")

    result = await service.correct(memory_id="does-not-exist", content="新内容")

    assert result["ok"] is False
    assert result["error"]
    assert store.list_memories() == []


# ----------------------------------------------------------------------
# A3 — precise forgetting
# ----------------------------------------------------------------------


async def test_precise_forgetting_keeps_unrelated_content_in_the_same_message(
    tmp_path,
):
    """Forgetting one fact leaves the rest of the source message intact."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    message_id, memory_id = _seed_fact(
        store,
        message="我喜欢咖啡不加糖，另外我下周要交毕业设计",
        memory="喜欢咖啡不加糖",
    )

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.forget(memory_id=memory_id)

    assert result["ok"] is True
    assert store.get_memory(memory_id) is None, "the forgotten fact must be gone"

    remaining = store.get_message(message_id)
    assert remaining is not None, "the source message is kept"
    assert "咖啡" not in remaining.content
    assert "毕业设计" in remaining.content, "unrelated content must survive"


async def test_forgetting_without_a_locatable_span_does_not_claim_success(
    tmp_path,
):
    """A memory whose span cannot be located must ask for a selection.

    Reporting success here would be a lie: nothing would have been removed, and
    the user would believe the fact was forgotten.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    message_id = store.add_message(
        role="user", source="user_text", content="这是一条旧消息", status="complete"
    )
    # A legacy row: no fragment recorded, and the memory text does not appear
    # in the source, so there is nothing deterministic to remove.
    memory_id = store.add_memory(content="一条无法定位的记忆", source_message_ids=[message_id])

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.forget(memory_id=memory_id)

    assert result["ok"] is False
    assert result["needs_selection"], "the user must be asked which span to drop"
    assert store.get_memory(memory_id) is not None, "nothing was removed"


async def test_forgetting_with_an_explicit_selection_succeeds(tmp_path):
    """A user-supplied span lets a legacy memory be forgotten precisely."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    message_id = store.add_message(
        role="user",
        source="user_text",
        content="旧消息：我的电话号码是 12345",
        status="complete",
    )
    memory_id = store.add_memory(content="电话号码是 12345", source_message_ids=[message_id])

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.forget(
        memory_id=memory_id, fragments=["我的电话号码是 12345"]
    )

    assert result["ok"] is True
    assert store.get_memory(memory_id) is None
    remaining = store.get_message(message_id)
    assert remaining is not None
    assert "12345" not in remaining.content


async def test_forgetting_is_consistent_across_a_restart(tmp_path):
    """A forgotten fact stays forgotten after the store is reopened.

    The fixture calls for restart consistency because an in-memory belief that
    the fact is gone is not the same as it being gone from disk.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    db_path = tmp_path / "aemeath.sqlite3"
    store = _store(tmp_path)
    _, memory_id = _seed_fact(
        store, message="我住在北京", memory="住在北京"
    )

    service = MemoryAdminService(store, db_path=db_path)
    result = await service.forget(memory_id=memory_id)
    assert result["ok"] is True

    # Reopen the same database, as a restarted process would.
    reopened = MemoryStore(db_path, TopicEmbedding(), similarity_floor=0.3)
    assert reopened.get_memory(memory_id) is None
    recalled = await reopened.recall("我住在哪里")
    assert memory_id not in [record.memory_id for record in recalled]


async def test_correction_is_consistent_across_a_restart(tmp_path):
    """A corrected fact recalls its new wording after a restart, not the old."""
    from aemeath.management.memory_admin import MemoryAdminService

    db_path = tmp_path / "aemeath.sqlite3"
    store = _store(tmp_path)
    _, old_id = _seed_fact(store, message="我住在北京", memory="住在北京")

    service = MemoryAdminService(store, db_path=db_path)
    corrected = await service.correct(memory_id=old_id, content="住在上海")
    assert corrected["ok"] is True
    new_id = corrected["memory_id"]

    # Reopen the database alone: the correction indexed the new memory when it
    # ran, so the recall below proves the correction *persisted* rather than
    # that a later rebuild happened to re-index it.
    reopened = MemoryStore(db_path, TopicEmbedding(), similarity_floor=0.3)

    recalled = await reopened.recall("住在哪里")
    recalled_ids = [record.memory_id for record in recalled]
    assert new_id in recalled_ids, "the corrected fact must survive the restart"
    assert old_id not in recalled_ids, "the superseded fact must not return"


# ----------------------------------------------------------------------
# A3b — source-cascading deletion is a different operation
# ----------------------------------------------------------------------


async def test_source_cascading_delete_is_reported_as_having_wider_impact(
    tmp_path,
):
    """The two removal operations must be distinguishable before acting.

    ``delete_memory()`` cascades into the source messages while ``forget()``
    removes only the located span. The issue requires the UI to show the
    *actual* impact, so the service has to report which one a given memory would
    use — and must never map "remove just this one" onto the cascading call.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    message_id, memory_id = _seed_fact(
        store,
        message="我喜欢咖啡不加糖，另外我下周要交毕业设计",
        memory="喜欢咖啡不加糖",
    )

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    impact = service.describe_impact(memory_id)

    assert impact["mode"] == "precise", "a locatable span means precise removal"
    assert impact["removes_source_messages"] is False
    assert impact["affected_message_ids"] == []

    # The precise path really does keep the source message.
    result = await service.forget(memory_id=memory_id)
    assert result["ok"] is True
    assert store.get_message(message_id) is not None


async def test_source_cascading_delete_reports_the_messages_it_removes(tmp_path):
    """The cascading operation states up front that sources will go.

    The memory text deliberately does **not** appear in the source message: that
    is what makes the span unlocatable, and an unlocatable span is the case
    where precise removal is impossible and the source messages have to go.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    message_id = store.add_message(
        role="user",
        source="user_text",
        content="一句和记忆内容没有字面重合的原文",
        status="complete",
    )
    memory_id = store.add_memory(
        content="一条无法在原文中定位的记忆", source_message_ids=[message_id]
    )

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    impact = service.describe_impact(memory_id)

    assert impact["mode"] == "cascading"
    assert impact["removes_source_messages"] is True
    assert message_id in impact["affected_message_ids"]


# ----------------------------------------------------------------------
# A4 — backup restore
# ----------------------------------------------------------------------


async def test_backup_restore_warns_that_forgotten_content_may_return(tmp_path):
    """A restore must warn before it runs, naming what may come back.

    The criterion is about honesty: the mechanism can reinstate content the user
    deliberately removed, and saying so afterwards is not a warning.
    """
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_path = backup_dir / "aemeath-backup.sqlite3"

    # A pre-forgetting copy, taken before anything was removed.
    _, memory_id = _seed_fact(store, message="我住在北京", memory="住在北京")
    backup_path.write_bytes((tmp_path / "aemeath.sqlite3").read_bytes())

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.forget(memory_id=memory_id)
    assert result["ok"] is True

    warning = service.describe_restore(backup_path)
    assert warning["requires_confirmation"] is True
    assert warning["may_restore_forgotten_content"] is True
    assert warning["warning"], "the user must be told what may come back"


async def test_restore_is_refused_without_confirmation(tmp_path):
    """An unconfirmed restore does not run."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_path = backup_dir / "aemeath-backup.sqlite3"
    _seed_fact(store, message="我住在北京", memory="住在北京")
    backup_path.write_bytes((tmp_path / "aemeath.sqlite3").read_bytes())

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    result = await service.restore(backup_path, confirm=False)

    assert result["ok"] is False
    assert result["requires_confirmation"] is True


async def test_restore_executes_the_existing_rules_once_confirmed(tmp_path):
    """A confirmed restore replaces the database and reports what happened."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_path = backup_dir / "aemeath-backup.sqlite3"

    _, memory_id = _seed_fact(store, message="我住在北京", memory="住在北京")
    backup_path.write_bytes((tmp_path / "aemeath.sqlite3").read_bytes())

    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")
    assert (await service.forget(memory_id=memory_id))["ok"] is True
    assert store.get_memory(memory_id) is None

    result = await service.restore(backup_path, confirm=True)

    assert result["ok"] is True
    # The existing restore rule is "replace the local database with the backup":
    # the forgotten fact is expected back, which is exactly what the warning
    # said would happen.
    reopened = MemoryStore(
        tmp_path / "aemeath.sqlite3", TopicEmbedding(), similarity_floor=0.3
    )
    assert reopened.get_memory(memory_id) is not None


async def test_a_missing_backup_is_reported_rather_than_assumed(tmp_path):
    """Restoring from a path that does not exist fails clearly."""
    from aemeath.management.memory_admin import MemoryAdminService

    store = _store(tmp_path)
    service = MemoryAdminService(store, db_path=tmp_path / "aemeath.sqlite3")

    result = await service.restore(tmp_path / "backups" / "absent.sqlite3", confirm=True)

    assert result["ok"] is False
    assert result["error"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _index_all(store: MemoryStore, *, embedding=None) -> None:
    """Give every valid memory a vector, as the extraction path would.

    Args:
        store: The store to index.
        embedding: Adapter to embed with. Defaults to the store's own; pass one
            explicitly when the store's adapter is a double that is meant to
            fail, so the fixture does not trip over it.
    """
    adapter = embedding or store.embedding
    for item in store.list_memories():
        vector = (await adapter.embed([item.content]))[0]
        store.store_embedding(item.memory_id, vector)
