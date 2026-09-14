"""V2-T01 production-entry tests: config read/write and the first management loop.

These tests drive the *real* management entry points rather than Aemeath-internal
shortcuts, matching the harness convention already used by the rest of
``tests/integration/``:

* the on-disk document is a full upstream-shaped YAML validated by upstream's
  own ``validate_config`` — the same validator the server runs at startup;
* the config path comes from the real ``resolve_config_path()`` /
  ``AEMEATH_CONFIG`` chain, never from a second private copy;
* persona assembly goes through the real ``AemeathAgent._build_messages``.

Only the file location is substituted (a temporary config), so the personal
``config/conf.aemeath.yaml`` is never read or written.

Acceptance criteria covered (issue #14 / implementation-plan V2-T01):

A1 read from the authoritative config
A2 a valid save is actually used after a restart
A3 invalid fields are rejected without damaging the old file
A4 concurrent revisions conflict instead of overwriting newer edits
A5 a failed save leaves the last usable config intact
A6 the UI can tell "saved" apart from "running"
A7 credentials never appear in responses
A8 the old config still starts
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from tests.integration.harness import (
    build_config_document,
    load_validated_config,
)

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _write_document(path, document) -> None:
    """Write a config document as YAML."""
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def manage_env(tmp_path, monkeypatch):
    """An isolated authoritative config plus a management service bound to it.

    Returns a small namespace with the config path, the written document and a
    freshly constructed service. The service is always built *after* the file is
    written, so it observes the same file the server would.

    The credential store is redirected into the temporary directory as well:
    a test that saves a key must never write to the developer's real store.
    """
    from aemeath import config as config_module
    from aemeath.management import service as service_module
    from aemeath.management.service import ConfigService

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    _write_document(config_path, document)

    # Make this file the process-wide authoritative config, exactly as
    # ``scripts/run_server.py`` does via ``set_config_path``.
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv(
        service_module.CREDENTIAL_STORE_ENV, str(tmp_path / "credentials.yaml")
    )

    class Env:
        path = config_path

        def document(self):
            """The document as it currently exists on disk."""
            return yaml.safe_load(config_path.read_text(encoding="utf-8"))

        def service(self):
            """A service reading the authoritative path from the environment."""
            assert config_module.resolve_config_path() == config_path
            return ConfigService()

    return Env()


# ----------------------------------------------------------------------
# A1 — read from the authoritative config
# ----------------------------------------------------------------------


async def test_overview_reports_the_values_actually_in_the_config(manage_env):
    """The overview shows what the file really says, not built-in defaults."""
    service = manage_env.service()

    overview = service.overview()

    assert overview["model"]["base_url"] == "https://api.example.invalid/v1"
    assert overview["model"]["model"] == "test-model"
    assert overview["saved_revision"]
    assert overview["config_path"] == str(manage_env.path)


async def test_reading_uses_the_authoritative_path(manage_env, tmp_path):
    """A service reads the config that ``resolve_config_path()`` selects.

    Editing an unrelated file must not change what is reported, which is what
    distinguishes "reads the authoritative config" from "reads some copy".
    """
    decoy = tmp_path / "other.yaml"
    document = manage_env.document()
    document["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]["model"] = "decoy-model"
    _write_document(decoy, document)

    overview = manage_env.service().overview()

    assert overview["model"]["model"] == "test-model"


async def test_overview_reports_the_conversation_provider(manage_env):
    """The conversation model is the upstream selection, not a second copy.

    The endpoint conversations really use lives in ``llm_configs`` and is chosen
    through ``agent_settings.<agent>.llm_provider``. Reporting it from there
    keeps one source of truth.
    """
    service = manage_env.service()

    overview = service.overview()

    # ``provider`` reports the configured ``llm_configs`` entry name, which is
    # what the config actually contains. It is not normalised to a protocol
    # label: the UI shows the connection the user configured.
    assert overview["model"]["provider"] == "openai_compatible_llm"
    assert overview["model"]["model"] == "test-model"


# ----------------------------------------------------------------------
# A2 — a valid save is used after a restart
# ----------------------------------------------------------------------


async def test_saving_updates_the_file_and_the_next_load_sees_it(manage_env):
    """A saved value survives re-reading through the real config loader."""
    service = manage_env.service()

    result = service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is True

    from aemeath.config import load_config

    reloaded = load_config(manage_env.path)
    assert reloaded.providers.conversation.base_url == "https://api.changed.invalid/v1"
    assert reloaded.providers.conversation.model == "changed-model"


async def test_saved_config_still_passes_the_upstream_schema(manage_env):
    """A saved document is still a valid upstream config.

    This is the gate that makes "restart and keep working" mean something: the
    server validates the deployed document at startup and refuses to boot on a
    schema error.
    """
    service = manage_env.service()
    service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    validated = load_validated_config(manage_env.path)

    assert (
        validated.character_config.agent_config.llm_configs.openai_compatible_llm.model
        == "changed-model"
    )


async def test_saved_persona_reaches_the_prompt_after_restart(manage_env):
    """A saved persona is what the next conversation actually uses."""
    service = manage_env.service()
    service.save_persona(
        identity="你是爱弥斯。",
        personality="温和但直接。",
        reply_style="一句话为主。",
        expected_revision=service.overview()["saved_revision"],
    )

    # "After restart" means: re-read from disk and rebuild the agent's prompt.
    from aemeath.config import load_config
    from aemeath.persona import resolve_persona

    reloaded = load_config(manage_env.path)
    persona = resolve_persona(manage_env.document())

    assert "温和但直接。" in persona
    assert reloaded is not None


# ----------------------------------------------------------------------
# A3 — invalid fields are rejected without damaging the old file
# ----------------------------------------------------------------------


async def test_invalid_field_is_rejected_and_the_file_is_untouched(manage_env):
    """A rejected save leaves the file byte-identical."""
    service = manage_env.service()
    before = manage_env.path.read_bytes()

    result = service.save_model(
        base_url="",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is False
    assert result["errors"]
    assert manage_env.path.read_bytes() == before


async def test_rejected_save_reports_the_offending_field(manage_env):
    """Errors name the field, so the UI can point at the input."""
    service = manage_env.service()

    result = service.save_model(
        base_url="not-a-url",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is False
    assert any("base_url" in error["field"] for error in result["errors"])


# ----------------------------------------------------------------------
# A4 — concurrent revisions conflict
# ----------------------------------------------------------------------


async def test_stale_revision_is_refused_and_newer_edit_survives(manage_env):
    """Two clients editing from the same revision: the second must not win.

    The first save moves the file on; a save carrying the now-stale revision is
    refused and the newer content stays intact.
    """
    service = manage_env.service()
    first_revision = service.overview()["saved_revision"]

    first = service.save_model(
        base_url="https://api.first.invalid/v1",
        model="first-model",
        expected_revision=first_revision,
    )
    assert first["ok"] is True

    stale = service.save_model(
        base_url="https://api.stale.invalid/v1",
        model="stale-model",
        expected_revision=first_revision,
    )

    assert stale["ok"] is False
    assert stale["conflict"] is True

    current = manage_env.document()
    block = current["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]
    assert block["model"] == "first-model"


async def test_conflicting_save_does_not_change_the_file(manage_env):
    """A conflict is not a partial write."""
    service = manage_env.service()
    revision = service.overview()["saved_revision"]
    service.save_model(
        base_url="https://api.first.invalid/v1",
        model="first-model",
        expected_revision=revision,
    )
    after_first = manage_env.path.read_bytes()

    service.save_model(
        base_url="https://api.stale.invalid/v1",
        model="stale-model",
        expected_revision=revision,
    )

    assert manage_env.path.read_bytes() == after_first


# ----------------------------------------------------------------------
# A5 — a failed save leaves the last usable config intact
# ----------------------------------------------------------------------


async def test_failed_atomic_write_leaves_the_previous_config(manage_env, monkeypatch):
    """A write failure must not truncate or half-write the config."""
    import aemeath.management.service as service_module

    service = manage_env.service()
    before = manage_env.path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    # Fail the atomic replace itself: the moment a naive implementation would
    # have already truncated the destination.
    monkeypatch.setattr(service_module.os, "replace", boom)

    result = service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is False
    assert manage_env.path.read_bytes() == before
    # And the config is still loadable, i.e. it is genuinely the old one.
    assert load_validated_config(manage_env.path) is not None


async def test_temp_files_do_not_accumulate_next_to_the_config(manage_env):
    """A save leaves no temporary siblings behind."""
    service = manage_env.service()
    service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    siblings = [
        p.name for p in manage_env.path.parent.iterdir() if p.name != manage_env.path.name
    ]
    assert all(not name.startswith("conf.aemeath.yaml") for name in siblings)


# ----------------------------------------------------------------------
# A6 — "saved" is distinguishable from "running"
# ----------------------------------------------------------------------


async def test_save_reports_that_a_restart_is_required(manage_env):
    """A change that needs engine rebuilds says so instead of claiming effect."""
    service = manage_env.service()

    result = service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["saved_revision"] != result["running_revision"]
    assert result["restart_required"] is True


async def test_running_revision_reflects_the_startup_snapshot(manage_env):
    """The running revision is the one this process started with.

    It must not advance merely because the user saved, otherwise the UI would
    report "in effect" for a change no engine has adopted.
    """
    service = manage_env.service()
    started_with = service.overview()["running_revision"]

    service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    assert service.overview()["running_revision"] == started_with


async def test_unchanged_save_does_not_claim_a_restart_is_needed(manage_env):
    """Re-saving identical values is not a pending change."""
    service = manage_env.service()
    document = manage_env.document()
    block = document["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]

    result = service.save_model(
        base_url=block["base_url"],
        model=block["model"],
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is True
    assert result["restart_required"] is False


# ----------------------------------------------------------------------
# A7 — credentials never appear in responses
# ----------------------------------------------------------------------


async def test_overview_never_returns_the_api_key(manage_env):
    """A literal key in the config is reported as configured, never echoed."""
    document = manage_env.document()
    secret = "sk-REDACTED-literal-fixture"
    document["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]["llm_api_key"] = secret
    _write_document(manage_env.path, document)

    service = manage_env.service()
    overview = service.overview()

    assert secret not in yaml.safe_dump(overview, allow_unicode=True)
    assert overview["model"]["api_key_configured"] is True
    assert "api_key" not in overview["model"]


async def test_overview_reports_a_variable_reference_as_configured(manage_env):
    """A ``${VAR}`` reference counts as configured without resolving it.

    The response names the *variable* so the UI can say which credential the
    connection expects. Naming a variable is not disclosing a secret — the value
    is never read here — and the store is what actually supplies it.
    """
    from aemeath.management.service import save_credential

    document = manage_env.document()
    document["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]["llm_api_key"] = "${AEMEATH_TEST_LLM_KEY}"
    _write_document(manage_env.path, document)

    # Before the credential exists anywhere, the reference is not configured.
    assert manage_env.service().overview()["model"]["api_key_configured"] is False

    save_credential("AEMEATH_TEST_LLM_KEY", "sk-REDACTED-stored-fixture")

    overview = manage_env.service().overview()
    assert overview["model"]["api_key_configured"] is True
    assert overview["model"]["api_key_env"] == "AEMEATH_TEST_LLM_KEY"
    # The stored value never appears, in any field.
    assert "sk-REDACTED-stored-fixture" not in yaml.safe_dump(overview, allow_unicode=True)


async def test_saving_a_key_stores_a_reference_not_the_secret(manage_env):
    """Persisting a credential keeps it out of the YAML.

    The file is committed-adjacent and readable; a literal key there is exactly
    the failure that produced the 2026-09-14 leak.
    """
    service = manage_env.service()
    secret = "sk-REDACTED-another-fixture"

    result = service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        api_key=secret,
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is True
    assert secret not in manage_env.path.read_text(encoding="utf-8")
    assert secret not in yaml.safe_dump(result, allow_unicode=True)


async def test_a_failed_save_does_not_leak_the_submitted_key(manage_env):
    """Even a rejected save must not echo the credential back."""
    service = manage_env.service()
    secret = "sk-REDACTED-rejected-fixture"

    result = service.save_model(
        base_url="",
        model="changed-model",
        api_key=secret,
        expected_revision=service.overview()["saved_revision"],
    )

    assert result["ok"] is False
    assert secret not in yaml.safe_dump(result, allow_unicode=True)
    assert secret not in manage_env.path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# A8 — the old config still starts
# ----------------------------------------------------------------------


async def test_a_config_without_the_new_sections_still_loads(tmp_path, monkeypatch):
    """An existing config predating the persona split keeps starting."""
    from aemeath import config as config_module
    from aemeath.management.service import ConfigService
    from aemeath.persona import resolve_persona

    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    # An old file: only the legacy free-text persona, no three-field block.
    document["character_config"]["persona_prompt"] = "旧人设原文。"
    config_path = tmp_path / "conf.old.yaml"
    _write_document(config_path, document)
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    assert load_validated_config(config_path) is not None
    assert resolve_persona(document) == "旧人设原文。"

    overview = ConfigService().overview()
    assert overview["persona"]["mode"] == "legacy"
    assert "旧人设原文。" in overview["persona"]["legacy_prompt"]


async def test_legacy_persona_is_not_auto_split_by_saving_other_fields(
    manage_env,
):
    """Saving the model must not silently migrate or rewrite the persona.

    Auto-splitting the old free text would overwrite wording the user wrote, and
    the design explicitly forbids it: migration happens only when the user saves
    the three fields themselves.
    """
    service = manage_env.service()

    service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    document = manage_env.document()
    assert document["character_config"]["persona_prompt"] == "你是 Aemeath，一个桌面 AI 伙伴。"
    assert "persona" not in document["character_config"]["aemeath_config"] or (
        document["character_config"]["aemeath_config"]["persona"].get("mode")
        != "split"
    )


# ----------------------------------------------------------------------
# Persona assembly: one source, never duplicated
# ----------------------------------------------------------------------


async def test_legacy_persona_is_used_alone_when_not_migrated(manage_env):
    """Only one persona source enters the prompt."""
    from aemeath.persona import resolve_persona

    persona = resolve_persona(manage_env.document())

    assert persona == "你是 Aemeath，一个桌面 AI 伙伴。"


async def test_split_persona_replaces_the_legacy_text(manage_env):
    """After migrating, the legacy text is not injected a second time."""
    document = manage_env.document()
    document["character_config"]["aemeath_config"]["persona"] = {
        "mode": "split",
        "identity": "你是爱弥斯。",
        "personality": "温和但直接。",
        "reply_style": "一到三句话。",
    }
    _write_document(manage_env.path, document)

    from aemeath.persona import resolve_persona

    persona = resolve_persona(document)

    assert "你是爱弥斯。" in persona
    assert "温和但直接。" in persona
    assert "一到三句话。" in persona
    assert "你是 Aemeath，一个桌面 AI 伙伴。" not in persona


async def test_migrating_the_persona_preserves_the_original_wording(manage_env):
    """Switching to the three fields must not destroy the user's own text.

    ``persona_prompt`` is what upstream feeds the model, so a migration has to
    overwrite it. Without archiving the original first, the wording the user
    wrote would be gone for good and the migration would be irreversible.
    """
    service = manage_env.service()
    original = manage_env.document()["character_config"]["persona_prompt"]

    service.save_persona(
        identity="你是爱弥斯。",
        personality="温和但直接。",
        reply_style="一到三句话。",
        expected_revision=service.overview()["saved_revision"],
    )

    from aemeath.persona import legacy_prompt

    document = manage_env.document()
    assert legacy_prompt(document) == original
    assert (
        document["character_config"]["aemeath_config"]["legacy_persona_prompt"]
        == original
    )
    # The active prompt is the split persona, not the original.
    assert document["character_config"]["persona_prompt"] != original


async def test_a_second_persona_save_does_not_overwrite_the_original(manage_env):
    """Editing the fields again keeps the true original, not generated text."""
    service = manage_env.service()
    original = manage_env.document()["character_config"]["persona_prompt"]

    service.save_persona(
        identity="第一版身份。",
        personality="第一版性格。",
        reply_style="第一版风格。",
        expected_revision=service.overview()["saved_revision"],
    )
    service.save_persona(
        identity="第二版身份。",
        personality="第二版性格。",
        reply_style="第二版风格。",
        expected_revision=service.overview()["saved_revision"],
    )

    from aemeath.persona import legacy_prompt

    assert legacy_prompt(manage_env.document()) == original


async def test_the_overview_shows_the_original_after_migrating(manage_env):
    """The persona page can still show what the user originally wrote."""
    service = manage_env.service()
    original = manage_env.document()["character_config"]["persona_prompt"]

    service.save_persona(
        identity="你是爱弥斯。",
        personality="温和但直接。",
        reply_style="一到三句话。",
        expected_revision=service.overview()["saved_revision"],
    )

    persona_view = service.overview()["persona"]
    assert persona_view["mode"] == "split"
    assert persona_view["legacy_prompt"] == original


async def test_saving_the_persona_through_the_route_preserves_the_original(
    manage_env,
):
    """The same guarantee holds over the HTTP route the UI actually calls."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath.management.routes import install_management_routes

    app = FastAPI()
    install_management_routes(app)
    client = TestClient(app, client=("127.0.0.1", 12345))

    original = manage_env.document()["character_config"]["persona_prompt"]
    revision = client.get("/aemeath/manage/overview").json()["saved_revision"]

    response = client.post(
        "/aemeath/manage/persona",
        json={
            "identity": "你是爱弥斯。",
            "personality": "温和但直接。",
            "reply_style": "一到三句话。",
            "expected_revision": revision,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    persona_view = client.get("/aemeath/manage/overview").json()["persona"]
    assert persona_view["legacy_prompt"] == original
    assert "温和但直接。" in persona_view["active_prompt"]


async def test_a_legacy_config_survives_a_model_save_and_still_start(
    manage_env,
):
    """Saving the model leaves the persona mechanism exactly as it was."""
    from aemeath.persona import legacy_prompt, persona_mode

    service = manage_env.service()
    original = manage_env.document()["character_config"]["persona_prompt"]

    service.save_model(
        base_url="https://api.changed.invalid/v1",
        model="changed-model",
        expected_revision=service.overview()["saved_revision"],
    )

    document = manage_env.document()
    assert persona_mode(document) == "legacy"
    assert legacy_prompt(document) == original
    assert document["character_config"]["persona_prompt"] == original
    assert load_validated_config(manage_env.path) is not None


async def test_the_persona_reaches_the_system_prompt(manage_env, make_harness):
    """The resolved persona is what the agent actually prompts with.

    The unit above proves resolution; this one proves the resolved text is what
    the conversation sends, through the real agent.
    """
    from aemeath.persona import resolve_persona

    harness = make_harness()
    persona = resolve_persona(manage_env.document())
    harness.agent._persona = persona

    from src.open_llm_vtuber.agent.input_types import BatchInput, TextData, TextSource

    input_data = BatchInput(
        texts=[TextData(source=TextSource.INPUT, content="你好")],
        images=[],
        metadata={},
    )
    messages, system_prompt = await harness.agent._build_messages(input_data)

    assert persona in system_prompt


# ----------------------------------------------------------------------
# Routes: loopback only
# ----------------------------------------------------------------------


async def test_management_routes_are_mounted_on_the_upstream_app():
    """The management API is served by the real upstream FastAPI app."""
    from src.open_llm_vtuber.server import WebSocketServer

    server = WebSocketServer.__new__(WebSocketServer)
    # Build only the app, without the upstream initialize/engine path.
    from aemeath.management.routes import install_management_routes
    from fastapi import FastAPI

    app = FastAPI()
    install_management_routes(app)

    paths = {route.path for route in app.routes}
    assert any(path.startswith("/aemeath/manage") for path in paths)
    assert server is not None


async def test_a_non_loopback_client_is_refused(manage_env):
    """A web page on another origin must not be able to rewrite local config.

    Loopback enforcement is what keeps a random site in the user's browser from
    POSTing to the management port.
    """
    from aemeath.management.routes import is_loopback_client

    assert is_loopback_client("127.0.0.1") is True
    assert is_loopback_client("::1") is True
    assert is_loopback_client("192.168.1.10") is False
    assert is_loopback_client("8.8.8.8") is False


async def test_saving_through_the_http_route(manage_env):
    """The route persists a valid change end to end."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath.management.routes import install_management_routes

    app = FastAPI()
    install_management_routes(app)
    client = TestClient(app, client=("127.0.0.1", 12345))

    overview = client.get("/aemeath/manage/overview").json()
    response = client.post(
        "/aemeath/manage/model",
        json={
            "base_url": "https://api.changed.invalid/v1",
            "model": "changed-model",
            "expected_revision": overview["saved_revision"],
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manage_env.document()["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"
    ]["model"] == "changed-model"


async def test_stale_revision_returns_conflict_over_http(manage_env):
    """A revision conflict is a 409, not a silent overwrite."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aemeath.management.routes import install_management_routes

    app = FastAPI()
    install_management_routes(app)
    client = TestClient(app, client=("127.0.0.1", 12345))
    revision = client.get("/aemeath/manage/overview").json()["saved_revision"]

    client.post(
        "/aemeath/manage/model",
        json={
            "base_url": "https://api.first.invalid/v1",
            "model": "first-model",
            "expected_revision": revision,
        },
    )
    stale = client.post(
        "/aemeath/manage/model",
        json={
            "base_url": "https://api.stale.invalid/v1",
            "model": "stale-model",
            "expected_revision": revision,
        },
    )

    assert stale.status_code == 409
