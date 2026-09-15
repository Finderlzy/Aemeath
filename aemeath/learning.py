"""Expression and jargon learning: what may be learned from a conversation.

This module turns "the user says things in a particular way" into a small set of
reviewable, revocable entries. It is deliberately a *separate* store from
:mod:`aemeath.memory` even though both live in the same SQLite file:

* a memory is a *fact about the user* and is retrieved by similarity;
* a learning item is a *style or vocabulary note* and is selected by the
  situation, then injected as a hint that may be ignored.

Conflating them would mean a learned catchphrase could be recalled as something
the user said about themselves, which is exactly the confusion the design
forbids. They share the database so a single forgetting operation can clean up
both, and so a backup restores them together.

The rules this module exists to enforce (docs/architecture.md「学习并发与遗忘」):

1. **Learning never rewrites the persona.** Entries are appended context, and a
   candidate that contradicts the identity or personality is refused, not
   stored-and-ignored.
2. **A candidate is not knowledge.** Only ``enabled`` entries are injected.
   Everything the checker could not confirm stays ``candidate`` and is visible
   as such.
3. **Her own words are not evidence.** A phrase Aemeath produced is her existing
   style, not something newly learned; such candidates are rejected.
4. **Manual edits win.** Once the user edits an entry it is stamped
   ``manual_override`` and no later automatic result may overwrite it.
5. **Revocation is durable.** A revoked fingerprint cannot be re-learned from
   the same evidence, and the anti-relearn record keeps only identifiers — never
   the text that was forgotten.
6. **Ambiguity is preserved, not overwritten.** A word with two meanings gets
   two rows; nothing here replaces the earlier meaning.
7. **Forgetting reaches the derivatives.** Removing a source message removes the
   learning evidence that cited it, and any entry left without evidence goes
   with it.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from loguru import logger

from .adapters import LearningAdapter, LearningCandidate

#: Bumped whenever this module's tables change shape.
LEARNING_SCHEMA_VERSION = 1

#: Entry kinds. ``expression`` is "how the user phrases things", ``jargon`` is
#: "a word that means something specific between these two".
KIND_EXPRESSION = "expression"
KIND_JARGON = "jargon"
KINDS = (KIND_EXPRESSION, KIND_JARGON)

#: Entry states. ``candidate`` is the only state that is not injected.
STATUS_CANDIDATE = "candidate"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_CANDIDATE, STATUS_ENABLED, STATUS_DISABLED, STATUS_REVOKED)

#: Where an entry came from.
ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

#: Per-meaning state for jargon: a word whose meaning could not be settled.
STATE_OK = "ok"
STATE_NEEDS_CLARIFICATION = "needs_clarification"

#: The model's own wording when it cannot tell what a word means. Treated as
#: "needs clarification" rather than stored as a meaning.
_UNCLEAR_MEANINGS = (
    "含义不明确",
    "不明确",
    "无法确定",
    "不确定",
    "未知",
    "unknown",
    "unclear",
)

#: Minimum length of a learned expression. A single character is almost always
#: punctuation noise or a particle, and injecting it would change the character's
#: voice for no reason.
_MIN_EXPRESSION_LENGTH = 2
_MAX_CONTENT_LENGTH = 80
_MAX_MEANING_LENGTH = 200
_MAX_SCENARIO_LENGTH = 200

#: Persona-contradicting patterns. A learning item may add colour; it may not
#: rewrite who she is, how she behaves, or what she must never do. These are
#: checked against the *normalised* candidate text.
_PERSONA_CONFLICT_PATTERNS = (
    r"你是",
    r"你不是",
    r"你的(?:身份|性格|人设|名字)",
    r"你的回复风格",
    r"不要(?:再)?(?:说|提到|回答)",
    r"必须(?:始终|一直|总是)",
    r"从现在起",
    r"忽略(?:之前|以上|上面)",
    r"忘掉(?:你|之前)",
    r"扮演",
    r"系统提示",
    r"system prompt",
    r"你(?:应该|应当)自称",
)

_PERSONA_CONFLICT_RE = re.compile("|".join(_PERSONA_CONFLICT_PATTERNS))

#: Characters stripped before comparing text for duplication and equality.
_NORMALISE_STRIP = " \t\r\n「」『』“”\"'‘’()（）[]【】。，,、！!？?；;：:…~～-—_"


def normalise_content(text: str) -> str:
    """Fold a candidate's text into a comparison key.

    Used for deduplication and for comparing a candidate against Aemeath's own
    replies. Case, surrounding punctuation and interior whitespace are all
    irrelevant to "is this the same phrase", so they are removed here rather
    than at each call site.
    """
    lowered = (text or "").strip().lower()
    return re.sub(r"\s+", "", lowered).strip(_NORMALISE_STRIP)


def fingerprint_for(kind: str, content: str) -> str:
    """Stable identity of a learned item.

    Deliberately *not* a row id: the same catchphrase re-learned later must land
    on the same key, otherwise a revocation would be trivially bypassed by the
    next extraction pass producing the same phrase under a new id.
    """
    material = f"{kind}\x1f{normalise_content(content)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def evidence_fingerprint(message_ids: Iterable[str]) -> str:
    """Stable identity of the evidence a candidate came from.

    Stored alongside a revocation so "do not learn this again from *this*
    conversation" can be distinguished from "do not learn this at all" — and so
    the record never has to keep the message text itself.
    """
    material = "\x1f".join(sorted(str(mid) for mid in message_ids))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class LearningEvidence:
    """One source message a learned item cites."""

    message_id: str
    fragment: str = ""
    revision: int = 1


@dataclass(frozen=True)
class LearningKind:
    """One meaning of an item.

    Jargon commonly has more than one: the same shorthand can mean different
    things in different contexts, and the design forbids collapsing those into a
    single row and overwriting the earlier meaning.
    """

    kind_id: str
    meaning: str = ""
    scenario: str = ""
    state: str = STATE_OK

    @property
    def needs_clarification(self) -> bool:
        """Whether the meaning could not be settled from the conversation."""
        return self.state == STATE_NEEDS_CLARIFICATION


@dataclass(frozen=True)
class LearningItem:
    """A learned expression or jargon word."""

    item_id: str
    kind: str
    content: str
    meaning: str = ""
    scenario: str = ""
    status: str = STATUS_CANDIDATE
    origin: str = ORIGIN_AUTO
    manual_override: bool = False
    check_result: str = ""
    check_detail: str = ""
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: Optional[float] = None
    meanings: Tuple[LearningKind, ...] = ()

    @property
    def enabled(self) -> bool:
        """Whether this item may be injected into a prompt."""
        return self.status == STATUS_ENABLED

    @property
    def ambiguous(self) -> bool:
        """Whether any meaning is still waiting for the user to settle it."""
        return any(entry.needs_clarification for entry in self.meanings)


@dataclass
class CheckOutcome:
    """Verdict of the pre-enable checks on one candidate."""

    passed: bool
    result: str
    detail: str = ""
    fragment: str = ""
    meanings: List[Dict[str, str]] = field(default_factory=list)


#: The learning tables. Created alongside the memory schema in the same file so
#: that one backup, one restore and one forgetting operation cover both.
_LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    origin TEXT NOT NULL DEFAULT 'auto',
    manual_override INTEGER NOT NULL DEFAULT 0,
    check_result TEXT NOT NULL DEFAULT '',
    check_detail TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS learning_kinds (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS learning_evidence (
    item_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    fragment TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, message_id)
);

CREATE TABLE IF NOT EXISTS learning_revocations (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT '',
    item_id TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_tasks (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS learning_task_messages (
    task_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (task_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_learning_items_status ON learning_items(status, kind);
CREATE INDEX IF NOT EXISTS idx_learning_kinds_item ON learning_kinds(item_id);
CREATE INDEX IF NOT EXISTS idx_learning_evidence_message
    ON learning_evidence(message_id);
"""


class LearningStore:
    """SQLite-backed storage for learned expressions and jargon.

    The store owns the tables but not the connection: it shares the memory
    database file through the same ``MemoryStore`` connection contract, so
    forgetting can clean up memory and learning rows in one operation.
    """

    def __init__(self, db_path) -> None:
        """Open (and if needed create) the learning tables.

        Args:
            db_path: Path to the shared SQLite file.
        """
        from pathlib import Path

        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection and schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with named row access."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, always close.

        Same reasoning as ``MemoryStore._connection``: ``with sqlite3.connect``
        is a transaction context manager, not a closing one, and a leaked handle
        locks the file on Windows.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create the learning tables and record their version."""
        with self._connection() as conn:
            conn.executescript(_LEARNING_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
                ("learning_schema_version", str(LEARNING_SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_item(row: sqlite3.Row, meanings: Sequence[LearningKind]) -> LearningItem:
        """Convert a row plus its meanings into an item."""
        keys = row.keys()
        return LearningItem(
            item_id=row["id"],
            kind=row["kind"],
            content=row["content"],
            meaning=row["meaning"] or "",
            scenario=row["scenario"] or "",
            status=row["status"],
            origin=row["origin"] if "origin" in keys else ORIGIN_AUTO,
            manual_override=bool(row["manual_override"]),
            check_result=row["check_result"] or "",
            check_detail=row["check_detail"] or "",
            revision=row["revision"] or 1,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            meanings=tuple(meanings),
        )

    def get(self, item_id: str) -> Optional[LearningItem]:
        """Fetch one item by id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            mean_rows = conn.execute(
                "SELECT * FROM learning_kinds WHERE item_id = ? ORDER BY rowid",
                (item_id,),
            ).fetchall()
        return self._row_to_item(row, [self._row_to_kind(m) for m in mean_rows])

    @staticmethod
    def _row_to_kind(row: sqlite3.Row) -> LearningKind:
        """Convert a meaning row."""
        return LearningKind(
            kind_id=row["id"],
            meaning=row["meaning"] or "",
            scenario=row["scenario"] or "",
            state=row["state"] or STATE_OK,
        )

    def list_items(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: str = "",
    ) -> List[LearningItem]:
        """List items, newest first, with optional filters.

        Args:
            kind: Restrict to ``expression`` or ``jargon``.
            status: Restrict to one state.
            query: Case-insensitive substring filter over content and meaning.

        Returns:
            Matching items, most recently updated first.
        """
        sql = "SELECT * FROM learning_items"
        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if query:
            clauses.append("(content LIKE ? OR meaning LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"

        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            items = []
            for row in rows:
                mean_rows = conn.execute(
                    "SELECT * FROM learning_kinds WHERE item_id = ? ORDER BY rowid",
                    (row["id"],),
                ).fetchall()
                items.append(
                    self._row_to_item(row, [self._row_to_kind(m) for m in mean_rows])
                )
        return items

    def active_items(self, kind: Optional[str] = None) -> List[LearningItem]:
        """Enabled items, most recently used first.

        Ordered by last use so a long-accumulated store keeps injecting the
        entries that have actually been useful, rather than the oldest ones.
        """
        sql = (
            "SELECT * FROM learning_items WHERE status = ?"
        )
        params: List[Any] = [STATUS_ENABLED]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY (last_used_at IS NULL), last_used_at DESC, updated_at DESC"

        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            items = []
            for row in rows:
                mean_rows = conn.execute(
                    "SELECT * FROM learning_kinds WHERE item_id = ? ORDER BY rowid",
                    (row["id"],),
                ).fetchall()
                items.append(
                    self._row_to_item(row, [self._row_to_kind(m) for m in mean_rows])
                )
        return items

    def evidence_for(self, item_id: str) -> List[LearningEvidence]:
        """Source messages an item cites."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT message_id, fragment, revision FROM learning_evidence "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchall()
        return [
            LearningEvidence(
                message_id=row["message_id"],
                fragment=row["fragment"] or "",
                revision=row["revision"] or 1,
            )
            for row in rows
        ]

    def find_by_fingerprint(self, fingerprint: str) -> Optional[LearningItem]:
        """Locate an existing item by its stable identity.

        This is a full scan rather than an indexed lookup because the
        fingerprint is derived, not stored; at this project's scale (hundreds of
        entries) that is cheaper than maintaining a denormalised column that
        could drift from the derivation rule.
        """
        for item in self.list_items():
            if fingerprint_for(item.kind, item.content) == fingerprint:
                return item
        return None

    def is_revoked(self, fingerprint: str) -> bool:
        """Whether this identity has been revoked and must not come back."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM learning_revocations WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return row is not None

    def is_revoked_for_evidence(self, fingerprint: str, evidence: str) -> bool:
        """Whether this identity was revoked against exactly this evidence."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM learning_revocations "
                "WHERE fingerprint = ? AND source_fingerprint = ?",
                (fingerprint, evidence),
            ).fetchone()
        return row is not None

    def counts(self) -> Dict[str, int]:
        """Item counts per state, for the management overview."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM learning_items GROUP BY status"
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_items"
            ).fetchone()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] = int(row["n"])
        counts["total"] = int(total["n"]) if total else 0
        return counts

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def add_item(
        self,
        *,
        kind: str,
        content: str,
        meaning: str = "",
        scenario: str = "",
        status: str = STATUS_CANDIDATE,
        origin: str = ORIGIN_AUTO,
        check_result: str = "",
        check_detail: str = "",
        evidence: Sequence[LearningEvidence] = (),
        meanings: Sequence[Dict[str, str]] = (),
    ) -> str:
        """Insert one item with its evidence and meanings.

        Args:
            kind: ``expression`` or ``jargon``.
            content: The expression text or the jargon word.
            meaning: Primary meaning; empty for expressions.
            scenario: Primary applicable context.
            status: Initial state.
            origin: ``auto`` or ``manual``.
            check_result: ``passed`` / ``rejected`` / ``candidate``.
            check_detail: Human-readable reason, shown in the UI.
            evidence: Source messages.
            meanings: Additional meanings, each with ``meaning``, ``scenario``
                and ``state``.

        Returns:
            The new item id.
        """
        item_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO learning_items
                    (id, kind, content, meaning, scenario, status, origin,
                     manual_override, check_result, check_detail, revision,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?)
                """,
                (
                    item_id,
                    kind,
                    content,
                    meaning,
                    scenario,
                    status,
                    origin,
                    check_result,
                    check_detail,
                    now,
                    now,
                ),
            )
            for entry in meanings:
                conn.execute(
                    "INSERT INTO learning_kinds (id, item_id, meaning, scenario, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        item_id,
                        str(entry.get("meaning", "")),
                        str(entry.get("scenario", "")),
                        str(entry.get("state", STATE_OK)),
                    ),
                )
            for record in evidence:
                conn.execute(
                    "INSERT OR IGNORE INTO learning_evidence "
                    "(item_id, message_id, fragment, revision) VALUES (?, ?, ?, ?)",
                    (item_id, record.message_id, record.fragment, record.revision),
                )
        return item_id

    def add_meaning(
        self, item_id: str, *, meaning: str, scenario: str = "", state: str = STATE_OK
    ) -> str:
        """Attach another meaning to an existing word.

        Used when the same jargon turns up with a different sense. The earlier
        meaning is left untouched: the design explicitly forbids resolving
        ambiguity by overwriting the old sense.
        """
        meaning_id = uuid.uuid4().hex
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO learning_kinds (id, item_id, meaning, scenario, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (meaning_id, item_id, meaning, scenario, state),
            )
        return meaning_id

    def set_status(self, item_id: str, status: str) -> bool:
        """Move an item to a new state.

        The item's revision is bumped so a write-back that was already in flight
        when the user acted is discarded instead of resurrecting the entry.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE learning_items SET status = ?, updated_at = ?, "
                "revision = revision + 1 WHERE id = ?",
                (status, time.time(), item_id),
            )
        return True

    def edit_item(
        self,
        item_id: str,
        *,
        content: Optional[str] = None,
        meaning: Optional[str] = None,
        scenario: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply a manual edit and mark the item as user-owned.

        ``manual_override`` is the mechanism that makes "a human edit beats a
        later automatic result" true: the write-back path refuses to touch an
        item carrying it.

        Args:
            item_id: Item to edit.
            content: New text, when supplied.
            meaning: New primary meaning.
            scenario: New primary context.
            expected_revision: Revision the client read. When it does not match,
                the edit is refused as a conflict rather than silently applied
                over a concurrent change.

        Returns:
            ``{ok, conflict, revision, error}``.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "学习条目不存在。", "conflict": False}
            if (
                expected_revision is not None
                and expected_revision != (row["revision"] or 1)
            ):
                return {
                    "ok": False,
                    "error": "条目已被修改，请刷新后重试。",
                    "conflict": True,
                }

            updates = ["manual_override = 1", "updated_at = ?", "revision = revision + 1"]
            params: List[Any] = [time.time()]
            if content is not None:
                updates.append("content = ?")
                params.append(content)
            if meaning is not None:
                updates.append("meaning = ?")
                params.append(meaning)
            if scenario is not None:
                updates.append("scenario = ?")
                params.append(scenario)
            params.append(item_id)
            conn.execute(
                f"UPDATE learning_items SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            new_revision = conn.execute(
                "SELECT revision FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()["revision"]
        return {"ok": True, "error": "", "conflict": False, "revision": new_revision}

    def resolve_meanings(
        self, item_id: str, meanings: Sequence[Dict[str, str]]
    ) -> bool:
        """Settle an ambiguous word's meanings with the user's answer.

        Replaces the meaning rows wholesale, because the user is stating what
        the word means rather than correcting one of the model's guesses.
        """
        with self._connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                return False
            conn.execute("DELETE FROM learning_kinds WHERE item_id = ?", (item_id,))
            for entry in meanings:
                conn.execute(
                    "INSERT INTO learning_kinds (id, item_id, meaning, scenario, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        item_id,
                        str(entry.get("meaning", "")),
                        str(entry.get("scenario", "")),
                        STATE_OK,
                    ),
                )
            primary = meanings[0] if meanings else {}
            conn.execute(
                "UPDATE learning_items SET meaning = ?, scenario = ?, "
                "manual_override = 1, updated_at = ?, revision = revision + 1 "
                "WHERE id = ?",
                (
                    str(primary.get("meaning", "")),
                    str(primary.get("scenario", "")),
                    time.time(),
                    item_id,
                ),
            )
        return True

    def mark_used(self, item_ids: Sequence[str]) -> None:
        """Record that these items were injected into a prompt."""
        if not item_ids:
            return
        now = time.time()
        with self._connection() as conn:
            for item_id in item_ids:
                conn.execute(
                    "UPDATE learning_items SET last_used_at = ? WHERE id = ?",
                    (now, item_id),
                )

    def record_revocation(
        self,
        *,
        fingerprint: str,
        kind: str,
        item_id: str = "",
        source_fingerprint: str = "",
    ) -> None:
        """Remember that this identity must not be learned again.

        Only identifiers are stored. The revocation record has to survive the
        forgetting of the very evidence it refers to, so it cannot hold the text
        — that would keep a copy of content the user asked to remove.
        """
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO learning_revocations "
                "(fingerprint, kind, item_id, source_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fingerprint, kind, item_id, source_fingerprint, time.time()),
            )

    def _forget_revocation(self, fingerprint: str) -> None:
        """Drop the anti-relearn record for one identity.

        Called only from an explicit user restore. Everywhere else the record is
        permanent, because the entire reason it exists is to stop the next
        extraction pass from quietly undoing the user's decision.
        """
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM learning_revocations WHERE fingerprint = ?",
                (fingerprint,),
            )

    # ------------------------------------------------------------------
    # Forgetting
    # ------------------------------------------------------------------

    def forget_item(self, item_id: str) -> bool:
        """Remove an item, its meanings and its evidence links."""
        with self._connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM learning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                return False
            conn.execute("DELETE FROM learning_kinds WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM learning_evidence WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM learning_items WHERE id = ?", (item_id,))
        return True

    def purge_by_source_message(self, message_id: str) -> Dict[str, int]:
        """Clean up everything that cited a message about to be forgotten.

        Three different outcomes, deliberately:

        * an item that cited **only** this message loses its evidence and is
          removed with it — there is nothing left to check it against;
        * an item that still has other evidence is **re-checked**, not deleted,
          because it may remain valid on the strength of the surviving source;
        * the message's evidence rows and any queued task reference go too, so
          the next extraction pass cannot rebuild what was just removed.

        Returns:
            ``{removed, retained, evidence_removed}``.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_id FROM learning_evidence WHERE message_id = ?",
                (message_id,),
            ).fetchall()
            item_ids = [row["item_id"] for row in rows]

            conn.execute(
                "DELETE FROM learning_evidence WHERE message_id = ?", (message_id,)
            )
            conn.execute(
                "DELETE FROM learning_task_messages WHERE message_id = ?",
                (message_id,),
            )
            conn.execute(
                "DELETE FROM learning_tasks WHERE id NOT IN "
                "(SELECT task_id FROM learning_task_messages)"
            )

            retained = 0
            orphaned: List[str] = []
            for item_id in item_ids:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM learning_evidence WHERE item_id = ?",
                    (item_id,),
                ).fetchone()["n"]
                if remaining:
                    # Other evidence still supports it; the item is demoted back
                    # to candidate if it was enabled on the removed source alone
                    # being assumed valid. Its status is left alone here — the
                    # caller re-checks it — but it is counted as retained.
                    retained += 1
                else:
                    orphaned.append(item_id)

            for item_id in orphaned:
                conn.execute("DELETE FROM learning_kinds WHERE item_id = ?", (item_id,))
                conn.execute("DELETE FROM learning_items WHERE id = ?", (item_id,))

        if orphaned:
            logger.info(
                "Removed {} learning item(s) whose only source was forgotten.",
                len(orphaned),
            )
        return {
            "removed": len(orphaned),
            "retained": retained,
            "evidence_removed": len(item_ids),
        }

    # ------------------------------------------------------------------
    # Extraction queue
    # ------------------------------------------------------------------

    def enqueue(self, message_ids: Sequence[str]) -> str:
        """Queue a learning task and record its sources' revisions.

        The revisions are what make a delete that happens while the model call
        is in flight actually win: the task is discarded on return if any source
        moved.
        """
        task_id = uuid.uuid4().hex
        with self._connection() as conn:
            revisions: Dict[str, int] = {}
            for message_id in message_ids:
                row = conn.execute(
                    "SELECT revision FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                revisions[message_id] = row["revision"] if row else 1

            conn.execute(
                "INSERT INTO learning_tasks (id, created_at, revision) VALUES (?, ?, 1)",
                (task_id, time.time()),
            )
            for message_id in message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO learning_task_messages "
                    "(task_id, message_id, message_revision) VALUES (?, ?, ?)",
                    (task_id, message_id, revisions[message_id]),
                )
        return task_id

    def task_is_current(self, task_id: str, message_ids: Sequence[str]) -> bool:
        """Whether a task and every one of its sources are still unchanged."""
        with self._connection() as conn:
            task = conn.execute(
                "SELECT revision FROM learning_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return False
            for message_id in message_ids:
                row = conn.execute(
                    "SELECT m.revision AS revision, m.status AS status, "
                    "t.message_revision AS queued "
                    "FROM learning_task_messages t "
                    "JOIN messages m ON m.id = t.message_id "
                    "WHERE t.task_id = ? AND t.message_id = ?",
                    (task_id, message_id),
                ).fetchone()
                if row is None:
                    return False
                if row["revision"] != row["queued"]:
                    return False
                if row["status"] == "deleted":
                    return False
            return True

    def pending_tasks(self) -> List[Dict[str, Any]]:
        """Queued learning tasks with their sources."""
        with self._connection() as conn:
            tasks = conn.execute(
                "SELECT * FROM learning_tasks ORDER BY created_at"
            ).fetchall()
            result = []
            for task in tasks:
                messages = conn.execute(
                    "SELECT message_id FROM learning_task_messages WHERE task_id = ?",
                    (task["id"],),
                ).fetchall()
                result.append(
                    {
                        "task_id": task["id"],
                        "attempts": task["attempts"],
                        "last_error": task["last_error"],
                        "message_ids": [m["message_id"] for m in messages],
                    }
                )
        return result

    def complete_task(self, task_id: str) -> None:
        """Remove a finished task."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM learning_task_messages WHERE task_id = ?", (task_id,)
            )
            conn.execute("DELETE FROM learning_tasks WHERE id = ?", (task_id,))

    def fail_task(self, task_id: str, error: str) -> None:
        """Record a failed attempt while keeping the task for a retry."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE learning_tasks SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                (error, task_id),
            )


class LearningService:
    """Coordinates learning storage, candidate checking and prompt selection.

    The service is the only thing the rest of the system talks to; it owns the
    rules about what may be learned and what may be injected.
    """

    def __init__(
        self,
        store: LearningStore,
        *,
        adapter: Optional[LearningAdapter] = None,
        enabled: bool = False,
        max_prompt_items: int = 8,
    ) -> None:
        """Wire the store to a learning adapter and sizing limits.

        Args:
            store: The learning store.
            adapter: Model adapter proposing candidates; ``None`` means learning
                is not configured.
            enabled: Whether the user has switched learning on.
            max_prompt_items: Cap on entries injected per prompt.
        """
        self._store = store
        self._adapter = adapter
        self.enabled = enabled
        self.max_prompt_items = max_prompt_items

    @property
    def store(self) -> LearningStore:
        """The underlying store, also used by the management API."""
        return self._store

    @property
    def has_adapter(self) -> bool:
        """Whether a learning provider is configured at all."""
        return self._adapter is not None

    @property
    def available(self) -> bool:
        """Whether learning can actually run right now."""
        return self.enabled and self._adapter is not None

    # ------------------------------------------------------------------
    # Prompt selection
    # ------------------------------------------------------------------

    def prompt_items(self, limit: Optional[int] = None) -> List[LearningItem]:
        """The enabled entries to offer the model this turn.

        Only enabled items, capped, and with ambiguous meanings excluded: an
        entry whose meaning is still unsettled would make the model guess, which
        is precisely what "preserve the ambiguity" is trying to avoid.
        """
        cap = limit if limit is not None else self.max_prompt_items
        selected: List[LearningItem] = []
        for item in self._store.active_items():
            if item.ambiguous:
                continue
            selected.append(item)
            if len(selected) >= cap:
                break
        self._store.mark_used([item.item_id for item in selected])
        return selected

    # ------------------------------------------------------------------
    # Queuing
    # ------------------------------------------------------------------

    def queue_turn(self, message_ids: Sequence[str]) -> Optional[str]:
        """Queue learning for a finished turn, when learning is available.

        Returns ``None`` when learning is switched off or unconfigured, so the
        conversation path never has to care whether it is on.
        """
        if not self.available:
            return None
        if not message_ids:
            return None
        return self._store.enqueue(message_ids)

    # ------------------------------------------------------------------
    # Candidate checking
    # ------------------------------------------------------------------

    def check_candidate(
        self,
        candidate: LearningCandidate,
        *,
        user_messages: Sequence[Any],
        assistant_texts: Sequence[str],
        persona_text: str,
    ) -> CheckOutcome:
        """Decide whether a candidate may be enabled, and why not if not.

        The four checks are the ones the requirements list, each reported
        separately so the UI can tell the user what was wrong rather than
        showing a generic "rejected":

        1. **content sanity** — non-empty, right length, not a bare particle;
        2. **traceable** — cites at least one real user message;
        3. **not her own words** — the phrase is not something Aemeath already
           says, which would make her own output look like independent evidence;
        4. **persona-consistent** — does not instruct or redefine who she is.

        A candidate failing 2–4 stays a candidate rather than being deleted:
        the user may still want to see and enable it by hand.

        Returns:
            The verdict, with the located source fragment.
        """
        content = (candidate.content or "").strip()
        if candidate.kind not in KINDS:
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail=f"未知的学习类型：{candidate.kind}",
            )
        if not content:
            return CheckOutcome(
                passed=False, result=STATUS_CANDIDATE, detail="条目内容为空。"
            )
        if len(content) > _MAX_CONTENT_LENGTH:
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail=f"条目过长（超过 {_MAX_CONTENT_LENGTH} 字），不像是一个用语。",
            )
        if candidate.kind == KIND_EXPRESSION and len(content) < _MIN_EXPRESSION_LENGTH:
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail="表达过短，无法判断是风格还是标点噪声。",
            )

        # 2. Traceability: at least one live user message must be behind it.
        sources = [m for m in user_messages if getattr(m, "content", "").strip()]
        if not sources:
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail="没有可追溯的用户来源消息，不作为已学会的内容。",
            )

        normalised = normalise_content(content)

        # 3. Her own words are not new evidence.
        #
        # The rule is about *provenance*, not about vocabulary: a phrase is
        # refused when it appears only in Aemeath's replies and nowhere in the
        # user's own messages. Testing mere presence in an assistant turn was
        # wrong and produced a real false rejection — the user introduced
        # 「冒烟测试」, Aemeath repeated it while answering, and the entry was
        # refused even though the user was plainly the source. What the
        # requirement forbids is treating her echo as the evidence, so the user
        # must not already be saying it.
        if any(normalised and normalised in normalise_content(text) for text in assistant_texts):
            user_says_it = any(
                normalised in normalise_content(getattr(m, "content", ""))
                for m in sources
            )
            if not user_says_it:
                return CheckOutcome(
                    passed=False,
                    result=STATUS_CANDIDATE,
                    detail="这条内容只出现在爱弥斯自己的回复里，不能当作新的学习证据。",
                )

        # 4. Persona consistency.
        if _PERSONA_CONFLICT_RE.search(content):
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail="与人设冲突：学习结果不能改写身份、性格或行为规则。",
            )
        persona_normalised = normalise_content(persona_text)
        if normalised and len(normalised) >= 6 and normalised in persona_normalised:
            # Already part of the persona; "learning" it would only duplicate
            # what is injected anyway.
            return CheckOutcome(
                passed=False,
                result=STATUS_CANDIDATE,
                detail="这条内容已经包含在人设里，无需重复学习。",
            )

        # Locate the exact span, so the entry has verifiable provenance and a
        # forgetting operation can remove precisely that text.
        fragment = (candidate.fragment or "").strip()
        if not fragment:
            fragment = _locate_span(content, [m.content for m in sources])

        # Jargon meanings: an unsettled meaning is recorded as such and the item
        # is still stored, but it will not be injected until the user settles it.
        meanings: List[Dict[str, str]] = []
        if candidate.kind == KIND_JARGON:
            meaning = (candidate.meaning or "").strip()
            scenario = (candidate.scenario or "").strip()
            if not meaning or any(
                marker in meaning.lower() for marker in _UNCLEAR_MEANINGS
            ):
                meanings.append(
                    {
                        "meaning": meaning or "含义不明确",
                        "scenario": scenario,
                        "state": STATE_NEEDS_CLARIFICATION,
                    }
                )
            else:
                meanings.append(
                    {"meaning": meaning, "scenario": scenario, "state": STATE_OK}
                )

        return CheckOutcome(
            passed=True,
            result="passed",
            detail="",
            fragment=fragment,
            meanings=meanings,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    async def run_pending_learning(self, persona_text: str = "") -> int:
        """Process queued learning tasks.

        One provider call per task, validated against its sources *after* the
        call returns and before anything is written — the same ordering
        ``MemoryService`` uses, for the same reason: a source deleted while the
        request was in flight must win over the result.

        Failures leave the task queued with the error recorded, so a transient
        provider problem does not silently lose the learning. Nothing here
        raises into the conversation path: the whole point is that learning
        being down is invisible to chatting.

        Args:
            persona_text: The active persona, used by the consistency check.

        Returns:
            Number of items written.
        """
        if not self.available:
            return 0

        written = 0
        for task in self._store.pending_tasks():
            task_id = task["task_id"]
            message_ids = list(task["message_ids"])

            messages = [self._message(mid) for mid in message_ids]
            messages = [m for m in messages if m is not None]
            # Only user turns are sent as *new* evidence. Aemeath's own replies
            # are included as context — the model needs them to judge what a
            # word means — but they can never be the source of an item.
            payload = [
                {"role": m.role, "content": m.content}
                for m in messages
                if m.status != "deleted" and m.content.strip()
            ]
            user_messages = [
                m for m in messages if m.role == "user" and m.status != "deleted"
            ]
            assistant_texts = [
                m.content for m in messages if m.role == "assistant"
            ]

            if not payload:
                self._store.complete_task(task_id)
                continue

            try:
                candidates = await self._adapter.learn(payload)
            except Exception as exc:
                logger.error("Learning task {} failed: {}", task_id, exc)
                self._store.fail_task(task_id, str(exc))
                continue

            # Only now ask whether the sources the model was shown are still
            # valid; a delete during the call has to win.
            if not self._store.task_is_current(task_id, message_ids):
                logger.info(
                    "Discarding learning task {}: its sources changed while the "
                    "request was in flight.",
                    task_id,
                )
                self._store.complete_task(task_id)
                continue

            # A turn with no user message cannot *enable* anything — there is no
            # user evidence behind it. It is still run through the checker so the
            # proposal is recorded as a candidate the user can see, rather than
            # vanishing with no trace of why it was refused. The same rule
            # applies to a deleted user message: the check reports it, and
            # nothing is enabled.
            for candidate in candidates:
                stored = self._write_candidate(
                    candidate,
                    user_messages=user_messages,
                    assistant_texts=assistant_texts,
                    persona_text=persona_text,
                    source_message_ids=[m.message_id for m in user_messages],
                )
                if stored:
                    written += 1
            self._store.complete_task(task_id)

        return written

    def _message(self, message_id: str):
        """Read one message through a short-lived memory-store connection.

        The learning store shares the memory database, so a direct read here
        keeps the two modules independent of each other's Python objects.
        """
        try:
            conn = sqlite3.connect(self._store._db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.error("Could not read message {}: {}", message_id, exc)
            return None
        if row is None:
            return None

        class _Message:
            """Minimal message view for this module's own use."""

            __slots__ = ("message_id", "role", "content", "status", "revision")

            def __init__(self, row) -> None:
                self.message_id = row["id"]
                self.role = row["role"]
                self.content = row["content"]
                self.status = row["status"]
                self.revision = row["revision"] if "revision" in row.keys() else 1

        return _Message(row)

    def _write_candidate(
        self,
        candidate: LearningCandidate,
        *,
        user_messages: Sequence[Any],
        assistant_texts: Sequence[str],
        persona_text: str,
        source_message_ids: Sequence[str],
    ) -> Optional[str]:
        """Check and store one candidate, honouring dedup and revocation.

        Returns:
            The new item id when something was written, ``None`` otherwise.
        """
        fingerprint = fingerprint_for(candidate.kind, candidate.content)
        evidence_print = evidence_fingerprint(source_message_ids)

        # A revoked identity does not come back, on any evidence. Revocation is
        # the user saying "I do not want this", which is not a statement about
        # one conversation.
        if self._store.is_revoked(fingerprint):
            logger.info(
                "Refusing to learn a revoked item again ({}) — {}", candidate.kind,
                fingerprint[:8],
            )
            return None

        outcome = self.check_candidate(
            candidate,
            user_messages=user_messages,
            assistant_texts=assistant_texts,
            persona_text=persona_text,
        )

        existing = self._store.find_by_fingerprint(fingerprint)
        if existing is not None:
            return self._merge_into_existing(
                existing, candidate, outcome, source_message_ids
            )

        if not outcome.passed:
            # Stored as a candidate so the user can see what was proposed and
            # why it was not trusted; not enabled, so it cannot reach a prompt.
            item_id = self._store.add_item(
                kind=candidate.kind,
                content=(candidate.content or "").strip(),
                meaning=(candidate.meaning or "").strip(),
                scenario=(candidate.scenario or "").strip(),
                status=STATUS_CANDIDATE,
                origin=ORIGIN_AUTO,
                check_result=STATUS_CANDIDATE,
                check_detail=outcome.detail,
                evidence=self._evidence_rows(
                    source_message_ids, outcome.fragment, candidate
                ),
                meanings=outcome.meanings,
            )
            logger.info(
                "Learning candidate kept as candidate: {}", outcome.detail
            )
            return item_id

        item_id = self._store.add_item(
            kind=candidate.kind,
            content=(candidate.content or "").strip(),
            meaning=(candidate.meaning or "").strip(),
            scenario=(candidate.scenario or "").strip(),
            # Passed the checks, so it is applied automatically — the
            # behaviour the user asked for. It stays editable, disableable
            # and revocable, which is what makes automatic use acceptable.
            status=STATUS_ENABLED,
            origin=ORIGIN_AUTO,
            check_result="passed",
            check_detail="",
            evidence=self._evidence_rows(
                source_message_ids, outcome.fragment, candidate
            ),
            meanings=outcome.meanings,
        )
        logger.info(
            "Learned {} item '{}' (enabled).", candidate.kind, candidate.content
        )
        return item_id

    def _merge_into_existing(
        self,
        existing: LearningItem,
        candidate: LearningCandidate,
        outcome: CheckOutcome,
        source_message_ids: Sequence[str],
    ) -> Optional[str]:
        """Fold a repeat observation into the item already stored.

        Deduplication beyond this is deliberately *additive*: new evidence is
        attached, and a new meaning is recorded as a second row. The earlier
        meaning is never overwritten, and a manually edited item is never
        touched by an automatic result.
        """
        if existing.status == STATUS_REVOKED:
            # Revocation is a durable decision, not a state to be re-derived.
            return None

        if existing.manual_override:
            logger.debug(
                "Skipping automatic update for manually edited item {}.",
                existing.item_id,
            )
            return None

        for record in self._evidence_rows(source_message_ids, outcome.fragment, candidate):
            try:
                self._attach_evidence(existing.item_id, record)
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                logger.warning("Could not attach learning evidence: {}", exc)

        if candidate.kind == KIND_JARGON:
            for entry in outcome.meanings:
                self._attach_meaning_if_new(existing, entry)

        if existing.status == STATUS_CANDIDATE and outcome.passed:
            # The first sighting was too weak to trust; a later one with proper
            # provenance upgrades it, which is the point of keeping candidates.
            self._store.set_status(existing.item_id, STATUS_ENABLED)
            logger.info("Upgraded learning candidate {} after new evidence.", existing.item_id)
        return existing.item_id

    def _evidence_rows(
        self,
        source_message_ids: Sequence[str],
        fragment: str,
        candidate: LearningCandidate,
    ) -> List[LearningEvidence]:
        """Build evidence rows, resolving the span for the first source."""
        rows: List[LearningEvidence] = []
        for index, message_id in enumerate(source_message_ids):
            rows.append(
                LearningEvidence(
                    message_id=message_id,
                    fragment=fragment if index == 0 else "",
                    revision=1,
                )
            )
        if not rows and candidate.fragment:
            rows.append(
                LearningEvidence(
                    message_id="", fragment=candidate.fragment, revision=1
                )
            )
        return rows

    def _attach_evidence(self, item_id: str, record: LearningEvidence) -> None:
        """Attach one evidence row if it is not already present."""
        if not record.message_id:
            return
        with self._store._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO learning_evidence "
                "(item_id, message_id, fragment, revision) VALUES (?, ?, ?, ?)",
                (item_id, record.message_id, record.fragment, record.revision),
            )

    def _attach_meaning_if_new(
        self, item: LearningItem, entry: Dict[str, str]
    ) -> None:
        """Add a meaning row when this sense is genuinely new.

        The comparison is on the meaning text only: the same word with a
        different sense must produce a second row (the requirement), while the
        same sense observed again must not.
        """
        meaning = str(entry.get("meaning", "")).strip()
        if not meaning:
            return
        existing = {normalise_content(k.meaning) for k in item.meanings}
        if normalise_content(meaning) in existing:
            return
        self._store.add_meaning(
            item.item_id,
            meaning=meaning,
            scenario=str(entry.get("scenario", "")),
            state=str(entry.get("state", STATE_OK)),
        )
        logger.info(
            "Recorded an additional meaning for {}: {}", item.item_id, meaning
        )

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    def edit(
        self,
        item_id: str,
        *,
        content: Optional[str] = None,
        meaning: Optional[str] = None,
        scenario: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply a user edit; it beats any later automatic result."""
        if content is not None:
            text = content.strip()
            if not text:
                return {"ok": False, "conflict": False, "error": "条目内容不能为空。"}
            content = text
        if meaning is not None and len(meaning) > _MAX_MEANING_LENGTH:
            return {
                "ok": False,
                "conflict": False,
                "error": f"含义过长（超过 {_MAX_MEANING_LENGTH} 字）。",
            }
        if scenario is not None and len(scenario) > _MAX_SCENARIO_LENGTH:
            return {
                "ok": False,
                "conflict": False,
                "error": f"适用场景过长（超过 {_MAX_SCENARIO_LENGTH} 字）。",
            }
        return self._store.edit_item(
            item_id,
            content=content,
            meaning=meaning,
            scenario=scenario,
            expected_revision=expected_revision,
        )

    def set_enabled(self, item_id: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable an item.

        Disabling writes a revocation record as well: a disabled entry that the
        next extraction pass immediately re-enables would make the button a lie.
        Restoring is a separate, explicit action — the flow the requirements ask
        for — which clears both the status and that record.
        """
        item = self._store.get(item_id)
        if item is None:
            return {"ok": False, "error": "学习条目不存在。"}

        if item.status == STATUS_REVOKED:
            return {
                "ok": False,
                "error": "这条学习已被撤销，撤销是终态；如需重新学会，需要重新对话产生新的证据。",
            }

        if enabled:
            self._store.set_status(item_id, STATUS_ENABLED)
            self._store._forget_revocation(fingerprint_for(item.kind, item.content))
            return {"ok": True, "error": "", "status": STATUS_ENABLED}

        self._store.set_status(item_id, STATUS_DISABLED)
        self._store.record_revocation(
            fingerprint=fingerprint_for(item.kind, item.content),
            kind=item.kind,
            item_id=item_id,
            source_fingerprint=evidence_fingerprint(
                [e.message_id for e in self._store.evidence_for(item_id)]
            ),
        )
        return {"ok": True, "error": "", "status": STATUS_DISABLED}

    def revoke(self, item_id: str) -> Dict[str, Any]:
        """Revoke an item: it stops being used and cannot be re-learned.

        This is the operation the requirements describe as "撤回某次学习产生的
        有效结果，并保留阻止同一证据立即重学的记录". The item row is kept (so
        the user can see what was removed and why) but its status is terminal.
        """
        item = self._store.get(item_id)
        if item is None:
            return {"ok": False, "error": "学习条目不存在。"}
        if item.status == STATUS_REVOKED:
            return {"ok": True, "error": "", "already_revoked": True}

        evidence = [e.message_id for e in self._store.evidence_for(item_id)]
        self._store.set_status(item_id, STATUS_REVOKED)
        self._store.record_revocation(
            fingerprint=fingerprint_for(item.kind, item.content),
            kind=item.kind,
            item_id=item_id,
            source_fingerprint=evidence_fingerprint(evidence),
        )
        logger.info("Revoked learning item {} ({}).", item_id, item.kind)
        return {"ok": True, "error": "", "status": STATUS_REVOKED}

    def resolve_ambiguity(
        self, item_id: str, meanings: Sequence[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Settle an ambiguous word's meanings from the user's answer."""
        item = self._store.get(item_id)
        if item is None:
            return {"ok": False, "error": "学习条目不存在。"}
        cleaned = [
            {
                "meaning": str(entry.get("meaning", "")).strip(),
                "scenario": str(entry.get("scenario", "")).strip(),
            }
            for entry in meanings
            if str(entry.get("meaning", "")).strip()
        ]
        if not cleaned:
            return {"ok": False, "error": "请至少填写一个含义。"}
        self._store.resolve_meanings(item_id, cleaned)
        return {"ok": True, "error": ""}

    def unavailable_reason(self) -> str:
        """Why learning is not running, phrased for the management page.

        An explicit reason rather than an empty list: "nothing has been learned
        yet" and "learning is switched off" are different facts and the page
        must not render them the same way.
        """
        if not self.enabled:
            return "表达与黑话学习当前是关闭的。开启后才会从对话中学习。"
        if self._adapter is None:
            return (
                "表达与黑话学习已开启，但学习模型未配置或初始化失败。"
                "请在配置中设置 character_config.aemeath_config.providers.learning。"
            )
        return ""


def _locate_span(content: str, sources: Sequence[str]) -> str:
    """Find the span of a learned phrase inside its source messages.

    Reuses the memory module's clause matcher rather than reimplementing it, so
    "the text this came from" means the same thing in both stores and a single
    forgetting operation can remove the same span from each.
    """
    from .memory import locate_fragment

    return locate_fragment(content, sources)


__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "KIND_EXPRESSION",
    "KIND_JARGON",
    "KINDS",
    "STATUS_CANDIDATE",
    "STATUS_ENABLED",
    "STATUS_DISABLED",
    "STATUS_REVOKED",
    "STATUSES",
    "ORIGIN_AUTO",
    "ORIGIN_MANUAL",
    "STATE_OK",
    "STATE_NEEDS_CLARIFICATION",
    "LearningEvidence",
    "LearningKind",
    "LearningItem",
    "CheckOutcome",
    "LearningStore",
    "LearningService",
    "fingerprint_for",
    "evidence_fingerprint",
    "normalise_content",
]
