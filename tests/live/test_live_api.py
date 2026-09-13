"""Live provider connectivity tests.

These call **real** providers and are excluded from the default run. Select them
explicitly:

    pytest -m live_api

They exist because the plan requires connectivity to be a repeatable check
rather than a one-off manual observation, and because a marker declaration
without test cases proves nothing.

Every test goes through the production path: the resolved config, the
``AdapterFactory``, and the adapters that conversations, memory and screen
observation actually call. A test that wrote its own request would only prove
the endpoint is reachable, not that Aemeath can use it.

Configuration comes from the same acceptance config the manual run uses, so
"the API works" means the same thing in both places. Set ``AEMEATH_CONFIG`` (or
pass ``--live-config PATH``) to point somewhere else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from aemeath.config import load_config, resolve_config_path  # noqa: E402
from aemeath.live import (  # noqa: E402
    probe_asr,
    probe_capture,
    probe_conversation,
    probe_embedding,
    probe_extraction,
    probe_tts,
    probe_vision,
)

pytestmark = pytest.mark.live_api


def _config():
    """Load the config the live run should use."""
    explicit = os.getenv("AEMEATH_LIVE_CONFIG")
    return load_config(resolve_config_path(explicit) if explicit else None)


def _require(result):
    """Fail with the probe's own diagnosis unless the capability is usable.

    Not-configured is skipped rather than failed: on this machine four of the six
    capabilities have no provider at all, and reporting "no provider configured"
    as a test failure would hide the real, recorded blocker behind noise. A
    *configured* capability that then fails does fail the test.
    """
    if result.skipped:
        pytest.skip(result.detail)
    assert result.ok, f"{result.capability}: {result.detail}"
    return result


async def test_conversation_returns_chinese_text():
    """The conversation model streams non-empty Chinese text."""
    result = await probe_conversation(_config())
    _require(result)
    assert result.evidence["contains_chinese"], result.evidence["text"][:200]


async def test_embedding_returns_usable_vectors():
    """Embeddings are fixed-dimension, finite and non-zero."""
    result = await probe_embedding(_config())
    _require(result)
    assert result.evidence["dimensions"] > 0


async def test_extraction_returns_facts_with_evidence():
    """Extraction yields facts traceable to a span of the user's message."""
    result = await probe_extraction(_config())
    _require(result)
    assert result.evidence["facts"], "no facts extracted"
    assert any(result.evidence["fragments"]), "no fact had a locatable fragment"


async def test_vision_reads_generated_image():
    """The vision model reads a token from a generated test image.

    Reading the token back is the check: a model that never decoded the image
    cannot produce it.
    """
    result = await probe_vision(_config())
    _require(result)
    assert result.evidence["token_read"], result.evidence["description"][:200]


async def test_asr_transcribes_recording():
    """The API ASR engine transcribes a real recording.

    Skipped unless a recording is supplied through ``AEMEATH_ASR_AUDIO``: a
    transcription test cannot invent speech, and synthesising the audio with the
    engine under test would make the assertion circular.
    """
    audio_path = os.getenv("AEMEATH_ASR_AUDIO")
    audio = Path(audio_path).read_bytes() if audio_path and Path(audio_path).is_file() else None
    result = await probe_asr(_config(), audio)
    _require(result)
    assert result.evidence["transcript"].strip()


async def test_tts_synthesises_audio():
    """The API TTS engine returns audio for a Chinese sentence.

    Synthesis only. Playing it back needs a speaker, so playback is a separate
    manual step rather than something a test can assert.
    """
    result = await probe_tts(_config())
    _require(result)
    assert result.evidence["audio_bytes"] > 0


async def test_screen_capture_grabs_a_frame():
    """The capture backend grabs and decodes a real frame.

    Initialisation alone is explicitly not enough: the plan says so, and the
    whole point of this check is that a passing capture capability means frames
    were actually produced.
    """
    result = await probe_capture(_config())
    _require(result)
    assert result.evidence["image_size"][0] > 0
    assert result.evidence["image_size"][1] > 0
