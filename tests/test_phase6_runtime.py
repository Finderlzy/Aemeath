"""Phase 6 tests: runtime assembly, metrics and single-instance behaviour."""

from __future__ import annotations

import json

import pytest

from aemeath.adapters import FakeEmbeddingAdapter, FakeExtractionAdapter
from aemeath.config import AemeathConfig, MemoryConfig, ProactiveConfig, ScreenConfig
from aemeath.interfaces import SpeechMode
from aemeath.runtime import (
    MetricsRecorder,
    UsageTotals,
    build_runtime,
    get_runtime,
    reset_runtime,
)
from tests.doubles import FakeScreenCapture, FakeVision, FakeWindow


@pytest.fixture(autouse=True)
def clean_runtime():
    """Ensure the process-wide runtime does not leak between tests."""
    reset_runtime()
    yield
    reset_runtime()


def make_config(tmp_path) -> AemeathConfig:
    """Configuration pointing at a temporary data directory."""
    return AemeathConfig(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        memory=MemoryConfig(recent_turns=12, recall_limit=5),
        proactive=ProactiveConfig(cooldown_seconds=900, max_per_hour=2),
        screen=ScreenConfig(),
    )


class TestRuntimeAssembly:
    """The runtime wires the modules against one database."""

    def test_build_creates_directories(self, tmp_path):
        config = make_config(tmp_path)
        build_runtime(config=config, embedding=FakeEmbeddingAdapter())
        assert config.data_dir.exists()
        assert config.log_dir.exists()

    def test_situation_and_memory_share_database(self, tmp_path):
        runtime = build_runtime(
            config=make_config(tmp_path), embedding=FakeEmbeddingAdapter()
        )
        assert runtime.config.db_path.exists()
        # Both modules read the same file.
        assert runtime.situation.state.mode is SpeechMode.NORMAL
        assert runtime.memory.store.list_memories() == []

    def test_screen_observer_absent_without_adapters(self, tmp_path):
        runtime = build_runtime(config=make_config(tmp_path))
        assert runtime.screen is None

    def test_screen_observer_built_with_adapters(self, tmp_path):
        runtime = build_runtime(
            config=make_config(tmp_path),
            vision=FakeVision(),
            capture=FakeScreenCapture(FakeWindow("editor")),
        )
        assert runtime.screen is not None

    def test_get_runtime_is_singleton(self, tmp_path):
        """Reconnecting the client must not create a second character state."""
        first = get_runtime(config=make_config(tmp_path))
        second = get_runtime(config=make_config(tmp_path))
        assert first is second

    def test_reset_allows_rebuild(self, tmp_path):
        first = get_runtime(config=make_config(tmp_path))
        reset_runtime()
        second = get_runtime(config=make_config(tmp_path))
        assert first is not second

    def test_status_snapshot(self, tmp_path):
        runtime = build_runtime(
            config=make_config(tmp_path), embedding=FakeEmbeddingAdapter()
        )
        status = runtime.status()
        assert status["mode"] == "normal"
        assert status["voice_allowed"] is True
        assert status["memories"] == 0
        assert "metrics" in status

    def test_status_reflects_class_mode(self, tmp_path):
        runtime = build_runtime(config=make_config(tmp_path))
        runtime.situation.set_mode(SpeechMode.CLASS)
        status = runtime.status()
        assert status["mode"] == "class"
        assert status["voice_allowed"] is False


class TestMetrics:
    """Latency and usage recording."""

    def test_turn_recorded(self, tmp_path):
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.mark_first_text("t1")
        recorder.finish_turn("t1", input_tokens=100, output_tokens=20)

        assert recorder.turns[0].total_ms is not None
        assert recorder.turns[0].first_text_ms is not None
        assert recorder.usage.input_tokens == 100

    def test_cancelled_turn_recorded(self):
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1", cancelled=True)
        assert recorder.turns[0].cancelled

    def test_error_recorded(self):
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1", error="provider down")
        assert recorder.turns[0].error == "provider down"

    def test_metrics_persisted_without_message_text(self, tmp_path):
        """Metrics must not leak conversation content."""
        path = tmp_path / "turns.jsonl"
        recorder = MetricsRecorder(log_path=path)
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1", input_tokens=5, output_tokens=5)

        payload = json.loads(path.read_text(encoding="utf-8").strip())
        assert "text" not in payload
        assert "content" not in payload
        assert payload["turn_id"] == "t1"

    def test_bounded_history(self):
        recorder = MetricsRecorder(max_turns=3)
        for i in range(5):
            recorder.start_turn(f"t{i}", "user_text")
        assert len(recorder.turns) == 3

    def test_summary_percentiles(self):
        recorder = MetricsRecorder()
        for i in range(3):
            recorder.start_turn(f"t{i}", "user_text")
            recorder.mark_first_text(f"t{i}")
            recorder.finish_turn(f"t{i}")
        summary = recorder.summary()
        assert summary["turns"] == 3
        assert summary["p95_first_text_ms"] is not None


class TestUsageCost:
    """Cost is only reported when a price is configured."""

    def test_cost_unavailable_without_price(self):
        usage = UsageTotals()
        usage.add(1000, 500)
        assert usage.estimated_cost() is None
        assert usage.to_dict()["cost_available"] is False

    def test_cost_computed_with_price(self):
        usage = UsageTotals(price_per_1k_input=0.01, price_per_1k_output=0.03)
        usage.add(1000, 1000)
        assert usage.estimated_cost() == pytest.approx(0.04)

    def test_usage_accumulates(self):
        usage = UsageTotals()
        usage.add(10, 5)
        usage.add(20, 15)
        assert usage.input_tokens == 30
        assert usage.output_tokens == 20
        assert usage.turns == 2

    def test_missing_tokens_treated_as_zero(self):
        usage = UsageTotals()
        usage.add(None, None)
        assert usage.input_tokens == 0
        assert usage.turns == 1


class TestExtractionThroughRuntime:
    """The runtime's memory service processes queued work."""

    async def test_turn_pipeline_writes_memory(self, tmp_path):
        extraction = FakeExtractionAdapter()
        runtime = build_runtime(
            config=make_config(tmp_path),
            embedding=FakeEmbeddingAdapter(),
            extraction=extraction,
        )
        service = runtime.memory
        message_id = service.record_user_message(
            "记住：我住在杭州", source="user_text"
        )
        await service.process_turn(message_id)
        written = await service.run_pending_extraction()

        assert written == 1
        recalled = await service.recall("我住在哪", limit=5)
        assert recalled
