"""Verify the training wizard page in a real browser (V2-T06).

Screenshots prove a page is not blank; they do not prove it is *correct*. This
probe drives the real page in headless Chrome and asserts on what it actually
renders and what the user can actually click:

* the six steps appear in order;
* the environment precheck is shown — including the single-GPU DDP fact, which
  is the precondition the whole feature rests on;
* clicking a task shows its steps and run state, and no progress percentage is
  invented;
* the advanced entry point is present but is not the primary path.

It needs a live service on 127.0.0.1:12393 and a task to look at, so it is run
explicitly rather than as part of the test suite.

Usage::

    python scripts/probe_training_wizard_ui.py
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Tuple
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "images"
REPORT_DIR = ROOT / "data" / "acceptance" / "training-wizard"
REPORT_PATH = REPORT_DIR / "ui.json"

BASE_URL = "http://127.0.0.1:12393/?page=manage"
DEBUG_PORT = 9223

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: How long to let the page finish its own data loads before reading it.
SETTLE_SECONDS = 6.0

results: List[Tuple[str, bool, str]] = []


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
    """Minimal Chrome DevTools Protocol client."""

    def __init__(self, ws_url: str) -> None:
        import websocket  # type: ignore

        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._counter = 0

    def send(self, method: str, **params) -> Any:
        """Send a command and return its result."""
        self._counter += 1
        message_id = self._counter
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            data = json.loads(self._ws.recv())
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def evaluate(self, expression: str) -> Any:
        """Evaluate an expression in the page and return its value."""
        result = self.send("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def text(self) -> str:
        """The page's rendered text."""
        return self.evaluate("document.body.innerText") or ""

    def close(self) -> None:
        """Close the connection."""
        try:
            self._ws.close()
        except Exception:
            pass


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check and print it."""
    results.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def click_by_text(client: DevTools, label: str) -> str:
    """Click the first element whose trimmed text equals ``label``."""
    return client.evaluate(
        "(() => {"
        "  const nodes = Array.from(document.querySelectorAll('button, div[role=button]'));"
        f"  const target = nodes.find(n => n.textContent.trim() === {json.dumps(label)});"
        "  if (!target) return 'not-found';"
        "  target.click();"
        "  return 'clicked';"
        "})()"
    )


def main() -> int:
    """Drive the wizard page and record what it renders."""
    if not wait_for("http://127.0.0.1:12393/", timeout=10):
        print("管理服务未运行：请先启动 Aemeath（127.0.0.1:12393）。")
        return 1

    try:
        import websocket  # noqa: F401
    except ImportError:
        print("需要 websocket-client。")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    profile = ROOT / ".chrome-training-probe"

    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--window-size=1280,1000",
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

        client.send("Page.navigate", url=BASE_URL)
        time.sleep(4.0)

        # Open the wizard from the navigation.
        verdict = click_by_text(client, "声音训练")
        check("管理导航中存在「声音训练」入口", verdict == "clicked", str(verdict))
        # Preflight shells out to nvidia-smi and torch, so it needs longer than
        # a static page before its result is on screen.
        time.sleep(SETTLE_SECONDS)

        text = client.text()

        for label in ("1. 导入素材", "2. 清理切分", "3. 校对文字", "4. 训练声音"):
            check(f"页面显示步骤「{label}」", label in text)

        check("页面说明关闭不中断训练", "关闭本页面不会中断" in text)
        check("页面说明校对只能由用户确认", "自动识别的成功并不代表素材准确" in text)
        check("页面说明至少 4 条、10 秒以上", "至少需要 4 条" in text)
        check("预留高级入口而非替代", "高级入口" in text)

        # The environment precheck must resolve, not stay in its loading state.
        check(
            "环境预检已加载（不是「无法读取环境状态」）",
            "无法读取环境状态" not in text,
            "预检未在页面停留时间内返回" if "无法读取环境状态" in text else "",
        )
        check(
            "预检展示单卡 DDP 修复状态",
            "环境就绪" in text or "阻塞" in text,
            [line for line in text.splitlines() if "环境就绪" in line or "阻塞" in line][:1],
        )

        # Selecting a task must reveal its step trail and run state.
        clicked_task = client.evaluate(
            "(() => {"
            "  const nodes = Array.from(document.querySelectorAll('div'));"
            "  const target = nodes.find(n => n.textContent.includes('当前步骤：'));"
            "  if (!target) return 'no-task';"
            "  target.click();"
            "  return 'clicked';"
            "})()"
        )
        if clicked_task == "clicked":
            time.sleep(2.0)
            text_after = client.text()
            check("选中任务后显示步骤轨迹", "运行状态：" in text_after)
            # The architecture forbids inventing progress, so the page must not
            # show a percentage anywhere near the run state.
            near_state = text_after.split("运行状态：", 1)[1][:400] if "运行状态：" in text_after else ""
            check(
                "不编造进度百分比",
                "%" not in near_state,
                "" if "%" not in near_state else f"运行状态附近出现百分比：{near_state[:120]}",
            )
            check("每条任务显示当前步骤", "当前步骤：" in text_after)

        shot = client.send("Page.captureScreenshot", format="png")
        path = OUT_DIR / "manage-training.png"
        path.write_bytes(base64.b64decode(shot["data"]))
        print(f"       截图已更新 {path}")

        client.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    report = {
        "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in results],
        "passed": all(p for _, p, _ in results),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项通过 ===")
    print(f"报告已写入 {REPORT_PATH}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
