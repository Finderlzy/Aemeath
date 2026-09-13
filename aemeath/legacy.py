"""One-off import of upstream JSON chat history into the local database.

Upstream stored conversations as JSON files under ``chat_history/<conf_uid>/``.
Aemeath's SQLite database is the single authoritative source, so those files are
**not** read automatically and are **not** imported at startup. The user runs an
explicit, one-off import instead.

Rules the plan fixes:

* The import is idempotent per source file: a file is imported once, tracked by
  its source key, so running it twice does not duplicate history.
* Messages are imported in order and attributed to their original roles.
* A source file is deleted only after its content is verified as present.
* When a file cannot be deleted, the migration is reported as **incomplete**.
  The caller must not describe the data sources as unified in that case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

#: Upstream writes these role names into JSON history files.
_ROLE_MAP = {"human": "user", "ai": "assistant", "system": "system"}


def _candidate_history_dirs() -> List[Path]:
    """Locations upstream may have used for JSON history."""
    root = Path(__file__).resolve().parent.parent
    return [
        root / "vendor" / "Open-LLM-VTuber" / "chat_history",
    ]


def _read_history_file(path: Path) -> List[Dict[str, Any]]:
    """Read one upstream history file into normalised message dicts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read legacy history {}: {}", path, exc)
        return []

    if not isinstance(data, dict):
        return []

    messages = data.get("messages") or []
    normalised: List[Dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = _ROLE_MAP.get(str(item.get("role") or ""), None)
        content = item.get("content")
        if role is None or not isinstance(content, str) or not content:
            continue
        normalised.append({"role": role, "content": content})
    return normalised


async def import_legacy_histories(store, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Import legacy JSON histories once, then delete the source files.

    Args:
        store: The authoritative :class:`~aemeath.memory.MemoryStore`.
        root: Optional override for the Aemeath project root.

    Returns:
        A report describing what was imported, skipped and failed. ``complete``
        is only ``True`` when every imported source file was verified and
        deleted.
    """
    imported: List[Dict[str, Any]] = []
    skipped: List[str] = []
    failed: List[Dict[str, str]] = []
    undeleted: List[str] = []

    directories = [root / "vendor" / "Open-LLM-VTuber" / "chat_history"] if root else []
    directories.extend(_candidate_history_dirs())

    seen: set[Path] = set()
    files: List[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            if path in seen:
                continue
            seen.add(path)
            files.append(path)

    for path in files:
        source_key = str(path)
        existing = store.conversation_by_source_key(source_key)
        if existing:
            # Already imported: leave the file alone and report it as skipped
            # rather than importing a duplicate copy.
            skipped.append(source_key)
            continue

        messages = _read_history_file(path)
        if not messages:
            skipped.append(source_key)
            continue

        conversation_id = store.create_conversation(
            title=path.stem, source_key=source_key
        )
        for message in messages:
            store.add_message(
                role=message["role"],
                source="legacy_import",
                content=message["content"],
                conversation_id=conversation_id,
            )

        # Verify before deleting: the import is only claimed once the rows are
        # actually readable back.
        stored = store.conversation_messages(conversation_id)
        if len(stored) != len(messages):
            failed.append(
                {
                    "file": source_key,
                    "reason": f"stored {len(stored)} of {len(messages)} messages",
                }
            )
            continue

        try:
            path.unlink()
        except Exception as exc:
            # The data is in SQLite, but the source file is still there. Report
            # the migration as incomplete rather than as finished.
            undeleted.append(source_key)
            logger.error("Imported but could not delete legacy history {}: {}", path, exc)

        imported.append(
            {
                "file": source_key,
                "conversation_id": conversation_id,
                "messages": len(messages),
            }
        )

    complete = not failed and not undeleted
    result = {
        "success": complete,
        "complete": complete,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "undeleted": undeleted,
        "summary": (
            f"imported {len(imported)} file(s), skipped {len(skipped)}, "
            f"failed {len(failed)}, undeleted {len(undeleted)}"
        ),
    }
    if not complete:
        logger.warning(
            "Legacy history migration is incomplete: {} file(s) could not be "
            "deleted and {} failed. Data sources are not yet unified.",
            len(undeleted),
            len(failed),
        )
    return result
