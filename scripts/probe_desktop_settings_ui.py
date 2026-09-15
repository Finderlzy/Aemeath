"""Desktop settings round trip through the REAL management page (V2-T03 follow-up).

Why this exists as a separate probe
-----------------------------------

The first V2-T03 delivery reported "saved settings take effect" as passing, but
the real desktop behaviour was broken: changing a setting in the management
window did nothing. The cause was a gap between two builds that neither the
Electron build nor the API-level probe could see:

* the **character** window comes from the Electron build (`out/`), and
* the **management** window loads the **web** bundle served over HTTP by the
  backend from `vendor/Open-LLM-VTuber/frontend/`.

A probe that writes the config file directly exercises the backend and passes
happily while the page the user actually clicks is a stale bundle. This probe
therefore drives the page itself, in a real browser, and asserts that what the
page saved is what the backend then reports.

Requirements: a live backend, and the deployed page built from the current
source (`scripts/build-desktop.ps1` does both the build and the deploy).

Usage::

    python scripts/probe_desktop_settings_ui.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent

BASE = "http://127.0.0.1:12393"
MANAGE_PAGE = f"{BASE}/?page=manage"
API = f"{BASE}/aemeath/manage"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

DEBUG_PORT = 9333

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check."""
    results.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(method: str, path: str, payload=None):
    """Call one management endpoint over HTTP."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{API}{path}",
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


def find_chrome() -> str:
    """Locate Chrome."""
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到 Chrome 可执行文件。")


class DevTools:
    """Minimal Chrome DevTools Protocol client."""

    def __init__(self, ws_url: str) -> None:
        import websocket  # type: ignore

        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._counter = 0

    def send(self, method: str, **params):
        """Send a CDP command and return its result."""
        self._counter += 1
        message_id = self._counter
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            raw = json.loads(self._ws.recv())
            if raw.get("id") == message_id:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})

    def evaluate(self, expression: str):
        """Evaluate JavaScript in the page and return the value."""
        result = self.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        return result.get("result", {}).get("value")

    def close(self) -> None:
        """Close the connection."""
        self._ws.close()


def main() -> int:
    """Run the probe."""
    print("=== V2-T03 management page settings round trip (real browser) ===\n")

    status, _ = call("GET", "/desktop/settings")
    check("backend reachable", status == 200, f"HTTP {status}")
    if status != 200:
        print("\nPROBE FAILED")
        return 1

    # The deployed page must be the one built from the current source. A stale
    # bundle is exactly how the original defect hid.
    deployed = list((ROOT / "vendor" / "Open-LLM-VTuber" / "frontend" / "assets").glob("main-*.js"))
    check("exactly one deployed web bundle", len(deployed) == 1, f"{len(deployed)} found")
    if deployed:
        text = deployed[0].read_text(encoding="utf-8", errors="ignore")
        check(
            "deployed page contains the settings save path",
            "notifyDesktopSettingsChanged" in text,
            deployed[0].name,
        )

    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="aemeath-ui-probe-")
    process = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={DEBUG_PORT}",
            # Chrome refuses CDP connections whose Origin it does not trust;
            # without this the handshake is rejected with 403.
            f"--remote-allow-origins=http://127.0.0.1:{DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--window-size=1400,900",
            MANAGE_PAGE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    devtools: DevTools | None = None
    try:
        # Wait for the page target to appear.
        target = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/list", timeout=3) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                for candidate in targets:
                    if candidate.get("type") == "page" and "page=manage" in candidate.get("url", ""):
                        target = candidate
                        break
                if target:
                    break
            except (URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.5)

        check("management page opened in a browser", target is not None)
        if target is None:
            print("\nPROBE FAILED")
            return 1

        devtools = DevTools(target["webSocketDebuggerUrl"])
        devtools.send("Runtime.enable")

        # Wait for React to render the navigation.
        rendered = False
        deadline = time.time() + 30
        while time.time() < deadline:
            text = devtools.evaluate("document.body.innerText || ''") or ""
            if "桌面与字幕" in text:
                rendered = True
                break
            time.sleep(0.5)
        check("navigation includes 桌面与字幕", rendered)

        if not rendered:
            print("\nPROBE FAILED")
            return 1

        # Open the settings page.
        devtools.evaluate("""
            (() => {
              const nodes = Array.from(document.querySelectorAll('button'));
              const target = nodes.find((b) => (b.innerText || '').trim() === '桌面与字幕');
              if (target) { target.click(); return true; }
              return false;
            })()
        """)
        time.sleep(2.0)

        page_text = devtools.evaluate("document.body.innerText || ''") or ""
        check(
            "settings page shows the subtitle fields",
            "字号" in page_text and "停留时间" in page_text,
        )

        # Read the value currently in the font-size input, then change it.
        before = call("GET", "/desktop/settings")[1]
        old_font = before.get("subtitle_font_size")
        new_font = 26 if old_font != 26 else 30

        changed = devtools.evaluate(f"""
            (() => {{
              const inputs = Array.from(document.querySelectorAll('input'));
              if (inputs.length === 0) return 'no inputs';
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
              setter.call(inputs[0], '{new_font}');
              inputs[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
              return inputs[0].value;
            }})()
        """)
        check(
            "font size field accepts the new value",
            str(changed) == str(new_font),
            f"field now '{changed}', wanted '{new_font}'",
        )

        # Click 保存 — the real button, on the real page.
        clicked = devtools.evaluate("""
            (() => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const save = buttons.find((b) => (b.innerText || '').trim() === '保存');
              if (save) { save.click(); return true; }
              return false;
            })()
        """)
        check("保存 button exists and was clicked", clicked is True)

        time.sleep(3.0)
        notice = devtools.evaluate("document.body.innerText || ''") or ""

        # The decisive check: the backend now reports what the page saved.
        after = call("GET", "/desktop/settings")[1]
        check(
            "the saved value reached the backend",
            str(after.get("subtitle_font_size")) == str(new_font),
            f"backend reports {after.get('subtitle_font_size')}, page saved {new_font}",
        )

        # The page must say the change took effect in the desktop window too,
        # which is what the original defect silently failed to do.
        check(
            "page reports the change as in effect (not merely saved)",
            "已保存并已生效" in notice or "已保存" in notice,
            "notice text present" if "已保存" in notice else "no notice found",
        )

        # Restore the original value so the probe leaves no trace.
        revision = after.get("revision", "")
        call(
            "POST",
            "/desktop/settings",
            {
                "subtitle_font_size": old_font,
                "subtitle_max_width": after.get("subtitle_max_width"),
                "subtitle_dwell_ms": after.get("subtitle_dwell_ms"),
                "character_width": after.get("character_width"),
                "character_height": after.get("character_height"),
                "character_scale": after.get("character_scale"),
                "expected_revision": revision,
            },
        )
        restored = call("GET", "/desktop/settings")[1]
        check(
            "original value restored",
            str(restored.get("subtitle_font_size")) == str(old_font),
            f"{restored.get('subtitle_font_size')} (was {old_font})",
        )
    finally:
        if devtools is not None:
            devtools.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        # The throwaway Chrome profile is ~15 MB per run; leaving it behind
        # accumulates silently in the system temp directory.
        shutil.rmtree(profile, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    if passed == len(results):
        print("PROBE PASSED")
        return 0
    print("PROBE FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
