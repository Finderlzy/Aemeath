"""End-to-end verification of the training wizard through the real server.

The other probe (``probe_training_wizard.py``) drives the service in-process,
which is fast and good for iterating. This one goes over HTTP against a running
Aemeath server, which is the only way to exercise two things that cannot be
reproduced in-process:

* the **apply** step, because ``VoiceService.apply()`` validates the candidate
  config through upstream's own schema (``src.open_llm_vtuber.config_manager``),
  and that module only resolves inside the server process — by design, not by
  accident;
* the mounted routes themselves, including the loopback guard.

It also demonstrates the property the acceptance criteria care about most:
training is tracked by the *server*, so it continues after the client that
started it goes away.

Usage::

    python scripts/probe_training_wizard_http.py --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT_DIR = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:12393/aemeath/manage"
REPORT_DIR = ROOT_DIR / "data" / "acceptance" / "training-wizard"
REPORT_PATH = REPORT_DIR / "http-flow.json"

MATERIAL_DIR = ROOT_DIR / "data" / "extracted_clean_voices"

results: List[Tuple[str, bool, str]] = []


def call(method: str, path: str, payload: Any = None, *, timeout: float = 120.0):
    """Call one management endpoint.

    Returns:
        ``(status, parsed body)``; status ``0`` means the service was unreachable.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
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
    """Record one check and print it."""
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def wait_for_state(job_id: str, wanted: set, *, timeout: float = 3600.0) -> Dict[str, Any]:
    """Poll a job until it reaches one of the wanted states."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status, body = call("GET", f"/training/{job_id}")
        job = body.get("job") or {}
        state = job.get("state", "")
        if state != last:
            print(f"    -> {state}: {job.get('detail', '')}", flush=True)
            last = state
        if state in wanted:
            return job
        time.sleep(3.0)
    return job if isinstance(job, dict) else {}


def run_flow(*, epochs: int) -> int:
    """Run the six steps over HTTP and record the outcome."""
    print("=== 声音训练向导 HTTP 端到端 ===\n")

    status, overview = call("GET", "/training/overview")
    check("训练任务接口可达", status == 200, f"HTTP {status}")

    status, preflight = call("GET", "/training/preflight")
    check("环境预检可达", status == 200, f"HTTP {status}")
    check(
        "DDP 修复已生效（训练前置条件）",
        bool(preflight.get("ddp", {}).get("patched")),
        preflight.get("ddp", {}).get("error", ""),
    )
    check(
        "预检通过（ffmpeg 缺失只作提示）",
        bool(preflight.get("ok")),
        "；".join(preflight.get("blocking", [])),
    )

    # Step 1
    status, imported = call(
        "POST",
        "/training/import",
        {"voice_name": "HTTP 探针声音（示例素材）", "material_dir": str(MATERIAL_DIR)},
        timeout=300,
    )
    check("步骤1 导入素材", status == 200 and imported.get("ok"), imported.get("error", ""))
    if not imported.get("ok"):
        return 1
    job_id = imported["job_id"]
    check(
        "导入报告素材条数与时长",
        imported.get("clip_count") == 4 and imported.get("total_seconds", 0) > 18,
        f"{imported.get('clip_count')} 条 / {imported.get('total_seconds')} 秒",
    )

    # Step 2
    started = time.time()
    status, prepared = call("POST", f"/training/{job_id}/prepare", timeout=900)
    check(
        "步骤2 清理切分",
        status == 200 and prepared.get("ok"),
        prepared.get("error", "") or f"{time.time() - started:.1f}s",
    )
    if not prepared.get("ok"):
        return 1

    status, job_body = call("GET", f"/training/{job_id}")
    check(
        "清理后停在「等待校对」而不是自动继续",
        (job_body.get("job") or {}).get("state") == "awaiting_proofread",
        (job_body.get("job") or {}).get("state", ""),
    )

    # Step 3 — the user's step.
    status, proofread_view = call("GET", f"/training/{job_id}/proofread")
    clips = proofread_view.get("clips", [])
    check("步骤3 可读取待校对内容", status == 200 and len(clips) == 4, f"{len(clips)} 条")
    check(
        "校对步骤在提交前不算完成",
        not proofread_view.get("confirmed", False),
        f"confirmed={proofread_view.get('confirmed')}",
    )

    status, submitted = call(
        "POST",
        f"/training/{job_id}/proofread",
        {"clips": [{"path": c["path"], "text": c["text"]} for c in clips]},
    )
    check("步骤3 提交校对结果", status == 200 and submitted.get("ok"), submitted.get("error", ""))

    # Step 4
    status, trained = call(
        "POST",
        f"/training/{job_id}/train",
        {"epochs": epochs, "batch_size": 1},
        timeout=300,
    )
    check("步骤4 启动训练", status == 200 and trained.get("ok"), trained.get("error", ""))
    if not trained.get("ok"):
        return 1
    check("训练在后台进行（请求立即返回）", bool(trained.get("pid")), f"PID {trained.get('pid')}")

    # Deliberately drop the client: the server owns the run, which is exactly
    # why closing the management page does not cancel training.
    print("    （客户端不再轮询，模拟关闭管理页）", flush=True)
    time.sleep(5.0)
    status, still = call("GET", f"/training/{job_id}")
    state_after_detach = (still.get("job") or {}).get("state", "")
    check(
        "关闭页面后训练仍在运行",
        state_after_detach in ("running", "awaiting_proofread"),
        state_after_detach,
    )

    job = wait_for_state(job_id, {"succeeded", "failed", "stopped"})
    check("步骤4 训练完成", job.get("state") == "succeeded", job.get("error", "") or job.get("state", ""))
    if job.get("state") != "succeeded":
        return 1
    check("训练产出权重产物", len(job.get("artifacts", [])) > 0, f"{len(job.get('artifacts', []))} 个")
    check(
        "记录训练时的上游版本",
        bool(job.get("upstream_commit")),
        job.get("upstream_commit", ""),
    )

    # Step 5
    status, audition = call(
        "POST",
        f"/training/{job_id}/audition",
        {"text": "你好，我是爱弥斯。今天天气不错。"},
        timeout=600,
    )
    check("步骤5 试听", status == 200 and audition.get("ok"), audition.get("error", ""))
    if audition.get("ok"):
        check(
            "试听返回可播放音频",
            len(audition.get("audio", "")) > 1000,
            f"{len(audition.get('audio', '')) * 3 // 4} 字节",
        )

    # Step 6 — the step that only works through the real process.
    status, voices_before = call("GET", "/voice/overview")
    active_before = voices_before.get("active_preset_id", "")

    status, applied = call(
        "POST",
        f"/training/{job_id}/apply",
        {"preset_name": "HTTP 探针训练声音（非正式音色）"},
        timeout=300,
    )
    check("步骤6 应用训练产物", status == 200 and applied.get("ok"), applied.get("error", ""))

    status, voices_after = call("GET", "/voice/overview")
    active_after = voices_after.get("active_preset_id", "")
    check(
        "应用后当前音色确实切换",
        bool(applied.get("ok")) and active_after != active_before and active_after != "",
        f"{active_before} -> {active_after}",
    )
    check(
        "应用提示需要重启才生效",
        bool(applied.get("restart_required")),
        f"restart_required={applied.get('restart_required')}",
    )

    report = {
        "job_id": job_id,
        "steps": {
            "import": imported,
            "prepare": {"ok": prepared.get("ok")},
            "proofread": submitted,
            "train": {"ok": True, "artifacts": len(job.get("artifacts", []))},
            "audition": {"ok": audition.get("ok"), "error": audition.get("error", "")},
            "apply": applied,
        },
        "active_preset_before": active_before,
        "active_preset_after": active_after,
        "preflight": {
            "ok": preflight.get("ok"),
            "ddp_patched": preflight.get("ddp", {}).get("patched"),
            "warnings": preflight.get("warnings", []),
        },
        "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in results],
        "passed": all(p for _, p, _ in results),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {REPORT_PATH}")
    return 0 if report["passed"] else 1


def main() -> int:
    """Run the HTTP probe."""
    parser = argparse.ArgumentParser(description="声音训练向导 HTTP 端到端探针")
    parser.add_argument("--all", action="store_true", help="运行完整六步")
    parser.add_argument("--epochs", type=int, default=2)
    args = parser.parse_args()

    if not args.all:
        parser.print_help()
        return 1

    code = run_flow(epochs=args.epochs)
    print("")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"=== {passed}/{len(results)} 项通过 ===")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
