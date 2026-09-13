"""Ad-hoc migration check: verify an existing database opens and keeps its data.

Kept out of ``tests/`` because it operates on the real local database rather
than a temporary one.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [
    str(ROOT / "vendor" / "Open-LLM-VTuber" / "src"),
    str(ROOT / "vendor" / "Open-LLM-VTuber"),
    str(ROOT),
]
sys.stdout.reconfigure(encoding="utf-8")

TABLES = ("messages", "memories", "embeddings", "memory_evidence")


def snapshot(db: str) -> dict:
    with sqlite3.connect(db) as conn:
        tables = sorted(
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in TABLES
        }
        columns = {
            t: sorted(r[1] for r in conn.execute(f"PRAGMA table_info({t})"))
            for t in ("messages", "memories")
        }
    return {
        "tables": tables,
        "version": version[0] if version else None,
        "counts": counts,
        "columns": columns,
    }


def main() -> int:
    db = str(ROOT / "data" / "aemeath.sqlite3")
    if not Path(db).exists():
        print(f"no existing database at {db}; nothing to migrate")
        return 0

    before = snapshot(db)
    print("BEFORE")
    print("  version:", before["version"])
    print("  counts :", before["counts"])
    print("  message columns:", before["columns"]["messages"])

    from aemeath.adapters import FakeEmbeddingAdapter
    from aemeath.memory import MemoryStore

    store = MemoryStore(Path(db), FakeEmbeddingAdapter())
    print("opened after migration OK")
    print("  valid memories:", len(store.list_memories()))
    print("  recent messages:", len(store.recent_messages(limit=200)))

    after = snapshot(db)
    print("AFTER")
    print("  version:", after["version"])
    print("  counts :", after["counts"])
    print("  message columns:", after["columns"]["messages"])
    print("  new tables:", sorted(set(after["tables"]) - set(before["tables"])))

    # The migration must not lose rows.
    preserved = all(
        after["counts"][t] >= before["counts"][t] for t in TABLES
    )
    print("data preserved:", preserved)
    return 0 if preserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
