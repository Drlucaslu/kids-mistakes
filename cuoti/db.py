"""SQLite storage. One file, a handful of tables, plain SQL."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import get_settings

SUBJECTS = {"chinese": "语文", "english": "英语", "math": "数学"}

DEFAULT_CAUSES = ["粗心", "概念不懂", "审题不清", "计算错误", "词汇/字词", "不会表达", "方法不对", "没做完"]
DEFAULT_CAUSES_EN = ["Careless", "Concept unclear", "Misread question", "Calculation error", "Vocabulary",
                     "Hard to express", "Wrong method", "Ran out of time"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('parent','tutor','child')),
  subjects TEXT NOT NULL DEFAULT '',          -- comma list, tutors only
  grade INTEGER,                              -- children only
  pin_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mistakes (
  id INTEGER PRIMARY KEY,
  child_id INTEGER NOT NULL REFERENCES users(id),
  subject TEXT NOT NULL,
  topic TEXT NOT NULL DEFAULT '',
  causes TEXT NOT NULL DEFAULT '',            -- comma list
  question_text TEXT NOT NULL DEFAULT '',
  child_answer TEXT NOT NULL DEFAULT '',
  answer_text TEXT NOT NULL DEFAULT '',       -- correct answer / explanation
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','mastered','archived')),
  stage INTEGER NOT NULL DEFAULT 0,
  correct_streak INTEGER NOT NULL DEFAULT 0,
  due_date TEXT,                              -- YYYY-MM-DD, NULL = nothing scheduled
  parent_id INTEGER REFERENCES mistakes(id),  -- set on variant questions
  source TEXT NOT NULL DEFAULT 'tutor' CHECK (source IN ('tutor','ai_variant')),
  approved INTEGER NOT NULL DEFAULT 1,        -- AI variants wait for a tutor before entering review
  ai_status TEXT NOT NULL DEFAULT '',         -- OCR: '', pending, done, failed
  ai_error TEXT NOT NULL DEFAULT '',
  variant_status TEXT NOT NULL DEFAULT '',    -- '', pending, failed:<msg>  (AI variant generation)
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  mastered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_mistakes_child ON mistakes(child_id, subject, status);
CREATE INDEX IF NOT EXISTS ix_mistakes_due ON mistakes(due_date);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  mistake_id INTEGER NOT NULL REFERENCES mistakes(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  mistake_id INTEGER NOT NULL REFERENCES mistakes(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'test' CHECK (kind IN ('test','practice')),  -- practice = child self-practice
  correct INTEGER,                            -- NULL for practice
  due_date TEXT,                              -- what the schedule asked for, for on-time stats
  note TEXT NOT NULL DEFAULT '',
  by_user INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_mistake ON reviews(mistake_id);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  child_id INTEGER NOT NULL REFERENCES users(id),
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  due_date TEXT NOT NULL,
  repeat TEXT NOT NULL DEFAULT 'none' CHECK (repeat IN ('none','daily','weekly')),
  status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','done','confirmed','cancelled')),
  done_at TEXT,
  confirmed_at TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_child ON tasks(child_id, status, due_date);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def today() -> str:
    return now().date().isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


def kv_get(c: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(c: sqlite3.Connection, key: str, value: Any) -> None:
    c.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, json.dumps(value, ensure_ascii=False)))


def default_causes() -> list[str]:
    from . import i18n
    return DEFAULT_CAUSES_EN if i18n.lang() == "en" else DEFAULT_CAUSES


def causes(c: sqlite3.Connection) -> list[str]:
    return kv_get(c, "causes", None) or default_causes()


def topics(c: sqlite3.Connection, subject: str) -> list[str]:
    """Topic library for a subject: the configured list plus everything already used, most used first."""
    configured = kv_get(c, f"topics:{subject}", [])
    used = [r["topic"] for r in c.execute(
        "SELECT topic, COUNT(*) n FROM mistakes WHERE subject = ? AND topic != '' GROUP BY topic ORDER BY n DESC",
        (subject,))]
    return list(dict.fromkeys(used + configured))


def split(s: str | None) -> list[str]:
    return [x for x in (s or "").split(",") if x]
