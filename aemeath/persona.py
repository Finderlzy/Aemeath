"""Persona resolution: three user fields over a legacy free-text prompt.

The v2 management UI edits three fields — identity, personality and reply style
(see ``context.md``: 身份／性格／回复风格). Existing configs only carry the
free-text ``character_config.persona_prompt``.

The rule that matters, and the reason this lives in its own module, is that the
legacy text is **never auto-split**. Only the user saving the three fields
themselves switches the source. Auto-splitting would guess at prose the user
wrote deliberately, and there would be no way back to the original wording.

Consequences enforced here:

* exactly one persona source is returned, so the prompt cannot receive both the
  old text and the new fields;
* a half-written split block (for example only ``identity``) is not treated as
  usable — it would silently drop the rest of the persona;
* the legacy prompt stays readable in the overview even after migration.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Where the split persona block lives inside the config document.
PERSONA_BLOCK_KEY = "persona"

#: Where the original free-text prompt is preserved after a migration.
#:
#: Once migrated, ``persona_prompt`` holds the active split persona, so the
#: user's original wording must be kept elsewhere or it is lost. This lives
#: beside the persona block, under ``aemeath_config``.
LEGACY_PROMPT_KEY = "legacy_persona_prompt"

#: Modes recorded in the block. ``legacy`` is the default and means "use
#: ``persona_prompt`` verbatim".
MODE_LEGACY = "legacy"
MODE_SPLIT = "split"

#: Labels used when rendering the three fields into one prompt.
_FIELD_LABELS = (
    ("identity", "身份"),
    ("personality", "性格"),
    ("reply_style", "回复风格"),
)


def _character_config(document: Dict[str, Any]) -> Dict[str, Any]:
    """The ``character_config`` section of a config document."""
    character = document.get("character_config")
    return character if isinstance(character, dict) else {}


def legacy_prompt(document: Dict[str, Any]) -> str:
    """The user's original free-text persona.

    After a migration ``persona_prompt`` holds the generated split persona, so
    the preserved copy is preferred when one exists. Before any migration the
    two are the same thing and this returns ``persona_prompt``.
    """
    preserved = _preserved_legacy_prompt(document)
    if preserved:
        return preserved
    character = _character_config(document)
    value = character.get("persona_prompt")
    return value if isinstance(value, str) else ""


def _preserved_legacy_prompt(document: Dict[str, Any]) -> str:
    """The archived original prompt, or an empty string when not archived."""
    character = _character_config(document)
    aemeath_config = character.get("aemeath_config")
    if not isinstance(aemeath_config, dict):
        return ""
    value = aemeath_config.get(LEGACY_PROMPT_KEY)
    return value if isinstance(value, str) else ""


def persona_block(document: Dict[str, Any]) -> Dict[str, Any]:
    """The stored split-persona block, or an empty dict when absent."""
    character = _character_config(document)
    aemeath_config = character.get("aemeath_config")
    if not isinstance(aemeath_config, dict):
        return {}
    block = aemeath_config.get(PERSONA_BLOCK_KEY)
    return block if isinstance(block, dict) else {}


def persona_mode(document: Dict[str, Any]) -> str:
    """Which persona source this document uses: ``legacy`` or ``split``.

    A block is only honoured when it says ``split`` *and* carries the three
    fields. Anything else falls back to the legacy text, so a damaged or
    partially written block degrades to the last usable persona instead of
    producing an empty one.
    """
    block = persona_block(document)
    if block.get("mode") != MODE_SPLIT:
        return MODE_LEGACY
    if not all(isinstance(block.get(key), str) and block.get(key).strip() for key, _ in _FIELD_LABELS):
        return MODE_LEGACY
    return MODE_SPLIT


def render_split(identity: str, personality: str, reply_style: str) -> str:
    """Render the three fields as one persona prompt.

    The labels are kept because the model benefits from knowing which part is
    the stable identity and which is a style preference; without them a
    personality clause and a formatting rule read as the same kind of
    instruction.
    """
    parts = []
    for (key, label), value in zip(_FIELD_LABELS, (identity, personality, reply_style)):
        text = (value or "").strip()
        if text and key:
            parts.append(f"{label}：{text}")
    return "\n\n".join(parts)


def resolve_persona(document: Dict[str, Any]) -> str:
    """The single persona prompt this document should use.

    Returns:
        The persona text. Callers must not append the legacy prompt as well:
        this function already decided which source is authoritative.
    """
    if persona_mode(document) == MODE_SPLIT:
        block = persona_block(document)
        rendered = render_split(
            block.get("identity", ""),
            block.get("personality", ""),
            block.get("reply_style", ""),
        )
        if rendered:
            return rendered
    return legacy_prompt(document)


def build_persona_block(
    identity: str, personality: str, reply_style: str
) -> Dict[str, Any]:
    """Build the block written when the user saves the three fields."""
    return {
        "mode": MODE_SPLIT,
        "identity": identity or "",
        "personality": personality or "",
        "reply_style": reply_style or "",
    }


def describe(document: Dict[str, Any]) -> Dict[str, Any]:
    """Overview payload for the persona page.

    The legacy text is always included so the user can see and preserve the
    original wording even after switching to the three fields.
    """
    block = persona_block(document)
    mode = persona_mode(document)
    return {
        "mode": mode,
        "identity": block.get("identity", "") if mode == MODE_SPLIT else "",
        "personality": block.get("personality", "") if mode == MODE_SPLIT else "",
        "reply_style": block.get("reply_style", "") if mode == MODE_SPLIT else "",
        "legacy_prompt": legacy_prompt(document),
        "active_prompt": resolve_persona(document),
    }


def validate_fields(
    identity: Optional[str], personality: Optional[str], reply_style: Optional[str]
) -> list[Dict[str, str]]:
    """Validate the three fields, returning a list of field errors.

    Saving all three blank would leave the character with no persona at all, so
    at least one is required. Individual fields are otherwise free text.
    """
    errors: list[Dict[str, str]] = []
    if not any((value or "").strip() for value in (identity, personality, reply_style)):
        errors.append(
            {
                "field": "persona",
                "message": "身份、性格、回复风格至少填写一项，否则角色将没有人设。",
            }
        )
    return errors


__all__ = [
    "PERSONA_BLOCK_KEY",
    "LEGACY_PROMPT_KEY",
    "MODE_LEGACY",
    "MODE_SPLIT",
    "legacy_prompt",
    "persona_block",
    "persona_mode",
    "resolve_persona",
    "render_split",
    "build_persona_block",
    "describe",
    "validate_fields",
]
