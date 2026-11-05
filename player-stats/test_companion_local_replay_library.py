"""Downloaded files supply local P without inventing upload evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from companion.desktop import LocalReportInventory
from companion.local_replay_library import (LocalReplayLibrary, associate, load_link,
                                           report_fingerprint)
from companion.service import LOCAL_REPORT_SCHEMA, public_report_data
from test_companion_replay_capture import native_report, replay_bytes
from test_companion_replay_evidence import sample_report


class LocalReplayLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.replays = self.base / "replays"
        self.replays.mkdir()
        self.path = self.replays / "download.rpl3"
        self.path.write_bytes(replay_bytes())
        self.directory = self.base / "reports"
        self.report = public_report_data(native_report())
        self.snapshot = self.directory / "unbound-replays" / "one.json"
        self.snapshot.parent.mkdir(parents=True)
        self.document = {"schema": LOCAL_REPORT_SCHEMA, "report": self.report}

    def save(self, **metadata):
        self.snapshot.write_text(json.dumps({**self.document, **metadata}), encoding="utf-8")

    def test_player_can_recover_old_report_without_session_locally_only(self):
        self.save()
        original = self.snapshot.read_bytes()
        inventory = LocalReportInventory(self.directory)
        self.assertIsNone(inventory.entries()[0]["map"])
        link = associate(self.directory, self.report, self.path, user_selected=True,
                         parser=lambda _: sample_report())
        entry = inventory.entries()[0]  # Same cached report, newly written evidence.
        self.assertEqual(entry["replay"], link["replay_sha256"])
        self.assertEqual(entry["kind"], "local")
        self.assertEqual(entry["map"], "Mogilev #10vs10")
        self.assertFalse(entry["linked"])  # A local link never becomes an upload export.
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertFalse((self.directory / "exports").exists())
        self.assertFalse((self.directory / "outbox.sqlite3").exists())
        self.assertEqual(load_link(self.directory, entry["fingerprint"])["association"], "user_selected")

    def test_matching_roster_without_session_never_automatically_links(self):
        self.save()
        inventory = LocalReportInventory(self.directory)
        library = LocalReplayLibrary(self.directory, [self.replays])
        with patch("companion.local_replay_library.associate") as parser:
            library.reconcile(inventory.entries())
        parser.assert_not_called()
        with self.assertRaisesRegex(ValueError, "requires_session_or_selection"):
            associate(self.directory, self.report, self.path)

    def test_native_session_matches_preexisting_download_without_etw(self):
        self.save(session_id="session-one", session_origin="native_replay_context")
        inventory = LocalReportInventory(self.directory)
        library = LocalReplayLibrary(self.directory, [self.replays])
        with patch("companion.replay_evidence.extract") as extract:
            library.reconcile(inventory.entries())
        extract.assert_called_once()
        link = load_link(self.directory, report_fingerprint(self.report))
        self.assertEqual(link["association"], "exact_native_session")
        self.assertEqual(inventory.entries()[0]["map"], "map-one")

    def test_header_only_session_hint_is_not_accepted_as_native_context(self):
        self.save(session_id="session-one")
        with patch("companion.local_replay_library.associate") as parser:
            LocalReplayLibrary(self.directory, [self.replays]).reconcile(
                LocalReportInventory(self.directory).entries())
        parser.assert_not_called()

    def test_session_roster_and_build_must_all_match(self):
        for change, code in (({"session_id": "another-session"}, "session_mismatch"),
                             ({"game_build": 1}, "roster_or_build_mismatch"),
                             ({"native_players": [{"player_user_id": "789", "alliance": 0}]},
                              "roster_or_build_mismatch")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, code):
                associate(self.directory, {**self.report, **change}, self.path,
                          session_id=change.get("session_id"), user_selected=True)
        self.assertFalse((self.directory / "local-replay-links").exists())

    def test_distinct_files_for_same_session_remain_ambiguous(self):
        self.save(session_id="session-one", session_origin="native_replay_context")
        (self.replays / "other.rpl3").write_bytes(replay_bytes(marker=b"other commands"))
        with patch("companion.local_replay_library.associate") as parser:
            LocalReplayLibrary(self.directory, [self.replays]).reconcile(
                LocalReportInventory(self.directory).entries())
        parser.assert_not_called()

    def test_incomplete_download_retries_after_it_finishes(self):
        self.save(session_id="session-one", session_origin="native_replay_context")
        data = self.path.read_bytes()
        self.path.write_bytes(data[:-3])
        clock = [1.0]
        library = LocalReplayLibrary(self.directory, [self.replays], clock=lambda: clock[0])
        inventory = LocalReportInventory(self.directory)
        with patch("companion.replay_evidence.extract") as extract:
            library.reconcile(inventory.entries())
            extract.assert_not_called()
            self.path.write_bytes(data)
            clock[0] += 16
            library.reconcile(inventory.entries())
            extract.assert_called_once()

    def test_evidence_arrival_refreshes_inventory_without_report_rewrite(self):
        self.save()
        inventory = LocalReportInventory(self.directory)
        before = inventory.poll()
        associate(self.directory, self.report, self.path, user_selected=True,
                  parser=lambda _: sample_report())
        after = inventory.poll()
        self.assertNotEqual(before, after)
        self.assertEqual((after["analyzed"], after["unbound"]), (1, 1))

    def test_existing_bound_sha_with_missing_evidence_is_retried_without_rebinding(self):
        import hashlib
        from companion.service import TASK_REPORT_SCHEMA
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        export = self.directory / "exports" / digest / "export.json"
        export.parent.mkdir(parents=True)
        export.write_text(json.dumps({**self.report, "schema": TASK_REPORT_SCHEMA,
            "submission_id": "one", "client_replay_sha256": digest,
            "session_id": "session-one"}), encoding="utf-8")
        # Another byte sequence with this SID cannot replace the known hash.
        (self.replays / "other.rpl3").write_bytes(replay_bytes(marker=b"other commands"))
        original = export.read_bytes()
        with patch("companion.replay_evidence.extract") as extract:
            LocalReplayLibrary(self.directory, [self.replays]).reconcile(
                LocalReportInventory(self.directory).entries())
        extract.assert_called_once()
        self.assertEqual(extract.call_args.args[1], digest)
        self.assertEqual(export.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
