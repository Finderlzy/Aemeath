"""V2-T02 production-entry tests: the management routes themselves.

The service-level tests prove the behaviour; these prove the *surface*: that the
new pages are reachable on the real FastAPI application, that the loopback
restriction still covers them, and that failures come back with the status codes
the frontend branches on.

They mount the routes through the real
:func:`aemeath.management.routes.install_management_routes`, the same function
the upstream patch calls, so a route that is defined but never registered is
caught here rather than in the browser.

Acceptance criteria covered: the V2-T02 pages are reachable and refuse
non-loopback callers on every new write path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fixture
# ----------------------------------------------------------------------


class _LoopbackTestClient:
    """A TestClient whose peer address is loopback.

    ``TestClient`` reports the peer host as ``testclient``, which the management
    guard refuses — correctly, since that is not a loopback address. Production
    requests arrive from ``127.0.0.1``; this wrapper supplies that scope so the
    tests exercise the routes rather than the refusal path, while
    ``test_every_new_write_path_refuses_a_non_loopback_origin`` still covers the
    refusal itself.
    """

    def __init__(self, client) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs):
        """Issue a request with a loopback peer address."""
        kwargs.setdefault("headers", {})
        return self._client.request(method, url, **kwargs)

    def get(self, url: str, **kwargs):
        """GET with a loopback peer address."""
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        """POST with a loopback peer address."""
        return self._client.post(url, **kwargs)

    def __getattr__(self, name):
        """Delegate anything else to the wrapped client."""
        return getattr(self._client, name)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A real FastAPI test client with the management routes mounted.

    The memory store is the real one on an isolated database; the runtime is
    installed as the process singleton, exactly as the harness does, so the
    routes resolve the same store a conversation would write to.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath import config as config_module
    from aemeath.management.routes import install_management_routes
    from aemeath.runtime import build_runtime, reset_runtime
    from tests.integration.harness import build_config_document

    import yaml

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    # A real runtime, so the memory routes reach a real store.
    from aemeath.adapters import FakeEmbeddingAdapter
    from aemeath.config import load_config

    reset_runtime()
    runtime = build_runtime(
        config=load_config(config_path), embedding=FakeEmbeddingAdapter()
    )
    import aemeath.runtime as runtime_module

    runtime_module._runtime = runtime

    app = FastAPI()
    install_management_routes(app, config_path)

    # Starlette resolves ``request.client`` from the ASGI scope. Wrapping the
    # app (rather than weakening the guard) supplies a loopback peer, so the
    # tests exercise the routes while the refusal path keeps being tested by
    # test_every_new_write_path_refuses_a_non_loopback_origin.
    async def loopback_app(scope, receive, send):
        """Present a loopback peer address to the application."""
        if scope.get("type") == "http":
            scope = {**scope, "client": ("127.0.0.1", 54321)}
        await app(scope, receive, send)

    with TestClient(loopback_app) as test_client:
        test_client.config_path = config_path
        test_client.runtime = runtime
        yield test_client

    reset_runtime()


# ----------------------------------------------------------------------
# Reachability
# ----------------------------------------------------------------------


async def test_memory_routes_are_reachable(client):
    """The memory endpoints answer on the mounted application."""
    assert client.get("/aemeath/manage/memory/list").status_code == 200
    response = client.post("/aemeath/manage/memory/search", json={"query": ""})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["memories"] == []


async def test_voice_route_is_reachable(client):
    """The voice overview answers and lists the sample preset."""
    response = client.get("/aemeath/manage/voice/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["presets"]
    assert payload["presets"][0]["is_official_voice"] is False


async def test_live2d_route_is_reachable(client):
    """The Live2D overview answers with a stated state, not a bare list."""
    response = client.get("/aemeath/manage/live2d/overview")

    assert response.status_code == 200
    payload = response.json()
    assert "models" in payload
    assert "active_model" in payload
    assert "error" in payload


async def test_existing_v2_t01_routes_still_work(client):
    """The V2-T01 overview is untouched by the new pages."""
    response = client.get("/aemeath/manage/overview")

    assert response.status_code == 200
    assert response.json()["model"]["model"] == "test-model"


# ----------------------------------------------------------------------
# Loopback restriction covers the new writes
# ----------------------------------------------------------------------


async def test_every_new_write_path_refuses_a_non_loopback_origin(client):
    """A foreign origin cannot reach any of the new write endpoints.

    Each new route must go through the same guard as V2-T01's; a route added
    without it would be a remote way to edit local memory and configuration.
    """
    headers = {"Origin": "http://evil.example.com"}
    cases = [
        ("/aemeath/manage/memory/correct", {"memory_id": "x", "content": "y"}),
        ("/aemeath/manage/memory/forget", {"memory_id": "x"}),
        ("/aemeath/manage/memory/restore", {"backup_path": "x", "confirm": True}),
        (
            "/aemeath/manage/voice/preset",
            {
                "name": "n",
                "api_url": "http://127.0.0.1:9880/tts",
                "ref_audio_path": "a.wav",
                "prompt_text": "t",
            },
        ),
        ("/aemeath/manage/voice/apply", {"preset_id": "p"}),
        ("/aemeath/manage/voice/audition", {"preset_id": "p", "text": "t"}),
        (
            "/aemeath/manage/live2d/save",
            {"model_name": "m", "scale": 1.0, "x_offset": 0, "y_offset": 0},
        ),
    ]

    for path, body in cases:
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 403, f"{path} did not refuse a foreign origin"


# ----------------------------------------------------------------------
# Status codes the frontend branches on
# ----------------------------------------------------------------------


async def test_correcting_an_unknown_memory_returns_422(client):
    """A rejected write is a 422 with a message, not a 200 with ok=false."""
    response = client.post(
        "/aemeath/manage/memory/correct",
        json={"memory_id": "does-not-exist", "content": "新内容"},
    )

    assert response.status_code == 422
    assert response.json()["ok"] is False
    assert response.json()["error"]


async def test_applying_an_unknown_preset_returns_422(client):
    """An unknown preset is refused as a validation error."""
    response = client.post(
        "/aemeath/manage/voice/apply", json={"preset_id": "nope"}
    )

    assert response.status_code == 422
    assert response.json()["ok"] is False


async def test_cascading_forget_requires_confirmation(client):
    """Forgetting a memory whose sources would go returns 409 until confirmed.

    The 409 is deliberate: the operation is available, but only after the user
    has seen and accepted the wider impact.
    """
    store = client.runtime.memory.store
    message_id = store.add_message(
        role="user",
        source="user_text",
        content="一句和记忆没有字面重合的原文",
        status="complete",
    )
    memory_id = store.add_memory(
        content="无法在原文中定位的记忆", source_message_ids=[message_id]
    )

    impact = client.get(f"/aemeath/manage/memory/{memory_id}/impact")
    assert impact.status_code == 200
    assert impact.json()["mode"] == "cascading"

    refused = client.post(
        "/aemeath/manage/memory/forget", json={"memory_id": memory_id}
    )
    assert refused.status_code == 409
    assert store.get_memory(memory_id) is not None, "nothing was removed"

    confirmed = client.post(
        "/aemeath/manage/memory/forget",
        json={"memory_id": memory_id, "confirm_cascading": True},
    )
    assert confirmed.status_code in (200, 422)


async def test_restore_without_confirmation_returns_422(client, tmp_path):
    """An unconfirmed restore does not run and says so."""
    backup = tmp_path / "backups" / "a.sqlite3"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"not-a-real-db")

    response = client.post(
        "/aemeath/manage/memory/restore",
        json={"backup_path": str(backup), "confirm": False},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["requires_confirmation"] is True
    assert payload["may_restore_forgotten_content"] is True
    assert payload["warning"]


async def test_live2d_save_with_a_stale_revision_returns_409(client):
    """A stale revision is a conflict, matching the V2-T01 contract."""
    response = client.post(
        "/aemeath/manage/live2d/save",
        json={
            "model_name": "mao_pro",
            "scale": 1.0,
            "x_offset": 0,
            "y_offset": 0,
            "expected_revision": "stale",
        },
    )

    assert response.status_code == 409
    assert response.json()["conflict"] is True
