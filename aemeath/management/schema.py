"""Request and response models for the Aemeath management API.

These models are the authoritative contract for the management surface. The
frontend carries no independent copy of the field list; it renders what the
overview returns and posts what the request models accept.

Two rules drive the shapes here:

* **Credentials are write-only.** A request may carry an API key, but no
  response model has a field that could hold one. ``api_key_configured`` is a
  boolean and nothing more, so a secret cannot be echoed even by accident.
* **A save states which revision it started from.** ``expected_revision`` is
  required rather than optional: a client that omits it would silently overwrite
  a concurrent edit, which is exactly the case the acceptance criteria forbid.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SaveModelRequest(BaseModel):
    """Change the conversation model connection.

    ``api_key`` is optional and, when supplied, is stored as a credential
    reference rather than written into the YAML.
    """

    base_url: str = Field(..., description="OpenAI-compatible endpoint base URL")
    model: str = Field(..., description="Model name to request")
    api_key: Optional[str] = Field(
        default=None,
        description="Credential value; persisted as a reference, never echoed back",
    )
    expected_revision: str = Field(
        ..., description="Revision the client read; guards against concurrent edits"
    )


class SavePersonaRequest(BaseModel):
    """Replace the three persona fields.

    Saving this block is what switches the character from the legacy free-text
    prompt to the split fields. The legacy text is retained and remains visible.
    """

    identity: Optional[str] = Field(default="", description="Who the character is")
    personality: Optional[str] = Field(default="", description="Stable tendencies")
    reply_style: Optional[str] = Field(default="", description="Wording and length")
    expected_revision: str = Field(...)


class FieldError(BaseModel):
    """One rejected field, addressed so the UI can focus the right input."""

    field: str
    message: str


class SaveResult(BaseModel):
    """Outcome of a save attempt.

    ``ok`` reports whether the file was written. ``restart_required`` is a
    separate question — a successful save is not a running change, and the UI
    must not present it as one.
    """

    ok: bool
    errors: List[FieldError] = Field(default_factory=list)
    conflict: bool = False
    message: str = ""
    saved_revision: str = ""
    running_revision: str = ""
    restart_required: bool = False


class ModelOverview(BaseModel):
    """The conversation connection as configured.

    There is deliberately no field for the key itself.
    """

    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_configured: bool = False
    api_key_env: str = ""


class PersonaOverview(BaseModel):
    """The persona as configured, with both sources visible."""

    mode: str = "legacy"
    identity: str = ""
    personality: str = ""
    reply_style: str = ""
    legacy_prompt: str = ""
    active_prompt: str = ""


class OverviewResponse(BaseModel):
    """Everything the overview page shows, in one round trip."""

    config_path: str = ""
    saved_revision: str = ""
    running_revision: str = ""
    restart_required: bool = False
    model: ModelOverview = Field(default_factory=ModelOverview)
    persona: PersonaOverview = Field(default_factory=PersonaOverview)
    read_error: str = ""


# ----------------------------------------------------------------------
# Memory management (V2-T02)
# ----------------------------------------------------------------------


class MemorySearchRequest(BaseModel):
    """Search the local memory.

    An empty ``query`` lists everything, which is what the page does on open.
    """

    query: str = Field(default="", description="Search text; empty lists all")
    limit: int = Field(default=20, ge=1, le=200)


class MemorySource(BaseModel):
    """One message a memory was extracted from."""

    message_id: str = ""
    fragment: str = ""
    content: str = ""
    revision: int = 1


class MemoryItemView(BaseModel):
    """A memory as the management page shows it."""

    memory_id: str = ""
    kind: str = ""
    content: str = ""
    valid: bool = True
    superseded_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    sources: List[MemorySource] = Field(default_factory=list)


class MemoryListResponse(BaseModel):
    """A memory listing that distinguishes "empty" from "unavailable".

    ``available`` is the distinction the acceptance criteria require: an empty
    list with ``ok`` false means the search could not run, which is a different
    situation from a genuinely empty memory and must not be rendered the same.
    """

    ok: bool = True
    available: bool = True
    error: str = ""
    memories: List[MemoryItemView] = Field(default_factory=list)


class MemoryCorrectRequest(BaseModel):
    """Replace a memory's content.

    The old memory is kept for provenance and excluded from retrieval.
    """

    memory_id: str = Field(..., description="Memory to supersede")
    content: str = Field(..., description="Replacement text")


class MemoryForgetRequest(BaseModel):
    """Forget one fact.

    ``fragments`` is the user's explicit span selection, needed when the stored
    evidence has no locatable span. Without it, such a memory is not removed and
    the response asks for a selection instead of claiming success.
    """

    memory_id: str = Field(..., description="Memory to forget")
    fragments: Optional[List[str]] = Field(
        default=None, description="Explicit source spans to remove"
    )
    confirm_cascading: bool = Field(
        default=False,
        description="Accepted the removal of the source messages as well",
    )


class MemoryImpactResponse(BaseModel):
    """What removing a memory would actually do.

    Shown before the user acts, so "remove just this one" cannot silently mean
    "and also delete the messages it came from".
    """

    ok: bool = True
    error: str = ""
    mode: str = ""
    message: str = ""
    removes_source_messages: bool = False
    affected_message_ids: List[str] = Field(default_factory=list)


class MemoryMutationResponse(BaseModel):
    """Outcome of a correction or a forgetting."""

    ok: bool = False
    error: str = ""
    memory_id: str = ""
    needs_selection: List[MemorySource] = Field(default_factory=list)
    removed_fragments: List[str] = Field(default_factory=list)
    invalidated_derived: List[str] = Field(default_factory=list)
    rebuild_required: bool = False


class BackupEntry(BaseModel):
    """One backup file available to restore."""

    path: str = ""
    name: str = ""
    size_bytes: int = 0
    modified_at: float = 0.0


class BackupListResponse(BaseModel):
    """Backups on disk."""

    ok: bool = True
    error: str = ""
    backups: List[BackupEntry] = Field(default_factory=list)


class RestoreRequest(BaseModel):
    """Restore a backup.

    ``confirm`` is required: the warning states that forgotten content may come
    back, and an unconfirmed restore does not run.
    """

    backup_path: str = ""
    confirm: bool = False


class RestoreResponse(BaseModel):
    """Outcome of a restore, carrying the warning either way."""

    ok: bool = False
    error: str = ""
    requires_confirmation: bool = False
    may_restore_forgotten_content: bool = False
    warning: str = ""
    backup_path: str = ""
    restored: bool = False
    restart_required: bool = False


# ----------------------------------------------------------------------
# Voice management (V2-T02)
# ----------------------------------------------------------------------


class VoicePreset(BaseModel):
    """One voice preset.

    A voice is the whole parameter set — weights, reference audio, transcript
    and synthesis parameters — because applying part of it would leave the
    character speaking with a transcript that does not match the audio.

    ``is_official_voice`` is always false while the real voice material does not
    exist; the UI relies on it to label the bundled voice as a sample.
    """

    preset_id: str = ""
    name: str = ""
    label: str = ""
    description: str = ""
    is_official_voice: bool = False
    builtin: bool = False
    api_url: str = ""
    ref_audio_path: str = ""
    prompt_text: str = ""
    text_lang: str = "zh"
    prompt_lang: str = "zh"
    text_split_method: str = "cut5"
    batch_size: str = "1"
    media_type: str = "wav"
    streaming_mode: str = "false"


class VoiceOverviewResponse(BaseModel):
    """Everything the voice page shows."""

    presets: List[VoicePreset] = Field(default_factory=list)
    active_preset_id: str = ""
    engine: str = ""
    engine_is_gpt_sovits: bool = False
    read_error: str = ""


class VoicePresetRequest(BaseModel):
    """Create a voice preset without activating it."""

    name: str = Field(..., description="Display name")
    api_url: str = Field(..., description="GPT-SoVITS api_v2 endpoint")
    ref_audio_path: str = Field(..., description="Reference audio defining the voice")
    prompt_text: str = Field(..., description="Transcript of the reference audio")
    text_lang: str = "zh"
    prompt_lang: str = "zh"
    text_split_method: str = "cut5"
    batch_size: str = "1"
    media_type: str = "wav"
    streaming_mode: str = "false"
    expected_revision: str = ""


class VoiceApplyRequest(BaseModel):
    """Activate a preset."""

    preset_id: str = Field(..., description="Preset to make active")
    expected_revision: str = ""


class VoiceAuditionRequest(BaseModel):
    """Synthesise a sample with a preset, without applying it."""

    preset_id: str = ""
    text: str = Field(default="你好，我是爱弥斯。", description="Text to speak")


class VoiceAuditionResponse(BaseModel):
    """Audition result. ``audio`` is base64 for transport in JSON."""

    ok: bool = False
    error: str = ""
    audio: str = ""
    media_type: str = ""


class VoiceActionResult(BaseModel):
    """Outcome of creating or applying a preset."""

    ok: bool = False
    error: str = ""
    preset_id: str = ""
    conflict: bool = False
    restart_required: bool = False


# ----------------------------------------------------------------------
# Live2D management (V2-T02)
# ----------------------------------------------------------------------


class Live2DModel(BaseModel):
    """One Live2D model available on this machine."""

    name: str = ""
    label: str = ""
    description: str = ""
    is_official_model: bool = False
    is_placeholder: bool = False
    url: str = ""
    scale: float = 1.0
    x_offset: float = 0.0
    y_offset: float = 0.0
    x_shift: float = 0.0


class Live2DOverviewResponse(BaseModel):
    """The Live2D page state.

    ``error`` is populated instead of leaving the page empty: a missing model is
    a state the user must be able to read and act on, not a blank screen.
    """

    ok: bool = True
    error: str = ""
    models_root: str = ""
    model_dict_path: str = ""
    active_model: str = ""
    active_model_available: bool = False
    models: List[Live2DModel] = Field(default_factory=list)
    active: Optional[Live2DModel] = None


class Live2DSaveRequest(BaseModel):
    """Select a model and save its scale and position."""

    model_name: str = Field(..., description="Model to activate")
    scale: float = 1.0
    x_offset: float = 0.0
    y_offset: float = 0.0
    expected_revision: str = ""


class Live2DSaveResult(BaseModel):
    """Outcome of a Live2D save."""

    ok: bool = False
    error: str = ""
    conflict: bool = False
    restart_required: bool = False


# ----------------------------------------------------------------------
# Desktop and subtitle (V2-T03)
# ----------------------------------------------------------------------


class DesktopSettingsResponse(BaseModel):
    """Subtitle and character-window presentation settings.

    Both windows read the same values: the character window lays out its
    subtitle with them, and the management window edits them. They therefore
    live in the authoritative config rather than in either window's local state.

    ``revision`` belongs to this response so a save is based on the exact read
    it came from; see ``DesktopSettingsService.overview``.
    """

    subtitle_font_size: int = 22
    subtitle_max_width: int = 520
    subtitle_dwell_ms: int = 4000
    character_width: int = 420
    character_height: int = 620
    character_scale: float = 1.0
    read_error: str = ""
    revision: str = ""


class DesktopSettingsRequest(BaseModel):
    """The complete candidate set for a desktop settings save.

    Every field is required: a partial update would leave the caller guessing
    which values were kept, and the page always has the full set on screen.
    """

    subtitle_font_size: int = Field(..., description="Subtitle font size in px")
    subtitle_max_width: int = Field(..., description="Max subtitle width in px")
    subtitle_dwell_ms: int = Field(..., description="Line dwell time in ms")
    character_width: int = Field(..., description="Character window width in px")
    character_height: int = Field(..., description="Character window height in px")
    character_scale: float = Field(..., description="Model scale multiplier")
    expected_revision: str = Field(
        ..., description="Revision the client read; guards concurrent edits"
    )


def to_dict(model: BaseModel) -> Dict[str, Any]:
    """Serialise a response model to plain JSON-compatible data."""
    return model.model_dump()


__all__ = [
    "SaveModelRequest",
    "SavePersonaRequest",
    "FieldError",
    "SaveResult",
    "ModelOverview",
    "PersonaOverview",
    "OverviewResponse",
    "MemorySearchRequest",
    "MemorySource",
    "MemoryItemView",
    "MemoryListResponse",
    "MemoryCorrectRequest",
    "MemoryForgetRequest",
    "MemoryImpactResponse",
    "MemoryMutationResponse",
    "BackupEntry",
    "BackupListResponse",
    "RestoreRequest",
    "RestoreResponse",
    "VoicePreset",
    "VoiceOverviewResponse",
    "VoicePresetRequest",
    "VoiceApplyRequest",
    "VoiceAuditionRequest",
    "VoiceAuditionResponse",
    "VoiceActionResult",
    "Live2DModel",
    "Live2DOverviewResponse",
    "Live2DSaveRequest",
    "Live2DSaveResult",
    "DesktopSettingsResponse",
    "DesktopSettingsRequest",
    "to_dict",
]
