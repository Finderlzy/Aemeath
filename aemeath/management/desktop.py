"""Desktop presentation settings: subtitle sizing and character window geometry.

V2-07 asks for a subtitle whose font size, width and dwell time are settable,
and V2-06 for a character window the user can size. Neither had a storage
location: the Live2D page only covers the model and its scale, and the window
geometry lived purely in the Electron main process.

This module gives both a home in the **authoritative** config, following the
same rules as the rest of the management surface:

* the values are validated as a whole before anything is written;
* the write is atomic, so a rejected save leaves the previous file intact;
* a revision is returned so the UI can distinguish 已保存 from 已生效.

Everything here is presentation only. Nothing in this module starts a session,
opens an audio device, or changes how the character speaks — the management
window must never become a second owner of the conversation.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..config import resolve_config_path

#: Section of the authoritative config that carries these values.
CONFIG_SECTION = "desktop"

#: Bounds, with the reason they exist rather than being arbitrary.
#:
#: * the subtitle must stay readable and fit one line: 10–48 px and 160–1600 px;
#: * the dwell has to be long enough to read and short enough not to pin stale
#:   text: 0.5–60 s;
#: * the character window must remain visible on a small display: 200–2000 px.
LIMITS = {
    "subtitle_font_size": (10, 48),
    "subtitle_max_width": (160, 1600),
    "subtitle_dwell_ms": (500, 60000),
    "character_width": (200, 2000),
    "character_height": (200, 2000),
    "character_scale": (0.1, 4.0),
}

#: Defaults, matching the character window's own fallbacks.
DEFAULTS: Dict[str, Any] = {
    "subtitle_font_size": 22,
    "subtitle_max_width": 520,
    "subtitle_dwell_ms": 4000,
    "character_width": 420,
    "character_height": 620,
    "character_scale": 1.0,
}

#: Human-readable names for error messages.
FIELD_LABELS = {
    "subtitle_font_size": "字幕字号",
    "subtitle_max_width": "字幕宽度",
    "subtitle_dwell_ms": "字幕停留时间",
    "character_width": "角色窗口宽度",
    "character_height": "角色窗口高度",
    "character_scale": "角色缩放",
}


class DesktopSettingsService:
    """Read and write the desktop presentation section of the config."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        """Bind the service to one config file.

        Args:
            config_path: Explicit config path; defaults to the authoritative
                one resolved from the environment.
        """
        self.config_path = Path(
            config_path or resolve_config_path()
        ).expanduser()

    # -- reading ---------------------------------------------------------

    def read_text(self) -> str:
        """The config file text, or an empty string when it does not exist."""
        try:
            return self.config_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def read_document(self) -> Dict[str, Any]:
        """The parsed config document.

        Parsed with ``yaml.safe_load`` rather than upstream's ``read_yaml``: the
        latter expands ``${VAR}`` references, which would put resolved secrets
        in this process's memory for no reason — this module never needs a
        credential.
        """
        text = self.read_text()
        if not text:
            return {}
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def revision(self) -> str:
        """A content revision token for the config document."""
        return hashlib.sha256(self.read_text().encode("utf-8")).hexdigest()[:16]

    def _section(self) -> Dict[str, Any]:
        """The desktop section as currently stored."""
        document = self.read_document()
        character = document.get("character_config") or {}
        aemeath_config = character.get("aemeath_config") or {}
        section = aemeath_config.get(CONFIG_SECTION) or {}
        return section if isinstance(section, dict) else {}

    def overview(self) -> Dict[str, Any]:
        """Current settings, with defaults filled in for anything unset.

        A missing or partly filled section is not an error: a machine that has
        never opened this page must still get usable values. ``read_error`` is
        reserved for a config that exists but cannot be parsed, which is the
        case the UI has to distinguish from "not configured yet".

        The response carries its own ``revision``. The page must save with the
        revision that belongs to *this* read: taking it from a different
        endpoint (as an earlier version did) meant a transient failure there
        silently produced an empty revision, and an empty revision makes the
        backend skip the conflict check entirely — so a save could overwrite a
        newer change while appearing to succeed.
        """
        read_error = ""
        if self.config_path.is_file():
            text = self.read_text()
            if text.strip():
                try:
                    yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    read_error = f"配置文件无法解析：{exc}"

        section = self._section()
        result: Dict[str, Any] = {
            "read_error": read_error,
            "revision": self.revision(),
        }
        for field, fallback in DEFAULTS.items():
            raw = section.get(field)
            if raw is None:
                result[field] = fallback
            elif field == "character_scale":
                result[field] = self._as_float(raw, fallback)
            else:
                result[field] = self._as_int(raw, fallback)
        return result

    # -- writing ---------------------------------------------------------

    def validate(self, values: Dict[str, Any]) -> List[Dict[str, str]]:
        """Check every field, returning one entry per rejection.

        All fields are checked before anything is written, so a form with two
        mistakes reports both instead of only the first.
        """
        errors: List[Dict[str, str]] = []
        for field, (low, high) in LIMITS.items():
            raw = values.get(field)
            label = FIELD_LABELS.get(field, field)
            if raw is None:
                errors.append({"field": field, "message": f"{label}不能为空。"})
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append({"field": field, "message": f"{label}必须是数字。"})
                continue
            if number < low or number > high:
                errors.append({
                    "field": field,
                    "message": f"{label}应在 {low} 到 {high} 之间，当前为 {number}。",
                })
        return errors

    async def save(
        self,
        *,
        values: Dict[str, Any],
        expected_revision: str = "",
    ) -> Dict[str, Any]:
        """Persist the desktop settings.

        Args:
            values: The complete candidate set.
            expected_revision: Revision the client read; guards concurrent edits.

        Returns:
            ``{ok, errors, conflict, message, saved_revision, running_revision,
            restart_required}``.
        """
        if expected_revision and expected_revision != self.revision():
            return {
                "ok": False,
                "errors": [],
                "conflict": True,
                "message": "配置已被其他操作修改，本次改动没有保存。请刷新后重试。",
                "saved_revision": self.revision(),
                "running_revision": "",
                "restart_required": False,
            }

        errors = self.validate(values)
        if errors:
            return {
                "ok": False,
                "errors": errors,
                "conflict": False,
                "message": "保存被拒绝，配置未改动。",
                "saved_revision": self.revision(),
                "running_revision": "",
                "restart_required": False,
            }

        original = self.read_text()
        if not original.strip():
            return {
                "ok": False,
                "errors": [],
                "conflict": False,
                "message": f"无法读取配置文件：{self.config_path}",
                "saved_revision": "",
                "running_revision": "",
                "restart_required": False,
            }

        # Apply the change as targeted text edits rather than re-serialising the
        # document. The config is documentation — it carries dozens of comment
        # lines explaining each setting and it is under version control — so a
        # `yaml.safe_dump` round trip would silently delete every comment and
        # reflow the whole file. V2-T01 established this rule for the model and
        # persona writes; this module follows the same one.
        updated = original
        try:
            updated = self._ensure_desktop_block(updated)
        except ValueError as exc:
            return {
                "ok": False,
                "errors": [],
                "conflict": False,
                "message": str(exc),
                "saved_revision": self.revision(),
                "running_revision": "",
                "restart_required": False,
            }

        for field in LIMITS:
            raw = values[field]
            written = (
                f"{round(float(raw), 3)}" if field == "character_scale" else f"{int(raw)}"
            )
            updated = self._set_desktop_scalar(updated, field, written)

        # Subtitle values are read by the running client on its next fetch, so
        # this section does not need a restart. That is stated in the response
        # rather than left for the user to guess.
        written_ok, error = self._write_atomic(updated)
        if not written_ok:
            return {
                "ok": False,
                "errors": [],
                "conflict": False,
                "message": error,
                "saved_revision": self.revision(),
                "running_revision": "",
                "restart_required": False,
            }

        revision = self.revision()
        return {
            "ok": True,
            "errors": [],
            "conflict": False,
            "message": "桌面与字幕设置已保存。",
            "saved_revision": revision,
            "running_revision": os.getenv("AEMEATH_RUNNING_REVISION", revision),
            "restart_required": False,
        }

    # -- textual editing -------------------------------------------------

    def _set_desktop_scalar(self, text: str, key: str, value: str) -> str:
        """Replace one scalar under the desktop block, in place.

        Args:
            text: The config text.
            key: Field name, e.g. ``subtitle_font_size``.
            value: The new numeric literal.

        Returns:
            The updated text. The key is matched only *inside* the desktop
            block, so a same-named field elsewhere (or a comment mentioning it)
            is never touched.
        """
        block = self._desktop_block_span(text)
        if block is None:
            return text
        start, end = block
        pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)(\S+)(\s*)$", re.MULTILINE)
        segment = text[start:end]
        new_segment, count = pattern.subn(
            lambda m: f"{m.group(1)}{value}{m.group(3)}", segment, count=1
        )
        if count == 0:
            # The field is missing from an existing block; append it so a save
            # is never silently partial.
            indent = "      "
            insertion = f"{indent}{key}: {value}\n"
            new_segment = new_segment.rstrip("\n") + "\n" + insertion
        return text[:start] + new_segment + text[end:]

    def _desktop_block_span(self, text: str) -> Optional[tuple[int, int]]:
        """Character span of the ``desktop:`` block inside the config.

        Returns:
            ``(start, end)`` offsets covering the block's body, or ``None`` when
            the block does not exist.
        """
        lines = text.splitlines(keepends=True)
        offset = 0
        header_indent: Optional[int] = None
        start = 0

        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if header_indent is None:
                if stripped == "desktop:":
                    header_indent = indent
                    start = offset + len(line)
                offset += len(line)
                continue
            if stripped and indent <= header_indent:
                return (start, offset)
            offset += len(line)

        if header_indent is None:
            return None
        return (start, offset)

    def _ensure_desktop_block(self, text: str) -> str:
        """Create the desktop block if the config has none yet.

        The block is inserted under ``aemeath_config`` so it lands where the
        reader expects it. A config with no ``aemeath_config`` section at all is
        reported rather than guessed at.
        """
        if self._desktop_block_span(text) is not None:
            return text

        pattern = re.compile(r"^(\s*)aemeath_config:\s*$", re.MULTILINE)
        match = pattern.search(text)
        if match is None:
            raise ValueError(
                "配置中没有 aemeath_config 段落，无法写入桌面设置；"
                "请先确认配置文件完整。"
            )

        indent = match.group(1)
        block = (
            f"{indent}  desktop:\n"
            + "".join(
                f"{indent}    {field}: {default}\n"
                for field, default in DEFAULTS.items()
            )
        )
        # Insert directly after the aemeath_config header line.
        insert_at = match.end()
        return text[:insert_at] + "\n" + block.rstrip("\n") + text[insert_at:]

    # -- internals -------------------------------------------------------

    def _write_atomic(self, text: str) -> tuple[bool, str]:
        """Write config text via a temporary file and ``os.replace``.

        A failure at any point leaves the previous config byte-identical, which
        is what makes a rejected save safe to retry.
        """
        try:
            directory = self.config_path.parent
            directory.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(
                prefix=".aemeath-desktop-", suffix=".yaml", dir=str(directory)
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.config_path)
            except BaseException:
                # The temporary file must not be left behind on any failure.
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            return False, f"写入配置失败，原文件保持不变：{exc}"
        return True, ""

    @staticmethod
    def _as_int(raw: Any, fallback: int) -> int:
        """Coerce to int, falling back rather than raising."""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _as_float(raw: Any, fallback: float) -> float:
        """Coerce to float, falling back rather than raising."""
        try:
            return float(raw)
        except (TypeError, ValueError):
            return fallback
