"""Live2D model configuration and preview.

There is no official Aemeath Live2D model yet (V2-08), so this page has two
jobs: let the user pick and tune whatever models *are* installed, and be honest
about the fact that the installed one is a placeholder.

Three properties the page depends on:

**A missing model is a state, not a blank page.** "There is no model here" and
"the page failed to load" look the same if the only signal is an empty list.
The overview therefore always returns the configured model name, the models it
could find, and — when something is wrong — an explanation saying what is
missing. The user can still edit the configuration in every one of those cases.

**The settings are written where the renderer reads them.** Scale and position
live in ``model_dict.json``, which the client fetches; the selected model lives
in ``character_config.live2d_model_name``, which the runtime reads. Writing a
second copy would let the page and the character window disagree.

**A save either happens or does not.** The dictionary is written to a temporary
file and moved into place, so a failure leaves the previous one intact — the
same rule V2-T01 established for the config.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger

from ..config import resolve_config_path

#: The bundled model ships with upstream and is not Aemeath's real asset.
PLACEHOLDER_MODEL_NAMES = {"mao_pro", "shizuku"}

#: Filename of the Live2D model dictionary, relative to the upstream root.
MODEL_DICT_FILENAME = "model_dict.json"


class Live2DService:
    """Read and write Live2D model selection and placement."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        models_root: Optional[Path] = None,
        model_dict_path: Optional[Path] = None,
    ) -> None:
        """Bind the service to the config and the model locations.

        Args:
            config_path: Explicit config path; defaults to the authoritative one.
            models_root: Directory holding Live2D models. Defaults to the
                upstream ``live2d-models`` directory, which is where the client
                loads them from.
            model_dict_path: The ``model_dict.json`` to read and write.
        """
        self._explicit_path = config_path
        self._models_root = models_root
        self._model_dict_path = model_dict_path

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        """The authoritative config file."""
        return resolve_config_path(self._explicit_path)

    @property
    def models_root(self) -> Path:
        """Directory containing Live2D models."""
        if self._models_root is not None:
            return Path(self._models_root)
        from ..config import ROOT_DIR

        return ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "live2d-models"

    @property
    def model_dict_path(self) -> Path:
        """The model dictionary the client reads."""
        if self._model_dict_path is not None:
            return Path(self._model_dict_path)
        from ..config import ROOT_DIR

        return ROOT_DIR / "vendor" / "Open-LLM-VTuber" / MODEL_DICT_FILENAME

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

    def configured_model(self) -> str:
        """The model name the config selects."""
        character = self.read_document().get("character_config") or {}
        return str(character.get("live2d_model_name") or "")

    def read_model_dict(self) -> List[Dict[str, Any]]:
        """The model dictionary, or an empty list when unreadable."""
        path = self.model_dict_path
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("model_dict.json is unreadable: {}", exc)
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _installed_models(self) -> List[str]:
        """Model names that actually have assets on disk.

        A directory counts only when it holds a ``*.model3.json``: an empty
        directory is not an installed model, and reporting it as one would make
        the page offer something that cannot load.
        """
        root = self.models_root
        if not root.is_dir():
            return []
        names = []
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir():
                continue
            if any(candidate.rglob("*.model3.json")):
                names.append(candidate.name)
        return names

    @staticmethod
    def _describe(name: str, entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """One model as the page needs it, with an honest label."""
        is_placeholder = name in PLACEHOLDER_MODEL_NAMES
        return {
            "name": name,
            "label": f"{name}（示例模型）" if is_placeholder else name,
            "description": (
                "随应用提供的示例模型，用于验证 Live2D 链路；"
                "它不是爱弥斯的正式模型。"
                if is_placeholder
                else ""
            ),
            "is_official_model": False,
            "is_placeholder": is_placeholder,
            "url": str((entry or {}).get("url") or ""),
            "scale": (entry or {}).get("kScale", 1.0),
            "x_offset": (entry or {}).get("kXOffset", 0),
            "y_offset": (entry or {}).get("initialYshift", 0),
            "x_shift": (entry or {}).get("initialXshift", 0),
        }

    def overview(self) -> Dict[str, Any]:
        """Everything the Live2D page needs, including what is missing.

        Never returns a silently empty page: when the model root is absent,
        empty, or the configured model has no assets, ``error`` explains it
        while the editable fields stay available.
        """
        configured = self.configured_model()
        entries = {str(e.get("name")): e for e in self.read_model_dict() if e.get("name")}
        installed = self._installed_models()

        # Only models with assets on disk are offered. The dictionary is a
        # placement record, not proof that a model exists: listing a
        # dictionary-only entry would present something the user cannot
        # actually load. The configured model is described separately below so
        # a stale selection is still visible (and explained) rather than hidden.
        models = [self._describe(name, entries.get(name)) for name in installed]

        error = ""
        root = self.models_root
        if not root.is_dir():
            error = (
                f"未找到 Live2D 模型目录（{root}）。"
                "角色会以无模型状态运行；聊天、语音与字幕不受影响。"
                "把模型放进该目录后刷新本页即可选择。"
            )
        elif not models:
            error = (
                "Live2D 模型目录中没有找到可用的模型。"
                "把包含 .model3.json 的模型目录放进模型目录后刷新本页即可选择。"
            )
        elif configured and configured not in installed:
            error = (
                f"当前配置的模型「{configured}」在本机没有找到对应资源，"
                "角色窗口可能无法加载模型。请选择下面已安装的模型。"
            )

        active_entry = entries.get(configured)
        return {
            "ok": not error,
            "error": error,
            "models_root": str(root),
            "model_dict_path": str(self.model_dict_path),
            "active_model": configured,
            "active_model_available": bool(configured) and configured in installed,
            "models": models,
            "active": self._describe(configured, active_entry) if configured else None,
        }

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def revision(self) -> str:
        """Content revision of the model dictionary.

        Covers the dictionary and the config together, because a save touches
        both: guarding only one would let a concurrent edit to the other slip
        through unnoticed.
        """
        import hashlib

        payload = ""
        try:
            payload += self.model_dict_path.read_text(encoding="utf-8")
        except OSError:
            pass
        payload += "\x00" + self.read_text()
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    async def save(
        self,
        *,
        model_name: str,
        scale: float,
        x_offset: float,
        y_offset: float,
        expected_revision: str = "",
    ) -> Dict[str, Any]:
        """Select a model and save its scale and position.

        Args:
            model_name: Model to make active.
            scale: Display scale.
            x_offset: Horizontal offset.
            y_offset: Vertical offset.
            expected_revision: Revision the client read.

        Returns:
            ``{ok, error, conflict, restart_required}``.
        """
        if expected_revision and expected_revision != self.revision():
            return {
                "ok": False,
                "error": "模型配置已被其他操作修改，请刷新后重试。",
                "conflict": True,
                "restart_required": False,
            }

        installed = self._installed_models()
        if model_name not in installed:
            return {
                "ok": False,
                "error": (
                    f"模型「{model_name}」在本机没有找到对应资源，无法选择。"
                    "请先把模型放进模型目录。"
                ),
                "conflict": False,
                "restart_required": False,
            }

        errors = self._validate_geometry(scale, x_offset, y_offset)
        if errors:
            return {
                "ok": False,
                "error": "；".join(errors),
                "conflict": False,
                "restart_required": False,
            }

        entries = self.read_model_dict()
        updated = False
        for entry in entries:
            if str(entry.get("name")) == model_name:
                entry["kScale"] = float(scale)
                entry["kXOffset"] = float(x_offset)
                entry["initialYshift"] = float(y_offset)
                updated = True
        if not updated:
            entries.append(
                {
                    "name": model_name,
                    "description": "",
                    "url": f"/live2d-models/{model_name}/{model_name}.model3.json",
                    "kScale": float(scale),
                    "initialXshift": 0,
                    "initialYshift": float(y_offset),
                    "kXOffset": float(x_offset),
                }
            )

        ok, message = self._write_model_dict(entries)
        if not ok:
            return {
                "ok": False,
                "error": message,
                "conflict": False,
                "restart_required": False,
            }

        ok, message = self._write_config_model(model_name)
        if not ok:
            return {
                "ok": False,
                "error": message,
                "conflict": False,
                "restart_required": False,
            }

        logger.info("Live2D model set to {} through the management API.", model_name)
        return {
            "ok": True,
            "error": "",
            "conflict": False,
            # The client fetches the model dictionary at load time, so the
            # character window shows the new settings after it reloads.
            "restart_required": True,
        }

    @staticmethod
    def _validate_geometry(scale: float, x_offset: float, y_offset: float) -> List[str]:
        """Check the placement values are usable numbers."""
        errors: List[str] = []
        try:
            value = float(scale)
        except (TypeError, ValueError):
            errors.append("缩放比例必须是数字。")
        else:
            if not (0.05 <= value <= 10.0):
                errors.append("缩放比例需要在 0.05 到 10 之间。")

        for label, raw in (("水平偏移", x_offset), ("垂直偏移", y_offset)):
            try:
                float(raw)
            except (TypeError, ValueError):
                errors.append(f"{label}必须是数字。")
        return errors

    def _write_model_dict(self, entries: List[Dict[str, Any]]) -> tuple[bool, str]:
        """Write the model dictionary atomically."""
        path = self.model_dict_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(entries, ensure_ascii=False, indent=4)
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
            logger.error("model_dict.json write failed; previous file intact: {}", exc)
            return False, f"保存失败，已保留原模型配置：{exc}"
        return True, ""

    def _write_config_model(self, model_name: str) -> tuple[bool, str]:
        """Set ``live2d_model_name`` in the config, keeping its comments.

        A targeted text edit rather than a re-serialisation, for the same reason
        V2-T01 does it: the config file is documentation and is under version
        control, so a one-field change must not delete its comments.
        """
        text = self.read_text()
        if not text:
            return False, "现有配置无法解析，拒绝覆盖以免损坏。"

        pattern = re.compile(r"^(\s*live2d_model_name:\s*)(.*?)(\s*)$", re.MULTILINE)
        match = pattern.search(text)
        if match is None:
            return False, "配置中找不到 live2d_model_name，未做修改。"

        edited = (
            text[: match.start()]
            + f"{match.group(1)}'{model_name}'{match.group(3)}"
            + text[match.end() :]
        )

        path = self.config_path
        try:
            handle, name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(edited)
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
            logger.error("Config write failed; previous config intact: {}", exc)
            return False, f"保存失败，已保留原配置：{exc}"
        return True, ""


__all__ = ["Live2DService", "PLACEHOLDER_MODEL_NAMES", "MODEL_DICT_FILENAME"]
