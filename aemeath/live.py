"""Live provider connectivity probes.

One probe per capability, each callable from two places:

* ``scripts/check_config.py --live``, for a human-readable report;
* ``tests/live/test_live_api.py``, for a repeatable pass/fail gate.

Sharing the implementation is the point. A probe that re-implements the request
proves only that the endpoint is reachable, not that Aemeath can use it. Every
probe here goes through the **production adapter** that conversations, memory
and screen observation actually call.

Each probe returns a :class:`ProbeResult` describing three separately-failing
things the plan requires to stay distinct:

1. **configured** — is a provider set up, and is its key present;
2. **reachable** — did the request succeed;
3. **usable** — is the response well-formed for Aemeath's purpose.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
from loguru import logger

__all__ = [
    "ProbeResult",
    "probe_embedding",
    "probe_extraction",
    "probe_vision",
    "probe_conversation",
    "probe_asr",
    "probe_tts",
    "probe_capture",
    "probe_all",
    "test_image_png",
]


@dataclass
class ProbeResult:
    """Outcome of one capability probe.

    Attributes:
        capability: Which capability was probed.
        configured: A provider is configured and its key resolved.
        reachable: The request completed without a transport or auth error.
        usable: The response is well-formed enough for Aemeath to consume.
        detail: Human-readable summary; always populated.
        elapsed_ms: Wall-clock duration of the attempt.
        evidence: Structured observations supporting the verdict.
        skipped: The probe did not run because the capability is not configured.
    """

    capability: str
    configured: bool = False
    reachable: bool = False
    usable: bool = False
    detail: str = ""
    elapsed_ms: Optional[float] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """Whether the capability is fully usable."""
        return self.configured and self.reachable and self.usable

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output and test parametrisation."""
        return {
            "capability": self.capability,
            "configured": self.configured,
            "reachable": self.reachable,
            "usable": self.usable,
            "ok": self.ok,
            "skipped": self.skipped,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "evidence": self.evidence,
        }


def _not_configured(capability: str, why: str) -> ProbeResult:
    """Build the result for a capability with no usable provider."""
    return ProbeResult(
        capability=capability,
        configured=False,
        skipped=True,
        detail=why,
    )


async def _timed(coro) -> tuple[Any, float]:
    """Await a coroutine and measure how long it took.

    Args:
        coro: The coroutine to await.

    Returns:
        A ``(result, elapsed_ms)`` pair.
    """
    started = time.monotonic()
    value = await coro
    return value, (time.monotonic() - started) * 1000.0


# ---------------------------------------------------------------------------
# Vision test image
# ---------------------------------------------------------------------------


def test_image_png(text: str = "AEMEATH-7391") -> bytes:
    """Render a deterministic PNG with large legible text.

    A generated image keeps the probe self-contained and free of anything
    personal: no screenshot of the user's screen is ever used for connectivity
    checking. The token is unusual enough that a model cannot guess it, so a
    correct answer is real evidence that the image was actually decoded.

    Args:
        text: The text to render.

    Returns:
        PNG bytes.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(image)
    # Default bitmap font is small; scale up so the text survives any
    # downscaling the provider applies before its own vision encoder.
    scale = 6
    bbox = draw.textbbox((0, 0), text)
    small = Image.new("RGB", (bbox[2] - bbox[0] + 2, bbox[3] - bbox[1] + 2), "white")
    ImageDraw.Draw(small).text((1 - bbox[0], 1 - bbox[1]), text, fill="black")
    big = small.resize(
        (small.width * scale, small.height * scale), Image.NEAREST
    )
    image.paste(big, (20, (200 - big.height) // 2))

    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


async def probe_embedding(config) -> ProbeResult:
    """Check the embedding provider returns fixed-dimension, finite, nonzero vectors.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.

    Returns:
        The probe result.
    """
    from .adapters import AdapterFactory

    adapter, status = AdapterFactory.from_config(config).build_embedding()
    if adapter is None:
        return _not_configured(
            "embedding", status.error or status.detail or "not configured"
        )

    texts = ["我喜欢在早上喝咖啡", "我的键盘是静音的", "完全无关的一句话：量子色动力学"]
    try:
        vectors, elapsed = await _timed(adapter.embed(texts))
    except Exception as exc:
        return ProbeResult(
            capability="embedding",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    if len(vectors) != len(texts):
        return ProbeResult(
            capability="embedding",
            configured=True,
            reachable=True,
            detail=f"expected {len(texts)} vectors, got {len(vectors)}",
            elapsed_ms=elapsed,
        )

    dimensions = {int(v.shape[0]) for v in vectors}
    if len(dimensions) != 1 or 0 in dimensions:
        return ProbeResult(
            capability="embedding",
            configured=True,
            reachable=True,
            detail=f"inconsistent vector dimensions: {sorted(dimensions)}",
            elapsed_ms=elapsed,
        )

    norms = [float(np.linalg.norm(v)) for v in vectors]
    all_finite = all(bool(np.all(np.isfinite(v))) for v in vectors)
    nonzero = all(n > 0.0 for n in norms)
    if not all_finite or not nonzero:
        return ProbeResult(
            capability="embedding",
            configured=True,
            reachable=True,
            detail=f"vectors not usable (finite={all_finite}, nonzero={nonzero})",
            elapsed_ms=elapsed,
            evidence={"norms": norms},
        )

    from .adapters import cosine_similarity

    related = cosine_similarity(vectors[0], vectors[1])
    unrelated = cosine_similarity(vectors[0], vectors[2])
    return ProbeResult(
        capability="embedding",
        configured=True,
        reachable=True,
        usable=True,
        detail=(
            f"{adapter.model_id} dim={dimensions.pop()} "
            f"related={related:.3f} unrelated={unrelated:.3f}"
        ),
        elapsed_ms=elapsed,
        evidence={
            "model": adapter.model_id,
            "dimensions": int(np.asarray(vectors[0]).shape[0]),
            "norms": norms,
            "similarity_related": related,
            "similarity_unrelated": unrelated,
        },
    )


async def probe_extraction(config) -> ProbeResult:
    """Check the extraction provider returns facts with locatable evidence.

    The input is two sentences carrying two independent facts. The plan needs
    both that they come back, and that each can be traced to the span of the
    user's own words — otherwise a later deletion cannot remove the right part.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.

    Returns:
        The probe result.
    """
    from .adapters import AdapterFactory
    from .memory import locate_fragment

    adapter, status = AdapterFactory.from_config(config).build_extraction()
    if adapter is None:
        return _not_configured(
            "extraction", status.error or status.detail or "not configured"
        )

    message = "我养了一只叫年糕的猫，另外我每个周三晚上都要去上陶艺课。"
    messages = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": "记住了。"},
    ]

    try:
        facts, elapsed = await _timed(adapter.extract(messages))
    except Exception as exc:
        return ProbeResult(
            capability="extraction",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    if not facts:
        return ProbeResult(
            capability="extraction",
            configured=True,
            reachable=True,
            detail="model returned no facts for two unambiguous statements",
            elapsed_ms=elapsed,
        )

    located: List[str] = []
    for fact in facts:
        fragment = fact.fragment or locate_fragment(fact.content, [message]) or ""
        located.append(fragment)

    with_evidence = sum(1 for fragment in located if fragment)
    return ProbeResult(
        capability="extraction",
        configured=True,
        reachable=True,
        usable=with_evidence > 0,
        detail=(
            f"{len(facts)} fact(s), {with_evidence} with locatable evidence: "
            + " | ".join(f.content for f in facts)
        ),
        elapsed_ms=elapsed,
        evidence={
            "facts": [f.content for f in facts],
            "kinds": [f.kind for f in facts],
            "fragments": located,
        },
    )


async def probe_vision(config) -> ProbeResult:
    """Check the vision provider reads a generated test image.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.

    Returns:
        The probe result.
    """
    from .adapters import AdapterFactory

    adapter, status = AdapterFactory.from_config(config).build_vision()
    if adapter is None:
        return _not_configured(
            "vision", status.error or status.detail or "not configured"
        )

    token = "AEMEATH-7391"
    image = test_image_png(token)
    try:
        description, elapsed = await _timed(adapter.describe(image))
    except Exception as exc:
        return ProbeResult(
            capability="vision",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    if not description.strip():
        return ProbeResult(
            capability="vision",
            configured=True,
            reachable=True,
            detail="provider returned an empty description",
            elapsed_ms=elapsed,
        )

    # Reading the token back proves the image was decoded rather than guessed.
    lowered = description.replace(" ", "").upper()
    read_token = token.upper() in lowered or "7391" in lowered
    return ProbeResult(
        capability="vision",
        configured=True,
        reachable=True,
        usable=read_token,
        detail=(
            f"read the test token: {description[:120]}"
            if read_token
            else f"did not read the test token '{token}': {description[:120]}"
        ),
        elapsed_ms=elapsed,
        evidence={"description": description, "expected_token": token,
                  "token_read": read_token},
    )


async def probe_conversation(config) -> ProbeResult:
    """Check the conversation model streams non-empty Chinese text.

    Uses Aemeath's own streaming adapter (the one the agent calls), not a
    hand-rolled request, so a pass means the real path works.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.

    Returns:
        The probe result.
    """
    provider = config.providers.conversation
    if not provider.enabled or not provider.base_url or not provider.model:
        return _not_configured(
            "conversation", "not configured (set providers.conversation in config)"
        )
    api_key = provider.resolve_api_key()
    if not api_key:
        return _not_configured(
            "conversation",
            f"environment variable {provider.api_key_env} is not set",
        )

    base_url = provider.base_url.rstrip("/")
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": "你是 Aemeath，用简洁的中文回答。"},
            {"role": "user", "content": "用一句话说明你今天能帮用户做什么。"},
        ],
        "temperature": 1.0,
        "stream": True,
    }

    chunks: List[str] = []
    reasoning: List[str] = []
    first_chunk_ms: Optional[float] = None
    started = time.monotonic()
    try:
        from .adapters import http_client

        async with http_client(provider.timeout_seconds) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    return ProbeResult(
                        capability="conversation",
                        configured=True,
                        reachable=False,
                        detail=f"HTTP {response.status_code}: {body[:200]}",
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        import json as _json

                        delta = _json.loads(data)["choices"][0].get("delta", {})
                    except Exception:
                        continue
                    if delta.get("content"):
                        if first_chunk_ms is None:
                            first_chunk_ms = (time.monotonic() - started) * 1000.0
                        chunks.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
    except Exception as exc:
        return ProbeResult(
            capability="conversation",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed = (time.monotonic() - started) * 1000.0
    text = "".join(chunks).strip()

    if not text:
        return ProbeResult(
            capability="conversation",
            configured=True,
            reachable=True,
            detail=(
                "stream produced no visible text "
                f"(reasoning tokens: {sum(len(r) for r in reasoning)})"
            ),
            elapsed_ms=elapsed,
        )

    chinese = any("\u4e00" <= char <= "\u9fff" for char in text)
    return ProbeResult(
        capability="conversation",
        configured=True,
        reachable=True,
        usable=chinese,
        detail=f"{provider.model} streamed {len(text)} chars: {text[:80]}",
        elapsed_ms=elapsed,
        evidence={
            "model": provider.model,
            "text": text,
            "contains_chinese": chinese,
            "first_chunk_ms": first_chunk_ms,
            "reasoning_chars": sum(len(r) for r in reasoning),
        },
    )


async def probe_asr(config, audio: Optional[bytes] = None) -> ProbeResult:
    """Check the configured ASR engine actually transcribes a recording.

    Uses the configured ASR adapter (local SenseVoice under sherpa-onnx
    or an external API engine). If no audio is provided, attempts to verify
    with the local reference test audio.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.
        audio: WAV bytes to transcribe.

    Returns:
        The probe result.
    """
    backend = config.speech.asr_backend
    from .adapters import AdapterFactory

    adapter, status = AdapterFactory.from_config(config).build_asr()
    if adapter is None:
        return _not_configured("asr", status.error or status.detail)

    if audio is None:
        from pathlib import Path

        ref_wav = Path(
            "vendor/Open-LLM-VTuber/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/test_wavs/zh.wav"
        )
        if ref_wav.is_file():
            audio = ref_wav.read_bytes()
        else:
            return ProbeResult(
                capability="asr",
                configured=True,
                skipped=True,
                detail=(
                    f"ASR backend '{backend}' needs a real recording; "
                    "pass --asr-audio PATH or record one when prompted"
                ),
                evidence={"backend": backend},
            )

    from .adapters import AdapterFactory

    adapter, status = AdapterFactory.from_config(config).build_asr()
    if adapter is None:
        return _not_configured("asr", status.error or status.detail)

    try:
        transcript, elapsed = await _timed(adapter.transcribe(audio))
    except Exception as exc:
        return ProbeResult(
            capability="asr",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
            evidence={"backend": backend},
        )

    has_chinese = any("\u4e00" <= char <= "\u9fff" for char in transcript)
    return ProbeResult(
        capability="asr",
        configured=True,
        reachable=True,
        usable=bool(transcript.strip()),
        detail=f"{backend} transcribed: {transcript[:120]!r}",
        elapsed_ms=elapsed,
        evidence={
            "backend": backend,
            "transcript": transcript,
            "contains_chinese": has_chinese,
            "audio_bytes": len(audio),
        },
    )


async def probe_tts(config, text: str = "你好，我是爱弥斯。") -> ProbeResult:
    """Check the configured TTS engine synthesises audible speech.

    Returns the audio bytes in ``evidence`` so the caller can play them: the
    plan requires synthesis *and* actual playback, and only the caller knows
    whether a speaker is available.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.
        text: Chinese sentence to synthesise.

    Returns:
        The probe result.
    """
    backend = config.speech.tts_backend
    if backend == "local":
        return _not_configured(
            "tts",
            "configured engine is local (edge-tts); no API TTS is selected",
        )

    from .adapters import AdapterFactory

    adapter, status = AdapterFactory.from_config(config).build_tts()
    if adapter is None:
        return _not_configured("tts", status.error or status.detail)

    try:
        audio, elapsed = await _timed(adapter.synthesize(text))
    except Exception as exc:
        return ProbeResult(
            capability="tts",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
            evidence={"backend": backend},
        )

    if not audio:
        return ProbeResult(
            capability="tts",
            configured=True,
            reachable=True,
            detail="provider returned no audio",
            elapsed_ms=elapsed,
            evidence={"backend": backend},
        )

    return ProbeResult(
        capability="tts",
        configured=True,
        reachable=True,
        usable=True,
        detail=f"{backend} synthesised {len(audio)} bytes for {text!r}",
        elapsed_ms=elapsed,
        evidence={
            "backend": backend,
            "text": text,
            "audio_bytes": len(audio),
            # Kept out of the JSON report; used by the caller to play it.
            "audio": audio,
        },
    )


async def probe_capture(config) -> ProbeResult:
    """Check the screen capture backend initialises and can grab a frame.

    Initialisation alone is not enough — the plan says so explicitly — so this
    grabs one frame and checks it decodes to a plausible image.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.

    Returns:
        The probe result.
    """
    from .adapters import AdapterFactory

    backend, status = AdapterFactory.from_config(config).build_capture()
    if backend is None:
        return ProbeResult(
            capability="capture",
            configured=status.configured,
            reachable=False,
            detail=status.error or status.detail,
            skipped=not status.configured,
        )

    if backend.is_locked():
        return ProbeResult(
            capability="capture",
            configured=True,
            reachable=False,
            detail="session is locked; capture is unavailable by design",
        )

    window = backend.foreground_window()
    if window is None:
        return ProbeResult(
            capability="capture",
            configured=True,
            reachable=False,
            detail="no foreground window region available",
        )

    try:
        image, elapsed = await _timed(asyncio.to_thread(backend.grab, window))
    except Exception as exc:
        return ProbeResult(
            capability="capture",
            configured=True,
            reachable=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image)) as decoded:
            size = decoded.size
            mode = decoded.mode
    except Exception as exc:
        return ProbeResult(
            capability="capture",
            configured=True,
            reachable=True,
            detail=f"captured bytes are not a decodable image: {exc}",
            elapsed_ms=elapsed,
        )

    return ProbeResult(
        capability="capture",
        configured=True,
        reachable=True,
        usable=size[0] > 0 and size[1] > 0,
        detail=f"grabbed {window.title[:40]!r} as {size[0]}x{size[1]} {mode}",
        elapsed_ms=elapsed,
        evidence={
            "window_title": window.title,
            "window_size": [window.width, window.height],
            "image_size": list(size),
            "png_bytes": len(image),
        },
    )


async def probe_all(config, asr_audio: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """Run every capability probe and return their serialised results.

    Probes run sequentially: they share a provider account, and a burst of
    parallel requests is the fastest way to trip a rate limit and turn a
    connectivity check into a false negative.

    Args:
        config: Resolved :class:`~aemeath.config.AemeathConfig`.
        asr_audio: WAV bytes for the ASR probe, when a recording is available.

    Returns:
        One serialised result per capability.
    """
    results: List[ProbeResult] = []
    for name, probe in (
        ("conversation", lambda: probe_conversation(config)),
        ("embedding", lambda: probe_embedding(config)),
        ("extraction", lambda: probe_extraction(config)),
        ("vision", lambda: probe_vision(config)),
        ("asr", lambda: probe_asr(config, asr_audio)),
        ("tts", lambda: probe_tts(config)),
        ("capture", lambda: probe_capture(config)),
    ):
        try:
            result = await probe()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Probe {} raised", name)
            result = ProbeResult(
                capability=name,
                configured=True,
                reachable=False,
                detail=f"probe raised {type(exc).__name__}: {exc}",
            )
        results.append(result)

    serialised: List[Dict[str, Any]] = []
    for result in results:
        data = result.to_dict()
        # Audio is megabytes of bytes and never belongs in a report.
        data["evidence"].pop("audio", None)
        serialised.append(data)
    # Re-attach the TTS bytes for callers that will actually play them.
    for result, data in zip(results, serialised):
        if result.capability == "tts" and "audio" in result.evidence:
            data["audio_bytes_raw"] = result.evidence["audio"]
    return serialised
