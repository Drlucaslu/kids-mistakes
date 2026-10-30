"""Spaced review: day 1, 3, 7, 14, 30 after entry. Three correct tests in a row = mastered, then one
spot check 30 days later. A wrong answer sends the question back to the 1-day step."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

INTERVALS = [1, 3, 7, 14, 30]
MASTERY_STREAK = 3
SPOT_CHECK_DAYS = 30


@dataclass
class State:
    status: str          # active | mastered | archived
    stage: int
    correct_streak: int
    due_date: str | None


def first_due(on: date) -> str:
    return (on + timedelta(days=INTERVALS[0])).isoformat()


def after_test(s: State, correct: bool, on: date) -> State:
    if s.status == "mastered":
        # spot check on a mastered question: pass = done for good, fail = start over
        if correct:
            return State("mastered", s.stage, s.correct_streak + 1, None)
        return State("active", 0, 0, first_due(on))
    if not correct:
        return State("active", 0, 0, first_due(on))
    streak = s.correct_streak + 1
    if streak >= MASTERY_STREAK:
        return State("mastered", s.stage + 1, streak, (on + timedelta(days=SPOT_CHECK_DAYS)).isoformat())
    stage = min(s.stage + 1, len(INTERVALS) - 1)
    return State("active", stage, streak, (on + timedelta(days=INTERVALS[stage])).isoformat())
