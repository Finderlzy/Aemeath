"""Wizard orchestration regressions (V2-T06).

The wizard is where the V2-T05 findings stop being *observations* and start
being *enforced*. Every rule below was measured in V2-T05 and would be cheap to
lose in a rewrite:

* **Exit code 0 is not success.** Upstream can finish cleanly having written no
  weight; only a file in the weight directory counts.
* **The DDP patch is a precondition.** The pinned upstream crashes on a single
  Windows GPU unless `s2_train.py` carries the `use_ddp` guard, and the crash is
  not catchable in Python. The precheck therefore has to look at the actual
  script and refuse to start rather than discovering it 30 seconds in.
* **Cancellation is PID-scoped.** `taskkill /t` walks the process tree, so
  cancelling without a PID risks killing the user's own TTS service.
* **Proofreading cannot be automated.** The requirement is explicit that a
  successful ASR pass is not evidence that the material is correct, so the
  service must never mark that step done on the user's behalf.
* **Applying is not training.** A new voice is auditioned first, and a failed
  apply leaves the previous voice byte-for-byte intact.

Nothing here runs real training: the process factory, the clock and the probe
are injected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training import wizard as wizard_module  # noqa: E402
from aemeath.training.jobs import JobState, TrainingJobStore  # noqa: E402
from aemeath.training.wizard import TrainingWizardService  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def make_upstream(root: Path, *, with_ddp_guard: bool = True) -> Path:
    """Build a minimal fake GPT-SoVITS checkout.

    Only the files the wizard actually inspects are created; the point is to
    exercise the *checks*, not to imitate upstream's whole tree.
    """
    (root / "GPT_SoVITS" / "prepare_datasets").mkdir(parents=True)
    (root / "GPT_SoVITS" / "configs").mkdir(parents=True)
    (root / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained").mkdir(
        parents=True
    )

    for script in (
        "GPT_SoVITS/s1_train.py",
        "GPT_SoVITS/s2_train.py",
        "GPT_SoVITS/prepare_datasets/1-get-text.py",
        "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
        "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
    ):
        (root / script).write_text("# stub\n", encoding="utf-8")

    # The guard is the difference between a run that finishes and one that
    # dies with an access violation, so the fixture writes both shapes.
    guard = "use_ddp = n_gpus > 1\n" if with_ddp_guard else "use_ddp = True\n"
    (root / "GPT_SoVITS" / "s2_train.py").write_text(
        "import torch\nn_gpus = torch.cuda.device_count()\n" + guard,
        encoding="utf-8",
    )

    (root / "GPT_SoVITS" / "configs" / "s2.json").write_text(
        '{"train": {}, "data": {}, "model": {}}', encoding="utf-8"
    )
    (root / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained" / "s2G2333k.pth").write_bytes(b"x")
    (root / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained" / "s2D2333k.pth").write_bytes(b"x")
    return root


def make_material(tmp_path: Path, *, clips: int = 4) -> tuple[Path, Path]:
    """Write a small but structurally valid material set.

    Durations are real WAV headers so the same validator the wizard uses can
    read them; the audio itself is silence, which is irrelevant to counting.
    """
    import wave

    audio_dir = tmp_path / "material"
    audio_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for index in range(clips):
        clip = audio_dir / f"clip_{index:02d}.wav"
        with wave.open(str(clip), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000 * 5)  # 5 seconds
        lines.append(f"{clip.name}|Aemeath|ZH|第 {index} 条测试文本。")

    list_path = audio_dir / "transcripts.list"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audio_dir, list_path


@pytest.fixture()
def service(tmp_path: Path) -> TrainingWizardService:
    """A wizard service on throwaway paths with the environment stubbed out."""
    upstream = make_upstream(tmp_path / "upstream")
    audio_dir, list_path = make_material(tmp_path)
    return TrainingWizardService(
        store=TrainingJobStore(tmp_path / "jobs.db"),
        upstream_root=upstream,
        material_dir=audio_dir,
        list_path=list_path,
        probe=lambda: {
            "ready": True,
            "blocking": [],
            "gpu": {"available": True, "name": "Fake", "total_mb": 8192, "used_mb": 100, "free_mb": 8092},
            "torch": {"ok": True, "cuda": True, "torch": "2.11.0", "python": "3.11.16", "device": "Fake"},
            "upstream": {"available": True, "missing": [], "root": str(upstream), "revision": {"commit": "48b1a01", "dirty": True}},
            "service": {"running": True, "url": "http://127.0.0.1:9880", "detail": "HTTP 200"},
            "material": {"problems": [], "audio_dir": str(audio_dir), "list_path": str(list_path)},
            "artifacts": {"problems": [], "opt_dir": str(upstream / "logs" / "aemeath_wizard")},
        },
    )


# ---------------------------------------------------------------------------
# The DDP precondition
# ---------------------------------------------------------------------------


def test_ddp_guard_is_detected_when_present(tmp_path: Path) -> None:
    """A patched checkout reports the guard as satisfied."""
    upstream = make_upstream(tmp_path / "up", with_ddp_guard=True)

    result = wizard_module.check_ddp_guard(upstream)

    assert result["ok"] is True
    assert result["patched"] is True
    assert result["error"] == ""


def test_missing_ddp_guard_blocks_training_with_a_recovery_step(tmp_path: Path) -> None:
    """An unpatched checkout must be refused, with the fix named.

    V2-T05 established that this is not a warning: on a single Windows GPU the
    run *will* die with an uncatchable access violation. Letting it start would
    waste the user's time and tell them nothing.
    """
    upstream = make_upstream(tmp_path / "up", with_ddp_guard=False)

    result = wizard_module.check_ddp_guard(upstream)

    assert result["ok"] is False
    assert result["patched"] is False
    assert "s2-train-single-gpu-ddp.patch" in result["error"]
    assert result["recovery"]


def test_ddp_check_reports_a_missing_script_rather_than_raising(tmp_path: Path) -> None:
    """A checkout without `s2_train.py` is a reported condition, not a crash."""
    upstream = tmp_path / "empty"
    upstream.mkdir()

    result = wizard_module.check_ddp_guard(upstream)

    assert result["ok"] is False
    assert result["error"]


def test_missing_ffmpeg_warns_but_does_not_block(
    service: TrainingWizardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg's absence is reported without refusing to train.

    V2-T05 ran a complete training pass on this machine with no ffmpeg on PATH.
    Treating it as fatal would refuse work that demonstrably succeeds, so it is
    surfaced as a warning with a recovery step and left out of `blocking`.
    """
    monkeypatch.setattr(
        wizard_module,
        "check_ffmpeg",
        lambda: {"available": False, "path": "", "error": "未找到 ffmpeg。", "recovery": "安装 ffmpeg。"},
    )

    result = service.preflight()

    assert result["ok"] is True
    assert "未找到 ffmpeg。" in result["warnings"]
    assert not any("ffmpeg" in item for item in result["blocking"])
    assert any("ffmpeg" in item["problem"] for item in result["recovery"])


# ---------------------------------------------------------------------------
# Material import
# ---------------------------------------------------------------------------


def test_import_material_reports_clip_count_and_duration(service: TrainingWizardService) -> None:
    """The user is told what was found before anything is trained."""
    result = service.import_material(voice_name="测试声音")

    assert result["ok"] is True
    job = service.store.get(result["job_id"])
    assert job is not None
    assert job.steps["import"]["clip_count"] == 4
    assert job.steps["import"]["total_seconds"] > 19


def test_import_rejects_material_below_the_floor(tmp_path: Path) -> None:
    """Too little audio is refused up front, not discovered by a failed run."""
    upstream = make_upstream(tmp_path / "up")
    audio_dir, list_path = make_material(tmp_path, clips=2)
    service = TrainingWizardService(
        store=TrainingJobStore(tmp_path / "jobs.db"),
        upstream_root=upstream,
        material_dir=audio_dir,
        list_path=list_path,
        probe=lambda: {"ready": True, "blocking": []},
    )

    result = service.import_material(voice_name="太短")

    assert result["ok"] is False
    assert "至少" in result["error"]
    assert service.store.list_jobs() == []


def test_import_requires_a_voice_name(service: TrainingWizardService) -> None:
    """An unnamed task would be unpickable in the task list later."""
    result = service.import_material(voice_name="  ")

    assert result["ok"] is False
    assert result["error"]


# ---------------------------------------------------------------------------
# Proofreading cannot be automated
# ---------------------------------------------------------------------------


def test_proofread_payload_lists_every_clip_with_its_transcript(
    service: TrainingWizardService,
) -> None:
    """The user reviews audio and text together; both must be present."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]

    payload = service.proofread_payload(job_id)

    assert payload["ok"] is True
    assert len(payload["clips"]) == 4
    assert payload["clips"][0]["text"]
    assert payload["clips"][0]["path"]
    assert payload["confirmed"] is False


def test_proofread_is_not_completed_without_the_user_confirming(
    service: TrainingWizardService,
) -> None:
    """The step stays open until a human submits it.

    The requirement is explicit: a successful ASR pass is *not* evidence that
    the material is accurate. A service that flipped this step on its own would
    violate that, and there is deliberately no code path that does.
    """
    job_id = service.import_material(voice_name="测试声音")["job_id"]

    job = service.store.get(job_id)
    assert "proofread" not in job.steps

    payload = service.proofread_payload(job_id)
    assert payload["confirmed"] is False
    assert service.store.get(job_id).steps.get("proofread") is None


def test_proofread_submission_writes_corrected_text_and_closes_the_step(
    service: TrainingWizardService,
) -> None:
    """What the user typed is what the training set will contain."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    payload = service.proofread_payload(job_id)
    clips = payload["clips"]
    corrected = [{"path": clip["path"], "text": "用户改过的文本。"} for clip in clips]

    result = service.submit_proofread(job_id, corrected)

    assert result["ok"] is True
    job = service.store.get(job_id)
    assert job.steps["proofread"]["done"] is True
    written = service.list_path.read_text(encoding="utf-8")
    assert "用户改过的文本。" in written
    assert "第 0 条测试文本。" not in written


def test_proofread_rejects_an_empty_transcript(service: TrainingWizardService) -> None:
    """A blank line would train the voice against nothing."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    payload = service.proofread_payload(job_id)
    bad = [{"path": clip["path"], "text": ""} for clip in payload["clips"]]

    result = service.submit_proofread(job_id, bad)

    assert result["ok"] is False
    assert result["error"]


# ---------------------------------------------------------------------------
# Training start guards
# ---------------------------------------------------------------------------


def test_training_refuses_to_start_before_proofreading(
    service: TrainingWizardService,
) -> None:
    """Ordering is enforced server-side; the UI is not the only guard."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]

    result = service.start_training(job_id)

    assert result["ok"] is False
    assert "校对" in result["error"]


def test_training_refuses_to_start_without_the_ddp_patch(tmp_path: Path) -> None:
    """The unpatched-checkout refusal reaches the caller, not just the probe."""
    upstream = make_upstream(tmp_path / "up", with_ddp_guard=False)
    audio_dir, list_path = make_material(tmp_path)
    service = TrainingWizardService(
        store=TrainingJobStore(tmp_path / "jobs.db"),
        upstream_root=upstream,
        material_dir=audio_dir,
        list_path=list_path,
        probe=lambda: {"ready": True, "blocking": []},
    )
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.submit_proofread(
        job_id,
        [{"path": clip["path"], "text": "文本。"} for clip in service.proofread_payload(job_id)["clips"]],
    )

    result = service.start_training(job_id)

    assert result["ok"] is False
    assert "s2-train-single-gpu-ddp.patch" in result["error"]


def test_training_refuses_when_the_precheck_is_blocking(tmp_path: Path) -> None:
    """A blocking precheck stops the run before a process is spawned."""
    upstream = make_upstream(tmp_path / "up")
    audio_dir, list_path = make_material(tmp_path)
    service = TrainingWizardService(
        store=TrainingJobStore(tmp_path / "jobs.db"),
        upstream_root=upstream,
        material_dir=audio_dir,
        list_path=list_path,
        probe=lambda: {"ready": False, "blocking": ["GPU 不可用"]},
    )
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.submit_proofread(
        job_id,
        [{"path": clip["path"], "text": "文本。"} for clip in service.proofread_payload(job_id)["clips"]],
    )

    result = service.start_training(job_id)

    assert result["ok"] is False
    assert "GPU 不可用" in result["error"]


def test_only_one_training_job_may_run_at_a_time(service: TrainingWizardService) -> None:
    """Upstream's generated config lives in a shared TEMP directory.

    Two concurrent runs would overwrite each other's settings, so the second
    start is refused rather than allowed to corrupt the first.
    """
    first = service.import_material(voice_name="第一个")["job_id"]
    second = service.import_material(voice_name="第二个")["job_id"]
    for job_id in (first, second):
        service.submit_proofread(
            job_id,
            [{"path": c["path"], "text": "文本。"} for c in service.proofread_payload(job_id)["clips"]],
        )
    service.store.transition(first, JobState.RUNNING, pid=999)

    result = service.start_training(second)

    assert result["ok"] is False
    assert "已有训练任务在运行" in result["error"]


def test_training_starts_from_a_job_staged_by_preprocessing(
    service: TrainingWizardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proofread job may start training without a restart in between.

    Found by the end-to-end probe: preprocessing leaves the job in `running`
    (the state it used to enter before parking on proofreading), and an earlier
    guard refused to start *any* job in that state. The user-visible symptom was
    a wizard that could never train anything. What actually matters is whether a
    live process exists, not which state the record happens to be in.
    """
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.run_preprocess(job_id)
    service.submit_proofread(
        job_id,
        [
            {"path": clip["path"], "text": clip["text"]}
            for clip in service.proofread_payload(job_id)["clips"]
        ],
    )
    # The staged state the probe hit.
    assert service.store.get(job_id).state in (
        JobState.RUNNING,
        JobState.AWAITING_PROOFREAD,
    )

    launched: list = []

    class FakeProcess:
        pid = 31337

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        launched.append(command)
        return FakeProcess()

    service._popen = fake_popen  # noqa: SLF001 - the injection seam this test needs
    monkeypatch.setattr(
        service, "_probe", lambda: {"ready": True, "blocking": []}
    )
    # The preprocessed artefacts must look present, or the run is refused for a
    # different (and correct) reason.
    from aemeath.training import wizard as wizard_module

    monkeypatch.setattr(
        wizard_module.TrainingWizardService,
        "check_training_ready",
        lambda self, job: {"ok": True, "error": "", "missing": [], "opt_dir": ""},
    )

    result = service.start_training(job_id)

    assert result["ok"] is True, result.get("error")
    assert launched, "训练进程未被启动"
    # Stop the watcher thread's job so the test does not depend on it settling.
    service.store.transition(job_id, JobState.FAILED, error="测试结束", error_kind="infrastructure")


def test_training_refuses_when_a_recorded_pid_is_still_alive(
    service: TrainingWizardService,
) -> None:
    """A genuinely live training process is not started twice.

    This is the case the guard above must still catch: the PID is real, so a
    second run would fight the first over upstream's shared TEMP config.
    """
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.submit_proofread(
        job_id,
        [
            {"path": clip["path"], "text": clip["text"]}
            for clip in service.proofread_payload(job_id)["clips"]
        ],
    )
    service.store.transition(job_id, JobState.RUNNING, pid=4242)
    service._pid_alive = lambda pid: True  # noqa: SLF001 - the injection seam

    result = service.start_training(job_id)

    assert result["ok"] is False
    assert "已有训练进程在运行" in result["error"]


# ---------------------------------------------------------------------------
# Cancellation scope
# ---------------------------------------------------------------------------


def test_stop_is_a_noop_without_a_recorded_pid(service: TrainingWizardService) -> None:
    """No PID means no kill: guessing would risk the user's own service.

    Upstream cancels with `taskkill /t /f /pid`, which walks the process tree.
    Pointed at a recycled or unrelated PID that terminates something the user
    is relying on.
    """
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=None)

    killed: list[int] = []
    result = service.stop(job_id, killer=lambda pid: killed.append(pid))

    assert result["ok"] is False
    assert killed == []


def test_stop_targets_only_this_jobs_pid(service: TrainingWizardService) -> None:
    """The kill is aimed at the PID this application started, and nothing else."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=4242)

    killed: list[int] = []
    result = service.stop(job_id, killer=lambda pid: killed.append(pid))

    assert killed == [4242]
    assert result["ok"] is True
    assert service.store.get(job_id).state is JobState.STOPPING


def test_repeated_stop_does_not_kill_twice(service: TrainingWizardService) -> None:
    """A second kill could land on a recycled PID.

    Once the job has left `running` there is nothing of ours to stop, so the
    call reports that instead of firing again.
    """
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=4242)

    killed: list[int] = []
    service.stop(job_id, killer=lambda pid: killed.append(pid))
    second = service.stop(job_id, killer=lambda pid: killed.append(pid))

    assert killed == [4242]
    assert second["ok"] is False


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retry_is_refused_for_a_material_failure(service: TrainingWizardService) -> None:
    """Retrying bad material reproduces the same failure and burns GPU time."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=1)
    service.store.transition(
        job_id,
        JobState.FAILED,
        error="退出码 0 但无产物。",
        error_kind="material",
    )

    result = service.retry(job_id)

    assert result["ok"] is False
    assert result["retryable"] is False


def test_retry_is_allowed_for_an_infrastructure_failure(
    service: TrainingWizardService,
) -> None:
    """A killed process or a missing prerequisite can be restarted."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=1)
    service.store.transition(
        job_id,
        JobState.FAILED,
        error="训练进程退出码 1。",
        error_kind="infrastructure",
    )

    result = service.retry(job_id)

    assert result["retryable"] is True
    assert service.store.get(job_id).steps.get("train") is None or result["ok"] is True


def test_retry_is_refused_for_a_running_job(service: TrainingWizardService) -> None:
    """Restarting a live run is the concurrency hazard, again."""
    job_id = service.import_material(voice_name="测试声音")["job_id"]
    service.store.transition(job_id, JobState.RUNNING, pid=1)

    result = service.retry(job_id)

    assert result["retryable"] is False


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_reports_steps_and_state_without_inventing_progress(
    service: TrainingWizardService,
) -> None:
    """No percentage is reported, because upstream gives no reliable one."""
    service.import_material(voice_name="测试声音")

    overview = service.overview()

    assert overview["ok"] is True
    assert len(overview["jobs"]) == 1
    job = overview["jobs"][0]
    assert job["state"]
    assert "progress" not in job or job["progress"] in (None, "")
    assert job["steps"]


def test_overview_distinguishes_no_jobs_from_an_unavailable_store(
    service: TrainingWizardService,
) -> None:
    """'Nothing yet' and 'broken' need different actions from the user."""
    overview = service.overview()

    assert overview["ok"] is True
    assert overview["available"] is True
    assert overview["jobs"] == []
