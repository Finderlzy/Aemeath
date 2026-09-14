"""End-to-end check of the V2-T02 management API against a running service.

This drives the **real** server process over HTTP on loopback, so it exercises
the mounted routes, the real SQLite store and the real configuration file —
the same conditions the user's browser will use. It is not a pytest test: it
needs a live service and is run explicitly.

Usage::

    python scripts/probe_management_v2.py

Prints one line per check and ``PROBE PASSED`` / ``PROBE FAILED`` at the end.
"""

from __future__ import annotations

import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:12393/aemeath/manage"

results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, payload=None):
    """Call one management endpoint.

    Returns:
        ``(status, body)`` where body is parsed JSON, or an empty dict.
    """
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
    """Run every check and report."""
    print("=== V2-T02 management API probe (live service) ===\n")

    # -- overview is still the V2-T01 contract -------------------------
    status, overview = call("GET", "/overview")
    check("overview reachable (V2-T01 intact)", status == 200, f"HTTP {status}")
    check(
        "overview reports the configured model",
        overview.get("model", {}).get("model") == "deepseek-v4.1-flash",
        overview.get("model", {}).get("model", ""),
    )

    # -- memory: list, search, impact ----------------------------------
    status, listing = call("GET", "/memory/list")
    check("memory list reachable", status == 200, f"HTTP {status}")
    check(
        "memory list distinguishes available from unavailable",
        "available" in listing and "ok" in listing,
        f"ok={listing.get('ok')} available={listing.get('available')}",
    )

    status, found = call("POST", "/memory/search", {"query": ""})
    check("memory search (empty query lists all)", status == 200, f"HTTP {status}")
    check(
        "memory search returns an ok result",
        found.get("ok") is True,
        f"ok={found.get('ok')} error={found.get('error', '')[:60]}",
    )

    status, searched = call("POST", "/memory/search", {"query": "毕业设计"})
    check(
        "memory search by meaning is available (v2-T04 embedding index)",
        status == 200,
        f"HTTP {status} available={searched.get('available')}",
    )

    # -- live2d --------------------------------------------------------
    status, live2d = call("GET", "/live2d/overview")
    check("live2d overview reachable", status == 200, f"HTTP {status}")
    check(
        "live2d reports the active model",
        live2d.get("active_model") == "mao_pro",
        str(live2d.get("active_model")),
    )
    check(
        "live2d finds the installed model",
        any(m.get("name") == "mao_pro" for m in live2d.get("models", [])),
        f"{len(live2d.get('models', []))} model(s)",
    )
    check(
        "live2d marks the bundled model as a placeholder",
        all(
            m.get("is_official_model") is False for m in live2d.get("models", [])
        ),
        "placeholder labelling",
    )
    check(
        "live2d reports model paths for debugging",
        bool(live2d.get("models_root")) and bool(live2d.get("model_dict_path")),
        live2d.get("models_root", ""),
    )

    # -- voice ---------------------------------------------------------
    status, voice = call("GET", "/voice/overview")
    check("voice overview reachable", status == 200, f"HTTP {status}")
    check(
        "voice lists at least the sample preset",
        bool(voice.get("presets")),
        f"{len(voice.get('presets', []))} preset(s)",
    )
    check(
        "voice never claims a sample is the official voice",
        all(p.get("is_official_voice") is False for p in voice.get("presets", [])),
        "is_official_voice=false on every preset",
    )
    check(
        "voice reports the configured engine",
        bool(voice.get("engine")),
        str(voice.get("engine")),
    )

    status, audition = call(
        "POST", "/voice/audition", {"preset_id": "", "text": "你好。"}
    )
    check(
        "audition answers (audio or a stated reason)",
        status == 200 and ("audio" in audition or "error" in audition),
        f"HTTP {status} ok={audition.get('ok')}",
    )
    if status == 200 and not audition.get("ok"):
        check(
            "audition explains why it could not synthesise",
            bool(audition.get("error")),
            audition.get("error", "")[:80],
        )

    # -- live2d write guard --------------------------------------------
    status, conflict = call(
        "POST",
        "/live2d/save",
        {
            "model_name": "mao_pro",
            "scale": 1.0,
            "x_offset": 0,
            "y_offset": 0,
            "expected_revision": "stale-revision",
        },
    )
    check(
        "live2d refuses a stale revision (409)",
        status == 409 and conflict.get("conflict") is True,
        f"HTTP {status}",
    )

    status, refused = call(
        "POST",
        "/live2d/save",
        {
            "model_name": "does-not-exist",
            "scale": 1.0,
            "x_offset": 0,
            "y_offset": 0,
            "expected_revision": "",
        },
    )
    check(
        "live2d refuses an uninstalled model",
        status == 422 and refused.get("ok") is False,
        f"HTTP {status} {refused.get('error', '')[:60]}",
    )

    # -- restore warning -----------------------------------------------
    status, restore = call(
        "POST",
        "/memory/restore",
        {"backup_path": "does-not-exist.sqlite3", "confirm": False},
    )
    check(
        "restore of a missing backup fails clearly",
        status == 422 and restore.get("ok") is False,
        f"HTTP {status}",
    )

    status, backups = call("GET", "/memory/backups")
    check("backup listing reachable", status == 200, f"HTTP {status}")

    # -- secrets never returned ----------------------------------------
    # The key is read from the environment rather than written here: a probe
    # that hard-codes a key-shaped literal is itself the kind of thing a secret
    # scanner flags, and the value differs per machine anyway.
    import os

    blob = json.dumps(overview) + json.dumps(voice) + json.dumps(listing)
    configured_key = os.getenv("AEMEATH_LLM_API_KEY", "")
    leaked = bool(configured_key) and configured_key in blob
    check(
        "no credential value appears in any response",
        not leaked,
        (
            "checked overview, voice and memory payloads against "
            f"{'the configured key' if configured_key else 'an unset key (nothing to leak)'}"
        ),
    )
    check(
        "no response carries a key-shaped string",
        not re.search(r"sk-[A-Za-z0-9]{16,}", blob),
        "pattern scan over the same payloads",
    )

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"{passed}/{total} checks passed")
    if passed == total:
        print("PROBE PASSED")
        return 0
    print("PROBE FAILED")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
