"""Configuration check entry point.

Validates the Aemeath setup *before* launching the desktop client so that
misconfiguration is reported as a concrete, readable error instead of a silent
non-responsive window.

Reports two separate things, because they fail for different reasons:

* **completeness** — is the capability configured, and does it have a key;
* **connectivity** — does the provider actually answer (``--live``).

Usage:
    python scripts/check_config.py [--config PATH] [--live]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_DIR = ROOT_DIR / "vendor" / "Open-LLM-VTuber"
UPSTREAM_SRC = UPSTREAM_DIR / "src"

# Upstream imports as a source tree rooted at vendor/Open-LLM-VTuber/src;
# the repo root must also be importable for the "src" namespace package to
# resolve (same layout run_server.py sets up).
sys.path.insert(0, str(UPSTREAM_DIR))
sys.path.insert(0, str(UPSTREAM_SRC))
sys.path.insert(0, str(ROOT_DIR))

from aemeath.config import resolve_config_path, set_config_path  # noqa: E402

#: Selected once in :func:`main`; every check reads this single value.
CONFIG_PATH: Path = ROOT_DIR / "config" / "conf.aemeath.yaml"

OK = "  [ok]  "
WARN = " [warn] "
FAIL = " [FAIL] "

_failures: list[str] = []
_warnings: list[str] = []


def _ok(msg: str) -> None:
    print(f"{OK}{msg}")


def _warn(msg: str, detail: str = "") -> None:
    _warnings.append(msg)
    print(f"{WARN}{msg}" + (f" — {detail}" if detail else ""))


def _fail(msg: str, detail: str = "") -> None:
    _failures.append(msg)
    print(f"{FAIL}{msg}" + (f" — {detail}" if detail else ""))


def check_python() -> None:
    """Confirm the interpreter matches the pinned upstream range."""
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) == (3, 11):
        _ok(f"Python {version} (pinned target)")
    elif (major, minor) in {(3, 10), (3, 12)}:
        _warn(f"Python {version} is allowed upstream but not the pinned target", "3.11")
    else:
        _fail(f"Python {version} is outside upstream's >=3.10,<3.13 range")


def check_upstream() -> None:
    """Confirm the pinned upstream checkout is present."""
    if not UPSTREAM_SRC.is_dir():
        _fail("Upstream source missing", str(UPSTREAM_SRC))
        return
    _ok("Upstream source present")

    frontend = UPSTREAM_DIR / "frontend" / "index.html"
    if frontend.is_file():
        _ok("Desktop client assets present")
    else:
        _fail(
            "Desktop client assets missing",
            "run: git -C vendor/Open-LLM-VTuber submodule update --init frontend",
        )


def check_imports() -> None:
    """Confirm the engines Aemeath depends on import cleanly."""
    try:
        import src.open_llm_vtuber  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        _fail("Cannot import upstream package", str(exc))
        return
    _ok("Upstream package imports")

    for module in ("numpy", "yaml", "pydantic"):
        try:
            __import__(module)
        except Exception as exc:  # pragma: no cover - environment dependent
            _fail(f"Cannot import {module}", str(exc))
    else:
        _ok("Core dependencies import")


def check_config() -> None:
    """Validate the Aemeath config against the upstream schema."""
    if not CONFIG_PATH.is_file():
        _fail("Config file missing", str(CONFIG_PATH))
        return

    from src.open_llm_vtuber.config_manager import read_yaml, validate_config

    try:
        parsed = read_yaml(str(CONFIG_PATH))
    except Exception as exc:
        _fail("Config file is not valid YAML", str(exc))
        return
    _ok("Config file parses as YAML")

    try:
        config = validate_config(parsed)
    except Exception as exc:
        # Upstream already logged the precise field errors; summarise here.
        _fail("Config failed schema validation", str(exc).splitlines()[0])
        return
    _ok("Config passes upstream schema validation")

    system = config.system_config
    if system.host in {"127.0.0.1", "localhost"}:
        _ok(f"Backend is local-only ({system.host}:{system.port})")
    else:
        _fail(
            f"Backend would listen on {system.host}",
            "first release requires a loopback address only",
        )

    agent = config.character_config.agent_config
    choice = agent.conversation_agent_choice
    if choice == "aemeath_agent":
        _ok("Agent is set to aemeath_agent")
    else:
        _warn(
            f"Agent is '{choice}', not 'aemeath_agent'",
            "expected until the Aemeath agent is registered",
        )

    check_llm_credentials(config)


def check_llm_credentials(config) -> None:  # noqa: ANN001 - upstream pydantic type
    """Report whether the selected LLM provider has a usable key.

    A missing key is a warning, not a failure: the config check must stay
    runnable before any real provider is chosen.
    """
    provider = None
    settings = config.character_config.agent_config.agent_settings
    for name in ("aemeath_agent", "basic_memory_agent"):
        block = getattr(settings, name, None)
        if block is not None and getattr(block, "llm_provider", None):
            provider = block.llm_provider
            break
    if not provider:
        _warn("No LLM provider selected for the active agent")
        return

    pool = config.character_config.agent_config.llm_configs
    provider_cfg = getattr(pool, provider, None)
    if provider_cfg is None:
        _fail(f"llm_configs has no entry for provider '{provider}'")
        return
    _ok(f"Active LLM provider: {provider}")

    key = getattr(provider_cfg, "llm_api_key", None) or ""
    base_url = getattr(provider_cfg, "base_url", "") or ""
    model = getattr(provider_cfg, "model", "") or ""

    if key.startswith("${") and key.endswith("}"):
        _warn(
            f"LLM API key environment variable is unset ({key})",
            "export it before real API validation",
        )
    elif not key:
        _warn("LLM API key is empty")
    else:
        _ok("LLM API key is present")

    if "example.invalid" in base_url or not base_url:
        _warn(f"LLM base_url is a placeholder ({base_url or 'empty'})")
    else:
        _ok(f"LLM base_url: {base_url}")

    if not model or model == "replace-me":
        _warn("LLM model name is a placeholder")
    else:
        _ok(f"LLM model: {model}")


def check_capabilities() -> None:
    """Report which of the six required capabilities are configured.

    Completeness is decided from configuration alone; whether a provider then
    answers is a separate question (``--live``). A capability that is missing
    its key is reported as a failure here, because a configured-but-keyless
    provider is the failure mode that otherwise only shows up mid-conversation.
    """
    from aemeath.adapters import AdapterFactory
    from aemeath.config import load_config

    try:
        config = load_config(CONFIG_PATH)
    except Exception as exc:
        _fail("Cannot load Aemeath config block", str(exc))
        return

    factory = AdapterFactory.from_config(config)

    print("-" * 60)
    print("Model capabilities (completeness)")
    for name, build in (
        ("embedding", factory.build_embedding),
        ("extraction", factory.build_extraction),
        ("vision", factory.build_vision),
    ):
        _adapter, status = build()
        if status.state == "ready":
            _ok(f"{name}: {status.detail}")
        elif status.state == "error":
            _fail(f"{name}: {status.error}")
        else:
            _warn(f"{name} is not configured", status.detail)

    _adapter, capture = factory.build_capture()
    if capture.state == "ready":
        _ok(f"capture: {capture.detail}")
    elif capture.state == "error":
        _fail(f"capture: {capture.error}")
    else:
        _warn("capture is disabled", capture.detail)


async def _probe_all() -> list[dict]:
    """Run the live connectivity probes for every configured capability."""
    from aemeath.config import load_config
    from aemeath.live import probe_all

    return await probe_all(load_config(CONFIG_PATH))


def check_live() -> None:
    """Call each configured provider and report whether it answers."""
    print("-" * 60)
    print("Model capabilities (connectivity)")
    try:
        results = asyncio.run(_probe_all())
    except Exception as exc:
        _fail("Live probe crashed", str(exc))
        return

    for result in results:
        label = result["capability"]
        if result["ok"]:
            _ok(f"{label}: {result['detail']}")
        elif result.get("skipped"):
            _warn(f"{label} not probed", result["detail"])
        else:
            _fail(f"{label} unreachable", result["detail"])

    print("-" * 60)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def check_paths() -> None:
    """Ensure local data and log locations are usable."""
    from aemeath.config import load_config

    try:
        config = load_config(CONFIG_PATH)
    except Exception as exc:
        _fail("Cannot load Aemeath config block", str(exc))
        return

    try:
        config.ensure_dirs()
    except Exception as exc:
        _fail("Cannot create local data/log directories", str(exc))
        return
    _ok(f"Data directory ready ({config.data_dir})")
    _ok(f"Log directory ready ({config.log_dir})")

    if not os.access(config.data_dir, os.W_OK):
        _fail("Data directory is not writable", str(config.data_dir))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the checker's own arguments."""
    parser = argparse.ArgumentParser(description="Aemeath configuration check")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file to check (default: config/conf.aemeath.yaml)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also call each configured provider and report whether it answers",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run every check in order and summarise the outcome."""
    global CONFIG_PATH

    args = _parse_args(argv)
    CONFIG_PATH = resolve_config_path(args.config)
    # Publish it so any subprocess this check spawns resolves the same file.
    set_config_path(CONFIG_PATH)

    print("Aemeath configuration check")
    print(f"config: {CONFIG_PATH}")
    print("=" * 60)
    check_python()
    check_upstream()
    check_imports()
    check_config()
    check_capabilities()
    if args.live:
        check_live()
    check_paths()
    print("=" * 60)

    if _failures:
        print(f"FAILED: {len(_failures)} problem(s) must be fixed.")
        return 1
    if _warnings:
        print(f"PASSED with {len(_warnings)} warning(s) (see markers above).")
        return 0
    print("PASSED: configuration is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
