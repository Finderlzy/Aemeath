"""Voice presets, auditioning and applying.

The voice page manages one thing: which GPT-SoVITS model and reference audio
the local speech engine should speak with. Three rules shape the code.

**A preset is the whole voice, not one field.** A voice is defined by its GPT
and SoVITS weights, the model version, the reference audio, the reference
transcript and language, and the synthesis parameters. Applying half of that
leaves the character with a voice that does not match the transcript, which
sounds worse than the voice it replaced — so a preset carries all of it and is
applied as a unit.

**Applying is transactional.** The candidate config is validated with the real
upstream schema *and* the adapter is constructed to confirm the parameters are
wire-legal before anything is written. If either step fails, the previous
preset stays active and the file is untouched. The unacceptable outcome is a
half-applied switch that leaves the character silent.

**A sample is called a sample.** The real Aemeath voice material has not been
produced yet, so the bundled default is labelled 示例音色. Nothing here claims
it is her official voice, because that would be untrue.

Auditioning reuses the same adapter the runtime engine uses, so what the user
hears in the page is what the character will say.
"""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from loguru import logger

from ..adapters import GPTSoVITSAdapter
from ..config import resolve_config_path

#: Preset ids are stored in the config, so they must be plain identifiers.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Where presets live inside the Aemeath block. Kept out of the upstream
#: ``tts_config`` because upstream owns that schema and would drop unknown keys.
PRESETS_KEY = "voice_presets"

#: The built-in preset. It is a sample: the official voice does not exist yet.
DEFAULT_PRESET_ID = "default-sample"


class VoiceService:
    """Read, audition and apply voice presets."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        transport: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Bind the service to a config file.

        Args:
            config_path: Explicit config path; defaults to the authoritative one.
            transport: Injection point for audition requests. Production builds
                an HTTP client through the adapter; tests supply a double so no
                real service is needed.
        """
        self._explicit_path = config_path
        self._transport = transport

    @property
    def config_path(self) -> Path:
        """The authoritative config file."""
        return resolve_config_path(self._explicit_path)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_text(self) -> str:
        """Raw config text, empty when the file is absent."""
        path = self.config_path
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def read_document(self) -> Dict[str, Any]:
        """The parsed config document, without ``${VAR}`` expansion."""
        text = self.read_text()
        if not text:
            return {}
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            logger.error("Config is not valid YAML: {}", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _aemeath_config(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """The Aemeath block, or an empty dict."""
        character = document.get("character_config") or {}
        block = character.get("aemeath_config")
        return block if isinstance(block, dict) else {}

    def _tts_config(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """The upstream ``tts_config`` block, or an empty dict."""
        character = document.get("character_config") or {}
        block = character.get("tts_config")
        return block if isinstance(block, dict) else {}

    def _stored_presets(self, document: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Presets saved in the config."""
        raw = self._aemeath_config(document).get(PRESETS_KEY)
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _demo_preset(self) -> Dict[str, Any]:
        """The bundled sample preset.

        Marked ``is_official_voice: False`` and described as a sample: it is a
        placeholder for a voice that has not been recorded, and the UI must not
        present it as Aemeath's real voice.
        """
        return {
            "preset_id": DEFAULT_PRESET_ID,
            "name": "默认示例音色",
            "label": "默认示例音色",
            "description": (
                "随应用提供的示例音色，用于验证语音链路。"
                "它不是爱弥斯的正式音色——正式音色素材尚未提供。"
            ),
            "is_official_voice": False,
            "builtin": True,
            "api_url": "http://127.0.0.1:9880/tts",
            "ref_audio_path": "",
            "prompt_text": "",
            "text_lang": "zh",
            "prompt_lang": "zh",
            "text_split_method": "cut5",
            "batch_size": "1",
            "media_type": "wav",
            "streaming_mode": "false",
        }

    def list_presets(self) -> List[Dict[str, Any]]:
        """Every preset, the sample first.

        A preset saved in the config for the *active* voice block is surfaced
        too, so a voice configured by hand outside the page is not invisible.
        """
        document = self.read_document()
        presets: List[Dict[str, Any]] = [self._demo_preset()]

        stored = self._stored_presets(document)
        stored_ids = {item.get("preset_id") for item in stored}
        for item in stored:
            presets.append({**self._demo_preset(), **item})

        current = self._current_voice(document)
        if current and current.get("preset_id") not in stored_ids | {DEFAULT_PRESET_ID}:
            presets.append(current)

        return presets

    def _current_voice(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """The voice the config currently selects, as a preset-shaped dict.

        Read from the real ``gpt_sovits`` block so the page reflects what the
        engine will actually use rather than what was last saved by the page.
        """
        tts_config = self._tts_config(document)
        if str(tts_config.get("tts_model") or "") != "gpt_sovits_tts":
            return {}
        block = tts_config.get("gpt_sovits")
        if not isinstance(block, dict):
            return {}

        api_url = str(block.get("api_url") or "")
        ref_audio = str(block.get("ref_audio_path") or "")
        # A hand-configured voice gets a stable id derived from its content, so
        # the page can mark it active without inventing a stored record.
        fingerprint = f"{api_url}|{ref_audio}|{block.get('prompt_text') or ''}"
        return {
            **self._demo_preset(),
            "preset_id": self._match_stored_id(document, block) or f"current-{abs(hash(fingerprint)) % 10**8}",
            "name": "当前使用中的音色",
            "label": "当前使用中的音色",
            "description": "当前配置中正在使用的音色（未通过本页保存为预设）。",
            "builtin": False,
            "api_url": api_url,
            "ref_audio_path": ref_audio,
            "prompt_text": str(block.get("prompt_text") or ""),
            "text_lang": str(block.get("text_lang") or "zh"),
            "prompt_lang": str(block.get("prompt_lang") or "zh"),
            "text_split_method": str(block.get("text_split_method") or "cut5"),
            "batch_size": str(block.get("batch_size") or "1"),
            "media_type": str(block.get("media_type") or "wav"),
            "streaming_mode": str(block.get("streaming_mode", "false")),
        }

    def _match_stored_id(
        self, document: Dict[str, Any], block: Dict[str, Any]
    ) -> str:
        """The stored preset id whose parameters equal the active block."""
        for item in self._stored_presets(document):
            if (
                item.get("api_url") == block.get("api_url")
                and item.get("ref_audio_path") == block.get("ref_audio_path")
                and item.get("prompt_text") == block.get("prompt_text")
            ):
                return str(item.get("preset_id") or "")
        return ""

    def active_preset_id(self) -> str:
        """Id of the preset the config currently uses."""
        document = self.read_document()
        tts_config = self._tts_config(document)
        if str(tts_config.get("tts_model") or "") == "gpt_sovits_tts":
            return self._current_voice(document).get("preset_id", "") or ""
        return DEFAULT_PRESET_ID

    def overview(self) -> Dict[str, Any]:
        """Everything the voice page needs in one round trip."""
        document = self.read_document()
        tts_config = self._tts_config(document)
        return {
            "presets": self.list_presets(),
            "active_preset_id": self.active_preset_id(),
            "engine": str(tts_config.get("tts_model") or ""),
            "engine_is_gpt_sovits": str(tts_config.get("tts_model") or "")
            == "gpt_sovits_tts",
            "read_error": "" if document else "配置文件为空或无法解析。",
        }

    # ------------------------------------------------------------------
    # Preset creation
    # ------------------------------------------------------------------

    def create_preset(
        self,
        *,
        name: str,
        api_url: str,
        ref_audio_path: str,
        prompt_text: str,
        text_lang: str = "zh",
        prompt_lang: str = "zh",
        text_split_method: str = "cut5",
        batch_size: str = "1",
        media_type: str = "wav",
        streaming_mode: str = "false",
        require_existing_audio: bool = True,
        expected_revision: str = "",
    ) -> Dict[str, Any]:
        """Save a new preset without activating it.

        Args:
            name: Display name.
            api_url: GPT-SoVITS api_v2 endpoint.
            ref_audio_path: Reference audio; defines the voice.
            prompt_text: Transcript of the reference audio.
            text_lang: Language of the text to synthesise.
            prompt_lang: Language of ``prompt_text``.
            text_split_method: api_v2 splitting strategy.
            batch_size: api_v2 batch size.
            media_type: Response container.
            streaming_mode: Bool or int 0-3, as a string.
            require_existing_audio: Whether the reference audio must exist on
                disk. Left on for the UI; a test that specifically needs an
                unusable preset turns it off and exercises the apply guard.
            expected_revision: Revision the client read, for conflict detection.

        Returns:
            ``{ok, preset_id, error, conflict}``.
        """
        if not (name or "").strip():
            return self._failure("预设名称不能为空。")

        params = self._parameters(
            api_url=api_url,
            ref_audio_path=ref_audio_path,
            prompt_text=prompt_text,
            text_lang=text_lang,
            prompt_lang=prompt_lang,
            text_split_method=text_split_method,
            batch_size=batch_size,
            media_type=media_type,
            streaming_mode=streaming_mode,
        )
        errors = self._validate_parameters(params, require_existing_audio=require_existing_audio)
        if errors:
            return self._failure("；".join(errors))

        document = self.read_document()
        if not document:
            return self._failure("现有配置无法解析，拒绝覆盖以免损坏。")

        if expected_revision and expected_revision != self._revision():
            return {
                "ok": False,
                "preset_id": "",
                "error": "配置已被其他操作修改，请刷新后重试。",
                "conflict": True,
            }

        candidate = copy.deepcopy(document)
        block = self._aemeath_block_for_write(candidate)
        if block is None:
            return self._failure("配置缺少 character_config.aemeath_config。")

        preset_id = f"preset-{uuid.uuid4().hex[:8]}"
        record = {"preset_id": preset_id, "name": name.strip(), **params}
        existing = self._stored_presets(candidate)
        existing.append(record)
        block[PRESETS_KEY] = existing

        ok, error = self._write(candidate)
        if not ok:
            return self._failure(error)
        logger.info("Created voice preset {}.", preset_id)
        return {"ok": True, "preset_id": preset_id, "error": "", "conflict": False}

    @staticmethod
    def _parameters(
        *,
        api_url: str,
        ref_audio_path: str,
        prompt_text: str,
        text_lang: str,
        prompt_lang: str,
        text_split_method: str,
        batch_size: str,
        media_type: str,
        streaming_mode: str,
    ) -> Dict[str, Any]:
        """Collect the parameters that together define a voice."""
        return {
            "api_url": (api_url or "").strip(),
            "ref_audio_path": (ref_audio_path or "").strip(),
            "prompt_text": (prompt_text or "").strip(),
            "text_lang": (text_lang or "zh").strip(),
            "prompt_lang": (prompt_lang or "zh").strip(),
            "text_split_method": (text_split_method or "cut5").strip(),
            "batch_size": str(batch_size or "1"),
            "media_type": (media_type or "wav").strip(),
            "streaming_mode": str(streaming_mode if streaming_mode is not None else "false"),
        }

    @staticmethod
    def _validate_parameters(
        params: Dict[str, Any], *, require_existing_audio: bool
    ) -> List[str]:
        """Check a voice's parameters before they are used.

        The reference transcript is checked as strictly as the audio: a
        mismatched transcript makes the cloned voice drift noticeably, so an
        empty one is a configuration error rather than a harmless default.

        Returns:
            Human-readable problems; empty when the parameters are usable.
        """
        errors: List[str] = []

        api_url = params["api_url"]
        if not api_url:
            errors.append("服务地址不能为空。")
        elif not api_url.startswith(("http://", "https://")):
            errors.append("服务地址必须以 http:// 或 https:// 开头。")

        if not params["ref_audio_path"]:
            errors.append("参考音频不能为空，它决定音色。")
        elif require_existing_audio:
            path = Path(params["ref_audio_path"]).expanduser()
            if not path.is_file():
                errors.append(f"参考音频不存在：{params['ref_audio_path']}")

        if not params["prompt_text"]:
            errors.append("参考文字不能为空，它必须是参考音频的真实转写。")

        # Constructing the adapter is how wire-legal parameters are confirmed;
        # upstream's own default here ("ture") is exactly the value api_v2
        # rejects with a 422.
        try:
            GPTSoVITSAdapter(**params)
        except ValueError as exc:
            errors.append(str(exc))

        return errors

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------

    async def apply(
        self, preset_id: str, *, expected_revision: str = ""
    ) -> Dict[str, Any]:
        """Make a preset the active voice.

        The candidate is validated and the adapter constructed before anything
        is written, so a preset that cannot work never becomes the active one.

        Args:
            preset_id: Preset to activate.
            expected_revision: Revision the client read.

        Returns:
            ``{ok, error, restart_required, conflict}``.
        """
        document = self.read_document()
        if not document:
            return self._failure("现有配置无法解析，拒绝覆盖以免损坏。")

        if expected_revision and expected_revision != self._revision():
            return {
                "ok": False,
                "error": "配置已被其他操作修改，请刷新后重试。",
                "restart_required": False,
                "conflict": True,
            }

        preset = self._find_preset(document, preset_id)
        if preset is None:
            return self._failure("找不到这个音色预设。")

        params = self._parameters_from_preset(preset)
        errors = self._validate_parameters(params, require_existing_audio=True)
        if errors:
            # The previous preset stays active and the file is untouched.
            logger.warning(
                "Refusing to apply voice preset {}: {}", preset_id, "; ".join(errors)
            )
            return self._failure("音色不可用，已保留原来使用中的音色：" + "；".join(errors))

        return await self.apply_raw(
            params,
            preset_id=preset_id,
            document=document,
            expected_revision=expected_revision,
        )

    async def apply_raw(
        self,
        params: Dict[str, Any],
        *,
        preset_id: str = "",
        document: Optional[Dict[str, Any]] = None,
        expected_revision: str = "",
    ) -> Dict[str, Any]:
        """Validate and apply a raw parameter set.

        Used by :meth:`apply` and exposed so a caller can apply parameters that
        were not stored as a named preset (for example a hand-edited block).
        """
        document = document or self.read_document()
        if not document:
            return self._failure("现有配置无法解析，拒绝覆盖以免损坏。")

        collected = self._parameters(
            api_url=params.get("api_url", ""),
            ref_audio_path=params.get("ref_audio_path", ""),
            prompt_text=params.get("prompt_text", ""),
            text_lang=params.get("text_lang", "zh"),
            prompt_lang=params.get("prompt_lang", "zh"),
            text_split_method=params.get("text_split_method", "cut5"),
            batch_size=params.get("batch_size", "1"),
            media_type=params.get("media_type", "wav"),
            streaming_mode=params.get("streaming_mode", "false"),
        )
        errors = self._validate_parameters(collected, require_existing_audio=True)
        if errors:
            return self._failure("音色不可用，已保留原来使用中的音色：" + "；".join(errors))

        if expected_revision and expected_revision != self._revision():
            return {
                "ok": False,
                "error": "配置已被其他操作修改，请刷新后重试。",
                "restart_required": False,
                "conflict": True,
            }

        candidate = copy.deepcopy(document)
        tts_config = self._tts_config_for_write(candidate)
        if tts_config is None:
            return self._failure("配置缺少 character_config.tts_config。")

        # The whole voice is switched together: engine plus every parameter
        # that defines the sound. Writing only some of them would leave the
        # character speaking with a transcript that does not match the audio.
        tts_config["tts_model"] = "gpt_sovits_tts"
        tts_config["gpt_sovits"] = {
            "api_url": collected["api_url"],
            "text_lang": collected["text_lang"],
            "ref_audio_path": collected["ref_audio_path"],
            "prompt_lang": collected["prompt_lang"],
            "prompt_text": collected["prompt_text"],
            "text_split_method": collected["text_split_method"],
            "batch_size": collected["batch_size"],
            "media_type": collected["media_type"],
            "streaming_mode": collected["streaming_mode"],
        }

        errors = self._validate_document(candidate)
        if errors:
            return self._failure("；".join(errors))

        ok, error = self._write(candidate)
        if not ok:
            return self._failure(error)

        logger.info("Applied voice preset {}.", preset_id or "(raw)")
        return {
            "ok": True,
            "preset_id": preset_id,
            "error": "",
            "conflict": False,
            # The engine reads its configuration at startup; the running process
            # keeps speaking with the previous voice until it restarts.
            "restart_required": True,
        }

    def _find_preset(
        self, document: Dict[str, Any], preset_id: str
    ) -> Optional[Dict[str, Any]]:
        """Locate a preset by id, including the built-in sample."""
        if preset_id == DEFAULT_PRESET_ID:
            # The built-in preset is a placeholder with no audio of its own; it
            # is not something that can be applied as a character voice.
            return None
        for item in self._stored_presets(document):
            if item.get("preset_id") == preset_id:
                return item
        return None

    def _parameters_from_preset(self, preset: Dict[str, Any]) -> Dict[str, Any]:
        """The voice parameters carried by a preset."""
        return self._parameters(
            api_url=str(preset.get("api_url") or ""),
            ref_audio_path=str(preset.get("ref_audio_path") or ""),
            prompt_text=str(preset.get("prompt_text") or ""),
            text_lang=str(preset.get("text_lang") or "zh"),
            prompt_lang=str(preset.get("prompt_lang") or "zh"),
            text_split_method=str(preset.get("text_split_method") or "cut5"),
            batch_size=str(preset.get("batch_size") or "1"),
            media_type=str(preset.get("media_type") or "wav"),
            streaming_mode=str(preset.get("streaming_mode", "false")),
        )

    # ------------------------------------------------------------------
    # Audition
    # ------------------------------------------------------------------

    async def audition(
        self, preset_id: str, *, text: str, timeout: float = 120.0
    ) -> Dict[str, Any]:
        """Synthesise a short sample with a preset, without applying it.

        Auditioning goes through the same adapter the engine uses, so the user
        hears what the character would actually sound like rather than an
        approximation produced by a second code path.

        Args:
            preset_id: Preset to speak with.
            text: Text to synthesise.
            timeout: Request timeout.

        Returns:
            ``{ok, audio, error, media_type}``. ``audio`` is base64 text for
            transport in JSON, or empty on failure.
        """
        import base64

        if not (text or "").strip():
            return {"ok": False, "audio": "", "error": "试听文字不能为空。", "media_type": ""}

        document = self.read_document()
        preset = self._find_preset(document, preset_id)
        if preset is None:
            # Fall back to the voice the config actually selects, so the page can
            # always audition whatever is in effect. The built-in sample preset
            # has no audio of its own and is deliberately not applicable, so
            # auditioning it must resolve to the configured voice instead of
            # failing on an empty reference path.
            active = self._current_voice(document)
            if not active or not active.get("ref_audio_path"):
                return {
                    "ok": False,
                    "audio": "",
                    "error": (
                        "当前没有可试听的音色：还没有配置参考音频。"
                        "请先导入参考音频并保存为预设，或切换到已配置的音色。"
                    ),
                    "media_type": "",
                }
            preset = active

        params = self._parameters_from_preset(preset)
        try:
            audio, media_type = await self._synthesise(params, text, timeout)
        except Exception as exc:
            # The local GPT-SoVITS server may simply not be running, which is
            # the common case. "Nothing happened" is not an acceptable answer,
            # so an unreachable service is named explicitly with what to do
            # about it; anything else is an ordinary synthesis failure.
            if self._looks_unreachable(exc):
                logger.warning(
                    "Voice audition could not reach the TTS service: {}", exc
                )
                return {
                    "ok": False,
                    "audio": "",
                    "error": (
                        f"本地语音服务不可用（{params['api_url']}）：{exc}。"
                        "请先启动 GPT-SoVITS 服务再试听。"
                    ),
                    "media_type": "",
                }
            logger.error("Voice audition failed: {}", exc)
            return {
                "ok": False,
                "audio": "",
                "error": f"试听失败：{exc}",
                "media_type": "",
            }

        return {
            "ok": True,
            "audio": base64.b64encode(audio).decode("ascii"),
            "error": "",
            "media_type": media_type,
        }

    async def _synthesise(
        self, params: Dict[str, Any], text: str, timeout: float
    ) -> tuple[bytes, str]:
        """Synthesise through the adapter, or the injected transport."""
        if self._transport is not None:
            payload = await self._transport(
                params["api_url"], {**params, "text": text}, timeout
            )
            return payload, params["media_type"]

        adapter = GPTSoVITSAdapter(**params)
        audio = await adapter.synthesize(text)
        return audio, params["media_type"]

    @staticmethod
    def _looks_unreachable(exc: BaseException) -> bool:
        """Whether a synthesis failure means "the service is not there".

        The adapter wraps transport errors in ``ModelError``, so catching
        ``ConnectionError`` directly would never fire and "the service is not
        running" would be reported as a generic synthesis failure. The chain is
        therefore walked and the message inspected.
        """
        markers = (
            "connection",
            "connect",
            "refused",
            "unreachable",
            "timed out",
            "timeout",
        )
        seen: set[int] = set()
        current: Optional[BaseException] = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (ConnectionError, TimeoutError)):
                return True
            lowered = str(current).lower()
            if any(marker in lowered for marker in markers):
                return True
            current = current.__cause__ or current.__context__
        return False

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    @staticmethod
    def _aemeath_block_for_write(document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The ``aemeath_config`` block to write presets into."""
        character = document.get("character_config")
        if not isinstance(character, dict):
            return None
        block = character.setdefault("aemeath_config", {})
        return block if isinstance(block, dict) else None

    @staticmethod
    def _tts_config_for_write(document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The ``tts_config`` block to write the active voice into."""
        character = document.get("character_config")
        if not isinstance(character, dict):
            return None
        block = character.get("tts_config")
        return block if isinstance(block, dict) else None

    def _validate_document(self, document: Dict[str, Any]) -> List[str]:
        """Validate a candidate document with the real upstream schema.

        The upstream validator would otherwise refuse to start the server on a
        bad config; catching it here means "applied a voice that prevents
        startup" cannot happen.
        """
        import os
        import tempfile

        from src.open_llm_vtuber.config_manager import read_yaml, validate_config

        text = yaml.safe_dump(document, allow_unicode=True)
        # Keep ${VAR} quoted: upstream substitutes into the text before parsing,
        # so a bare placeholder whose value is all digits becomes an int and
        # fails a schema that requires str (the defect V2-T01 fixed).
        text = re.sub(
            r"^(\s*[\w-]+:\s*)(\$\{\w+\})\s*$", r"\1'\2'", text, flags=re.MULTILINE
        )

        handle, name = tempfile.mkstemp(suffix=".yaml")
        os.close(handle)
        candidate = Path(name)
        try:
            candidate.write_text(text, encoding="utf-8")
            try:
                validate_config(read_yaml(str(candidate)))
            except Exception as exc:
                return [f"配置未通过上游校验：{exc}"]
            return []
        finally:
            try:
                candidate.unlink()
            except OSError:  # pragma: no cover
                pass

    def _write(self, document: Dict[str, Any]) -> tuple[bool, str]:
        """Write the config atomically; a failure leaves the file untouched."""
        import os
        import tempfile

        text = yaml.safe_dump(document, allow_unicode=True)
        text = re.sub(
            r"^(\s*[\w-]+:\s*)(\$\{\w+\})\s*$", r"\1'\2'", text, flags=re.MULTILINE
        )
        path = self.config_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            except BaseException:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.error("Voice config write failed; previous config intact: {}", exc)
            return False, f"保存失败，已保留原配置：{exc}"
        return True, ""

    def _revision(self) -> str:
        """Content revision of the config file."""
        import hashlib

        return hashlib.sha256(self.read_text().encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _failure(message: str) -> Dict[str, Any]:
        """A failed result with the fields callers expect."""
        return {
            "ok": False,
            "preset_id": "",
            "error": message,
            "conflict": False,
            "restart_required": False,
        }


__all__ = [
    "VoiceService",
    "DEFAULT_PRESET_ID",
    "PRESETS_KEY",
    "_ID_PATTERN",
]
