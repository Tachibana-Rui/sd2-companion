"""Independent room archives must agree before an inbox report is bound."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import companion_receiver
from companion.receiver import CompositeArchiveIndex, ReceiverStore
from companion.server_sessions import ServerObservation, ServerSessionLedger
from test_companion_receiver import native_archive, native_live_payload, replay_payload


class MultiArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-multi-archive-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.first = self.base / "first.sqlite3"
        self.second = self.base / "second.sqlite3"
        self.inbox = self.base / "inbox.sqlite3"

    def index(self):
        return CompositeArchiveIndex([self.first, self.second])

    def saved(self):
        with contextlib.closing(sqlite3.connect(self.inbox)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM submissions").fetchone())

    def empty_archive(self, path, *, sources=True):
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)")
            if sources:
                db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")

    def test_replay_matches_second_room_without_falling_back_to_first_session(self):
        native_archive(self.first, sha="d" * 64, room="first-room")
        native_archive(self.second, room="second-room")
        before = [path.read_bytes() for path in (self.first, self.second)]
        for paths in ((self.first, self.second), (self.second, self.first)):
            index = CompositeArchiveIndex(paths)
            self.assertEqual(index(replay_payload())["server_sha256"], "c" * 64)
            self.assertEqual(index(replay_payload())["archive_rooms"], ["second-room"])
        store = ReceiverStore(self.inbox, archive_index=self.index())
        self.assertEqual(store.submit(replay_payload())["status"], "accepted")
        self.assertEqual(self.saved()["room_hint"], "second-room")
        self.assertEqual(self.saved()["server_sha256"], "c" * 64)
        self.assertEqual(self.saved()["binding_evidence"], "exact_replay_sha256_archive")
        self.assertEqual(before, [path.read_bytes() for path in (self.first, self.second)])

    def test_invalid_exact_replay_never_falls_back_to_other_room_session(self):
        native_archive(self.first, sha="d" * 64, room="first-room")
        native_archive(self.second, room="second-room", build=100)
        for paths in ((self.first, self.second), (self.second, self.first)):
            self.assertEqual(CompositeArchiveIndex(paths)(replay_payload()),
                             {"pending_reason": "archive_replay_metadata_mismatch"})

    def test_replay_session_fallback_stays_ambiguous_without_any_exact_bytes(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, sha="d" * 64, room="second-room")
        for paths in ((self.first, self.second), (self.second, self.first)):
            self.assertEqual(CompositeArchiveIndex(paths)(replay_payload(sha="f" * 64)),
                             {"pending_reason": "archive_match_ambiguous"})

    def test_disappearing_exact_row_does_not_turn_into_a_session_fallback(self):
        native_archive(self.first, sha="d" * 64, room="first-room")
        native_archive(self.second, room="second-room")
        index = self.index()
        original = index._has_candidate
        def remove_after_probe(path, report, *, exact_only=False):
            found = original(path, report, exact_only=exact_only)
            if exact_only and Path(path) == self.second.resolve():
                with contextlib.closing(sqlite3.connect(self.second)) as db, db:
                    db.execute("DELETE FROM reports WHERE sha256=?", ("c" * 64,))
            return found
        with mock.patch.object(index, '_has_candidate', side_effect=remove_after_probe):
            self.assertEqual(index(replay_payload()), {"pending_reason": "replay_sha256_not_analyzed"})

    def test_native_session_different_hash_matches_are_ambiguous_in_either_order(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, sha="d" * 64, room="second-room")
        for paths in ([self.first, self.second], [self.second, self.first]):
            index = CompositeArchiveIndex(paths)
            self.assertEqual(index(native_live_payload()), {"pending_reason": "server_session_archive_ambiguous"})

    def test_same_hash_in_two_rooms_is_one_match_with_both_sources(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, room="second-room")
        store = ReceiverStore(self.inbox, archive_index=self.index())
        self.assertEqual(store.submit(native_live_payload())["status"], "accepted")
        saved = self.saved()
        self.assertEqual(saved["room_hint"], "")
        self.assertEqual(json.loads(saved["archive_rooms"]), ["first-room", "second-room"])
        self.assertEqual(saved["map"], "fixture-map")
        self.assertEqual(saved["server_state_verified"], 0)

    def test_explicit_room_excludes_different_room_session(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, sha="d" * 64, room="second-room")
        report = native_live_payload()
        report["room_hint"] = "second-room"
        self.assertEqual(self.index()(report)["server_sha256"], "d" * 64)

    def test_conflicting_native_session_metadata_prevents_other_room_match(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, sha="d" * 64, room="second-room", players=[{"id": "456", "alliance": 0}])
        store = ReceiverStore(self.inbox, archive_index=self.index())
        self.assertEqual(store.submit(native_live_payload())["status"], "pending_match")
        self.assertIsNone(self.saved()["server_sha256"])

    def test_same_replay_hash_with_conflicting_archive_metadata_stays_pending(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, room="second-room", build=100)
        result = self.index()(replay_payload())
        self.assertEqual(result, {"pending_reason": "archive_replay_metadata_mismatch"})

    def test_native_map_conflict_is_rejected_even_without_a_client_map(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, room="second-room")
        with contextlib.closing(sqlite3.connect(self.second)) as db, db:
            document = json.loads(db.execute("SELECT payload FROM reports").fetchone()[0])
            document["match"]["map"] = "other-map"
            db.execute("UPDATE reports SET payload=?", (json.dumps(document),))
        self.assertEqual(self.index()(native_live_payload()), {"pending_reason": "archive_metadata_conflict"})

    def test_two_room_live_observations_do_not_select_one_server_epoch(self):
        native_archive(self.first, room="first-room")
        native_archive(self.second, room="second-room")
        now = [100]
        ledger = ServerSessionLedger(self.base / "ledger.sqlite3", clock=lambda: now[0])
        for room, epoch in (("first-room", "a" * 64), ("second-room", "b" * 64)):
            ledger.observe(ServerObservation(room, epoch, "fixture-session", 53, "fixture-map", 168061,
                                             (("123", 0),), now[0], 1, "server_native_game_setup"))
        now[0] = 101
        for room, epoch in (("first-room", "a" * 64), ("second-room", "b" * 64)):
            ledger.observe(ServerObservation(room, epoch, "fixture-session", 101, "fixture-map", 168061,
                                             (("123", 0),), now[0], 2, "server_native_game_setup"))
        index = CompositeArchiveIndex([self.first, self.second], server_sessions=ledger)
        self.assertEqual(index(native_live_payload()), {"pending_reason": "server_live_state_ambiguous"})

    def test_unfinished_exact_hash_prevents_other_archive_match(self):
        native_archive(self.first)
        native_archive(self.second, finished=False)
        self.assertEqual(self.index()(replay_payload()), {"pending_reason": "archive_replay_not_finished"})

    def test_removed_archive_prevents_binding_and_is_not_recreated(self):
        native_archive(self.first)
        self.empty_archive(self.second)
        index = self.index()
        self.second.unlink()
        self.assertEqual(index(replay_payload()), {"pending_reason": "archive_temporarily_unavailable"})
        self.assertFalse(self.second.exists())

    def test_corrupt_archive_prevents_other_archive_match(self):
        native_archive(self.first)
        self.second.write_bytes(b"not a sqlite archive")
        self.assertEqual(self.index()(replay_payload()), {"pending_reason": "archive_temporarily_unavailable"})

    def test_empty_pre_migration_room_does_not_require_creating_source_table(self):
        native_archive(self.first)
        self.empty_archive(self.second, sources=False)
        before = self.second.read_bytes()
        self.assertEqual(self.index()(native_live_payload())["server_sha256"], "c" * 64)
        self.assertEqual(self.second.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(self.second)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='report_sources'").fetchone())

    def test_nonempty_archive_without_sources_is_not_ignored(self):
        native_archive(self.first)
        self.empty_archive(self.second, sources=False)
        with contextlib.closing(sqlite3.connect(self.second)) as db, db:
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", ("e" * 64, "other-session", "{}", "finished"))
        self.assertEqual(self.index()(replay_payload()), {"pending_reason": "archive_temporarily_unavailable"})

    def test_later_second_room_report_is_found_by_reconciliation(self):
        self.empty_archive(self.first)
        self.empty_archive(self.second)
        store = ReceiverStore(self.inbox, archive_index=self.index())
        report = replay_payload()
        self.assertEqual(store.submit(report)["status"], "pending_match")
        native_archive(self.second, room="second-room")
        self.assertEqual(store.reconcile(), {"checked": 1, "accepted": 1})
        self.assertEqual(self.saved()["room_hint"], "second-room")
        self.assertEqual(json.loads(self.saved()["payload"]), report)

    def test_duplicate_path_is_read_once_and_archive_cannot_be_inbox(self):
        native_archive(self.first)
        index = CompositeArchiveIndex([self.first, self.first])
        self.assertEqual(index.paths, (self.first.resolve(),))
        before = self.first.read_bytes()
        with self.assertRaisesRegex(ValueError, "receiver_database_must_be_separate"):
            ReceiverStore(self.first, archive_index=index)
        self.assertEqual(before, self.first.read_bytes())

    def test_cli_accepts_repeated_archive_arguments_and_single_archive_remains_supported(self):
        native_archive(self.first)
        self.empty_archive(self.second)
        for archives in ([self.first], [self.first, self.second]):
            server = mock.Mock(server_port=8770)
            server.serve_forever.side_effect = KeyboardInterrupt
            with mock.patch.dict(os.environ, {"SD2_COMPANION_TOKEN": "fixture-token"}), \
                    mock.patch.object(companion_receiver, "create_server", return_value=server) as create, \
                    contextlib.redirect_stdout(io.StringIO()):
                arguments = ["--database", str(self.inbox)]
                for archive in archives:
                    arguments.extend(["--archive-db", str(archive)])
                self.assertEqual(companion_receiver.main(arguments), 0)
                index = create.call_args.args[0].archive_index
                self.assertEqual(index(replay_payload())["server_sha256"], "c" * 64)
                if len(archives) == 2:
                    self.assertIsInstance(index, CompositeArchiveIndex)
                server.server_close.assert_called_once()

    def test_cli_ledger_alias_guard_checks_every_archive(self):
        native_archive(self.first)
        self.empty_archive(self.second)
        before = self.second.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            companion_receiver.main(["--archive-db", str(self.first), "--archive-db", str(self.second),
                                     "--server-sessions-db", str(self.second)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(before, self.second.read_bytes())


if __name__ == "__main__":
    unittest.main()
