# -*- coding: utf-8 -*-
"""Live screen-understanding acceptance against production components.

Uses the real WindowsCaptureBackend, the real ScreenObserver and the real
OpenAICompatibleVision adapter (EasyCLIProxyAPI + gemini-3.8-flash-high).
Nothing is stubbed except timing gates, which are bypassed with force=True
so the script does not have to wait between observations.

Run from the Aemeath root with the acceptance venv and AEMEATH_VISION_API_KEY set:

    vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_screen_live.py

The user is expected to keep one non-sensitive window in the foreground
between prompts (the script pauses and says which round is next).
"""
import asyncio
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aemeath.adapters import OpenAICompatibleVision
from aemeath.screen import ScreenObserver, WindowsCaptureBackend

DATA_DIR = os.path.join("data", "acceptance", "screen-live")
RUN_TAG = time.strftime("%Y%m%d-%H%M%S")


async def observe_round(observer: ScreenObserver, round_id: str, note: str) -> dict:
    print(f"\n=== {round_id} ({note}) ===")
    result = await observer.observe(force=True)
    if result is None:
        print("  -> observation returned None (capture failed or locked)")
        return {"round_id": round_id, "note": note, "ok": False}
    print(f"  window : {result.window_title}")
    print(f"  obs id : {result.observation_id}")
    print(f"  summary: {result.summary}")
    return {
        "round_id": round_id,
        "note": note,
        "ok": True,
        "window_title": result.window_title,
        "observation_id": result.observation_id,
        "summary": result.summary,
    }


async def main() -> int:
    api_key = os.environ.get("AEMEATH_VISION_API_KEY", "")
    if not api_key:
        print("AEMEATH_VISION_API_KEY is not set")
        return 1

    vision = OpenAICompatibleVision(
        base_url="http://127.0.0.1:8317/v1",
        api_key=api_key,
        model="gemini-3.8-flash-high",
    )
    backend = WindowsCaptureBackend()
    observer = ScreenObserver(backend, vision, min_stable_seconds=0)

    rounds = []

    def pause(msg: str) -> None:
        input(f"\n>>> {msg} 然后按回车继续...")

    pause("把【网页浏览器】放到前台")
    rounds.append(await observe_round(observer, f"{RUN_TAG}-web-1", "网页第1次"))
    rounds.append(await observe_round(observer, f"{RUN_TAG}-web-2", "网页第2次"))

    pause("把【编辑器或 IDE】放到前台")
    rounds.append(await observe_round(observer, f"{RUN_TAG}-editor-1", "编辑器第1次"))
    rounds.append(await observe_round(observer, f"{RUN_TAG}-editor-2", "编辑器第2次"))

    pause("把【文档查看器（PDF/Word/记事本等）】放到前台")
    rounds.append(await observe_round(observer, f"{RUN_TAG}-doc-1", "文档第1次"))
    rounds.append(await observe_round(observer, f"{RUN_TAG}-doc-2", "文档第2次"))

    # Window-switch check: after switching windows, the summary must describe
    # the new window, not the previous one.
    pause("再切换回【网页浏览器】（验证切换后不串画面）")
    rounds.append(await observe_round(observer, f"{RUN_TAG}-switch", "切换窗口后重新观察"))

    # Stale-summary rejection: reset() must clear the cached summary.
    observer.reset()
    stale = observer.current_summary()
    rounds.append(
        {
            "round_id": f"{RUN_TAG}-reset",
            "note": "关闭观察后 current_summary 应为空（迟到摘要被拒）",
            "ok": stale is None,
        }
    )
    print(f"\nreset 后 current_summary: {stale}")

    # Disk check: no observation images should be written anywhere under the
    # acceptance data dir by the observer (only this script's report JSON).
    leaks = []
    for root, _dirs, files in os.walk(DATA_DIR):
        for name in files:
            if name.endswith(".png"):
                leaks.append(os.path.join(root, name))
    rounds.append(
        {
            "round_id": f"{RUN_TAG}-disk",
            "note": "截图不落盘",
            "ok": not leaks,
        }
    )
    print(f"落盘 PNG 检查: {leaks if leaks else '无（通过）'}")

    os.makedirs(DATA_DIR, exist_ok=True)
    report_path = os.path.join(DATA_DIR, f"report-{RUN_TAG}.json")
    import json

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(rounds, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入 {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
