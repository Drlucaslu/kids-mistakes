from datetime import date

from cuoti.schedule import State, after_test, first_due

D = date(2026, 11, 2)


def test_first_due_is_tomorrow():
    assert first_due(D) == "2026-11-03"


def test_three_correct_in_a_row_masters_with_spot_check():
    s = State("active", 0, 0, first_due(D))
    s = after_test(s, True, D)
    assert (s.status, s.stage, s.correct_streak, s.due_date) == ("active", 1, 1, "2026-11-05")
    s = after_test(s, True, date(2026, 11, 5))
    assert (s.stage, s.due_date) == (2, "2026-11-12")
    s = after_test(s, True, date(2026, 11, 12))
    assert s.status == "mastered" and s.due_date == "2026-12-12"


def test_wrong_resets_to_one_day():
    s = State("active", 2, 2, "2026-11-12")
    s = after_test(s, False, D)
    assert (s.status, s.stage, s.correct_streak, s.due_date) == ("active", 0, 0, "2026-11-03")


def test_spot_check():
    m = State("mastered", 3, 3, "2026-12-12")
    passed = after_test(m, True, date(2026, 12, 12))
    assert passed.status == "mastered" and passed.due_date is None
    failed = after_test(m, False, date(2026, 12, 12))
    assert failed.status == "active" and failed.stage == 0
