"""Integration tests for SenseVoice local ASR adapter and factory.

Tests that SenseVoice is properly loaded, configuration paths are resolved,
and audio is transcribed correctly.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest
import numpy as np

from aemeath.adapters import AdapterFactory, SenseVoiceAdapter
from aemeath.config import AemeathConfig, SpeechConfig


ROOT = Path(__file__).resolve().parent.parent.parent
WAV_PATH = (
    ROOT
    / "vendor"
    / "Open-LLM-VTuber"
    / "models"
    / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    / "test_wavs"
    / "zh.wav"
)
MODEL_DIR = (
    ROOT
    / "vendor"
    / "Open-LLM-VTuber"
    / "models"
    / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
)


class TestSenseVoiceAdapter:
    """Test direct transcribe behavior of SenseVoiceAdapter."""

    def test_missing_model_raises_file_not_found(self):
        adapter = SenseVoiceAdapter(
            model_path="non_existent_model.onnx",
            tokens_path="non_existent_tokens.txt",
        )
        with pytest.raises(FileNotFoundError, match="model not found"):
            adapter._get_recognizer()

    def test_missing_tokens_raises_file_not_found(self):
        adapter = SenseVoiceAdapter(
            model_path=str(MODEL_DIR / "model.int8.onnx"),
            tokens_path="non_existent_tokens.txt",
        )
        with pytest.raises(FileNotFoundError, match="tokens not found"):
            adapter._get_recognizer()

    @pytest.mark.asyncio
    async def test_transcribe_wav_bytes(self):
        if not WAV_PATH.is_file():
            pytest.skip("Test wav file missing")
        adapter = SenseVoiceAdapter(
            model_path=str(MODEL_DIR / "model.int8.onnx"),
            tokens_path=str(MODEL_DIR / "tokens.txt"),
        )
        wav_bytes = WAV_PATH.read_bytes()
        text = await adapter.transcribe(wav_bytes)
        assert "开放时间" in text or "9点" in text

    def test_transcribe_np_array(self):
        if not WAV_PATH.is_file():
            pytest.skip("Test wav file missing")
        import wave

        with wave.open(str(WAV_PATH), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        adapter = SenseVoiceAdapter(
            model_path=str(MODEL_DIR / "model.int8.onnx"),
            tokens_path=str(MODEL_DIR / "tokens.txt"),
        )
        text = adapter.transcribe_np(audio, 16000)
        assert "开放时间" in text or "9点" in text


class TestSenseVoiceFactory:
    """Test AdapterFactory.build_asr() with SenseVoice configuration."""

    def test_build_asr_from_config(self):
        config = AemeathConfig(
            speech=SpeechConfig(
                asr_backend="sherpa_onnx_asr",
                sherpa_onnx={
                    "model_type": "sense_voice",
                    "sense_voice": "./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx",
                    "tokens": "./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt",
                },
            )
        )
        factory = AdapterFactory.from_config(config)
        adapter, status = factory.build_asr()
        assert status.state == "ready"
        assert adapter is not None
        assert isinstance(adapter, SenseVoiceAdapter)

    def test_build_asr_unconfigured(self):
        config = AemeathConfig(
            speech=SpeechConfig(
                asr_backend="unknown_asr",
            )
        )
        factory = AdapterFactory.from_config(config)
        adapter, status = factory.build_asr()
        assert adapter is None
        assert status.state == "disabled"
