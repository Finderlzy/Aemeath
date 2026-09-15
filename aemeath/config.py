"""Aemeath configuration loading and path resolution.

Two configuration layers exist and must not be confused:

1. The upstream Open-LLM-VTuber config (``config/conf.aemeath.yaml``) drives the
   desktop client, engines and the agent selection. Upstream validates it.
2. The Aemeath block inside that file (``character_config.aemeath_config``)
   carries our own tunables. Upstream ignores unknown keys, so we read it
   ourselves and apply defaults for anything missing.

Secrets never live in the YAML file. Upstream expands ``${VAR}`` references
from the process environment (see ``config_manager.utils.read_yaml``), so the
config only stores the variable *name*.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# Repository root: this file is <root>/aemeath/config.py
ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "conf.aemeath.yaml"
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_LOG_DIR = ROOT_DIR / "logs"

#: Environment variable carrying the active config path.
#:
#: The acceptance run needs its own config, database and log directory without
#: touching the personal setup. The path is passed on the command line and must
#: reach *every* layer: the launcher, the config check, the status script and
#: the Aemeath runtime inside the upstream server process. An environment
#: variable is what makes that possible, because the upstream server re-reads
#: its configuration deep inside a process the launcher only hands over to.
#: :func:`set_config_path` writes it and :func:`resolve_config_path` reads it,
#: so the two layers can never disagree about which config is active.
CONFIG_PATH_ENV = "AEMEATH_CONFIG"


def set_config_path(config_path: Optional[Path]) -> Optional[Path]:
    """Record the active config path for this process and its children.

    Args:
        config_path: Explicit config path, or ``None`` to clear the override.

    Returns:
        The resolved absolute path, or ``None`` when cleared.
    """
    if config_path is None:
        os.environ.pop(CONFIG_PATH_ENV, None)
        return None
    resolved = Path(config_path).expanduser().resolve()
    os.environ[CONFIG_PATH_ENV] = str(resolved)
    return resolved


def resolve_config_path(config_path: Optional[Path] = None) -> Path:
    """Determine which config file is authoritative.

    Resolution order, highest priority first:

    1. an explicit argument (``--config``);
    2. the ``AEMEATH_CONFIG`` environment variable;
    3. the repository default.

    Every entry point funnels through here, so "which config is this actually
    running" has exactly one answer.

    Args:
        config_path: Explicit override, if the caller has one.

    Returns:
        The absolute config path.
    """
    if config_path is not None:
        return Path(config_path).expanduser().resolve()
    from_env = os.getenv(CONFIG_PATH_ENV)
    if from_env:
        return Path(from_env).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


@dataclass(frozen=True)
class ProviderConfig:
    """One model provider's connection settings.

    The API key itself is never stored here: only the *name* of the
    environment variable holding it, so a config file can be reviewed and
    committed without containing a secret.
    """

    enabled: bool = True
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 60.0
    dimensions: int = 0

    def resolve_api_key(self) -> Optional[str]:
        """Read the API key from the configured environment variable."""
        return env_secret(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class MemoryConfig:
    """Retrieval and context sizes for the local memory module."""

    recent_turns: int = 12
    recall_limit: int = 5
    experience_summary_every: int = 10
    #: Minimum cosine similarity for a memory to be returned. Retrieval is
    #: allowed to return nothing; a floor that is too low is how unrelated
    #: memories end up injected into every prompt.
    similarity_floor: float = 0.5
    #: How often the background worker drains the extraction queue.
    extraction_interval_seconds: float = 5.0


@dataclass(frozen=True)
class LearningConfig:
    """Sizing and pacing for expression and jargon learning (V2-T04).

    Learning is *off* by default. It sends conversation text to a model, which
    is a visible behaviour change rather than a background detail, so it only
    runs once the user has both configured a provider and switched it on.
    """

    enabled: bool = False
    #: How often the background worker drains the learning queue.
    interval_seconds: float = 30.0
    #: Maximum enabled entries of each kind injected into one prompt. A cap
    #: exists because the reference block is a *style hint*, not a knowledge
    #: base: dumping every learned phrase into every prompt would crowd out the
    #: persona and the retrieved memories.
    max_prompt_items: int = 8


@dataclass(frozen=True)
class ProactiveConfig:
    """Rate limits deciding whether Aemeath is *allowed* to speak up.

    These are eligibility limits only; the model still decides whether it is
    worth saying anything.
    """

    cooldown_seconds: int = 900
    max_per_hour: int = 2
    startup_greeting_enabled: bool = True
    #: How often the scheduler task evaluates eligibility.
    check_interval_seconds: float = 60.0


@dataclass(frozen=True)
class ScreenConfig:
    """Screen observation bounds."""

    max_edge_px: int = 1600
    min_interval_seconds: int = 60
    min_stable_seconds: int = 10
    summary_max_age_seconds: int = 120


@dataclass(frozen=True)
class SpeechConfig:
    """Which ASR and TTS backends are live.

    ``local`` means the first-release defaults (sherpa SenseVoice / edge-tts
    and the other offline engines); anything else names the formal engine.
    ``gpt_sovits_tts`` is the confirmed fixed-character-voice choice: it runs
    as a local HTTP server, but it is the *formal* backend rather than a
    placeholder, so it is reported by name.
    """

    asr_backend: str = "local"
    tts_backend: str = "local"
    #: The upstream ``gpt_sovits`` config block, read from the same validated
    #: document the engine is built from. Kept as a plain dict because
    #: upstream owns the schema; only the probe adapter consumes it.
    gpt_sovits: Optional[Dict[str, Any]] = None
    #: The upstream ``sherpa_onnx_asr`` config block.
    sherpa_onnx: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ProviderSet:
    """All configured model providers."""

    embedding: ProviderConfig = field(default_factory=ProviderConfig)
    extraction: ProviderConfig = field(default_factory=ProviderConfig)
    vision: ProviderConfig = field(default_factory=ProviderConfig)
    #: The expression / jargon learning model. Kept separate from extraction so
    #: the user can point learning at a different model (or switch it off)
    #: without changing how memories are written.
    learning: ProviderConfig = field(default_factory=ProviderConfig)
    #: The conversation model. Mirrors the upstream ``llm_configs`` entry the
    #: active agent uses, so the connectivity probe exercises the same endpoint
    #: conversations will.
    conversation: ProviderConfig = field(default_factory=ProviderConfig)


@dataclass(frozen=True)
class AemeathConfig:
    """Resolved Aemeath-side configuration."""

    data_dir: Path = DEFAULT_DATA_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    providers: ProviderSet = field(default_factory=ProviderSet)
    #: Whether to capture the screen at all. Absent dependencies are reported
    #: rather than silently disabling observation.
    screen_capture_enabled: bool = True

    @property
    def db_path(self) -> Path:
        """Authoritative local database holding messages, memory and state."""
        return self.data_dir / "aemeath.sqlite3"

    def ensure_dirs(self) -> None:
        """Create the local data and log directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "backups").mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def _resolve_dir(raw: Optional[str], fallback: Path) -> Path:
    """Resolve a configured directory relative to the repository root.

    Paths in the config file are written relative to the repository root
    (for example ``data``), not to the config file's own directory, so that
    moving the config file cannot silently relocate user data.
    """
    if not raw:
        return fallback
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    return candidate.resolve()


def _positive_int(raw: Any, fallback: int) -> int:
    """Return a positive int, falling back on invalid input rather than raising.

    Configuration mistakes must not prevent startup; they are reported by the
    config check entry point instead.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _positive_float(raw: Any, fallback: float) -> float:
    """Return a positive float, falling back on invalid input."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _fraction(raw: Any, fallback: float) -> float:
    """Return a value clamped to ``[0, 1]``, falling back when invalid.

    Used for the similarity floor, where a value outside the cosine range
    would either disable filtering or suppress everything.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return min(max(value, 0.0), 1.0)


def _provider(raw: Optional[Dict[str, Any]], default_env: str) -> ProviderConfig:
    """Parse one provider block."""
    block = raw or {}
    if not block:
        return ProviderConfig(
            enabled=False, model="", base_url="", api_key_env=default_env
        )
    return ProviderConfig(
        enabled=bool(block.get("enabled", True)),
        provider=str(block.get("provider") or "openai_compatible"),
        base_url=str(block.get("base_url") or ""),
        model=str(block.get("model") or ""),
        api_key_env=str(block.get("api_key_env") or default_env),
        timeout_seconds=_positive_float(block.get("timeout_seconds"), 60.0),
        dimensions=int(block.get("dimensions") or 0),
    )


def _upstream_llm_block(parsed: Dict[str, Any], config_path: str) -> Dict[str, Any]:
    """Read the active agent's LLM connection block from the upstream config.

    The conversation model is configured once, upstream, and selected through
    ``agent_settings.<agent>.llm_provider`` -> ``llm_configs.<provider>``. This
    follows that indirection so the Aemeath-side view of "which endpoint answers
    conversations" is derived from the real selection rather than duplicated.

    Args:
        parsed: The parsed YAML document.
        config_path: Path used only for error messages.

    Returns:
        A provider block with ``base_url``, ``model`` and ``api_key_env``, or an
        empty dict when it cannot be resolved.
    """
    character = (parsed.get("character_config") or {})
    agent_config = character.get("agent_config") or {}
    settings = agent_config.get("agent_settings") or {}

    chosen = agent_config.get("conversation_agent_choice")
    block = settings.get(chosen) if chosen else None
    if not isinstance(block, dict):
        # Fall back to the Aemeath agent, then to any agent with a provider.
        block = settings.get("aemeath_agent")
        if not isinstance(block, dict):
            block = next(
                (b for b in settings.values() if isinstance(b, dict) and b.get("llm_provider")),
                None,
            )
    if not isinstance(block, dict):
        return {}

    provider_name = block.get("llm_provider")
    pool = agent_config.get("llm_configs") or {}
    provider_block = pool.get(provider_name) if provider_name else None
    if not isinstance(provider_block, dict):
        return {}

    # ''/None key means "no auth needed"; a '${VAR}' value names a variable.
    raw_key = provider_block.get("llm_api_key") or ""
    api_key_env = ""
    if isinstance(raw_key, str) and raw_key.startswith("${") and raw_key.endswith("}"):
        api_key_env = raw_key[2:-1]
    elif raw_key:
        # A literal key cannot be re-read from the environment; store the value
        # under a name the adapter factory understands by returning it inline.
        api_key_env = ""

    return {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": str(provider_block.get("base_url") or ""),
        "model": str(provider_block.get("model") or ""),
        "api_key_env": api_key_env,
        "timeout_seconds": provider_block.get("timeout_seconds") or 60.0,
        "_literal_key": raw_key if isinstance(raw_key, str) and api_key_env == "" else "",
        "_source": config_path,
    }


def _upstream_speech_backends(
    parsed: Dict[str, Any], config_path: str
) -> tuple[str, str]:
    """Report which ASR and TTS engines the upstream config selects.

    Args:
        parsed: The parsed YAML document.
        config_path: Path used only for error messages.

    Returns:
        ``(asr_backend, tts_backend)`` where each is ``"local"`` for the
        first-release offline defaults and otherwise the API engine name.
    """
    character = (parsed.get("character_config") or {})
    asr_model = str((character.get("asr_config") or {}).get("asr_model") or "")
    tts_config = character.get("tts_config") or {}
    tts_model = str(tts_config.get("tts_model") or "")

    # Engines that run on this machine and need no provider. ``gpt_sovits_tts``
    # and ``sherpa_onnx_asr`` are NOT here: they are the confirmed formal
    # voice and ASR solutions, so they must be reported by name rather than
    # hidden behind the anonymous "local" placeholder.
    local_asr = {"faster_whisper", "funasr"}
    local_tts = {"edge_tts", "melo_tts", "piper_tts", "bark_tts"}

    asr = "local" if (not asr_model or asr_model in local_asr) else asr_model
    tts = "local" if (not tts_model or tts_model in local_tts) else tts_model
    return asr, tts


def _upstream_sherpa_onnx_block(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read the ``sherpa_onnx_asr`` block from the upstream ASR config."""
    character = (parsed.get("character_config") or {})
    asr_config = character.get("asr_config") or {}
    block = asr_config.get("sherpa_onnx_asr")
    return dict(block) if isinstance(block, dict) else None


def _upstream_gpt_sovits_block(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read the ``gpt_sovits`` block from the upstream TTS config.

    The block is upstream's schema (``GPTSoVITSConfig``); Aemeath only
    mirrors it for the live probe adapter, exactly as ``llm_configs`` is
    mirrored for the conversation provider.

    Args:
        parsed: The parsed YAML document.

    Returns:
        The block as a plain dict, or ``None`` when absent.
    """
    tts_config = (parsed.get("character_config") or {}).get("tts_config") or {}
    block = tts_config.get("gpt_sovits")
    return dict(block) if isinstance(block, dict) else None


def load_config(config_path: Optional[Path] = None) -> AemeathConfig:
    """Read the Aemeath block from the upstream config file.

    Missing sections fall back to the documented defaults, so a partially
    filled config still starts.

    Args:
        config_path: Override for the config file location.

    Returns:
        A resolved :class:`AemeathConfig`.
    """
    path = resolve_config_path(config_path)
    raw: Dict[str, Any] = {}
    if path.exists():
        # Imported lazily so the module stays importable without upstream.
        # Uses the same ``src.`` prefix the server loads upstream under, so the
        # config schema class is the same object rather than a second copy.
        from src.open_llm_vtuber.config_manager import read_yaml

        parsed = read_yaml(str(path)) or {}
        raw = (parsed.get("character_config") or {}).get("aemeath_config") or {}

    memory_raw = raw.get("memory") or {}
    learning_raw = raw.get("learning") or {}
    proactive_raw = raw.get("proactive") or {}
    screen_raw = raw.get("screen") or {}
    providers_raw = raw.get("providers") or {}

    # The conversation provider is not declared inside aemeath_config: it is the
    # upstream llm_configs entry the active agent points at. Mirroring it here
    # means the connectivity probe tests the endpoint conversations really use,
    # rather than a second copy that can drift out of sync.
    upstream_parsed = parsed if path.exists() else {}
    conversation_raw = providers_raw.get("conversation")
    if conversation_raw is None:
        conversation_raw = _upstream_llm_block(
            upstream_parsed, str(path)
        )
    asr_backend, tts_backend = _upstream_speech_backends(upstream_parsed, str(path))

    defaults = MemoryConfig()
    learning_defaults = LearningConfig()
    proactive_defaults = ProactiveConfig()
    screen_defaults = ScreenConfig()

    return AemeathConfig(
        data_dir=_resolve_dir(raw.get("data_dir"), DEFAULT_DATA_DIR),
        log_dir=_resolve_dir(raw.get("log_dir"), DEFAULT_LOG_DIR),
        memory=MemoryConfig(
            recent_turns=_positive_int(
                memory_raw.get("recent_turns"), defaults.recent_turns
            ),
            recall_limit=_positive_int(
                memory_raw.get("recall_limit"), defaults.recall_limit
            ),
            experience_summary_every=_positive_int(
                memory_raw.get("experience_summary_every"),
                defaults.experience_summary_every,
            ),
            similarity_floor=_fraction(
                memory_raw.get("similarity_floor"), defaults.similarity_floor
            ),
            extraction_interval_seconds=_positive_float(
                memory_raw.get("extraction_interval_seconds"),
                defaults.extraction_interval_seconds,
            ),
        ),
        learning=LearningConfig(
            # Learning is opt-in. ``enabled`` defaults to false even when a
            # provider block exists, so configuring a model cannot silently
            # start sending conversation text to it.
            enabled=bool(
                learning_raw.get("enabled", learning_defaults.enabled)
            ),
            interval_seconds=_positive_float(
                learning_raw.get("interval_seconds"),
                learning_defaults.interval_seconds,
            ),
            max_prompt_items=_positive_int(
                learning_raw.get("max_prompt_items"),
                learning_defaults.max_prompt_items,
            ),
        ),
        proactive=ProactiveConfig(
            cooldown_seconds=_positive_int(
                proactive_raw.get("cooldown_seconds"),
                proactive_defaults.cooldown_seconds,
            ),
            max_per_hour=_positive_int(
                proactive_raw.get("max_per_hour"), proactive_defaults.max_per_hour
            ),
            startup_greeting_enabled=bool(
                proactive_raw.get(
                    "startup_greeting_enabled",
                    proactive_defaults.startup_greeting_enabled,
                )
            ),
            check_interval_seconds=_positive_float(
                proactive_raw.get("check_interval_seconds"),
                proactive_defaults.check_interval_seconds,
            ),
        ),
        screen=ScreenConfig(
            max_edge_px=_positive_int(
                screen_raw.get("max_edge_px"), screen_defaults.max_edge_px
            ),
            min_interval_seconds=_positive_int(
                screen_raw.get("min_interval_seconds"),
                screen_defaults.min_interval_seconds,
            ),
            min_stable_seconds=_positive_int(
                screen_raw.get("min_stable_seconds"),
                screen_defaults.min_stable_seconds,
            ),
            summary_max_age_seconds=_positive_int(
                screen_raw.get("summary_max_age_seconds"),
                screen_defaults.summary_max_age_seconds,
            ),
        ),
        speech=SpeechConfig(
            asr_backend=asr_backend,
            tts_backend=tts_backend,
            gpt_sovits=_upstream_gpt_sovits_block(upstream_parsed),
            sherpa_onnx=_upstream_sherpa_onnx_block(upstream_parsed),
        ),
        providers=ProviderSet(
            embedding=_provider(
                providers_raw.get("embedding"), "AEMEATH_EMBEDDING_API_KEY"
            ),
            extraction=_provider(
                providers_raw.get("extraction"), "AEMEATH_EXTRACTION_API_KEY"
            ),
            vision=_provider(
                providers_raw.get("vision"), "AEMEATH_VISION_API_KEY"
            ),
            learning=_provider(
                providers_raw.get("learning"), "AEMEATH_LEARNING_API_KEY"
            ),
            conversation=_provider(conversation_raw, "AEMEATH_LLM_API_KEY"),
        ),
        screen_capture_enabled=bool(raw.get("screen_capture_enabled", True)),
    )


def env_secret(name: str) -> Optional[str]:
    """Read a secret from the environment.

    Args:
        name: Environment variable name.

    Returns:
        The value, or ``None`` when unset or empty.
    """
    value = os.getenv(name)
    return value or None


__all__ = [
    "ROOT_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DATA_DIR",
    "DEFAULT_LOG_DIR",
    "CONFIG_PATH_ENV",
    "ProviderConfig",
    "MemoryConfig",
    "LearningConfig",
    "ProactiveConfig",
    "ScreenConfig",
    "ProviderSet",
    "SpeechConfig",
    "AemeathConfig",
    "load_config",
    "set_config_path",
    "resolve_config_path",
    "env_secret",
]
