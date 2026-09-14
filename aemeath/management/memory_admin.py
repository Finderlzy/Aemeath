"""Memory management: search, correction, precise forgetting and restore.

This is the use-case layer behind the management API's memory page. It sits on
the real :class:`~aemeath.memory.MemoryStore` and adds exactly four things the
store does not do by itself:

1. **Telling "empty" apart from "broken".** Retrieval can legitimately return
   nothing, and it can also be unavailable because no embedding provider is
   configured or because the provider failed. Those are different states and
   the UI must not render the second as the first (issue #15, acceptance A1).
2. **Making the *impact* of a removal explicit.** ``delete_memory()`` deletes
   the source messages along with the memory, while ``forget()`` removes only
   the located span. A user asking to drop "one fact" must not silently get the
   cascading behaviour, so :meth:`MemoryAdminService.describe_impact` reports
   which operation a given memory would take and what it would take with it.
3. **Refusing to claim success that did not happen.** ``forget()`` reports
   ``needs_selection`` when it cannot locate the span; that is passed through as
   a failure with the sources attached, never as a completed removal.
4. **Warning before a restore.** A backup predates the forgetting, so restoring
   it can bring forgotten content back. The warning is produced *before* the
   restore runs and the unconfirmed call does not run at all.

Everything here is synchronous in the store but exposed with ``async`` where
retrieval is involved, matching the store's own contract.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from ..memory import MemoryNotConfiguredError, MemoryStore

#: How a removal of a given memory would actually be carried out.
MODE_PRECISE = "precise"
MODE_CASCADING = "cascading"


class MemoryAdminService:
    """Search and edit the local memory through the authoritative store."""

    def __init__(self, store: MemoryStore, *, db_path: Optional[Path] = None) -> None:
        """Bind the service to a store.

        Args:
            store: The authoritative store. The service never opens a second
                database of its own.
            db_path: Location of the SQLite file. Only needed for restore, which
                replaces the file itself. Defaults to the store's own path.
        """
        self._store = store
        self._db_path = Path(db_path) if db_path else Path(getattr(store, "_db_path"))

    @property
    def store(self) -> MemoryStore:
        """The underlying store."""
        return self._store

    # ------------------------------------------------------------------
    # Listing and search
    # ------------------------------------------------------------------

    def list_all(self, *, include_invalid: bool = False) -> Dict[str, Any]:
        """Every memory, with its sources, for the management list.

        This is a plain database read: it does not use the embedding index, so
        it keeps working even when retrieval is unavailable. That is deliberate
        — the user must still be able to *see* and *fix* their memories when the
        embedding provider is down.
        """
        items = self._store.list_memories(include_invalid=include_invalid)
        return {
            "ok": True,
            "available": True,
            "error": "",
            "memories": [self._describe(item) for item in items],
        }

    async def search(self, query: str, *, limit: int = 20) -> Dict[str, Any]:
        """Search memories by meaning, distinguishing empty from unavailable.

        Args:
            query: The user's search text. Empty lists everything.
            limit: Maximum results.

        Returns:
            A payload with ``ok``, ``available``, ``error`` and ``memories``.
            ``ok`` is false only when the search could not be performed.
        """
        if not query.strip():
            result = self.list_all()
            result["memories"] = result["memories"][:limit]
            return result

        try:
            records = await self._store.recall(query, limit=limit)
        except MemoryNotConfiguredError as exc:
            # Configured-but-absent provider: a real failure, not an empty set.
            logger.info("Memory search unavailable: {}", exc)
            return self._unavailable(
                "记忆检索不可用：未配置嵌入模型。列表仍可查看，但按含义搜索需要先配置嵌入模型。"
            )
        except Exception as exc:
            logger.error("Memory search failed: {}", exc)
            return self._unavailable(f"记忆检索失败：{exc}")

        memories = []
        for record in records:
            item = self._store.get_memory(record.memory_id)
            if item is not None:
                memories.append(self._describe(item))

        return {
            "ok": True,
            "available": True,
            "error": "",
            "memories": memories,
        }

    @staticmethod
    def _unavailable(message: str) -> Dict[str, Any]:
        """A failure payload, never an empty success."""
        return {
            "ok": False,
            "available": False,
            "error": message,
            "memories": [],
        }

    def _describe(self, item) -> Dict[str, Any]:
        """One memory as the management UI needs it."""
        evidence = self._store.evidence_records(item.memory_id)
        return {
            "memory_id": item.memory_id,
            "kind": item.kind,
            "content": item.content,
            "valid": item.valid,
            "superseded_by": item.superseded_by or "",
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "sources": [
                {
                    "message_id": record.message_id,
                    "fragment": record.fragment,
                    "content": self._source_content(record.message_id),
                    "revision": record.revision,
                }
                for record in evidence
            ],
        }

    def _source_content(self, message_id: str) -> str:
        """The current text of a source message, or empty when it is gone."""
        message = self._store.get_message(message_id)
        return message.content if message is not None else ""

    # ------------------------------------------------------------------
    # Impact
    # ------------------------------------------------------------------

    def describe_impact(self, memory_id: str) -> Dict[str, Any]:
        """What removing this memory would actually do.

        The management UI shows this before it acts. It exists because the two
        removal operations have genuinely different consequences and the user
        has to be able to tell them apart.

        Returns:
            ``mode`` is ``precise`` when the evidence span can be located (only
            that span is removed) and ``cascading`` when it cannot (the source
            messages go too). Unknown ids report a not-found payload.
        """
        memory = self._store.get_memory(memory_id)
        if memory is None:
            return {
                "ok": False,
                "error": "记忆不存在。",
                "mode": "",
                "removes_source_messages": False,
                "affected_message_ids": [],
            }

        locatable = self._locatable_evidence(memory_id, memory.content)
        if locatable:
            return {
                "ok": True,
                "error": "",
                "mode": MODE_PRECISE,
                "removes_source_messages": False,
                "affected_message_ids": [],
                "message": "只会移除这条事实在原文中的片段，同一条消息里的其他内容保留。",
            }

        evidence = self._store.evidence_records(memory_id)
        message_ids = [record.message_id for record in evidence]
        return {
            "ok": True,
            "error": "",
            "mode": MODE_CASCADING,
            "removes_source_messages": bool(message_ids),
            "affected_message_ids": message_ids,
            "message": (
                "这条记忆没有可定位的原文片段，只能连同来源消息一起删除，"
                "来源消息中的其他内容也会一并消失。"
                if message_ids
                else "这条记忆没有来源消息。"
            ),
        }

    def _locatable_evidence(self, memory_id: str, content: str) -> List[tuple]:
        """Evidence spans that can be removed precisely.

        Mirrors the store's own resolution rule: a recorded fragment is usable,
        and a legacy row with no fragment is usable only when the memory text
        can be found inside the source deterministically.
        """
        from ..memory import _locate_fragment

        resolved: List[tuple] = []
        for record in self._store.evidence_records(memory_id):
            fragment = (record.fragment or "").strip()
            if not fragment:
                source = self._store.get_message(record.message_id)
                if source is not None:
                    fragment = _locate_fragment(source.content, content) or ""
            if fragment:
                resolved.append((record.message_id, fragment))
        return resolved

    # ------------------------------------------------------------------
    # Correction
    # ------------------------------------------------------------------

    async def correct(self, *, memory_id: str, content: str) -> Dict[str, Any]:
        """Replace a memory's content, keeping the old one for provenance.

        The rewrite is delegated to :meth:`MemoryStore.correct_memory`, which
        invalidates the old row and drops its vector. The new memory is indexed
        immediately so that "the corrected fact is recallable" holds as soon as
        the call returns rather than after some later background pass.

        Args:
            memory_id: Memory to supersede.
            content: Replacement text.

        Returns:
            ``{ok, memory_id, error}``. ``memory_id`` is the new memory.
        """
        text = (content or "").strip()
        if not text:
            return {"ok": False, "memory_id": "", "error": "记忆内容不能为空。"}

        existing = self._store.get_memory(memory_id)
        if existing is None:
            return {"ok": False, "memory_id": "", "error": "记忆不存在。"}

        try:
            new_id = self._store.correct_memory(memory_id, text)
        except KeyError:
            return {"ok": False, "memory_id": "", "error": "记忆不存在。"}
        except Exception as exc:
            logger.error("Correcting memory {} failed: {}", memory_id, exc)
            return {"ok": False, "memory_id": "", "error": f"纠正失败：{exc}"}

        await self._index(new_id, text)
        logger.info("Memory {} corrected to {}.", memory_id, new_id)
        return {"ok": True, "memory_id": new_id, "error": ""}

    async def _index(self, memory_id: str, content: str) -> None:
        """Embed one memory if a provider is configured.

        Failure is logged and swallowed on purpose: the correction itself has
        already been committed, and an index that could not be updated is
        recoverable by a rebuild. Reporting the correction as failed would be
        wrong — it did happen.
        """
        embedding = self._store.embedding
        if embedding is None:
            return
        try:
            vector = (await embedding.embed([content]))[0]
            # ``store_embedding_if_current`` re-checks the revision, so a
            # correction that was itself superseded while the request was in
            # flight cannot attach a stale vector.
            revision = self._store.memory_revision(memory_id)
            if revision is not None:
                self._store.store_embedding_if_current(memory_id, vector, revision)
        except Exception as exc:  # pragma: no cover - depends on provider
            logger.warning("Could not index corrected memory {}: {}", memory_id, exc)

    # ------------------------------------------------------------------
    # Forgetting
    # ------------------------------------------------------------------

    async def forget(
        self, memory_id: str, *, fragments: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """Forget one fact precisely.

        Args:
            memory_id: Memory to forget.
            fragments: Explicit spans chosen by the user, used when the stored
                evidence has no locatable span.

        Returns:
            ``{ok, error, needs_selection, removed_fragments, invalidated_derived}``.
            ``ok`` is false when nothing was removed — including the case where
            the span could not be located and the user has to choose.
        """
        memory = self._store.get_memory(memory_id)
        if memory is None:
            return {
                "ok": False,
                "error": "记忆不存在。",
                "needs_selection": [],
                "removed_fragments": [],
                "invalidated_derived": [],
            }

        try:
            result = self._store.forget(memory_id, fragments=fragments)
        except Exception as exc:
            logger.error("Forgetting memory {} failed: {}", memory_id, exc)
            return {
                "ok": False,
                "error": f"遗忘失败：{exc}",
                "needs_selection": [],
                "removed_fragments": [],
                "invalidated_derived": [],
            }

        if not result.get("success"):
            reason = str(result.get("reason") or "")
            if result.get("needs_selection"):
                # Nothing was removed. Say so, and hand back the sources so the
                # user can pick the span.
                return {
                    "ok": False,
                    "error": (
                        "这条记忆没有可定位的原文片段，无法确定该删除哪一段。"
                        "请从来源消息中选择要移除的片段；在你选择之前不会删除任何内容。"
                    ),
                    "needs_selection": result.get("needs_selection") or [],
                    "removed_fragments": [],
                    "invalidated_derived": [],
                }
            return {
                "ok": False,
                "error": f"遗忘未完成：{reason}" if reason else "遗忘未完成。",
                "needs_selection": [],
                "removed_fragments": [],
                "invalidated_derived": [],
            }

        return {
            "ok": True,
            "error": "",
            "needs_selection": [],
            "removed_fragments": result.get("removed_fragments") or [],
            "invalidated_derived": result.get("invalidated_derived") or [],
            "rebuild_required": bool(result.get("rebuild_required")),
        }

    # ------------------------------------------------------------------
    # Backup restore
    # ------------------------------------------------------------------

    def describe_restore(self, backup_path: Path) -> Dict[str, Any]:
        """What restoring this backup would do, stated before it happens.

        A backup was taken before the user forgot anything, so restoring it puts
        that content back. The warning is produced unconditionally for an
        existing file: the mechanism cannot know what was forgotten after the
        copy was made, and a warning that only fires when it can prove the risk
        would stay silent in exactly the case it exists for.
        """
        path = Path(backup_path)
        if not path.is_file():
            return {
                "ok": False,
                "error": "备份文件不存在。",
                "requires_confirmation": False,
                "may_restore_forgotten_content": False,
                "warning": "",
                "backup_path": str(path),
            }

        return {
            "ok": True,
            "error": "",
            "requires_confirmation": True,
            "may_restore_forgotten_content": True,
            "warning": (
                "恢复备份会用备份中的内容替换当前本地数据库。"
                "如果这份备份是在你删除或遗忘记忆之前创建的，"
                "那些已经被遗忘的内容会随备份一起回来。"
                "恢复前请确认这是你想要的结果。"
            ),
            "backup_path": str(path),
            "backup_revision": self._file_revision(path),
        }

    async def restore(self, backup_path: Path, *, confirm: bool = False) -> Dict[str, Any]:
        """Replace the local database with a backup, after confirmation.

        Args:
            backup_path: Backup file to restore from.
            confirm: Whether the user has accepted the warning. When false the
                restore does not run and the warning is returned.

        Returns:
            A result payload. ``ok`` is only true when the database was actually
            replaced.
        """
        description = self.describe_restore(backup_path)
        if not description["ok"]:
            return {**description, "requires_confirmation": False}

        if not confirm:
            return {
                **description,
                "ok": False,
                "error": "恢复备份前需要确认。",
            }

        path = Path(backup_path)
        try:
            # Copy the backup next to the database and move it into place, so an
            # interrupted restore cannot leave a half-written database behind.
            staging = self._db_path.with_suffix(self._db_path.suffix + ".restore.tmp")
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, staging)
            os.replace(staging, self._db_path)
        except Exception as exc:
            logger.error("Restoring backup {} failed: {}", path, exc)
            return {
                **description,
                "ok": False,
                "error": f"恢复备份失败，当前数据库未被替换：{exc}",
            }

        logger.info("Restored local database from {}.", path)
        return {
            **description,
            "ok": True,
            "error": "",
            "restored": True,
            "restart_required": True,
        }

    @staticmethod
    def _file_revision(path: Path) -> str:
        """A content token for a backup file, for display."""
        import hashlib

        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""
        return digest[:16]

    def list_backups(self, directory: Path) -> List[Dict[str, Any]]:
        """Backups available to restore, newest first."""
        path = Path(directory)
        if not path.is_dir():
            return []
        entries = []
        for candidate in sorted(path.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = candidate.stat()
            entries.append(
                {
                    "path": str(candidate),
                    "name": candidate.name,
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        return entries


__all__ = ["MemoryAdminService", "MODE_CASCADING", "MODE_PRECISE"]
