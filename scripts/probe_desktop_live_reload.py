"""Does a save from the REAL management window resize the character window?

This is the check that the original V2-T03 verification was missing.

The earlier acceptance ran `probe_desktop_v2.py`, which writes the config over
HTTP and then launches the app: that proves the character window *reads* the
setting on startup, but says nothing about the live path the user actually
takes — opening the management window, changing a value, pressing 保存, and
seeing the character window change **without a restart**.

That live path needs three things to line up across three processes:

1. the deployed web page (served by the backend) must contain the save code,
   including the IPC notification;
2. the management BrowserWindow's preload must expose that IPC;
3. the main process must re-read the config and re-place the character window.

Any one of them missing leaves "保存 succeeds, nothing happens" — which is
exactly what was reported from a real desktop session.

Usage (backend must be running)::

    python scripts/probe_desktop_live_reload.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "vendor" / "Open-LLM-VTuber-Web" / "release" / "win-unpacked" / "Aemeath.exe"
API = "http://127.0.0.1:12393/aemeath/manage"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check."""
    results.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(method: str, path: str, payload=None):
    """Call one management endpoint."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{API}{path}", data=data, method=method,
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


# --- Win32 window inspection ------------------------------------------------

import ctypes
import ctypes.wintypes as wintypes

user32 = ctypes.windll.user32

APP_WINDOW_CLASSES = {"Chrome_WidgetWin_0", "Chrome_WidgetWin_1"}


class RECT(ctypes.Structure):
    """A Win32 rectangle."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def visible_windows(pids: set[int]) -> list[dict]:
    """Visible top-level application windows for the given processes."""
    found: list[dict] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value not in APP_WINDOW_CLASSES or not user32.IsWindowVisible(hwnd):
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width > 0 and height > 0:
            found.append({
                "hwnd": int(hwnd), "width": width, "height": height,
                "x": rect.left, "y": rect.top,
            })
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def app_pids() -> set[int]:
    """Process ids of the running Aemeath processes."""
    output = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Aemeath.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=False,
    ).stdout
    pids: set[int] = set()
    for line in output.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "aemeath.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def character_window(pids: set[int]) -> dict | None:
    """The character window: the small visible one, not the 1100x760 manager."""
    windows = visible_windows(pids)
    # The management window is 1100x760; the character is configured well below
    # that. Pick the visible app window that is not the manager.
    small = [w for w in windows if w["width"] < 1000 and w["height"] < 800]
    return small[0] if small else None


#: Port used to attach to the running app. The app must be started with
#: ``--remote-debugging-port`` for the page to be reachable.
DEBUG_PORT = 9334


def drive_management_page_save(width: int, height: int) -> bool:
    """Change the character size and press 保存 in the running app's page.

    Opens the management window through the app's own IPC, attaches to the
    page's renderer over CDP, edits the two size fields and clicks 保存.

    Args:
        width: Character window width to set.
        height: Character window height to set.

    Returns:
        ``True`` when the page reported the save as accepted.
    """
    # Ask the main process to open the management window, exactly as the tray
    # menu item does, through the character window's preload bridge.
    if not open_management_window():
        print("  (could not request the management window via IPC)")

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

    if target is None:
        print("  (management page target not found on the debugging port)")
        return False

    import websocket  # type: ignore

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
    counter = {"n": 0}

    def evaluate(expression: str):
        counter["n"] += 1
        message_id = counter["n"]
        ws.send(json.dumps({
            "id": message_id, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            raw = json.loads(ws.recv())
            if raw.get("id") == message_id:
                if "error" in raw:
                    raise RuntimeError(raw["error"])
                return raw.get("result", {}).get("result", {}).get("value")

    try:
        ws.send(json.dumps({"id": 0, "method": "Runtime.enable", "params": {}}))
        # Wait for the page to render.
        deadline = time.time() + 40
        while time.time() < deadline:
            text = evaluate("document.body.innerText || ''") or ""
            if "桌面与字幕" in text:
                break
            time.sleep(0.5)
        else:
            print("  (management page did not render its navigation)")
            return False

        evaluate("""
            (() => {
              const b = Array.from(document.querySelectorAll('button'))
                .find((x) => (x.innerText || '').trim() === '桌面与字幕');
              if (b) b.click();
              return !!b;
            })()
        """)
        time.sleep(1.5)

        # Fill the character window width/height, which are the 4th and 5th
        # inputs on the page (three subtitle fields precede them).
        filled = evaluate(f"""
            (() => {{
              const inputs = Array.from(document.querySelectorAll('input'));
              if (inputs.length < 5) return 'only ' + inputs.length + ' inputs';
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
              const put = (el, v) => {{
                setter.call(el, String(v));
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
              }};
              put(inputs[3], {width});
              put(inputs[4], {height});
              return inputs[3].value + 'x' + inputs[4].value;
            }})()
        """)
        if filled != f"{width}x{height}":
            print(f"  (form fill gave '{filled}')")

        clicked = evaluate("""
            (() => {
              const b = Array.from(document.querySelectorAll('button'))
                .find((x) => (x.innerText || '').trim() === '保存');
              if (b) b.click();
              return !!b;
            })()
        """)
        if not clicked:
            print("  (no 保存 button found)")
            return False

        time.sleep(3.0)
        text = evaluate("document.body.innerText || ''") or ""
        return "已保存" in text
    finally:
        ws.close()


def open_management_window() -> bool:
    """Ask the running app to open its management window.

    Uses the app's own debugging port: the character window's page exposes
    ``window.api.openManagement()``, which is the same call the UI makes.
    """
    try:
        with urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/list", timeout=3) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError):
        return False

    character = None
    for candidate in targets:
        url = candidate.get("url", "")
        if candidate.get("type") == "page" and "page=manage" not in url:
            character = candidate
            break
    if character is None:
        return False

    import websocket  # type: ignore

    ws = websocket.create_connection(character["webSocketDebuggerUrl"], timeout=20)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": "(window.api && window.api.openManagement) ? "
                              "(window.api.openManagement(), 'asked') : 'no api'",
                "returnByValue": True,
            },
        }))
        while True:
            raw = json.loads(ws.recv())
            if raw.get("id") == 1:
                return raw.get("result", {}).get("result", {}).get("value") == "asked"
    except Exception:  # noqa: BLE001 - the caller falls back to a report
        return False
    finally:
        ws.close()


def main() -> int:
    """Run the probe."""
    print("=== V2-T03 live reload: management window → character window ===\n")

    check("build artifact exists", EXE.is_file(), str(EXE))
    if not EXE.is_file():
        print("\nPROBE FAILED")
        return 1

    status, settings = call("GET", "/desktop/settings")
    check("backend reachable", status == 200, f"HTTP {status}")
    if status != 200:
        print("\nPROBE FAILED")
        return 1

    original = dict(settings)
    print(f"当前配置: {original['character_width']}x{original['character_height']}\n")

    # Open the management window the way the user does: through the tray's IPC
    # handler. The character window is a separate BrowserWindow and the save
    # path runs in *that* window's renderer, so an HTTP save from this script
    # would never emit the IPC notification and would prove nothing.
    subprocess.run(["taskkill", "/F", "/IM", "Aemeath.exe"], capture_output=True, check=False)
    time.sleep(2)

    process = subprocess.Popen(
        # The debugging port is how this probe reaches the management page's
        # renderer, which is where the save (and its IPC notification) runs.
        # Chrome/Electron reject CDP connections from an untrusted Origin
        # unless it is explicitly allowed.
        [
            str(EXE),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--remote-allow-origins=http://127.0.0.1:{DEBUG_PORT}",
        ],
        cwd=str(EXE.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the character window to appear.
    window = None
    deadline = time.time() + 45
    while time.time() < deadline:
        if process.poll() is not None:
            break
        window = character_window(app_pids())
        if window:
            break
        time.sleep(1.0)

    check("character window is visible", window is not None,
          f"{window['width']}x{window['height']}" if window else "not found")
    if window is None:
        print("\nPROBE FAILED")
        return 1

    start_size = (window["width"], window["height"])
    check(
        "character window matches the configured size",
        start_size == (original["character_width"], original["character_height"]),
        f"window={start_size[0]}x{start_size[1]}, configured="
        f"{original['character_width']}x{original['character_height']}",
    )

    # Drive the save from the management page inside the running app.
    #
    # This is the only way to exercise the real path: the notification is sent
    # by the page's renderer over the preload bridge, so a save issued from this
    # script over HTTP would leave the character window untouched and the probe
    # would report a failure that does not exist.
    new_width = 540 if start_size[0] != 540 else 480
    new_height = 720 if start_size[1] != 720 else 660

    saved_ok = drive_management_page_save(new_width, new_height)
    check("save issued from the management page", saved_ok,
          f"page saved {new_width}x{new_height}")

    # Now the point of the probe: does the RUNNING character window adopt it?
    # Without the live path it keeps its old size until restarted.
    adopted = False
    deadline = time.time() + 25
    while time.time() < deadline:
        window = character_window(app_pids())
        if window and (window["width"], window["height"]) == (new_width, new_height):
            adopted = True
            break
        time.sleep(1.0)

    final = character_window(app_pids())
    check(
        "running character window adopts the saved size without a restart",
        adopted,
        f"window is now {final['width']}x{final['height']}" if final
        else "no window",
    )

    # Restore through the same page path, so the probe leaves the app consistent.
    call("GET", "/desktop/settings")
    restore_ok = drive_management_page_save(
        original["character_width"], original["character_height"],
    )
    time.sleep(2)
    restored = call("GET", "/desktop/settings")[1]
    check(
        "original configuration restored",
        restored["character_width"] == original["character_width"]
        and restored["character_height"] == original["character_height"],
        f"{restored['character_width']}x{restored['character_height']} "
        f"(page save {'ok' if restore_ok else 'failed'})",
    )

    subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                   capture_output=True, check=False)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    if passed == len(results):
        print("PROBE PASSED")
        return 0
    print("PROBE FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
