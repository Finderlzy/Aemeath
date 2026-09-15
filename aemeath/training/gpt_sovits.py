"""GPT-SoVITS training orchestration, measured against the real local install.

Why this module exists
----------------------
Aemeath's TTS runs against a local GPT-SoVITS ``api_v2`` server. Producing a
*new* voice means driving that checkout's own training pipeline, and V2-T05 is
the task that establishes what that pipeline can actually support before the
V2-T06 wizard is designed around it.

Three properties of the upstream training path shape everything below. Each was
read out of the pinned checkout at ``E:/WorkSpace/Tools/GPT-SoVITS`` and is
pinned by ``tests/integration/test_training_gpt_sovits_contract.py``:

1. **Training is a subprocess, not an API.** Upstream's WebUI builds a command
   string and runs ``Popen(cmd, shell=True)``; there is no importable training
   entry point. Any adapter therefore *is* process orchestration, and the
   command it builds must match what upstream itself would build.
2. **The inputs travel through the environment.** ``prepare_datasets/1-get-text.py``
   and friends read ``inp_text`` / ``inp_wav_dir`` / ``exp_name`` / ``opt_dir``
   from ``os.environ``, not from ``argv``. Passing them as arguments runs the
   script with everything unset.
3. **The temporary config is the only thing separating two experiments.**
   Upstream writes the generated config to a shared ``TEMP/`` directory, so two
   concurrent runs would overwrite each other's settings. This is recorded as a
   real constraint on the wizard rather than papered over.

Wording note: this module reports *what was observed*. Where the local install
could not settle a question, the result is recorded as unverified rather than
assumed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Default location of the pinned GPT-SoVITS checkout on this machine.
DEFAULT_UPSTREAM_ROOT = Path(r"E:\WorkSpace\Tools\GPT-SoVITS")

#: Environment variable that overrides the checkout location.
UPSTREAM_ROOT_ENV = "GPT_SOVITS_DIR"

#: Scripts the training pipeline depends on, relative to the checkout root.
REQUIRED_SCRIPTS = (
    "GPT_SoVITS/s1_train.py",
    "GPT_SoVITS/s2_train.py",
    "GPT_SoVITS/prepare_datasets/1-get-text.py",
    "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
    "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
)

#: Weight directories per model version, as upstream defines them in ``config.py``.
SOVITS_WEIGHT_DIRS: Dict[str, str] = {
    "v1": "SoVITS_weights",
    "v2": "SoVITS_weights_v2",
    "v3": "SoVITS_weights_v3",
    "v4": "SoVITS_weights_v4",
    "v2Pro": "SoVITS_weights_v2Pro",
    "v2ProPlus": "SoVITS_weights_v2ProPlus",
}

GPT_WEIGHT_DIRS: Dict[str, str] = {
    "v1": "GPT_weights",
    "v2": "GPT_weights_v2",
    "v3": "GPT_weights_v3",
    "v4": "GPT_weights_v4",
    "v2Pro": "GPT_weights_v2Pro",
    "v2ProPlus": "GPT_weights_v2ProPlus",
}

#: The preprocessed artefacts upstream requires before ``s2_train.py`` will run.
#:
#: ``tools.my_utils.check_for_existance(is_train=True)`` appends exactly these to
#: the experiment directory and refuses the run if any is missing.
REQUIRED_TRAINING_ARTEFACTS = (
    "2-name2text.txt",
    "3-bert",
    "4-cnhubert",
    "5-wav32k",
    "6-name2semantic.tsv",
)

#: Minimum number of clips assumed necessary for a usable voice.
#:
#: This is a floor for *refusing obviously hopeless input*, not a quality claim:
#: a real voice needs far more. It exists so the wizard can stop before spending
#: a training run on one clip.
MIN_CLIPS = 4

#: Minimum total audio duration, in seconds, for the same reason as ``MIN_CLIPS``.
MIN_TOTAL_SECONDS = 10.0


class TrainingStatus(str, Enum):
    """Lifecycle of one training run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PreprocessSpec:
    """One upstream dataset-preparation step, ready to execute.

    Attributes:
        step: Step name, e.g. ``1-get-text``.
        command: Full argv, including the Python interpreter.
        env: Environment additions the script reads its inputs from.
        cwd: Working directory (the checkout root).
        outputs: Paths this step is expected to produce.
    """

    step: str
    command: List[str]
    env: Dict[str, str]
    cwd: str
    outputs: List[Path] = field(default_factory=list)


@dataclass(frozen=True)
class TrainingSpec:
    """One upstream training stage, ready to execute.

    Attributes:
        stage: ``sovits`` or ``gpt``.
        command: Full argv, including the Python interpreter.
        env: Environment additions.
        cwd: Working directory (the checkout root).
        config_path: Generated temporary config the stage was pointed at.
        outputs: Directories the stage writes weights into.
    """

    stage: str
    command: List[str]
    env: Dict[str, str]
    cwd: str
    config_path: Optional[str] = None
    outputs: List[Path] = field(default_factory=list)


def resolve_upstream_root(upstream_root: Optional[Path] = None) -> Path:
    """Locate the GPT-SoVITS checkout.

    Resolution order, highest priority first: explicit argument, the
    ``GPT_SOVITS_DIR`` environment variable, then the machine default. This
    mirrors ``scripts/start_gpt_sovits.ps1`` so the server and the trainer can
    never disagree about which checkout is in use.

    Args:
        upstream_root: Explicit override, if the caller has one.

    Returns:
        The checkout path (not necessarily existing).
    """
    if upstream_root is not None:
        return Path(upstream_root)
    from_env = os.getenv(UPSTREAM_ROOT_ENV)
    if from_env:
        return Path(from_env)
    return DEFAULT_UPSTREAM_ROOT


def _git_revision(root: Path) -> Dict[str, Any]:
    """Read the checkout's git revision, if it is one.

    The pinned revision matters: V2-T05's whole result is only meaningful for a
    known version of upstream.

    Args:
        root: Checkout root.

    Returns:
        ``{"commit": ..., "dirty": ..., "error": ...}``; empty values when git
        is unavailable or the directory is not a repository.
    """
    if not (root / ".git").exists():
        return {"commit": "", "dirty": None, "error": "not a git checkout"}
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return {"commit": "", "dirty": None, "error": str(exc)}

    if commit.returncode != 0:
        return {"commit": "", "dirty": None, "error": commit.stderr.strip()}
    return {
        "commit": commit.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "error": "",
    }


def fingerprint_upstream(upstream_root: Optional[Path] = None) -> Dict[str, Any]:
    """Describe the local checkout well enough to pin a verification result.

    Args:
        upstream_root: Checkout override; resolved via
            :func:`resolve_upstream_root` when omitted.

    Returns:
        A dict with ``available``, ``root``, ``revision``, ``missing``,
        ``weight_dirs`` and ``python_exec``. ``available`` is ``False`` with a
        non-empty ``missing`` when any required script is absent, so a
        half-cloned checkout fails at precheck rather than mid-run.
    """
    root = resolve_upstream_root(upstream_root)

    missing: List[str] = []
    if not root.exists():
        return {
            "available": False,
            "root": str(root),
            "revision": {"commit": "", "dirty": None, "error": "missing"},
            "missing": [f"上游目录不存在: {root}"],
            "weight_dirs": {},
            "python_exec": "",
        }

    for relative in REQUIRED_SCRIPTS:
        if not (root / relative).exists():
            missing.append(f"缺少脚本: {relative}")

    # The venv interpreter is what upstream's own WebUI would use. A missing one
    # is reported separately rather than folded into ``missing``: the scripts are
    # what make the checkout usable, and the caller can still run them with the
    # interpreter currently executing Aemeath.
    python_exec = root / ".venv" / "Scripts" / "python.exe"
    if not python_exec.exists():
        python_exec = root / "runtime" / "python.exe"

    return {
        "available": not missing,
        "root": str(root),
        "revision": _git_revision(root),
        "missing": missing,
        "weight_dirs": {
            "sovits": dict(SOVITS_WEIGHT_DIRS),
            "gpt": dict(GPT_WEIGHT_DIRS),
        },
        "python_exec": str(python_exec) if python_exec.exists() else "",
        "python_exec_missing": not python_exec.exists(),
    }


def training_python(upstream_root: Optional[Path] = None) -> str:
    """The interpreter used to run upstream training scripts.

    Args:
        upstream_root: Checkout override.

    Returns:
        Path to the checkout's interpreter, falling back to the running one so
        the caller gets a usable value rather than an empty command.
    """
    fingerprint = fingerprint_upstream(upstream_root)
    return str(fingerprint.get("python_exec") or sys.executable)


def _parse_listing(list_path: Path) -> List[str]:
    """Read a GPT-SoVITS ``.list`` file.

    Each non-empty line is ``path|speaker|language|text``.

    Args:
        list_path: The listing file.

    Returns:
        The non-empty lines, stripped.
    """
    try:
        raw = list_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def validate_material(
    *,
    audio_dir: Path,
    list_path: Path,
    min_clips: int = MIN_CLIPS,
    min_total_seconds: float = MIN_TOTAL_SECONDS,
) -> List[str]:
    """Check training material *before* a run is started.

    Args:
        audio_dir: Directory holding the clips.
        list_path: The ``.list`` annotation file.
        min_clips: Minimum acceptable number of annotated clips.
        min_total_seconds: Minimum acceptable total duration.

    Returns:
        A list of human-readable problems; empty means usable.
    """
    problems: List[str] = []

    if not audio_dir.exists():
        problems.append(f"素材目录不存在: {audio_dir}")
    if not list_path.exists():
        problems.append(f"标注文件不存在: {list_path}")
    if problems:
        return problems

    lines = _parse_listing(list_path)
    if not lines:
        problems.append(f"标注文件为空: {list_path}")
        return problems

    total_seconds = 0.0
    for line in lines:
        parts = line.split("|")
        if len(parts) < 4:
            problems.append(f"标注行格式不正确（应为 路径|说话人|语言|文本）: {line[:60]}")
            continue
        clip = _resolve_clip(audio_dir=audio_dir, wav_name=parts[0])
        if not clip.exists():
            problems.append(f"标注引用的音频不存在: {clip}")
            continue
        total_seconds += _wav_duration(clip)

    if len(lines) < min_clips:
        problems.append(
            f"素材条数不足：{len(lines)} 条，至少需要 {min_clips} 条才能开始训练。"
        )
    if total_seconds and total_seconds < min_total_seconds:
        problems.append(
            f"素材总时长不足：{total_seconds:.1f} 秒，至少需要 {min_total_seconds:.0f} 秒。"
        )

    return problems


def _wav_duration(path: Path) -> float:
    """Duration of a WAV file in seconds, or ``0.0`` if unreadable.

    Uses the standard library only: this runs in precheck, where adding an audio
    dependency would make the check itself a failure mode.

    Args:
        path: WAV file.

    Returns:
        Duration in seconds, or ``0.0`` when the header cannot be read.
    """
    import wave

    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if not rate:
                return 0.0
            return handle.getnframes() / float(rate)
    except (OSError, wave.Error, EOFError):
        return 0.0


def _resolve_clip(*, audio_dir: Path, wav_name: str) -> Path:
    """Resolve one listing entry to a file, following upstream's own rule.

    ``2-get-hubert-wav32k.py`` does ``wav_path = "%s/%s" % (inp_wav_dir, wav_name)``
    but falls back to ``wav_name`` verbatim when that path does not exist, so an
    entry holding an absolute path is used as-is. Validation mirrors that rule
    exactly, otherwise it would reject material the trainer accepts (or accept
    material it rejects).

    Args:
        audio_dir: The ``inp_wav_dir`` passed to the pipeline.
        wav_name: First field of a listing line.

    Returns:
        The path upstream would read.
    """
    candidate = Path(wav_name)
    if candidate.is_absolute():
        return candidate
    joined = audio_dir / wav_name
    return joined


def _s2_config_relative(version: str) -> str:
    """The s2 config path upstream expects, relative to the checkout root.

    The WebUI passes exactly this value as ``s2config_path``, and
    ``3-get-semantic.py`` opens it during preprocessing to rebuild the
    quantizer. Only the Pro variants have their own template.

    Args:
        version: Model version.

    Returns:
        A path relative to the checkout root.
    """
    if version in {"v2Pro", "v2ProPlus"}:
        return f"GPT_SoVITS/configs/s2{version}.json"
    return "GPT_SoVITS/configs/s2.json"


def merge_part_files(
    *,
    opt_dir: Path,
    stem: str,
    suffix: str,
    parts: Sequence[int] = (0,),
    newline_terminated: bool = True,
) -> Path:
    """Combine per-part artefact files into the merged name training expects.

    Both multi-part outputs follow the same shape. The scripts write
    ``<stem>-<part><suffix>``; the *WebUI* concatenates them into
    ``<stem><suffix>`` and deletes the part files. A command-line caller gets
    only the part file, so the merge has to be reproduced here or
    ``s2_train.py`` refuses to run with "以下文件或文件夹不存在".

    Args:
        opt_dir: Experiment artefact directory.
        stem: Base name without the part index, e.g. ``2-name2text``.
        suffix: Extension including the dot, e.g. ``.txt``.
        parts: Part indices to merge, in order.
        newline_terminated: Whether to end the merged file with a newline
            (text does; the TSV does not).

    Returns:
        Path to the merged file.
    """
    merged: List[str] = []
    consumed: List[Path] = []
    for index in parts:
        part = opt_dir / f"{stem}-{index}{suffix}"
        if not part.exists():
            continue
        raw = part.read_text(encoding="utf-8", errors="replace")
        merged.extend(line for line in raw.strip("\n").split("\n") if line.strip())
        consumed.append(part)

    target = opt_dir / f"{stem}{suffix}"
    body = "\n".join(merged)
    if newline_terminated and body:
        body += "\n"
    target.write_text(body, encoding="utf-8")

    # Upstream removes the part files once merged; matching that keeps reruns
    # idempotent instead of appending the same lines again.
    for part in consumed:
        try:
            part.unlink()
        except OSError:  # pragma: no cover - non-fatal cleanup
            pass

    return target


def merge_phoneme_files(*, opt_dir: Path, parts: Sequence[int] = (0,)) -> Path:
    """Merge ``2-name2text-<part>.txt`` into ``2-name2text.txt``.

    Args:
        opt_dir: Experiment artefact directory.
        parts: Part indices to merge.

    Returns:
        Path to the merged file.
    """
    return merge_part_files(opt_dir=opt_dir, stem="2-name2text", suffix=".txt", parts=parts)


def merge_semantic_files(*, opt_dir: Path, parts: Sequence[int] = (0,)) -> Path:
    """Merge ``6-name2semantic-<part>.tsv`` into ``6-name2semantic.tsv``.

    Args:
        opt_dir: Experiment artefact directory.
        parts: Part indices to merge.

    Returns:
        Path to the merged file.
    """
    return merge_part_files(
        opt_dir=opt_dir,
        stem="6-name2semantic",
        suffix=".tsv",
        parts=parts,
        newline_terminated=False,
    )


def merge_all_part_files(*, opt_dir: Path, parts: Sequence[int] = (0,)) -> List[Path]:
    """Apply both merges, skipping whichever stage has not produced parts yet.

    The preprocessing stages run in sequence, so at any moment only some parts
    exist. Merging blindly would create empty files that later look like
    completed artefacts.

    Args:
        opt_dir: Experiment artefact directory.
        parts: Part indices to merge.

    Returns:
        The merged files that were actually written.
    """
    specs = (
        ("2-name2text", ".txt", True),
        ("6-name2semantic", ".tsv", False),
    )
    produced: List[Path] = []
    for stem, suffix, newline_terminated in specs:
        has_parts = any((opt_dir / f"{stem}-{index}{suffix}").exists() for index in parts)
        if not has_parts:
            continue
        produced.append(
            merge_part_files(
                opt_dir=opt_dir,
                stem=stem,
                suffix=suffix,
                parts=parts,
                newline_terminated=newline_terminated,
            )
        )
    return produced


def _preprocess_filename(step: str) -> str:
    """Map a step name to its script filename.

    Args:
        step: Step name, e.g. ``1-get-text``.

    Returns:
        The script's filename.
    """
    return f"{step}.py"


def build_preprocess_spec(
    *,
    upstream_root: Path,
    step: str,
    exp_name: str,
    list_path: Path,
    audio_dir: Path,
    opt_dir: Path,
    version: str = "v2",
    python_exec: Optional[str] = None,
    gpu_numbers: str = "0",
) -> PreprocessSpec:
    """Build one dataset-preparation step.

    Upstream reads these inputs from ``os.environ`` (see the module docstring),
    so they are placed in ``env`` and **not** appended to the command.

    Args:
        upstream_root: Checkout root; also the working directory.
        step: Step name, e.g. ``1-get-text``.
        exp_name: Experiment name (groups all artefacts for one voice).
        list_path: The ``.list`` annotation file.
        audio_dir: Directory holding the source clips.
        opt_dir: Where preprocessed artefacts are written.
        version: Model version, e.g. ``v2``.
        python_exec: Interpreter override.
        gpu_numbers: GPU index list in upstream's dash form.

    Returns:
        A :class:`PreprocessSpec` ready to execute.

    Raises:
        FileNotFoundError: If the step's script is not in the checkout.
    """
    script = upstream_root / "GPT_SoVITS" / "prepare_datasets" / _preprocess_filename(step)
    if not script.exists():
        raise FileNotFoundError(f"上游预处理脚本不存在: {script}")

    # ``v1`` ships the generator as a bare file; newer versions keep it inside
    # the ``gsv-v2final-pretrained`` directory.
    if version == "v1":
        pretrained_s2g = (
            upstream_root / "GPT_SoVITS" / "pretrained_models" / "s2G2333k.pth"
        )
    else:
        pretrained_s2g = (
            upstream_root
            / "GPT_SoVITS"
            / "pretrained_models"
            / "gsv-v2final-pretrained"
            / "s2G2333k.pth"
        )

    env = {
        "inp_text": str(list_path),
        "inp_wav_dir": str(audio_dir),
        "exp_name": exp_name,
        "opt_dir": str(opt_dir),
        "version": version,
        "i_part": "0",
        "all_parts": "1",
        "gpu_numbers": gpu_numbers,
        "_CUDA_VISIBLE_DEVICES": gpu_numbers,
        "bert_pretrained_dir": str(
            upstream_root / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large"
        ),
        "cnhubert_base_dir": str(
            upstream_root / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base"
        ),
        "pretrained_s2G": str(pretrained_s2g),
        # ``3-get-semantic.py`` loads this JSON directly and raises
        # ``TypeError: expected str ... not NoneType`` without it. The WebUI
        # passes it in; a command-line-only caller has to supply it too.
        "s2config_path": _s2_config_relative(version),
    }

    return PreprocessSpec(
        step=step,
        command=[python_exec or training_python(upstream_root), "-s", str(script)],
        env=env,
        cwd=str(upstream_root),
        outputs=_preprocess_outputs(step=step, opt_dir=opt_dir, version=version),
    )


def _preprocess_outputs(*, step: str, opt_dir: Path, version: str) -> List[Path]:
    """Artefacts a preprocessing step is expected to create.

    Args:
        step: Step name.
        opt_dir: Experiment artefact directory.
        version: Model version.

    Returns:
        Expected output paths (not guaranteed to exist yet).
    """
    # ``1-get-text`` writes per-part text files; the merged name is what the
    # later stages and the trainer read.
    if step == "1-get-text":
        return [opt_dir / "2-name2text.txt", opt_dir / "2-name2text-0.txt"]
    if step == "2-get-hubert-wav32k":
        return [opt_dir / "3-bert", opt_dir / "4-cnhubert", opt_dir / "5-wav32k"]
    if step == "3-get-semantic":
        return [opt_dir / "6-name2semantic.tsv"]
    return []


def check_training_inputs(*, opt_dir: Path, version: str = "v2") -> List[str]:
    """Report which preprocessed artefacts are missing before training.

    Mirrors upstream's ``check_for_existance(is_train=True)`` so the wizard can
    name the missing file instead of surfacing an upstream ``gr.Warning`` that
    never reaches Aemeath.

    Args:
        opt_dir: Experiment artefact directory.
        version: Model version (reserved; artefact names are version-stable).

    Returns:
        A list of missing artefact paths; empty means training may start.
    """
    missing: List[str] = []
    if not opt_dir.exists():
        return [f"实验目录不存在: {opt_dir}"]
    for name in REQUIRED_TRAINING_ARTEFACTS:
        if not (opt_dir / name).exists():
            missing.append(f"缺少训练所需产物: {opt_dir / name}")
    return missing


def pretrained_sovits_paths(*, upstream_root: Path, version: str = "v2") -> Dict[str, Path]:
    """Locate the pretrained generator/discriminator for a version.

    Fine-tuning starts from these; ``s2_train.py`` reads them out of the
    generated config's ``train`` block and *requires the keys to exist*, raising
    ``AttributeError: 'HParams' object has no attribute 'pretrained_s2G'`` when
    they are absent. Upstream's WebUI sets both when it builds the same config.

    Args:
        upstream_root: Checkout root.
        version: Model version.

    Returns:
        ``{"s2G": Path, "s2D": Path}``; paths may not exist, and callers should
        check before starting a run.
    """
    if version == "v1":
        base = upstream_root / "GPT_SoVITS" / "pretrained_models"
        name_g, name_d = "s2G2333k.pth", "s2D2333k.pth"
    else:
        base = upstream_root / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained"
        name_g, name_d = "s2G2333k.pth", "s2D2333k.pth"
    return {"s2G": base / name_g, "s2D": base / name_d}


def build_sovits_train_spec(
    *,
    upstream_root: Path,
    exp_name: str,
    opt_dir: Path,
    version: str = "v2",
    python_exec: Optional[str] = None,
    epochs: int = 8,
    batch_size: int = 1,
    work_dir: Optional[Path] = None,
    gpu_numbers: str = "0",
    save_every_epoch: int = 1,
    text_low_lr_rate: float = 0.4,
    if_save_latest: bool = True,
    if_save_every_weights: bool = True,
    if_grad_ckpt: bool = False,
) -> TrainingSpec:
    """Build the SoVITS (``s2``) training stage.

    Upstream starts from a JSON config template and overwrites the fields that
    describe *this* experiment, then writes the result to ``TEMP/`` and runs
    ``s2_train.py --config <that file>``. The same shape is reproduced here, with
    the temporary file written under the caller's ``work_dir`` so verification
    runs stay isolated from the real ``TEMP/``.

    Args:
        upstream_root: Checkout root; also the working directory.
        exp_name: Experiment name.
        opt_dir: Experiment artefact directory (the preprocessed inputs).
        version: Model version, e.g. ``v2``.
        python_exec: Interpreter override.
        epochs: Training epochs.
        batch_size: Batch size; keep at 1 for the 8 GB laptop GPU.
        work_dir: Where the generated config is written. Defaults to the
            checkout's own ``TEMP/``, matching upstream.
        gpu_numbers: GPU index list in upstream's dash form.
        save_every_epoch: Checkpoint interval.
        text_low_lr_rate: Upstream text-branch learning-rate factor.
        if_save_latest: Keep only the newest checkpoint.
        if_save_every_weights: Also emit a directly loadable weight.
        if_grad_ckpt: Gradient checkpointing (trades speed for memory).

    Returns:
        A :class:`TrainingSpec` ready to execute.

    Raises:
        FileNotFoundError: If the training script or config template is absent.
    """
    script = upstream_root / "GPT_SoVITS" / "s2_train.py"
    if not script.exists():
        raise FileNotFoundError(f"上游训练脚本不存在: {script}")

    template = upstream_root / "GPT_SoVITS" / "configs" / "s2.json"
    if not template.exists():
        raise FileNotFoundError(f"上游训练配置模板不存在: {template}")

    config = json.loads(template.read_text(encoding="utf-8"))

    weight_dir_name = SOVITS_WEIGHT_DIRS.get(version, "SoVITS_weights_v2")
    weight_dir = upstream_root / weight_dir_name
    weight_dir.mkdir(parents=True, exist_ok=True)
    (opt_dir / f"logs_s2_{version}").mkdir(parents=True, exist_ok=True)

    config.setdefault("train", {})
    config.setdefault("data", {})
    config.setdefault("model", {})
    pretrained = pretrained_sovits_paths(upstream_root=upstream_root, version=version)
    config["train"].update(
        {
            "batch_size": batch_size,
            "epochs": epochs,
            "text_low_lr_rate": text_low_lr_rate,
            "if_save_latest": if_save_latest,
            "if_save_every_weights": if_save_every_weights,
            "save_every_epoch": save_every_epoch,
            "gpu_numbers": gpu_numbers,
            "grad_ckpt": if_grad_ckpt,
            # ``s2_train.py`` reads these straight off the config and raises
            # AttributeError when they are missing, so they are mandatory even
            # though the JSON template does not contain them.
            "pretrained_s2G": str(pretrained["s2G"]),
            "pretrained_s2D": str(pretrained["s2D"]),
        }
    )
    config["model"]["version"] = version
    config["data"]["exp_dir"] = str(opt_dir)
    config["s2_ckpt_dir"] = config["data"]["exp_dir"]
    config["save_weight_dir"] = str(weight_dir)
    config["name"] = exp_name
    config["version"] = version

    config_dir = Path(work_dir) if work_dir is not None else upstream_root / "TEMP"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"aemeath_tmp_s2_{exp_name}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    return TrainingSpec(
        stage="sovits",
        command=[
            python_exec or training_python(upstream_root),
            "-s",
            str(script),
            "--config",
            str(config_path),
        ],
        env={"_CUDA_VISIBLE_DEVICES": gpu_numbers, "gpu_numbers": gpu_numbers},
        cwd=str(upstream_root),
        config_path=str(config_path),
        outputs=[weight_dir],
    )


def find_artifacts(
    *,
    upstream_root: Path,
    exp_name: str,
    version: str = "v2",
    stage: str = "sovits",
) -> List[Path]:
    """Locate weight files a run produced for one experiment.

    Upstream names the directly loadable weight ``<exp_name>_e<epoch>_s<step>.pth``
    inside a version-specific directory, so a run that exits 0 without writing
    one has not actually produced a voice.

    Args:
        upstream_root: Checkout root.
        exp_name: Experiment name.
        version: Model version.
        stage: ``sovits`` or ``gpt``.

    Returns:
        Matching weight files, newest first.
    """
    if stage == "gpt":
        directory = upstream_root / GPT_WEIGHT_DIRS.get(version, "GPT_weights_v2")
    else:
        directory = upstream_root / SOVITS_WEIGHT_DIRS.get(version, "SoVITS_weights_v2")

    if not directory.exists():
        return []
    matches = [
        path
        for path in directory.glob(f"{exp_name}*.pth")
        if path.is_file()
    ]
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)


class TrainingJob:
    """One orchestrated training run and its observable state.

    The job owns exactly one process. Cancellation is scoped to that process:
    upstream's own ``kill_process`` runs ``taskkill /t /f /pid``, which
    terminates the whole process tree, so the PID it is aimed at is the
    difference between stopping our run and stopping the user's TTS service.

    Attributes:
        job_id: Stable id for this run.
        exp_name: Experiment name.
        version: Model version.
        status: Current lifecycle state.
        pid: PID of the started process, once launched.
        detail: Human-readable explanation of the current state.
        artifacts: Weight files found on disk.
        failure_kind: ``""``, ``infrastructure`` or ``material``.
    """

    def __init__(self, *, job_id: str, exp_name: str, version: str = "v2") -> None:
        self.job_id = job_id
        self.exp_name = exp_name
        self.version = version
        self.status = TrainingStatus.PENDING
        self.pid: Optional[int] = None
        self.detail = ""
        self.artifacts: List[Path] = []
        self.failure_kind = ""
        self._process: Any = None
        self._spec: Optional[TrainingSpec] = None

    # -- lifecycle -----------------------------------------------------

    def launch(
        self,
        spec: TrainingSpec,
        *,
        popen: Optional[Callable[..., Any]] = None,
    ) -> "TrainingJob":
        """Start the stage and record the PID.

        Args:
            spec: Stage to run.
            popen: ``subprocess.Popen``-compatible factory; injected by tests.

        Returns:
            ``self``, for chaining.

        Raises:
            RuntimeError: If this job already owns a running process.
        """
        if self._process is not None and self.status == TrainingStatus.RUNNING:
            raise RuntimeError("该任务已有运行中的进程，不能重复启动。")

        factory = popen or subprocess.Popen
        env = dict(os.environ)
        env.update(spec.env)
        # A headless run must not try to pop a console window on Windows.
        creationflags = 0
        if os.name == "nt" and popen is None:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._spec = spec
        self._process = factory(
            spec.command,
            cwd=spec.cwd,
            env=env,
            **({"creationflags": creationflags} if creationflags else {}),
        )
        self.pid = getattr(self._process, "pid", None)
        self.status = TrainingStatus.RUNNING
        self.detail = "训练进程已启动。"
        return self

    def refresh(self) -> TrainingStatus:
        """Re-read the process state and update :attr:`status`.

        A zero exit code is **not** sufficient for success: upstream can exit 0
        having written no usable weight, so the artefacts on disk are what
        decide. Reporting success there would let a later step apply a voice
        that does not exist.

        Returns:
            The updated status.
        """
        if self._process is None:
            return self.status

        if self.status in (
            TrainingStatus.CANCELLED,
            TrainingStatus.SUCCEEDED,
            TrainingStatus.FAILED,
        ):
            return self.status

        returncode = self._process.poll()
        if returncode is None:
            return self.status

        self._collect_artifacts()

        if returncode == 0 and self.artifacts:
            self.status = TrainingStatus.SUCCEEDED
            self.detail = f"训练完成，产物 {len(self.artifacts)} 个。"
        elif returncode == 0:
            self.status = TrainingStatus.FAILED
            self.failure_kind = "material"
            self.detail = "训练进程以 0 退出，但未找到任何产物，按失败处理。"
        else:
            self.status = TrainingStatus.FAILED
            if not self.failure_kind:
                self.failure_kind = "infrastructure"
            self.detail = f"训练进程退出码 {returncode}。"
        return self.status

    def _collect_artifacts(self) -> None:
        """Scan the weight directories for this experiment's output."""
        if self._spec is None:
            return
        root = Path(self._spec.cwd)
        self.artifacts = find_artifacts(
            upstream_root=root, exp_name=self.exp_name, version=self.version
        )
        if self._spec.stage == "gpt":
            self.artifacts += find_artifacts(
                upstream_root=root,
                exp_name=self.exp_name,
                version=self.version,
                stage="gpt",
            )

    def cancel(self, *, taskkill: Optional[Callable[[int], Any]] = None) -> bool:
        """Stop this job's own process tree.

        Args:
            taskkill: Callable receiving the PID; injected by tests. Defaults to
                ``taskkill /t /f /pid <pid>`` on Windows and ``terminate``
                elsewhere.

        Returns:
            ``True`` when a kill was issued, ``False`` when there was nothing
            live to cancel. A repeated call is deliberately a no-op: sending a
            second kill to a recycled PID would hit an unrelated process.
        """
        if self.status in (TrainingStatus.CANCELLED, TrainingStatus.SUCCEEDED):
            return False
        if self._process is None or self.pid is None:
            return False
        if self.status is TrainingStatus.FAILED:
            return False

        if taskkill is not None:
            taskkill(self.pid)
        else:
            self._kill_own_tree()

        self.status = TrainingStatus.CANCELLED
        self.detail = f"已终止训练进程 PID {self.pid}。"
        return True

    def _kill_own_tree(self) -> None:
        """Terminate this job's process tree using upstream's mechanism.

        Upstream kills with ``taskkill /t /f /pid``; reusing exactly that keeps
        behaviour identical to the WebUI while the PID keeps the blast radius to
        the process we started.
        """
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/t", "/f", "/pid", str(self.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:  # pragma: no cover - Windows is the supported target
            self._process.terminate()

    # -- policy --------------------------------------------------------

    def retry_verdict(self) -> Dict[str, Any]:
        """Whether this job may be started again, and why.

        Retrying is sound only for infrastructure failures. A material failure
        reproduces identically, and a running job cannot be restarted because
        upstream's generated config lives in a shared ``TEMP/`` file that a
        second run would overwrite.

        Returns:
            ``{"retryable": bool, "reason": str}``.
        """
        if self.status is TrainingStatus.RUNNING:
            return {
                "retryable": False,
                "reason": "任务仍在运行，不能重复启动；上游临时配置存放在共享 TEMP 目录，并发训练会互相覆盖。",
            }
        if self.status is TrainingStatus.SUCCEEDED:
            return {"retryable": False, "reason": "任务已成功，无需重试。"}
        if self.status is TrainingStatus.PENDING:
            return {"retryable": False, "reason": "任务尚未启动。"}
        if self.failure_kind == "material":
            return {
                "retryable": False,
                "reason": "素材或产物问题导致失败，直接重试会复现同一结果；请先修正素材。",
            }
        if self.status is TrainingStatus.CANCELLED:
            return {"retryable": True, "reason": "任务已被取消，可重新启动。"}
        return {"retryable": True, "reason": "基础设施类失败，可重新启动。"}
