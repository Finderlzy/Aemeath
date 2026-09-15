"""Environment precheck for the GPT-SoVITS training path (V2-T05).

The wizard (V2-T06) must refuse to start a long training run on a machine that
cannot finish it, and it must say *which* prerequisite is missing. This module
answers that question up front: checkout present and complete, GPU visible,
material sufficient, artefacts ready, service state known.

It reports facts only. Nothing here starts training or touches the running TTS
service.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gpt_sovits import (
    DEFAULT_UPSTREAM_ROOT,
    check_training_inputs,
    fingerprint_upstream,
    resolve_upstream_root,
    validate_material,
)

#: Aemeath's own extracted material, used as the default small-sample source.
DEFAULT_MATERIAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "extracted_clean_voices"


def gpu_report() -> Dict[str, Any]:
    """Read the GPU situation via ``nvidia-smi``.

    Training and the live TTS service share this device, and an 8 GB laptop GPU
    is the binding constraint on the whole feature, so its state is part of the
    precheck rather than a footnote.

    Returns:
        ``{"available", "name", "total_mb", "used_mb", "free_mb", "error"}``.
        ``available`` is ``False`` with an ``error`` when no usable GPU is found.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {
            "available": False,
            "name": "",
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
            "error": "nvidia-smi 不可用",
        }

    try:
        result = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return {
            "available": False,
            "name": "",
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
            "error": str(exc),
        }

    if result.returncode != 0:
        return {
            "available": False,
            "name": "",
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
            "error": result.stderr.strip() or f"nvidia-smi 退出码 {result.returncode}",
        }

    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    parts = [item.strip() for item in line.split(",")]
    if len(parts) < 3:
        return {
            "available": False,
            "name": "",
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
            "error": f"无法解析 nvidia-smi 输出: {line}",
        }

    try:
        total = int(float(parts[1]))
        used = int(float(parts[2]))
    except ValueError:
        return {
            "available": False,
            "name": parts[0],
            "total_mb": 0,
            "used_mb": 0,
            "free_mb": 0,
            "error": f"无法解析显存数值: {line}",
        }

    return {
        "available": True,
        "name": parts[0],
        "total_mb": total,
        "used_mb": used,
        "free_mb": total - used,
        "error": "",
    }


def check_torch(upstream_root: Optional[Path] = None) -> Dict[str, Any]:
    """Ask the checkout's interpreter what torch/CUDA it actually has.

    Run in a child process so a broken checkout cannot take the caller down, and
    so the answer describes the interpreter that will run training rather than
    whatever is importing this module.

    Args:
        upstream_root: Checkout override.

    Returns:
        ``{"ok", "python", "torch", "cuda", "device", "error"}``.
    """
    from .gpt_sovits import training_python

    root = resolve_upstream_root(upstream_root)
    python_exec = training_python(root)
    probe = (
        "import json,sys;"
        "info={'python':sys.version.split()[0]};"
        "\nimport torch;"
        "info['torch']=torch.__version__;"
        "info['cuda']=torch.cuda.is_available();"
        "info['device']=torch.cuda.get_device_name(0) if torch.cuda.is_available() else '';"
        "print(json.dumps(info))"
    )
    try:
        result = subprocess.run(
            [python_exec, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(root) if root.exists() else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "python": python_exec,
            "torch": "",
            "cuda": False,
            "device": "",
            "error": str(exc),
        }

    if result.returncode != 0:
        return {
            "ok": False,
            "python": python_exec,
            "torch": "",
            "cuda": False,
            "device": "",
            "error": (result.stderr or result.stdout).strip()[-400:],
        }

    import json

    try:
        info = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {
            "ok": False,
            "python": python_exec,
            "torch": "",
            "cuda": False,
            "device": "",
            "error": f"无法解析探测输出: {exc}",
        }

    return {
        "ok": True,
        "python": info.get("python", ""),
        "torch": info.get("torch", ""),
        "cuda": bool(info.get("cuda")),
        "device": info.get("device", ""),
        "error": "",
    }


def check_service(api_url: str = "http://127.0.0.1:9880") -> Dict[str, Any]:
    """Whether a GPT-SoVITS api_v2 service is already listening.

    Knowing this up front matters for two reasons: the training run must not
    disturb it, and it is the other consumer of the GPU.

    Args:
        api_url: Base URL of the service.

    Returns:
        ``{"running": bool, "url": str, "detail": str}``.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    endpoint = f"{api_url.rstrip('/')}/openapi.json"
    try:
        with urlopen(endpoint, timeout=5) as response:
            status = response.status
    except HTTPError as exc:
        return {"running": False, "url": api_url, "detail": f"HTTP {exc.code}"}
    except (URLError, OSError) as exc:
        return {"running": False, "url": api_url, "detail": str(exc)}

    return {
        "running": status == 200,
        "url": api_url,
        "detail": f"HTTP {status}",
    }


def precheck(
    *,
    upstream_root: Optional[Path] = None,
    material_dir: Optional[Path] = None,
    list_path: Optional[Path] = None,
    exp_name: str = "aemeath_v2",
    version: str = "v2",
    api_url: str = "http://127.0.0.1:9880",
) -> Dict[str, Any]:
    """Run the full precheck and report whether training may start.

    Args:
        upstream_root: Checkout override; resolved via ``GPT_SOVITS_DIR`` when
            omitted.
        material_dir: Directory holding training clips.
        list_path: The ``.list`` annotation file.
        exp_name: Experiment name; used to locate preprocessed artefacts.
        version: Model version.
        api_url: Base URL of the local TTS service.

    Returns:
        A report with ``ready``, ``blocking`` and one entry per section. Only
        things that genuinely prevent training land in ``blocking``.
    """
    root = resolve_upstream_root(upstream_root)
    audio_dir = Path(material_dir) if material_dir else DEFAULT_MATERIAL_DIR
    listing = Path(list_path) if list_path else audio_dir / "transcripts.list"

    report: Dict[str, Any] = {
        "upstream": fingerprint_upstream(root),
        "torch": check_torch(root),
        "gpu": gpu_report(),
        "service": check_service(api_url),
        "material": {
            "audio_dir": str(audio_dir),
            "list_path": str(listing),
            "problems": validate_material(audio_dir=audio_dir, list_path=listing),
        },
        "artifacts": {
            "opt_dir": str(root / "logs" / exp_name),
            "problems": [],
        },
    }

    # Preprocessed artefacts only block the *training* stage, which is what the
    # wizard runs after preprocessing. Report them without treating their
    # absence as a hard failure here.
    report["artifacts"]["problems"] = check_training_inputs(
        opt_dir=root / "logs" / exp_name, version=version
    )

    blocking: List[str] = []
    if not report["upstream"]["available"]:
        blocking.extend(report["upstream"]["missing"])
    if not report["torch"]["ok"]:
        blocking.append(f"torch/CUDA 探测失败: {report['torch']['error']}")
    elif not report["torch"]["cuda"]:
        blocking.append("torch 报告 CUDA 不可用，训练将无法使用 GPU。")
    if not report["gpu"]["available"]:
        blocking.append(f"GPU 不可用: {report['gpu']['error']}")

    report["blocking"] = blocking
    report["ready"] = not blocking
    return report


def format_report(report: Dict[str, Any]) -> str:
    """Render a precheck report as short lines for the console.

    Args:
        report: Output of :func:`precheck`.

    Returns:
        A multi-line human-readable summary.
    """
    lines: List[str] = []
    upstream = report["upstream"]
    revision = upstream.get("revision", {})

    lines.append(f"上游目录: {upstream['root']}")
    lines.append(
        "上游版本: "
        + (revision.get("commit") or "未知")
        + ("（有未提交改动）" if revision.get("dirty") else "")
    )
    lines.append(f"上游可用: {'是' if upstream['available'] else '否'}")
    if upstream["missing"]:
        for item in upstream["missing"]:
            lines.append(f"  缺失: {item}")

    torch_info = report["torch"]
    if torch_info["ok"]:
        lines.append(
            f"torch: {torch_info['torch']} / CUDA {torch_info['cuda']} / {torch_info['device']}"
        )
    else:
        lines.append(f"torch 探测失败: {torch_info['error']}")

    gpu = report["gpu"]
    if gpu["available"]:
        lines.append(
            f"GPU: {gpu['name']} 共 {gpu['total_mb']}MB，已用 {gpu['used_mb']}MB，"
            f"空闲 {gpu['free_mb']}MB"
        )
    else:
        lines.append(f"GPU 不可用: {gpu['error']}")

    service = report["service"]
    lines.append(
        f"TTS 服务 ({service['url']}): "
        + ("运行中" if service["running"] else f"未运行 ({service['detail']})")
    )

    material = report["material"]
    lines.append(f"素材目录: {material['audio_dir']}")
    if material["problems"]:
        for item in material["problems"]:
            lines.append(f"  素材问题: {item}")
    else:
        lines.append("素材: 通过")

    artifacts = report["artifacts"]
    lines.append(f"预处理产物目录: {artifacts['opt_dir']}")
    if artifacts["problems"]:
        lines.append(f"  预处理产物未就绪（{len(artifacts['problems'])} 项缺失，训练前须先完成预处理）")
    else:
        lines.append("预处理产物: 就绪")

    lines.append("")
    if report["ready"]:
        lines.append("结论: 环境就绪，可以开始训练。")
    else:
        lines.append("结论: 环境未就绪，存在阻塞项：")
        for item in report["blocking"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)
