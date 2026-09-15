"""FastAPI routes for the Aemeath management API.

Mounted by the upstream patch at ``/aemeath/manage``. Three properties matter:

* **Loopback only.** The service listens on ``127.0.0.1``, but a page in the
  user's browser can still issue cross-origin requests to it. Every write is
  therefore refused unless the peer address is loopback, and the ``Origin``
  header, when present, must be a loopback origin.
* **Writes are explicit about failure.** A revision conflict is a 409, an
  invalid field is a 422, and neither one touches the file.
* **Errors carry the same shape as success responses** so the UI has one
  rendering path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .schema import (
    DesktopSettingsRequest,
    LearningActionRequest,
    LearningEditRequest,
    LearningMeaningRequest,
    Live2DSaveRequest,
    MemoryCorrectRequest,
    MemoryForgetRequest,
    MemorySearchRequest,
    RestoreRequest,
    SaveModelRequest,
    SavePersonaRequest,
    VoiceApplyRequest,
    VoiceAuditionRequest,
    VoicePresetRequest,
)
from .service import ConfigService

#: Route prefix, fixed by the v2 design (see docs/architecture.md).
ROUTE_PREFIX = "/aemeath/manage"

#: Loopback addresses a management request may come from.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def is_loopback_client(host: Optional[str]) -> bool:
    """Whether a peer address is loopback.

    Args:
        host: The peer host as reported by the ASGI server.

    Returns:
        ``True`` only for loopback addresses. Unknown or missing values are
        treated as non-loopback: failing closed is the point of the check.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _origin_is_allowed(origin: str) -> bool:
    """Whether an ``Origin`` header names a loopback origin.

    A browser sends ``Origin`` on cross-origin writes. Requiring loopback there
    blocks a page served from the network (or the internet) from rewriting local
    settings, while the desktop client — served from the same loopback host —
    keeps working.
    """
    if not origin:
        return True
    try:
        host = urlparse(origin).hostname
    except ValueError:
        return False
    return is_loopback_client(host)


def install_management_routes(app, config_path=None) -> None:
    """Attach the management routes to an existing FastAPI app.

    Args:
        app: The upstream FastAPI application.
        config_path: Optional explicit config path; defaults to the
            authoritative path resolved from the environment.
    """
    router = APIRouter(prefix=ROUTE_PREFIX, tags=["aemeath-management"])

    def _guard(request: Request):
        """Reject a request that did not come from this machine."""
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        if not is_loopback_client(host):
            logger.warning("Refused a management request from non-loopback {}.", host)
            return JSONResponse(
                status_code=403,
                content={"ok": False, "message": "管理接口仅允许本机访问。"},
            )
        if not _origin_is_allowed(request.headers.get("origin", "")):
            logger.warning(
                "Refused a management request from a non-loopback origin."
            )
            return JSONResponse(
                status_code=403,
                content={"ok": False, "message": "管理接口仅允许本机来源。"},
            )
        return None

    #: One service for the life of the process.
    #:
    #: ``ConfigService`` records the revision the process started with, which is
    #: the definition of "what is running". Building a new service per request
    #: would re-read that revision after every save, making the saved and
    #: running revisions equal — the UI would then report a change as already in
    #: effect when no engine has adopted it. The revision is therefore captured
    #: once, here, at import/mount time.
    service = ConfigService(config_path)

    def _service() -> ConfigService:
        """The service bound to the authoritative config."""
        return service

    @router.get("/overview")
    async def overview(request: Request):
        """Current model, persona and revision state."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return _service().overview()

    @router.post("/model")
    async def save_model(request: Request, payload: SaveModelRequest = Body(...)):
        """Persist a conversation model change."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _service().save_model(
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    @router.post("/persona")
    async def save_persona(request: Request, payload: SavePersonaRequest = Body(...)):
        """Persist the three persona fields."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _service().save_persona(
            identity=payload.identity or "",
            personality=payload.personality or "",
            reply_style=payload.reply_style or "",
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    def _respond(result):
        """Map a save result onto the right status code."""
        if result.get("ok"):
            return JSONResponse(status_code=200, content=result)
        if result.get("conflict"):
            return JSONResponse(status_code=409, content=result)
        return JSONResponse(status_code=422, content=result)

    # ------------------------------------------------------------------
    # Memory (V2-T02)
    # ------------------------------------------------------------------

    def _memory_service():
        """Build the memory service on the runtime's authoritative store.

        The store comes from the single process-wide runtime, so the page edits
        exactly the database conversations write to. Building a second store
        here would let the page report memories the running process cannot see.
        """
        from .memory_admin import MemoryAdminService

        from ..runtime import get_runtime

        runtime = get_runtime()
        return MemoryAdminService(runtime.memory.store)

    @router.post("/memory/search")
    async def memory_search(
        request: Request, payload: MemorySearchRequest = Body(...)
    ):
        """Search the local memory, distinguishing empty from unavailable."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return await _memory_service().search(payload.query, limit=payload.limit)

    @router.get("/memory/list")
    async def memory_list(request: Request):
        """Every memory, including ones the search index cannot reach."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return _memory_service().list_all()

    @router.get("/memory/{memory_id}/impact")
    async def memory_impact(request: Request, memory_id: str):
        """What removing this memory would actually do.

        Read before the user acts so the wider cascading effect is never
        discovered afterwards.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        return _memory_service().describe_impact(memory_id)

    @router.post("/memory/correct")
    async def memory_correct(
        request: Request, payload: MemoryCorrectRequest = Body(...)
    ):
        """Replace a memory's content."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = await _memory_service().correct(
            memory_id=payload.memory_id, content=payload.content
        )
        return _respond(result)

    @router.post("/memory/forget")
    async def memory_forget(
        request: Request, payload: MemoryForgetRequest = Body(...)
    ):
        """Forget one fact precisely.

        A memory whose span cannot be located is not removed here; the response
        asks for a selection instead of reporting a success that did not happen.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        service = _memory_service()
        impact = service.describe_impact(payload.memory_id)

        # Removing the source messages is the wider operation; it only happens
        # with the user's explicit acceptance of that impact.
        if (
            impact.get("mode") == "cascading"
            and impact.get("removes_source_messages")
            and not payload.confirm_cascading
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": (
                        "这条记忆没有可定位的原文片段，遗忘它会连同来源消息一起删除。"
                        "确认后才会执行。"
                    ),
                    "needs_selection": [],
                    "removed_fragments": [],
                    "invalidated_derived": [],
                    "impact": impact,
                },
            )

        result = await service.forget(
            payload.memory_id, fragments=payload.fragments
        )
        return _respond(result)

    @router.get("/memory/backups")
    async def memory_backups(request: Request):
        """Backups available to restore."""
        refused = _guard(request)
        if refused is not None:
            return refused
        from ..config import load_config

        directory = load_config().data_dir / "backups"
        return {
            "ok": True,
            "error": "",
            "backups": _memory_service().list_backups(directory),
        }

    @router.post("/memory/restore")
    async def memory_restore(request: Request, payload: RestoreRequest = Body(...)):
        """Restore a backup, with the warning that forgotten content may return."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = await _memory_service().restore(
            Path(payload.backup_path), confirm=payload.confirm
        )
        return _respond(result)

    # ------------------------------------------------------------------
    # Voice (V2-T02)
    # ------------------------------------------------------------------

    def _voice_service():
        """Build the voice service on the authoritative config."""
        from .voices import VoiceService

        return VoiceService(config_path)

    @router.get("/voice/overview")
    async def voice_overview(request: Request):
        """Presets, the active one, and the configured engine."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return _voice_service().overview()

    @router.post("/voice/preset")
    async def voice_create_preset(
        request: Request, payload: VoicePresetRequest = Body(...)
    ):
        """Save a preset without activating it."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _voice_service().create_preset(
            name=payload.name,
            api_url=payload.api_url,
            ref_audio_path=payload.ref_audio_path,
            prompt_text=payload.prompt_text,
            text_lang=payload.text_lang,
            prompt_lang=payload.prompt_lang,
            text_split_method=payload.text_split_method,
            batch_size=payload.batch_size,
            media_type=payload.media_type,
            streaming_mode=payload.streaming_mode,
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    @router.post("/voice/apply")
    async def voice_apply(request: Request, payload: VoiceApplyRequest = Body(...)):
        """Make a preset the active voice.

        A preset that cannot work is refused before anything is written, so a
        failed apply leaves the previous voice in place.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        result = await _voice_service().apply(
            payload.preset_id, expected_revision=payload.expected_revision
        )
        return _respond(result)

    @router.post("/voice/audition")
    async def voice_audition(
        request: Request, payload: VoiceAuditionRequest = Body(...)
    ):
        """Synthesise a sample without applying the preset."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return await _voice_service().audition(
            payload.preset_id, text=payload.text
        )

    # ------------------------------------------------------------------
    # Live2D (V2-T02)
    # ------------------------------------------------------------------

    def _live2d_service():
        """Build the Live2D service on the authoritative config."""
        from .live2d import Live2DService

        return Live2DService(config_path)

    @router.get("/live2d/overview")
    async def live2d_overview(request: Request):
        """Installed models, the active one, and what is missing.

        Always returns the editable state, so a missing model cannot leave the
        page blank and unusable.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        return _live2d_service().overview()

    @router.post("/live2d/save")
    async def live2d_save(request: Request, payload: Live2DSaveRequest = Body(...)):
        """Select a model and save its scale and position."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = await _live2d_service().save(
            model_name=payload.model_name,
            scale=payload.scale,
            x_offset=payload.x_offset,
            y_offset=payload.y_offset,
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    # ------------------------------------------------------------------
    # Desktop and subtitle (V2-T03)
    # ------------------------------------------------------------------

    def _desktop_service():
        """Build the desktop settings service on the authoritative config."""
        from .desktop import DesktopSettingsService

        return DesktopSettingsService(config_path)

    @router.get("/desktop/settings")
    async def desktop_settings(request: Request):
        """Subtitle sizing and character window geometry.

        Returns usable defaults for anything unset, so a machine that has never
        configured these still gets a readable character.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        return _desktop_service().overview()

    @router.post("/desktop/settings")
    async def save_desktop_settings(
        request: Request, payload: DesktopSettingsRequest = Body(...)
    ):
        """Persist the desktop and subtitle settings."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = await _desktop_service().save(
            values={
                "subtitle_font_size": payload.subtitle_font_size,
                "subtitle_max_width": payload.subtitle_max_width,
                "subtitle_dwell_ms": payload.subtitle_dwell_ms,
                "character_width": payload.character_width,
                "character_height": payload.character_height,
                "character_scale": payload.character_scale,
            },
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    # ------------------------------------------------------------------
    # Expression and jargon learning (V2-T04)
    # ------------------------------------------------------------------

    def _learning_service():
        """Build the learning admin service on the runtime's own service.

        One service for the life of the process, because the entries the page
        edits are the entries the conversation path reads: a second service
        would eventually disagree about which items are enabled.
        """
        from .learning import LearningAdminService

        from ..runtime import get_runtime

        runtime = get_runtime()
        return LearningAdminService(
            getattr(runtime, "learning", None),
            store=getattr(getattr(runtime, "learning", None), "store", None),
        )

    @router.get("/learning/overview")
    async def learning_overview(request: Request, kind: str = "", status: str = "", query: str = ""):
        """List learned expressions or jargon.

        ``kind`` selects the page (``expression``／``jargon``); the response
        always distinguishes "nothing learned yet" from "learning is not
        running", because those need different actions from the user.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        return _learning_service().overview(kind=kind, status=status, query=query)

    @router.get("/learning/{item_id}/sources")
    async def learning_sources(request: Request, item_id: str):
        """Where a learned item came from."""
        refused = _guard(request)
        if refused is not None:
            return refused
        return _learning_service().sources(item_id)

    @router.post("/learning/edit")
    async def learning_edit(request: Request, payload: LearningEditRequest = Body(...)):
        """Apply a manual edit; it takes priority over later automatic results."""
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _learning_service().edit(
            item_id=payload.item_id,
            content=payload.content,
            meaning=payload.meaning,
            scenario=payload.scenario,
            expected_revision=payload.expected_revision,
        )
        return _respond(result)

    @router.post("/learning/toggle")
    async def learning_toggle(
        request: Request, payload: LearningActionRequest = Body(...)
    ):
        """Enable or disable one item.

        ``enabled`` is required: a missing value would have to be guessed, and a
        guess here silently enables something the user asked to switch off.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        if payload.enabled is None:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error": "缺少 enabled 字段：启用或禁用必须明确指定。",
                    "conflict": False,
                    "item_id": payload.item_id,
                    "status": "",
                },
            )
        result = _learning_service().set_enabled(
            item_id=payload.item_id, enabled=payload.enabled
        )
        return _respond(result)

    @router.post("/learning/revoke")
    async def learning_revoke(
        request: Request, payload: LearningActionRequest = Body(...)
    ):
        """Revoke one item.

        A revoked item stops being used and cannot be re-learned from the same
        evidence. This is the operation the requirements describe as 撤销学习.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _learning_service().revoke(item_id=payload.item_id)
        return _respond(result)

    @router.post("/learning/meanings")
    async def learning_meanings(
        request: Request, payload: LearningMeaningRequest = Body(...)
    ):
        """Settle an ambiguous word's meanings.

        Until this is answered the word is not injected at all, because a model
        guessing at an unsettled meaning is how a wrong sense becomes permanent.
        """
        refused = _guard(request)
        if refused is not None:
            return refused
        result = _learning_service().resolve_ambiguity(
            item_id=payload.item_id,
            meanings=[entry.model_dump() for entry in payload.meanings],
        )
        return _respond(result)

    app.include_router(router)


__all__ = ["ROUTE_PREFIX", "install_management_routes", "is_loopback_client"]
