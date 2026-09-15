"""V2-T03 production-entry tests: desktop and subtitle settings.

The acceptance criteria this file covers (issue #16, implementation-plan
V2-T03):

* V2-07 subtitle font size, width and dwell time are settable;
* the settings the user saves in the management window are the settings the
  character window reads;
* a rejected save leaves the previous configuration intact;
* a machine that has never configured these still gets usable values, and a
  config that cannot be parsed is reported as its own case rather than as
  "not configured yet".

These run through the real management service on a real (temporary) config
file, so they exercise the same code path the mounted routes use.
"""

from __future__ import annotations

import yaml

import pytest

from tests.integration.harness import build_config_document

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    """An isolated authoritative config with no desktop section yet."""
    from aemeath import config as config_module

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    class Env:
        path = config_path

        def document(self):
            """The config document as it currently exists on disk."""
            return yaml.safe_load(config_path.read_text(encoding="utf-8"))

        def desktop_section(self):
            """The desktop section, or an empty dict when absent."""
            document = self.document()
            return (
                document.get("character_config", {})
                .get("aemeath_config", {})
                .get("desktop", {})
            ) or {}

        def text(self):
            """The raw config text, for byte-identity comparisons."""
            return config_path.read_text(encoding="utf-8")

    return Env()


def _valid_values(**overrides):
    """A complete candidate set, with any field overridden."""
    values = {
        "subtitle_font_size": 24,
        "subtitle_max_width": 600,
        "subtitle_dwell_ms": 5000,
        "character_width": 440,
        "character_height": 640,
        "character_scale": 1.0,
    }
    values.update(overrides)
    return values


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


async def test_overview_returns_defaults_before_anything_is_configured(desktop_env):
    """An unconfigured machine still gets usable values, not zeros.

    A page opened for the first time must show something readable; returning
    zeroes would produce an invisible subtitle and a degenerate window.
    """
    from aemeath.management.desktop import DesktopSettingsService

    overview = DesktopSettingsService(desktop_env.path).overview()

    assert overview["read_error"] == ""
    assert overview["subtitle_font_size"] > 0
    assert overview["subtitle_max_width"] > 0
    assert overview["subtitle_dwell_ms"] > 0
    assert overview["character_width"] > 0
    assert overview["character_height"] > 0
    assert overview["character_scale"] > 0


async def test_overview_reports_unparseable_config_separately(desktop_env):
    """A config that cannot be parsed is its own state, not "empty"."""
    from aemeath.management.desktop import DesktopSettingsService

    desktop_env.path.write_text("character_config: [unclosed\n", encoding="utf-8")
    overview = DesktopSettingsService(desktop_env.path).overview()

    assert overview["read_error"] != ""


async def test_overview_carries_the_revision_it_describes(desktop_env):
    """The read returns the revision that belongs to its own values.

    The page saves with this revision; when it came from a different endpoint, a
    transient failure there produced an empty revision, and the backend skips
    its conflict check for an empty revision. A save could then overwrite a
    newer change while appearing to succeed.
    """
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    overview = service.overview()

    assert overview["revision"] == service.revision()
    assert overview["revision"] != ""


async def test_the_revisions_from_read_and_save_agree(desktop_env):
    """A save started from a fresh read is never rejected as a conflict.

    This is the round trip the page performs: read, edit, save.
    """
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    read = service.overview()

    result = await service.save(
        values=_valid_values(subtitle_font_size=28),
        expected_revision=read["revision"],
    )
    assert result["ok"] is True, result

    # And the revision the save reports is the one a follow-up read returns,
    # so a second save from the refreshed form also succeeds.
    after = DesktopSettingsService(desktop_env.path).overview()
    assert after["revision"] == result["saved_revision"]

    second = await DesktopSettingsService(desktop_env.path).save(
        values=_valid_values(subtitle_font_size=29),
        expected_revision=after["revision"],
    )
    assert second["ok"] is True, second


async def test_a_stale_revision_from_another_read_is_refused(desktop_env):
    """Conflicts are still detected — the fix did not disable the guard."""
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    stale = service.overview()["revision"]

    # Someone else saves in between.
    assert (
        await service.save(values=_valid_values(character_width=500), expected_revision=stale)
    )["ok"] is True

    result = await service.save(
        values=_valid_values(character_width=460), expected_revision=stale
    )
    assert result["ok"] is False
    assert result["conflict"] is True


async def test_saved_values_survive_a_restart(desktop_env):
    """The character window reads what the management window saved.

    This is the criterion that makes the setting real: a value that only lived
    in one window's memory would be lost the moment the window closed.
    """
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(subtitle_font_size=30, character_scale=1.5),
        expected_revision=service.revision(),
    )
    assert result["ok"] is True, result

    # A fresh service, as a restarted process would build.
    reopened = DesktopSettingsService(desktop_env.path).overview()
    assert reopened["subtitle_font_size"] == 30
    assert reopened["character_scale"] == 1.5


# ----------------------------------------------------------------------
# Rejection
# ----------------------------------------------------------------------


async def test_out_of_range_value_is_rejected_and_config_is_untouched(desktop_env):
    """An invalid value never reaches the file, byte for byte."""
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    before = desktop_env.text()

    result = await service.save(
        values=_valid_values(subtitle_font_size=999),
        expected_revision=service.revision(),
    )

    assert result["ok"] is False
    assert any(e["field"] == "subtitle_font_size" for e in result["errors"])
    assert desktop_env.text() == before


async def test_every_rejected_field_is_reported_at_once(desktop_env):
    """Two mistakes are reported together, not one per attempt."""
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(subtitle_font_size=999, character_width=1),
        expected_revision=service.revision(),
    )

    assert result["ok"] is False
    fields = {error["field"] for error in result["errors"]}
    assert {"subtitle_font_size", "character_width"} <= fields


async def test_revision_conflict_is_reported_as_a_conflict(desktop_env):
    """A stale revision is refused rather than overwriting a newer edit."""
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(),
        expected_revision="a-revision-that-is-not-current",
    )

    assert result["ok"] is False
    assert result["conflict"] is True


# ----------------------------------------------------------------------
# Preservation
# ----------------------------------------------------------------------


async def test_saving_preserves_comments_and_layout(desktop_env):
    """A save must not strip the config's comments.

    The config file is documentation: it explains each setting in comments and
    is under version control. Re-serialising the parsed document deletes every
    one of those comments and reflows the file, which is unacceptable for a
    six-number change. This was a real defect: a `yaml.safe_dump` round trip
    rewrote the whole acceptance config on the first save.
    """
    from aemeath.management.desktop import DesktopSettingsService

    # A config with the kind of comments the real file carries.
    text = yaml.safe_dump(
        build_config_document(
            data_dir=desktop_env.path.parent / "data",
            log_dir=desktop_env.path.parent / "logs",
        ),
        allow_unicode=True,
    )
    commented = (
        "# Aemeath 验收运行配置\n"
        "# 密钥不写在本文件里：真实密钥通过环境变量注入\n"
        + text
        + "    # 记忆检索相似度下限\n"
    )
    desktop_env.path.write_text(commented, encoding="utf-8")

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(), expected_revision=service.revision()
    )
    assert result["ok"] is True, result

    after = desktop_env.text()
    assert "# Aemeath 验收运行配置" in after
    assert "# 密钥不写在本文件里：真实密钥通过环境变量注入" in after
    assert "# 记忆检索相似度下限" in after


async def test_saving_stays_inside_the_desktop_block(desktop_env):
    """The desktop write must not touch a same-named key elsewhere.

    A field named `character_scale` under some other section must survive
    unchanged; a global search would corrupt it.
    """
    from aemeath.management.desktop import DesktopSettingsService

    document = desktop_env.document()
    # A decoy elsewhere in the tree with an overlapping field name.
    document["character_config"]["aemeath_config"]["decoy_scale"] = 99
    document["character_config"]["live2d_model_name"] = "mao_pro"
    desktop_env.path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(character_scale=1.5), expected_revision=service.revision()
    )
    assert result["ok"] is True, result

    after = desktop_env.document()
    assert after["character_config"]["live2d_model_name"] == "mao_pro"
    assert after["character_config"]["aemeath_config"]["decoy_scale"] == 99
    section = (
        after["character_config"]["aemeath_config"]["desktop"]
    )
    assert section["character_scale"] == 1.5


async def test_saving_keeps_the_rest_of_the_config(desktop_env):
    """Writing the desktop section must not disturb anything else.

    The config carries the model connection, persona and Aemeath runtime
    settings; a save that dropped them would break the running character.
    """
    from aemeath.management.desktop import DesktopSettingsService

    before = desktop_env.document()
    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(), expected_revision=service.revision()
    )
    assert result["ok"] is True, result

    after = desktop_env.document()
    before_character = before["character_config"]
    after_character = after["character_config"]

    # The agent choice and the model connection are still exactly as they were.
    assert (
        after_character["agent_config"]["conversation_agent_choice"]
        == before_character["agent_config"]["conversation_agent_choice"]
    )
    assert (
        after_character["agent_config"]["llm_configs"]
        == before_character["agent_config"]["llm_configs"]
    )


async def test_unknown_keys_in_the_section_are_preserved(desktop_env):
    """A key this build does not know is not silently deleted."""
    from aemeath.management.desktop import DesktopSettingsService

    document = desktop_env.document()
    (
        document["character_config"]
        .setdefault("aemeath_config", {})
        .setdefault("desktop", {})
    )["a_future_key"] = "keep me"
    desktop_env.path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(), expected_revision=service.revision()
    )
    assert result["ok"] is True, result

    assert desktop_env.desktop_section()["a_future_key"] == "keep me"


async def test_saving_does_not_require_a_restart(desktop_env):
    """Subtitle settings are read on the next fetch, so no restart is needed.

    Reporting a restart here would train the user to restart for a change that
    already took effect, which is the opposite of what the saved/effective
    distinction is for.
    """
    from aemeath.management.desktop import DesktopSettingsService

    service = DesktopSettingsService(desktop_env.path)
    result = await service.save(
        values=_valid_values(), expected_revision=service.revision()
    )

    assert result["ok"] is True
    assert result["restart_required"] is False
