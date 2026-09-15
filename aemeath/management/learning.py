"""Expression and jargon management: the use-case layer behind the two pages.

The two pages — 表达学习 and 黑话词典 — are one store viewed two ways, so they
share this service and differ only by the ``kind`` they filter on. Splitting the
storage would mean three duplicated mechanisms (candidate checking, the
anti-relearn record, the forgetting linkage) that would then drift apart.

This layer adds exactly what the raw store does not provide:

1. **"Nothing learned yet" versus "learning is not running".** Learning is off by
   default and can also be misconfigured. Those are different facts, and a
   header that says "no results" for all three would leave the user unable to
   tell whether to wait or to fix something. ``available``, ``enabled`` and
   ``error`` carry them separately.
2. **Provenance the user can read.** Every item is returned with the messages it
   came from and the located span, so "where did she learn this" is answerable
   without reading the database.
3. **Actions that state their own outcome.** A revoked item is terminal and says
   so; a stale edit is a conflict and changes nothing; an unsettled meaning is
   reported as needing the user's answer rather than being silently dropped.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from ..learning import LearningItem, LearningService

#: Statuses the page can filter on, exposed so the frontend does not invent its
#: own vocabulary.
_STATUS_LABELS = {
    "candidate": "候选",
    "enabled": "已启用",
    "disabled": "已禁用",
    "revoked": "已撤销",
}


class LearningAdminService:
    """Read and act on learned expressions and jargon through the real service."""

    def __init__(self, service: Optional[LearningService], store=None) -> None:
        """Bind to the runtime's learning service.

        Args:
            service: The live service from the runtime, or ``None`` when it
                could not be built. A missing service is reported as such rather
                than as an empty store.
            store: The learning store, used when no service exists (the tables
                can still be read even with learning switched off, so the user
                keeps access to what was learned before).
        """
        self._service = service
        self._store = store if store is not None else getattr(service, "store", None)

    @property
    def store(self):
        """The underlying learning store."""
        return self._store

    @property
    def service(self) -> Optional[LearningService]:
        """The live learning service, when one exists."""
        return self._service

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def overview(
        self,
        *,
        kind: str = "",
        status: str = "",
        query: str = "",
    ) -> Dict[str, Any]:
        """The list a page shows, with the reason when learning is not running.

        Args:
            kind: ``expression`` or ``jargon``; empty lists both.
            status: Optional state filter.
            query: Optional substring filter over content and meaning.

        Returns:
            A payload with ``ok``, ``available``, ``enabled``, ``error``,
            ``counts`` and ``items``.
        """
        if self._store is None:
            return {
                "ok": False,
                "available": False,
                "enabled": False,
                "error": "学习存储不可用：运行时尚未初始化学习模块。",
                "kind": kind,
                "counts": {},
                "items": [],
            }

        try:
            items = self._store.list_items(
                kind=kind or None, status=status or None, query=query
            )
            counts = self._store.counts()
        except Exception as exc:
            logger.error("Reading learning items failed: {}", exc)
            return {
                "ok": False,
                "available": False,
                "enabled": False,
                "error": f"读取学习条目失败：{exc}",
                "kind": kind,
                "counts": {},
                "items": [],
            }

        available = bool(self._service and self._service.available)
        enabled = bool(self._service and self._service.enabled)

        if not available:
            # Still a successful read — the entries that already exist remain
            # fully browsable and editable; only new learning is not happening.
            return {
                "ok": True,
                "available": False,
                "enabled": enabled,
                "error": (
                    self._service.unavailable_reason()
                    if self._service is not None
                    else "学习服务不可用。"
                ),
                "kind": kind,
                "counts": counts,
                "items": [self._describe(item) for item in items],
            }

        return {
            "ok": True,
            "available": True,
            "enabled": enabled,
            "error": "",
            "kind": kind,
            "counts": counts,
            "items": [self._describe(item) for item in items],
        }

    def sources(self, item_id: str) -> Dict[str, Any]:
        """The source messages behind one item, for the "where from" view."""
        if self._store is None:
            return {"ok": False, "error": "学习存储不可用。", "sources": []}
        item = self._store.get(item_id)
        if item is None:
            return {"ok": False, "error": "学习条目不存在。", "sources": []}
        return {
            "ok": True,
            "error": "",
            "item_id": item_id,
            "sources": self._sources(item),
        }

    def _describe(self, item: LearningItem) -> Dict[str, Any]:
        """One item in the shape the page consumes."""
        return {
            "item_id": item.item_id,
            "kind": item.kind,
            "content": item.content,
            "meaning": item.meaning,
            "scenario": item.scenario,
            "status": item.status,
            "origin": item.origin,
            "manual_override": item.manual_override,
            "check_result": item.check_result,
            "check_detail": item.check_detail,
            "revision": item.revision,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "last_used_at": item.last_used_at,
            "ambiguous": item.ambiguous,
            "meanings": [
                {
                    "kind_id": meaning.kind_id,
                    "meaning": meaning.meaning,
                    "scenario": meaning.scenario,
                    "state": meaning.state,
                    "needs_clarification": meaning.needs_clarification,
                }
                for meaning in item.meanings
            ],
            "sources": self._sources(item),
        }

    def _sources(self, item: LearningItem) -> List[Dict[str, Any]]:
        """Evidence rows with the readable source text attached.

        Read even for a forgotten message: the row is gone then, and an empty
        ``content`` is the honest answer — the page shows the entry as having no
        readable source rather than inventing one.
        """
        rows = []
        for record in self._store.evidence_for(item.item_id):
            content = ""
            message = self._message(record.message_id)
            if message is not None:
                content = message.content
            rows.append(
                {
                    "message_id": record.message_id,
                    "fragment": record.fragment,
                    "content": content,
                    "revision": record.revision,
                }
            )
        return rows

    def _message(self, message_id: str):
        """Read one message from the shared database, tolerating its absence."""
        if not message_id:
            return None
        import sqlite3

        try:
            conn = sqlite3.connect(self._store._db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT content, status FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("Could not read source message {}: {}", message_id, exc)
            return None
        if row is None or row["status"] == "deleted":
            return None

        class _View:
            """Minimal readable message view."""

            __slots__ = ("content",)

            def __init__(self, content: str) -> None:
                self.content = content

        return _View(row["content"])

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def edit(
        self,
        *,
        item_id: str,
        content: Optional[str] = None,
        meaning: Optional[str] = None,
        scenario: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply a manual edit through the learning service."""
        if self._service is None:
            return {"ok": False, "error": "学习服务不可用，无法编辑。", "conflict": False}
        result = self._service.edit(
            item_id,
            content=content,
            meaning=meaning,
            scenario=scenario,
            expected_revision=expected_revision,
        )
        return {
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "conflict": bool(result.get("conflict")),
            "item_id": item_id,
            "status": self._current_status(item_id),
        }

    def set_enabled(self, *, item_id: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable one item."""
        if self._service is None:
            return {"ok": False, "error": "学习服务不可用。", "conflict": False}
        result = self._service.set_enabled(item_id, enabled)
        return {
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "conflict": False,
            "item_id": item_id,
            "status": self._current_status(item_id),
        }

    def revoke(self, *, item_id: str) -> Dict[str, Any]:
        """Revoke one item; the outcome is terminal and reported as such."""
        if self._service is None:
            return {"ok": False, "error": "学习服务不可用。", "conflict": False}
        result = self._service.revoke(item_id)
        return {
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "conflict": False,
            "item_id": item_id,
            "status": self._current_status(item_id),
        }

    def resolve_ambiguity(
        self, *, item_id: str, meanings: Sequence[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Settle an ambiguous word's meanings."""
        if self._service is None:
            return {"ok": False, "error": "学习服务不可用。", "conflict": False}
        result = self._service.resolve_ambiguity(item_id, meanings)
        return {
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "conflict": False,
            "item_id": item_id,
            "status": self._current_status(item_id),
        }

    def _current_status(self, item_id: str) -> str:
        """The item's status after an action, for the page to render."""
        if self._store is None:
            return ""
        item = self._store.get(item_id)
        return item.status if item is not None else ""


__all__ = ["LearningAdminService", "_STATUS_LABELS"]
