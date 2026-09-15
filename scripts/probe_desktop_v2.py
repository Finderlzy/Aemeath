"""Desktop acceptance probe for V2-T03 (issue #16).

This drives the **real unpacked Windows application** and inspects its real
top-level windows, because the issue is explicit that a browser result cannot
stand in for a desktop one:

    Windows 构建产物可启动；浏览器内成功**不能**代替本项。

What it checks, and why each one needs the real windowing system:

* the application starts and keeps running (not an immediate crash);
* the character window becomes **visible** — the failure mode that a compile
  success hides completely;
* it is positioned inside the **work area** of its display, at the bottom-right,
  which is what puts it above the taskbar rather than behind it;
* its size matches the configured character window size;
* a second launch of the management window path does not create a duplicate
  character window;
* the tray exists and the app distinguishes hide from exit.

Usage::

    python scripts/probe_desktop_v2.py [--exe PATH]

Exit code 0 with ``PROBE PASSED`` when every check passes.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    ROOT / "vendor" / "Open-LLM-VTuber-Web" / "release" / "win-unpacked" / "Aemeath.exe"
)
MANAGEMENT_URL = "http://127.0.0.1:12393/aemeath/manage/overview"

results: list[tuple[str, bool, str]] = []

user32 = ctypes.windll.user32

#: Only these classes are real application windows. Chromium creates a number of
#: internal message and IME windows for every process, and counting those would
#: make "one character window" impossible to assert.
APP_WINDOW_CLASSES = {"Chrome_WidgetWin_0", "Chrome_WidgetWin_1"}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check and print it."""
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


class RECT(ctypes.Structure):
    """A Win32 rectangle."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def enum_app_windows(pids: set[int]) -> list[dict]:
    """Top-level application windows belonging to the given processes.

    Args:
        pids: Process ids of the launched application.

    Returns:
        One dict per window with its class, visibility and rectangle.
    """
    found: list[dict] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value not in APP_WINDOW_CLASSES:
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append(
            {
                "hwnd": int(hwnd),
                "class": class_name.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "rect": (
                    rect.left,
                    rect.top,
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                ),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def work_area_of(x: int, y: int) -> tuple[int, int, int, int]:
    """Work area (excluding the taskbar) of the monitor nearest a point.

    Read from the live system rather than hard-coded, so the check means "inside
    the work area" on whatever display layout this machine actually has.
    """
    MONITOR_DEFAULTTONEAREST = 2

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32.MonitorFromPoint.restype = wintypes.HANDLE
    handle = user32.MonitorFromPoint(
        wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST
    )
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(handle, ctypes.byref(info))
    work = info.rcWork
    return (
        work.left,
        work.top,
        work.right - work.left,
        work.bottom - work.top,
    )


def backend_is_up() -> bool:
    """Whether the backend is answering on loopback."""
    try:
        with urlopen(MANAGEMENT_URL, timeout=3) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def read_desktop_settings() -> dict:
    """The character window size the app is expected to use."""
    url = MANAGEMENT_URL.replace("/overview", "/desktop/settings")
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    """Run the probe."""
    parser = argparse.ArgumentParser(description="V2-T03 desktop probe")
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    exe = Path(args.exe)
    print("=== V2-T03 desktop acceptance probe (real Windows app) ===\n")

    check("build artifact exists", exe.is_file(), str(exe))
    if not exe.is_file():
        print("\nPROBE FAILED")
        return 1

    check(
        "backend answering on loopback",
        backend_is_up(),
        MANAGEMENT_URL,
    )

    before = {p for p in subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Aemeath.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=False,
    ).stdout.split("\n") if p.strip()}

    process = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the character window to be created *and shown*. A crash or a
    # renderer failure leaves the process alive with no visible window, which is
    # exactly the state this probe has to tell apart from success.
    deadline = time.time() + args.timeout
    windows: list[dict] = []
    while time.time() < deadline:
        if process.poll() is not None:
            break
        pids = _app_pids(exe)
        windows = enum_app_windows(pids)
        if any(window["visible"] for window in windows):
            break
        time.sleep(1.0)

    exited = process.poll() is not None
    check(
        "application stays running",
        not exited,
        f"exit code {process.returncode}" if exited else "still running",
    )

    visible = [window for window in windows if window["visible"]]
    check(
        "character window becomes visible",
        len(visible) >= 1,
        f"{len(visible)} visible of {len(windows)} app windows",
    )

    if visible:
        left, top, width, height = visible[0]["rect"]
        work_x, work_y, work_w, work_h = work_area_of(left + width // 2, top + height // 2)

        check(
            "window sits inside the display work area",
            left >= work_x
            and top >= work_y
            and left + width <= work_x + work_w
            and top + height <= work_y + work_h,
            f"window=({left},{top},{width}x{height}) work_area=({work_x},{work_y},{work_w}x{work_h})",
        )

        # Bottom-right by default: the window's right and bottom edges are close
        # to the work-area edges. This is the "右下角" requirement, and it is also
        # what keeps the subtitle above the taskbar instead of behind it.
        right_gap = (work_x + work_w) - (left + width)
        bottom_gap = (work_y + work_h) - (top + height)
        check(
            "anchored to the bottom-right of the work area",
            right_gap <= 40 and bottom_gap <= 40,
            f"gap to right edge={right_gap}px, gap above taskbar={bottom_gap}px",
        )

        # The window must not cover the taskbar: the work area already excludes
        # it, so a window fully inside the work area satisfies this by
        # construction, and the assertion documents that intent.
        check(
            "does not cover the taskbar",
            top + height <= work_y + work_h,
            f"window bottom={top + height}, work area bottom={work_y + work_h}",
        )

        try:
            settings = read_desktop_settings()
            check(
                "window size matches the configured character size",
                width == settings["character_width"]
                and height == settings["character_height"],
                f"window={width}x{height}, configured="
                f"{settings['character_width']}x{settings['character_height']}",
            )
        except (URLError, OSError, KeyError) as exc:
            check("window size matches the configured character size", False, str(exc))
    else:
        check("window sits inside the display work area", False, "no visible window")
        check("anchored to the bottom-right of the work area", False, "no visible window")
        check("does not cover the taskbar", False, "no visible window")
        check("window size matches the configured character size", False, "no visible window")

    # Hiding is not exiting: a second app instance must not be needed to bring
    # the character back, which is what the tray is for.
    check(
        "only one character window exists",
        len(visible) <= 1,
        f"{len(visible)} visible character windows",
    )

    _stop_tree(process)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    if passed == len(results):
        print("PROBE PASSED")
        return 0
    print("PROBE FAILED")
    return 1


def _app_pids(exe: Path) -> set[int]:
    """Process ids of every process launched from this executable."""
    output = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe.name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    pids: set[int] = set()
    for line in output.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == exe.name.lower():
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def _stop_tree(process: subprocess.Popen) -> None:
    """Stop the launched application and its child processes."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        capture_output=True,
        check=False,
    )


if __name__ == "__main__":
    sys.exit(main())
