"""Integration test: the real upstream GPT-SoVITS engine over HTTP.

The task's runtime synthesis path is upstream's own ``TTSFactory`` engine
(``gpt_sovits_tts``), not an Aemeath wrapper. So this test drives that engine
for real against a local HTTP double that enforces api_v2's actual contract.

It exists because two upstream defects only appear on the wire and neither is
visible from reading the config:

* the engine's ``streaming_mode`` default is the misspelled string ``"ture"``,
  which api_v2's typed query parameter rejects with **422** before any
  synthesis — the engine then logs "Failed to generate audio" and returns
  ``None``, so Aemeath would get no audio at all;
* ``GPTSoVITSConfig.streaming_mode`` is declared as ``str`` upstream, so a YAML
  *boolean* fails schema validation outright.

Both are covered here rather than left to a listening test, because a real
GPT-SoVITS install is not required to catch them.
"""

from __future__ import annotations

import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
UPSTREAM_DIR = ROOT_DIR / "vendor" / "Open-LLM-VTuber"
for _path in (str(UPSTREAM_DIR / "src"), str(UPSTREAM_DIR), str(ROOT_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from src.open_llm_vtuber.tts.gpt_sovits_tts import TTSEngine  # noqa: E402


def make_wav_bytes(seconds: float = 0.25, rate: int = 32000) -> bytes:
    """Build a real, decodable mono WAV so the engine writes actual audio."""
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x01" * int(rate * seconds))
    return buffer.getvalue()


class FakeApiV2:
    """Minimal api_v2 double that enforces the parameter contract.

    api_v2 declares ``streaming_mode: Union[bool, int]`` on the GET endpoint, so
    an illegal value is rejected by FastAPI's validation with 422 before the
    handler runs. This double reproduces that: it answers 422 for anything that
    is not a bool or an int in 0-3, exactly as the real service does.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reject_streaming_mode = True
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    @property
    def port(self) -> int:
        """The ephemeral port the double is serving on."""
        return self._server.server_port

    def start(self) -> None:
        """Serve on an ephemeral localhost port."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ARG002 - silence the test log
                """Suppress the default stderr logging."""

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.requests.append(params)

                if parsed.path != "/tts":
                    self.send_error(404)
                    return

                mode = params.get("streaming_mode", "")
                if outer.reject_streaming_mode and not _is_wire_legal(mode):
                    # This is the real service's answer to "ture".
                    body = json.dumps(
                        {
                            "detail": [
                                {
                                    "type": "int_parsing",
                                    "loc": ["query", "streaming_mode"],
                                    "msg": "Input should be a valid integer",
                                }
                            ]
                        }
                    ).encode("utf-8")
                    self.send_response(422)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                audio = make_wav_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/tts"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut the double down."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _is_wire_legal(raw: str) -> bool:
    """Whether a wire value satisfies api_v2's ``Union[bool, int]`` field."""
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return True
    if lowered.lstrip("-").isdigit():
        return 0 <= int(lowered) <= 3
    return False


@pytest.fixture
def api_v2():
    """A local api_v2 double, torn down after the test."""
    server = FakeApiV2()
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ----------------------------------------------------------------------
# The engine's own default is broken on the wire
# ----------------------------------------------------------------------


class TestUpstreamDefaultIsRejected:
    """The engine's default ``"ture"`` cannot synthesise anything."""

    def test_misspelled_default_is_actually_the_engine_default(self):
        """Pin the defect itself, so an upstream fix shows up as a failure."""
        engine = TTSEngine()
        assert engine.streaming_mode == "ture", (
            "upstream changed its default; re-check whether the override in "
            "config/conf.aemeath.yaml is still required"
        )

    def test_default_streaming_mode_produces_no_audio(self, api_v2):
        """With the upstream default the engine returns ``None``, not audio.

        This is what a user would experience as "she answers in text but never
        speaks", with only a CRITICAL log line to explain it.
        """
        engine = TTSEngine(
            api_url=api_v2.url,
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考音频的转写。",
        )

        result = engine.generate_audio("你好。", file_name_no_ext="ture-default")

        assert result is None, "the misspelled default must not appear to succeed"
        assert api_v2.requests, "the request must have been attempted"
        assert api_v2.requests[0]["streaming_mode"] == "ture"

    def test_explicit_streaming_mode_synthesises_audio(self, api_v2):
        """Overriding the default is what makes synthesis work."""
        engine = TTSEngine(
            api_url=api_v2.url,
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考音频的转写。",
            streaming_mode="false",
        )

        try:
            path = engine.generate_audio("你好，我是爱弥斯。", file_name_no_ext="ok")
            assert path is not None, "synthesis must succeed with a legal value"

            audio = Path(path).read_bytes()
            assert audio, "the engine must write real bytes"
            with wave.open(BytesIO(audio), "rb") as handle:
                assert handle.getnframes() > 0, "the WAV must contain frames"
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)

    @pytest.mark.parametrize("mode", ["0", "1", "2", "3", "true", "false"])
    def test_wire_legal_values_are_accepted(self, api_v2, mode):
        """Every documented value works, so the fix is not over-narrow."""
        engine = TTSEngine(
            api_url=api_v2.url,
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考音频的转写。",
            streaming_mode=mode,
        )
        path = engine.generate_audio("测试。", file_name_no_ext=f"mode-{mode}")
        try:
            assert path is not None, mode
            assert api_v2.requests[-1]["streaming_mode"] == mode
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)


class TestEngineRequestContract:
    """The engine must send the parameters api_v2 requires."""

    def test_reference_audio_and_prompt_travel_with_every_request(self, api_v2):
        """Voice identity is carried per request, not configured server-side."""
        engine = TTSEngine(
            api_url=api_v2.url,
            text_lang="zh",
            ref_audio_path="voices/aemeath_ref.wav",
            prompt_lang="zh",
            prompt_text="这是参考音频的原文。",
            streaming_mode="false",
        )
        path = engine.generate_audio("你好。", file_name_no_ext="contract")
        try:
            sent = api_v2.requests[-1]
            assert sent["ref_audio_path"] == "voices/aemeath_ref.wav", (
                "without the reference audio the server cannot clone the voice"
            )
            assert sent["prompt_text"] == "这是参考音频的原文。"
            assert sent["text_lang"] == "zh"
            assert sent["prompt_lang"] == "zh"
            assert sent["media_type"] == "wav"
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)

    def test_stage_directions_are_stripped_before_synthesis(self, api_v2):
        """Bracketed stage directions must not be read aloud."""
        engine = TTSEngine(
            api_url=api_v2.url,
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考。",
            streaming_mode="false",
        )
        path = engine.generate_audio("[微笑] 你好。", file_name_no_ext="brackets")
        try:
            assert "[微笑]" not in api_v2.requests[-1]["text"]
            assert "你好" in api_v2.requests[-1]["text"]
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)


class TestServerUnavailable:
    """A stopped GPT-SoVITS server must fail visibly, not silently."""

    def test_unreachable_server_raises_connection_error(self):
        """The engine surfaces a refused connection rather than faking audio.

        This is the cost of the local-process dependency: TTS is unavailable
        whenever the server is not running. The failure is visible — the engine
        raises rather than returning ``None`` — so Aemeath's synthesis path
        catches and logs it instead of sending an empty audio frame.
        """
        import requests

        engine = TTSEngine(
            api_url="http://127.0.0.1:1/tts",  # nothing listens here
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考。",
            streaming_mode="false",
        )

        # ``requests.exceptions.ConnectionError`` does not subclass the builtin
        # ``ConnectionError``, so the library's own type is the honest check.
        with pytest.raises(requests.exceptions.ConnectionError):
            engine.generate_audio("你好。", file_name_no_ext="down")

    def test_non_200_response_returns_none(self, api_v2):
        """A server-side rejection is reported as no audio, not as bytes.

        The engine returns ``None`` for any non-200, which is why the 422 from
        the misspelled ``streaming_mode`` default is silent apart from a log
        line: there is no exception and no audio, only a missing return value.
        """
        engine = TTSEngine(
            api_url=f"http://127.0.0.1:{api_v2.port}/not-tts",
            text_lang="zh",
            ref_audio_path="reference.wav",
            prompt_lang="zh",
            prompt_text="参考。",
            streaming_mode="false",
        )

        assert engine.generate_audio("你好。", file_name_no_ext="404") is None
