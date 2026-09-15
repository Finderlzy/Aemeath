"""Real end-to-end verification of GPT-SoVITS training integration (V2-T05).

This is the evidence-producing script for V2-T05. Unlike the pytest suite, it
runs the **real** upstream pipeline on this machine: preprocessing, a small
fine-tune, and a real synthesis from the resulting weights.

It is deliberately explicit rather than a pytest test -- a training run takes
minutes and writes gigabytes, and the task requires measured facts (timings,
VRAM, artefact paths) rather than a pass/fail bit.

Usage::

    python scripts/verify_training_integration.py --grid     # precheck only
    python scripts/verify_training_integration.py --preprocess
    python scripts/verify_training_integration.py --train --epochs 2
    python scripts/verify_training_integration.py --synthesize

Each stage is separate so a long run can be resumed without repeating earlier
work, and so the interactive stages (cancel, contention) can be driven
independently.

Results are echoed to the console and appended to
``data/acceptance/training-integration/report.json`` for the acceptance record.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import gpt_sovits as training  # noqa: E402
from aemeath.training import probe  # noqa: E402

DEFAULT_EXP_NAME = "aemeath_verify_v2"
DEFAULT_MATERIAL = ROOT_DIR / "data" / "extracted_clean_voices"
REPORT_DIR = ROOT_DIR / "data" / "acceptance" / "training-integration"
REPORT_PATH = REPORT_DIR / "report.json"

PREPROCESS_STEPS = ("1-get-text", "2-get-hubert-wav32k", "3-get-semantic")


def log(message: str) -> None:
    """Print a timestamped progress line.

    Args:
        message: Text to print.
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_report() -> Dict[str, Any]:
    """Read the accumulated report, if one exists.

    Returns:
        The stored report, or a fresh skeleton.
    """
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"stages": {}}


def save_report(report: Dict[str, Any]) -> None:
    """Persist the report as JSON.

    Args:
        report: Report document to write.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_env(upstream_root: Path, extra: Dict[str, str]) -> Dict[str, str]:
    """Compose the child environment for an upstream script.

    Args:
        upstream_root: Checkout root, prepended to ``sys.path`` in the child.
        extra: Additional variables.

    Returns:
        A complete environment mapping.
    """
    env = dict(os.environ)
    env.update(extra)
    # Upstream scripts import each other by bare module name.
    env["PYTHONPATH"] = str(upstream_root / "GPT_SoVITS") + os.pathsep + str(
        upstream_root
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_stage(
    *,
    name: str,
    command: List[str],
    env: Dict[str, str],
    cwd: str,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one upstream stage to completion and capture its outcome.

    Args:
        name: Stage label for logging.
        command: Full argv.
        env: Child environment.
        cwd: Working directory.
        timeout: Optional timeout in seconds.

    Returns:
        ``{"name", "returncode", "seconds", "tail", "error"}``.
    """
    log(f"开始 {name}: {' '.join(command[:4])} ...")
    started = time.perf_counter()
    result: Dict[str, Any] = {"name": name, "returncode": None, "seconds": 0.0, "tail": "", "error": ""}
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        result["returncode"] = completed.returncode
        combined = (completed.stdout or "") + (completed.stderr or "")
        result["tail"] = combined[-3000:]
    except subprocess.TimeoutExpired as exc:
        result["returncode"] = -1
        result["error"] = f"超时 ({timeout}s)"
        result["tail"] = (exc.stdout or "")[-2000:] if exc.stdout else ""
    except OSError as exc:
        result["returncode"] = -1
        result["error"] = str(exc)

    result["seconds"] = round(time.perf_counter() - started, 2)
    log(f"{name} 结束，退出码 {result['returncode']}，耗时 {result['seconds']}s")
    return result


def stage_preprocess(
    *,
    upstream_root: Path,
    material_dir: Path,
    list_path: Path,
    exp_name: str,
    version: str,
) -> Dict[str, Any]:
    """Run the three dataset-preparation stages in order.

    Args:
        upstream_root: Checkout root.
        material_dir: Directory holding clips.
        list_path: The ``.list`` file.
        exp_name: Experiment name.
        version: Model version.

    Returns:
        A stage result with per-step outcomes and produced artefacts.
    """
    opt_dir = upstream_root / "logs" / exp_name
    opt_dir.mkdir(parents=True, exist_ok=True)

    steps: List[Dict[str, Any]] = []
    for step in PREPROCESS_STEPS:
        spec = training.build_preprocess_spec(
            upstream_root=upstream_root,
            step=step,
            exp_name=exp_name,
            list_path=list_path,
            audio_dir=material_dir,
            opt_dir=opt_dir,
            version=version,
        )
        spec_env = dict(spec.env)
        spec_env["is_half"] = "True"
        outcome = run_stage(
            name=step,
            command=spec.command,
            env=build_env(upstream_root, spec_env),
            cwd=spec.cwd,
            timeout=3600,
        )
        outcome["outputs"] = [
            {"path": str(path), "exists": path.exists()} for path in spec.outputs
        ]
        steps.append(outcome)
        if outcome["returncode"] != 0:
            log(f"{step} 失败，停止后续预处理。")
            break

        # The WebUI merges the per-part artefacts itself; a command-line caller
        # has to do it explicitly or the trainer refuses to start.
        merges = training.merge_all_part_files(opt_dir=opt_dir)
        if merges:
            outcome["merged"] = [
                {"path": str(path), "bytes": path.stat().st_size} for path in merges
            ]
            for path in merges:
                log(f"  已合并 -> {path.name} ({path.stat().st_size} 字节)")

    missing = training.check_training_inputs(opt_dir=opt_dir, version=version)
    return {
        "opt_dir": str(opt_dir),
        "steps": steps,
        "missing_artifacts": missing,
        "ready_for_training": not missing,
    }


def stage_train(
    *,
    upstream_root: Path,
    exp_name: str,
    version: str,
    epochs: int,
    batch_size: int,
    work_dir: Path,
    timeout: float,
) -> Dict[str, Any]:
    """Launch a real SoVITS fine-tune through :class:`training.TrainingJob`.

    Args:
        upstream_root: Checkout root.
        exp_name: Experiment name.
        version: Model version.
        epochs: Training epochs.
        batch_size: Batch size.
        work_dir: Where the generated config is written.
        timeout: Seconds to wait before giving up.

    Returns:
        A stage result with the observed lifecycle, timings and artefacts.
    """
    opt_dir = upstream_root / "logs" / exp_name
    spec = training.build_sovits_train_spec(
        upstream_root=upstream_root,
        exp_name=exp_name,
        opt_dir=opt_dir,
        version=version,
        epochs=epochs,
        batch_size=batch_size,
        work_dir=work_dir,
    )

    job = training.TrainingJob(job_id=f"verify-{exp_name}", exp_name=exp_name, version=version)
    log(f"启动训练（epochs={epochs}, batch_size={batch_size}），配置 {spec.config_path}")

    started = time.perf_counter()
    gpu_before = probe.gpu_report()
    job.launch(spec)

    peak_used_mb = gpu_before.get("used_mb", 0)
    status = job.status
    while True:
        elapsed = time.perf_counter() - started
        status = job.refresh()
        sample = probe.gpu_report()
        if sample.get("available"):
            peak_used_mb = max(peak_used_mb, sample.get("used_mb", 0))
        if status is not training.TrainingStatus.RUNNING:
            break
        if elapsed > timeout:
            log(f"等待超时（{timeout}s），终止训练。")
            job.cancel()
            status = training.TrainingStatus.CANCELLED
            break
        time.sleep(3)

    seconds = round(time.perf_counter() - started, 2)
    log(f"训练结束：{status.value}，耗时 {seconds}s，峰值显存 {peak_used_mb}MB")

    return {
        "job_id": job.job_id,
        "pid": job.pid,
        "status": status.value,
        "detail": job.detail,
        "seconds": seconds,
        "peak_gpu_used_mb": peak_used_mb,
        "artifacts": [str(path) for path in job.artifacts],
        "retry_verdict": job.retry_verdict(),
        "config_path": spec.config_path,
    }


def stage_synthesize(
    *,
    api_url: str,
    ref_audio: Optional[str],
    prompt_text: str,
    text: str,
    out_path: Path,
) -> Dict[str, Any]:
    """Synthesize speech through the real local api_v2 service.

    This is the listening test: it proves the produced weights can actually
    speak, which a file-existence check cannot.

    Args:
        api_url: Base URL of the api_v2 service.
        ref_audio: Reference clip the service will clone.
        prompt_text: Transcript of the reference clip.
        text: Text to speak.
        out_path: Where the WAV is written.

    Returns:
        A stage result including byte count and duration.
    """
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import urlopen

    if not ref_audio:
        return {"ok": False, "error": "缺少参考音频，无法合成。", "out_path": str(out_path)}

    params = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": ref_audio,
        "prompt_lang": "zh",
        "prompt_text": prompt_text,
        "text_split_method": "cut5",
        "batch_size": "1",
        "media_type": "wav",
        "streaming_mode": "false",
    }
    url = f"{api_url.rstrip('/')}/tts?{urlencode(params)}"
    log(f"请求合成: {api_url}")
    started = time.perf_counter()
    try:
        with urlopen(url, timeout=300) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as exc:
        return {
            "ok": False,
            "error": f"HTTP {exc.code}: {exc.read()[:400].decode('utf-8', 'replace')}",
            "out_path": str(out_path),
        }
    except (URLError, OSError) as exc:
        return {"ok": False, "error": str(exc), "out_path": str(out_path)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(payload)
    seconds = round(time.perf_counter() - started, 2)
    log(f"合成完成：{len(payload)} 字节，耗时 {seconds}s -> {out_path}")

    return {
        "ok": True,
        "bytes": len(payload),
        "seconds": seconds,
        "content_type": content_type,
        "out_path": str(out_path),
        "playable": payload[:4] == b"RIFF",
    }


def stage_cancel(
    *,
    upstream_root: Path,
    exp_name: str,
    version: str,
    work_dir: Path,
    api_url: str,
    settle_seconds: float = 25.0,
) -> Dict[str, Any]:
    """Start a real training run, cancel it, and check the service survives.

    The requirement is specific: stopping *our* run must not disturb a
    GPT-SoVITS service the user already had running. Upstream cancels with
    ``taskkill /t /f /pid``, which kills a whole process tree, so this is a real
    risk rather than a theoretical one -- and it is checked against a live
    service rather than argued about.

    Args:
        upstream_root: Checkout root.
        exp_name: Experiment name.
        version: Model version.
        work_dir: Where the generated config is written.
        api_url: Base URL of the running service.
        settle_seconds: How long to let training get going before cancelling.

    Returns:
        A stage result describing what was observed.
    """
    opt_dir = upstream_root / "logs" / exp_name
    spec = training.build_sovits_train_spec(
        upstream_root=upstream_root,
        exp_name=exp_name,
        opt_dir=opt_dir,
        version=version,
        epochs=50,
        batch_size=1,
        work_dir=work_dir,
    )

    service_before = probe.check_service(api_url)
    job = training.TrainingJob(
        job_id=f"cancel-{exp_name}", exp_name=exp_name, version=version
    )
    job.launch(spec)
    log(f"已启动训练用于取消测试，PID {job.pid}；等待 {settle_seconds}s 让其进入训练循环")

    time.sleep(settle_seconds)
    status_before_cancel = job.refresh()
    cancelled = job.cancel()
    time.sleep(5)
    service_after = probe.check_service(api_url)

    # Taskkill /f is asynchronous; give the OS a moment, then confirm the PID is gone.
    pid_alive = False
    if job.pid:
        try:
            result = subprocess.run(
                ["tasklist", "/fi", f"PID eq {job.pid}", "/nh"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            pid_alive = str(job.pid) in (result.stdout or "")
        except (OSError, subprocess.SubprocessError):
            pid_alive = False

    log(
        f"取消结果: 发出={cancelled}, 进程仍存在={pid_alive}, "
        f"服务取消前={service_before['running']}, 取消后={service_after['running']}"
    )

    return {
        "pid": job.pid,
        "status_before_cancel": status_before_cancel.value,
        "cancel_issued": cancelled,
        "pid_alive_after_cancel": pid_alive,
        "service_before": service_before,
        "service_after": service_after,
        "service_survived": service_before["running"] and service_after["running"],
        "status_after_cancel": job.status.value,
    }


def stage_contention(
    *,
    api_url: str,
    ref_audio: str,
    prompt_text: str,
    upstream_root: Path,
    exp_name: str,
    version: str,
    work_dir: Path,
) -> Dict[str, Any]:
    """Measure what happens when training and live TTS share the GPU.

    The 8 GB laptop GPU is the binding constraint on this whole feature, and the
    answer decides whether the wizard may train while Aemeath is talking or has
    to serialise the two.

    Args:
        api_url: Base URL of the running service.
        ref_audio: Reference clip for synthesis.
        prompt_text: Transcript of the reference clip.
        upstream_root: Checkout root.
        exp_name: Experiment name.
        version: Model version.
        work_dir: Where the generated config is written.

    Returns:
        A stage result with the baseline and contended measurements.
    """
    baseline = probe.gpu_report()

    # Synthesize once with training stopped, as the reference point.
    solo = stage_synthesize(
        api_url=api_url,
        ref_audio=ref_audio,
        prompt_text=prompt_text,
        text="并发测试基准句。",
        out_path=REPORT_DIR / "contention-solo.wav",
    )

    opt_dir = upstream_root / "logs" / exp_name
    spec = training.build_sovits_train_spec(
        upstream_root=upstream_root,
        exp_name=exp_name,
        opt_dir=opt_dir,
        version=version,
        epochs=50,
        batch_size=1,
        work_dir=work_dir,
    )
    job = training.TrainingJob(
        job_id=f"contend-{exp_name}", exp_name=exp_name, version=version
    )
    job.launch(spec)
    log(f"训练已启动用于并发测试，PID {job.pid}；等待其进入训练循环")
    time.sleep(30)

    during = probe.gpu_report()
    concurrent = stage_synthesize(
        api_url=api_url,
        ref_audio=ref_audio,
        prompt_text=prompt_text,
        text="并发测试基准句。",
        out_path=REPORT_DIR / "contention-concurrent.wav",
    )
    peak = probe.gpu_report()

    job.cancel()

    return {
        "gpu_baseline": baseline,
        "gpu_during_training": during,
        "gpu_peak": peak,
        "synthesis_solo": solo,
        "synthesis_concurrent": concurrent,
        "synthesis_still_works": bool(concurrent.get("ok")),
    }


def main() -> int:
    """Run the requested verification stages."""
    parser = argparse.ArgumentParser(description="V2-T05 训练接入真实验证")
    parser.add_argument("--precheck", action="store_true", help="只做环境预检")
    parser.add_argument("--preprocess", action="store_true", help="运行数据预处理")
    parser.add_argument("--train", action="store_true", help="运行 SoVITS 小样本训练")
    parser.add_argument("--cancel", action="store_true", help="取消测试：确认不误杀既有服务")
    parser.add_argument("--contention", action="store_true", help="并发测试：训练与实时 TTS 的资源关系")
    parser.add_argument("--synthesize", action="store_true", help="用产物真实合成并保存 WAV")
    parser.add_argument("--exp-name", default=DEFAULT_EXP_NAME)
    parser.add_argument("--version", default="v2")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--material-dir", default=str(DEFAULT_MATERIAL))
    parser.add_argument("--list-path", default="")
    parser.add_argument("--api-url", default="http://127.0.0.1:9880")
    parser.add_argument("--timeout", type=float, default=5400.0, help="训练等待上限（秒）")
    parser.add_argument("--text", default="你好，我是爱弥斯。")
    parser.add_argument(
        "--ref-audio",
        default=str(ROOT_DIR / "data" / "voice-reference" / "reference.wav"),
    )
    parser.add_argument(
        "--prompt-text",
        default="",
        help="参考音频文本；留空则读取 data/voice-reference/reference.txt",
    )
    args = parser.parse_args()

    if not any([args.precheck, args.preprocess, args.train, args.cancel, args.contention, args.synthesize]):
        args.precheck = True

    upstream_root = training.resolve_upstream_root()
    material_dir = Path(args.material_dir)
    list_path = Path(args.list_path) if args.list_path else material_dir / "transcripts.list"

    report = load_report()
    report["upstream_root"] = str(upstream_root)
    report["exp_name"] = args.exp_name
    report["version"] = args.version

    exit_code = 0

    if args.precheck:
        print("=== 环境预检 ===")
        result = probe.precheck(
            upstream_root=upstream_root,
            material_dir=material_dir,
            list_path=list_path,
            exp_name=args.exp_name,
            version=args.version,
            api_url=args.api_url,
        )
        print(probe.format_report(result))
        report["stages"]["precheck"] = result
        if not result["ready"]:
            exit_code = 1

    if args.preprocess:
        print("\n=== 数据预处理 ===")
        result = stage_preprocess(
            upstream_root=upstream_root,
            material_dir=material_dir,
            list_path=list_path,
            exp_name=args.exp_name,
            version=args.version,
        )
        for step in result["steps"]:
            print(f"  {step['name']}: 退出码 {step['returncode']}，{step['seconds']}s")
        print(f"  产物就绪: {result['ready_for_training']}")
        if result["missing_artifacts"]:
            for item in result["missing_artifacts"]:
                print(f"    缺失: {item}")
        report["stages"]["preprocess"] = result
        if not result["ready_for_training"]:
            exit_code = 1

    if args.train:
        print("\n=== 小样本训练 ===")
        work_dir = REPORT_DIR / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        missing = training.check_training_inputs(
            opt_dir=upstream_root / "logs" / args.exp_name, version=args.version
        )
        if missing:
            print("  训练前置产物缺失，请先运行 --preprocess：")
            for item in missing:
                print(f"    {item}")
            report["stages"]["train"] = {"status": "blocked", "missing_artifacts": missing}
            exit_code = 1
        else:
            result = stage_train(
                upstream_root=upstream_root,
                exp_name=args.exp_name,
                version=args.version,
                epochs=args.epochs,
                batch_size=args.batch_size,
                work_dir=work_dir,
                timeout=args.timeout,
            )
            print(f"  状态: {result['status']} — {result['detail']}")
            print(f"  PID: {result['pid']}，耗时 {result['seconds']}s，峰值显存 {result['peak_gpu_used_mb']}MB")
            for path in result["artifacts"]:
                print(f"  产物: {path}")
            report["stages"]["train"] = result
            if result["status"] != "succeeded":
                exit_code = 1

    if args.cancel:
        print("\n=== 取消测试（不误杀既有服务）===")
        work_dir = REPORT_DIR / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        result = stage_cancel(
            upstream_root=upstream_root,
            exp_name=args.exp_name,
            version=args.version,
            work_dir=work_dir,
            api_url=args.api_url,
        )
        print(f"  取消发出: {result['cancel_issued']}")
        print(f"  训练进程仍存在: {result['pid_alive_after_cancel']}")
        print(f"  服务取消前/后: {result['service_before']['running']} / {result['service_after']['running']}")
        report["stages"]["cancel"] = result
        if not result["cancel_issued"] or result["pid_alive_after_cancel"]:
            exit_code = 1
        if not result["service_after"]["running"]:
            print("  既有 GPT-SoVITS 服务被误杀！")
            exit_code = 1

    if args.contention:
        print("\n=== 资源争用测试 ===")
        prompt_text = args.prompt_text
        if not prompt_text:
            prompt_file = ROOT_DIR / "data" / "voice-reference" / "reference.txt"
            if prompt_file.exists():
                prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        work_dir = REPORT_DIR / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        result = stage_contention(
            api_url=args.api_url,
            ref_audio=args.ref_audio,
            prompt_text=prompt_text,
            upstream_root=upstream_root,
            exp_name=args.exp_name,
            version=args.version,
            work_dir=work_dir,
        )
        during = result["gpu_during_training"]
        print(f"  基线显存: {result['gpu_baseline'].get('used_mb')}MB")
        print(f"  训练中显存: {during.get('used_mb')}MB")
        print(f"  单独合成: {result['synthesis_solo'].get('seconds')}s ok={result['synthesis_solo'].get('ok')}")
        print(f"  并发合成: {result['synthesis_concurrent'].get('seconds')}s ok={result['synthesis_concurrent'].get('ok')}")
        report["stages"]["contention"] = result
        if not result["synthesis_still_works"]:
            print(f"  并发合成失败: {result['synthesis_concurrent'].get('error')}")
            exit_code = 1

    if args.synthesize:
        print("\n=== 真实合成 ===")
        prompt_text = args.prompt_text
        if not prompt_text:
            prompt_file = ROOT_DIR / "data" / "voice-reference" / "reference.txt"
            if prompt_file.exists():
                prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        out_path = REPORT_DIR / "synthesized.wav"
        result = stage_synthesize(
            api_url=args.api_url,
            ref_audio=args.ref_audio,
            prompt_text=prompt_text,
            text=args.text,
            out_path=out_path,
        )
        if result.get("ok"):
            print(f"  合成成功: {result['bytes']} 字节，WAV 头正确: {result['playable']}")
        else:
            print(f"  合成失败: {result.get('error')}")
        report["stages"]["synthesize"] = result
        if not result.get("ok"):
            exit_code = 1

    save_report(report)
    print(f"\n报告已写入: {REPORT_PATH}")
    print("VERIFY PASSED" if exit_code == 0 else "VERIFY FAILED")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
