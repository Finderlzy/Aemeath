# -*- coding: utf-8 -*-
"""Regression: scripts/check_config.py must be importable from any cwd.

The upstream ``src`` tree is a namespace package, so the upstream repo root
(not only its ``src`` directory) has to be on ``sys.path``. This regressed
once: the script ran fine when launched from inside
``vendor/Open-LLM-VTuber`` but failed with ``No module named 'src'`` from
the repo root, which is the documented way to run it.
"""

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / "scripts" / "check_config.py"


def test_check_config_imports_upstream_from_repo_root():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", "config/acceptance/conf.acceptance.yaml"],
        cwd=ROOT_DIR,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert "No module named 'src'" not in output
    assert "Upstream package imports" in output
