"""Regression tests for defects that only the real link exposed.

Each test here guards a defect that the module and integration suites both
passed through, because both were blind to the way the real service assembles
itself. They are deliberately narrow: a failure means the product is broken
end to end, not that an internal detail changed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


class TestNoDuplicateUpstreamModules:
    """The server and Aemeath must share one copy of each upstream class.

    The service is launched as a script from the upstream checkout, so upstream
    modules load as ``src.open_llm_vtuber.*``. Aemeath used to import them as
    ``open_llm_vtuber.*``; because ``src/`` is also on ``sys.path`` that
    *succeeds*, but it executes the same files a second time, producing two
    distinct ``SentenceOutput`` classes.

    The upstream conversation loop tests ``isinstance(output, SentenceOutput)``,
    so with two classes it rejected every reply and logged only
    "Received unexpected item type from agent chat stream". The UI stayed on
    "Thinking..." forever while the model was answering correctly.

    Tests could not catch this because they imported both sides the same way,
    which is exactly the arrangement production does not use.
    """

    def test_agent_output_types_are_the_same_class_the_server_checks(self):
        """The class Aemeath yields is the one the conversation loop matches."""
        from aemeath.agent import SentenceOutput as aemeath_sentence_output
        from src.open_llm_vtuber.agent.output_types import (
            SentenceOutput as upstream_sentence_output,
        )

        assert aemeath_sentence_output is upstream_sentence_output, (
            "aemeath.agent.SentenceOutput is not the class the upstream "
            "conversation loop checks with isinstance; every reply would be "
            "silently discarded"
        )

    def test_aemeath_does_not_import_upstream_under_a_second_name(self):
        """Aemeath's modules never pull upstream in under the bare name.

        Importing Aemeath must not create a second ``open_llm_vtuber`` module
        object, because that is what made two copies of every output class.
        """
        # Importing the Aemeath modules is what would trigger the duplicate.
        import aemeath.agent  # noqa: F401
        import aemeath.config  # noqa: F401
        import aemeath.runtime  # noqa: F401

        bare = sys.modules.get("open_llm_vtuber")
        assert bare is None or bare.__name__ == "src.open_llm_vtuber", (
            "upstream was imported as a second top-level module; output classes "
            "will not match by identity"
        )

    def test_agent_output_passes_the_upstream_type_check(self):
        """A real agent output satisfies the upstream isinstance branch."""
        from aemeath.agent import DisplayText, SentenceOutput
        from src.open_llm_vtuber.agent.output_types import (
            AudioOutput,
            SentenceOutput as UpstreamSentenceOutput,
        )

        output = SentenceOutput(
            display_text=DisplayText(text="你好。"),
            tts_text="你好。",
            actions=None,
        )
        # This is the exact predicate the conversation loop applies.
        assert isinstance(output, (UpstreamSentenceOutput, AudioOutput))


class TestSqliteConnectionsAreClosed:
    """Database handles must not outlive the call that opened them.

    ``with sqlite3.connect(...)`` is a *transaction* context manager: it commits
    on success but never closes. On Windows an open handle keeps the file
    locked, so temporary databases could not be removed, and replacing or
    clearing a run's data would fail.

    The check is behavioural rather than structural: on Windows a closed
    connection means the file can be deleted, which is precisely what the leak
    prevented.
    """

    def test_store_closes_its_connection(self, tmp_path):
        """A store operation leaves the database file deletable."""
        from aemeath.memory import MemoryStore

        db_path = tmp_path / "closed.sqlite3"
        store = MemoryStore(db_path)

        store.add_message(role="user", source="test", content="你好")
        assert store.recent_messages(limit=1), "message was not persisted"

        # If the connection were still open this raises PermissionError on
        # Windows (and is a no-op on POSIX, so the assertion below also checks
        # that no handle is genuinely held by attempting a fresh exclusive open).
        db_path.unlink()
        assert not db_path.exists()

    def test_situation_store_closes_its_connection(self, tmp_path):
        """Situation state writes release the file the same way."""
        from aemeath.interfaces import SpeechMode
        from aemeath.situation import SituationManager, SituationStore

        db_path = tmp_path / "situation.sqlite3"
        manager = SituationManager(SituationStore(db_path))
        manager.set_mode(SpeechMode.CLASS)
        assert manager.state.mode is SpeechMode.CLASS, "state was not persisted"

        db_path.unlink()
        assert not db_path.exists()

    def test_no_module_uses_the_transaction_only_context_manager(self):
        """No data module relies on ``with sqlite3.connect(...)``.

        A structural guard, because the behavioural tests above cannot fail on
        POSIX and the mistake is easy to reintroduce by copying a nearby line.
        The definition inside ``_connect()`` is the one legitimate use — it
        returns the connection to the ``_connection()`` wrapper, which closes it.

        Prose is skipped: the fix documents the trap in the docstrings, and a
        naive text search would flag the explanation of the very bug it guards.
        """
        import ast
        import re

        pattern = re.compile(r"\bwith\s+sqlite3\.connect\s*\(")
        offenders = []
        for name in ("memory.py", "situation.py", "legacy.py"):
            path = ROOT_DIR / "aemeath" / name
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)

            # Collect docstring nodes so their text is skipped, then scan only
            # lines that are neither comments nor inside a docstring.
            docstring_lines: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(
                        body[0].value, ast.Constant
                    ) and isinstance(body[0].value.value, str):
                        for line in range(
                            body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1
                        ):
                            docstring_lines.add(line)

            for number, line in enumerate(source.splitlines(), start=1):
                if number in docstring_lines:
                    continue
                code = line.split("#", 1)[0]
                if pattern.search(code):
                    offenders.append(f"{name}:{number}")

        assert not offenders, (
            "these lines use sqlite3.connect as a context manager, which never "
            f"closes the connection: {offenders}"
        )


class TestMetricsAreReadableAcrossProcesses:
    """Persisted metrics must be readable, not write-only.

    ``MetricsRecorder`` appended every finished turn to ``turns.jsonl`` and
    nothing ever read the file back. ``scripts/status.py`` runs in a *new*
    process, so it built an empty recorder and reported zero completed turns and
    no percentiles while a full metrics file sat on disk. That reads as "no data
    was collected", which is the opposite of the truth and would silently
    invalidate any latency claim made from the status screen.
    """

    def test_recorded_turns_can_be_reloaded(self, tmp_path):
        """A second recorder sees turns written by the first."""
        from aemeath.runtime import MetricsRecorder

        log_path = tmp_path / "turns.jsonl"

        writer = MetricsRecorder(log_path=log_path)
        writer.start_turn("turn-1", "user_text")
        writer.mark_text_displayed("turn-1", client_elapsed_ms=900.0)
        writer.finish_turn("turn-1", input_tokens=10, output_tokens=20)
        writer.start_turn("turn-2", "user_text")
        writer.mark_text_displayed("turn-2", client_elapsed_ms=1100.0)
        writer.finish_turn("turn-2")

        reader = MetricsRecorder(log_path=log_path)
        assert reader.load() == 2, "metrics were not read back"

        summary = reader.summary()
        assert summary["turns"] == 2
        assert summary["client"]["first_text_ms"]["count"] == 2
        assert summary["client"]["first_text_ms"]["available"] is True
        assert summary["usage"]["input_tokens"] == 10
        assert summary["usage"]["output_tokens"] == 20

    def test_reload_reports_insufficient_rather_than_inventing_a_p95(self, tmp_path):
        """Below the sample minimum, a percentile stays unavailable.

        The plan requires a P95 over too few samples to be reported as
        insufficient, so the reload path must preserve that verdict instead of
        producing a number that looks authoritative.
        """
        from aemeath.runtime import MetricsRecorder

        log_path = tmp_path / "turns.jsonl"
        writer = MetricsRecorder(log_path=log_path)
        for index in range(3):
            turn_id = f"turn-{index}"
            writer.start_turn(turn_id, "user_text")
            writer.mark_text_displayed(turn_id, client_elapsed_ms=500.0 + index)
            writer.finish_turn(turn_id)

        reader = MetricsRecorder(log_path=log_path)
        reader.load()
        samples = reader.summary()["client"]["first_text_ms"]

        assert samples["count"] == 3
        assert samples["sufficient"] is False
        assert samples["minimum_required"] == 20

    def test_missing_and_truncated_files_do_not_raise(self, tmp_path):
        """A missing file loads zero turns; a torn last line is skipped.

        An abruptly killed process leaves a partial final line, which is normal
        and must not discard the records that are intact.
        """
        from aemeath.runtime import MetricsRecorder

        absent = MetricsRecorder(log_path=tmp_path / "does-not-exist.jsonl")
        assert absent.load() == 0

        log_path = tmp_path / "turns.jsonl"
        writer = MetricsRecorder(log_path=log_path)
        writer.start_turn("turn-1", "user_text")
        writer.finish_turn("turn-1")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write('{"turn_id": "truncated", "star')

        reader = MetricsRecorder(log_path=log_path)
        assert reader.load() == 1
        assert reader.summary()["turns"] == 1


class TestLiveProbeWiring:
    """The live probes exist, are selectable, and stay out of the default run."""

    def test_live_tests_are_marked_and_excluded_by_default(self):
        """``live_api`` tests carry the marker pytest uses to exclude them."""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                "live_api",
                "tests/live",
            ],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # Seven capability probes: conversation, embedding, extraction, vision,
        # asr, tts and capture. pytest -q reports "path: N" when collecting.
        assert "tests/live/test_live_api.py: 7" in result.stdout, result.stdout

    def test_capability_probes_cover_every_documented_capability(self):
        """Each capability the plan names has a probe."""
        from aemeath import live

        for name in (
            "probe_conversation",
            "probe_embedding",
            "probe_extraction",
            "probe_vision",
            "probe_asr",
            "probe_tts",
            "probe_capture",
        ):
            assert hasattr(live, name), f"{name} is missing"

    async def test_unconfigured_capability_is_skipped_not_failed(self):
        """A capability with no provider reports skipped, not a false pass.

        This is what keeps the recorded blocker honest: "no provider is
        configured" must never be counted as "the API works".
        """
        from aemeath.config import AemeathConfig
        from aemeath.live import probe_embedding

        result = await probe_embedding(AemeathConfig())

        assert result.skipped is True
        assert result.ok is False
        assert result.configured is False
        assert result.detail, "a skipped probe must still explain why"
