"""The six-step voice-training wizard (V2-T06).

What the user walks through
---------------------------
导入素材 → 清理切分 → 校对文字 → 准备训练集与训练 → 试听 → 应用

This module is the orchestration *behind* that flow. It owns no HTTP and no UI;
:mod:`aemeath.management.routes` maps requests onto it and ``training-page.tsx``
renders it. Keeping it standalone is what lets ``scripts/probe_training_wizard.py``
drive the real flow without a browser.

The rules it enforces, and why
------------------------------
Each of these was measured in V2-T05 (see ``docs/acceptance.md`` §21) and each
one is a place where the convenient implementation is the wrong one:

**The DDP precondition is checked, not assumed.** The pinned upstream crashes on
a single Windows GPU with an access violation that Python cannot catch. The
guard is therefore read out of the actual checkout before every start, and its
absence is a refusal that names the patch — not a warning that leads to a
30-second mystery.

**Exit code 0 is not success.** Upstream can finish cleanly having written no
weight, so success is decided by artefacts on disk.

**Cancellation is PID-scoped, and refusable.** ``taskkill /t /f /pid`` walks the
process tree, so the PID is the only thing separating our training run from the
user's own TTS service. No PID means no kill. A second stop is a no-op rather
than a second kill at a possibly recycled PID.

**Proofreading belongs to a human.** The requirement says a successful ASR pass
is not evidence that the material is accurate, so nothing here can mark that
step complete. Only :meth:`TrainingWizardService.submit_proofread` does, and it
only runs when the user posts text.

**Training does not touch the active voice.** Applying is a separate, explicit
action that delegates to the existing :class:`~aemeath.management.voices.VoiceService`,
so a failed or stopped run leaves the character speaking exactly as before.

**One run at a time.** Upstream writes its generated config into a shared
``TEMP/`` directory, so two concurrent runs overwrite each other's settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from .gpt_sovits import (
    MIN_CLIPS,
    MIN_TOTAL_SECONDS,
    TrainingJob,
    build_preprocess_spec,
    build_sovits_train_spec,
    find_artifacts,
    merge_all_part_files,
    resolve_upstream_root,
    training_python,
    validate_material,
)
from .jobs import JobState, TrainingJobRecord, TrainingJobStore

#: Relative path of the upstream patch that fixes single-GPU training.
DDP_PATCH_PATH = "docs/patches/upstream/s2-train-single-gpu-ddp.patch"

#: The upstream script the DDP guard lives in, relative to the checkout root.
S2_TRAIN_RELATIVE = "GPT_SoVITS/s2_train.py"

#: Default experiment name used when the caller does not supply one.
DEFAULT_EXP_NAME = "aemeath_wizard"

#: Preprocessing steps, in upstream's required order.
PREPROCESS_STEPS = ("1-get-text", "2-get-hubert-wav32k", "3-get-semantic")

#: The six steps the wizard shows, in order.
WIZARD_STEPS: Sequence[Dict[str, str]] = (
    {"id": "import", "label": "导入素材", "hint": "选择包含录音的目录与标注文件。"},
    {"id": "clean", "label": "清理切分", "hint": "按上游流程提取文本、特征与语义。"},
    {"id": "proofread", "label": "校对文字", "hint": "逐条听音频、核对文字；这一步只能由你确认。"},
    {"id": "prepare", "label": "准备训练集", "hint": "核对训练所需的预处理产物是否齐备。"},
    {"id": "train", "label": "训练声音", "hint": "在本机 GPU 上用 GPT-SoVITS 微调。"},
    {"id": "audition", "label": "试听", "hint": "先用新声音合成一句，不会改变当前音色。"},
    {"id": "apply", "label": "应用", "hint": "确认后才会替换当前使用中的音色。"},
)


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------


def check_ddp_guard(upstream_root: Optional[Path] = None) -> Dict[str, Any]:
    """Whether the checkout carries the single-GPU fix.

    V2-T05 established that upstream's ``s2_train.py`` unconditionally enables
    DDP. On one Windows GPU that crashes the child process with
    ``0xC0000005``, which surfaces as an uncatchable crash rather than a Python
    exception — so it cannot be handled at the point of failure and has to be
    refused up front.

    The check reads the script rather than trusting a note, because the patch is
    not upstreamed: a reinstall silently reverts it.

    Args:
        upstream_root: Checkout override; resolved from the environment when
            omitted.

    Returns:
        ``{"ok", "patched", "path", "error", "recovery"}``. ``recovery`` is a
        user-facing instruction, present only when the check fails.
    """
    root = resolve_upstream_root(upstream_root)
    script = root / S2_TRAIN_RELATIVE

    if not script.is_file():
        return {
            "ok": False,
            "patched": False,
            "path": str(script),
            "error": f"找不到上游训练脚本：{script}",
            "recovery": "确认 GPT-SoVITS 目录是否正确，或设置环境变量 GPT_SOVITS_DIR。",
        }

    try:
        source = script.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - unreadable file
        return {
            "ok": False,
            "patched": False,
            "path": str(script),
            "error": f"无法读取上游训练脚本：{exc}",
            "recovery": "检查文件权限后重试。",
        }

    # The guard is `use_ddp = n_gpus > 1`. An unpatched file either lacks the
    # name entirely or hardcodes it to True, and both mean the same thing.
    guarded = False
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("use_ddp"):
            continue
        # Normalise whitespace so formatting differences do not matter.
        compact = stripped.replace(" ", "")
        if compact.startswith("use_ddp=n_gpus>1"):
            guarded = True
            break

    if guarded:
        return {
            "ok": True,
            "patched": True,
            "path": str(script),
            "error": "",
            "recovery": "",
        }

    recovery = (
        f"在 GPT-SoVITS 检出根目录应用补丁：git apply {DDP_PATCH_PATH}；"
        "若上游已修复，请更新到修复版本。"
    )
    return {
        "ok": False,
        "patched": False,
        "path": str(script),
        # The error text carries the fix as well as the symptom: this string is
        # what appears in the blocking list the user reads, and a diagnosis with
        # no next step is not actionable.
        "error": (
            "上游 s2_train.py 未包含单卡 DDP 修复（use_ddp = n_gpus > 1）。"
            "在本机 Windows 单卡环境下，训练会以访问违例崩溃，且无法在 Python 层捕获。"
            f"修复方式：应用 {DDP_PATCH_PATH}。"
        ),
        "recovery": recovery,
    }


def check_ffmpeg() -> Dict[str, Any]:
    """Whether ``ffmpeg`` is reachable, because upstream preprocessing needs it.

    Reported separately from the GPU checks so the user learns about it at the
    step that needs it rather than from a failed run.

    Returns:
        ``{"available", "path", "error", "recovery"}``.
    """
    import shutil

    exe = shutil.which("ffmpeg")
    if not exe:
        return {
            "available": False,
            "path": "",
            "error": "未找到 ffmpeg。",
            "recovery": "安装 ffmpeg 并加入 PATH，或把其所在目录加入 PATH 后重启 Aemeath。",
        }
    return {"available": True, "path": exe, "error": "", "recovery": ""}


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def fingerprint_file(path: Path) -> str:
    """A stable fingerprint of one file's content.

    Content, not mtime: a file that is re-saved with the same bytes has not
    changed, and re-running preprocessing for it would waste minutes.

    Args:
        path: File to fingerprint.

    Returns:
        A hex digest, or ``""`` when the file cannot be read.
    """
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def fingerprint_material(*, audio_dir: Path, list_path: Path) -> str:
    """Fingerprint the material a step consumes.

    Both the annotation file and every referenced clip go in: editing a
    transcript and replacing a recording are both real changes, and reusing
    artefacts across either is how a voice ends up trained on material the user
    never approved.

    Args:
        audio_dir: Directory holding the clips.
        list_path: The ``.list`` annotation file.

    Returns:
        A hex digest.
    """
    digest = hashlib.sha256()
    digest.update(fingerprint_file(list_path).encode("ascii"))

    entries: List[str] = []
    try:
        raw = Path(list_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split("|")[0]
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = Path(audio_dir) / name
        entries.append(f"{name}:{fingerprint_file(candidate)}")

    for entry in sorted(entries):
        digest.update(entry.encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


ProcessFactory = Callable[..., Any]


class TrainingWizardService:
    """Orchestrates the wizard's steps on top of the V2-T05 adapter."""

    def __init__(
        self,
        *,
        store: TrainingJobStore,
        upstream_root: Optional[Path] = None,
        material_dir: Optional[Path] = None,
        list_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
        probe: Optional[Callable[[], Dict[str, Any]]] = None,
        popen: Optional[ProcessFactory] = None,
        pid_alive: Optional[Callable[[int], bool]] = None,
    ) -> None:
        """Bind the service.

        Args:
            store: Persistent job store.
            upstream_root: GPT-SoVITS checkout; resolved when omitted.
            material_dir: Default clip directory.
            list_path: Default annotation file.
            config_path: Config the apply step writes through.
            probe: Environment-precheck callable; injected by tests.
            popen: ``subprocess.Popen``-compatible factory; injected by tests.
            pid_alive: PID liveness check; injected by tests.
        """
        self.store = store
        self.upstream_root = resolve_upstream_root(upstream_root)
        from .probe import DEFAULT_MATERIAL_DIR, precheck

        self.material_dir = Path(material_dir) if material_dir else DEFAULT_MATERIAL_DIR
        self.list_path = (
            Path(list_path) if list_path else self.material_dir / "transcripts.list"
        )
        self.config_path = config_path
        self._probe = probe or (lambda: precheck(upstream_root=self.upstream_root))
        self._popen = popen
        self._pid_alive = pid_alive or default_pid_alive
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def preflight(self) -> Dict[str, Any]:
        """Everything that must hold before training can start.

        Combines the V2-T05 environment probe with the two checks this task
        added: the DDP guard and ``ffmpeg``.

        Returns:
            ``{"ok", "blocking", "recovery", "environment", "ddp", "ffmpeg"}``.
            ``recovery`` maps each blocking item to an actionable instruction.
        """
        environment = self._probe()
        ddp = check_ddp_guard(self.upstream_root)
        ffmpeg = check_ffmpeg()

        blocking: List[str] = list(environment.get("blocking") or [])
        warnings: List[str] = []
        recovery: List[Dict[str, str]] = []

        for item in environment.get("blocking") or []:
            recovery.append({"problem": item, "action": _environment_recovery(item)})

        if not ddp["ok"]:
            blocking.append(ddp["error"])
            recovery.append({"problem": ddp["error"], "action": ddp["recovery"]})

        # ffmpeg is reported but does not block. V2-T05 ran a full training pass
        # on this machine with no ffmpeg on PATH, so treating its absence as
        # fatal would refuse work that demonstrably succeeds — and the upstream
        # scripts that do need it fail with their own clear message at the step
        # that needs it.
        if not ffmpeg["available"]:
            warnings.append(ffmpeg["error"])
            recovery.append({"problem": ffmpeg["error"], "action": ffmpeg["recovery"]})

        return {
            "ok": not blocking,
            "blocking": blocking,
            "warnings": warnings,
            "recovery": recovery,
            "environment": environment,
            "ddp": ddp,
            "ffmpeg": ffmpeg,
        }

    # ------------------------------------------------------------------
    # Steps 1-2: import and clean
    # ------------------------------------------------------------------

    def import_material(
        self,
        *,
        voice_name: str,
        material_dir: Optional[Path] = None,
        list_path: Optional[Path] = None,
        exp_name: str = "",
    ) -> Dict[str, Any]:
        """Step 1: check the material and open a job for it.

        Args:
            voice_name: What the user calls this voice.
            material_dir: Override clip directory.
            list_path: Override annotation file.
            exp_name: Upstream experiment name; derived when omitted.

        Returns:
            ``{ok, job_id, error, clip_count, total_seconds}``.
        """
        if not (voice_name or "").strip():
            return self._failure("请先给这个声音起一个名字。")

        audio_dir = Path(material_dir) if material_dir else self.material_dir
        listing = Path(list_path) if list_path else (
            audio_dir / "transcripts.list" if material_dir else self.list_path
        )

        problems = validate_material(audio_dir=audio_dir, list_path=listing)
        if problems:
            return self._failure(
                "素材不可用：" + "；".join(problems),
                extra={"problems": problems},
            )

        clip_count, total_seconds = _material_stats(audio_dir=audio_dir, list_path=listing)

        job_id = f"train-{uuid.uuid4().hex[:8]}"
        record = TrainingJobRecord(
            job_id=job_id,
            exp_name=exp_name or f"{DEFAULT_EXP_NAME}_{job_id.split('-')[-1]}",
            voice_name=voice_name.strip(),
            state=JobState.PENDING,
            stage="import",
            material_dir=str(audio_dir),
            list_path=str(listing),
            upstream_root=str(self.upstream_root),
        )
        self.store.save(record)

        fingerprint = fingerprint_material(audio_dir=audio_dir, list_path=listing)
        self.store.record_step(
            job_id,
            "import",
            fingerprint=fingerprint,
            outputs=[str(listing)],
            clip_count=clip_count,
            total_seconds=round(total_seconds, 2),
            audio_dir=str(audio_dir),
        )
        logger.info(
            "Opened training job {} for {} clip(s), {:.1f}s of audio.",
            job_id,
            clip_count,
            total_seconds,
        )
        return {
            "ok": True,
            "job_id": job_id,
            "error": "",
            "clip_count": clip_count,
            "total_seconds": round(total_seconds, 2),
        }

    def run_preprocess(self, job_id: str) -> Dict[str, Any]:
        """Step 2: run upstream's three dataset-preparation steps.

        Each step's parts are merged afterwards, because that merge happens in
        upstream's *WebUI* rather than in any script — a command-line caller
        that skips it is refused by ``s2_train.py`` with a missing-artefact
        error (found in V2-T05).

        Args:
            job_id: Job to prepare.

        Returns:
            ``{ok, error, steps}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        audio_dir = Path(record.material_dir)
        listing = Path(record.list_path)
        opt_dir = self.upstream_root / "logs" / record.exp_name

        fingerprint = fingerprint_material(audio_dir=audio_dir, list_path=listing)
        if self.store.step_matches(job_id, "clean", fingerprint):
            # The inputs are byte-identical to last time; re-running would cost
            # minutes and produce the same artefacts.
            return {"ok": True, "error": "", "steps": [], "reused": True}

        # Anything downstream was built from the previous inputs.
        self.store.invalidate_from(job_id, "clean")

        if record.state is JobState.PENDING:
            self.store.transition(job_id, JobState.RUNNING, detail="开始清理切分。")

        results: List[Dict[str, Any]] = []
        for step in PREPROCESS_STEPS:
            try:
                spec = build_preprocess_spec(
                    upstream_root=self.upstream_root,
                    step=step,
                    exp_name=record.exp_name,
                    list_path=listing,
                    audio_dir=audio_dir,
                    opt_dir=opt_dir,
                    version="v2",
                    python_exec=training_python(self.upstream_root),
                )
            except FileNotFoundError as exc:
                self._fail(job_id, str(exc), kind="environment")
                return self._failure(str(exc))

            started = time.time()
            completed = subprocess.run(
                spec.command,
                cwd=spec.cwd,
                env={**os.environ, **spec.env},
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed = round(time.time() - started, 2)

            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip()[-600:]
                message = f"步骤 {step} 失败（退出码 {completed.returncode}）：{tail}"
                self._fail(job_id, message, kind="infrastructure")
                return self._failure(message, extra={"steps": results})

            merged = [str(path) for path in merge_all_part_files(opt_dir=opt_dir)]
            results.append(
                {"step": step, "seconds": elapsed, "merged": merged}
            )

        self.store.record_step(
            job_id,
            "clean",
            fingerprint=fingerprint,
            outputs=[str(opt_dir)],
            steps=results,
        )
        # Preprocessing is done but a human has not reviewed the text yet, so
        # the job parks in the state the architecture reserves for exactly this.
        self.store.transition(
            job_id,
            JobState.AWAITING_PROOFREAD,
            detail="清理切分完成，等待你校对文字。",
        )
        return {"ok": True, "error": "", "steps": results, "reused": False}

    # ------------------------------------------------------------------
    # Step 3: proofreading
    # ------------------------------------------------------------------

    def proofread_payload(self, job_id: str) -> Dict[str, Any]:
        """The clips and transcripts for the user to review.

        Returns:
            ``{ok, error, clips, confirmed}``; each clip carries its path and
            its current text.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        clips = self._read_clips(audio_dir=record.material_dir, list_path=record.list_path)
        step = record.steps.get("proofread") or {}
        return {
            "ok": True,
            "error": "",
            "clips": clips,
            "confirmed": bool(step.get("done")),
        }

    def submit_proofread(
        self, job_id: str, clips: Sequence[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Step 3: record the user's corrected transcripts.

        This is the only path that closes the proofreading step. There is
        deliberately no automatic variant: the requirement is that a successful
        ASR pass does not stand in for the material being correct.

        Args:
            job_id: Job to update.
            clips: ``[{"path": ..., "text": ...}]`` as reviewed by the user.

        Returns:
            ``{ok, error, clip_count}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")
        if not clips:
            return self._failure("没有收到任何校对结果。")

        lines: List[str] = []
        for clip in clips:
            path = str(clip.get("path") or "").strip()
            text = str(clip.get("text") or "").strip()
            if not path:
                return self._failure("校对结果缺少音频路径。")
            if not text:
                # An empty transcript trains the voice against nothing, which
                # upstream accepts and then produces a worse voice.
                return self._failure(f"音频 {Path(path).name} 的文字为空，请填写后再提交。")
            name = Path(path).name
            lines.append(f"{path}|Aemeath|ZH|{text}")

        listing = Path(record.list_path)
        try:
            listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            return self._failure(f"无法写入标注文件：{exc}")

        fingerprint = fingerprint_material(
            audio_dir=Path(record.material_dir), list_path=listing
        )
        self.store.record_step(
            job_id,
            "proofread",
            fingerprint=fingerprint,
            outputs=[str(listing)],
            clip_count=len(lines),
        )
        if record.state is JobState.AWAITING_PROOFREAD:
            self.store.transition(
                job_id,
                JobState.RUNNING,
                detail=f"已确认 {len(lines)} 条素材文字。",
            )
        return {"ok": True, "error": "", "clip_count": len(lines)}

    # ------------------------------------------------------------------
    # Step 4: prepare and train
    # ------------------------------------------------------------------

    def check_training_ready(self, job_id: str) -> Dict[str, Any]:
        """Whether the preprocessed artefacts the trainer needs exist.

        Named separately because it is its own wizard step: the user is shown
        exactly which file is missing instead of an upstream refusal.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        from .gpt_sovits import check_training_inputs

        opt_dir = self.upstream_root / "logs" / record.exp_name
        missing = check_training_inputs(opt_dir=opt_dir, version="v2")
        return {
            "ok": not missing,
            "error": "；".join(missing),
            "missing": missing,
            "opt_dir": str(opt_dir),
        }

    def start_training(
        self,
        job_id: str,
        *,
        epochs: int = 8,
        batch_size: int = 1,
        if_grad_ckpt: bool = False,
        timeout: float = 5400.0,
    ) -> Dict[str, Any]:
        """Step 4: start the real training run.

        Refuses for four distinct reasons, each with its own message: missing
        proofreading, a blocking preflight, an unpatched checkout, and another
        run already in flight.

        The run itself is asynchronous: a watcher thread polls the process and
        writes state back to the store, so the HTTP request returns immediately
        and closing the page does not stop anything.

        Args:
            job_id: Job to train.
            epochs: Training epochs.
            batch_size: Batch size; 1 suits an 8 GB laptop GPU.
            if_grad_ckpt: Gradient checkpointing, trading speed for memory.
            timeout: Upper bound on the watcher's patience, in seconds.

        Returns:
            ``{ok, error, retryable}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        if "proofread" not in record.steps:
            return self._failure("请先完成文字校对，再开始训练。")

        # Refuse only when a *process* is believed to be running. A job sitting
        # in `running` with no watcher here and no live PID is simply staged for
        # training — that is the normal state between preprocessing and this
        # call, and refusing it would make the wizard unusable. A job whose PID
        # is still alive but which this process does not track really was left
        # behind by an earlier process, and starting a second run against it
        # would collide in upstream's shared TEMP config.
        if record.pid is not None and self._pid_alive(record.pid):
            return self._failure(
                f"这个任务已有训练进程在运行（PID {record.pid}），不能重复启动；"
                "如需重新开始，请先停止它。"
            )

        # One run at a time: upstream's generated config lives in a shared
        # TEMP directory, so a second run would overwrite the first's settings.
        # Only a job that actually holds a process counts — see
        # `TrainingJobStore.running_jobs`.
        for other in self.store.running_jobs():
            if other.job_id != job_id:
                return self._failure(
                    f"已有训练任务在运行（{other.voice_name}，PID {other.pid}）。"
                    "上游的训练配置存放在共享临时目录，不能同时运行两个训练；"
                    "请先停止那个任务再开始。"
                )

        preflight = self.preflight()
        if not preflight["ok"]:
            detail = "；".join(preflight["blocking"])
            return self._failure(
                f"环境预检未通过，已阻止训练：{detail}",
                extra={"recovery": preflight["recovery"]},
            )

        ready = self.check_training_ready(job_id)
        if not ready["ok"]:
            return self._failure(
                "训练素材未就绪，请先完成清理切分：" + ready["error"],
                extra={"missing": ready["missing"]},
            )

        opt_dir = self.upstream_root / "logs" / record.exp_name

        try:
            spec = build_sovits_train_spec(
                upstream_root=self.upstream_root,
                exp_name=record.exp_name,
                opt_dir=opt_dir,
                version="v2",
                python_exec=training_python(self.upstream_root),
                epochs=int(epochs),
                batch_size=int(batch_size),
                if_grad_ckpt=bool(if_grad_ckpt),
            )
        except FileNotFoundError as exc:
            return self._failure(str(exc))

        job = TrainingJob(job_id=job_id, exp_name=record.exp_name, version="v2")
        try:
            job.launch(spec, popen=self._popen)
        except OSError as exc:
            self._fail(job_id, f"无法启动训练进程：{exc}", kind="infrastructure")
            return self._failure(f"无法启动训练进程：{exc}")

        # The checkout revision travels with the job so a result stays
        # attributable even after the checkout moves on.
        from .gpt_sovits import fingerprint_upstream

        upstream = fingerprint_upstream(self.upstream_root)
        revision = upstream.get("revision") or {}

        if record.state in (JobState.PENDING, JobState.FAILED, JobState.STOPPED):
            self.store.transition(job_id, JobState.RUNNING, detail="训练进程已启动。")
        updated = self.store.get(job_id)
        updated.pid = job.pid
        updated.upstream_commit = revision.get("commit", "")
        updated.upstream_dirty = revision.get("dirty")
        self.store.save(updated)

        watcher = threading.Thread(
            target=self._watch_training,
            args=(job_id, job, timeout),
            name=f"training-watch-{job_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[job_id] = watcher
        watcher.start()

        return {
            "ok": True,
            "error": "",
            "pid": job.pid,
            "retryable": False,
            "epochs": int(epochs),
            "batch_size": int(batch_size),
        }

    def _watch_training(self, job_id: str, job: TrainingJob, timeout: float) -> None:
        """Poll a running job until it settles, persisting each change.

        The artefacts are what decide success; a zero exit code alone is not
        enough, because upstream can finish cleanly having written no weight.
        """
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                status = job.refresh()
                if status.value in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(2.0)

            job.refresh()
            record = self.store.get(job_id)
            if record is None:
                return

            if job.status.value == "succeeded":
                artifacts = [
                    {"path": str(path), "size_bytes": path.stat().st_size}
                    for path in job.artifacts
                    if path.exists()
                ]
                self.store.record_step(
                    job_id,
                    "train",
                    fingerprint=record.upstream_commit or "unknown",
                    outputs=[item["path"] for item in artifacts],
                    stage="sovits",
                )
                record = self.store.get(job_id)
                record.artifacts = artifacts
                self.store.save(record)
                self.store.transition(
                    job_id,
                    JobState.SUCCEEDED,
                    detail=f"训练完成，产物 {len(artifacts)} 个。",
                    error="",
                )
            elif job.status.value == "cancelled":
                if record.state is JobState.STOPPING:
                    self.store.transition(
                        job_id, JobState.STOPPED, detail="训练已停止。"
                    )
                else:
                    self.store.transition(
                        job_id, JobState.STOPPED, detail="训练进程已结束。"
                    )
            else:
                self.store.transition(
                    job_id,
                    JobState.FAILED,
                    error=job.detail or "训练进程异常结束。",
                    error_kind=job.failure_kind or "infrastructure",
                    detail=job.detail,
                )
        except Exception as exc:  # pragma: no cover - watcher must never die silently
            logger.exception("Training watcher for {} failed.", job_id)
            record = self.store.get(job_id)
            if record is not None and record.state in (
                JobState.RUNNING,
                JobState.STOPPING,
            ):
                try:
                    self.store.transition(
                        job_id,
                        JobState.FAILED,
                        error=f"训练状态跟踪失败：{exc}",
                        error_kind="infrastructure",
                    )
                except ValueError:
                    pass
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    # ------------------------------------------------------------------
    # Stopping and retrying
    # ------------------------------------------------------------------

    def stop(
        self, job_id: str, *, killer: Optional[Callable[[int], Any]] = None
    ) -> Dict[str, Any]:
        """Stop a run this application started.

        The PID is the entire safety mechanism. Upstream kills with
        ``taskkill /t /f /pid``, which terminates the process tree, so firing at
        an unknown, stale or recycled PID would take down something the user is
        relying on — their own TTS service, most obviously.

        Args:
            job_id: Job to stop.
            killer: Kill callable; injected by tests.

        Returns:
            ``{ok, error}``. A refusal is a normal answer here, not an error
            the caller should retry.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        if record.state is not JobState.RUNNING:
            # Repeated stops are no-ops on purpose: a second kill at a PID that
            # may since have been recycled is exactly the hazard above.
            return self._failure(f"任务当前状态为 {record.state.value}，没有可停止的训练进程。")

        if record.pid is None:
            return self._failure("任务没有记录训练进程号，为避免误停其他程序，不执行停止。")

        self.store.transition(
            job_id, JobState.STOPPING, detail=f"正在停止训练进程 PID {record.pid}。"
        )

        if killer is not None:
            killer(record.pid)
        else:
            _kill_tree(record.pid)

        return {"ok": True, "error": "", "pid": record.pid}

    def retry(self, job_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Restart a run that may soundly be repeated.

        Retrying a material failure reproduces it and spends another training
        run, so it is refused with the reason rather than attempted.

        Args:
            job_id: Job to retry.
            **kwargs: Forwarded to :meth:`start_training`.

        Returns:
            ``{ok, error, retryable, reason}``. ``retryable`` describes whether
            this failure *may* be repeated; ``ok`` describes whether the restart
            actually happened. They differ when the failure is retryable but the
            job is no longer startable — an unproofread job, for instance — and
            keeping them separate is what lets the UI offer an accurate button.
        """
        record = self.store.get(job_id)
        if record is None:
            return {**self._failure("找不到这个训练任务。"), "retryable": False}

        verdict = _retry_verdict(record)
        if not verdict["retryable"]:
            return {
                "ok": False,
                "error": verdict["reason"],
                "retryable": False,
                "reason": verdict["reason"],
            }

        # Only steps whose inputs still match are reused; the training step
        # itself is dropped so the run actually happens again.
        self.store.invalidate_from(job_id, "train")
        result = self.start_training(job_id, **kwargs)
        # The verdict stands on its own: whether the restart succeeded is what
        # `ok` reports, not whether a retry was permitted.
        result["retryable"] = True
        result["reason"] = verdict["reason"]
        return result

    # ------------------------------------------------------------------
    # Steps 5-6: audition and apply
    # ------------------------------------------------------------------

    async def audition(
        self, job_id: str, *, text: str, voice_service: Any = None
    ) -> Dict[str, Any]:
        """Step 5: synthesise a sample with the new voice, without applying it.

        The weights are loaded into the running api_v2 service, which changes
        what the *service* holds but not what Aemeath is configured to use — so
        the character keeps its current voice until the user applies.

        Args:
            job_id: Job whose artefacts to audition.
            text: Sentence to synthesise.
            voice_service: Voice service override; injected by tests.

        Returns:
            ``{ok, error, audio, media_type, weights}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        artifacts = record.artifacts or []
        weight = _pick_sovits_weight(artifacts)
        if not weight:
            return self._failure("这次训练没有可用的声音权重，无法试听。")

        service = voice_service or self._voice_service()
        # ``audition_weights`` is a coroutine: it talks to the running api_v2
        # service to load the weights and synthesise the sample.
        return await service.audition_weights(record, weight, text=text)

    async def apply(
        self,
        job_id: str,
        *,
        preset_name: str = "",
        text: str = "",
        voice_service: Any = None,
    ) -> Dict[str, Any]:
        """Step 6: make the trained voice the active one.

        This delegates to the existing preset machinery rather than writing the
        config here. V2-T02 already made applying transactional — the candidate
        preset is validated and the adapter constructed before anything is
        written — and a second implementation would eventually disagree with
        the first about what a valid voice is.

        Args:
            job_id: Job whose artefacts to apply.
            preset_name: Name for the saved preset; defaults to the voice name.
            text: Reference transcript used for the preset.
            voice_service: Voice service override; injected by tests.

        Returns:
            ``{ok, error, preset_id, restart_required, conflict}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        artifacts = record.artifacts or []
        weight = _pick_sovits_weight(artifacts)
        if not weight:
            return self._failure("这次训练没有可用的声音权重，无法应用。")

        reference = record.ref_audio_path or _pick_reference(record)
        prompt_text = record.prompt_text or _reference_transcript(record, reference)
        if not reference or not prompt_text:
            return self._failure(
                "缺少参考音频或它的转写文字，无法应用这个声音。请在试听步骤中选择参考片段。"
            )

        service = voice_service or self._voice_service()

        created = service.create_preset(
            name=(preset_name or record.voice_name or "训练的声音").strip(),
            api_url="http://127.0.0.1:9880/tts",
            ref_audio_path=reference,
            prompt_text=prompt_text,
            expected_revision="",
        )
        if not created.get("ok"):
            # Nothing was written; the previously applied voice still applies.
            return self._failure(
                created.get("error") or "无法保存这个声音的预设。",
                extra={"conflict": bool(created.get("conflict"))},
            )

        preset_id = created.get("preset_id", "")
        # ``VoiceService.apply`` is a coroutine (it validates through the real
        # adapter), so it has to be awaited rather than called.
        result = await service.apply(preset_id, expected_revision="")
        if not result.get("ok"):
            return self._failure(
                result.get("error") or "应用失败，原来的音色保持不变。",
                extra={"conflict": bool(result.get("conflict"))},
            )

        updated = self.store.get(job_id)
        updated.applied_preset_id = preset_id
        self.store.save(updated)
        self.store.record_step(
            job_id,
            "apply",
            fingerprint=weight,
            outputs=[preset_id],
        )
        return {
            "ok": True,
            "error": "",
            "preset_id": preset_id,
            "restart_required": bool(result.get("restart_required")),
            "conflict": False,
        }

    def set_reference(
        self, job_id: str, *, ref_audio_path: str, prompt_text: str = ""
    ) -> Dict[str, Any]:
        """Choose which clip a trained voice is auditioned and applied with.

        A trained voice needs a reference clip at inference time: GPT-SoVITS
        clones the timbre from it, so the choice is part of the voice rather
        than a detail of one request. Recording it on the job keeps audition and
        apply consistent — otherwise the user could approve one reference and
        apply another.

        Args:
            job_id: Job to update.
            ref_audio_path: Clip to use as the reference.
            prompt_text: Its transcript; derived from the material when omitted.

        Returns:
            ``{ok, error, ref_audio_path, prompt_text}``.
        """
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")

        path = str(ref_audio_path or "").strip()
        if not path:
            return self._failure("参考音频不能为空。")
        if not Path(path).is_file():
            return self._failure(f"参考音频不存在：{path}")

        text = str(prompt_text or "").strip() or _reference_transcript(record, path)
        if not text:
            # A reference without its true transcript makes the cloned voice
            # drift audibly, so this is an error rather than a blank default.
            return self._failure("参考音频缺少对应的转写文字。")

        record.ref_audio_path = path
        record.prompt_text = text
        self.store.save(record)
        return {"ok": True, "error": "", "ref_audio_path": path, "prompt_text": text}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def overview(self, *, job_id: str = "") -> Dict[str, Any]:
        """Everything the wizard page needs in one round trip.

        Reports ``available`` separately from the job list so "nothing has been
        started yet" and "the store cannot be read" are distinguishable — the
        first needs patience, the second needs a fix.
        """
        try:
            jobs = self.store.list_jobs()
        except Exception as exc:  # pragma: no cover - storage failure
            logger.exception("Could not read the training job store.")
            return {
                "ok": False,
                "available": False,
                "error": f"无法读取训练任务记录：{exc}",
                "jobs": [],
                "steps": list(WIZARD_STEPS),
                "current": None,
            }

        current = None
        if job_id:
            record = self.store.get(job_id)
            if record is None:
                return {
                    "ok": False,
                    "available": True,
                    "error": "找不到这个训练任务。",
                    "jobs": [self._view(job) for job in jobs],
                    "steps": list(WIZARD_STEPS),
                    "current": None,
                }
            current = self._view(record)

        return {
            "ok": True,
            "available": True,
            "error": "",
            "jobs": [self._view(job) for job in jobs],
            "steps": list(WIZARD_STEPS),
            "current": current,
        }

    def detail(self, job_id: str) -> Dict[str, Any]:
        """One job's full view, including its steps and artefacts."""
        record = self.store.get(job_id)
        if record is None:
            return self._failure("找不到这个训练任务。")
        return {"ok": True, "error": "", "job": self._view(record)}

    @staticmethod
    def _view(record: TrainingJobRecord) -> Dict[str, Any]:
        """Render a record for the page.

        Note what is *not* here: a percentage. Upstream reports no reliable
        progress, and the architecture forbids inventing one, so the page shows
        the step and the run state instead.
        """
        return {
            "job_id": record.job_id,
            "voice_name": record.voice_name,
            "exp_name": record.exp_name,
            "state": record.state.value,
            "stage": record.stage,
            "detail": record.detail,
            "error": record.error,
            "error_kind": record.error_kind,
            "pid": record.pid,
            "steps": [
                {
                    "id": step,
                    "label": label,
                    "done": bool((record.steps.get(step) or {}).get("done")),
                    "current": step == record.stage,
                }
                for step, label in (
                    ("import", "导入素材"),
                    ("clean", "清理切分"),
                    ("proofread", "校对文字"),
                    ("prepare", "准备训练集"),
                    ("train", "训练声音"),
                    ("audition", "试听"),
                    ("apply", "应用"),
                )
            ],
            "artifacts": record.artifacts,
            "applied_preset_id": record.applied_preset_id,
            "upstream_commit": record.upstream_commit,
            "upstream_dirty": record.upstream_dirty,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _voice_service(self) -> Any:
        """The existing preset service, which owns what "apply" means."""
        from ..management.voices import VoiceService

        return VoiceService(self.config_path)

    def _read_clips(self, *, audio_dir: str, list_path: str) -> List[Dict[str, Any]]:
        """Read the annotation file into reviewable clip entries."""
        clips: List[Dict[str, Any]] = []
        try:
            raw = Path(list_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return clips

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            name = parts[0]
            clip_path = Path(name)
            if not clip_path.is_absolute():
                clip_path = Path(audio_dir) / name
            clips.append(
                {
                    "path": str(clip_path),
                    "name": clip_path.name,
                    "text": parts[3],
                    "exists": clip_path.exists(),
                }
            )
        return clips

    def _fail(self, job_id: str, message: str, *, kind: str = "infrastructure") -> None:
        """Move a job to failed, tolerating a state that cannot reach it."""
        try:
            self.store.transition(
                job_id, JobState.FAILED, error=message, error_kind=kind, detail=message
            )
        except (KeyError, ValueError):
            logger.warning("Could not mark job {} failed: {}", job_id, message)

    @staticmethod
    def _failure(message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A refusal shaped like every other result."""
        return {"ok": False, "error": message, **(extra or {})}


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def default_pid_alive(pid: int) -> bool:
    """Whether a PID is running, without importing psutil.

    ``tasklist`` is used on Windows and ``os.kill(pid, 0)`` elsewhere; both are
    available wherever this runs, and a failure to determine liveness is
    reported as "not alive" so reconciliation errs towards *not* claiming a
    process is running.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in result.stdout
    try:  # pragma: no cover - Windows is the supported target
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_tree(pid: int) -> None:
    """Terminate a process tree, matching upstream's own mechanism."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/t", "/f", "/pid", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:  # pragma: no cover - Windows is the supported target
        import signal

        os.kill(pid, signal.SIGTERM)


def _retry_verdict(record: TrainingJobRecord) -> Dict[str, Any]:
    """Whether a job may soundly be started again.

    Mirrors :meth:`aemeath.training.gpt_sovits.TrainingJob.retry_verdict`, but
    works from the persisted record — after a restart there is no
    :class:`TrainingJob` object left to ask.
    """
    if record.state in (JobState.RUNNING, JobState.STOPPING):
        return {
            "retryable": False,
            "reason": "任务仍在运行，不能重复启动；上游临时配置存放在共享 TEMP 目录，并发训练会互相覆盖。",
        }
    if record.state is JobState.SUCCEEDED:
        return {"retryable": False, "reason": "任务已成功，无需重试。"}
    if record.state is JobState.PENDING:
        return {"retryable": False, "reason": "任务尚未启动。"}
    if record.state is JobState.AWAITING_PROOFREAD:
        return {"retryable": False, "reason": "任务正在等待你校对文字。"}
    if record.error_kind == "material":
        return {
            "retryable": False,
            "reason": "素材或产物问题导致失败，直接重试会复现同一结果；请先修正素材。",
        }
    if record.state is JobState.STOPPED:
        return {"retryable": True, "reason": "任务已被停止，可以重新开始训练。"}
    return {"retryable": True, "reason": "基础设施类失败，可以重新开始训练。"}


def _material_stats(*, audio_dir: Path, list_path: Path) -> tuple[int, float]:
    """Count clips and total duration from the annotation file."""
    from .gpt_sovits import _wav_duration  # noqa: PLC2701 - same package

    count = 0
    total = 0.0
    try:
        raw = Path(list_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0.0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        count += 1
        candidate = Path(parts[0])
        if not candidate.is_absolute():
            candidate = Path(audio_dir) / parts[0]
        total += _wav_duration(candidate)
    return count, total


def _pick_sovits_weight(artifacts: Sequence[Dict[str, Any]]) -> str:
    """Choose the SoVITS weight to use from a job's artefacts.

    SoVITS weights are what the api_v2 service loads for timbre; the newest one
    is the right choice for a freshly finished run.
    """
    candidates = [
        str(item.get("path") or "")
        for item in artifacts
        if str(item.get("path") or "").lower().endswith(".pth")
        and "sovits" in str(item.get("path") or "").lower().replace("\\", "/")
    ]
    if not candidates:
        candidates = [
            str(item.get("path") or "")
            for item in artifacts
            if str(item.get("path") or "").lower().endswith(".pth")
        ]
    if not candidates:
        return ""
    try:
        return max(candidates, key=lambda path: Path(path).stat().st_mtime)
    except OSError:
        return candidates[-1]


def _pick_reference(record: TrainingJobRecord) -> str:
    """The first usable clip, used as the reference audio when none was chosen."""
    for clip in _plain_read_clips(record):
        if clip.get("exists"):
            return str(clip["path"])
    return ""


def _plain_read_clips(record: TrainingJobRecord) -> List[Dict[str, Any]]:
    """Read clips without needing a service instance."""
    clips: List[Dict[str, Any]] = []
    try:
        raw = Path(record.list_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return clips
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        path = Path(parts[0])
        if not path.is_absolute():
            path = Path(record.material_dir) / parts[0]
        clips.append({"path": str(path), "text": parts[3], "exists": path.exists()})
    return clips


def _reference_transcript(record: TrainingJobRecord, reference: str) -> str:
    """The transcript belonging to the chosen reference clip."""
    for clip in _plain_read_clips(record):
        if str(clip["path"]) == str(reference):
            return str(clip["text"])
    return ""


def _environment_recovery(problem: str) -> str:
    """A user-facing action for one blocking preflight item."""
    if "GPU" in problem or "CUDA" in problem:
        return "确认 NVIDIA 驱动与 CUDA 可用，并关闭占用显存的程序后重试。"
    if "torch" in problem:
        return "按 docs/runbook.md 修复 GPT-SoVITS 检出的 Python 环境。"
    if "上游" in problem or "缺少脚本" in problem:
        return "确认 GPT-SoVITS 检出完整，或设置环境变量 GPT_SOVITS_DIR 指向正确的目录。"
    return "按《启动手册》检查本机环境后重试。"


__all__ = [
    "DDP_PATCH_PATH",
    "DEFAULT_EXP_NAME",
    "PREPROCESS_STEPS",
    "WIZARD_STEPS",
    "TrainingWizardService",
    "check_ddp_guard",
    "check_ffmpeg",
    "default_pid_alive",
    "fingerprint_file",
    "fingerprint_material",
]
