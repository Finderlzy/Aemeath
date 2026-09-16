"""Contract tests against the *real* local GPT-SoVITS checkout (V2-T05).

These tests read the pinned install at ``E:/WorkSpace/Tools/GPT-SoVITS`` and
assert that the assumptions :mod:`aemeath.training.gpt_sovits` is built on are
still true of the code on disk. They are the reason an upstream upgrade fails
here rather than in the middle of a training run.

Everything they assert was first read out of the checkout by hand:

* training is launched as a subprocess running ``s2_train.py`` with a generated
  config — there is no importable training API;
* the preprocessing scripts take their inputs from ``os.environ``;
* ``s2_train.py`` refuses to run unless five named artefacts exist;
* cancellation upstream is ``taskkill /t /f /pid``;
* the generated config is written to a shared ``TEMP/`` directory.

The tests skip when the checkout is absent, so the suite still runs on a
machine without GPT-SoVITS installed. They never start training.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import gpt_sovits as training  # noqa: E402

UPSTREAM_ROOT = training.resolve_upstream_root()

pytestmark = pytest.mark.skipif(
    not (UPSTREAM_ROOT / "GPT_SoVITS" / "s2_train.py").exists(),
    reason=f"local GPT-SoVITS checkout not present at {UPSTREAM_ROOT}",
)


def read(relative: str) -> str:
    """Read a file from the checkout.

    Args:
        relative: Path relative to the checkout root.

    Returns:
        The file's text.
    """
    return (UPSTREAM_ROOT / relative).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The checkout is usable and versioned
# ---------------------------------------------------------------------------


def test_checkout_is_complete() -> None:
    """Every script the adapter invokes exists in the real checkout."""
    fingerprint = training.fingerprint_upstream()

    assert fingerprint["available"] is True, fingerprint["missing"]
    assert fingerprint["missing"] == []


def test_checkout_revision_is_recorded() -> None:
    """A verification result is only meaningful against a known revision."""
    revision = training.fingerprint_upstream()["revision"]

    assert revision["commit"], revision
    assert len(revision["commit"]) >= 7


# ---------------------------------------------------------------------------
# Training is a subprocess, not an API
# ---------------------------------------------------------------------------


def test_training_has_no_importable_entry_point() -> None:
    """``s2_train.py`` is a script with a ``main()`` guard, not a library.

    If upstream ever exposes a callable API this test should be revisited --
    but until then, orchestration has to be process control.
    """
    source = read("GPT_SoVITS/s2_train.py")

    assert "__main__" in source
    assert re.search(r"^\s*main\(\)\s*$", source, re.MULTILINE)


def test_upstream_builds_a_command_string_for_s2_training() -> None:
    """The WebUI runs ``s2_train.py --config <tmp json>`` via ``Popen``."""
    webui = read("webui.py")

    assert "s2_train.py" in webui
    assert re.search(r"Popen\(cmd", webui), "upstream no longer launches training with Popen"


def test_upstream_writes_the_generated_config_to_shared_temp() -> None:
    """The temporary config path is shared, which is why runs cannot overlap."""
    webui = read("webui.py")

    assert "tmp_s2.json" in webui
    assert re.search(r'tmp\s*=\s*os\.path\.join\(now_dir,\s*"TEMP"\)', webui), (
        "upstream no longer writes the generated config into TEMP/"
    )


def test_s2_train_reads_its_config_from_argv() -> None:
    """``--config`` is parsed by ``utils.get_hparams``, which ``s2_train`` calls.

    The flag is not handled inside ``s2_train.py`` itself: the script calls
    ``utils.get_hparams(stage=2)`` at import time, and *that* builds the
    argument parser. Anchoring the assertion to ``utils.py`` is what makes it
    meaningful -- asserting on ``s2_train.py`` would fail even though the
    command the adapter builds is correct.
    """
    stage = read("GPT_SoVITS/s2_train.py")
    utils_source = read("GPT_SoVITS/utils.py")

    assert "get_hparams(stage=2)" in stage, "s2_train no longer reads its config via get_hparams"
    assert re.search(r'"-c",\s*\n\s*"--config"', utils_source), (
        "utils.get_hparams no longer accepts --config"
    )


# ---------------------------------------------------------------------------
# Preprocessing reads the environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "GPT_SoVITS/prepare_datasets/1-get-text.py",
        "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
        "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
    ],
)
def test_preprocessing_reads_inputs_from_environment(script: str) -> None:
    """These scripts take their inputs from ``os.environ``, not ``argv``.

    Handing the paths as command-line arguments runs them with every input
    unset -- and the failure is an upstream ``gr.Warning`` that never reaches
    Aemeath, so it would look like a silent hang.
    """
    source = read(script)

    assert re.search(r'os\.environ\.get\("inp_text"\)', source), (
        f"{script} no longer reads inp_text from the environment"
    )
    assert re.search(r'os\.environ\.get\("exp_name"\)', source)
    assert re.search(r'os\.environ\.get\("opt_dir"\)', source)


def test_hubert_stage_resolves_clips_against_inp_wav_dir() -> None:
    """Listing entries are joined to ``inp_wav_dir`` with an absolute fallback.

    :func:`aemeath.training.gpt_sovits._resolve_clip` mirrors this rule; if it
    changes, material validation would disagree with the trainer.
    """
    source = read("GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py")

    assert re.search(r'wav_path\s*=\s*"%s/%s"\s*%\s*\(inp_wav_dir,\s*wav_name\)', source), (
        "clip resolution rule changed; _resolve_clip must be updated to match"
    )


# ---------------------------------------------------------------------------
# Artefact requirements
# ---------------------------------------------------------------------------


def test_training_refuses_without_the_five_artefacts() -> None:
    """Upstream's own gate names exactly the artefacts the adapter checks.

    ``check_for_existance(is_train=True)`` appends these five to the experiment
    directory and refuses the run if any is absent.
    """
    source = (UPSTREAM_ROOT / "tools" / "my_utils.py").read_text(
        encoding="utf-8", errors="replace"
    )

    for artefact in training.REQUIRED_TRAINING_ARTEFACTS:
        assert artefact in source, f"{artefact} is no longer part of upstream's gate"


def test_preprocessing_stages_write_the_expected_names() -> None:
    """Each stage's output names are the ones the next stage and gate expect."""
    assert "2-name2text" in read("GPT_SoVITS/prepare_datasets/1-get-text.py")
    assert "4-cnhubert" in read("GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py")
    assert "5-wav32k" in read("GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py")
    assert "6-name2semantic" in read("GPT_SoVITS/prepare_datasets/3-get-semantic.py")


# ---------------------------------------------------------------------------
# Cancellation reach
# ---------------------------------------------------------------------------


def test_upstream_kills_a_whole_process_tree() -> None:
    """Cancellation upstream is ``taskkill /t /f /pid``.

    ``/t`` terminates the tree, so the PID it is aimed at decides whether we
    stop our own run or the user's TTS service. This is why
    :meth:`TrainingJob.cancel` refuses to act without a recorded PID.
    """
    webui = read("webui.py")

    assert re.search(r"taskkill\s+/t\s+/f\s+/pid", webui), (
        "upstream changed its kill mechanism; re-check the blast radius"
    )


# ---------------------------------------------------------------------------
# Weight output layout
# ---------------------------------------------------------------------------


def test_weight_directories_exist_for_the_supported_versions() -> None:
    """The adapter's version-to-directory map matches the real checkout."""
    config_source = read("config.py")

    for version, directory in training.SOVITS_WEIGHT_DIRS.items():
        assert directory in config_source, f"{directory} is not in upstream config.py"
    assert "SoVITS_weight_version2root" in config_source


def test_s2_config_template_has_the_fields_the_adapter_sets() -> None:
    """The template supplies the model/data shape the adapter builds on.

    Note that several training switches are *added by the caller*, not present
    in the template: upstream's WebUI injects ``if_save_latest`` and friends at
    runtime, and the adapter does the same. Asserting those were in the template
    would have been wrong -- they were read out of ``webui.py`` instead.
    """
    config = json.loads(read("GPT_SoVITS/configs/s2.json"))

    for section in ("train", "data", "model"):
        assert section in config, f"template lost its {section!r} section"
    # Present in the template; the adapter overwrites them.
    for key in ("epochs", "batch_size", "log_interval", "seed"):
        assert key in config["train"], f"template lost train.{key}"

    # Injected at runtime by upstream's WebUI, and by the adapter likewise.
    webui = read("webui.py")
    for key in ("if_save_latest", "if_save_every_weights", "save_every_epoch", "gpu_numbers"):
        assert f'data["train"]["{key}"]' in webui, (
            f"upstream no longer injects train.{key}; the adapter may be diverging"
        )


def test_generated_config_is_accepted_by_the_real_template_shape() -> None:
    """A spec built by the adapter lines up with the real template's keys.

    Building a spec has a side effect: the adapter creates the experiment
    directory and the version's weight directory, exactly as upstream's WebUI
    does. Those are cleaned up here so running the suite never leaves droppings
    inside the user's own GPT-SoVITS checkout.
    """
    import shutil
    import tempfile

    exp_name = "__contract_probe__"
    opt_dir = UPSTREAM_ROOT / "logs" / exp_name
    weight_dir = UPSTREAM_ROOT / training.SOVITS_WEIGHT_DIRS["v2"]

    try:
        with tempfile.TemporaryDirectory() as work_dir:
            spec = training.build_sovits_train_spec(
                upstream_root=UPSTREAM_ROOT,
                exp_name=exp_name,
                opt_dir=opt_dir,
                version="v2",
                epochs=1,
                batch_size=1,
                work_dir=Path(work_dir),
            )
            generated = json.loads(Path(spec.config_path).read_text(encoding="utf-8"))
            template = json.loads(read("GPT_SoVITS/configs/s2.json"))

            # Every key the template defines must survive into the generated file,
            # along with the ones the adapter adds.
            for key in template["train"]:
                assert key in generated["train"], f"generated config lost train.{key}"
            for key in ("if_save_latest", "if_save_every_weights", "save_every_epoch", "gpu_numbers"):
                assert key in generated["train"], f"generated config is missing train.{key}"
            assert generated["train"]["epochs"] == 1
            assert generated["train"]["batch_size"] == 1
            # The experiment paths are what keep two runs apart in the shared TEMP/.
            assert generated["name"] == exp_name
            assert generated["model"]["version"] == "v2"
    finally:
        shutil.rmtree(opt_dir, ignore_errors=True)
        # Only remove the weight directory if this test is what created it.
        if not any(weight_dir.glob("*.pth")):
            shutil.rmtree(weight_dir, ignore_errors=True)
