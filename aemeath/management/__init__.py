"""Aemeath management API: configuration read/write for the management UI.

The first management loop (v2 task V2-T01): show the model and persona that are
actually configured, let the user change them, and report precisely whether the
change has taken effect.

Split by responsibility:

* ``schema`` — the request/response contract, authoritative for the UI;
* ``service`` — reading, validating and atomically writing the config;
* ``routes`` — the FastAPI surface, loopback-restricted.

The upstream FastAPI app mounts ``routes.install_management_routes`` through
``docs/patches/0005-mount-aemeath-management-routes.patch``.
"""

from __future__ import annotations

from .routes import ROUTE_PREFIX, install_management_routes
from .service import ConfigService

__all__ = ["ConfigService", "ROUTE_PREFIX", "install_management_routes"]
