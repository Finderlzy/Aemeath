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
    "to_dict",
]
