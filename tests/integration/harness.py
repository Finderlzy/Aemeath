"""Production-entry integration harness.

The plan requires integration tests to run through the *real* entry points —
the upstream config schema, ``AgentFactory``, the Aemeath bridge, the upstream
output processing and the client protocol — substituting only model providers,
audio devices and the capture backend.

Everything in this module therefore builds on upstream code rather than on
Aemeath-internal shortcuts:

* the config is a real ``conf.aemeath.yaml``-shaped document validated by
  ``open_llm_vtuber.config_manager.validate_config``;
* the agent comes from ``AgentFactory.create_agent``;
* the WebSocket surface is the real ``WebSocketHandler`` with a fake socket.

Tests may still replace the LLM, TTS, ASR, embedding, extraction and vision
providers, because those need credentials or hardware.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
UPSTREAM_DIR = ROOT_DIR / "vendor" / "Open-LLM-VTuber"
UPSTREAM_SRC = UPSTREAM_DIR / "src"

# Upstream is a source tree, not an installed package: its repo root is needed
# for the top-level ``prompts`` package, and ``src`` for ``open_llm_vtuber``.
for _path in (str(UPSTREAM_SRC), str(UPSTREAM_DIR), str(ROOT_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


def build_config_document(
    *,
    data_dir: Path,
    log_dir: Path,
    agent_choice: str = "aemeath_agent",
    aemeath_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a full upstream-shaped config document for tests.

    The document is deliberately constructed here rather than read from
    ``config/conf.aemeath.yaml`` so tests stay hermetic (no writes to the real
    data directory), while still passing through the genuine upstream schema.
    """
    aemeath_config: Dict[str, Any] = {
        "data_dir": str(data_dir),
        "log_dir": str(log_dir),
        "memory": {
            "recent_turns": 12,
            "recall_limit": 5,
            "similarity_floor": 0.5,
        },
        "proactive": {
            "cooldown_seconds": 900,
            "max_per_hour": 2,
            "startup_greeting_enabled": True,
        },
        "screen": {
            "max_edge_px": 1600,
            "min_interval_seconds": 60,
            "min_stable_seconds": 10,
            "summary_max_age_seconds": 120,
        },
    }
    if aemeath_overrides:
        for key, value in aemeath_overrides.items():
            if isinstance(value, dict) and isinstance(aemeath_config.get(key), dict):
                aemeath_config[key].update(value)
            else:
                aemeath_config[key] = value

    return {
        "system_config": {
            "conf_version": "v1.2.1",
            "host": "127.0.0.1",
            "port": 12393,
            "config_alts_dir": "characters",
            "tool_prompts": {
                "live2d_expression_prompt": "live2d_expression_prompt",
                "proactive_speak_prompt": "proactive_speak_prompt",
            },
        },
        "character_config": {
            "conf_name": "aemeath",
            "conf_uid": "aemeath_test",
            "live2d_model_name": "mao_pro",
            "character_name": "Aemeath",
            "avatar": "mao.png",
            "human_name": "你",
            "persona_prompt": "你是 Aemeath，一个桌面 AI 伙伴。",
            "agent_config": {
                "conversation_agent_choice": agent_choice,
                "agent_settings": {
                    "aemeath_agent": {
                        "llm_provider": "openai_compatible_llm",
                        "faster_first_response": True,
                        "segment_method": "pysbd",
                    },
                    "basic_memory_agent": {
                        "llm_provider": "openai_compatible_llm",
                        "faster_first_response": True,
                        "segment_method": "pysbd",
                        "use_mcpp": False,
                        "mcp_enabled_servers": [],
                    },
                    "hume_ai_agent": {
                        "api_key": "",
                        "host": "api.hume.ai",
                        "config_id": "",
                        "idle_timeout": 15,
                    },
                    "letta_agent": {
                        "host": "localhost",
                        "port": 8283,
                        "id": "xxx",
                        "faster_first_response": True,
                        "segment_method": "pysbd",
                    },
                },
                "llm_configs": {
                    "openai_compatible_llm": {
                        "base_url": "https://api.example.invalid/v1",
                        "llm_api_key": "test-key",
                        "organization_id": None,
                        "project_id": None,
                        "model": "test-model",
                        "temperature": 1.0,
                        "interrupt_method": "user",
                    }
                },
            },
            "asr_config": {
                "asr_model": "sherpa_onnx_asr",
                "sherpa_onnx_asr": {
                    "model_type": "sense_voice",
                    "sense_voice": "./models/test/model.int8.onnx",
                    "tokens": "./models/test/tokens.txt",
                    "num_threads": 1,
                    "use_itn": True,
                    "provider": "cpu",
                },
                "faster_whisper": {
                    "model_path": "large-v3-turbo",
                    "download_root": "models/whisper",
                    "language": "zh",
                    "device": "auto",
                    "compute_type": "int8",
                    "prompt": "",
                },
                "groq_whisper_asr": {
                    "api_key": "",
                    "model": "whisper-large-v3-turbo",
                    "lang": "zh",
                },
            },
            "tts_config": {
                "tts_model": "edge_tts",
                "edge_tts": {"voice": "zh-CN-XiaoxiaoNeural"},
                "siliconflow_tts": {
                    "api_url": "https://api.siliconflow.cn/v1/audio/speech",
                    "api_key": "",
                    "default_model": "FunAudioLLM/CosyVoice2-0.5B",
                    "default_voice": "",
                    "sample_rate": 32000,
                    "response_format": "mp3",
                    "stream": True,
                    "speed": 1,
                    "gain": 0,
                },
            },
            "vad_config": {
                "vad_model": "silero_vad",
                "silero_vad": {
                    "orig_sr": 16000,
                    "target_sr": 16000,
                    "prob_threshold": 0.4,
                    "db_threshold": 60,
                    "required_hits": 3,
                    "required_misses": 24,
                    "smoothing_window": 5,
                },
            },
            "tts_preprocessor_config": {
                "remove_special_char": True,
                "ignore_brackets": True,
                "ignore_parentheses": True,
                "ignore_asterisks": True,
                "ignore_angle_brackets": True,
                "translator_config": {
                    "translate_audio": False,
                    "translate_provider": "deeplx",
                    "deeplx": {
                        "deeplx_target_lang": "ZH",
                        "deeplx_api_endpoint": "http://localhost:1188/translate",
                    },
                },
            },
            "aemeath_config": aemeath_config,
        },
        "live_config": {"bilibili_live": {"room_ids": [], "sessdata": ""}},
    }


@pytest.fixture
def aemeath_config_path(tmp_path: Path) -> Path:
    """Write a validated upstream config document to a temp file."""
    import yaml

    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    path = tmp_path / "conf.aemeath.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    return path


def load_validated_config(path: Path):
    """Load a config file through upstream's real validator."""
    from src.open_llm_vtuber.config_manager import read_yaml, validate_config

    return validate_config(read_yaml(str(path)))


def aemeath_config_from_document(path: Path):
    """Resolve Aemeath's own settings from a written config file."""
    from aemeath.config import load_config

    return load_config(path)


# ----------------------------------------------------------------------
# Fake WebSocket
# ----------------------------------------------------------------------


class FakeWebSocket:
    """Minimal WebSocket stand-in that records everything sent to the client.

    Records ``json.loads``ed frames so tests assert on real protocol payloads
    rather than on strings that could be malformed.
    """

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.accepted = False
        self.closed = False

    async def send_text(self, text: str) -> None:
        """Record one outbound frame."""
        self.sent.append(json.loads(text))

    async def send_json(self, data: Dict[str, Any]) -> None:
        """Record one outbound JSON frame."""
        self.sent.append(data)

    async def accept(self) -> None:
        """Mark the socket as accepted."""
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        """Mark the socket as closed."""
        self.closed = True

    # -- assertions helpers -------------------------------------------

    def types_sent(self) -> List[str]:
        """Every message type sent, in order."""
        return [frame.get("type", "") for frame in self.sent]

    def frames_of(self, msg_type: str) -> List[Dict[str, Any]]:
        """All frames of one type."""
        return [frame for frame in self.sent if frame.get("type") == msg_type]

    def last_of(self, msg_type: str) -> Optional[Dict[str, Any]]:
        """The most recent frame of one type."""
        matches = self.frames_of(msg_type)
        return matches[-1] if matches else None

    def clear(self) -> None:
        """Forget recorded frames."""
        self.sent.clear()


# ----------------------------------------------------------------------
# Async barriers
# ----------------------------------------------------------------------


class Barrier:
    """Explicit rendezvous used to pin races at an exact point.

    The plan forbids relying on ``sleep`` to hit a race; tests block the code
    under test until the test itself releases it.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        """Signal arrival and block until released."""
        self.reached.set()
        await self.release.wait()

    async def arrive(self, timeout: float = 5.0) -> None:
        """Wait until the code under test has reached the barrier."""
        await asyncio.wait_for(self.reached.wait(), timeout=timeout)

    def open(self) -> None:
        """Release the blocked code."""
        self.release.set()


# ----------------------------------------------------------------------
# Runtime construction through the agent factory
# ----------------------------------------------------------------------


class IntegrationHarness:
    """Holds one fully wired Aemeath stack for a test."""

    def __init__(
        self,
        *,
        runtime,
        agent,
        context,
        websocket: FakeWebSocket,
        bridge,
        config,
    ) -> None:
        self.runtime = runtime
        self.agent = agent
        self.context = context
        self.websocket = websocket
        self.bridge = bridge
        self.config = config


@pytest.fixture
def make_harness(tmp_path: Path):
    """Factory building a harness wired through ``AgentFactory``.

    Returns a callable so individual tests can vary adapters (a failing or
    delayed extractor, for example) while still going through the production
    construction path.
    """

    def _make(
        *,
        llm=None,
        embedding=None,
        extraction=None,
        vision=None,
        capture=None,
        tts=None,
        asr=None,
        data_dir: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        aemeath_overrides: Optional[Dict[str, Any]] = None,
        agent_choice: str = "aemeath_agent",
        real_extraction_queue: bool = True,
    ) -> IntegrationHarness:
        import yaml

        from aemeath.config import load_config
        from aemeath.runtime import build_runtime, reset_runtime

        data_dir = data_dir or (tmp_path / "data")
        log_dir = log_dir or (tmp_path / "logs")
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        document = build_config_document(
            data_dir=data_dir,
            log_dir=log_dir,
            agent_choice=agent_choice,
            aemeath_overrides=aemeath_overrides,
        )
        config_path = tmp_path / f"conf.{abs(hash(str(data_dir)))}.yaml"
        config_path.write_text(
            yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
        )

        # Both halves read the same validated document.
        validated = load_validated_config(config_path)
        aemeath_config = load_config(config_path)

        reset_runtime()
        runtime = build_runtime(
            config=aemeath_config,
            embedding=embedding,
            extraction=extraction,
            vision=vision,
            capture=capture,
        )

        if llm is None:
            from tests.doubles import FakeLLM

            llm = FakeLLM(["好的。"])

        # Build the agent through the real factory with the real config schema.
        # The factory calls ``get_runtime()``, which returns the runtime built
        # above because the cache was just reset.
        from src.open_llm_vtuber.agent.agent_factory import AgentFactory

        character_config = validated.character_config
        agent = AgentFactory.create_agent(
            conversation_agent_choice=character_config.agent_config.conversation_agent_choice,
            agent_settings=character_config.agent_config.agent_settings.model_dump(),
            llm_configs=character_config.agent_config.llm_configs.model_dump(),
            system_prompt=character_config.persona_prompt,
            live2d_model=None,
            tts_preprocessor_config=character_config.tts_preprocessor_config,
            aemeath_config=aemeath_config,
        )
        # Swap in the test LLM while keeping the factory-built agent instance.
        agent._llm = llm

        websocket = FakeWebSocket()
        bridge = runtime.bridge
        bridge.attach_client(websocket.send_text, client_uid="test-client")
        bridge.set_history_uid(bridge.create_history())

        context = _make_service_context(
            character_config=character_config,
            agent=agent,
            tts=tts,
            asr=asr,
            websocket=websocket,
            bridge=bridge,
        )

        # The conversation layer reads the bridge off the context, exactly as
        # the patched upstream code does.
        context.history_uid = bridge.history_uid

        return IntegrationHarness(
            runtime=runtime,
            agent=agent,
            context=context,
            websocket=websocket,
            bridge=bridge,
            config=aemeath_config,
        )

    yield _make

    from aemeath.runtime import reset_runtime

    reset_runtime()


def _make_service_context(
    *, character_config, agent, tts, asr, websocket, bridge
):
    """Build a real ``ServiceContext`` with test engines attached."""
    from src.open_llm_vtuber.service_context import ServiceContext

    context = ServiceContext()
    context.character_config = character_config
    context.agent_engine = agent
    context.send_text = websocket.send_text
    context.client_uid = "test-client"
    # The real bridge is installed so the handler routes through it.
    context.aemeath_bridge = bridge
    context.tts_engine = tts or FakeTTSEngine()
    context.asr_engine = asr
    # Live2D is replaced because expression extraction is not under test.
    context.live2d_model = _NullLive2D()
    return context


class FakeTTSEngine:
    """TTS engine stand-in that produces a real audio file on disk.

    Upstream's TTS path returns a file path and the payload builder reads it,
    so a believable double has to write an actual file rather than return
    bytes. The content is a tiny WAV header and is never played.
    """

    def __init__(self) -> None:
        self.synthesised: List[str] = []
        self.fail = False
        self.files: List[str] = []

    async def async_generate_audio(self, text: str, file_name_no_ext: str = "") -> str:
        """Write a placeholder audio file and return its path."""
        if self.fail:
            raise RuntimeError("tts provider unavailable")
        import tempfile
        from pathlib import Path

        self.synthesised.append(text)
        directory = Path(tempfile.mkdtemp())
        path = directory / f"{file_name_no_ext or 'audio'}.wav"
        path.write_bytes(_SILENT_WAV)
        self.files.append(str(path))
        return str(path)

    def remove_file(self, file_path: str) -> None:
        """Delete a generated file, as upstream does after sending."""
        from pathlib import Path

        try:
            Path(file_path).unlink()
        except Exception:
            pass


def _build_test_wav(samples: int = 1600) -> bytes:
    """Build a valid 16-bit mono 16 kHz WAV with a non-silent tone.

    Upstream's payload builder decodes the file and derives a per-slice volume
    array for lip sync. It rejects two degenerate cases that a naive double
    hits: an empty data chunk (``max() arg is an empty sequence``) and an
    all-zero one (``Audio is empty or all zero``). Real samples are therefore
    required, not just a valid header.
    """
    import math
    import struct

    sample_rate = 16000
    frames = bytearray()
    for index in range(samples):
        value = int(8000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        frames += struct.pack("<h", value)
    data = bytes(frames)

    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


#: A valid non-silent mono WAV, used by :class:`FakeTTSEngine`.
_SILENT_WAV = _build_test_wav()


class _NullLive2D:
    """Live2D stand-in: no expression extraction, no model info."""

    model_info = {"url": "/live2d-models/mao_pro/mao_pro.model3.json", "name": "mao_pro"}
    emo_str = ""

    @staticmethod
    def extract_emotion(text: str):
        """Report that no expression was detected."""
        return None


# ----------------------------------------------------------------------
# Conversation driving
# ----------------------------------------------------------------------


async def run_turn(
    harness: IntegrationHarness,
    text: str,
    *,
    images: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    socket: Optional[FakeWebSocket] = None,
):
    """Run one conversation turn through the real upstream entry point.

    Args:
        harness: The wired stack.
        text: User input.
        images: Optional screen image payloads.
        metadata: Optional upstream metadata flags.
        socket: Optional alternate client socket. Output always goes through
            the bridge, so this only changes where the *upstream* conversation
            layer writes its own frames; it lets a test confirm that a
            superseded socket stops receiving Aemeath output.

    Upstream's ``finalize_conversation_turn`` waits for a
    ``frontend-playback-complete`` message from the client before returning.
    A test client that never plays audio would hang there forever, so the
    response is delivered concurrently — which is exactly what a real client
    does when it finishes playing.
    """
    from src.open_llm_vtuber.conversations.single_conversation import (
        process_single_conversation,
    )
    from src.open_llm_vtuber.message_handler import message_handler

    sender = (socket or harness.websocket).send_text

    async def answer_playback_requests() -> None:
        """Answer the playback-complete wait, as a real client would."""
        for _ in range(200):
            await asyncio.sleep(0.01)
            if message_handler._response_events.get("test-client"):
                message_handler.handle_message(
                    "test-client", {"type": "frontend-playback-complete"}
                )
                return

    responder = asyncio.create_task(answer_playback_requests())
    try:
        return await asyncio.wait_for(
            process_single_conversation(
                context=harness.context,
                websocket_send=sender,
                client_uid="test-client",
                user_input=text,
                images=images,
                metadata=metadata,
            ),
            timeout=20.0,
        )
    finally:
        responder.cancel()
        try:
            await responder
        except (asyncio.CancelledError, Exception):
            pass


def deep_copy_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a config document so a test can mutate it safely."""
    return copy.deepcopy(document)
