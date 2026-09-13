"""Shared pytest fixtures and test doubles.

Tests must run without real API credentials, a microphone, a screen or a
desktop client. Model calls, the clock, screen capture and audio playback are
therefore substituted with the doubles in this package.

Real-provider tests live behind the ``live_api`` marker and are excluded from
the default run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_SRC = ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "src"

# Upstream is consumed as a source tree; make both roots importable.
for path in (str(UPSTREAM_SRC), str(ROOT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# The integration harness defines fixtures used by tests/integration; register
# it as a plugin so pytest collects those fixtures from the shared module.
pytest_plugins = ["tests.integration.harness"]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "live_api: requires real provider credentials; not run by default"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip live API tests unless explicitly selected."""
    if config.getoption("-m") and "live_api" in config.getoption("-m"):
        return
    skip_live = pytest.mark.skip(reason="live API test (use -m live_api to run)")
    for item in items:
        if "live_api" in item.keywords:
            item.add_marker(skip_live)
