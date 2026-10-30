"""Daily SQLite backup into DATA_DIR/backups (visible in Olares Files under Data/<app>/backups). Keeps 30."""
from __future__ import annotations

import logging
import sqlite3
import time

from . import db
from .config import get_settings

KEEP = 30
log = logging.getLogger("cuoti.backup")


def run(force: bool = False) -> str:
    s = get_settings()
    name = f"cuoti-{db.today()}.db"
    target = s.backup_dir / name
    if target.exists() and not force:
        return name
    src = db.connect()
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(s.backup_dir.glob("cuoti-*.db"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return name


def list_backups() -> list[str]:
    return sorted((p.name for p in get_settings().backup_dir.glob("cuoti-*.db")), reverse=True)[:5]


def loop() -> None:
    while True:
        try:
            run()
        except Exception:
            log.exception("backup failed")
        time.sleep(3600)
