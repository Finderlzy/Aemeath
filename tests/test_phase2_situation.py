"""Phase 2 tests: situation state and classroom muting.

Covers the plan's acceptance list:

* switching to class mid-playback drops queued audio
* ten further turns produce no ordinary reply audio
* the mode survives a restart
* typing in normal mode does not switch modes
* "如果我在上课，你会怎么办？" does not switch modes
* an explicit end-of-class restores voice
"""

from __future__ import annotations

import pytest

from aemeath.interfaces import SpeechMode, SituationState
from aemeath.situation import (
    SituationManager,
    SituationStore,
    detect_explicit_mode,
    detect_pause,
    is_hypothetical,
)


@pytest.fixture
def manager(tmp_path):
    """A manager backed by a temporary database."""
    store = SituationStore(tmp_path / "aemeath.sqlite3")
    return SituationManager(store)


class TestModeSwitching:
    """Explicit phrases switch modes; ambiguity does not."""

    def test_default_is_normal(self, manager):
        assert manager.state.mode is SpeechMode.NORMAL
        assert manager.state.voice_allowed

    def test_class_phrase_switches_mode(self, manager):
        ack = manager.observe_user_text("我在上课")
        assert manager.state.mode is SpeechMode.CLASS
        assert ack is not None
        assert not manager.state.voice_allowed

    @pytest.mark.parametrize(
        "phrase", ["我在上课", "我上课了", "开始上课", "我要上课", "在上课"]
    )
    def test_class_phrases_recognised(self, manager, phrase):
        manager.observe_user_text(phrase)
        assert manager.state.mode is SpeechMode.CLASS

    @pytest.mark.parametrize("phrase", ["我下课了", "下课了", "上完课了"])
    def test_normal_phrases_recognised(self, manager, phrase):
        manager.set_mode(SpeechMode.CLASS)
        manager.observe_user_text(phrase)
        assert manager.state.mode is SpeechMode.NORMAL

    def test_hypothetical_does_not_switch(self, manager):
        """The plan's specific case: a question about class is not a request."""
        ack = manager.observe_user_text("如果我在上课，你会怎么办？")
        assert manager.state.mode is SpeechMode.NORMAL
        assert ack is None

    @pytest.mark.parametrize(
        "phrase",
        [
            "如果我在上课，你会怎么办？",
            "假如我在上课呢",
            "我在上课的时候你会说话吗？",
            "要是我在上课怎么办",
            "你上课会干什么",
        ],
    )
    def test_various_hypotheticals_ignored(self, manager, phrase):
        manager.observe_user_text(phrase)
        assert manager.state.mode is SpeechMode.NORMAL

    def test_plain_typing_does_not_switch(self, manager):
        """Typing is not a request for permanent silence."""
        for phrase in ["你好", "今天天气不错", "帮我看看这个", "嗯嗯"]:
            manager.observe_user_text(phrase)
            assert manager.state.mode is SpeechMode.NORMAL

    def test_long_sentence_not_treated_as_command(self, manager):
        manager.observe_user_text(
            "我今天上午在图书馆写代码，下午再去上课，你觉得这个安排怎么样"
        )
        assert manager.state.mode is SpeechMode.NORMAL

    def test_switch_is_idempotent(self, manager):
        manager.observe_user_text("我在上课")
        ack = manager.observe_user_text("我在上课")
        assert manager.state.mode is SpeechMode.CLASS
        assert ack is None, "repeating the same mode should not acknowledge twice"


class TestPause:
    """Explicit requests to stop talking."""

    def test_pause_phrase(self, manager):
        manager.observe_user_text("先别说话")
        assert manager.state.user_paused
        assert not manager.state.voice_allowed

    def test_resume_phrase(self, manager):
        manager.observe_user_text("先别说话")
        manager.observe_user_text("可以说话了")
        assert not manager.state.user_paused
        assert manager.state.voice_allowed

    def test_hypothetical_pause_ignored(self, manager):
        manager.observe_user_text("如果你一直不说话会怎样？")
        assert not manager.state.user_paused


class TestVoiceGating:
    """Speaking is gated by mode and pause independently of input method."""

    def test_class_mode_blocks_voice(self, manager):
        manager.set_mode(SpeechMode.CLASS)
        assert not manager.should_speak()

    def test_normal_mode_allows_voice(self, manager):
        assert manager.should_speak()

    def test_paused_blocks_voice(self, manager):
        manager.set_paused(True)
        assert not manager.should_speak()

    def test_class_mode_blocks_voice_even_when_paused_then_resumed(self, manager):
        manager.set_mode(SpeechMode.CLASS)
        manager.set_paused(True)
        manager.set_paused(False)
        assert not manager.should_speak(), "resuming chat must not un-mute class"


class TestPendingAudioCancellation:
    """Entering class mode must stop audio that is already queued."""

    def test_switching_to_class_clears_pending_audio(self, manager):
        manager.register_pending_audio("turn-1")
        manager.register_pending_audio("turn-2")
        manager.set_mode(SpeechMode.CLASS)
        assert manager._pending_audio == set()

    def test_switching_to_class_from_normal_mid_playback(self, manager):
        """The plan's scenario: audio playing, user types '我在上课'."""
        manager.register_pending_audio("turn-playing")
        assert manager.should_speak()

        manager.observe_user_text("我在上课")

        assert not manager.should_speak(), "audio must stop"
        assert "turn-playing" not in manager._pending_audio


class TestPersistence:
    """State must survive a restart."""

    def test_mode_persisted_across_reload(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        first = SituationManager(SituationStore(db))
        first.observe_user_text("我在上课")

        # Simulate a process restart: brand new store over the same file.
        second = SituationManager(SituationStore(db))
        assert second.state.mode is SpeechMode.CLASS
        assert not second.state.voice_allowed

    def test_switches_persisted(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        first = SituationManager(SituationStore(db))
        first.set_microphone(True)
        first.set_screen_observation(True)
        first.set_proactive(True)

        second = SituationManager(SituationStore(db))
        assert second.state.microphone_enabled
        assert second.state.screen_observation_enabled
        assert second.state.proactive_enabled

    def test_paused_persisted(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        first = SituationManager(SituationStore(db))
        first.set_paused(True)
        second = SituationManager(SituationStore(db))
        assert second.state.user_paused

    def test_unknown_mode_value_falls_back(self, tmp_path):
        db = tmp_path / "aemeath.sqlite3"
        store = SituationStore(db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO situation_state (id, mode, updated_at) VALUES (1, 'bogus', 0)"
            )
        reloaded = SituationStore(db)
        assert reloaded.state.mode is SpeechMode.NORMAL


class TestDetectionHelpers:
    """Unit-level checks on the phrase detectors."""

    def test_hypothetical_markers(self):
        assert is_hypothetical("如果我在上课")
        assert is_hypothetical("我在上课吗？")
        assert not is_hypothetical("我在上课")

    def test_detect_explicit_mode_returns_none_for_smalltalk(self):
        assert detect_explicit_mode("今天吃什么") is None

    def test_detect_pause_resume(self):
        assert detect_pause("先别说话") is True
        assert detect_pause("可以说话了") is False

    def test_empty_input(self):
        assert detect_explicit_mode("") is None
        assert detect_pause("") is None
        assert detect_explicit_mode("   ") is None


class TestTenClassTurns:
    """Ten continued turns in class mode must produce no reply audio."""

    def test_ten_turns_no_voice(self, manager):
        manager.observe_user_text("我在上课")
        spoken = []
        for i in range(10):
            manager.observe_user_text(f"第{i}个问题")
            if manager.should_speak():
                spoken.append(i)
        assert spoken == [], "class mode must never permit voice"
        assert manager.state.mode is SpeechMode.CLASS, "mode must not drift"
