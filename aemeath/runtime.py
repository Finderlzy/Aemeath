"""Runtime assembly and observability.

Builds one Aemeath runtime per backend process: the situation store, memory,
screen observer, proactive scheduler and coordinator, sharing a single local
database.

Single-instance rule: exactly one runtime exists per process, and reconnecting
the desktop client must not create a second character state. :func:`get_runtime`
enforces that.

Logging records turn ids, module, duration, errors and cancellations. Message
bodies, recordings and screenshots are deliberately **not** logged by default.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from .adapters import EmbeddingAdapter, ExtractionAdapter
from .config import AemeathConfig, load_config
from .coordinator import CoordinatorHooks, EventCoordinator
from .interfaces import EventSource, SpeechMode
from .memory import MemoryService, MemoryStore
from .proactive import ProactiveScheduler
from .screen import CaptureBackend, ScreenObserver, VisionAdapter
from .situation import SituationManager, SituationStore


@dataclass
class TurnMetric:
    """Timing and outcome for one turn.

    Message text is intentionally absent: latency data is useful, transcripts
    are not needed to diagnose it.

    Two families of timings are recorded and never mixed:

    * **backend** timings measured with this process's monotonic clock, from
      turn start to the point where the backend produced something;
    * **client** timings measured with the *client's* monotonic clock, reported
      through receipts as a single duration.

    Subtracting one from the other is meaningless because the two clocks have
    different origins, so the two families are kept in separate fields.
    """

    turn_id: str
    source: str
    started_at: float
    # -- backend stage timings (monotonic, this process) -----------------
    first_text_ms: Optional[float] = None
    first_audio_ms: Optional[float] = None
    total_ms: Optional[float] = None
    # -- client experience timings (reported durations) -----------------
    client_first_text_ms: Optional[float] = None
    client_playback_start_ms: Optional[float] = None
    client_cancel_ms: Optional[float] = None
    cancelled: bool = False
    error: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    #: True when the turn ended without a recorded outcome, so double-finishing
    #: a turn is idempotent.
    finished: bool = False
    #: Whether generation finished, separately from whether playback finished.
    generation_finished: bool = False
    playback_finished: bool = False


@dataclass
class SampleSet:
    """A set of latency samples with an explicit sufficiency verdict.

    The plan requires that a P95 over too few samples is reported as
    insufficient rather than as a number that looks authoritative.
    """

    #: Below this count a percentile is reported as insufficient.
    minimum: int = 20

    values: List[float] = field(default_factory=list)

    def add(self, value: Optional[float]) -> None:
        """Record one sample, ignoring missing values."""
        if value is not None:
            self.values.append(float(value))

    @property
    def count(self) -> int:
        """Number of samples collected."""
        return len(self.values)

    @property
    def sufficient(self) -> bool:
        """Whether there are enough samples for a meaningful percentile."""
        return self.count >= self.minimum

    def percentile(self, fraction: float) -> Optional[float]:
        """Percentile using the nearest-rank method.

        The plan fixes the index at ``ceil(fraction * n)`` on the sorted
        samples, so the result does not depend on an interpolation choice.
        """
        if not self.values:
            return None
        ordered = sorted(self.values)
        index = math.ceil(fraction * len(ordered))
        index = min(max(index, 1), len(ordered))
        return ordered[index - 1]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the samples and their sufficiency verdict."""
        return {
            "count": self.count,
            "sufficient": self.sufficient,
            "minimum_required": self.minimum,
            "p50": self.percentile(0.50),
            "p95": self.percentile(0.95),
        }


@dataclass
class UsageTotals:
    """Accumulated token usage.

    Costs are only reported when a price is configured; the plan forbids
    guessing a price that was never supplied. An unknown token count stays
    unknown rather than being folded in as zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    price_per_1k_input: Optional[float] = None
    price_per_1k_output: Optional[float] = None
    #: Turns whose provider did not report usage.
    unknown_usage_turns: int = 0

    def add(self, input_tokens: Optional[int], output_tokens: Optional[int]) -> None:
        """Accumulate usage from one turn.

        A turn where the provider reported nothing is counted as unknown rather
        than as zero usage, so totals are never silently understated.
        """
        self.turns += 1
        if input_tokens is None and output_tokens is None:
            self.unknown_usage_turns += 1
            return
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)

    def estimated_cost(self) -> Optional[float]:
        """Estimated cost, or ``None`` when no price is configured."""
        if self.price_per_1k_input is None or self.price_per_1k_output is None:
            return None
        return (
            self.input_tokens / 1000.0 * self.price_per_1k_input
            + self.output_tokens / 1000.0 * self.price_per_1k_output
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise usage, including cost only when it is known."""
        data = asdict(self)
        cost = self.estimated_cost()
        data["estimated_cost"] = cost
        data["cost_available"] = cost is not None
        data["usage_complete"] = self.unknown_usage_turns == 0
        return data


class MetricsRecorder:
    """Collects per-turn metrics for the settings screen and diagnostics.

    Metrics are recorded in memory *and* appended to a JSONL file, because the
    two answer different questions: the live process needs current numbers, while
    the status screen — a separate process — can only see what was written down.

    Only the writing half used to exist, so ``scripts/status.py`` always reported
    zero completed turns and no percentiles, even with a full metrics file on
    disk. That reads as "no data collected" rather than "this process just
    started", which is exactly the wrong conclusion to draw from a latency
    report, so :meth:`load` reads the file back.
    """

    def __init__(self, log_path: Optional[Path] = None, max_turns: int = 500) -> None:
        """Configure where metrics are kept.

        Args:
            log_path: Optional JSONL file for metrics.
            max_turns: How many recent turns to keep in memory.
        """
        self._path = log_path
        self._max = max_turns
        self.turns: List[TurnMetric] = []
        self.usage = UsageTotals()

        # Backend stage timings (this process's monotonic clock).
        self.backend_first_text = SampleSet()
        self.backend_first_audio = SampleSet()
        # Client-reported experience timings (client's monotonic clock).
        self.client_first_text = SampleSet()
        self.client_playback_start = SampleSet()
        self.client_cancel = SampleSet()

        #: Counters for turns that ended without completing normally.
        self.cancelled_count = 0
        self.error_count = 0

    def load(self) -> int:
        """Read previously persisted turns back into memory.

        Used by entry points that report on a *past* run: without this, a status
        check started in a fresh process sees an empty recorder and reports
        "no samples" no matter how many turns were recorded.

        Malformed lines are skipped rather than aborting the load: a truncated
        final line is normal after an abrupt shutdown, and losing the whole
        history over it would be worse than losing one record.

        Returns:
            How many turns were loaded.
        """
        if self._path is None or not self._path.is_file():
            return 0

        loaded = 0
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("Could not read metrics file {}: {}", self._path, exc)
            return 0

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            metric = self._from_record(record)
            if metric is None:
                continue
            self.turns.append(metric)
            loaded += 1

        # Keep memory bounded the same way live recording does.
        if len(self.turns) > self._max:
            self.turns = self.turns[-self._max :]

        self._rebuild_aggregates()
        return loaded

    @staticmethod
    def _from_record(record: Dict[str, Any]) -> Optional[TurnMetric]:
        """Rebuild a :class:`TurnMetric` from one JSONL line.

        Unknown keys are ignored so a newer writer cannot break an older reader.
        """
        if not isinstance(record, dict) or "turn_id" not in record:
            return None
        fields = {f for f in TurnMetric.__dataclass_fields__}
        values = {key: value for key, value in record.items() if key in fields}
        try:
            return TurnMetric(**values)
        except TypeError:
            return None

    def _rebuild_aggregates(self) -> None:
        """Recompute sample sets and counters from the loaded turns.

        The aggregates are what the status screen actually reports, so they must
        reflect loaded history and not only turns finished in this process.
        """
        self.backend_first_text = SampleSet()
        self.backend_first_audio = SampleSet()
        self.client_first_text = SampleSet()
        self.client_playback_start = SampleSet()
        self.client_cancel = SampleSet()
        self.cancelled_count = 0
        self.error_count = 0
        self.usage = UsageTotals()

        for metric in self.turns:
            if not metric.finished:
                continue
            if metric.cancelled:
                self.cancelled_count += 1
            if metric.error:
                self.error_count += 1
            self.backend_first_text.add(metric.first_text_ms)
            self.backend_first_audio.add(metric.first_audio_ms)
            self.client_first_text.add(metric.client_first_text_ms)
            self.client_playback_start.add(metric.client_playback_start_ms)
            self.client_cancel.add(metric.client_cancel_ms)
            self.usage.add(metric.input_tokens, metric.output_tokens)

    def start_turn(self, turn_id: str, source: str) -> TurnMetric:
        """Record the start of a turn."""
        metric = TurnMetric(
            turn_id=turn_id, source=source, started_at=time.monotonic()
        )
        self.turns.append(metric)
        if len(self.turns) > self._max:
            self.turns = self.turns[-self._max :]
        return metric

    def _find(self, turn_id: str) -> Optional[TurnMetric]:
        """Look up the metric for a turn."""
        for metric in reversed(self.turns):
            if metric.turn_id == turn_id:
                return metric
        return None

    def mark_first_text(self, turn_id: str) -> None:
        """Record backend time to first display text (monotonic)."""
        metric = self._find(turn_id)
        if metric is not None and metric.first_text_ms is None:
            metric.first_text_ms = (time.monotonic() - metric.started_at) * 1000.0

    def mark_first_audio(self, turn_id: str) -> None:
        """Record backend time to first audio (monotonic)."""
        metric = self._find(turn_id)
        if metric is not None and metric.first_audio_ms is None:
            metric.first_audio_ms = (time.monotonic() - metric.started_at) * 1000.0

    def mark_generation_finished(self, turn_id: str) -> None:
        """Record that generation ended, independently of playback."""
        metric = self._find(turn_id)
        if metric is not None:
            metric.generation_finished = True

    def mark_playback_finished(self, turn_id: str, audio_slice_id: str = "",
                               client_elapsed_ms: Optional[float] = None) -> None:
        """Record that playback for a turn ended (client-reported)."""
        metric = self._find(turn_id)
        if metric is not None:
            metric.playback_finished = True

    # -- client receipts ------------------------------------------------

    def mark_text_displayed(self, turn_id: str,
                            client_elapsed_ms: Optional[float] = None) -> None:
        """Record the client-measured input-to-display duration."""
        metric = self._find(turn_id)
        if metric is None or client_elapsed_ms is None:
            return
        if metric.client_first_text_ms is None:
            metric.client_first_text_ms = float(client_elapsed_ms)
            self.client_first_text.add(client_elapsed_ms)
            if metric.finished:
                self._update_persisted(metric)

    def mark_playback_started(self, turn_id: str, audio_slice_id: str = "",
                              client_elapsed_ms: Optional[float] = None) -> None:
        """Record the client-measured stop-speaking-to-playback duration."""
        metric = self._find(turn_id)
        if metric is None or client_elapsed_ms is None:
            return
        if metric.client_playback_start_ms is None:
            metric.client_playback_start_ms = float(client_elapsed_ms)
            self.client_playback_start.add(client_elapsed_ms)
            if metric.finished:
                self._update_persisted(metric)

    def mark_cancel_complete(self, turn_id: str,
                             client_elapsed_ms: Optional[float] = None) -> None:
        """Record the client-measured stop-click-to-silence duration."""
        metric = self._find(turn_id)
        if metric is None or client_elapsed_ms is None:
            return
        metric.client_cancel_ms = float(client_elapsed_ms)
        self.client_cancel.add(client_elapsed_ms)
        if metric.finished:
            self._update_persisted(metric)

    def finish_turn(
        self,
        turn_id: str,
        *,
        cancelled: bool = False,
        error: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        """Record the end of a turn and its outcome.

        Finishing is idempotent: a turn that is cancelled and then unwound, or
        that fails after producing output, must not be counted twice.
        """
        metric = self._find(turn_id)
        if metric is None or metric.finished:
            return
        metric.finished = True
        metric.total_ms = (time.monotonic() - metric.started_at) * 1000.0
        metric.cancelled = cancelled
        metric.error = error
        metric.input_tokens = input_tokens
        metric.output_tokens = output_tokens
        if cancelled:
            self.cancelled_count += 1
        if error:
            self.error_count += 1
        if metric.first_text_ms is not None:
            self.backend_first_text.add(metric.first_text_ms)
        if metric.first_audio_ms is not None:
            self.backend_first_audio.add(metric.first_audio_ms)
        self.usage.add(input_tokens, output_tokens)
        self._persist(metric)

    def _persist(self, metric: TurnMetric) -> None:
        """Append one metric line to the metrics file, if configured."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            record = asdict(metric)
            # Wall-clock date for humans reading the log; durations above are
            # all monotonic and this field is never subtracted from them.
            record["logged_at"] = datetime.now(timezone.utc).isoformat()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover - filesystem dependent
            logger.warning("Could not write metrics: {}", exc)

    def _update_persisted(self, metric: TurnMetric) -> None:
        """Update an already persisted record with late arriving client receipts."""
        if self._path is None or not self._path.is_file():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            new_lines = []
            target = f'"turn_id": "{metric.turn_id}"'
            for line in lines:
                if target in line:
                    record = asdict(metric)
                    try:
                        old_rec = json.loads(line)
                        if "logged_at" in old_rec:
                            record["logged_at"] = old_rec["logged_at"]
                    except Exception:
                        pass
                    if "logged_at" not in record:
                        record["logged_at"] = datetime.now(timezone.utc).isoformat()
                    new_lines.append(json.dumps(record, ensure_ascii=False))
                else:
                    new_lines.append(line)
            self._path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except Exception as exc:  # pragma: no cover - filesystem dependent
            logger.warning("Could not update persisted metrics: {}", exc)

    @staticmethod
    def _duration_summary(samples: SampleSet) -> Dict[str, Any]:
        """Report a sample set, marking unavailable values explicitly."""
        data = samples.to_dict()
        data["available"] = samples.count > 0
        return data

    def summary(self) -> Dict[str, Any]:
        """Aggregate latency percentiles and usage.

        Client experience metrics and backend generation metrics are reported
        in separate sections, and a missing client receipt is reported as
        unavailable rather than as zero.
        """
        return {
            "turns": sum(1 for m in self.turns if m.finished),
            "cancelled": self.cancelled_count,
            "errors": self.error_count,
            "backend": {
                "first_text_ms": self._duration_summary(self.backend_first_text),
                "first_audio_ms": self._duration_summary(self.backend_first_audio),
            },
            "client": {
                "first_text_ms": self._duration_summary(self.client_first_text),
                "playback_start_ms": self._duration_summary(self.client_playback_start),
                "cancel_ms": self._duration_summary(self.client_cancel),
            },
            # Retained for the existing status screen.
            "p95_first_text_ms": self.backend_first_text.percentile(0.95),
            "p95_first_audio_ms": self.backend_first_audio.percentile(0.95),
            "usage": self.usage.to_dict(),
        }


@dataclass
class CapabilityStatus:
    """Whether one optional capability is usable.

    Three states are distinguished, because the plan forbids collapsing them:

    * ``configured`` — configured and initialised successfully;
    * ``disabled`` — the user explicitly turned it off;
    * ``error`` — configured, but initialisation failed. This is never silently
      treated as "fine".
    """

    name: str
    configured: bool = False
    enabled: bool = False
    error: Optional[str] = None
    detail: str = ""

    @property
    def state(self) -> str:
        """One of ``ready``, ``disabled`` or ``error``."""
        if self.error:
            return "error"
        if not self.configured:
            return "disabled"
        return "ready" if self.enabled else "disabled"

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the capability status."""
        return {
            "name": self.name,
            "state": self.state,
            "configured": self.configured,
            "enabled": self.enabled,
            "error": self.error,
            "detail": self.detail,
        }


@dataclass
class AemeathRuntime:
    """Holds the assembled Aemeath modules for one backend process."""

    config: AemeathConfig
    situation: SituationManager
    memory: MemoryService
    coordinator: EventCoordinator
    scheduler: ProactiveScheduler
    metrics: MetricsRecorder
    screen: Optional[ScreenObserver] = None
    bridge: Any = None
    capabilities: Dict[str, CapabilityStatus] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    #: Background tasks owned by the runtime, cancelled together on shutdown.
    tasks: Dict[str, asyncio.Task] = field(default_factory=dict, repr=False)

    def status(self) -> Dict[str, Any]:
        """Return a serialisable status snapshot for the settings screen."""
        state = self.situation.state
        return {
            "mode": state.mode.value,
            "state_version": self.situation.state_version,
            "microphone_enabled": state.microphone_enabled,
            "screen_observation_enabled": state.screen_observation_enabled,
            "proactive_enabled": state.proactive_enabled,
            "user_paused": state.user_paused,
            "voice_allowed": state.voice_allowed,
            "memories": len(self.memory.store.list_memories()),
            "pending_tasks": len(self.memory.store.pending_tasks()),
            "data_dir": str(self.config.data_dir),
            "capabilities": {
                name: capability.to_dict()
                for name, capability in self.capabilities.items()
            },
            "metrics": self.metrics.summary(),
        }

    def unhealthy_capabilities(self) -> List[CapabilityStatus]:
        """Capabilities that failed to initialise despite being configured.

        A non-empty result means the runtime must not report itself ready.
        """
        return [c for c in self.capabilities.values() if c.state == "error"]

    def assert_healthy(self) -> None:
        """Raise when a configured capability failed to initialise."""
        broken = self.unhealthy_capabilities()
        if broken:
            detail = "; ".join(f"{c.name}: {c.error}" for c in broken)
            raise RuntimeError(f"configured capability failed to initialise: {detail}")

    async def start(self) -> None:
        """Start the background tasks the runtime owns.

        A memory worker handles extraction and a proactive task drives the
        scheduler. The screen task only runs while observation is enabled.
        """
        if self.tasks:
            return
        loop = asyncio.get_running_loop()
        self.tasks["memory"] = loop.create_task(self._memory_worker())
        self.tasks["proactive"] = loop.create_task(self._proactive_worker())
        logger.info("Aemeath background tasks started ({}).", len(self.tasks))

    async def stop(self) -> None:
        """Cancel background tasks and release resources.

        Every task is cancelled and awaited so nothing keeps running after
        shutdown, and the client is detached so no further output is produced.
        """
        tasks, self.tasks = self.tasks, {}
        for task in tasks.values():
            task.cancel()
        for name, task in tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("Background task {} cancelled.", name)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Background task {} failed during shutdown: {}", name, exc)
        if self.bridge is not None:
            await self.bridge.shutdown()
        logger.info("Aemeath runtime stopped.")

    async def _memory_worker(self) -> None:
        """Process queued extraction tasks until cancelled."""
        interval = self.config.memory.extraction_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                if self.memory.has_extraction_adapter:
                    await self.memory.run_pending_extraction()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Memory worker iteration failed: {}", exc)

    async def _proactive_worker(self) -> None:
        """Drive proactive checks on a timer until cancelled.

        The scheduler is the only source of proactive turns; this worker asks
        the bridge to attempt one, and the bridge applies the eligibility rules
        before anything is generated. A denied attempt is the common case and
        costs nothing, so a fixed tick is sufficient.

        Whether Aemeath may speak is deliberately **not** read from the config
        here. There is no ``proactive.enabled`` setting: permission comes from
        the persisted situation switch, which the bridge and the scheduler
        consult per attempt. Reading a non-existent config field here used to
        raise ``AttributeError`` on the first tick, so the timer never ran at
        all and the backend could never start a topic on its own.
        """
        interval = self.config.proactive.check_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

            bridge = self.bridge
            if bridge is None:
                continue

            try:
                # The bridge re-checks every rule and observes on demand; the
                # timer only asks.
                await bridge.run_proactive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Proactive attempt failed: {}", exc)


_runtime: Optional[AemeathRuntime] = None


def build_runtime(
    *,
    config: Optional[AemeathConfig] = None,
    embedding: Optional[EmbeddingAdapter] = None,
    extraction: Optional[ExtractionAdapter] = None,
    vision: Optional[VisionAdapter] = None,
    capture: Optional[CaptureBackend] = None,
    hooks: Optional[CoordinatorHooks] = None,
    adapters: Optional[Any] = None,
    require_capabilities: bool = False,
) -> AemeathRuntime:
    """Construct a runtime from configuration and adapters.

    When no adapters are supplied the factories in :mod:`aemeath.adapters`
    build them from configuration. A capability that is configured but fails to
    initialise is recorded as an error; it is never silently substituted with
    ``None`` and then reported as ready.

    Args:
        config: Resolved Aemeath configuration; loaded from disk when omitted.
        embedding: Embedding adapter for memory retrieval.
        extraction: Extraction adapter for writing memories.
        vision: Vision adapter for screen summaries.
        capture: Platform capture backend.
        hooks: Output callbacks used by the coordinator.
        adapters: Adapter factory bundle; built from config when omitted.
        require_capabilities: Raise when a configured capability failed.

    Returns:
        A fully wired :class:`AemeathRuntime`.

    Raises:
        RuntimeError: When ``require_capabilities`` is set and an enabled
            capability failed to initialise.
    """
    resolved = config or load_config()
    resolved.ensure_dirs()

    capabilities: Dict[str, CapabilityStatus] = {}

    if adapters is None:
        from .adapters import AdapterFactory

        adapters = AdapterFactory.from_config(resolved)

    # --- build each capability, recording its real state ----------------
    if embedding is None:
        embedding, capabilities["embedding"] = adapters.build_embedding()
    else:
        capabilities["embedding"] = CapabilityStatus(
            name="embedding", configured=True, enabled=True, detail="injected"
        )

    if extraction is None:
        extraction, capabilities["extraction"] = adapters.build_extraction()
    else:
        capabilities["extraction"] = CapabilityStatus(
            name="extraction", configured=True, enabled=True, detail="injected"
        )

    if vision is None:
        vision, capabilities["vision"] = adapters.build_vision()
    else:
        capabilities["vision"] = CapabilityStatus(
            name="vision", configured=True, enabled=True, detail="injected"
        )

    if capture is None:
        capture, capabilities["capture"] = adapters.build_capture()
    else:
        capabilities["capture"] = CapabilityStatus(
            name="capture", configured=True, enabled=True, detail="injected"
        )

    situation = SituationManager(SituationStore(resolved.db_path))
    store = MemoryStore(
        resolved.db_path, embedding, similarity_floor=resolved.memory.similarity_floor
    )
    memory = MemoryService(
        store,
        extraction=extraction,
        recent_turns=resolved.memory.recent_turns,
        recall_limit=resolved.memory.recall_limit,
        summary_every=resolved.memory.experience_summary_every,
    )
    scheduler = ProactiveScheduler(
        cooldown_seconds=resolved.proactive.cooldown_seconds,
        max_per_hour=resolved.proactive.max_per_hour,
        startup_greeting_enabled=resolved.proactive.startup_greeting_enabled,
    )
    metrics = MetricsRecorder(log_path=resolved.log_dir / "turns.jsonl")

    screen = None
    if capture is not None and vision is not None:
        screen = ScreenObserver(
            capture,
            vision,
            max_edge_px=resolved.screen.max_edge_px,
            min_interval_seconds=resolved.screen.min_interval_seconds,
            min_stable_seconds=resolved.screen.min_stable_seconds,
            summary_max_age_seconds=resolved.screen.summary_max_age_seconds,
        )
        # The observer starts disabled and follows the persisted situation
        # state, so a restart never begins observing a screen the user had
        # switched off. ``set_enabled`` is a no-op when already False, so this
        # does not bump the generation on a cold start.
        screen.set_enabled(situation.state.screen_observation_enabled)

    # The bridge is created first so the coordinator's output hooks can point at
    # it: *whether* to speak stays the coordinator's decision, while delivering
    # the text and voicing it with upstream's synthesis engine is the bridge's.
    from .bridge import AemeathBridge

    bridge = AemeathBridge(
        coordinator=None,
        situation=situation,
        memory=memory,
        screen=screen,
        metrics=metrics,
        config=resolved,
    )

    async def _display_text(turn_id, text) -> None:
        """Send display text through the bridge's tagged frame."""
        await bridge.send_display_text(str(turn_id), text)

    hooks = hooks or CoordinatorHooks(
        on_display_text=_display_text, on_speak=bridge.on_speak
    )
    coordinator = EventCoordinator(
        situation=situation, scheduler=scheduler, hooks=hooks
    )
    bridge.attach_coordinator(coordinator)

    runtime = AemeathRuntime(
        config=resolved,
        situation=situation,
        memory=memory,
        coordinator=coordinator,
        scheduler=scheduler,
        metrics=metrics,
        screen=screen,
        bridge=bridge,
        capabilities=capabilities,
    )

    broken = runtime.unhealthy_capabilities()
    if broken:
        detail = "; ".join(f"{c.name}: {c.error}" for c in broken)
        logger.error("Aemeath capability initialisation failed: {}", detail)
        if require_capabilities:
            runtime.assert_healthy()

    logger.info(
        "Aemeath runtime ready (data={}, mode={}, capabilities={}).",
        resolved.data_dir,
        situation.state.mode.value,
        {name: c.state for name, c in capabilities.items()},
    )
    return runtime


def get_runtime(**kwargs: Any) -> AemeathRuntime:
    """Return the process-wide runtime, creating it on first use.

    Guarantees a single character state per process even if the desktop client
    reconnects.
    """
    global _runtime
    if _runtime is None:
        _runtime = build_runtime(**kwargs)
    return _runtime


def reset_runtime() -> None:
    """Drop the cached runtime (used by tests and on shutdown)."""
    global _runtime
    _runtime = None
