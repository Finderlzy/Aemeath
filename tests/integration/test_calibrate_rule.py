"""Regression tests for the calibration threshold rule.

The plan's acceptance rule is two-dimensional: a similarity floor qualifies
only when synonym recall **of the annotated target fact** and the unrelated
empty-result rate both reach 9/10 at the same floor. These tests pin the
three deciding scenarios: a clean separation, an overlapping distribution
that still qualifies per-question, and a scan where nothing qualifies and
the tool must report failure instead of lowering the bar.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from calibrate_memory import qualifies, select_floor  # noqa: E402


def _floor(value, synonym_hits, unrelated_clean, total=10):
    """Build one floor report the way :func:`scan_thresholds` produces it."""
    return {
        "floor": value,
        "synonym_hits": synonym_hits,
        "synonym_total": total,
        "unrelated_clean": unrelated_clean,
        "unrelated_total": total,
    }


class TestQualifies:
    """Both ratios must reach 9/10 at the same floor."""

    def test_both_at_threshold_qualifies(self):
        assert qualifies(_floor(0.42, 9, 9)) is True

    def test_one_side_short_fails(self):
        # 8/10 recall disqualifies even with a perfect empty rate.
        assert qualifies(_floor(0.42, 8, 10)) is False
        # 8/10 empty rate disqualifies even with perfect recall.
        assert qualifies(_floor(0.42, 10, 8)) is False

    def test_empty_measurement_never_qualifies(self):
        assert qualifies(_floor(0.42, 0, 0, total=0)) is False


class TestSelectFloor:
    """Selection order and the no-candidate failure mode."""

    def test_fully_separated_scan_selects_a_qualifying_floor(self):
        # Clean separation: every floor in the gap passes both sides.
        floors = [
            _floor(0.20, 10, 4),
            _floor(0.35, 10, 9),
            _floor(0.45, 10, 10),
            _floor(0.60, 6, 10),
        ]
        selected = select_floor(floors)
        assert selected is not None
        # 0.35 and 0.45 tie on both ratios; the higher floor wins.
        assert selected["floor"] == 0.45

    def test_overlapping_distribution_can_still_qualify(self):
        # Zone overlap (some unrelated question outscores some synonym
        # question) does not by itself fail the rule: what matters is the
        # per-question counts at each floor. Here 0.50 keeps 9/10 recall and
        # 9/10 empty even though the score zones overlap.
        floors = [
            _floor(0.30, 10, 3),
            _floor(0.50, 9, 9),
            _floor(0.65, 5, 10),
        ]
        selected = select_floor(floors)
        assert selected is not None
        assert selected["floor"] == 0.50

    def test_no_qualifying_floor_reports_failure(self):
        floors = [
            _floor(0.20, 10, 2),
            _floor(0.40, 8, 8),
            _floor(0.60, 4, 10),
        ]
        # No auto-lowering: the caller must report failure, not pick the
        # "best" candidate.
        assert select_floor(floors) is None

    def test_higher_recall_beats_higher_empty_rate(self):
        floors = [
            _floor(0.30, 9, 10),
            _floor(0.50, 10, 9),
        ]
        assert select_floor(floors)["floor"] == 0.50
