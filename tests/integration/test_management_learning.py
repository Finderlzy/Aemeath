"""V2-T04 production-entry tests: the learning management surface.

The service-level suites prove the learning behaviour; these prove the *surface*
the two pages actually talk to:

* the routes are registered on the real FastAPI application, through the same
  ``install_management_routes`` the upstream patch calls — a route that is
  defined but never mounted is caught here rather than in the browser;
* every write goes through the same loopback guard as the V2-T01/V2-T02 pages;
* the status codes the frontend branches on are the ones that come back;
* the page can distinguish "nothing learned yet" from "learning is not running";
* answers never carry forgotten text.

The client is the real ``TestClient`` over an isolated database; only the model
providers are substituted.
"""

from __future__ import annotations

import pytest
import yaml

pytestmark = pytest.mark.asyncio

BASE = "/aemeath/manage"


@pytest.fixture
def learning_client(tmp_path, monkeypatch):
    """A real FastAPI client with the management routes and a live runtime.

    The learning adapter is injected, so the routes resolve the same service a
    conversation would consult rather than a second, disconnected one.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath import config as config_module
    from aemeath.adapters import FakeLearningAdapter, LearningCandidate
    from aemeath.config import load_config
    from aemeath.management.routes import install_management_routes
    from aemeath.runtime import build_runtime, reset_runtime
    from tests.integration.harness import build_config_document

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    document["character_config"]["aemeath_config"]["learning"] = {"enabled": True}
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    adapter = FakeLearningAdapter(
        [
            LearningCandidate(
                kind="jargon", content="冒烟测试", meaning="快速验证主流程"
            ),
            LearningCandidate(
                kind="expression", content="先放着，回头再说", scenario="想推迟时"
            ),
        ]
    )

    reset_runtime()
    runtime = build_runtime(config=load_config(config_path), learning=adapter)
    import aemeath.runtime as runtime_module

    runtime_module._runtime = runtime

    app = FastAPI()
    install_management_routes(app, config_path)

    async def loopback_app(scope, receive, send):
        """Present a loopback peer address to the application."""
        if scope.get("type") == "http":
            scope = {**scope, "client": ("127.0.0.1", 54321)}
        await app(scope, receive, send)

    with TestClient(loopback_app) as client:
        client.runtime = runtime
        client.learning_adapter = adapter
        client.config_path = config_path
        yield client

    reset_runtime()


async def seed_items(client, tmp_path) -> list:
    """Learn two items through the real service, as a conversation would."""
    runtime = client.runtime
    service = runtime.learning
    user_id = runtime.memory.record_user_message(
        "这个先冒烟测试一下。先放着，回头再说。", source="user_text"
    )
    service.queue_turn([user_id])
    await service.run_pending_learning(runtime.persona_text())
    return service.store.list_items()


# ----------------------------------------------------------------------
# Reachability and shape
# ----------------------------------------------------------------------


async def test_learning_overview_is_reachable_and_filtered_by_kind(
    learning_client, tmp_path
) -> None:
    """Both pages read the same endpoint, filtered by ``kind``."""
    await seed_items(learning_client, tmp_path)

    jargon = learning_client.get(f"{BASE}/learning/overview?kind=jargon")
    assert jargon.status_code == 200
    payload = jargon.json()
    assert payload["ok"] is True
    assert payload["available"] is True
    assert payload["kind"] == "jargon"
    assert [item["content"] for item in payload["items"]] == ["冒烟测试"]

    expression = learning_client.get(f"{BASE}/learning/overview?kind=expression")
    assert [i["content"] for i in expression.json()["items"]] == ["先放着，回头再说"]


async def test_learning_overview_distinguishes_empty_from_not_running(
    learning_client,
) -> None:
    """With nothing learned the read still succeeds, and says learning is on.

    "No items" and "learning is not running" need different user actions, so the
    payload carries ``available`` separately from the item list.
    """
    response = learning_client.get(f"{BASE}/learning/overview?kind=expression")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["items"] == []
    assert payload["available"] is True
    assert payload["enabled"] is True
    assert payload["error"] == ""
    assert payload["counts"]["total"] == 0


async def test_learning_disabled_reports_why_not_an_empty_list(
    tmp_path, monkeypatch
) -> None:
    """With learning switched off the page gets a reason, not a silent empty list."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath import config as config_module
    from aemeath.config import load_config
    from aemeath.management.routes import install_management_routes
    from aemeath.runtime import build_runtime, reset_runtime
    from tests.integration.harness import build_config_document

    config_path = tmp_path / "conf.off.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    # No learning block: the default is off, and no adapter is injected.
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    reset_runtime()
    runtime = build_runtime(config=load_config(config_path))
    import aemeath.runtime as runtime_module

    runtime_module._runtime = runtime

    app = FastAPI()
    install_management_routes(app, config_path)

    async def loopback_app(scope, receive, send):
        """Present a loopback peer address to the application."""
        if scope.get("type") == "http":
            scope = {**scope, "client": ("127.0.0.1", 54321)}
        await app(scope, receive, send)

    try:
        with TestClient(loopback_app) as client:
            response = client.get(f"{BASE}/learning/overview?kind=expression")
            payload = response.json()
    finally:
        reset_runtime()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["items"] == []
    assert payload["available"] is False
    assert payload["enabled"] is False
    assert "关闭" in payload["error"], "必须说明为什么没有学习结果"


async def test_learning_items_expose_source_and_status(learning_client, tmp_path) -> None:
    """Every item carries its provenance, status and check result."""
    await seed_items(learning_client, tmp_path)
    payload = learning_client.get(f"{BASE}/learning/overview").json()

    for item in payload["items"]:
        assert item["status"] in ("candidate", "enabled", "disabled", "revoked")
        assert item["sources"], "条目必须带来源，界面要显示来源与语境"
        assert item["sources"][0]["message_id"]
        assert item["sources"][0]["content"], "来源原文必须可读"
        assert "check_result" in item


async def test_learning_sources_endpoint_answers(learning_client, tmp_path) -> None:
    """The dedicated sources endpoint answers for a real item and 200 with a reason for a bad id."""
    items = await seed_items(learning_client, tmp_path)
    item_id = items[0].item_id

    response = learning_client.get(f"{BASE}/learning/{item_id}/sources")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["sources"]

    missing = learning_client.get(f"{BASE}/learning/does-not-exist/sources")
    assert missing.status_code == 200
    assert missing.json()["ok"] is False
    assert "不存在" in missing.json()["error"]


# ----------------------------------------------------------------------
# Actions and the status codes the frontend branches on
# ----------------------------------------------------------------------


async def test_toggle_disables_and_re_enables(learning_client, tmp_path) -> None:
    """Disable and restore round-trip through the API."""
    items = await seed_items(learning_client, tmp_path)
    item_id = items[0].item_id

    off = learning_client.post(
        f"{BASE}/learning/toggle", json={"item_id": item_id, "enabled": False}
    )
    assert off.status_code == 200
    assert off.json()["ok"] is True
    assert off.json()["status"] == "disabled"

    on = learning_client.post(
        f"{BASE}/learning/toggle", json={"item_id": item_id, "enabled": True}
    )
    assert on.json()["status"] == "enabled"


async def test_toggle_requires_an_explicit_enabled_value(
    learning_client, tmp_path
) -> None:
    """An omitted ``enabled`` is a 422, never a guessed default.

    Guessing here would either silently enable something the user asked to
    switch off, or silently do nothing while the page shows a success.
    """
    items = await seed_items(learning_client, tmp_path)
    response = learning_client.post(
        f"{BASE}/learning/toggle", json={"item_id": items[0].item_id}
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["ok"] is False
    assert "enabled" in payload["error"]


async def test_revoke_is_terminal_and_reported(learning_client, tmp_path) -> None:
    """Revoking reports the terminal status, and re-enabling is refused."""
    items = await seed_items(learning_client, tmp_path)
    item_id = [
        item.item_id for item in items if item.content == "冒烟测试"
    ][0]

    revoked = learning_client.post(f"{BASE}/learning/revoke", json={"item_id": item_id})
    assert revoked.status_code == 200
    assert revoked.json()["ok"] is True
    assert revoked.json()["status"] == "revoked"

    refused = learning_client.post(
        f"{BASE}/learning/toggle", json={"item_id": item_id, "enabled": True}
    )
    assert refused.status_code == 422
    assert "撤销" in refused.json()["error"]


async def test_edit_takes_priority_and_conflicts_are_409(
    learning_client, tmp_path
) -> None:
    """A manual edit succeeds; a stale revision comes back as a 409."""
    items = await seed_items(learning_client, tmp_path)
    item_id = items[0].item_id
    stale_revision = items[0].revision

    first = learning_client.post(
        f"{BASE}/learning/edit",
        json={"item_id": item_id, "meaning": "用户改过的含义"},
    )
    assert first.status_code == 200
    assert first.json()["ok"] is True

    stale = learning_client.post(
        f"{BASE}/learning/edit",
        json={
            "item_id": item_id,
            "meaning": "并发修改",
            "expected_revision": stale_revision,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["conflict"] is True


async def test_edit_rejects_an_empty_content(learning_client, tmp_path) -> None:
    """Blank content is a 422 and leaves the entry alone."""
    items = await seed_items(learning_client, tmp_path)
    response = learning_client.post(
        f"{BASE}/learning/edit", json={"item_id": items[0].item_id, "content": "   "}
    )
    assert response.status_code == 422
    assert response.json()["ok"] is False


async def test_ambiguity_can_be_settled_through_the_api(
    learning_client, tmp_path
) -> None:
    """A word with an unsettled meaning is flagged, then settled via the API."""
    runtime = learning_client.runtime
    from aemeath.adapters import LearningCandidate

    runtime.learning._adapter.candidates = [
        LearningCandidate(kind="jargon", content="上岛", meaning="含义不明确")
    ]
    user_id = runtime.memory.record_user_message("明天上岛。", source="user_text")
    runtime.learning.queue_turn([user_id])
    await runtime.learning.run_pending_learning(runtime.persona_text())

    listing = learning_client.get(f"{BASE}/learning/overview?kind=jargon").json()
    item = [i for i in listing["items"] if i["content"] == "上岛"][0]
    assert item["ambiguous"] is True, "含义不明确的词必须在界面上标识出来"

    settled = learning_client.post(
        f"{BASE}/learning/meanings",
        json={
            "item_id": item["item_id"],
            "meanings": [{"meaning": "去岛上那家咖啡馆", "scenario": "约见面时"}],
        },
    )
    assert settled.status_code == 200
    assert settled.json()["ok"] is True

    refreshed = learning_client.get(f"{BASE}/learning/overview?kind=jargon").json()
    updated = [i for i in refreshed["items"] if i["content"] == "上岛"][0]
    assert updated["ambiguous"] is False
    assert updated["meanings"][0]["meaning"] == "去岛上那家咖啡馆"


async def test_ambiguity_requires_at_least_one_meaning(learning_client, tmp_path) -> None:
    """An empty meanings list is a 422 rather than a silent no-op."""
    items = await seed_items(learning_client, tmp_path)
    response = learning_client.post(
        f"{BASE}/learning/meanings",
        json={"item_id": items[0].item_id, "meanings": []},
    )
    assert response.status_code == 422
    assert response.json()["ok"] is False


async def test_status_filter_narrows_the_list(learning_client, tmp_path) -> None:
    """The status filter the pages offer actually filters."""
    items = await seed_items(learning_client, tmp_path)
    target = items[0].item_id
    learning_client.post(
        f"{BASE}/learning/toggle", json={"item_id": target, "enabled": False}
    )

    disabled = learning_client.get(
        f"{BASE}/learning/overview?status=disabled"
    ).json()
    assert [i["item_id"] for i in disabled["items"]] == [target]

    enabled = learning_client.get(f"{BASE}/learning/overview?status=enabled").json()
    assert target not in [i["item_id"] for i in enabled["items"]]


# ----------------------------------------------------------------------
# Security and privacy of the new surface
# ----------------------------------------------------------------------


async def test_every_learning_write_refuses_a_non_loopback_origin(
    learning_client,
) -> None:
    """A foreign origin cannot reach any learning write endpoint."""
    headers = {"Origin": "http://evil.example.com"}
    cases = [
        (f"{BASE}/learning/edit", {"item_id": "x", "meaning": "y"}),
        (f"{BASE}/learning/toggle", {"item_id": "x", "enabled": False}),
        (f"{BASE}/learning/revoke", {"item_id": "x"}),
        (f"{BASE}/learning/meanings", {"item_id": "x", "meanings": [{"meaning": "y"}]}),
    ]

    for path, body in cases:
        response = learning_client.post(path, json=body, headers=headers)
        assert response.status_code == 403, f"{path} accepted a foreign origin"


async def test_learning_overview_refuses_a_non_loopback_origin(
    learning_client,
) -> None:
    """The read path is guarded too: it exposes what was learned from private chat."""
    response = learning_client.get(
        f"{BASE}/learning/overview", headers={"Origin": "http://evil.example.com"}
    )
    assert response.status_code == 403


async def test_forgotten_source_text_is_not_returned_by_the_api(
    learning_client, tmp_path
) -> None:
    """After forgetting the source, the API does not hand its text back.

    The entry is removed with it, so the content must not reappear through the
    list, the sources endpoint or any other field.
    """
    runtime = learning_client.runtime
    from aemeath.adapters import LearningCandidate

    secret = "别提了（项目代号：黑曜石）"
    runtime.learning._adapter.candidates = [
        LearningCandidate(kind="expression", content="别提了")
    ]
    user_id = runtime.memory.record_user_message(secret, source="user_text")
    runtime.learning.queue_turn([user_id])
    await runtime.learning.run_pending_learning(runtime.persona_text())

    before = learning_client.get(f"{BASE}/learning/overview").json()
    assert any(i["content"] == "别提了" for i in before["items"])

    runtime.memory.store.forget_by_message(user_id)

    after = learning_client.get(f"{BASE}/learning/overview").json()
    assert not any(i["content"] == "别提了" for i in after["items"])

    dumped = yaml.safe_dump(after, allow_unicode=True)
    assert "黑曜石" not in dumped, "接口不得返回已遗忘的正文"
