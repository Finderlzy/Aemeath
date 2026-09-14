"""Capture the Aemeath management pages with headless Chrome.

The management window is a real page served by the running service, so the
screenshots are taken from a real Chrome rather than from a rendering
approximation. Requires a live service on 127.0.0.1:12393.

Usage::

    python scripts/capture_manage_pages.py

Writes ``docs/images/manage-<page>.png`` for each page.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "images"

BASE_URL = "http://127.0.0.1:12393/?page=manage"
DEBUG_PORT = 9222

#: Chrome install locations to try, in order.
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: Pages to capture: (nav label, output file stem).
PAGES = (
    ("概览", "overview"),
    ("模型", "model"),
    ("人设", "persona"),
    ("声音", "voice"),
    ("记忆", "memory"),
    ("Live2D", "live2d"),
)


def find_chrome() -> str:
    """Locate the Chrome executable."""
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到 Chrome 可执行文件。")


def wait_for(url: str, timeout: float = 20.0) -> bool:
    """Wait until a URL answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (URLError, OSError):
            time.sleep(0.5)
    return False


class DevTools:
    """Minimal Chrome DevTools Protocol client over the browser WebSocket."""

    def __init__(self, ws_url: str) -> None:
        import websocket  # type: ignore

        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._counter = 0

    def send(self, method: str, **params):
        """Send a command and return its result."""
        self._counter += 1
        message_id = self._counter
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def close(self) -> None:
        """Close the connection."""
        try:
            self._ws.close()
        except Exception:
            pass


def main() -> int:
    """Launch Chrome, walk the pages and save screenshots."""
    if not wait_for("http://127.0.0.1:12393/", timeout=10):
        print("管理服务未运行：请先启动 Aemeath（127.0.0.1:12393）。")
        return 1

    try:
        import websocket  # noqa: F401
    except ImportError:
        print("需要 websocket-client：")
        print("  .\\vendor\\Open-LLM-VTuber\\.venv\\Scripts\\python.exe -m pip install websocket-client")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    profile = ROOT / ".chrome-manage-shot"

    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={DEBUG_PORT}",
            # Chrome refuses the DevTools WebSocket unless the connecting
            # origin is allowed; without this the handshake returns 403.
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--window-size=1280,900",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not wait_for(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=25):
            print("Chrome 调试端口未就绪。")
            return 1

        with urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json") as response:
            targets = json.loads(response.read().decode("utf-8"))
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            print("找不到可用的页面目标。")
            return 1

        client = DevTools(page["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")

        for label, stem in PAGES:
            client.send("Page.navigate", url=BASE_URL)
            time.sleep(2.5)

            # Click the nav button for this page by its visible label.
            clicked = client.send(
                "Runtime.evaluate",
                expression=(
                    "(() => {"
                    "  const buttons = Array.from(document.querySelectorAll('button'));"
                    f"  const target = buttons.find(b => b.textContent.trim() === {json.dumps(label)});"
                    "  if (!target) return 'not-found';"
                    "  target.click();"
                    "  return 'clicked';"
                    "})()"
                ),
                returnByValue=True,
            )
            verdict = clicked.get("result", {}).get("value")
            time.sleep(1.2)

            shot = client.send("Page.captureScreenshot", format="png")
            path = OUT_DIR / f"manage-{stem}.png"
            path.write_bytes(base64.b64decode(shot["data"]))

            # Record what the page actually rendered. A screenshot of a blank or
            # broken page is still a valid PNG, so the capture is only useful if
            # the expected content was really on screen.
            content = client.send(
                "Runtime.evaluate",
                expression="document.body.innerText.slice(0, 200)",
                returnByValue=True,
            )
            text = content.get("result", {}).get("value", "") or ""
            ok = verdict == "clicked" and bool(text.strip())
            reason = (
                "rendered"
                if ok
                else ("nav button not found" if verdict != "clicked" else "page was empty")
            )
            print(f"[{'OK' if ok else 'WARN'}] {label} -> {path.name} ({reason})")
            print(f"       {text.strip().splitlines()[0][:70] if text.strip() else '(no text)'}")

        client.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    print("\n截图完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
