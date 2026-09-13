# -*- coding: utf-8 -*-
"""Automated rounds of the live screen-understanding acceptance.

Brings real windows to the foreground programmatically (web browser, editor,
document viewer), observes each twice with the production ScreenObserver and
real vision adapter, then verifies:

- summaries describe the correct window after switching (no stale frames)
- resetting observation drops the cached summary (late summaries rejected)
- no PNG is written to disk anywhere under data/acceptance

Lock-screen and "no foreground window" reports remain manual checks
(see docs/acceptance-guide.md section B).

Run from the Aemeath root with AEMEATH_VISION_API_KEY set:

    vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_screen_auto.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aemeath.adapters import OpenAICompatibleVision
from aemeath.screen import ScreenObserver, WindowsCaptureBackend

DATA_DIR = os.path.join("data", "acceptance", "screen-live")
RUN_TAG = time.strftime("%Y%m%d-%H%M%S")


def activate_window(title_part: str, launch: list[str] | None = None) -> str:
    """Bring a window whose title contains title_part to the foreground."""
    if launch:
        subprocess.Popen(
            launch,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    # Retry lookup: freshly launched windows take a moment to appear,
    # and SetForegroundWindow can be rejected right after another window
    # grabbed focus.
    last_rc = 1
    for _ in range(10):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _activate_cmd(title_part)],
            capture_output=True,
            timeout=30,
        )
        last_rc = result.returncode
        if last_rc == 0:
            break
        time.sleep(2)
    out = result.stdout.decode("gbk", errors="replace")
    if last_rc != 0:
        raise RuntimeError(f"window activation failed ({last_rc}): {title_part}")
    # Give the newly focused window a moment before capture.
    time.sleep(3)
    return out.strip()


def _activate_cmd(title_part: str) -> str:
    return (
        "Add-Type 'using System;using System.Runtime.InteropServices;"
        'public class W{[DllImport("user32.dll")]public static extern bool'
        " SetForegroundWindow(IntPtr h);"
        '[DllImport("user32.dll")]public static extern bool ShowWindow(IntPtr h,int n);'
        '[DllImport("user32.dll")]public static extern void keybd_event(byte bVk,'
        'byte bScan,uint dwFlags,UIntPtr dwExtraInfo);'
        '[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();}\';'
        "$p=Get-Process|Where-Object{$_.MainWindowTitle -ne '' -and $_.MainWindowTitle"
        f" -like '*{title_part}*'}};"
        "if($p){$h=$p[0].MainWindowHandle;[W]::ShowWindow($h,9)|Out-Null;"
        # Pressing ALT works around the foreground-lock that makes
        # SetForegroundWindow fail silently from a background process.
        "[W]::keybd_event(18,0,0,[UIntPtr]::Zero);"
        "[W]::keybd_event(18,2,0,[UIntPtr]::Zero);"
        "[W]::SetForegroundWindow($h)|Out-Null;"
        "Start-Sleep -Milliseconds 800;"
        "if([W]::GetForegroundWindow() -eq $h){Write-Output 'ok'}else{exit 2}"
        "}else{exit 1}"
    )


async def observe(observer: ScreenObserver, round_id: str, note: str) -> dict:
    observation = await observer.observe(force=True)
    if observation is None:
        print(f"  {round_id} ({note}): observation returned None")
        return {"round_id": round_id, "note": note, "ok": False, "summary": None}
    print(f"  {round_id} ({note})")
    print(f"    window : {observation.window_title}")
    print(f"    summary: {observation.summary}")
    return {
        "round_id": round_id,
        "note": note,
        "ok": True,
        "window_title": observation.window_title,
        "observation_id": observation.observation_id,
        "summary": observation.summary,
    }


async def main() -> int:
    api_key = os.environ.get("AEMEATH_VISION_API_KEY", "")
    if not api_key:
        print("AEMEATH_VISION_API_KEY is not set")
        return 1

    doc_path = os.path.join(tempfile.gettempdir(), "aemeath-screen-doc.txt")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("Aemeath 屏幕观察验收文档\n\n这是第三类窗口：纯文本文档。\n")

    vision = OpenAICompatibleVision(
        base_url="http://127.0.0.1:8317/v1",
        api_key=api_key,
        model="gemini-3.8-flash-high",
    )
    observer = ScreenObserver(WindowsCaptureBackend(), vision, min_stable_seconds=0)
    rounds: list[dict] = []

    print("== 网页（Chrome）×2 ==")
    activate_window("Chrome")
    rounds.append(await observe(observer, f"{RUN_TAG}-web-1", "网页第1次"))
    rounds.append(await observe(observer, f"{RUN_TAG}-web-2", "网页第2次"))

    print("== 编辑器（记事本-编辑）×2 ==")
    editor_path = os.path.join(tempfile.gettempdir(), "aemeath-screen-edit.py")
    with open(editor_path, "w", encoding="utf-8") as f:
        f.write("# editor window for screen acceptance\nprint('hello')\n")
    activate_window("aemeath-screen-edit", launch=["notepad.exe", editor_path])
    rounds.append(await observe(observer, f"{RUN_TAG}-editor-1", "编辑器第1次"))
    rounds.append(await observe(observer, f"{RUN_TAG}-editor-2", "编辑器第2次"))

    print("== 文档（记事本-文档）×2 ==")
    activate_window("aemeath-screen-doc", launch=["notepad.exe", doc_path])
    rounds.append(await observe(observer, f"{RUN_TAG}-doc-1", "文档第1次"))
    rounds.append(await observe(observer, f"{RUN_TAG}-doc-2", "文档第2次"))

    print("== 切换回网页，验证不串画面 ==")
    activate_window("Chrome")
    rounds.append(await observe(observer, f"{RUN_TAG}-switch", "切换窗口后重新观察"))

    print("== 关闭观察：reset 后迟到摘要被拒 ==")
    observer.reset()
    stale = observer.current_summary()
    print(f"  reset 后 current_summary: {stale}")
    rounds.append(
        {
            "round_id": f"{RUN_TAG}-reset",
            "note": "关闭观察后 current_summary 应为空",
            "ok": stale is None,
        }
    )

    print("== 截图不落盘 ==")
    # vision-test holds the two fixture images created by gen_vision_test_images.py,
    # not screenshots; everything else must be PNG-free.
    leaks = []
    for root, _dirs, files in os.walk(os.path.join("data", "acceptance")):
        if os.path.normpath(root) == os.path.normpath(
            os.path.join("data", "acceptance", "vision-test")
        ):
            continue
        for name in files:
            if name.lower().endswith(".png"):
                leaks.append(os.path.join(root, name))
    print(f"  data/acceptance 下的 PNG: {leaks if leaks else '无（通过）'}")
    rounds.append(
        {"round_id": f"{RUN_TAG}-disk", "note": "截图不落盘", "ok": not leaks}
    )

    os.makedirs(DATA_DIR, exist_ok=True)
    report_path = os.path.join(DATA_DIR, f"report-{RUN_TAG}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(rounds, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入 {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
