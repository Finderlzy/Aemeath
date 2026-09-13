"""GPT-SoVITS fixed-character-voice integration.

Scope of this task: selecting ``gpt_sovits_tts`` in the upstream config makes
GPT-SoVITS (local HTTP, api_v2) the *formal* TTS backend for Aemeath, replacing
the first-release placeholder (edge-tts) for the voice itself. The runtime
synthesis path is upstream's own ``TTSFactory`` engine, which needs no Aemeath
code; what Aemeath owns is:

* classifying the backend honestly (``gpt_sovits_tts`` is a named backend, not
  the anonymous ``local`` default), so probes and status don't misreport it;
* a ``build_tts`` factory method: ``probe_tts`` already calls it, and without
  it the TTS probe crashes the moment any non-local engine is selected;
* an adapter for the standalone live probe that speaks the same api_v2
  contract as the upstream engine, so "probe passed" and "server answered"
  are the same question.

GPT-SoVITS is not installed on this machine, so these tests exercise the
contract with a local HTTP double. Real-synthesis and listening acceptance
stay with T05 (issue #6).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from aemeath.adapters import GPTSoVITSAdapter  # noqa: E402


# ----------------------------------------------------------------------
# speech backend classification
# ----------------------------------------------------------------------


class TestSpeechBackendClassification:
    """``gpt_sovits_tts`` is a named formal backend, not ``local``."""

    def test_gpt_sovits_is_not_the_anonymous_local_default(self):
        from aemeath.config import _upstream_speech_backends

        parsed = {
            "character_config": {
                "tts_config": {"tts_model": "gpt_sovits_tts"},
                "asr_config": {"asr_model": "sherpa_onnx_asr"},
            }
        }
        asr, tts = _upstream_speech_backends(parsed, "test")
        assert tts == "gpt_sovits_tts"
        assert asr == "sherpa_onnx_asr"

    def test_edge_tts_and_absent_model_stay_local(self):
        from aemeath.config import _upstream_speech_backends

        base = {"character_config": {"tts_config": {}, "asr_config": {}}}
        for model in ("", "edge_tts", "melo_tts", "piper_tts", "bark_tts"):
            parsed = {
                "character_config": {
                    "tts_config": {"tts_model": model},
                    "asr_config": {},
                }
            }
            _, tts = _upstream_speech_backends(parsed, "test")
            assert tts == "local", model
        _, tts = _upstream_speech_backends(base, "test")
        assert tts == "local"


# ----------------------------------------------------------------------
# streaming_mode normalisation
# ----------------------------------------------------------------------


class TestStreamingModeNormalisation:
    """The upstream engine's default ``"ture"`` must never reach the wire.

    GPT-SoVITS api_v2 types ``streaming_mode`` as ``Union[bool, int]`` and
    rejects anything else with a 422 before synthesis starts. The upstream
    engine's own default is the misspelled string ``"ture"``, so a config
    that relies on defaults fails silently per request. The adapter owns the
    guard: it normalises valid values and rejects invalid ones up front.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("false", False),
            ("True", True),
            ("False", False),
            (True, True),
            (False, False),
            ("1", 1),
            ("0", 0),
            (1, 1),
            (0, 0),
            ("2", 2),
            ("3", 3),
        ],
    )
    def test_valid_values_are_normalised(self, raw, expected):
        adapter = GPTSoVITSAdapter._create_for_test(streaming_mode=raw)
        assert adapter.streaming_mode == expected

    @pytest.mark.parametrize("raw", ["ture", "yes", "maybe", "", "4", "stream"])
    def test_invalid_values_are_rejected(self, raw):
        with pytest.raises(ValueError, match="streaming_mode"):
            GPTSoVITSAdapter._create_for_test(streaming_mode=raw)


# ----------------------------------------------------------------------
# api_v2 request contract
# ----------------------------------------------------------------------


class _FakeGSVServer:
    """Minimal api_v2-shaped double: one GET /tts returning WAV bytes."""

    def __init__(self):
        import httpx

        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self._handler)

    def _handler(self, request: "httpx.Request") -> "httpx.Response":
        import httpx

        self.requests.append(
            {"url": str(request.url), "params": dict(request.url.params)}
        )
        if request.url.path != "/tts":
            return httpx.Response(404, json={"message": "not found"})
        streaming_mode = request.url.params.get("streaming_mode")
        if streaming_mode not in ("true", "false", "True", "False", "0", "1", "2", "3"):
            return httpx.Response(
                422,
                json={"message": "streaming_mode must be bool or int"},
            )
        return httpx.Response(200, content=b"RIFF-fake-wav-bytes")


class TestGPTSoVITSAdapterRequests:
    """The adapter speaks the api_v2 GET /tts contract the engine uses."""

    def _adapter(self, server, **overrides):
        params = {
            "api_url": "http://127.0.0.1:9880/tts",
            "text_lang": "zh",
            "ref_audio_path": "D:/voice/aemeath-ref.wav",
            "prompt_lang": "zh",
            "prompt_text": "参考音频的文本内容。",
            "streaming_mode": False,
        }
        params.update(overrides)
        adapter = GPTSoVITSAdapter(**params)
        adapter._transport = server.transport
        return adapter

    @pytest.mark.asyncio
    async def test_synthesize_sends_api_v2_parameters(self):
        server = _FakeGSVServer()
        adapter = self._adapter(server)

        audio = await adapter.synthesize("你好，我是爱弥斯。")

        assert audio == b"RIFF-fake-wav-bytes"
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request["url"].startswith("http://127.0.0.1:9880/tts?")
        params = request["params"]
        assert params["text"] == "你好，我是爱弥斯。"
        assert params["text_lang"] == "zh"
        assert params["ref_audio_path"] == "D:/voice/aemeath-ref.wav"
        assert params["prompt_lang"] == "zh"
        assert params["prompt_text"] == "参考音频的文本内容。"
        assert params["streaming_mode"] == "false"

    @pytest.mark.asyncio
    async def test_bracketed_expressions_are_stripped_like_the_engine(self):
        server = _FakeGSVServer()
        adapter = self._adapter(server)

        await adapter.synthesize("你好[开心]，我在呢。")

        assert server.requests[0]["params"]["text"] == "你好，我在呢。"

    @pytest.mark.asyncio
    async def test_http_error_raises_model_error(self):
        from aemeath.adapters import ModelError

        server = _FakeGSVServer()
        adapter = self._adapter(server, api_url="http://127.0.0.1:9880/nope")

        with pytest.raises(ModelError, match="500|404"):
            await adapter.synthesize("你好")

    @pytest.mark.asyncio
    async def test_streaming_mode_typo_is_rejected_before_the_request(self):
        server = _FakeGSVServer()
        with pytest.raises(ValueError, match="streaming_mode"):
            self._adapter(server, streaming_mode="ture")


# ----------------------------------------------------------------------
# AdapterFactory.build_tts
# ----------------------------------------------------------------------


class TestBuildTTS:
    """``build_tts`` exists and resolves the configured TTS engine.

    ``probe_tts`` has called ``AdapterFactory.build_tts()`` since the live
    probes were written, but no such method existed: the edge-tts ``local``
    branch returned early, so the missing method only surfaced as an
    ``AttributeError`` once any non-local engine was selected.
    """

    def test_build_tts_exists_on_the_factory(self):
        from aemeath.adapters import AdapterFactory

        assert hasattr(AdapterFactory, "build_tts")

    def test_local_backend_is_not_configured(self):
        from aemeath.adapters import AdapterFactory
        from aemeath.config import AemeathConfig, SpeechConfig

        config = AemeathConfig(speech=SpeechConfig(tts_backend="local"))
        adapter, status = AdapterFactory.from_config(config).build_tts()
        assert adapter is None
        assert not status.configured
        assert not status.enabled
        assert "local" in status.detail

    def test_gpt_sovits_backend_builds_an_adapter(self):
        from aemeath.adapters import AdapterFactory, GPTSoVITSAdapter
        from aemeath.config import AemeathConfig, SpeechConfig

        config = AemeathConfig(
            speech=SpeechConfig(tts_backend="gpt_sovits_tts"),
        )
        # The upstream gpt_sovits config block, as validated by the schema.
        # AemeathConfig is frozen; SpeechConfig now carries the block, so it
        # is attached through the speech value rather than patched onto the
        # outer config.
        object.__setattr__(
            config,
            "speech",
            SpeechConfig(
                tts_backend="gpt_sovits_tts",
                gpt_sovits={
                    "api_url": "http://127.0.0.1:9880/tts",
                    "text_lang": "zh",
                    "ref_audio_path": "D:/voice/aemeath-ref.wav",
                    "prompt_lang": "zh",
                    "prompt_text": "参考音频的文本内容。",
                    "text_split_method": "cut5",
                    "batch_size": "1",
                    "media_type": "wav",
                    "streaming_mode": "false",
                },
            ),
        )
        adapter, status = AdapterFactory.from_config(config).build_tts()
        assert isinstance(adapter, GPTSoVITSAdapter)
        assert status.configured
        assert status.enabled

    def test_invalid_streaming_mode_is_an_init_error_not_a_crash(self):
        from aemeath.adapters import AdapterFactory
        from aemeath.config import AemeathConfig, SpeechConfig

        config = AemeathConfig(speech=SpeechConfig(tts_backend="gpt_sovits_tts"))
        object.__setattr__(
            config,
            "speech",
            SpeechConfig(
                tts_backend="gpt_sovits_tts",
                gpt_sovits={
                    "api_url": "http://127.0.0.1:9880/tts",
                    "text_lang": "zh",
                    "ref_audio_path": "D:/voice/aemeath-ref.wav",
                    "prompt_lang": "zh",
                    "prompt_text": "参考音频的文本内容。",
                    "text_split_method": "cut5",
                    "batch_size": "1",
                    "media_type": "wav",
                    "streaming_mode": "ture",
                },
            ),
        )
        adapter, status = AdapterFactory.from_config(config).build_tts()
        assert adapter is None
        assert status.configured
        assert not status.enabled
        assert "streaming_mode" in (status.error or "")


# ----------------------------------------------------------------------
# probe_tts wiring
# ----------------------------------------------------------------------


class TestProbeTTSWiring:
    """The probe reports the configured engine without crashing."""

    async def test_probe_tts_reports_local_backend_as_skipped(self):
        from aemeath.config import AemeathConfig, SpeechConfig
        from aemeath.live import probe_tts

        config = AemeathConfig(speech=SpeechConfig(tts_backend="local"))
        result = await probe_tts(config)

        assert result.skipped is True
        assert result.ok is False
        assert result.configured is False
        assert "local" in result.detail
