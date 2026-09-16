"""Training-job persistence and state machine (V2-T06).

The wizard is a *long-running* operation that outlives the page that started it.
That single property is the source of every rule pinned here:

* a job's state must survive the management page closing **and** the process
  restarting, so the user who comes back sees what actually happened rather
  than a page that forgot;
* the restart reconciliation must never *promote* a job. Upstream can exit 0
  without writing a weight (V2-T05 measured this), so a job that claims to be
  running while its process is gone is reported as failed unless the artefacts
  really exist. Silently flipping it to success is how a user ends up applying
  a voice that was never produced;
* a restart must not re-run training on its own. Training costs minutes and
  GPU memory, and the user did not ask for a second run;
* cancellation is scoped to the PID this application started. The PID is the
  only thing separating "stop our training" from "stop the user's TTS service",
  so a job without one must refuse to cancel rather than guess.

Nothing here needs GPT-SoVITS, a GPU or a real training run: the process and the
clock are injected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aemeath.training.jobs import (  # noqa: E402
    JobState,
    TrainingJobRecord,
    TrainingJobStore,
    can_transition,
    reconcile_startup,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> TrainingJobStore:
    """A store on a throwaway database."""
    return TrainingJobStore(tmp_path / "jobs.db")


def make_record(**overrides) -> TrainingJobRecord:
    """A record with the fields every test cares about already filled in."""
    values = {
        "job_id": "train-abc12345",
        "exp_name": "aemeath_wizard",
        "voice_name": "测试声音",
        "state": JobState.PENDING,
    }
    values.update(overrides)
    return TrainingJobRecord(**values)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_job_survives_a_new_store_on_the_same_file(store: TrainingJobStore) -> None:
    """A job written by one process is visible to the next one.

    This is the whole point of the store: the management page is a window onto
    a task that keeps running after the window closes.
    """
    record = make_record(state=JobState.RUNNING, pid=4242)
    store.save(record)

    reopened = TrainingJobStore(store.path)
    loaded = reopened.get("train-abc12345")

    assert loaded is not None
    assert loaded.state is JobState.RUNNING
    assert loaded.pid == 4242
    assert loaded.exp_name == "aemeath_wizard"
    assert loaded.voice_name == "测试声音"


def test_store_path_is_created_on_demand(tmp_path: Path) -> None:
    """Opening a store where no database exists yet must not raise."""
    target = tmp_path / "nested" / "jobs.db"
    store = TrainingJobStore(target)

    assert store.list_jobs() == []
    assert target.exists()


def test_steps_round_trip_through_the_store(store: TrainingJobStore) -> None:
    """Per-step fingerprints survive, because retry decisions depend on them."""
    record = make_record(
        steps={
            "import": {"done": True, "fingerprint": "abc"},
            "clean": {"done": True, "fingerprint": "def"},
        }
    )
    store.save(record)

    loaded = TrainingJobStore(store.path).get("train-abc12345")
    assert loaded is not None
    assert loaded.steps["import"]["fingerprint"] == "abc"
    assert loaded.steps["clean"]["done"] is True


def test_artifacts_round_trip_through_the_store(store: TrainingJobStore) -> None:
    """The artefact list is what makes a success claim checkable after restart."""
    record = make_record(
        state=JobState.SUCCEEDED,
        artifacts=[{"path": "SoVITS_weights_v2/x_e1.pth", "size_bytes": 84992}],
    )
    store.save(record)

    loaded = TrainingJobStore(store.path).get("train-abc12345")
    assert loaded is not None
    assert loaded.artifacts[0]["size_bytes"] == 84992


def test_saving_the_same_job_twice_updates_it(store: TrainingJobStore) -> None:
    """One job is one row; a status refresh updates rather than duplicates."""
    store.save(make_record(state=JobState.RUNNING, pid=100))
    store.save(make_record(state=JobState.SUCCEEDED, pid=100))

    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].state is JobState.SUCCEEDED


def test_list_jobs_returns_newest_first(store: TrainingJobStore) -> None:
    """The page shows the most recent task without the caller re-sorting."""
    store.save(make_record(job_id="train-old", created_at=1000.0))
    store.save(make_record(job_id="train-new", created_at=2000.0))

    assert [job.job_id for job in store.list_jobs()] == ["train-old", "train-new"][::-1]


def test_get_returns_none_for_an_unknown_job(store: TrainingJobStore) -> None:
    """An unknown id is a normal answer, not an exception."""
    assert store.get("train-nope") is None


def test_running_jobs_are_listed_for_the_exit_prompt(store: TrainingJobStore) -> None:
    """Quitting while training runs must be able to name the live tasks."""
    store.save(make_record(job_id="train-a", state=JobState.RUNNING, pid=1234))
    store.save(make_record(job_id="train-b", state=JobState.SUCCEEDED))

    running = store.list_jobs(state=JobState.RUNNING)
    assert [job.job_id for job in running] == ["train-a"]


def test_running_jobs_ignores_a_job_with_no_process(store: TrainingJobStore) -> None:
    """A job in `running` without a PID holds nothing.

    Preprocessing parks a job in `running` before proofreading, so counting
    those as live made the single-run guard refuse every later training task
    because of a job that owned no process. Found by the end-to-end probe.
    """
    store.save(make_record(job_id="train-staged", state=JobState.RUNNING, pid=None))
    store.save(make_record(job_id="train-live", state=JobState.RUNNING, pid=4242))

    assert [job.job_id for job in store.running_jobs()] == ["train-live"]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_pending_may_only_start_running() -> None:
    """A task that has not begun cannot claim to have finished."""
    assert can_transition(JobState.PENDING, JobState.RUNNING)
    assert not can_transition(JobState.PENDING, JobState.SUCCEEDED)
    assert not can_transition(JobState.PENDING, JobState.STOPPED)


def test_stopping_is_an_intermediate_state_not_a_finish() -> None:
    """`stopping` exists because a kill is not instantaneous.

    The architecture lists it explicitly; collapsing it into `stopped` would
    show the user a settled state while the process is still dying.
    """
    assert can_transition(JobState.RUNNING, JobState.STOPPING)
    assert can_transition(JobState.STOPPING, JobState.STOPPED)
    assert can_transition(JobState.STOPPING, JobState.FAILED)
    # And it is not terminal: a stopping job is still in flight.
    assert not can_transition(JobState.STOPPING, JobState.SUCCEEDED)


def test_awaiting_proofread_is_reachable_from_running() -> None:
    """Preprocessing hands control back to the user rather than to training."""
    assert can_transition(JobState.RUNNING, JobState.AWAITING_PROOFREAD)
    assert can_transition(JobState.AWAITING_PROOFREAD, JobState.RUNNING)


def test_outcome_states_do_not_drift_back_to_running() -> None:
    """An outcome is not quietly overwritten by a late poll.

    Success is final outright. Failure and stop are final *outcomes* too, but
    they are the two states the user is allowed to retry from, so the only
    legal move out of them is an explicit restart.
    """
    for target in JobState:
        assert not can_transition(JobState.SUCCEEDED, target), f"succeeded -> {target}"

    for outcome in (JobState.FAILED, JobState.STOPPED):
        for target in JobState:
            expected = target is JobState.RUNNING
            assert can_transition(outcome, target) is expected, f"{outcome} -> {target}"


def test_failed_may_restart_but_succeeded_may_not() -> None:
    """Retry re-enters the machine only where V2-T05 said retry is sound."""
    assert can_transition(JobState.FAILED, JobState.RUNNING)
    assert can_transition(JobState.STOPPED, JobState.RUNNING)
    assert not can_transition(JobState.SUCCEEDED, JobState.RUNNING)


def test_store_rejects_an_illegal_transition(store: TrainingJobStore) -> None:
    """The guard lives in the store so no caller can bypass it."""
    store.save(make_record(state=JobState.SUCCEEDED))

    with pytest.raises(ValueError):
        store.transition("train-abc12345", JobState.RUNNING)


def test_store_transition_records_detail_and_error(store: TrainingJobStore) -> None:
    """A failure carries a readable reason, not just a state."""
    store.save(make_record(state=JobState.PENDING))
    store.transition("train-abc12345", JobState.RUNNING, detail="训练进程已启动。")
    store.transition(
        "train-abc12345",
        JobState.FAILED,
        detail="训练进程退出码 1。",
        error="CUDA out of memory",
        error_kind="infrastructure",
    )

    loaded = store.get("train-abc12345")
    assert loaded is not None
    assert loaded.state is JobState.FAILED
    assert loaded.error == "CUDA out of memory"
    assert loaded.error_kind == "infrastructure"
    assert loaded.detail == "训练进程退出码 1。"


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


def test_alive_process_keeps_the_job_running(store: TrainingJobStore) -> None:
    """A running job whose PID is still alive stays running.

    The tempting shortcut — relabel everything `failed` on startup because we
    no longer hold the handle — would report a healthy run as broken.
    """
    store.save(make_record(state=JobState.RUNNING, pid=4242))

    report = reconcile_startup(store, pid_alive=lambda pid: True)

    assert report["checked"] == 1
    assert report["kept_running"] == ["train-abc12345"]
    assert report["restarted"] == []
    assert store.get("train-abc12345").state is JobState.RUNNING


def test_dead_process_with_artifacts_becomes_success(store: TrainingJobStore) -> None:
    """The artefacts decide, not the absence of a handle.

    Training can outlive the process that launched it, so a job whose PID is
    gone but whose weight exists really did finish.
    """
    store.save(make_record(state=JobState.RUNNING, pid=4242))

    report = reconcile_startup(
        store,
        pid_alive=lambda pid: False,
        artifacts_for=lambda job: [{"path": "SoVITS_weights_v2/x_e1.pth", "size_bytes": 1}],
    )

    loaded = store.get("train-abc12345")
    assert loaded.state is JobState.SUCCEEDED
    assert report["succeeded"] == ["train-abc12345"]


def test_dead_process_without_artifacts_becomes_failed(store: TrainingJobStore) -> None:
    """No process and no weight is a failure, not an optimistic 'maybe'.

    V2-T05 measured upstream exiting 0 while writing nothing; a job left in
    `running` would let the user believe a voice is coming.
    """
    store.save(make_record(state=JobState.RUNNING, pid=4242))

    report = reconcile_startup(
        store, pid_alive=lambda pid: False, artifacts_for=lambda job: []
    )

    loaded = store.get("train-abc12345")
    assert loaded.state is JobState.FAILED
    assert loaded.error
    assert report["failed"] == ["train-abc12345"]


def test_reconciliation_never_restarts_training(store: TrainingJobStore) -> None:
    """Startup must not spend GPU time the user did not ask for.

    The report says so explicitly, so a future caller cannot start doing it
    without contradicting a pinned expectation.
    """
    store.save(make_record(state=JobState.RUNNING, pid=4242))

    report = reconcile_startup(
        store, pid_alive=lambda pid: False, artifacts_for=lambda job: []
    )

    assert report["restarted"] == []
    assert report["restart_policy"] == "never"


def test_stopping_becomes_stopped_when_the_process_is_gone(
    store: TrainingJobStore,
) -> None:
    """A kill that landed while the app was closed settles as stopped."""
    store.save(make_record(state=JobState.STOPPING, pid=4242))

    report = reconcile_startup(
        store, pid_alive=lambda pid: False, artifacts_for=lambda job: []
    )

    assert store.get("train-abc12345").state is JobState.STOPPED
    assert report["stopped"] == ["train-abc12345"]


def test_reconciliation_leaves_terminal_jobs_untouched(store: TrainingJobStore) -> None:
    """Finished work is history; startup does not rewrite it."""
    store.save(make_record(job_id="train-done", state=JobState.SUCCEEDED, pid=1))
    store.save(make_record(job_id="train-bad", state=JobState.FAILED, pid=2))

    report = reconcile_startup(store, pid_alive=lambda pid: True)

    assert report["checked"] == 0
    assert store.get("train-done").state is JobState.SUCCEEDED
    assert store.get("train-bad").state is JobState.FAILED


def test_running_job_without_pid_is_failed_not_left_hanging(
    store: TrainingJobStore,
) -> None:
    """A `running` job with no PID cannot be reconciled, so it is not trusted.

    Without a PID there is no way to ask whether the work is alive, and leaving
    it `running` forever is strictly worse than reporting that the record is
    incomplete.
    """
    store.save(make_record(state=JobState.RUNNING, pid=None))

    report = reconcile_startup(
        store, pid_alive=lambda pid: True, artifacts_for=lambda job: []
    )

    loaded = store.get("train-abc12345")
    assert loaded.state is JobState.FAILED
    assert loaded.error
    assert report["failed"] == ["train-abc12345"]


# ---------------------------------------------------------------------------
# Step fingerprints
# ---------------------------------------------------------------------------


def test_step_fingerprint_records_inputs(store: TrainingJobStore) -> None:
    """Each step stores what it consumed, so reuse can be justified later."""
    store.save(make_record())
    store.record_step("train-abc12345", "import", fingerprint="f1", outputs=["a.list"])

    loaded = store.get("train-abc12345")
    assert loaded.steps["import"] == {
        "fingerprint": "f1",
        "outputs": ["a.list"],
        "done": True,
    }


def test_changed_fingerprint_invalidates_downstream_steps(
    store: TrainingJobStore,
) -> None:
    """Editing the material must not silently reuse artefacts made from it.

    Reusing preprocessed data built from different audio is how a trained voice
    ends up not matching the clips the user proofread.
    """
    store.save(make_record())
    for step in ("import", "clean", "proofread"):
        store.record_step("train-abc12345", step, fingerprint="same", outputs=[])

    invalidated = store.invalidate_from("train-abc12345", "clean")

    loaded = store.get("train-abc12345")
    assert "clean" in invalidated
    assert "proofread" in invalidated
    assert "import" not in invalidated
    assert "clean" not in loaded.steps
    assert "proofread" not in loaded.steps
    assert "import" in loaded.steps


def test_step_still_matches_only_for_an_unchanged_fingerprint(
    store: TrainingJobStore,
) -> None:
    """The reuse decision is a comparison, not a flag."""
    store.save(make_record())
    store.record_step("train-abc12345", "clean", fingerprint="f1", outputs=[])

    assert store.step_matches("train-abc12345", "clean", "f1")
    assert not store.step_matches("train-abc12345", "clean", "f2")
    assert not store.step_matches("train-abc12345", "never-ran", "f1")
