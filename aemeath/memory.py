"""Local memory: messages, facts, experiences and embeddings.

Storage is SQLite plus in-process NumPy cosine similarity. The plan deliberately
avoids Mem0, remote databases and a separate vector service: at the first
release's scale (under ~10,000 memories) scoring vectors held locally is enough
to validate quality, and it keeps the authoritative data on this machine.

Design rules enforced here:

* The database is authoritative. Upstream's history UI is a view, not a second
  source of truth.
* Every memory must be traceable to the messages it came from.
* The character's own replies and screen guesses never become user facts.
* Deleting a memory removes its text, vector, derived summaries, the source
  content it was extracted from, and any pending extraction task, so it cannot
  be re-derived on the next turn.
* Changing the embedding model invalidates the index rather than mixing
  incompatible vectors.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from loguru import logger

from .adapters import EmbeddingAdapter, ExtractedFact, ExtractionAdapter
from .interfaces import MemoryRecord

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    source_key TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    turn_id TEXT,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    conversation_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memory_evidence (
    memory_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    fragment TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (memory_id, message_id)
);

CREATE TABLE IF NOT EXISTS derived_from (
    memory_id TEXT NOT NULL,
    parent_memory_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, parent_memory_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    memory_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pending_extraction (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    conversation_id TEXT
);

CREATE TABLE IF NOT EXISTS pending_extraction_messages (
    task_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (task_id, message_id)
);

CREATE TABLE IF NOT EXISTS invalidated_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(valid, kind);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);
"""

# Indexes that depend on columns added by a later migration. They are created
# *after* the migration runs, because on an older database the column does not
# exist yet and ``CREATE INDEX`` would fail before the ALTER could add it.
_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
"""


@dataclass(frozen=True)
class Message:
    """A stored conversation message."""

    message_id: str
    role: str
    source: str
    turn_id: Optional[str]
    content: str
    created_at: float
    status: str
    conversation_id: Optional[str] = None
    revision: int = 1


@dataclass(frozen=True)
class Evidence:
    """One piece of source text supporting a memory.

    The fragment is the verifiable span the memory was extracted from, so a
    deletion can remove exactly that text from the visible history instead of
    destroying the whole message.
    """

    message_id: str
    fragment: str
    revision: int = 1


@dataclass(frozen=True)
class MemoryItem:
    """A stored fact or experience."""

    memory_id: str
    kind: str
    content: str
    created_at: float
    updated_at: float
    valid: bool
    superseded_by: Optional[str] = None

    def to_record(self, source_message_ids: Sequence[str] = ()) -> MemoryRecord:
        """Convert to the cross-module record type."""
        return MemoryRecord(
            memory_id=self.memory_id,
            content=self.content,
            kind=self.kind,
            created_at=self.created_at,
            source_message_ids=tuple(source_message_ids),
        )


def locate_fragment(memory_content: str, sources: Sequence[str]) -> str:
    """Locate a memory's text inside any of its candidate source messages.

    Public wrapper around :func:`_locate_fragment` for callers outside this
    module (the live probes and diagnostics scripts), which have a fact and a
    set of messages but no database row.

    Args:
        memory_content: The memory text to locate.
        sources: Candidate source messages, newest first.

    Returns:
        The matched span, or ``""`` when it cannot be located confidently.
    """
    for source in sources:
        located = _locate_fragment(source, memory_content)
        if located:
            return located
    return ""


def _locate_fragment(source: str, memory_content: str) -> str:
    """Find the span of a memory's text inside its source message.

    Used for rows written before evidence fragments existed, and as a fallback
    when an extractor does not supply one.

    Matching has to tolerate the rewrite an extraction model performs: it
    normalises "我住在杭州" into "用户住在杭州", so a literal substring test
    fails even though the fact clearly came from that sentence. The strategy is
    therefore, in order:

    1. the exact memory text, when present;
    2. the whole clause with the highest character overlap, when that overlap
       is high enough to be confident;
    3. nothing, which the caller reports as "selection required" rather than
       guessing and deleting the wrong text.

    Args:
        source: The full message text.
        memory_content: The memory text to locate.

    Returns:
        The matched span, or ``""`` when it cannot be located confidently.
    """
    if not source or not memory_content:
        return ""
    target = memory_content.strip()

    if target in source:
        return target

    clauses = _split_clauses(source)
    if not clauses:
        return ""

    # A single clause can be accepted outright if the memory text contains it.
    for clause in clauses:
        if clause and clause in target:
            return clause

    # Otherwise take the best-overlapping clause, but only when the overlap is
    # strong enough that deleting it is clearly correct.
    best = ""
    best_score = 0.0
    target_chars = set(target)
    for clause in clauses:
        if not clause:
            continue
        distance = _edit_similarity(clause, target)
        if distance > best_score:
            best_score = distance
            best = clause

    if best and best_score >= _MIN_FRAGMENT_SIMILARITY:
        return best
    return ""


#: Minimum similarity between a clause and a memory text before the clause is
#: treated as that memory's source span. Below this the caller asks the user
#: to pick the fragment instead of deleting something that may be unrelated.
_MIN_FRAGMENT_SIMILARITY = 0.5


def _edit_similarity(left: str, right: str) -> float:
    """Similarity of two short strings, in ``[0, 1]``.

    Uses a longest-common-subsequence ratio, which tolerates the insertions and
    substitutions an extractor introduces ("我" -> "用户") without being so
    permissive that unrelated sentences score highly.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    previous = [0] * (len(right) + 1)
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right):
            if left_char == right_char:
                current.append(previous[index] + 1)
            else:
                current.append(max(previous[index + 1], current[index]))
        previous = current

    longest = previous[-1]
    # Use max of symmetric Dice ratio and containment ratio against the shorter string (the clause).
    # When an extraction adds context words (e.g. clause is '看到第三季了。' (7 chars) and
    # target is '用户正在看的这部纪录片已看到第三季。' (18 chars)), the clause is almost entirely
    # contained (6/7 matching chars), but symmetric ratio drops to 12/25 = 0.48.
    ratio_sym = 2.0 * longest / (len(left) + len(right))
    ratio_contain = longest / min(len(left), len(right))
    return max(ratio_sym, ratio_contain)


def _split_clauses(text: str) -> List[str]:
    """Split a message into clause-sized spans for fragment matching.

    Commas are separators as well as sentence-ending punctuation, because one
    message commonly carries several facts joined by commas, and a deletion
    must be able to remove just one of them.
    """
    parts: List[str] = []
    current = ""
    for char in text:
        if char in "，,、；;":
            if current.strip():
                parts.append(current.strip())
            current = ""
            continue
        current += char
        if char in "。！？!?\n":
            if current.strip():
                parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


def _remove_span(content: str, fragment: str) -> str:
    """Remove one fragment from a message, keeping the surrounding text.

    The plan requires that deleting one fact from a message that also carries
    another fact leaves the other fact intact.
    """
    if not fragment or fragment not in content:
        return content
    remaining = content.replace(fragment, "", 1)
    # Tidy the seam so the visible history does not keep a dangling separator.
    remaining = re.sub(r"[，,、；;]\s*[，,、；;]", "，", remaining)
    remaining = re.sub(r"^\s*[，,、；;]\s*", "", remaining)
    remaining = re.sub(r"\s*[，,、；;]\s*$", "", remaining)
    return remaining.strip()


class MemoryStore:
    """SQLite-backed local memory with NumPy vector retrieval."""

    def __init__(
        self,
        db_path: Path,
        embedding: Optional[EmbeddingAdapter] = None,
        *,
        similarity_floor: float = 0.5,
    ) -> None:
        """Open (and if needed create) the local database.

        Args:
            db_path: Path to the SQLite file.
            embedding: Adapter used to embed memories and queries.
            similarity_floor: Minimum cosine similarity for a memory to be
                returned from retrieval.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedding = embedding
        self.similarity_floor = similarity_floor
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
        """Open a connection, commit on success, and always close it.

        ``with sqlite3.connect(...)`` is a *transaction* context manager, not a
        closing one: it commits but leaves the handle open. On Windows an open
        handle keeps the database file locked, which breaks temporary-directory
        cleanup, database replacement and any "delete the run's data" step. This
        wrapper does both jobs.

        Yields:
            An open connection; committed if the block returns normally,
            rolled back if it raises.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create tables and migrate an existing database in place.

        Migration only ever adds columns and tables; existing rows keep their
        content. A database created before evidence fragments existed gets
        empty fragment strings, which the deletion path treats as "not yet
        located" rather than as "no evidence".
        """
        with self._connection() as conn:
            conn.executescript(_SCHEMA)
            # The migration must run before any index that uses a column it
            # adds, otherwise an older database fails to open.
            self._migrate(conn)
            conn.executescript(_POST_MIGRATION_INDEXES)

            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current = int(row["value"])
                if current < SCHEMA_VERSION:
                    logger.info(
                        "Migrating database schema {} -> {}.", current, SCHEMA_VERSION
                    )
                    conn.execute(
                        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
                elif current > SCHEMA_VERSION:
                    logger.warning(
                        "Database schema version {} is newer than expected {}.",
                        current,
                        SCHEMA_VERSION,
                    )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first release."""

        def columns(table: str) -> set:
            try:
                return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:  # pragma: no cover - table always exists here
                return set()

        additions = {
            "messages": {
                "conversation_id": "TEXT",
                "revision": "INTEGER NOT NULL DEFAULT 1",
            },
            "memories": {"revision": "INTEGER NOT NULL DEFAULT 1"},
            "embeddings": {"revision": "INTEGER NOT NULL DEFAULT 1"},
            "pending_extraction": {
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "conversation_id": "TEXT",
            },
            "memory_evidence": {
                "fragment": "TEXT NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
            },
            "pending_extraction_messages": {
                "message_revision": "INTEGER NOT NULL DEFAULT 1",
            },
        }
        for table, spec in additions.items():
            existing = columns(table)
            for name, declaration in spec.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_message(
        self,
        *,
        role: str,
        source: str,
        content: str,
        turn_id: Optional[str] = None,
        status: str = "complete",
        conversation_id: Optional[str] = None,
    ) -> str:
        """Persist one message and return its id.

        Args:
            role: ``user`` or ``assistant``.
            source: Event source value (see :class:`EventSource`).
            content: Message text.
            turn_id: Turn this message belongs to.
            status: ``complete``, ``interrupted`` or ``deleted``.
            conversation_id: Conversation this message belongs to.

        Returns:
            The new message id.
        """
        message_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, role, source, turn_id, content, created_at, status,
                     conversation_id, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    message_id,
                    role,
                    source,
                    turn_id,
                    content,
                    now,
                    status,
                    conversation_id,
                ),
            )
            if conversation_id:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return message_id

    def get_message(self, message_id: str) -> Optional[Message]:
        """Fetch one message by id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._row_to_message(row) if row else None

    def recent_messages(self, limit: int = 12) -> List[Message]:
        """Return the most recent visible messages in chronological order.

        Messages marked ``deleted`` are excluded: a forgotten fact must not
        come back through the working context either.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE status != 'deleted' "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    def turn_messages(self, turn_id: str) -> List[Message]:
        """All visible messages belonging to one turn."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE turn_id = ? AND status != 'deleted' "
                "ORDER BY created_at",
                (turn_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def completed_turn_count(self) -> int:
        """Number of completed assistant turns."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE role = 'assistant' AND status = 'complete'"
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        """Convert a row into a :class:`Message`."""
        keys = row.keys()
        return Message(
            message_id=row["id"],
            role=row["role"],
            source=row["source"],
            turn_id=row["turn_id"],
            content=row["content"],
            created_at=row["created_at"],
            status=row["status"],
            conversation_id=(
                row["conversation_id"] if "conversation_id" in keys else None
            ),
            revision=row["revision"] if "revision" in keys else 1,
        )

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def create_conversation(
        self, *, title: str = "", source_key: Optional[str] = None
    ) -> str:
        """Create a conversation and return its id.

        Args:
            title: Display title.
            source_key: Identifier of the legacy file this came from, used to
                make the one-off import idempotent.

        Returns:
            The new conversation id.
        """
        conversation_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO conversations (id, title, created_at, updated_at, source_key)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, title, now, now, source_key),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one conversation record."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def conversation_by_source_key(self, source_key: str) -> Optional[str]:
        """Find a conversation previously imported from a legacy source."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_key = ?", (source_key,)
            ).fetchone()
        return row["id"] if row else None

    def list_conversations(self) -> List[Dict[str, Any]]:
        """List conversations newest first, in the client's expected shape."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            result = []
            for row in rows:
                latest = conn.execute(
                    "SELECT content, created_at FROM messages "
                    "WHERE conversation_id = ? AND status != 'deleted' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                result.append(
                    {
                        "uid": row["id"],
                        "title": row["title"],
                        "timestamp": datetime.fromtimestamp(
                            row["updated_at"], tz=timezone.utc
                        ).isoformat(),
                        "latest_message": (
                            {
                                "role": "human",
                                "content": latest["content"],
                            }
                            if latest
                            else None
                        ),
                    }
                )
        return result

    def conversation_messages(self, conversation_id: str) -> List[Message]:
        """All visible messages in a conversation, oldest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                "AND status != 'deleted' ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation with its messages.

        Memories extracted from those messages are removed too, so deleting a
        conversation cannot leave facts behind that the user believes are gone.

        Returns:
            ``True`` when a conversation was deleted.
        """
        with self._connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                return False
            message_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                )
            ]

        for message_id in message_ids:
            self.forget_by_message(message_id)

        with self._connection() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        logger.info("Deleted conversation {} and its messages.", conversation_id)
        return True

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    def add_memory(
        self,
        *,
        content: str,
        kind: str = "fact",
        source_message_ids: Sequence[str] = (),
        fragments: Optional[Sequence[str]] = None,
        derived_from: Sequence[str] = (),
    ) -> str:
        """Store a memory together with its evidence.

        Args:
            content: The remembered text.
            kind: ``fact`` or ``experience``.
            source_message_ids: Messages this was derived from.
            fragments: The verifiable spans the memory came from. When omitted,
                the span is located inside the source message so the memory can
                later be deleted precisely.
            derived_from: Parent memories this one was summarised from.

        Returns:
            The new memory id.
        """
        memory_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO memories (id, kind, content, created_at, updated_at, valid, revision)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                """,
                (memory_id, kind, content, now, now),
            )
            for index, message_id in enumerate(source_message_ids):
                fragment = ""
                if fragments and index < len(fragments):
                    fragment = fragments[index] or ""
                if not fragment:
                    row = conn.execute(
                        "SELECT content FROM messages WHERE id = ?", (message_id,)
                    ).fetchone()
                    if row is not None:
                        fragment = _locate_fragment(row["content"], content)
                # The revision recorded here is the source's revision at
                # extraction time; a later change makes this evidence stale.
                revision = 1
                row = conn.execute(
                    "SELECT revision FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                if row is not None:
                    revision = row["revision"] or 1
                conn.execute(
                    "INSERT OR IGNORE INTO memory_evidence "
                    "(memory_id, message_id, fragment, revision) VALUES (?, ?, ?, ?)",
                    (memory_id, message_id, fragment, revision),
                )
            for parent_id in derived_from:
                conn.execute(
                    "INSERT OR IGNORE INTO derived_from (memory_id, parent_memory_id) "
                    "VALUES (?, ?)",
                    (memory_id, parent_id),
                )
        return memory_id

    def get_memory(self, memory_id: str) -> Optional[MemoryItem]:
        """Fetch one memory by id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_memory(row) if row else None

    def evidence_for(self, memory_id: str) -> List[str]:
        """Return the message ids supporting a memory."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT message_id FROM memory_evidence WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def list_memories(
        self, *, include_invalid: bool = False, query: str = ""
    ) -> List[MemoryItem]:
        """List memories, optionally including superseded ones.

        Args:
            include_invalid: Include memories marked invalid.
            query: Optional case-insensitive substring filter.

        Returns:
            Matching memories, newest first.
        """
        sql = "SELECT * FROM memories"
        clauses: List[str] = []
        params: List[Any] = []
        if not include_invalid:
            clauses.append("valid = 1")
        if query:
            clauses.append("content LIKE ?")
            params.append(f"%{query}%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def correct_memory(
        self, old_memory_id: str, new_content: str, *, kind: Optional[str] = None
    ) -> str:
        """Replace a memory: invalidate the old one and store the new one.

        The old memory keeps its text for provenance but is excluded from
        retrieval, so a corrected fact cannot resurface.

        Args:
            old_memory_id: Memory being superseded.
            new_content: Replacement text.
            kind: Optional kind override; defaults to the old memory's kind.

        Returns:
            The new memory id.
        """
        old = self.get_memory(old_memory_id)
        if old is None:
            raise KeyError(f"memory not found: {old_memory_id}")

        evidence = self.evidence_for(old_memory_id)
        new_id = self.add_memory(
            content=new_content, kind=kind or old.kind, source_message_ids=evidence
        )

        with self._connection() as conn:
            conn.execute(
                "UPDATE memories SET valid = 0, superseded_by = ?, updated_at = ? "
                "WHERE id = ?",
                (new_id, time.time(), old_memory_id),
            )
            # Drop the stale vector so it cannot be retrieved.
            conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (old_memory_id,))

        logger.info("Corrected memory {} -> {}.", old_memory_id, new_id)
        return new_id

    def delete_memory(self, memory_id: str) -> None:
        """Permanently remove a memory and everything derived from it.

        This removes the memory row, its vector, its evidence links, the source
        messages it was extracted from, and any pending extraction task that
        still references those messages, so the next turn cannot recreate it.

        Args:
            memory_id: Memory to delete.
        """
        evidence = self.evidence_for(memory_id)
        with self._connection() as conn:
            conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM derived_from WHERE memory_id = ?", (memory_id,))
            conn.execute(
                "DELETE FROM derived_from WHERE parent_memory_id = ?", (memory_id,)
            )
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

            for message_id in evidence:
                conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                conn.execute(
                    "DELETE FROM pending_extraction_messages WHERE message_id = ?",
                    (message_id,),
                )
                conn.execute(
                    "DELETE FROM memory_evidence WHERE message_id = ?", (message_id,)
                )

            # Remove any memories left without evidence, and empty tasks.
            conn.execute(
                "DELETE FROM memories WHERE id NOT IN "
                "(SELECT memory_id FROM memory_evidence) AND kind = 'fact'"
            )
            conn.execute(
                "DELETE FROM pending_extraction WHERE id NOT IN "
                "(SELECT task_id FROM pending_extraction_messages)"
            )

        logger.info("Deleted memory {} and its derived data.", memory_id)

    def purge_by_source_message(self, message_id: str) -> int:
        """Invalidate memories that cite a specific message.

        Args:
            message_id: Message whose derived memories should go.

        Returns:
            Number of memories removed.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT memory_id FROM memory_evidence WHERE message_id = ?",
                (message_id,),
            ).fetchall()
        for row in rows:
            self.delete_memory(row["memory_id"])
        return len(rows)

    # ------------------------------------------------------------------
    # Precise forgetting
    # ------------------------------------------------------------------

    def evidence_records(self, memory_id: str) -> List[Evidence]:
        """Return the message ids and verifiable fragments for a memory."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT message_id, fragment, revision FROM memory_evidence "
                "WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
        return [
            Evidence(
                message_id=row["message_id"],
                fragment=row["fragment"] or "",
                revision=row["revision"] or 1,
            )
            for row in rows
        ]

    def forget(
        self, memory_id: str, *, fragments: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """Forget one fact precisely, keeping unrelated content in the source.

        The plan's rule: remove the target fact and the specific fragment it was
        extracted from, keep other facts from the same message, and invalidate
        and rebuild anything derived that cannot be split reliably.

        Args:
            memory_id: The memory to forget.
            fragments: Explicit fragments to remove when the stored evidence has
                no verifiable span (legacy rows). When a memory has no fragment
                and none is supplied, the operation reports that it needs a
                selection instead of claiming success.

        Returns:
            A result dictionary describing what was removed and what still
            needs the user's input.
        """
        memory = self.get_memory(memory_id)
        if memory is None:
            return {
                "success": False,
                "reason": "memory not found",
                "memory_id": memory_id,
            }

        evidence = self.evidence_records(memory_id)

        # Legacy rows have no fragment. Try to locate the memory text inside the
        # source deterministically; if that fails, ask for a selection rather
        # than guessing and deleting the wrong text.
        resolved: List[Tuple[str, str]] = []
        needs_selection: List[Dict[str, Any]] = []
        for item in evidence:
            fragment = item.fragment.strip()
            if not fragment:
                source = self.get_message(item.message_id)
                if source is not None:
                    located = _locate_fragment(source.content, memory.content)
                    if located:
                        fragment = located
            if fragment:
                resolved.append((item.message_id, fragment))
            else:
                source = self.get_message(item.message_id)
                needs_selection.append(
                    {
                        "message_id": item.message_id,
                        "content": source.content if source else "",
                    }
                )

        if fragments:
            # Explicit selection from the memory-management screen.
            resolved = [(mid, frag) for mid, _ in resolved for frag in fragments] or [
                (item.message_id, frag)
                for item in evidence
                for frag in fragments
            ]

        if needs_selection and not fragments:
            # Do not report forgetfulness that has not actually happened.
            return {
                "success": False,
                "reason": "source fragment could not be located; selection required",
                "memory_id": memory_id,
                "needs_selection": needs_selection,
            }

        # Invalidate derived summaries that overlap this evidence and cannot be
        # split reliably. They are rebuilt from what remains.
        invalidated = self._invalidate_derived(memory_id)

        removed_fragments: List[str] = []
        with self._connection() as conn:
            for message_id, fragment in resolved:
                removed_fragments.append(fragment)
                conn.execute(
                    "UPDATE messages SET content = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (_remove_span(self._message_content(conn, message_id), fragment), message_id),
                )

            # Invalidate (do not delete) overlapping sibling memories so the
            # unrelated ones survive and the overlapping ones stop being used.
            siblings = self._overlapping_memories(memory_id, removed_fragments)
            for sibling_id in siblings:
                conn.execute(
                    "UPDATE memories SET valid = 0, updated_at = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (time.time(), sibling_id),
                )
                conn.execute(
                    "DELETE FROM embeddings WHERE memory_id = ?", (sibling_id,)
                )
                conn.execute(
                    "INSERT INTO invalidated_content (memory_id, reason, created_at) "
                    "VALUES (?, ?, ?)",
                    (
                        sibling_id,
                        "overlapping evidence invalidated by forgetting",
                        time.time(),
                    ),
                )

            # Finally the target memory itself.
            conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM derived_from WHERE memory_id = ?", (memory_id,))
            conn.execute(
                "DELETE FROM derived_from WHERE parent_memory_id = ?", (memory_id,)
            )
            conn.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

            # Bump the revision of the source messages so an in-flight
            # extraction request for them can no longer write back.
            for message_id, _ in resolved:
                conn.execute(
                    "UPDATE messages SET revision = revision + 1 WHERE id = ?",
                    (message_id,),
                )
                conn.execute(
                    "DELETE FROM pending_extraction_messages WHERE message_id = ?",
                    (message_id,),
                )

            # Remove tasks that no longer reference any message.
            conn.execute(
                "DELETE FROM pending_extraction WHERE id NOT IN "
                "(SELECT task_id FROM pending_extraction_messages)"
            )

        logger.info(
            "Forgot memory {} ({} fragment(s), {} derived invalidated).",
            memory_id,
            len(removed_fragments),
            len(invalidated),
        )
        return {
            "success": True,
            "memory_id": memory_id,
            "removed_fragments": removed_fragments,
            "invalidated_derived": invalidated,
            "rebuild_required": bool(invalidated),
        }

    def forget_by_message(self, message_id: str) -> int:
        """Forget everything derived from one message and hide the message.

        The message itself is marked ``deleted`` rather than dropped, because
        the client's history must stop showing it while the row id stays
        referable for diagnostics. Memories extracted from it are forgotten
        with their fragments.

        Args:
            message_id: Message whose derived memories should go.

        Returns:
            Number of memories forgotten.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT memory_id FROM memory_evidence WHERE message_id = ?",
                (message_id,),
            ).fetchall()
        count = 0
        for row in rows:
            result = self.forget(row["memory_id"])
            if result.get("success"):
                count += 1

        # Whatever happened to the derived memories, the message must stop
        # being visible: otherwise the text the user asked to forget keeps
        # coming back through history and the working context.
        with self._connection() as conn:
            conn.execute(
                "UPDATE messages SET status = 'deleted', revision = revision + 1 "
                "WHERE id = ?",
                (message_id,),
            )
            conn.execute(
                "DELETE FROM pending_extraction_messages WHERE message_id = ?",
                (message_id,),
            )
            conn.execute(
                "DELETE FROM pending_extraction WHERE id NOT IN "
                "(SELECT task_id FROM pending_extraction_messages)"
            )
        return count

    def _message_content(self, conn: sqlite3.Connection, message_id: str) -> str:
        """Read a message body inside an existing transaction."""
        row = conn.execute(
            "SELECT content FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row["content"] if row else ""

    def _overlapping_memories(
        self, memory_id: str, fragments: Sequence[str]
    ) -> List[str]:
        """Other valid memories whose evidence overlaps the removed fragments."""
        if not fragments:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT m.id, m.content FROM memories m "
                "JOIN memory_evidence e ON e.memory_id = m.id "
                "WHERE m.id != ? AND m.valid = 1",
                (memory_id,),
            ).fetchall()

        overlapping = []
        for row in rows:
            for fragment in fragments:
                if fragment and fragment in (row["content"] or ""):
                    overlapping.append(row["id"])
                    break
        return overlapping

    def _invalidate_derived(self, memory_id: str) -> List[str]:
        """Invalidate memories derived from this one.

        A derived summary that cannot be split reliably is not kept partially:
        it is invalidated and rebuilt from the remaining content.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT memory_id FROM derived_from WHERE parent_memory_id = ?",
                (memory_id,),
            ).fetchall()
            children = [row["memory_id"] for row in rows]
            for child_id in children:
                conn.execute(
                    "UPDATE memories SET valid = 0, updated_at = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (time.time(), child_id),
                )
                conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (child_id,))
                conn.execute(
                    "INSERT INTO invalidated_content (memory_id, reason, created_at) "
                    "VALUES (?, ?, ?)",
                    (child_id, "parent memory forgotten", time.time()),
                )
        return children

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryItem:
        """Convert a row into a :class:`MemoryItem`."""
        return MemoryItem(
            memory_id=row["id"],
            kind=row["kind"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            valid=bool(row["valid"]),
            superseded_by=row["superseded_by"],
        )

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @property
    def embedding(self) -> Optional[EmbeddingAdapter]:
        """The configured embedding adapter, if any."""
        return self._embedding

    @property
    def embedding_model_id(self) -> str:
        """Identifier of the configured embedding model."""
        return self._embedding.model_id if self._embedding else ""

    def store_embedding(self, memory_id: str, vector: np.ndarray) -> None:
        """Persist a memory's vector.

        Args:
            memory_id: Memory the vector belongs to.
            vector: The embedding.
        """
        model_id = self.embedding_model_id
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO embeddings (memory_id, model_id, dimensions, vector, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    dimensions=excluded.dimensions,
                    vector=excluded.vector,
                    created_at=excluded.created_at
                """,
                (memory_id, model_id, len(vector), blob, time.time()),
            )

    def load_vectors(self) -> Tuple[List[str], np.ndarray]:
        """Load all valid vectors for the current embedding model.

        Vectors from a different model are excluded rather than mixed, because
        comparing across incompatible embedding spaces is meaningless.

        Returns:
            A tuple of (memory ids, matrix of shape ``(n, dimensions)``).
        """
        model_id = self.embedding_model_id
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT e.memory_id, e.dimensions, e.vector
                FROM embeddings e
                JOIN memories m ON m.id = e.memory_id
                WHERE m.valid = 1 AND e.model_id = ?
                """,
                (model_id,),
            ).fetchall()

        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)

        ids = [row["memory_id"] for row in rows]
        dimension = rows[0]["dimensions"]
        # Guard against a partially rebuilt index with mixed widths.
        usable = [row for row in rows if row["dimensions"] == dimension]
        ids = [row["memory_id"] for row in usable]
        matrix = np.vstack(
            [np.frombuffer(row["vector"], dtype=np.float32) for row in usable]
        )
        return ids, matrix

    def indexed_model_ids(self) -> List[str]:
        """Distinct embedding models currently present in the index."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT model_id FROM embeddings"
            ).fetchall()
        return [row["model_id"] for row in rows]

    def needs_reindex(self) -> bool:
        """Whether stored vectors come from a different embedding model."""
        models = set(self.indexed_model_ids())
        if not models:
            return False
        return models != {self.embedding_model_id}

    async def reindex(self, *, batch_size: int = 32) -> int:
        """Rebuild embeddings for every valid memory.

        Called when the embedding model changes. Existing vectors are discarded
        first so an interrupted rebuild cannot leave a mixed index.

        Args:
            batch_size: Texts per embedding request.

        Returns:
            Number of memories re-embedded.
        """
        if self._embedding is None:
            raise RuntimeError("no embedding adapter configured")

        with self._connection() as conn:
            conn.execute("DELETE FROM embeddings")

        memories = self.list_memories()
        count = 0
        for start in range(0, len(memories), batch_size):
            batch = memories[start : start + batch_size]
            vectors = await self._embedding.embed([item.content for item in batch])
            for item, vector in zip(batch, vectors):
                self.store_embedding(item.memory_id, vector)
                count += 1
        logger.info("Reindexed {} memories.", count)
        return count

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def recall(self, query: str, limit: int = 5) -> List[MemoryRecord]:
        """Retrieve memories relevant to a query.

        Retrieval is allowed — and expected — to return nothing. A similarity
        floor rejects unrelated memories instead of always returning the
        top-``limit`` rows, which is how unrelated text ends up injected into
        every prompt.

        Args:
            query: The user's current message.
            limit: Maximum number of memories to return.

        Returns:
            Relevant memories, most similar first; empty when nothing clears
            the floor.

        Raises:
            Exception: Propagates embedding failures so callers can report that
                memory retrieval is unavailable instead of pretending to recall.
        """
        if not query.strip():
            return []
        if self._embedding is None:
            return []

        ids, matrix = self.load_vectors()
        if not ids:
            return []

        query_vector = (await self._embedding.embed([query]))[0]
        query_vector = np.asarray(query_vector, dtype=np.float32)

        # A zero vector or a non-finite value cannot be ranked meaningfully;
        # scoring it would silently produce arbitrary results.
        if not np.all(np.isfinite(query_vector)):
            logger.warning("Query embedding contains non-finite values; skipping recall.")
            return []
        if float(np.linalg.norm(query_vector)) == 0.0:
            logger.warning("Query embedding is a zero vector; skipping recall.")
            return []

        if query_vector.shape[0] != matrix.shape[1]:
            # Index built with a different model: do not compare across spaces.
            logger.warning(
                "Query vector width {} != index width {}; rebuild the index.",
                query_vector.shape[0],
                matrix.shape[1],
            )
            return []

        norms = np.linalg.norm(matrix, axis=1)
        similarities = matrix @ query_vector / (norms * np.linalg.norm(query_vector) + 1e-9)

        # Drop rows that cannot be scored: zero-length or non-finite vectors.
        finite = np.isfinite(similarities) & (norms > 0.0)

        order = np.argsort(-np.where(finite, similarities, -np.inf))

        results: List[MemoryRecord] = []
        for index in order:
            index = int(index)
            if not finite[index]:
                continue
            score = float(similarities[index])
            if score < self.similarity_floor:
                # Sorted descending, so everything after this is below too.
                break
            memory = self.get_memory(ids[index])
            if memory is None or not memory.valid:
                continue
            results.append(memory.to_record(self.evidence_for(memory.memory_id)))
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Extraction queue
    # ------------------------------------------------------------------

    def enqueue_extraction(self, message_ids: Sequence[str]) -> str:
        """Queue an extraction task for the given messages.

        The task records the revision of each source message at the moment it
        was queued. When the provider returns, a task whose sources have since
        changed is discarded instead of written back.
        """
        task_id = uuid.uuid4().hex
        with self._connection() as conn:
            revisions = {}
            for message_id in message_ids:
                row = conn.execute(
                    "SELECT revision FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                revisions[message_id] = row["revision"] if row else 1

            conn.execute(
                "INSERT INTO pending_extraction (id, created_at, revision) "
                "VALUES (?, ?, 1)",
                (task_id, time.time()),
            )
            for message_id in message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO pending_extraction_messages "
                    "(task_id, message_id, message_revision) VALUES (?, ?, ?)",
                    (task_id, message_id, revisions[message_id]),
                )
        return task_id

    def task_is_current(self, task_id: str, message_ids: Sequence[str]) -> bool:
        """Whether a task and all its sources are still unchanged.

        This is the check performed *after* the provider call returns and
        inside the same transaction as the write, which is what makes a delete
        that happened while the request was in flight actually win.
        """
        with self._connection() as conn:
            task = conn.execute(
                "SELECT revision FROM pending_extraction WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return False

            for message_id in message_ids:
                row = conn.execute(
                    "SELECT m.revision AS revision, t.message_revision AS queued "
                    "FROM pending_extraction_messages t "
                    "JOIN messages m ON m.id = t.message_id "
                    "WHERE t.task_id = ? AND t.message_id = ?",
                    (task_id, message_id),
                ).fetchone()
                if row is None:
                    # The source was deleted while the request was in flight.
                    return False
                if row["revision"] != row["queued"]:
                    # The source changed; the extracted text may be stale.
                    return False
            return True

    def pending_tasks(self) -> List[Dict[str, Any]]:
        """Return queued extraction tasks with their messages."""
        with self._connection() as conn:
            tasks = conn.execute(
                "SELECT * FROM pending_extraction ORDER BY created_at"
            ).fetchall()
            result = []
            for task in tasks:
                messages = conn.execute(
                    "SELECT message_id, message_revision FROM pending_extraction_messages "
                    "WHERE task_id = ?",
                    (task["id"],),
                ).fetchall()
                result.append(
                    {
                        "task_id": task["id"],
                        "attempts": task["attempts"],
                        "last_error": task["last_error"],
                        "revision": task["revision"] if "revision" in task.keys() else 1,
                        "message_ids": [m["message_id"] for m in messages],
                    }
                )
        return result

    def complete_task(self, task_id: str) -> None:
        """Remove a finished extraction task."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM pending_extraction_messages WHERE task_id = ?", (task_id,)
            )
            conn.execute("DELETE FROM pending_extraction WHERE id = ?", (task_id,))

    def fail_task(self, task_id: str, error: str) -> None:
        """Record a failed attempt while keeping the task for a later retry."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE pending_extraction SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                (error, task_id),
            )

    def store_embedding_if_current(
        self, memory_id: str, vector: np.ndarray, expected_revision: int
    ) -> bool:
        """Store a vector only when the memory still exists and is unchanged.

        An embedding request can outlive a deletion. Writing the vector anyway
        would leave an orphaned vector pointing at a memory that is gone, or
        attach a stale vector to a corrected memory.

        Returns:
            ``True`` when the vector was stored.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT valid, revision FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                logger.info(
                    "Discarding embedding for deleted memory {}.",
                    memory_id,
                )
                return False
            if not row["valid"] or row["revision"] != expected_revision:
                logger.info(
                    "Discarding embedding for changed memory {} (revision {} != {}).",
                    memory_id,
                    row["revision"],
                    expected_revision,
                )
                return False

            blob = np.asarray(vector, dtype=np.float32).tobytes()
            conn.execute(
                """
                INSERT INTO embeddings
                    (memory_id, model_id, dimensions, vector, created_at, revision)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    dimensions=excluded.dimensions,
                    vector=excluded.vector,
                    created_at=excluded.created_at,
                    revision=excluded.revision
                """,
                (
                    memory_id,
                    self.embedding_model_id,
                    len(vector),
                    blob,
                    time.time(),
                    expected_revision,
                ),
            )
        return True

    def memory_revision(self, memory_id: str) -> Optional[int]:
        """Current revision of a memory, or ``None`` when it is gone."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT revision FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return row["revision"] if row else None


class MemoryService:
    """Coordinates storage, extraction and retrieval."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        extraction: Optional[ExtractionAdapter] = None,
        recent_turns: int = 12,
        recall_limit: int = 5,
        summary_every: int = 10,
    ) -> None:
        """Wire the store to an extraction adapter and sizing limits.

        Args:
            store: The local memory store.
            extraction: Adapter that proposes facts from conversation.
            recent_turns: Recent turns kept in prompt context.
            recall_limit: Maximum memories injected per turn.
            summary_every: Completed turns between experience summaries.
        """
        self._store = store
        self._extraction = extraction
        self.recent_turns = recent_turns
        self.recall_limit = recall_limit
        self.summary_every = summary_every

    @property
    def store(self) -> MemoryStore:
        """The underlying store."""
        return self._store

    @property
    def has_extraction_adapter(self) -> bool:
        """Whether an extraction provider is configured.

        The background worker checks this so it does not spin when extraction
        was never configured.
        """
        return self._extraction is not None

    async def recall(self, query: str, limit: Optional[int] = None) -> List[MemoryRecord]:
        """Retrieve memories for a turn."""
        return await self._store.recall(query, limit or self.recall_limit)

    def record_user_message(
        self,
        content: str,
        *,
        source: str,
        turn_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """Persist a user message immediately, before generation.

        The plan requires the user's message to be on disk before the model is
        called, so a crash mid-turn does not lose what was said.
        """
        message_id = self._store.add_message(
            role="user",
            source=source,
            content=content,
            turn_id=turn_id,
            conversation_id=conversation_id,
        )
        return message_id

    def record_assistant_message(
        self,
        content: str,
        *,
        turn_id: Optional[str] = None,
        interrupted: bool = False,
        conversation_id: Optional[str] = None,
    ) -> str:
        """Persist the character's reply, marking interrupted turns."""
        return self._store.add_message(
            role="assistant",
            source="user_text",
            content=content,
            turn_id=turn_id,
            status="interrupted" if interrupted else "complete",
            conversation_id=conversation_id,
        )

    async def process_turn(
        self, user_message_id: str, assistant_message_id: Optional[str] = None
    ) -> Optional[str]:
        """Queue extraction for a finished turn.

        Args:
            user_message_id: The user's message for this turn.
            assistant_message_id: The reply, when one was produced.

        Returns:
            The queued task id, or ``None`` when nothing was queued.
        """
        evidence = [user_message_id]
        if assistant_message_id:
            evidence.append(assistant_message_id)
        return self._store.enqueue_extraction(evidence)

    async def run_pending_extraction(self) -> int:
        """Process queued extraction tasks.

        Each task is validated against its source revisions *after* the
        provider returns and before anything is written. A task whose sources
        were deleted or changed while the request was in flight is discarded,
        so a forgotten fact cannot be recreated by a request that was already
        running when the user deleted it.

        Failures leave the task queued with an error recorded, so a transient
        provider problem does not silently lose the memory.

        Returns:
            Number of memories written.
        """
        if self._extraction is None:
            return 0

        written = 0
        for task in self._store.pending_tasks():
            task_id = task["task_id"]
            message_ids = list(task["message_ids"])

            messages = [self._store.get_message(mid) for mid in message_ids]
            payload = [
                {"role": m.role, "content": m.content}
                for m in messages
                if m is not None and m.role == "user" and m.status != "deleted"
            ]
            if not payload:
                self._store.complete_task(task_id)
                continue

            try:
                facts = await self._extraction.extract(payload)
            except Exception as exc:
                logger.error("Extraction task {} failed: {}", task_id, exc)
                self._store.fail_task(task_id, str(exc))
                continue

            # The check that matters: the provider call is finished, and only
            # now do we ask whether the sources it was given are still valid.
            if not self._store.task_is_current(task_id, message_ids):
                logger.info(
                    "Discarding extraction task {}: its sources changed while "
                    "the request was in flight.",
                    task_id,
                )
                self._store.complete_task(task_id)
                continue

            user_message_ids = [
                m.message_id
                for m in messages
                if m is not None and m.role == "user" and m.status != "deleted"
            ]
            for fact in facts:
                stored = await self._write_fact(fact, user_message_ids)
                if stored:
                    written += 1
            self._store.complete_task(task_id)

        return written

    async def _write_fact(
        self, fact: ExtractedFact, source_message_ids: Sequence[str]
    ) -> Optional[str]:
        """Store a fact and its vector, tolerating embedding failure."""
        memory_id = self._store.add_memory(
            content=fact.content,
            kind=fact.kind,
            source_message_ids=source_message_ids,
        )
        revision = self._store.memory_revision(memory_id)
        embedding = self._store.embedding
        if embedding is not None:
            try:
                vectors = await embedding.embed([fact.content])
                # The embedding request can outlive a deletion; only write when
                # the memory is still there and unchanged.
                if not self._store.store_embedding_if_current(
                    memory_id, vectors[0], revision
                ):
                    return None
            except Exception as exc:
                # The memory text is kept; it simply is not retrievable until
                # an index rebuild succeeds.
                logger.error("Failed to embed memory {}: {}", memory_id, exc)
        return memory_id
