"""End-to-end evidence for the voice-training wizard (V2-T06).

This drives the six steps a user actually walks through — 导入素材 → 清理切分 →
校对文字 → 训练 → 试听 → 应用 — against the real local GPT-SoVITS checkout,
and records what happened.

Why a script and not only tests
-------------------------------
The unit tests pin the *policy* (which transition is legal, what may be retried,
how a restart is reconciled) with everything injected. They cannot show that a
voice is really produced, that the produced weights really synthesise audio, or
that cancelling really leaves the user's own TTS service alone. Those are
claims about this machine, so they are measured here.

The material is the 18.48-second set under ``data/extracted_clean_voices``. It
is far below what a good voice needs, so the report states plainly that the
*flow* passed and the *timbre* is not production quality — the sample must never
be presented as Aemeath's real voice.

Usage::

    python scripts/probe_training_wizard.py --all
    python scripts/probe_training_wizard.py --preflight
    python scripts/probe_training_wizard.py --flow --epochs 2
    python scripts/probe_training_wizard.py --stop-scope
    python scripts/probe_training_wizard.py --restart
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import gpt_sovits as training  # noqa: E402
from aemeath.training.jobs import (  # noqa: E402
    JobState,
    TrainingJobStore,
    reconcile_startup,
)
from aemeath.training.wizard import TrainingWizardService, default_pid_alive  # noqa: E402

REPORT_DIR = ROOT_DIR / "data" / "acceptance" / "training-wizard"
REPORT_PATH = REPORT_DIR / "flow.json"
SCOPE_PATH = REPORT_DIR / "stop-scope.json"
RESTART_PATH = REPORT_DIR / "restart.json"

MATERIAL_DIR = ROOT_DIR / "data" / "extracted_clean_voices"
LIST_PATH = MATERIAL_DIR / "transcripts.list"

#: A scratch store, so a probe run never mixes with the user's real tasks.
PROBE_STORE = REPORT_DIR / "probe-jobs.db"

DEFAULT_API_URL = "http://127.0.0.1:9880"


def log(message: str) -> None:
    """Print one progress line."""
    print(message, flush=True)


def load_report(path: Path) -> Dict[str, Any]:
    """Read an existing report, or start a new one."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_report(path: Path, report: Dict[str, Any]) -> None:
    """Write a report as UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  报告已写入 {path}")


def make_service(*, probe_override: Optional[Dict[str, Any]] = None) -> TrainingWizardService:
    """Build a wizard service on the probe's isolated store."""
    report = load_report(REPORT_PATH)
    report.setdefault("host", sys.platform)
    save_report(REPORT_PATH, report)

    return TrainingWizardService(
        store=TrainingJobStore(PROBE_STORE),
        material_dir=MATERIAL_DIR,
        list_path=LIST_PATH,
        probe=(lambda: probe_override) if probe_override else None,
    )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_preflight() -> int:
    """Check the environment without training anything."""
    log("== 预检 ==")
    service = make_service()
    result = service.preflight()

    upstream = result["environment"].get("upstream", {})
    revision = upstream.get("revision", {}) if isinstance(upstream, dict) else {}

    log(f"  上游: {upstream.get('root', '?')} @ {revision.get('commit', '?')}")
    log(f"  DDP 修复: {'已生效' if result['ddp']['patched'] else '未生效'}")
    log(f"  ffmpeg: {'可用' if result['ffmpeg']['available'] else '不可用（不影响训练）'}")
    log(f"  结论: {'就绪' if result['ok'] else '存在阻塞'}")
    for item in result["blocking"]:
        log(f"    阻塞: {item}")

    report = load_report(REPORT_PATH)
    report["preflight"] = {
        "ok": result["ok"],
        "blocking": result["blocking"],
        "warnings": result.get("warnings", []),
        "ddp_patched": result["ddp"]["patched"],
        "ffmpeg_available": result["ffmpeg"]["available"],
        "upstream_commit": revision.get("commit", ""),
        "upstream_dirty": revision.get("dirty"),
        "gpu": result["environment"].get("gpu", {}),
        "torch": result["environment"].get("torch", {}),
    }
    save_report(REPORT_PATH, report)
    return 0 if result["ok"] else 1


def stage_flow(*, epochs: int, batch_size: int, keep: bool) -> int:
    """Run the six steps end to end and record each one."""
    log("== 六步流程 ==")
    service = make_service()

    started = time.time()
    steps: List[Dict[str, Any]] = []

    # --- Step 1: import ---------------------------------------------------
    imported = service.import_material(voice_name="探针声音（示例素材）")
    if not imported["ok"]:
        log(f"  步骤1 失败: {imported['error']}")
        return 1
    job_id = imported["job_id"]
    log(
        f"  步骤1 导入素材: 通过（{imported['clip_count']} 条，"
        f"{imported['total_seconds']} 秒）"
    )
    steps.append({"step": "import", **imported})

    # --- Step 2: preprocess ----------------------------------------------
    preprocess_started = time.time()
    pre = service.run_preprocess(job_id)
    if not pre["ok"]:
        log(f"  步骤2 失败: {pre['error']}")
        _record_failure(service, steps, "clean", pre["error"], started)
        return 1
    log(f"  步骤2 清理切分: 通过（{time.time() - preprocess_started:.1f}s）")
    steps.append(
        {
            "step": "clean",
            "ok": True,
            "seconds": round(time.time() - preprocess_started, 2),
            "sub_steps": pre.get("steps", []),
        }
    )

    # --- Step 3: proofread ------------------------------------------------
    payload = service.proofread_payload(job_id)
    if not payload["ok"] or not payload["clips"]:
        log("  步骤3 失败: 无法读取校对内容")
        return 1
    # The probe confirms with the material's own transcripts. That is the probe
    # standing in for the user, not an automatic ASR path: the service has no
    # way to close this step by itself, which is the property being preserved.
    submitted = service.submit_proofread(
        job_id,
        [{"path": clip["path"], "text": clip["text"]} for clip in payload["clips"]],
    )
    if not submitted["ok"]:
        log(f"  步骤3 失败: {submitted['error']}")
        return 1
    log(f"  步骤3 校对文字: 通过（{submitted['clip_count']} 条）")
    steps.append({"step": "proofread", "ok": True, "clips": submitted["clip_count"]})

    # --- Step 4: train ----------------------------------------------------
    ready = service.check_training_ready(job_id)
    log(f"  步骤4a 训练集就绪: {'是' if ready['ok'] else '否 — ' + ready['error']}")

    train_started = time.time()
    started_result = service.start_training(job_id, epochs=epochs, batch_size=batch_size)
    if not started_result["ok"]:
        log(f"  步骤4 启动失败: {started_result['error']}")
        _record_failure(service, steps, "train", started_result["error"], started)
        return 1

    log(f"  步骤4 训练进程已启动 PID {started_result['pid']}，等待完成…")
    final = _await_training(service, job_id, timeout=5400.0)
    elapsed = round(time.time() - train_started, 2)
    log(f"  步骤4 训练: {final.state.value}（{elapsed}s）")

    job = service.store.get(job_id)
    steps.append(
        {
            "step": "train",
            "ok": final.state is JobState.SUCCEEDED,
            "state": final.state.value,
            "seconds": elapsed,
            "detail": final.detail,
            "error": final.error,
            "artifacts": job.artifacts if job else [],
            "upstream_commit": job.upstream_commit if job else "",
        }
    )
    if final.state is not JobState.SUCCEEDED:
        log(f"  训练未成功: {final.error}")
        _record_failure(service, steps, "train", final.error, started)
        return 1

    # --- Step 5: audition -------------------------------------------------
    log("  步骤5 试听…")
    import asyncio

    from aemeath.management.voices import VoiceService

    # Auditioning synthesises through the running api_v2 service, which needs no
    # configuration schema, so it *can* be driven from here.
    voices = VoiceService()
    audition = asyncio.run(
        service.audition(
            job_id,
            text="你好，我是爱弥斯。今天天气不错。",
            voice_service=voices,
        )
    )
    if audition["ok"]:
        audio_bytes = len(audition["audio"]) * 3 // 4
        log(f"  步骤5 试听: 通过（{audio_bytes} 字节音频）")
    else:
        log(f"  步骤5 试听失败: {audition['error']}")
    steps.append(
        {
            "step": "audition",
            "ok": audition["ok"],
            "error": audition.get("error", ""),
            "audio_bytes": len(audition.get("audio", "")) * 3 // 4,
            "weights": audition.get("weights", ""),
        }
    )

    # --- Step 6: apply ----------------------------------------------------
    # Applying validates the candidate config through upstream's own schema
    # (`src.open_llm_vtuber.config_manager`), which the pinned upstream only
    # makes importable inside the server process. Driving it from here would
    # either fail on the import or, worse, validate against a *second* copy of
    # the module — the exact duplication the project warns about. So the apply
    # step is measured over HTTP by `probe_training_wizard_http.py` against the
    # real server, and this script reports that it deferred rather than
    # pretending to have checked it.
    log("  步骤6 应用: 交由 probe_training_wizard_http.py 经真实服务验证（本进程无法导入上游 schema）")
    steps.append(
        {
            "step": "apply",
            "ok": None,
            "deferred_to": "scripts/probe_training_wizard_http.py",
            "reason": "应用需要上游 config schema，只在服务进程内可导入。",
        }
    )

    report = load_report(REPORT_PATH)
    report["flow"] = {
        "job_id": job_id,
        "exp_name": service.store.get(job_id).exp_name,
        "total_seconds": round(time.time() - started, 2),
        "steps": steps,
        "material": {
            "dir": str(MATERIAL_DIR),
            "clips": imported["clip_count"],
            "total_seconds": imported["total_seconds"],
        },
        "verdict": {
            # The apply step is deferred, so the in-process stages are what this
            # report claims; the HTTP probe carries the full six-step verdict.
            "in_process_steps_passed": all(
                step.get("ok") for step in steps if step.get("ok") is not None
            ),
            "apply_verified_by": "scripts/probe_training_wizard_http.py",
            "timbre_quality": "未达标：素材仅 18.48 秒，无法作为正式爱弥斯音色",
        },
    }
    save_report(REPORT_PATH, report)

    if keep:
        log(f"  保留任务 {job_id} 与产物用于应用验收")
    return 0


def _await_training(
    service: TrainingWizardService, job_id: str, *, timeout: float
) -> Any:
    """Wait for a job to settle, reporting periodically."""
    deadline = time.time() + timeout
    last_state = ""
    while time.time() < deadline:
        job = service.store.get(job_id)
        if job is None:
            break
        if job.state.value != last_state:
            log(f"    状态: {job.state.value} — {job.detail}")
            last_state = job.state.value
        if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.STOPPED):
            return job
        time.sleep(3.0)
    return service.store.get(job_id)


def _record_failure(
    service: TrainingWizardService,
    steps: List[Dict[str, Any]],
    step: str,
    error: str,
    started: float,
) -> None:
    """Persist a failed run, so the failure is evidence rather than a log line."""
    report = load_report(REPORT_PATH)
    report["flow"] = {
        "steps": steps,
        "total_seconds": round(time.time() - started, 2),
        "failed_at": step,
        "error": error,
        "verdict": {"flow_passed": False},
    }
    save_report(REPORT_PATH, report)


# ---------------------------------------------------------------------------
# Stop scope
# ---------------------------------------------------------------------------


def stage_stop_scope() -> int:
    """Prove stopping a training run does not touch the user's TTS service.

    Upstream cancels with ``taskkill /t /f /pid``, which walks the process tree.
    The whole safety argument for the wizard's stop button rests on the PID
    being *ours*, so this measures both halves: the training process dies, and
    the pre-existing service stays up.
    """
    log("== 停止范围 ==")
    import urllib.error
    import urllib.request

    def service_up() -> bool:
        try:
            with urllib.request.urlopen(f"{DEFAULT_API_URL}/openapi.json", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    before_up = service_up()
    log(f"  停止前，用户既有服务 HTTP 200: {before_up}")

    service = make_service()
    imported = service.import_material(voice_name="停止范围探针")
    if not imported["ok"]:
        log(f"  无法建立任务: {imported['error']}")
        return 1
    job_id = imported["job_id"]

    pre = service.run_preprocess(job_id)
    if not pre["ok"]:
        log(f"  预处理失败: {pre['error']}")
        return 1
    service.submit_proofread(
        job_id,
        [
            {"path": clip["path"], "text": clip["text"]}
            for clip in service.proofread_payload(job_id)["clips"]
        ],
    )

    started = service.start_training(job_id, epochs=20, batch_size=1)
    if not started["ok"]:
        log(f"  启动失败: {started['error']}")
        return 1
    pid = started["pid"]
    log(f"  训练已启动 PID {pid}，等待其进入训练步…")
    time.sleep(25.0)

    alive_before = default_pid_alive(pid)
    stopped = service.stop(job_id)
    time.sleep(8.0)
    alive_after = default_pid_alive(pid)
    after_up = service_up()

    log(f"  训练进程存活（停止前/后）: {alive_before}/{alive_after}")
    log(f"  用户既有服务存活（停止后）: {after_up}")

    report = {
        "job_id": job_id,
        "training_pid": pid,
        "training_alive_before_stop": alive_before,
        "training_alive_after_stop": alive_after,
        "stop_ok": stopped["ok"],
        "service_up_before": before_up,
        "service_up_after": after_up,
        "passed": bool(alive_before and not alive_after and after_up),
        "note": "既有服务可能在本机未运行；此时 service_up 两项同为 False 属正常，不代表被误杀。",
    }
    save_report(SCOPE_PATH, report)
    return 0 if report["passed"] else 1


# ---------------------------------------------------------------------------
# Restart reconciliation
# ---------------------------------------------------------------------------


def stage_restart() -> int:
    """Prove a restart reconciles state from the process and the artefacts.

    The rule under test is the one that keeps the user honest: a task left
    ``running`` by a previous process must be re-decided from reality, never
    promoted to success on the strength of its own record and never re-run.
    """
    log("== 重启核对 ==")
    store = TrainingJobStore(PROBE_STORE)
    service = TrainingWizardService(
        store=store, material_dir=MATERIAL_DIR, list_path=LIST_PATH
    )

    case: Dict[str, Any] = {}

    # Case A: a job whose recorded PID is gone but whose weight exists.
    imported = service.import_material(voice_name="重启核对-已产出")
    job_id = imported["job_id"]
    job = store.get(job_id)
    job.state = JobState.RUNNING
    job.pid = 999999  # certainly not running
    store.save(job)

    weight = _find_existing_weight()
    case["artifacts_found"] = bool(weight)
    report = reconcile_startup(
        store,
        pid_alive=default_pid_alive,
        artifacts_for=lambda record: (
            [{"path": str(weight), "size_bytes": weight.stat().st_size}] if weight else []
        ),
    )
    after_a = store.get(job_id)
    case["dead_pid_with_artifacts_state"] = after_a.state.value
    log(f"  用例A 进程已退出但有产物 -> {after_a.state.value}")

    # Case B: a job whose recorded PID is gone and which has no weight.
    imported_b = service.import_material(voice_name="重启核对-无产物")
    job_b = store.get(imported_b["job_id"])
    job_b.state = JobState.RUNNING
    job_b.pid = 999999
    store.save(job_b)
    report_b = reconcile_startup(
        store, pid_alive=default_pid_alive, artifacts_for=lambda record: []
    )
    after_b = store.get(imported_b["job_id"])
    case["dead_pid_without_artifacts_state"] = after_b.state.value
    log(f"  用例B 进程已退出且无产物 -> {after_b.state.value}")

    # Case C: a live PID must be left alone. This machine's own PID stands in
    # for "a training process we still own".
    imported_c = service.import_material(voice_name="重启核对-仍在运行")
    job_c = store.get(imported_c["job_id"])
    job_c.state = JobState.RUNNING
    job_c.pid = os.getpid()
    store.save(job_c)
    report_c = reconcile_startup(
        store, pid_alive=default_pid_alive, artifacts_for=lambda record: []
    )
    after_c = store.get(imported_c["job_id"])
    case["live_pid_state"] = after_c.state.value
    log(f"  用例C 进程仍在运行 -> {after_c.state.value}")

    case["restarted"] = (
        report["restarted"] + report_b["restarted"] + report_c["restarted"]
    )
    case["restart_policy"] = report["restart_policy"]
    case["passed"] = (
        case["dead_pid_with_artifacts_state"] == "succeeded"
        and case["dead_pid_without_artifacts_state"] == "failed"
        and case["live_pid_state"] == "running"
        and case["restarted"] == []
    )

    save_report(RESTART_PATH, case)
    return 0 if case["passed"] else 1


def _find_existing_weight() -> Optional[Path]:
    """Any weight already produced on this machine, for the restart case."""
    root = training.resolve_upstream_root()
    for version in ("v2", "v1"):
        directory = root / training.SOVITS_WEIGHT_DIRS.get(version, "SoVITS_weights_v2")
        if not directory.exists():
            continue
        matches = sorted(directory.glob("*.pth"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the requested stages."""
    parser = argparse.ArgumentParser(description="声音训练向导端到端探针（V2-T06）")
    parser.add_argument("--preflight", action="store_true", help="只做环境预检")
    parser.add_argument("--flow", action="store_true", help="跑通六步流程")
    parser.add_argument("--stop-scope", action="store_true", help="验证停止只作用于自身进程")
    parser.add_argument("--restart", action="store_true", help="验证重启后的状态核对")
    parser.add_argument("--all", action="store_true", help="运行全部阶段")
    parser.add_argument("--epochs", type=int, default=2, help="训练轮数")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--keep", action="store_true", help="保留任务与产物")
    args = parser.parse_args()

    if not any([args.preflight, args.flow, args.stop_scope, args.restart, args.all]):
        parser.print_help()
        return 1

    codes: Dict[str, int] = {}
    if args.preflight or args.all:
        codes["preflight"] = stage_preflight()
    if args.flow or args.all:
        codes["flow"] = stage_flow(
            epochs=args.epochs, batch_size=args.batch_size, keep=args.keep
        )
    if args.stop_scope or args.all:
        codes["stop_scope"] = stage_stop_scope()
    if args.restart or args.all:
        codes["restart"] = stage_restart()

    log("")
    log("== 汇总 ==")
    for name, code in codes.items():
        log(f"  {name}: {'通过' if code == 0 else '失败'}")
    return 0 if all(code == 0 for code in codes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
