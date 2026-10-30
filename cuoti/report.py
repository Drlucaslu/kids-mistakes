"""Weekly report numbers per child × subject."""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta

from . import db


def week_bounds(offset: int = 0) -> tuple[date, date]:
    """Monday..Sunday of the current week (offset -1 = last week)."""
    t = db.now().date()
    start = t - timedelta(days=t.weekday()) + timedelta(weeks=offset)
    return start, start + timedelta(days=6)


def child_subject(c: sqlite3.Connection, child_id: int, subject: str, start: date, end: date) -> dict:
    s, e = start.isoformat(), (end + timedelta(days=1)).isoformat()
    base = "FROM mistakes WHERE child_id = ? AND subject = ? AND approved = 1"
    args = (child_id, subject)
    new = c.execute(f"SELECT COUNT(*) {base} AND source = 'tutor' AND created_at >= ? AND created_at < ?",
                    (*args, s, e)).fetchone()[0]
    mastered = c.execute(f"SELECT COUNT(*) {base} AND status = 'mastered' AND mastered_at >= ? AND mastered_at < ?",
                         (*args, s, e)).fetchone()[0]
    open_ = c.execute(f"SELECT COUNT(*) {base} AND status = 'active'", args).fetchone()[0]
    total_mastered = c.execute(f"SELECT COUNT(*) {base} AND status = 'mastered'", args).fetchone()[0]
    tests = c.execute(
        """SELECT r.correct, r.due_date, r.created_at FROM reviews r JOIN mistakes m ON m.id = r.mistake_id
           WHERE m.child_id = ? AND m.subject = ? AND r.kind = 'test' AND r.created_at >= ? AND r.created_at < ?""",
        (*args, s, e)).fetchall()
    on_time = sum(1 for r in tests if not r["due_date"] or r["created_at"][:10] <= r["due_date"])
    correct = sum(1 for r in tests if r["correct"])
    # reviews that were due this week but never happened also count against "on time"
    missed = c.execute(f"SELECT COUNT(*) {base} AND status != 'archived' AND due_date >= ? AND due_date < ? "
                       "AND due_date < ?", (*args, s, e, db.today())).fetchone()[0]
    causes = Counter()
    topics = Counter()
    for r in c.execute(f"SELECT causes, topic {base} AND status = 'active' AND source = 'tutor'", args):
        causes.update(db.split(r["causes"]))
        if r["topic"]:
            topics[r["topic"]] += 1
    tasks = c.execute("SELECT status FROM tasks WHERE child_id = ? AND subject = ? AND status != 'cancelled' "
                      "AND due_date >= ? AND due_date <= ?", (*args, s, end.isoformat())).fetchall()
    tasks_done = sum(1 for t in tasks if t["status"] in ("done", "confirmed"))
    due_total = len(tests) + missed
    return {
        "new": new, "mastered": mastered, "open": open_, "total_mastered": total_mastered,
        "tests": len(tests), "correct": correct,
        "accuracy": round(100 * correct / len(tests)) if tests else None,
        "on_time": round(100 * on_time / due_total) if due_total else None,
        "missed": missed,
        "causes": causes.most_common(3), "topics": topics.most_common(5),
        "tasks": len(tasks), "tasks_done": tasks_done,
        "task_rate": round(100 * tasks_done / len(tasks)) if tasks else None,
    }
