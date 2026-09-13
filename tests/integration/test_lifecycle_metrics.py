"""Integration tests: connection lifecycle and metrics.

The plan's remaining scenarios:

* reconnecting does not duplicate scheduling, does not send output to the old
  connection, and background tasks are released on exit;
* only one client is active — a new connection takes over and the old one can
  no longer produce valid output;
* client-reported experience metrics are recorded separately from backend
  generation metrics, a missing receipt reports as unavailable rather than as
  zero, and P95 uses the nearest-rank rule with an explicit sample-count
  verdict.
"""

from __future__ import annotations

import asyncio

import pytest

from aemeath.interfaces import EventSource
from aemeath.runtime import MetricsRecorder, SampleSet
from tests.doubles import FakeLLM
from tests.integration.harness import FakeWebSocket
from tests.integration.harness import run_turn


class TestConnectionLifecycle:
    """One active client, and a clean release on exit."""

    async def test_second_connection_takes_over(self, make_harness):
        """A new connection supersedes the old one."""
        harness = make_harness(llm=FakeLLM(["你好。"]))
        first_generation = harness.bridge.generation

        second_socket = FakeWebSocket()
        session = harness.bridge.attach_client(
            second_socket.send_text, client_uid="second-client"
        )

        assert session.generation > first_generation
        assert harness.bridge.session.client_uid == "second-client"
        assert harness.bridge._is_current(session.generation) is True
        assert harness.bridge._is_current(first_generation) is False

    async def test_old_connection_stops_receiving_output(self, make_harness):
        """After takeover, the old socket must not receive new frames."""
        harness = make_harness(llm=FakeLLM(["新回复。"]))
        old_socket = harness.websocket
        old_socket.clear()

        new_socket = FakeWebSocket()
        harness.bridge.attach_client(new_socket.send_text, client_uid="second-client")
        new_socket.clear()

        await run_turn(harness, "你好", socket=new_socket)

        assert new_socket.frames_of("aemeath-text"), "new client receives output"
        assert old_socket.frames_of("aemeath-text") == [], "old client must be silent"

    async def test_takeover_cancels_old_turn(self, make_harness):
        """The previous connection's in-flight turn is cancelled."""
        harness = make_harness(llm=FakeLLM(["慢。"]))
        turn = harness.bridge._coordinator.begin_turn(EventSource.USER_TEXT)
        turn_id = str(turn.turn_id)

        from aemeath.interfaces import TurnId

        new_socket = FakeWebSocket()
        harness.bridge.attach_client(new_socket.send_text, client_uid="second")

        assert harness.bridge._coordinator.is_cancelled(TurnId(turn_id))

    async def test_disconnect_stops_output_and_reports_disconnected(
        self, make_harness
    ):
        """Detaching marks the client disconnected so proactive stays quiet."""
        harness = make_harness()
        await harness.bridge.set_switch("proactive", True)
        assert harness.bridge._coordinator.client_connected is True

        harness.bridge.detach_client()

        assert harness.bridge._coordinator.client_connected is False
        decision = await harness.bridge._coordinator.consider_proactive()
        assert decision.eligible is False
        assert "disconnected" in decision.reason

    async def test_background_tasks_start_and_stop(self, make_harness):
        """The runtime starts its tasks and releases them on stop."""
        harness = make_harness()
        runtime = harness.runtime

        await runtime.start()
        assert set(runtime.tasks) == {"memory", "proactive"}
        assert all(not task.done() for task in runtime.tasks.values())

        await runtime.stop()
        assert runtime.tasks == {}, "tasks must be released on shutdown"
        assert runtime.bridge.session is None

    async def test_factory_starts_runtime_background_tasks(self, tmp_path):
        """Building the agent through the real factory must start the workers.

        Previously the factory created the runtime but never called ``start()``,
        so in the real server no memory worker and no proactive timer existed.
        The gap was invisible because every other test called ``start()`` by
        hand, which no production code path did.
        """
        import asyncio as _asyncio

        import yaml

        from aemeath.config import load_config
        from aemeath.runtime import get_runtime, reset_runtime
        from tests.integration.harness import (
            build_config_document,
            load_validated_config,
        )

        data_dir = tmp_path / "data"
        log_dir = tmp_path / "logs"
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        document = build_config_document(data_dir=data_dir, log_dir=log_dir)
        config_path = tmp_path / "conf.factory.yaml"
        config_path.write_text(
            yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
        )

        validated = load_validated_config(config_path)
        aemeath_config = load_config(config_path)

        reset_runtime()
        try:
            from src.open_llm_vtuber.agent.agent_factory import AgentFactory

            character_config = validated.character_config
            # Called inside a running loop, exactly as the server does.
            AgentFactory.create_agent(
                conversation_agent_choice=(
                    character_config.agent_config.conversation_agent_choice
                ),
                agent_settings=(
                    character_config.agent_config.agent_settings.model_dump()
                ),
                llm_configs=character_config.agent_config.llm_configs.model_dump(),
                system_prompt=character_config.persona_prompt,
                live2d_model=None,
                tts_preprocessor_config=character_config.tts_preprocessor_config,
                aemeath_config=aemeath_config,
            )
            # Let the scheduled start coroutine run.
            await _asyncio.sleep(0)

            runtime = get_runtime()
            assert set(runtime.tasks) == {"memory", "proactive"}, (
                "creating the agent through the factory must start the "
                "runtime's background workers"
            )
            await runtime.stop()
        finally:
            reset_runtime()

    async def test_start_is_idempotent(self, make_harness):
        """Starting twice must not duplicate the background work."""
        harness = make_harness()
        runtime = harness.runtime

        await runtime.start()
        tasks = dict(runtime.tasks)
        await runtime.start()

        assert runtime.tasks.keys() == tasks.keys()
        assert runtime.tasks["memory"] is tasks["memory"], "no second worker started"
        await runtime.stop()

    async def test_stop_is_safe_without_start(self, make_harness):
        """Stopping a runtime that never started must not raise."""
        harness = make_harness()
        await harness.runtime.stop()

    async def test_reconnect_does_not_duplicate_tasks(self, make_harness):
        """Reconnecting a client must not start another pair of tasks."""
        harness = make_harness()
        runtime = harness.runtime
        await runtime.start()
        original = dict(runtime.tasks)

        runtime.bridge.detach_client()
        new_socket = FakeWebSocket()
        runtime.bridge.attach_client(new_socket.send_text, client_uid="again")

        assert runtime.tasks == original, "reconnect started duplicate tasks"
        await runtime.stop()


class TestMetricsSeparation:
    """Client experience metrics and backend metrics stay distinct."""

    def test_backend_metrics_use_monotonic_clock(self):
        """Backend stage timings are measured, not wall-clock subtracted."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.mark_first_text("t1")
        recorder.finish_turn("t1")

        metric = recorder.turns[0]
        assert metric.first_text_ms is not None
        assert metric.first_text_ms >= 0.0

    def test_client_receipts_recorded_separately(self):
        """A client duration lands in the client fields, not the backend ones."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")

        recorder.mark_text_displayed("t1", client_elapsed_ms=1234.0)
        recorder.mark_playback_started("t1", client_elapsed_ms=5678.0)
        recorder.mark_cancel_complete("t1", client_elapsed_ms=250.0)
        recorder.finish_turn("t1")

        metric = recorder.turns[0]
        assert metric.client_first_text_ms == 1234.0
        assert metric.client_playback_start_ms == 5678.0
        assert metric.client_cancel_ms == 250.0
        # The backend timings were never marked, so they stay unknown.
        assert metric.first_text_ms is None
        assert metric.first_audio_ms is None

    def test_missing_receipt_reports_unavailable_not_zero(self):
        """Without a receipt the metric is unavailable, never zero."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1")

        summary = recorder.summary()
        client_text = summary["client"]["first_text_ms"]
        assert client_text["available"] is False
        assert client_text["p95"] is None
        assert client_text["count"] == 0

    def test_backend_and_client_reported_separately(self):
        """The summary keeps the two families in their own sections."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.mark_first_text("t1")
        recorder.mark_text_displayed("t1", client_elapsed_ms=999.0)
        recorder.finish_turn("t1")

        summary = recorder.summary()
        assert summary["backend"]["first_text_ms"]["available"] is True
        assert summary["client"]["first_text_ms"]["available"] is True
        assert "backend" in summary and "client" in summary

    def test_p95_uses_nearest_rank(self):
        """P95 is the ceil(0.95 x n)-th ordered sample."""
        samples = SampleSet()
        for value in range(1, 101):
            samples.add(float(value))
        # ceil(0.95 * 100) = 95 -> the 95th ordered value.
        assert samples.percentile(0.95) == 95.0

    def test_p95_reports_insufficient_samples(self):
        """Fewer than 20 samples is reported as insufficient."""
        samples = SampleSet()
        for value in range(1, 20):
            samples.add(float(value))
        assert samples.sufficient is False
        assert samples.to_dict()["sufficient"] is False

        samples.add(20.0)
        assert samples.sufficient is True

    def test_cancelled_and_failed_turns_counted_separately(self):
        """Cancellations and errors have their own counters."""
        recorder = MetricsRecorder()
        recorder.start_turn("ok", "user_text")
        recorder.finish_turn("ok")
        recorder.start_turn("cancel", "user_text")
        recorder.finish_turn("cancel", cancelled=True)
        recorder.start_turn("fail", "user_text")
        recorder.finish_turn("fail", error="provider down")

        summary = recorder.summary()
        assert summary["turns"] == 3
        assert summary["cancelled"] == 1
        assert summary["errors"] == 1

    def test_finish_is_idempotent(self):
        """Ending a turn twice must not double-count it."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1")
        recorder.finish_turn("t1", cancelled=True)

        assert recorder.summary()["turns"] == 1
        assert recorder.summary()["cancelled"] == 0

    def test_unknown_token_usage_stays_unknown(self):
        """A provider that reports no usage must not be recorded as zero."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.finish_turn("t1")  # no token counts supplied

        usage = recorder.summary()["usage"]
        assert usage["unknown_usage_turns"] == 1
        assert usage["usage_complete"] is False

    def test_generation_end_is_separate_from_playback_end(self):
        """Generation finishing does not imply playback finished."""
        recorder = MetricsRecorder()
        recorder.start_turn("t1", "user_text")
        recorder.mark_generation_finished("t1")

        metric = recorder.turns[0]
        assert metric.generation_finished is True
        assert metric.playback_finished is False


class TestMetricsThroughBridge:
    """The bridge records receipts that arrive from the client."""

    async def test_receipts_update_the_recorder(self, make_harness):
        """Receipts delivered through the bridge reach the metrics recorder."""
        harness = make_harness(llm=FakeLLM(["带指标的回复。"]))
        harness.websocket.clear()

        await run_turn(harness, "你好")

        # Find the turn the bridge opened and feed it a receipt.
        turn_ids = [
            frame["turn_id"] for frame in harness.websocket.frames_of("aemeath-text")
        ]
        assert turn_ids
        turn_id = turn_ids[0]

        await harness.bridge.on_display_text_shown(
            turn_id=turn_id, client_elapsed_ms=1500.0
        )
        await harness.bridge.on_playback_started(
            turn_id=turn_id, audio_slice_id="slice-1", client_elapsed_ms=4000.0
        )
        await harness.bridge.on_cancel_complete(
            turn_id=turn_id, client_elapsed_ms=300.0
        )

        metric = harness.runtime.metrics._find(turn_id)
        assert metric is not None
        assert metric.client_first_text_ms == 1500.0
        assert metric.client_playback_start_ms == 4000.0
        assert metric.client_cancel_ms == 300.0

    async def test_turn_is_finished_exactly_once_through_pipeline(self, make_harness):
        """A pipeline turn is counted once, with backend text timing present."""
        harness = make_harness(llm=FakeLLM(["你好呀。"]))
        await run_turn(harness, "你好")

        summary = harness.runtime.metrics.summary()
        assert summary["turns"] == 1
        assert summary["backend"]["first_text_ms"]["count"] == 1
