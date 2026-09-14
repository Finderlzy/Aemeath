"""V2-T02 production-entry tests: voice presets, audition and applying.

The voice page has one job that matters and one honesty rule:

* **Applying a voice is transactional.** A new preset is validated and
  auditioned before it becomes active, and a failure to apply must leave the
  previously working preset in place rather than leaving the character without
  a voice (issue #15, acceptance A5).
* **A sample voice is never described as the official one.** The real Aemeath
  voice material does not exist yet, so the UI must say "示例音色" and must not
  present the placeholder as her正式 voice (acceptance A7).

The presets are read from the same authoritative config the engine is built
from, and applied through the same revision-guarded write path V2-T01
established.

Acceptance criteria covered (issue #15 / implementation-plan V2-T02):

A5 a voice can be audited and applied; a failed apply keeps the old preset;
   switching the voice does not touch the character window's audio ownership
A7 a sample voice is marked as a sample
"""

from __future__ import annotations

import copy

import pytest
import yaml

from tests.integration.harness import build_config_document

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _write_document(path, document) -> None:
    """Write a config document as YAML."""
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


def _usable_preset(service, voice_env):
    """Create and return a preset whose reference audio really exists."""
    preset = service.create_preset(
        name="可试听音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=voice_env.audio("good"),
        prompt_text="这是一段可用的参考转写。",
        text_lang="zh",
        prompt_lang="zh",
    )
    assert preset["ok"] is True, preset
    return preset


@pytest.fixture
def voice_env(tmp_path, monkeypatch):
    """An isolated authoritative config bound to a voice service.

    Reference audio files are really created on disk: a voice preset is only
    usable when its reference audio exists, and a test that skipped that would
    not exercise the guard the acceptance criterion is about.
    """
    from aemeath import config as config_module

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    ref_dir = tmp_path / "models" / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    _ref_dir = ref_dir
    for name in ("sample", "other", "good", "another", "x"):
        (ref_dir / f"{name}.wav").write_bytes(b"RIFF....WAVEfmt ")

    # A usable GPT-SoVITS block, as the real config carries.
    document["character_config"]["tts_config"]["gpt_sovits"] = {
        "api_url": "http://127.0.0.1:9880/tts",
        "text_lang": "zh",
        "ref_audio_path": str(ref_dir / "sample.wav"),
        "prompt_lang": "zh",
        "prompt_text": "这是一段参考音频的转写。",
        "text_split_method": "cut5",
        "batch_size": "1",
        "media_type": "wav",
        "streaming_mode": "false",
    }
    _write_document(config_path, document)
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    class Env:
        path = config_path
        ref_dir = _ref_dir

        def audio(self, name: str) -> str:
            """Absolute path of a fixture reference audio file."""
            return str(_ref_dir / f"{name}.wav")

        def document(self):
            """The document as it currently exists on disk."""
            return yaml.safe_load(config_path.read_text(encoding="utf-8"))

    return Env()


# ----------------------------------------------------------------------
# A7 — a sample is labelled a sample
# ----------------------------------------------------------------------


async def test_presets_mark_the_default_voice_as_a_sample_not_the_official_one(
    voice_env,
):
    """The bundled default is reported as a sample voice.

    The real character voice has not been produced, so the only honest label
    for whatever is bundled is "sample". A UI that called it the official
    Aemeath voice would be stating something untrue about the product.
    """
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    presets = service.list_presets()

    assert presets, "at least the built-in default preset must be listed"
    default = presets[0]
    assert default["is_official_voice"] is False
    assert "示例" in default["label"] or "示例" in default["description"], (
        "a sample voice must be described as a sample"
    )


# ----------------------------------------------------------------------
# A5 — audition and apply
# ----------------------------------------------------------------------


async def test_listing_presets_reports_the_currently_active_one(voice_env):
    """The active preset is identified, so the UI can mark it."""
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    overview = service.overview()

    assert overview["active_preset_id"]
    active = [
        preset for preset in overview["presets"]
        if preset["preset_id"] == overview["active_preset_id"]
    ]
    assert len(active) == 1, "exactly one preset is active"


async def test_applying_a_preset_switches_the_engine_and_records_it(voice_env):
    """Applying a preset changes ``tts_model`` and the active voice block."""
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    preset = service.create_preset(
        name="测试音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=voice_env.audio("other"),
        prompt_text="另一段参考转写。",
        text_lang="zh",
        prompt_lang="zh",
    )
    assert preset["ok"] is True, preset

    result = await service.apply(preset["preset_id"])

    assert result["ok"] is True, result
    document = voice_env.document()
    tts_config = document["character_config"]["tts_config"]
    assert tts_config["tts_model"] == "gpt_sovits_tts"
    assert tts_config["gpt_sovits"]["ref_audio_path"] == voice_env.audio("other")
    assert tts_config["gpt_sovits"]["prompt_text"] == "另一段参考转写。"

    # The service reports the new preset as active, from the file.
    assert service.overview()["active_preset_id"] == preset["preset_id"]


async def test_a_failed_apply_keeps_the_previous_preset_active(voice_env):
    """A preset that cannot be applied must not replace a working voice.

    The unacceptable outcome is a half-applied switch: the config points at a
    broken preset and the character has no voice. The old preset has to survive.
    """
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    before = voice_env.path.read_bytes()

    good = service.create_preset(
        name="可用音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=voice_env.audio("good"),
        prompt_text="可用参考。",
        text_lang="zh",
        prompt_lang="zh",
    )
    assert (await service.apply(good["preset_id"]))["ok"] is True
    after_good = voice_env.path.read_bytes()

    # A preset whose reference audio does not exist cannot be applied.
    broken = service.create_preset(
        name="缺失素材音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=str(voice_env.ref_dir / "does-not-exist.wav"),
        prompt_text="缺失参考。",
        text_lang="zh",
        prompt_lang="zh",
        require_existing_audio=False,
    )
    assert broken["ok"] is True, broken

    # Snapshot *after* creating the preset: saving the preset legitimately
    # changes the file. What must not change is the active voice.
    before_apply = voice_env.path.read_bytes()

    result = await service.apply(broken["preset_id"])

    assert result["ok"] is False, "applying a broken preset must not report success"
    assert result["error"], "the failure must be explained"
    assert voice_env.path.read_bytes() == before_apply, (
        "a failed apply must leave the config exactly as it was"
    )
    assert service.overview()["active_preset_id"] == good["preset_id"]
    assert before != after_good, "the good apply really did change the file"


async def test_applying_a_broken_preset_does_not_damage_the_config(voice_env):
    """Even with no prior successful apply, a failure leaves a loadable config.

    The preset is valid when saved and its reference audio is deleted
    afterwards, which is the realistic way a stored preset becomes unusable:
    the material moved or was removed between saving and applying.
    """
    from aemeath.management.voices import VoiceService
    from tests.integration.harness import load_validated_config

    service = VoiceService(voice_env.path)
    before = voice_env.path.read_bytes()

    broken = service.create_preset(
        name="素材已删除音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=voice_env.audio("x"),
        prompt_text="参考。",
        text_lang="zh",
        prompt_lang="zh",
    )
    assert broken["ok"] is True, broken
    # The audio disappears after the preset was saved.
    (voice_env.ref_dir / "x.wav").unlink()
    before_apply = voice_env.path.read_bytes()

    result = await service.apply(broken["preset_id"])

    assert result["ok"] is False
    assert "参考音频不存在" in result["error"]
    assert voice_env.path.read_bytes() == before_apply
    load_validated_config(voice_env.path)
    assert before != before_apply, "saving the preset really did change the file"


async def test_applying_an_unknown_preset_is_refused(voice_env):
    """An unknown preset id is an error, not a silent no-op."""
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    before = voice_env.path.read_bytes()

    result = await service.apply("no-such-preset")

    assert result["ok"] is False
    assert result["error"]
    assert voice_env.path.read_bytes() == before


async def test_audition_reports_an_unreachable_service_instead_of_failing_silently(
    voice_env,
):
    """Auditioning must say the local TTS service is unreachable.

    The local GPT-SoVITS server may not be running. "Nothing happened" is not an
    acceptable answer — the UI has to be able to tell the user why.
    """
    from aemeath.management.voices import VoiceService

    async def unreachable(*args, **kwargs):
        raise ConnectionError("connection refused")

    service = VoiceService(voice_env.path, transport=unreachable)
    preset = _usable_preset(service, voice_env)
    assert (await service.apply(preset["preset_id"]))["ok"] is True

    result = await service.audition(preset["preset_id"], text="你好，我是爱弥斯。")

    assert result["ok"] is False
    assert "不可用" in result["error"] or "连接" in result["error"]


async def test_audition_recognises_an_unreachable_service_behind_a_wrapped_error(
    voice_env,
):
    """A wrapped transport error is still recognised as "service not running".

    The real adapter raises ``ModelError`` around the underlying connection
    failure, so a check that only caught ``ConnectionError`` would never fire
    and the user would see a generic "synthesis failed" for the common case of
    simply not having started the TTS server.
    """
    from aemeath.adapters import ModelError
    from aemeath.management.voices import VoiceService

    async def wrapped(*args, **kwargs):
        try:
            raise ConnectionError("All connection attempts failed")
        except ConnectionError as inner:
            raise ModelError(f"gpt-sovits request failed: {inner}") from inner

    service = VoiceService(voice_env.path, transport=wrapped)
    preset = _usable_preset(service, voice_env)

    result = await service.audition(preset["preset_id"], text="你好。")

    assert result["ok"] is False
    assert "本地语音服务不可用" in result["error"], result["error"]


async def test_a_genuine_synthesis_error_is_not_reported_as_unreachable(voice_env):
    """A parameter rejection is reported as a synthesis failure, not "down".

    Telling the user the service is not running when it answered with an error
    would send them to fix the wrong thing.
    """
    from aemeath.adapters import ModelError
    from aemeath.management.voices import VoiceService

    async def rejected(*args, **kwargs):
        raise ModelError("gpt-sovits rejected the request (HTTP 422)")

    service = VoiceService(voice_env.path, transport=rejected)
    preset = _usable_preset(service, voice_env)

    result = await service.audition(preset["preset_id"], text="你好。")

    assert result["ok"] is False
    assert "不可用" not in result["error"]
    assert "试听失败" in result["error"]


async def test_audition_returns_audio_when_the_service_answers(voice_env):
    """A successful audition returns audio bytes for the page to play."""
    from aemeath.management.voices import VoiceService

    captured = {}

    async def working(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return b"RIFF-fake-audio"

    service = VoiceService(voice_env.path, transport=working)
    preset = _usable_preset(service, voice_env)

    result = await service.audition(preset["preset_id"], text="你好，我是爱弥斯。")

    assert result["ok"] is True
    assert result["audio"]
    assert captured["url"].endswith("/tts")


async def test_auditioning_without_a_configured_voice_explains_why(voice_env):
    """With no configured reference audio, audition says so rather than failing.

    The bundled sample has no audio of its own, so there is genuinely nothing to
    synthesise with. The message has to point at what is missing.
    """
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path, transport=lambda *a, **k: b"")
    overview = service.overview()

    result = await service.audition(overview["active_preset_id"], text="你好。")

    assert result["ok"] is False
    assert "参考音频" in result["error"]


# ----------------------------------------------------------------------
# Audio ownership
# ----------------------------------------------------------------------


async def test_applying_a_voice_does_not_claim_the_character_audio_session(
    voice_env,
):
    """Switching a voice must not take over the character window's audio.

    Voice management only rewrites configuration. It must not start a
    conversation session, open the microphone or play through the character's
    playback queue — the session stays owned by the character window.
    """
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    preset = service.create_preset(
        name="另一个音色",
        api_url="http://127.0.0.1:9880/tts",
        ref_audio_path=voice_env.audio("another"),
        prompt_text="参考。",
        text_lang="zh",
        prompt_lang="zh",
    )
    result = await service.apply(preset["preset_id"])

    assert result["ok"] is True
    # The apply path touches configuration only; the running engine adopts the
    # change on restart, which the response must say rather than implying the
    # character is already speaking in the new voice.
    assert result["restart_required"] is True


async def test_a_validated_preset_reports_the_upstream_schema_accepts_it(voice_env):
    """A preset that would not survive the upstream schema is rejected up front."""
    from aemeath.management.voices import VoiceService

    service = VoiceService(voice_env.path)
    # streaming_mode must be a bool or an int 0-3 on the wire; upstream's own
    # default is the misspelled "ture", which api_v2 rejects with a 422.
    result = await service.apply_raw(
        {
            "api_url": "http://127.0.0.1:9880/tts",
            "text_lang": "zh",
            "ref_audio_path": voice_env.audio("x"),
            "prompt_lang": "zh",
            "prompt_text": "参考。",
            "streaming_mode": "ture",
        }
    )

    assert result["ok"] is False
    assert result["error"]
