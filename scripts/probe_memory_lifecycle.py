"""End-to-end memory lifecycle check against a running service.

Drives the real management API over HTTP and the real SQLite database, so the
correction/forgetting behaviour is verified under the same conditions the page
uses. Needs a live service.

Usage::

    python scripts/probe_memory_lifecycle.py

Prints ``LIFECYCLE PASSED`` / ``LIFECYCLE FAILED``.
"""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:12393/aemeath/manage"

results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, payload=None):
    """Call one management endpoint and return ``(status, body)``."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}
    except URLError as exc:
        return 0, {"error": str(exc)}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check."""
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    """Seed a memory, correct it, forget it, and verify each step."""
    print("=== memory lifecycle probe (live service) ===\n")

    # Seed a memory directly in the authoritative database, on the same
    # connection the service uses. This stands in for a conversation that
    # taught the character the fact.
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    upstream = root / "vendor" / "Open-LLM-VTuber"
    # ``src`` is a package directory inside the upstream checkout, so the
    # checkout root goes on the path (not ``src`` itself), together with the
    # project root for ``aemeath``.
    for path in (upstream, root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from aemeath.config import load_config
    from aemeath.memory import MemoryStore

    config = load_config()
    store = MemoryStore(config.db_path, embedding=None)
    message_id = store.add_message(
        role="user",
        source="user_text",
        content="我喜欢咖啡不加糖，另外我下周要交毕业设计",
        status="complete",
    )
    memory_id = store.add_memory(
        content="喜欢咖啡不加糖",
        source_message_ids=[message_id],
        fragments=["我喜欢咖啡不加糖"],
    )
    print(f"seeded memory {memory_id[:8]} from message {message_id[:8]}\n")

    # -- it appears in the listing -------------------------------------
    status, listing = call("GET", "/memory/list")
    ids = [m["memory_id"] for m in listing.get("memories", [])]
    check("the seeded memory appears in the listing", memory_id in ids)
    item = next((m for m in listing.get("memories", []) if m["memory_id"] == memory_id), {})
    check(
        "the listing carries the source message",
        bool(item.get("sources")),
        f"{len(item.get('sources', []))} source(s)",
    )

    # -- impact is precise, not cascading ------------------------------
    status, impact = call("GET", f"/memory/{memory_id}/impact")
    check("impact is reachable", status == 200, f"HTTP {status}")
    check(
        "a locatable span reports precise removal",
        impact.get("mode") == "precise",
        f"mode={impact.get('mode')}",
    )
    check(
        "precise removal does not claim to delete source messages",
        impact.get("removes_source_messages") is False,
    )

    # -- correction ----------------------------------------------------
    status, corrected = call(
        "POST",
        "/memory/correct",
        {"memory_id": memory_id, "content": "喜欢咖啡加糖"},
    )
    check("correction succeeds", status == 200 and corrected.get("ok") is True,
          f"HTTP {status}")
    new_id = corrected.get("memory_id", "")
    check("correction returns a new memory id", bool(new_id) and new_id != memory_id)

    status, listing = call("GET", "/memory/list")
    by_id = {m["memory_id"]: m for m in listing.get("memories", [])}
    check(
        "the corrected memory is listed with the new text",
        by_id.get(new_id, {}).get("content") == "喜欢咖啡加糖",
        str(by_id.get(new_id, {}).get("content")),
    )
    check(
        "the superseded memory is excluded from the default listing",
        memory_id not in by_id,
    )

    status, with_invalid = call("GET", "/memory/list")
    check("listing still answers after a correction", status == 200)

    # -- forgetting ----------------------------------------------------
    status, impact = call("GET", f"/memory/{new_id}/impact")
    check("impact is available for the corrected memory", status == 200)

    status, forgotten = call("POST", "/memory/forget", {"memory_id": new_id})
    check(
        "forgetting succeeds",
        status == 200 and forgotten.get("ok") is True,
        f"HTTP {status} removed={len(forgotten.get('removed_fragments', []))} fragment(s)",
    )

    status, listing = call("GET", "/memory/list")
    ids = [m["memory_id"] for m in listing.get("memories", [])]
    check("the forgotten memory is gone from the listing", new_id not in ids)

    # The source message must survive with its unrelated content, which is the
    # whole point of precise forgetting.
    remaining = store.get_message(message_id)
    check("the source message survives the forgetting", remaining is not None)
    if remaining is not None:
        check(
            "the unrelated part of the source message survives",
            "毕业设计" in remaining.content,
            repr(remaining.content[:60]),
        )

    # -- restart consistency -------------------------------------------
    reopened = MemoryStore(config.db_path, embedding=None)
    check(
        "the forgotten memory is gone after reopening the database",
        reopened.get_memory(new_id) is None,
    )

    # Clean up what this probe created so it leaves no residue behind.
    for mid in (memory_id, new_id):
        if reopened.get_memory(mid) is not None:
            reopened.delete_memory(mid)

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"{passed}/{total} checks passed")
    if passed == total:
        print("LIFECYCLE PASSED")
        return 0
    print("LIFECYCLE FAILED")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
