"""Persistent state for the voice-training wizard (V2-T06).

Why a store at all
------------------
Training a voice takes minutes, and the requirement is that closing the
management page does not cancel it. The page is therefore a *window* onto a task
it does not own, which means the task's state cannot live in the page's memory —
or even in the server process's. It lives here, in SQLite, next to the other
local data.

The state machine
-----------------
:class:`JobState` is the one the architecture already fixed: 待开始、运行中、
等待用户校对、停止中、已停止、失败、成功. Two of those deserve a note because
they are the ones a shortcut would drop:

``STOPPING``
    Killing a process is not instantaneous, and upstream's ``taskkill /t /f``
    walks the whole tree. Reporting ``stopped`` the moment the kill is *sent*
    would tell the user the machine is idle while it is still working.

``AWAITING_PROOFREAD``
    Preprocessing hands control back to a human. The requirement is explicit
    that a good ASR pass is not evidence that the material is correct, so there
    is a real state where the wizard is waiting on the user rather than on a
    process.

Restart reconciliation
----------------------
:func:`reconcile_startup` is the reason this module is careful rather than
convenient. When the process restarts it holds no handles, and the tempting
shortcut is to declare every interrupted job dead. That is wrong in both
directions: training can outlive the launcher, so a job whose PID is gone may
have *succeeded*; and a job left labelled ``running`` forever is a lie the user
cannot act on. So the rule is:

* PID still alive        -> stay ``running``;
* PID gone, artefacts    -> ``succeeded``;
* PID gone, no artefacts -> ``failed``;
* never restart anything.

The artefacts decide because V2-T05 measured upstream exiting 0 while writing no
weight. Trusting the exit code alone would let a later step apply a voice that
does not exist.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: Where the wizard keeps its tasks, relative to the local data directory.
DEFAULT_STORE_RELATIVE = Path("training") / "jobs.db"

#: Order in which the wizard's steps run. Used for downstream invalidation.
STEP_ORDER = ("import", "clean", "proofread", "prepare", "train", "audition", "apply")


class JobState(str, Enum):
    """Lifecycle of one wizard task.

    The values match the states the architecture names, and are persisted, so
    they must not be renamed without a migration.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_PROOFREAD = "awaiting_proofread"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


#: States a job never leaves.
TERMINAL_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.STOPPED)

#: Allowed transitions.
#:
#: Written out explicitly rather than derived, because the *edges* are the
#: contract: `pending -> succeeded` must be impossible (nothing ran) and
#: `succeeded -> running` must be impossible (the outcome is already acted on).
_TRANSITIONS: Dict[JobState, frozenset] = {
    JobState.PENDING: frozenset({JobState.RUNNING, JobState.FAILED}),
    JobState.RUNNING: frozenset(
        {
            JobState.AWAITING_PROOFREAD,
            JobState.STOPPING,
            JobState.SUCCEEDED,
            JobState.FAILED,
        }
    ),
    JobState.AWAITING_PROOFREAD: frozenset(
        {JobState.RUNNING, JobState.STOPPING, JobState.FAILED}
    ),
    JobState.STOPPING: frozenset({JobState.STOPPED, JobState.FAILED}),
    # A stopped or failed job may be retried; a succeeded one may not.
    JobState.STOPPED: frozenset({JobState.RUNNING}),
    JobState.FAILED: frozenset({JobState.RUNNING}),
    JobState.SUCCEEDED: frozenset(),
}


def can_transition(source: JobState, target: JobState) -> bool:
    """Whether ``source`` may become ``target``.

    Args:
        source: Current state.
        target: Proposed state.

    Returns:
        ``True`` when the move is legal.
    """
    if source is target:
        return False
    return target in _TRANSITIONS.get(source, frozenset())


@dataclass
class TrainingJobRecord:
    """One wizard task as it is stored.

    Attributes:
        job_id: Stable identifier, also the primary key.
        exp_name: Experiment name that groups the upstream artefacts.
        voice_name: What the user called this voice.
        state: Current lifecycle state.
        stage: The step the user is currently looking at.
        pid: PID of the process this application started, when there is one.
        detail: Human-readable description of the current state.
        error: Readable failure reason, empty when there is none.
        error_kind: ``""``, ``infrastructure``, ``material`` or ``environment``.
        steps: Per-step record of inputs consumed and outputs produced.
        artifacts: Weight files found on disk.
        material_dir: Directory holding the training clips.
        list_path: The ``.list`` annotation file.
        ref_audio_path: Reference audio chosen for the applied voice.
        prompt_text: Transcript of the reference audio.
        upstream_root: Checkout this job trained against.
        upstream_commit: Checkout revision, so a result stays attributable.
        upstream_dirty: Whether the checkout had local changes.
        applied_preset_id: Preset written when the voice was applied.
        created_at: Creation time, epoch seconds.
        updated_at: Last modification time, epoch seconds.
    """

    job_id: str
    exp_name: str
    voice_name: str
    state: JobState = JobState.PENDING
    stage: str = "import"
    pid: Optional[int] = None
    detail: str = ""
    error: str = ""
    error_kind: str = ""
    steps: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    material_dir: str = ""
    list_path: str = ""
    ref_audio_path: str = ""
    prompt_text: str = ""
    upstream_root: str = ""
    upstream_commit: str = ""
    upstream_dirty: Optional[bool] = None
    applied_preset_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


#: Columns persisted for one job, in a fixed order.
_COLUMNS = (
    "job_id",
    "exp_name",
    "voice_name",
    "state",
    "stage",
    "pid",
    "detail",
    "error",
    "error_kind",
    "steps",
    "artifacts",
    "material_dir",
    "list_path",
    "ref_audio_path",
    "prompt_text",
    "upstream_root",
    "upstream_commit",
    "upstream_dirty",
    "applied_preset_id",
    "created_at",
    "updated_at",
)

#: Columns stored as JSON text.
_JSON_COLUMNS = ("steps", "artifacts")


class TrainingJobStore:
    """SQLite-backed storage for wizard tasks."""

    def __init__(self, path: Path) -> None:
        """Open (and create) the store.

        Args:
            path: Database file. Parent directories are created as needed.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- schema --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with row access by name."""
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        """Create the table when it is absent.

        ``CREATE TABLE IF NOT EXISTS`` is the whole migration story here: the
        wizard is new, so there is no earlier shape to upgrade from.
        """
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS training_jobs (
                    job_id TEXT PRIMARY KEY,
                    exp_name TEXT NOT NULL,
                    voice_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    pid INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    error_kind TEXT NOT NULL DEFAULT '',
                    steps TEXT NOT NULL DEFAULT '{}',
                    artifacts TEXT NOT NULL DEFAULT '[]',
                    material_dir TEXT NOT NULL DEFAULT '',
                    list_path TEXT NOT NULL DEFAULT '',
                    ref_audio_path TEXT NOT NULL DEFAULT '',
                    prompt_text TEXT NOT NULL DEFAULT '',
                    upstream_root TEXT NOT NULL DEFAULT '',
                    upstream_commit TEXT NOT NULL DEFAULT '',
                    upstream_dirty INTEGER,
                    applied_preset_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )

    # -- conversion ----------------------------------------------------

    @staticmethod
    def _to_row(record: TrainingJobRecord) -> Dict[str, Any]:
        """Flatten a record into database values."""
        values: Dict[str, Any] = {}
        for column in _COLUMNS:
            value = getattr(record, column)
            if column == "state":
                value = value.value if isinstance(value, JobState) else str(value)
            elif column in _JSON_COLUMNS:
                value = json.dumps(value, ensure_ascii=False)
            elif column == "upstream_dirty":
                value = None if value is None else int(bool(value))
            values[column] = value

        now = time.time()
        if not values["created_at"]:
            values["created_at"] = now
        values["updated_at"] = now
        return values

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TrainingJobRecord:
        """Rebuild a record from a database row.

        A row written by an older build could carry a state this version does
        not know. It is surfaced as ``failed`` rather than raising, because a
        task list that cannot be read at all is worse than one task that needs
        attention.
        """
        data: Dict[str, Any] = {}
        for column in _COLUMNS:
            value = row[column]
            if column in _JSON_COLUMNS:
                try:
                    value = json.loads(value) if value else ({} if column == "steps" else [])
                except (TypeError, ValueError):
                    value = {} if column == "steps" else []
            elif column == "upstream_dirty":
                value = None if value is None else bool(value)
            data[column] = value

        try:
            data["state"] = JobState(data["state"])
        except ValueError:
            data["state"] = JobState.FAILED
            data["error"] = data.get("error") or f"未知状态: {row['state']}"
        return TrainingJobRecord(**data)

    # -- reads ---------------------------------------------------------

    def get(self, job_id: str) -> Optional[TrainingJobRecord]:
        """One job, or ``None`` when the id is unknown."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM training_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_jobs(self, *, state: Optional[JobState] = None) -> List[TrainingJobRecord]:
        """Jobs newest first, optionally filtered by state.

        Args:
            state: Restrict to one state, e.g. the live tasks shown when the
                user quits while training is running.

        Returns:
            Matching records, most recently created first.
        """
        query = "SELECT * FROM training_jobs"
        params: tuple = ()
        if state is not None:
            query += " WHERE state = ?"
            params = (state.value,)
        query += " ORDER BY created_at DESC, rowid DESC"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def running_jobs(self) -> List[TrainingJobRecord]:
        """Jobs that still hold a *process*.

        The PID is what makes this meaningful. A job in ``running`` with no PID
        is not training — preprocessing parks a job there before proofreading
        does its work — and counting it would make the single-run guard refuse
        every later task because of a job that holds nothing.
        """
        return [
            job
            for job in self.list_jobs()
            if job.state in (JobState.RUNNING, JobState.STOPPING) and job.pid is not None
        ]

    # -- writes --------------------------------------------------------

    def save(self, record: TrainingJobRecord) -> TrainingJobRecord:
        """Insert or update one job.

        Args:
            record: The record to persist.

        Returns:
            The record as stored, with timestamps filled in.
        """
        existing = self.get(record.job_id)
        if existing is not None and not record.created_at:
            record.created_at = existing.created_at

        values = self._to_row(record)
        record.created_at = values["created_at"]
        record.updated_at = values["updated_at"]

        placeholders = ", ".join("?" for _ in _COLUMNS)
        assignments = ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO training_jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(job_id) DO UPDATE SET {assignments}",
                tuple(values[column] for column in _COLUMNS),
            )
        return record

    def transition(
        self,
        job_id: str,
        target: JobState,
        *,
        pid: Optional[int] = None,
        detail: str = "",
        error: str = "",
        error_kind: str = "",
        stage: str = "",
    ) -> TrainingJobRecord:
        """Move a job to a new state, refusing illegal moves.

        The guard lives here rather than in the callers so that no path —
        route, probe or test — can put a job into a state it cannot legitimately
        reach.

        Args:
            job_id: Job to move.
            target: State to move to.
            pid: New PID, when the move starts or stops a process.
            detail: Human-readable description.
            error: Failure reason; stored only when non-empty.
            error_kind: Failure category.
            stage: The step the job is now on, when it changed.

        Returns:
            The updated record.

        Raises:
            KeyError: If the job does not exist.
            ValueError: If the transition is not legal.
        """
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)
        if not can_transition(record.state, target):
            raise ValueError(
                f"非法的状态迁移：{record.state.value} -> {target.value}"
            )

        record.state = target
        if pid is not None:
            record.pid = pid
        # Leaving `running` means we no longer own a live process; keeping the
        # PID would let a later stop fire at a PID that may have been recycled.
        if target in TERMINAL_STATES:
            record.pid = None
        if detail:
            record.detail = detail
        if error:
            record.error = error
        if error_kind:
            record.error_kind = error_kind
        if stage:
            record.stage = stage
        return self.save(record)

    # -- step bookkeeping ---------------------------------------------

    def record_step(
        self,
        job_id: str,
        step: str,
        *,
        fingerprint: str,
        outputs: Optional[List[str]] = None,
        **extra: Any,
    ) -> TrainingJobRecord:
        """Record that a step finished, and what it consumed.

        Args:
            job_id: Job to update.
            step: Step name.
            fingerprint: Hash of the inputs this step ran against.
            outputs: Paths produced.
            **extra: Additional facts, e.g. clip count or total duration.

        Returns:
            The updated record.

        Raises:
            KeyError: If the job does not exist.
        """
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)

        entry: Dict[str, Any] = {
            "fingerprint": fingerprint,
            "outputs": list(outputs or []),
            "done": True,
        }
        entry.update(extra)
        steps = dict(record.steps)
        steps[step] = entry
        record.steps = steps
        record.stage = step
        return self.save(record)

    def step_matches(self, job_id: str, step: str, fingerprint: str) -> bool:
        """Whether a finished step may be reused for the given inputs.

        Reuse is a comparison, not a flag: artefacts built from different audio
        must not be carried forward just because a step once succeeded.

        Args:
            job_id: Job to inspect.
            step: Step name.
            fingerprint: Fingerprint of the inputs about to be used.

        Returns:
            ``True`` when the step is done and consumed these exact inputs.
        """
        record = self.get(job_id)
        if record is None:
            return False
        entry = record.steps.get(step)
        if not isinstance(entry, dict) or not entry.get("done"):
            return False
        return entry.get("fingerprint") == fingerprint

    def invalidate_from(self, job_id: str, step: str) -> List[str]:
        """Drop a step and everything downstream of it.

        Used when an input changed. Keeping a downstream artefact whose input
        was edited is how a voice ends up trained on clips the user never
        approved.

        Args:
            job_id: Job to update.
            step: First step to invalidate.

        Returns:
            The steps that were removed, in run order.

        Raises:
            KeyError: If the job does not exist.
        """
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)

        if step not in STEP_ORDER:
            return []
        start = STEP_ORDER.index(step)
        removed = [name for name in STEP_ORDER[start:] if name in record.steps]
        if not removed:
            return []

        steps = dict(record.steps)
        for name in removed:
            steps.pop(name, None)
        record.steps = steps
        # The job can no longer claim the state that step produced.
        if record.state is JobState.AWAITING_PROOFREAD and step in ("import", "clean"):
            record.state = JobState.RUNNING
        self.save(record)
        return removed


def reconcile_startup(
    store: TrainingJobStore,
    *,
    pid_alive: Callable[[int], bool],
    artifacts_for: Optional[Callable[[TrainingJobRecord], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Reconcile in-flight jobs against reality after a restart.

    The rules are in the module docstring; the short version is that a job is
    only ever moved *towards* the truth the machine reports, and nothing is ever
    restarted.

    Args:
        store: The job store.
        pid_alive: Callable answering whether a PID is still running. Injected
            so tests need no real processes.
        artifacts_for: Callable returning the artefacts on disk for a job.
            Defaults to using whatever the record already recorded, which is
            the conservative choice: it cannot invent a weight file.

    Returns:
        A report naming what happened to each job, including
        ``restart_policy: "never"`` so the no-restart rule is explicit.
    """
    report: Dict[str, Any] = {
        "checked": 0,
        "kept_running": [],
        "succeeded": [],
        "failed": [],
        "stopped": [],
        "restarted": [],
        "restart_policy": "never",
    }

    lookup = artifacts_for or (lambda job: list(job.artifacts))

    for job in store.list_jobs():
        if job.state not in (JobState.RUNNING, JobState.STOPPING):
            continue
        report["checked"] += 1

        # No PID means the record cannot be checked at all. Leaving it
        # `running` forever would be a state the user can neither act on nor
        # clear, so it is reported as failed with the reason.
        if job.pid is None:
            store.transition(
                job.job_id,
                JobState.FAILED,
                error="任务记录缺少进程号，无法确认其运行状态；请重试或重新开始。",
                error_kind="infrastructure",
                detail="重启后无法核对运行状态。",
            )
            report["failed"].append(job.job_id)
            continue

        if pid_alive(job.pid):
            report["kept_running"].append(job.job_id)
            continue

        if job.state is JobState.STOPPING:
            store.transition(
                job.job_id,
                JobState.STOPPED,
                detail="停止请求已完成，训练进程已结束。",
            )
            report["stopped"].append(job.job_id)
            continue

        artifacts = lookup(job)
        if artifacts:
            store.transition(
                job.job_id,
                JobState.SUCCEEDED,
                detail=f"训练进程已结束，找到产物 {len(artifacts)} 个。",
            )
            if artifacts != job.artifacts:
                updated = store.get(job.job_id)
                if updated is not None:
                    updated.artifacts = artifacts
                    store.save(updated)
            report["succeeded"].append(job.job_id)
        else:
            store.transition(
                job.job_id,
                JobState.FAILED,
                error="训练进程已不在运行，且未找到任何产物。",
                error_kind="material",
                detail="重启后核对：无运行进程，也没有产物。",
            )
            report["failed"].append(job.job_id)

    return report


__all__ = [
    "DEFAULT_STORE_RELATIVE",
    "JobState",
    "STEP_ORDER",
    "TERMINAL_STATES",
    "TrainingJobRecord",
    "TrainingJobStore",
    "can_transition",
    "reconcile_startup",
]
