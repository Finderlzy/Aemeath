"""Retry-boundary evidence for the training adapter (V2-T05).

The wizard must know when "再试一次" is a sound offer. This script produces that
evidence against the real upstream scripts rather than asserting it: it starts
real training runs and observes what each failure mode leaves behind.

Three cases are measured:

1. **Cancelled run** -- may be restarted.
2. **Upstream script rejects the run** (missing artefacts) -- an infrastructure
   failure, retryable once the input is fixed.
3. **Process killed mid-run** -- may be restarted.
4. **Run that exits 0 without producing a weight** -- classed as a material
   failure, because retrying reproduces the same result.

Usage::

    python scripts/probe_training_retry.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import gpt_sovits as training  # noqa: E402

REPORT_DIR = ROOT_DIR / "data" / "acceptance" / "training-integration"
REPORT_PATH = REPORT_DIR / "retry-boundaries.json"
EXP_NAME = "aemeath_verify_v2"


def record(results: List[Dict[str, Any]], name: str, job: training.TrainingJob, note: str = "") -> None:
    """Append one observed case.

    Args:
        results: Accumulator.
        name: Case label.
        job: The job that was exercised.
        note: Extra context for the report.
    """
    verdict = job.retry_verdict()
    entry = {
        "case": name,
        "status": job.status.value,
        "failure_kind": job.failure_kind,
        "detail": job.detail,
        "retryable": verdict["retryable"],
        "reason": verdict["reason"],
        "note": note,
    }
    results.append(entry)
    print(
        f"[{'RETRY OK' if verdict['retryable'] else 'NO RETRY'}] {name}: "
        f"status={entry['status']} kind={entry['failure_kind'] or '-'}"
    )


def main() -> int:
    """Observe each retry case and write the report."""
    upstream_root = training.resolve_upstream_root()
    opt_dir = upstream_root / "logs" / EXP_NAME
    work_dir = REPORT_DIR / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    if training.check_training_inputs(opt_dir=opt_dir, version="v2"):
        print("预处理产物缺失，请先运行 verify_training_integration.py --preprocess")
        return 1

    results: List[Dict[str, Any]] = []

    # -- case 1: cancelled run ------------------------------------------
    spec = training.build_sovits_train_spec(
        upstream_root=upstream_root, exp_name=EXP_NAME, opt_dir=opt_dir,
        version="v2", epochs=50, batch_size=1, work_dir=work_dir,
    )
    job = training.TrainingJob(job_id="retry-cancel", exp_name=EXP_NAME, version="v2")
    job.launch(spec)
    time.sleep(20)
    job.cancel()
    time.sleep(3)
    record(results, "取消后重试", job, "训练运行中被取消")

    # -- case 2: upstream refuses because artefacts are missing ---------
    missing_job = training.TrainingJob(job_id="retry-missing", exp_name="no_such_exp", version="v2")
    empty_spec = training.build_sovits_train_spec(
        upstream_root=upstream_root, exp_name="no_such_exp",
        opt_dir=upstream_root / "logs" / "no_such_exp",
        version="v2", epochs=1, batch_size=1, work_dir=work_dir,
    )
    missing_job.launch(empty_spec)
    for _ in range(60):
        if missing_job.refresh() is not training.TrainingStatus.RUNNING:
            break
        time.sleep(1)
    missing_job.refresh()
    record(results, "上游缺少产物而拒绝", missing_job, "上游 check_for_existance 拒绝运行")

    # -- case 3: process killed from outside ----------------------------
    kill_spec = training.build_sovits_train_spec(
        upstream_root=upstream_root, exp_name=EXP_NAME, opt_dir=opt_dir,
        version="v2", epochs=50, batch_size=1, work_dir=work_dir,
    )
    kill_job = training.TrainingJob(job_id="retry-kill", exp_name=EXP_NAME, version="v2")
    kill_job.launch(kill_spec)
    time.sleep(20)
    if kill_job.pid:
        subprocess.run(
            ["taskkill", "/t", "/f", "/pid", str(kill_job.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    for _ in range(30):
        if kill_job.refresh() is not training.TrainingStatus.RUNNING:
            break
        time.sleep(1)
    kill_job.refresh()
    record(results, "进程被外部杀死", kill_job, "模拟断电或用户强杀")

    # -- case 4: exit 0 without producing a weight ----------------------
    #
    # This one cannot be produced by running upstream honestly: a run that gets
    # far enough to exit 0 normally also writes a weight. The situation arises
    # when upstream reports success without usable output (a build/version
    # mismatch, or a weight written to a directory the adapter does not scan),
    # so it is reproduced by driving the same code path with a process that
    # reports exit 0 and yields nothing. The point being pinned is the *policy*:
    # exit code alone must never mean success, and this class of failure must
    # not be offered as a retry.
    class ExitZeroProcess:
        """A process that reports a clean exit while producing nothing."""

        pid = 999001
        returncode = 0

        def poll(self):
            return self.returncode

    no_output_job = training.TrainingJob(
        job_id="retry-nooutput", exp_name="aemeath_no_output", version="v2"
    )
    no_output_spec = training.build_sovits_train_spec(
        upstream_root=upstream_root, exp_name="aemeath_no_output",
        opt_dir=upstream_root / "logs" / "aemeath_no_output",
        version="v2", epochs=1, batch_size=1, work_dir=work_dir,
    )
    no_output_job.launch(no_output_spec, popen=lambda *a, **k: ExitZeroProcess())
    no_output_job.refresh()
    record(results, "退出码 0 但无产物", no_output_job, "进程报告成功但未写出任何权重")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {REPORT_PATH}")

    retryable = [item for item in results if item["retryable"]]
    blocked = [item for item in results if not item["retryable"]]
    print(f"可重试 {len(retryable)} 项，不可重试 {len(blocked)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
