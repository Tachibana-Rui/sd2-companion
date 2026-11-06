"""Read-only SD2 player companion; importing never attaches to a game."""

import sqlite3
from pathlib import Path

__version__ = "0.5.3"
UPDATE_URL = "https://sd2.rui-blog.link/downloads/sd2-companion/update.json"


def open_readonly_database(path):
    """Open an existing database that may sit on a read-only bind mount.

    A WAL database needs writable ``-shm``/``-wal`` side files, and a read-only
    bind mount cannot create them; a plain ``mode=ro`` open then fails with
    "unable to open database file". When both side files are absent the last
    writer has checkpointed the file, so an immutable connection reads exactly
    the same committed content without touching the directory. A live WAL is
    never read immutably: the caller still reports the archive as unavailable
    and retries instead of returning stale rows.
    """
    target = Path(path)
    wal = Path(str(target) + "-wal")
    shm = Path(str(target) + "-shm")
    settled = not shm.exists() and (not wal.exists() or wal.stat().st_size == 0)
    uri = target.as_uri() + ("?mode=ro&immutable=1" if settled else "?mode=ro")
    return sqlite3.connect(uri, uri=True, timeout=5)


