"""Model adapters.

Each capability is configured independently so a provider can be swapped
without touching the modules that use it. The plan is explicit that "OpenAI
compatible" must not be assumed to work for every endpoint, so adapters declare
what they support and fail with a clear message otherwise.

The fake adapters let the whole pipeline be exercised without credentials;
final acceptance still requires real providers.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from loguru import logger

__all__ = [
    "ModelError",
    "AuthError",
    "RateLimitError",
    "EmbeddingAdapter",
    "FakeEmbeddingAdapter",
    "OpenAICompatibleEmbedding",
    "ExtractionAdapter",
    "FakeExtractionAdapter",
    "OpenAICompatibleExtraction",
    "OpenAICompatibleVision",
    "AdapterFactory",
    "RetryPolicy",
    "cosine_similarity",
    "http_client",
    "TLS_INSECURE_ENV",
]


class ModelError(RuntimeError):
    """Base class for model adapter failures."""


class AuthError(ModelError):
    """Credentials were rejected, or a model name is wrong."""


class RateLimitError(ModelError):
    """The provider asked us to slow down or failed transiently."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry behaviour for provider calls.

    The plan allows at most two retries and only when no content has been
    produced yet, so a partially delivered reply is never regenerated.
    """

    max_retries: int = 2

    def should_retry(self, attempt: int, produced_output: bool) -> bool:
        """Whether another attempt is permitted.

        Args:
            attempt: Zero-based attempt index that just failed.
            produced_output: Whether any content was already emitted.

        Returns:
            ``True`` when a retry is allowed.
        """
        if produced_output:
            return False
        return attempt < self.max_retries


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Similarity in ``[-1, 1]``; ``0.0`` when either vector is zero-length.
    """
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


#: Environment variable that relaxes TLS verification for outbound model calls.
#:
#: This machine reaches providers through a local proxy whose CA is self-signed,
#: so verification fails with ``CERTIFICATE_VERIFY_FAILED`` even though the
#: traffic is fine. Setting ``AEMEATH_TLS_INSECURE=1`` disables verification
#: **for model API calls only**; the first such call logs a warning, so an
#: insecure run is never silent.
TLS_INSECURE_ENV = "AEMEATH_TLS_INSECURE"

_tls_warning_emitted = False


def http_client(timeout: float):
    """Build an HTTP client for provider calls, applying the TLS policy.

    Centralised so the verification decision is made in exactly one place
    instead of being repeated — and eventually diverging — across adapters.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        A configured ``httpx.AsyncClient``.
    """
    global _tls_warning_emitted

    import httpx

    insecure = os.getenv(TLS_INSECURE_ENV, "").strip() not in ("", "0", "false")
    if insecure and not _tls_warning_emitted:
        _tls_warning_emitted = True
        logger.warning(
            "TLS certificate verification is DISABLED for model API calls "
            "({}=1). Use only against a trusted local proxy.",
            TLS_INSECURE_ENV,
        )
    return httpx.AsyncClient(timeout=timeout, verify=not insecure)


class EmbeddingAdapter:
    """Interface for text embedding providers."""

    #: Identifier persisted alongside vectors so incompatible ones are never mixed.
    model_id: str = "unknown"
    dimensions: int = 0

    async def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        """Embed a batch of texts.

        Args:
            texts: Texts to embed.

        Returns:
            One vector per input text.
        """
        raise NotImplementedError


class FakeEmbeddingAdapter(EmbeddingAdapter):
    """Deterministic embedding stand-in for tests.

    Produces a bag-of-characters hash vector, so texts sharing characters
    (including Chinese ones) score as similar without a provider.
    """

    def __init__(self, dimensions: int = 64, model_id: str = "fake-embed") -> None:
        """Configure the fake embedding size and identifier."""
        self.dimensions = dimensions
        self.model_id = model_id
        self.calls: List[List[str]] = []
        self.fail = False

    async def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        """Embed texts using a deterministic character-hash bag of words."""
        self.calls.append(list(texts))
        if self.fail:
            raise ModelError("embedding provider unavailable")

        vectors: List[np.ndarray] = []
        for text in texts:
            vector = np.zeros(self.dimensions, dtype=np.float32)
            for char in text:
                vector[ord(char) % self.dimensions] += 1.0
            norm = np.linalg.norm(vector)
            if norm > 0:
                vector = vector / norm
            vectors.append(vector)
        return vectors


class OpenAICompatibleEmbedding(EmbeddingAdapter):
    """Embedding adapter for OpenAI-compatible ``/embeddings`` endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 0,
        timeout: float = 30.0,
    ) -> None:
        """Store connection settings.

        Args:
            base_url: Provider base URL.
            api_key: Provider credential.
            model: Embedding model name.
            dimensions: Expected vector size; ``0`` means "learn on first call".
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model
        self.dimensions = dimensions
        self.timeout = timeout

    async def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        """Call the provider's embeddings endpoint."""
        if not self.api_key:
            raise AuthError("embedding API key is not configured")

        payload: Dict[str, Any] = {"model": self.model_id, "input": list(texts)}
        try:
            async with http_client(self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except Exception as exc:
            raise ModelError(f"embedding request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(
                f"embedding provider rejected credentials (HTTP {response.status_code})"
            )
        if response.status_code == 429:
            raise RateLimitError("embedding provider rate limited the request")
        if response.status_code >= 400:
            raise ModelError(
                f"embedding provider returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            vectors = [np.asarray(item["embedding"], dtype=np.float32) for item in items]
        except Exception as exc:
            raise ModelError(f"unexpected embedding response shape: {exc}") from exc

        if vectors and not self.dimensions:
            self.dimensions = int(vectors[0].shape[0])
        return vectors


@dataclass
class ExtractedFact:
    """A fact or experience proposed by the extraction model.

    Attributes:
        content: The remembered text, as written by the model.
        kind: ``fact`` or ``experience``.
        fragment: The exact span of the source message this was derived from.
            The plan requires every new extraction to cite verifiable source
            text, so deletion can remove that span and leave the rest of the
            message intact. When the model does not supply one, the caller
            attempts a deterministic match and reports the shortfall rather
            than guessing.
    """

    content: str
    kind: str = "fact"
    fragment: str = ""


class ExtractionAdapter:
    """Interface for turning conversation into memories."""

    async def extract(
        self, messages: Sequence[Dict[str, str]]
    ) -> List[ExtractedFact]:
        """Extract stable facts and experiences from recent messages."""
        raise NotImplementedError


class FakeExtractionAdapter(ExtractionAdapter):
    """Extraction stand-in driven by simple markers in the text."""

    def __init__(self, facts: Optional[List[ExtractedFact]] = None) -> None:
        """Optionally fix the output for deterministic tests."""
        self.facts = facts
        self.calls: List[Sequence[Dict[str, str]]] = []

    async def extract(
        self, messages: Sequence[Dict[str, str]]
    ) -> List[ExtractedFact]:
        """Return configured facts, or parse ``记住：`` markers."""
        self.calls.append(messages)
        if self.facts is not None:
            return list(self.facts)

        results: List[ExtractedFact] = []
        for message in messages:
            if message.get("role") != "user":
                continue
            for match in re.finditer(r"记住[：:]\s*(.+)", message.get("content", "")):
                results.append(ExtractedFact(content=match.group(1).strip()))
        return results


class OpenAICompatibleExtraction(ExtractionAdapter):
    """Extraction adapter using a chat model with a JSON contract.

    The model is asked for structured output and the result is validated before
    anything is stored: an extraction that cannot be parsed is discarded rather
    than turned into a malformed memory.
    """

    _SYSTEM = (
        "你从对话中提取关于用户的稳定事实。只提取用户明确表达、"
        "且在未来对话中仍然成立的信息，例如称呼、长期偏好、工作习惯、"
        "固定安排。不要提取一次性的临时状态，不要提取角色的回复内容，"
        "不要根据猜测补充。\n"
        "只输出 JSON，格式：{\"facts\": [{\"content\": \"...\", \"kind\": \"fact\"}]}\n"
        "没有可提取的内容时输出 {\"facts\": []}。"
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        """Store connection settings."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def extract(
        self, messages: Sequence[Dict[str, str]]
    ) -> List[ExtractedFact]:
        """Ask the model for facts and validate the returned structure."""
        if not self.api_key:
            raise AuthError("extraction API key is not configured")

        transcript = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.0,
        }

        try:
            async with http_client(self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except Exception as exc:
            raise ModelError(f"extraction request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError("extraction provider rejected credentials")
        if response.status_code == 429:
            raise RateLimitError("extraction provider rate limited the request")
        if response.status_code >= 400:
            raise ModelError(
                f"extraction provider returned HTTP {response.status_code}"
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
            # Tolerate a fenced code block around the JSON object.
            cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M)
            data = json.loads(cleaned)
        except Exception as exc:
            raise ModelError(f"extraction returned unparseable output: {exc}") from exc

        facts: List[ExtractedFact] = []
        for item in data.get("facts", []) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            kind = str(item.get("kind", "fact")).strip() or "fact"
            if kind not in ("fact", "experience"):
                kind = "fact"
            facts.append(ExtractedFact(content=text, kind=kind))

        logger.debug("Extraction produced {} item(s).", len(facts))
        return facts


class OpenAICompatibleVision:
    """Vision adapter for OpenAI-compatible chat endpoints with image input.

    Screenshots are sent as a base64 data URL and are never written to disk.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        """Store connection settings."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    _SYSTEM = (
        "你在看用户电脑的前台窗口截图。用一两句中文描述用户当前在做什么，"
        "只描述你确实看到的内容，不要猜测用户的目的，不要编造看不清的细节。"
    )

    async def describe(self, image: bytes, window_title: str = "") -> str:
        """Ask the model to describe a screenshot."""
        import base64

        if not self.api_key:
            raise AuthError("vision API key is not configured")

        encoded = base64.b64encode(image).decode("ascii")
        prompt = "描述这个窗口的内容。"
        if window_title:
            prompt = f"窗口标题：{window_title}\n描述这个窗口的内容。"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
            "temperature": 0.0,
        }

        try:
            async with http_client(self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except Exception as exc:
            raise ModelError(f"vision request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError("vision provider rejected credentials")
        if response.status_code == 429:
            raise RateLimitError("vision provider rate limited the request")
        if response.status_code >= 400:
            raise ModelError(f"vision provider returned HTTP {response.status_code}")

        try:
            return str(response.json()["choices"][0]["message"]["content"]).strip()
        except Exception as exc:
            raise ModelError(f"unexpected vision response shape: {exc}") from exc


class AdapterFactory:
    """Builds model adapters from configuration.

    Each capability is resolved independently and its outcome is reported as a
    :class:`~aemeath.runtime.CapabilityStatus`, distinguishing "the user turned
    this off" from "this failed to initialise". The second case must never be
    silently downgraded to ``None`` and then reported as ready.
    """

    def __init__(self, config) -> None:
        """Store the resolved Aemeath configuration."""
        self._config = config

    @classmethod
    def from_config(cls, config) -> "AdapterFactory":
        """Create a factory for a resolved configuration."""
        return cls(config)

    @staticmethod
    def _status(name: str, *, configured: bool, enabled: bool,
                error: Optional[str] = None, detail: str = ""):
        """Build a capability status without importing the runtime module."""
        from .runtime import CapabilityStatus

        return CapabilityStatus(
            name=name,
            configured=configured,
            enabled=enabled,
            error=error,
            detail=detail,
        )

    def build_embedding(self):
        """Build the embedding adapter and report its state."""
        provider = self._config.providers.embedding
        if not provider.enabled or not provider.base_url or not provider.model:
            return None, self._status(
                "embedding",
                configured=False,
                enabled=False,
                detail="not configured (set providers.embedding in config)",
            )

        api_key = provider.resolve_api_key()
        if not api_key:
            # Configured but unusable: this is an error, not "disabled".
            return None, self._status(
                "embedding",
                configured=True,
                enabled=False,
                error=f"environment variable {provider.api_key_env} is not set",
            )

        try:
            adapter = OpenAICompatibleEmbedding(
                base_url=provider.base_url,
                api_key=api_key,
                model=provider.model,
                dimensions=provider.dimensions,
                timeout=provider.timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - constructor is trivial
            return None, self._status(
                "embedding", configured=True, enabled=False, error=str(exc)
            )

        return adapter, self._status(
            "embedding",
            configured=True,
            enabled=True,
            detail=f"{provider.provider}:{provider.model}",
        )

    def build_extraction(self):
        """Build the memory-extraction adapter and report its state."""
        provider = self._config.providers.extraction
        if not provider.enabled or not provider.base_url or not provider.model:
            return None, self._status(
                "extraction",
                configured=False,
                enabled=False,
                detail="not configured (set providers.extraction in config)",
            )

        api_key = provider.resolve_api_key()
        if not api_key:
            return None, self._status(
                "extraction",
                configured=True,
                enabled=False,
                error=f"environment variable {provider.api_key_env} is not set",
            )

        try:
            adapter = OpenAICompatibleExtraction(
                base_url=provider.base_url,
                api_key=api_key,
                model=provider.model,
                timeout=provider.timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover
            return None, self._status(
                "extraction", configured=True, enabled=False, error=str(exc)
            )

        return adapter, self._status(
            "extraction",
            configured=True,
            enabled=True,
            detail=f"{provider.provider}:{provider.model}",
        )

    def build_vision(self):
        """Build the vision adapter and report its state."""
        provider = self._config.providers.vision
        if not provider.enabled or not provider.base_url or not provider.model:
            return None, self._status(
                "vision",
                configured=False,
                enabled=False,
                detail="not configured (set providers.vision in config)",
            )

        api_key = provider.resolve_api_key()
        if not api_key:
            return None, self._status(
                "vision",
                configured=True,
                enabled=False,
                error=f"environment variable {provider.api_key_env} is not set",
            )

        try:
            adapter = OpenAICompatibleVision(
                base_url=provider.base_url,
                api_key=api_key,
                model=provider.model,
                timeout=provider.timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover
            return None, self._status(
                "vision", configured=True, enabled=False, error=str(exc)
            )

        return adapter, self._status(
            "vision",
            configured=True,
            enabled=True,
            detail=f"{provider.provider}:{provider.model}",
        )

    def build_capture(self):
        """Build the platform capture backend and report a concrete reason.

        When the optional capture dependencies are missing the reported error
        names them, so "screen observation does not work" always has a cause
        rather than being an unexplained ``None``.
        """
        if not self._config.screen_capture_enabled:
            return None, self._status(
                "capture",
                configured=False,
                enabled=False,
                detail="screen capture disabled in configuration",
            )

        from .screen import WindowsCaptureBackend

        backend = WindowsCaptureBackend()
        if not backend.capture_available:
            return None, self._status(
                "capture",
                configured=True,
                enabled=False,
                error=backend.unavailable_reason
                or "screen capture dependencies are not importable",
            )

        return backend, self._status(
            "capture", configured=True, enabled=True, detail="windows (mss+pygetwindow)"
        )
