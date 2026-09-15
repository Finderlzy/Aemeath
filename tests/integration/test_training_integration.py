"""Training-orchestration regressions (V2-T05).

These tests pin the *contract* the training wizard (V2-T06) will build on,
without running a real training job. They exist because the upstream training
path has properties that are cheap to get wrong and expensive to discover
during a 30-minute real run:

* upstream launches training as ``Popen(cmd, shell=True)`` of a **generated
  temporary config**. A naive adapter that re-derives the command will silently
  diverge from what upstream actually executes.
* cancelling must kill **only** the process this application started. Upstream
  ships ``kill_process`` as ``taskkill /t /f /pid``, which walks the process
  tree — pointed at the wrong PID it terminates the user's own GPT-SoVITS
  service.
* training refuses to start unless every preprocessed artefact exists. The
  adapter must report *which* one is missing, not fail with an opaque upstream
  error.
* retry is only sound for some failures; retrying a bad-material failure just
  burns another training run.

The tests use a fake upstream tree and a fake process handle, so they need
neither GPT-SoVITS nor a GPU.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import gpt_sovits as training  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stand-in for ``subprocess.Popen``.

    Records the command it was handed and lets a test drive the exit status.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_upstream(tmp_path: Path) -> Path:
    """Build a minimal tree shaped like a GPT-SoVITS checkout.

    Only the files the adapter actually inspects are created.
    """
    root = tmp_path / "GPT-SoVITS"
    for relative in (
        "GPT_SoVITS/s1_train.py",
        "GPT_SoVITS/s2_train.py",
        "GPT_SoVITS/configs/s2.json",
        "GPT_SoVITS/prepare_datasets/1-get-text.py",
        "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
        "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# upstream placeholder\n", encoding="utf-8")

    (root / "GPT_SoVITS" / "configs" / "s2.json").write_text(
        json.dumps({"train": {"epochs": 100}, "data": {}, "model": {}}),
        encoding="utf-8",
    )
    for name in (
        "SoVITS_weights",
        "SoVITS_weights_v2",
        "GPT_weights",
        "GPT_weights_v2",
        "TEMP",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Upstream fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_reports_missing_install(tmp_path: Path) -> None:
    """A nonexistent install is reported, not crashed on."""
    fingerprint = training.fingerprint_upstream(tmp_path / "nope")

    assert fingerprint["available"] is False
    assert fingerprint["missing"]


def test_fingerprint_reads_real_version_and_paths(fake_upstream: Path) -> None:
    """The fingerprint exposes the fixed version and the paths it will use."""
    fingerprint = training.fingerprint_upstream(fake_upstream)

    assert fingerprint["available"] is True
    assert fingerprint["root"] == str(fake_upstream)
    # Every script the pipeline depends on is checked, not assumed.
    assert fingerprint["missing"] == []
    assert fingerprint["weight_dirs"]["sovits"]["v2"] == "SoVITS_weights_v2"
    assert fingerprint["weight_dirs"]["gpt"]["v2"] == "GPT_weights_v2"


def test_fingerprint_detects_an_incomplete_install(fake_upstream: Path) -> None:
    """A half-cloned checkout must fail loudly rather than at training time."""
    (fake_upstream / "GPT_SoVITS" / "s2_train.py").unlink()

    fingerprint = training.fingerprint_upstream(fake_upstream)

    assert fingerprint["available"] is False
    assert any("s2_train.py" in item for item in fingerprint["missing"])


# ---------------------------------------------------------------------------
# Material validation
# ---------------------------------------------------------------------------


def test_missing_material_is_named(tmp_path: Path) -> None:
    """Every absent piece of material is listed so the wizard can point at it."""
    problems = training.validate_material(
        audio_dir=tmp_path / "no-such-audio",
        list_path=tmp_path / "no-such.list",
    )

    assert len(problems) == 2
    assert any("no-such-audio" in item for item in problems)
    assert any("no-such.list" in item for item in problems)


def test_material_with_too_little_audio_is_flagged(tmp_path: Path) -> None:
    """A single short clip cannot train a voice; say so before spending a run."""
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "one.wav").write_bytes(b"RIFF")
    listing = tmp_path / "list.list"
    listing.write_text("one.wav|Speaker|ZH|你好\n", encoding="utf-8")

    problems = training.validate_material(audio_dir=audio, list_path=listing)

    assert problems
    assert any("素材" in item or "条" in item for item in problems)


def test_valid_material_reports_no_problem(tmp_path: Path) -> None:
    """A complete listing with real audio passes validation."""
    audio = tmp_path / "audio"
    audio.mkdir()
    lines = []
    for index in range(5):
        name = f"clip{index}.wav"
        (audio / name).write_bytes(b"RIFF")
        lines.append(f"{name}|Speaker|ZH|测试文本{index}")
    listing = tmp_path / "list.list"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    problems = training.validate_material(
        audio_dir=audio, list_path=listing, min_clips=5
    )

    assert problems == []


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_preprocess_command_uses_upstream_env_contract(fake_upstream: Path) -> None:
    """Upstream reads its inputs from the environment, not argv.

    ``1-get-text.py`` takes ``inp_text`` / ``inp_wav_dir`` / ``exp_name`` /
    ``opt_dir`` from ``os.environ``. Passing them as arguments would run the
    script with everything unset.
    """
    spec = training.build_preprocess_spec(
        upstream_root=fake_upstream,
        step="1-get-text",
        exp_name="aemeath_v2",
        list_path=Path("E:/materials/voices.list"),
        audio_dir=Path("E:/materials"),
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
    )

    assert spec.env["inp_text"]
    assert spec.env["inp_wav_dir"]
    assert spec.env["exp_name"] == "aemeath_v2"
    assert spec.env["version"] == "v2"
    assert "1-get-text.py" in " ".join(spec.command)


def test_preprocess_requires_every_produced_artefact(fake_upstream: Path) -> None:
    """The expected outputs are known in advance, so readiness is checkable."""
    spec = training.build_preprocess_spec(
        upstream_root=fake_upstream,
        step="2-get-hubert-wav32k",
        exp_name="aemeath_v2",
        list_path=Path("E:/materials/voices.list"),
        audio_dir=Path("E:/materials"),
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
    )

    assert spec.outputs
    assert any("4-cnhubert" in str(path) for path in spec.outputs)


def test_preprocess_supplies_the_semantic_config_path(fake_upstream: Path) -> None:
    """``3-get-semantic.py`` opens ``s2config_path`` directly.

    Without it the script dies with ``TypeError: expected str, bytes or
    os.PathLike object, not NoneType`` -- observed on the real install during
    this task. The WebUI passes the value in, so a command-line caller must too.
    """
    spec = training.build_preprocess_spec(
        upstream_root=fake_upstream,
        step="3-get-semantic",
        exp_name="aemeath_v2",
        list_path=Path("E:/materials/voices.list"),
        audio_dir=Path("E:/materials"),
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
    )

    assert spec.env["s2config_path"]
    assert spec.env["s2config_path"].endswith("s2.json")
    assert spec.env["pretrained_s2G"]


def test_phoneme_parts_are_merged_into_the_expected_name(fake_upstream: Path) -> None:
    """The merged ``2-name2text.txt`` is produced by the WebUI, not by a script.

    ``1-get-text.py`` writes only ``2-name2text-0.txt``; the merged file the
    trainer requires is created by ``webui.py``. Reproducing that merge is what
    makes a command-line run equivalent to clicking through the WebUI.
    """
    opt_dir = fake_upstream / "logs" / "aemeath_v2"
    opt_dir.mkdir(parents=True, exist_ok=True)
    (opt_dir / "2-name2text-0.txt").write_text(
        "a.wav\tphones\tword2ph\t文本一\nb.wav\tphones\tword2ph\t文本二\n",
        encoding="utf-8",
    )

    merged = training.merge_phoneme_files(opt_dir=opt_dir)

    assert merged.name == "2-name2text.txt"
    assert len(merged.read_text(encoding="utf-8").strip().splitlines()) == 2
    # Upstream deletes the part file after merging, and so must we: leaving it
    # would let a rerun append the same lines twice.
    assert not (opt_dir / "2-name2text-0.txt").exists()

    merged_again = training.merge_phoneme_files(opt_dir=opt_dir)
    assert len(merged_again.read_text(encoding="utf-8").strip().splitlines()) == 0


def test_sovits_train_command_targets_s2_script(fake_upstream: Path, tmp_path: Path) -> None:
    """The SoVITS stage runs ``s2_train.py`` against a generated config."""
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=2,
        batch_size=1,
        work_dir=tmp_path,
    )

    assert "s2_train.py" in " ".join(spec.command)
    assert spec.config_path is not None
    assert spec.outputs


def test_generated_config_carries_experiment_paths(
    fake_upstream: Path, tmp_path: Path
) -> None:
    """The temporary config must point training at *our* experiment dirs.

    Upstream writes this file into a shared ``TEMP/`` directory, so the paths
    inside it are the only thing that keeps two experiments apart.
    """
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=3,
        batch_size=2,
        work_dir=tmp_path,
    )
    config = json.loads(Path(spec.config_path).read_text(encoding="utf-8"))

    assert config["train"]["epochs"] == 3
    assert config["train"]["batch_size"] == 2
    assert "aemeath_v2" in config["data"]["exp_dir"]
    assert "SoVITS_weights_v2" in config["save_weight_dir"]


def test_generated_config_supplies_pretrained_weights(
    fake_upstream: Path, tmp_path: Path
) -> None:
    """``s2_train.py`` reads ``pretrained_s2G``/``s2D`` off the config directly.

    Neither key is in the JSON template -- upstream's WebUI injects them. This
    was found the hard way: without them the run dies with ``AttributeError:
    'HParams' object has no attribute 'pretrained_s2G'`` after the dataset has
    already loaded, i.e. a minute into a real run with no useful error.
    """
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    config = json.loads(Path(spec.config_path).read_text(encoding="utf-8"))

    assert config["train"]["pretrained_s2G"]
    assert config["train"]["pretrained_s2D"]
    assert "s2G2333k.pth" in config["train"]["pretrained_s2G"]


def test_pretrained_paths_match_the_real_layout(fake_upstream: Path) -> None:
    """The v2 pretrained weights live in ``gsv-v2final-pretrained``."""
    paths = training.pretrained_sovits_paths(upstream_root=fake_upstream, version="v2")

    assert paths["s2G"].name == "s2G2333k.pth"
    assert paths["s2D"].name == "s2D2333k.pth"
    assert "gsv-v2final-pretrained" in str(paths["s2G"])

    v1 = training.pretrained_sovits_paths(upstream_root=fake_upstream, version="v1")
    assert "gsv-v2final-pretrained" not in str(v1["s2G"])


def test_training_refuses_when_artefacts_are_absent(fake_upstream: Path, tmp_path: Path) -> None:
    """Upstream would reject this anyway; fail first with a usable message."""
    opt_dir = fake_upstream / "logs" / "aemeath_v2"
    opt_dir.mkdir(parents=True, exist_ok=True)

    problems = training.check_training_inputs(opt_dir=opt_dir, version="v2")

    assert problems
    assert any("2-name2text" in item for item in problems)


def test_training_inputs_pass_when_all_artefacts_exist(
    fake_upstream: Path, tmp_path: Path
) -> None:
    """All five upstream artefacts present means the run may start."""
    opt_dir = fake_upstream / "logs" / "aemeath_v2"
    opt_dir.mkdir(parents=True, exist_ok=True)
    (opt_dir / "2-name2text.txt").write_text("", encoding="utf-8")
    (opt_dir / "6-name2semantic.tsv").write_text("", encoding="utf-8")
    for name in ("3-bert", "4-cnhubert", "5-wav32k"):
        (opt_dir / name).mkdir(parents=True, exist_ok=True)

    assert training.check_training_inputs(opt_dir=opt_dir, version="v2") == []


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------


def test_launch_records_the_started_pid(fake_upstream: Path, tmp_path: Path) -> None:
    """The PID is the handle the canceller needs; losing it strands the job."""
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-1", exp_name="aemeath_v2", version="v2")

    job.launch(spec, popen=lambda *a, **k: FakeProcess(pid=777))

    assert job.pid == 777
    assert job.status == training.TrainingStatus.RUNNING


def test_cancel_kills_only_the_started_process(fake_upstream: Path, tmp_path: Path) -> None:
    """Cancellation must not reach beyond the process we started.

    Upstream's ``kill_process`` runs ``taskkill /t /f``, which terminates the
    whole tree. That is the right reach for a tree we own and the wrong reach
    for anything else on the machine — notably the user's own TTS service.
    """
    process = FakeProcess(pid=888)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-2", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)

    killed: list[int] = []
    ok = job.cancel(taskkill=lambda pid: killed.append(pid))

    assert ok is True
    assert killed == [888]
    assert job.status == training.TrainingStatus.CANCELLED


def test_cancel_is_a_noop_without_a_started_job() -> None:
    """Cancelling something never started must not target an arbitrary PID."""
    job = training.TrainingJob(job_id="job-3", exp_name="x", version="v2")

    killed: list[int] = []

    assert job.cancel(taskkill=lambda pid: killed.append(pid)) is False
    assert killed == []


def test_cancel_is_not_repeatable(fake_upstream: Path, tmp_path: Path) -> None:
    """A finished job must not send a second kill to a recycled PID."""
    process = FakeProcess(pid=999)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-4", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)

    killed: list[int] = []
    assert job.cancel(taskkill=lambda pid: killed.append(pid)) is True
    assert job.cancel(taskkill=lambda pid: killed.append(pid)) is False
    assert killed == [999]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_follows_the_process_exit_code(fake_upstream: Path, tmp_path: Path) -> None:
    """Success and failure are read from the process, not guessed.

    A zero exit code only counts once it is backed by a weight file; see
    ``test_success_without_artefacts_is_not_success`` for the other half of this
    rule.
    """
    process = FakeProcess(pid=1000)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-5", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)

    assert job.refresh() == training.TrainingStatus.RUNNING

    (fake_upstream / "SoVITS_weights_v2" / "aemeath_v2_e1_s1.pth").write_bytes(b"w")
    process.returncode = 0
    assert job.refresh() == training.TrainingStatus.SUCCEEDED

    process.returncode = 1
    job.status = training.TrainingStatus.RUNNING
    assert job.refresh() == training.TrainingStatus.FAILED


def test_success_without_artefacts_is_not_success(fake_upstream: Path, tmp_path: Path) -> None:
    """Exit code 0 with no weights on disk is a failure, not a pass.

    Upstream can exit 0 having written nothing usable. Reporting success there
    would let the wizard apply a voice that does not exist.
    """
    process = FakeProcess(pid=1001)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-6", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)

    process.returncode = 0
    job.refresh()

    assert job.status == training.TrainingStatus.FAILED
    assert "产物" in job.detail


def test_success_with_artefacts_is_reported(fake_upstream: Path, tmp_path: Path) -> None:
    """A produced weight file is what makes the run a real success."""
    process = FakeProcess(pid=1002)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    weight = fake_upstream / "SoVITS_weights_v2" / "aemeath_v2_e1_s100.pth"
    weight.write_bytes(b"weights")

    job = training.TrainingJob(job_id="job-7", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)
    process.returncode = 0
    job.refresh()

    assert job.status == training.TrainingStatus.SUCCEEDED
    assert job.artifacts


# ---------------------------------------------------------------------------
# Retry boundaries
# ---------------------------------------------------------------------------


def test_retry_allowed_after_an_infrastructure_failure(fake_upstream: Path, tmp_path: Path) -> None:
    """A killed or crashed run may simply be started again."""
    process = FakeProcess(pid=1003)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-8", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)
    process.returncode = 1
    job.refresh()

    verdict = job.retry_verdict()

    assert verdict["retryable"] is True


def test_retry_refused_while_running(fake_upstream: Path, tmp_path: Path) -> None:
    """Two concurrent runs would collide in upstream's shared TEMP config."""
    process = FakeProcess(pid=1004)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-9", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)

    verdict = job.retry_verdict()

    assert verdict["retryable"] is False
    assert verdict["reason"]


def test_retry_refused_when_material_is_bad(fake_upstream: Path, tmp_path: Path) -> None:
    """Retrying bad material only burns another run; the wizard must fix input."""
    process = FakeProcess(pid=1005)
    spec = training.build_sovits_train_spec(
        upstream_root=fake_upstream,
        exp_name="aemeath_v2",
        opt_dir=fake_upstream / "logs" / "aemeath_v2",
        version="v2",
        python_exec="python.exe",
        epochs=1,
        batch_size=1,
        work_dir=tmp_path,
    )
    job = training.TrainingJob(job_id="job-10", exp_name="aemeath_v2", version="v2")
    job.launch(spec, popen=lambda *a, **k: process)
    process.returncode = 1
    job.failure_kind = "material"
    job.refresh()

    verdict = job.retry_verdict()

    assert verdict["retryable"] is False
