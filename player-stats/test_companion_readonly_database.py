"""Read-only archive opens must survive a read-only bind mount (WAL sidecars)."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from companion import open_readonly_database


def uri_of(path, *, shm=None, wal=None):
    """Return the URI the helper would use for this on-disk layout."""
    path = Path(path)
    for suffix, content in (("-shm", shm), ("-wal", wal)):
        side = Path(str(path) + suffix)
        if content is None:
            if side.exists():
                side.unlink()
        else:
            side.write_bytes(b"x" * content)
    with patch("companion.sqlite3.connect") as connect:
        open_readonly_database(path)
    return connect.call_args[0][0]


class ReadOnlyOpenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "stats.sqlite3"
        with closing(sqlite3.connect(str(self.path))) as db:
            db.execute("CREATE TABLE reports(sha256 TEXT)")
            db.commit()
        self.database = str(self.path)

    def test_settled_database_is_opened_immutable(self):
        # A read-only mount cannot create -shm, so the plain read-only open
        # fails exactly when the last writer checkpointed the file.
        self.assertEqual(uri_of(self.path), self.path.as_uri() + "?mode=ro&immutable=1")

    def test_live_shared_memory_keeps_plain_read_only(self):
        self.assertEqual(uri_of(self.path, shm=32768),
                         self.path.as_uri() + "?mode=ro")

    def test_uncheckpointed_wal_is_never_read_immutably(self):
        # Committed rows may still live only in the WAL; substituting an
        # immutable read would silently return stale content.
        self.assertEqual(uri_of(self.path, wal=1024), self.path.as_uri() + "?mode=ro")

    def test_settled_archive_still_reads_committed_rows(self):
        with closing(sqlite3.connect(str(self.path))) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO reports VALUES('a' * 64)")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        side = Path(str(self.path) + "-shm")
        if side.exists():  # Windows may keep the shared memory file mapped.
            self.skipTest("shared memory side file stayed open")
        with closing(open_readonly_database(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)

    def test_helper_does_not_create_files(self):
        before = sorted(os.listdir(self.temporary.name))
        with closing(open_readonly_database(self.path)) as db:
            db.execute("SELECT 1 FROM reports LIMIT 1").fetchone()
        self.assertEqual(sorted(os.listdir(self.temporary.name)), before)


if __name__ == "__main__":
    unittest.main()
