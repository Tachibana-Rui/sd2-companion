"""Local record deletion preserves unrelated captures and game replay files."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from companion.local_deletion import delete_record, deleted_fingerprints
from companion.local_replay_library import report_fingerprint
from companion.service import LOCAL_REPORT_SCHEMA, TASK_REPORT_SCHEMA


class LocalDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.root.mkdir()
        self.report = {"game_build": 168061, "report_generation": 1,
                       "native_players": [{"player_user_id": "123", "alliance": 0}],
                       "rows": [{"game_time_seconds": 60.0}]}
        self.fingerprint = report_fingerprint(self.report)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def capture(self, relative="unbound-replays/one.json", report=None, **metadata):
        return self.write(relative, {"schema": LOCAL_REPORT_SCHEMA,
            "report": self.report if report is None else report, **metadata})

    def test_deletes_all_local_representations_of_only_the_selected_capture(self):
        capture = self.capture(session_id="same-session")
        exported = self.write("exports/replay/one.json", {"schema": TASK_REPORT_SCHEMA,
            "submission_id": "a" * 64, "session_id": "same-session", **self.report})
        other = self.capture("unbound-live/other.json",
            report={**self.report, "report_generation": 2}, session_id="same-session")
        link = self.write("local-replay-links/" + self.fingerprint + ".json", {"replay_sha256": "b" * 64})
        evidence = self.write("replay-evidence/" + "b" * 64 + ".json", {"map": "fixture-map"})
        replay = self.root / "game.rpl3"
        replay.write_bytes(b"original replay bytes")
        before = {path: path.read_bytes() for path in (capture, exported, other, link, evidence, replay)}
        outbox = Mock()
        def cancel(fingerprint):
            self.assertEqual(fingerprint, self.fingerprint)
            self.assertIn(fingerprint, deleted_fingerprints(self.root))
            self.assertTrue(capture.exists())
            return 2
        outbox.cancel_report.side_effect = cancel
        result = delete_record(self.root, self.fingerprint, outbox=outbox)
        self.assertEqual(result, {"reports": 2, "queued_cancelled": 2})
        recovery = self.root / "deleted-reports" / self.fingerprint / "files"
        for path in (capture, exported, link):
            self.assertFalse(path.exists())
            self.assertEqual((recovery / path.relative_to(self.root)).read_bytes(), before[path])
        for path in (other, evidence, replay):
            self.assertEqual(path.read_bytes(), before[path])
        self.assertEqual(deleted_fingerprints(self.root), {self.fingerprint})
        outbox.cancel_report.assert_called_once_with(self.fingerprint)

    def test_restart_marker_and_repeated_delete_preserve_first_recovery_copy(self):
        capture = self.capture()
        original = capture.read_bytes()
        delete_record(self.root, self.fingerprint)
        marker = self.root / "deleted-reports" / (self.fingerprint + ".json")
        first_marker = marker.read_bytes()
        self.assertEqual(delete_record(self.root, self.fingerprint), {"reports": 0, "queued_cancelled": 0})
        self.assertEqual(marker.read_bytes(), first_marker)
        # A late capture write is still deleted after restart. Changes to local
        # annotations do not alter the native report fingerprint.
        self.capture(status="late metadata")
        result = delete_record(self.root, self.fingerprint)
        self.assertEqual(result["reports"], 1)
        self.assertFalse(capture.exists())
        copies = list((self.root / "deleted-reports" / self.fingerprint / "files" / "unbound-replays").glob("*.json"))
        self.assertEqual(len(copies), 2)
        self.assertIn(original, [path.read_bytes() for path in copies])
        self.assertEqual(marker.read_bytes(), first_marker)

    def test_bad_reports_are_retained_and_invalid_marker_files_are_ignored(self):
        self.capture()
        bad = self.write("unbound-replays/not-a-report.json", ["not a report"])
        marker_directory = self.root / "deleted-reports"
        marker_directory.mkdir()
        (marker_directory / ("c" * 64 + ".json")).write_text("broken", encoding="utf-8")
        (marker_directory / "not-a-fingerprint.json").write_text("{}", encoding="utf-8")
        self.assertEqual(deleted_fingerprints(self.root), set())
        self.assertEqual(delete_record(self.root, self.fingerprint)["reports"], 1)
        self.assertTrue(bad.exists())

    def test_invalid_fingerprints_cannot_create_paths(self):
        for fingerprint in (None, "", "../outside", "A" * 64, "a" * 63, "a" * 65):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(ValueError):
                delete_record(self.root, fingerprint)
        self.assertEqual(list(self.root.iterdir()), [])

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest("symlink creation unavailable: " + type(exc).__name__)

    def test_external_report_directory_is_never_followed_or_modified(self):
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        report = external / "one.json"
        report.write_text(json.dumps({"schema": LOCAL_REPORT_SCHEMA, "report": self.report}), encoding="utf-8")
        before = report.read_bytes()
        self.symlink(self.root / "unbound-replays", external)
        self.assertEqual(delete_record(self.root, self.fingerprint)["reports"], 0)
        self.assertEqual(report.read_bytes(), before)

    def test_external_recovery_directory_is_rejected_before_writing(self):
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        self.symlink(self.root / "deleted-reports", external)
        capture = self.capture()
        with self.assertRaisesRegex(ValueError, "unsafe_local_record_path"):
            delete_record(self.root, self.fingerprint)
        self.assertTrue(capture.exists())
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
