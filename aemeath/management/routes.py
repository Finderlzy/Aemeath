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

from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .schema import SaveModelRequest, SavePersonaRequest
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

    app.include_router(router)


__all__ = ["ROUTE_PREFIX", "install_management_routes", "is_loopback_client"]
