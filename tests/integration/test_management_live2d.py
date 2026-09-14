"""V2-T02 production-entry tests: Live2D configuration and preview.

The governing rule for this page is honesty about absence. There is no official
Aemeath Live2D model yet, and the page has to stay usable anyway: a missing
model must produce a stated, actionable error — never a blank page that looks
broken (issue #15, acceptance A6). A bundled placeholder is a placeholder and
must be labelled as one, not presented as her real model (V2-08).

The page reads and writes the same ``model_dict.json`` and
``character_config.live2d_model_name`` the renderer and the runtime use, so what
it shows is what the character window will load.

Acceptance criteria covered (issue #15 / implementation-plan V2-T02):

A6 with no Live2D model the page still configures and shows a clear error
"""

from __future__ import annotations

import json

import pytest

from tests.integration.harness import build_config_document

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def live2d_env(tmp_path, monkeypatch):
    """An isolated config plus model directory with one real model."""
    from aemeath import config as config_module

    model_root = tmp_path / "live2d-models"
    model_root.mkdir(parents=True, exist_ok=True)

    # One complete model, so "the model exists" is a real check.
    model_dir = model_root / "mao_pro" / "runtime"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "mao_pro.model3.json").write_text(
        json.dumps({"Version": 3, "FileReferences": {}}), encoding="utf-8"
    )

    model_dict_path = tmp_path / "model_dict.json"
    model_dict_path.write_text(
        json.dumps(
            [
                {
                    "name": "mao_pro",
                    "description": "",
                    "url": "/live2d-models/mao_pro/runtime/mao_pro.model3.json",
                    "kScale": 0.5,
                    "initialXshift": 0,
                    "initialYshift": 0,
                    "kXOffset": 1150,
                    "idleMotionGroupName": "Idle",
                }
            ]
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    config_path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    # The config is JSON here (valid YAML), so write it as YAML for realism.
    import yaml

    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    class Env:
        path = config_path
        models = model_root
        model_dict = model_dict_path

        def document(self):
            """The config document as it currently exists on disk."""
            import yaml as _yaml

            return _yaml.safe_load(config_path.read_text(encoding="utf-8"))

        def model_dict_data(self):
            """The model dictionary as it currently exists on disk."""
            return json.loads(model_dict_path.read_text(encoding="utf-8"))

    return Env()


# ----------------------------------------------------------------------
# A6 — a missing model must not blank the page
# ----------------------------------------------------------------------


async def test_overview_reports_models_and_the_active_one(live2d_env):
    """The page lists discoverable models and marks the active one."""
    from aemeath.management.live2d import Live2DService

    service = Live2DService(live2d_env.path, models_root=live2d_env.models)
    overview = service.overview()

    assert overview["ok"] is True
    names = [model["name"] for model in overview["models"]]
    assert "mao_pro" in names
    assert overview["active_model"] == "mao_pro"


async def test_missing_model_directory_is_reported_not_left_blank(tmp_path, monkeypatch):
    """No model directory at all produces a stated error, not an empty page.

    This is the acceptance criterion: the page has to remain usable and explain
    what is missing. An empty list with no explanation would look like a broken
    page rather than a missing asset.
    """
    from aemeath import config as config_module
    from aemeath.management.live2d import Live2DService

    import yaml

    config_path = tmp_path / "conf.aemeath.yaml"
    document = build_config_document(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))

    absent = tmp_path / "no-such-models"
    service = Live2DService(config_path, models_root=absent)
    overview = service.overview()

    assert overview["ok"] is False
    assert overview["error"], "a missing model root must be explained"
    assert "模型" in overview["error"]
    # The page can still show and edit the configured name, so the user is not
    # locked out of configuration just because the assets are absent.
    assert overview["active_model"]
    assert isinstance(overview["models"], list)


async def test_configured_model_that_does_not_exist_is_flagged(live2d_env):
    """A configured model with no assets behind it is reported as missing."""
    import yaml

    from aemeath.management.live2d import Live2DService

    document = live2d_env.document()
    document["character_config"]["live2d_model_name"] = "not-installed"
    live2d_env.path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    service = Live2DService(live2d_env.path, models_root=live2d_env.models)
    overview = service.overview()

    assert overview["active_model"] == "not-installed"
    assert overview["active_model_available"] is False
    assert overview["error"], "an unavailable configured model must be reported"


async def test_empty_model_directory_is_reported_as_no_models(live2d_env):
    """A model root that exists but holds nothing says so explicitly."""
    from aemeath.management.live2d import Live2DService

    empty = live2d_env.models.parent / "empty-models"
    empty.mkdir(parents=True, exist_ok=True)

    service = Live2DService(live2d_env.path, models_root=empty)
    overview = service.overview()

    assert overview["models"] == []
    assert overview["error"], "an empty model root must be explained"
    assert "没有" in overview["error"] or "未找到" in overview["error"]


# ----------------------------------------------------------------------
# Placeholder honesty (V2-08)
# ----------------------------------------------------------------------


async def test_the_bundled_model_is_marked_as_a_placeholder(live2d_env):
    """The model in use is labelled a placeholder, not the official asset.

    Aemeath's real Live2D model has not been made yet. Presenting the bundled
    one as her official model would misstate what the user is looking at.
    """
    from aemeath.management.live2d import Live2DService

    service = Live2DService(live2d_env.path, models_root=live2d_env.models)
    overview = service.overview()

    active = [m for m in overview["models"] if m["name"] == overview["active_model"]]
    assert active, "the active model must appear in the list"
    assert active[0]["is_official_model"] is False
    assert active[0]["label"]


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


async def test_saving_scale_and_position_updates_the_model_dictionary(live2d_env):
    """Proportion and position changes are written where the renderer reads."""
    import yaml

    from aemeath.management.live2d import Live2DService

    # The config must be YAML for the service to parse it.
    document = live2d_env.document()
    live2d_env.path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    service = Live2DService(
        live2d_env.path,
        models_root=live2d_env.models,
        model_dict_path=live2d_env.model_dict,
    )
    result = await service.save(
        model_name="mao_pro",
        scale=1.25,
        x_offset=100,
        y_offset=-50,
        expected_revision=service.revision(),
    )

    assert result["ok"] is True, result
    entry = live2d_env.model_dict_data()[0]
    assert entry["kScale"] == 1.25
    assert entry["kXOffset"] == 100
    assert entry["initialYshift"] == -50


async def test_saving_selects_the_model_in_the_authoritative_config(live2d_env):
    """Choosing a model writes ``live2d_model_name`` in the real config."""
    from aemeath.management.live2d import Live2DService

    service = Live2DService(
        live2d_env.path,
        models_root=live2d_env.models,
        model_dict_path=live2d_env.model_dict,
    )
    result = await service.save(
        model_name="mao_pro",
        scale=0.5,
        x_offset=1150,
        y_offset=0,
        expected_revision=service.revision(),
    )

    assert result["ok"] is True, result
    assert live2d_env.document()["character_config"]["live2d_model_name"] == "mao_pro"
    assert result["restart_required"] is True


async def test_saving_an_unknown_model_is_refused(live2d_env):
    """A model that is not installed cannot be selected."""
    from aemeath.management.live2d import Live2DService

    service = Live2DService(
        live2d_env.path,
        models_root=live2d_env.models,
        model_dict_path=live2d_env.model_dict,
    )
    before = live2d_env.model_dict.read_bytes()

    result = await service.save(
        model_name="not-installed",
        scale=1.0,
        x_offset=0,
        y_offset=0,
        expected_revision=service.revision(),
    )

    assert result["ok"] is False
    assert result["error"]
    assert live2d_env.model_dict.read_bytes() == before


async def test_saving_with_a_stale_revision_conflicts(live2d_env):
    """A save based on an outdated revision does not overwrite the file."""
    from aemeath.management.live2d import Live2DService

    service = Live2DService(
        live2d_env.path,
        models_root=live2d_env.models,
        model_dict_path=live2d_env.model_dict,
    )
    before = live2d_env.model_dict.read_bytes()

    result = await service.save(
        model_name="mao_pro",
        scale=2.0,
        x_offset=0,
        y_offset=0,
        expected_revision="a-revision-from-long-ago",
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert live2d_env.model_dict.read_bytes() == before


async def test_a_failed_save_leaves_the_model_dictionary_intact(live2d_env, monkeypatch):
    """A write failure leaves the previous dictionary byte-identical."""
    import os

    from aemeath.management.live2d import Live2DService

    service = Live2DService(
        live2d_env.path,
        models_root=live2d_env.models,
        model_dict_path=live2d_env.model_dict,
    )
    before = live2d_env.model_dict.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    result = await service.save(
        model_name="mao_pro",
        scale=1.5,
        x_offset=10,
        y_offset=10,
        expected_revision=service.revision(),
    )

    assert result["ok"] is False
    assert live2d_env.model_dict.read_bytes() == before
