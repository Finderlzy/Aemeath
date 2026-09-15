"""Environment-precheck regressions for the training path (V2-T05).

The wizard must refuse to start a long run on a machine that cannot finish it,
and it must name the missing prerequisite. These tests pin that behaviour using
the real machine where the answer is stable, and injected values where it is
not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import probe  # noqa: E402


def test_gpu_report_reads_the_real_device() -> None:
    """The GPU probe reports usable facts on a machine that has one.

    Skipped rather than failed when no NVIDIA device is present, because the
    absence of a GPU is a different (and separately reported) condition.
    """
    report = probe.gpu_report()

    if not report["available"]:
        pytest.skip(f"no usable GPU on this machine: {report['error']}")
    assert report["name"]
    assert report["total_mb"] > 0
    assert report["free_mb"] >= 0


def test_torch_probe_uses_the_checkout_interpreter() -> None:
    """The probe describes the interpreter that will actually run training."""
    result = probe.check_torch()

    assert result["python"].startswith("3.")
    # The pinned checkout has torch with CUDA on this machine; if a machine
    # lacks it the probe must still return a well-formed answer.
    assert "ok" in result and "cuda" in result


def test_service_check_reports_not_running_on_a_dead_port() -> None:
    """An unused port is reported as not running, not raised."""
    result = probe.check_service("http://127.0.0.1:1")

    assert result["running"] is False
    assert result["detail"]


def test_service_check_detects_a_live_endpoint() -> None:
    """A reachable api_v2 service is reported as running.

    Uses the real service when it is up. This matters because training must not
    disturb it, so its presence has to be knowable before a run starts.
    """
    result = probe.check_service("http://127.0.0.1:9880")

    if not result["running"]:
        pytest.skip("local GPT-SoVITS service is not running")
    assert result["running"] is True


def test_precheck_reports_missing_material(tmp_path: Path) -> None:
    """Bad material is surfaced in the report rather than assumed good."""
    report = probe.precheck(
        material_dir=tmp_path / "nothing",
        list_path=tmp_path / "nothing.list",
    )

    assert report["material"]["problems"]
    assert any("不存在" in item for item in report["material"]["problems"])


def test_precheck_blocks_on_a_missing_checkout(tmp_path: Path) -> None:
    """A missing checkout blocks training and says so."""
    report = probe.precheck(upstream_root=tmp_path / "no-such-checkout")

    assert report["ready"] is False
    assert report["blocking"]


def test_precheck_reports_artifacts_separately_from_blocking() -> None:
    """Missing preprocessed artefacts are reported without blocking the precheck.

    They gate the *training* stage, which runs after preprocessing. Treating
    them as a hard environment failure would make the precheck fail on every
    first run, when nothing has been preprocessed yet.
    """
    report = probe.precheck(exp_name="__no_such_experiment__")

    if not report["upstream"]["available"]:
        pytest.skip("GPT-SoVITS checkout not present")
    assert report["artifacts"]["problems"]
    assert report["ready"] is True


def test_format_report_mentions_the_essentials() -> None:
    """The rendered report names the version, GPU and service state."""
    report = probe.precheck()
    text = probe.format_report(report)

    assert "上游版本" in text
    assert "GPU" in text
    assert "TTS 服务" in text
    assert "结论:" in text
