"""Aemeath backend launcher.

Starts the pinned upstream server with Aemeath's configuration and makes the
Aemeath module importable from the upstream process.

The desktop client is then reached at http://127.0.0.1:12393 by opening the
served frontend in a browser (the upstream desktop client is a web UI).

Usage:
    python scripts/run_server.py [--verbose] [--config PATH]

``--config`` selects which YAML drives the run and is threaded through *both*
layers: the file deployed into the upstream checkout, and the Aemeath runtime
inside the server process (via the ``AEMEATH_CONFIG`` environment variable). A
launch therefore cannot end up with the upstream engines reading one config
while the Aemeath modules read another.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_DIR = ROOT_DIR / "vendor" / "Open-LLM-VTuber"
UPSTREAM_CONFIG = UPSTREAM_DIR / "conf.yaml"

sys.path.insert(0, str(ROOT_DIR))

from aemeath.config import resolve_config_path, set_config_path  # noqa: E402


def _parse_args(argv: list[str]) -> tuple[Path, list[str]]:
    """Split Aemeath's own flags from the ones upstream understands.

    Upstream parses ``sys.argv`` itself, so unrecognised flags have to be
    forwarded untouched; only ``--config`` is consumed here.

    Args:
        argv: Raw command-line arguments.

    Returns:
        The selected config path and the arguments to forward upstream.
    """
    parser = argparse.ArgumentParser(
        description="Aemeath backend launcher", add_help=False
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file to run with (default: config/conf.aemeath.yaml)",
    )
    known, forwarded = parser.parse_known_args(argv)
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        raise SystemExit(0)

    forwarded = [arg for arg in forwarded if arg in ("--verbose", "--hf_mirror")]
    return resolve_config_path(known.config), forwarded


def _deploy_config(config_path: Path) -> None:
    """Copy Aemeath's config to where upstream expects it.

    Upstream reads ``conf.yaml`` from its own directory. Aemeath keeps the
    authoritative copy outside the vendored tree so it stays reviewable; this
    copies it into place at startup.

    Args:
        config_path: The authoritative Aemeath config to deploy.
    """
    if not config_path.is_file():
        raise SystemExit(f"Aemeath config not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    if not UPSTREAM_CONFIG.exists() or UPSTREAM_CONFIG.read_text(
        encoding="utf-8"
    ) != text:
        UPSTREAM_CONFIG.write_text(text, encoding="utf-8")
        print(f"Deployed config -> {UPSTREAM_CONFIG} (from {config_path})")


def _configure_paths() -> None:
    """Make both upstream and Aemeath importable in the server process.

    Upstream is a source tree, not an installed package: it needs both its repo
    root (for the top-level ``prompts`` package) and its ``src`` directory.
    """
    for path in (
        str(UPSTREAM_DIR),
        str(UPSTREAM_DIR / "src"),
        str(ROOT_DIR),
    ):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(UPSTREAM_DIR)


def main() -> None:
    """Deploy config, fix up imports and hand over to upstream's entry point."""
    config_path, forwarded = _parse_args(sys.argv[1:])

    # Publish the choice before anything reads a config, so the Aemeath runtime
    # inside the server process resolves the same file as the launcher did.
    set_config_path(config_path)
    print(f"Aemeath config  -> {config_path}")

    _deploy_config(config_path)
    _configure_paths()

    sys.argv = ["run_server.py", *forwarded]
    runpy.run_path(str(UPSTREAM_DIR / "run_server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
