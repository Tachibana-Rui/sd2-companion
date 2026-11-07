"""Explicit server association never fabricates automatic playback evidence."""
from copy import deepcopy
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.outbox import Outbox
from companion.replay_association import enqueue_association, prepare_submission
from companion.replay_capture import ReplayFileError, inspect_replay_file
from companion.receiver import API_PATH, ArchiveIndex, ReceiverStore, create_server
from companion.service import LOCAL_REPORT_SCHEMA, public_report_data
from companion.upload import Uploader
from test_companion_replay_capture import native_report, replay_bytes


class ReplayAssociationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "download.rpl3"
        self.path.write_bytes(replay_bytes())
        self.directory = self.base / "reports"
        self.outbox = Outbox(self.base / "outbox.sqlite3")
        self.report = public_report_data(native_report())

    def prepare(self, **options):
        return prepare_submission(self.report, self.path, self.outbox.installation_id,
                                  **{"user_selected": True, **options})

    def enqueue(self, **options):
        return enqueue_association(self.directory, self.report, self.path, self.outbox,
                                   **{"user_selected": True, **options})

    @contextmanager
    def http_archive(self, *, sha=None, session="session-one", map_id="map-one"):
        """A complete synthetic server archive, served only on loopback."""
        folder = Path(tempfile.mkdtemp(dir=self.base))
        archive = folder / "server-archive.sqlite3"
        sha = sha or hashlib.sha256(self.path.read_bytes()).hexdigest()
        document = {"schema": 1, "source": {"origin": "server_collection", "sha256": sha},
                    "match": {"session_id": session, "map": map_id, "version": 168061,
                              "completion": {"status": "finished"}},
                    "players": [{"id": "123", "alliance": 0}]}
        with closing(sqlite3.connect(archive)) as db, db:
            db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)")
            db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
            db.execute("INSERT INTO reports VALUES(?,?,?,?)",
                       (sha, "server:fixture-room:" + session, json.dumps(document), "finished"))
            db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, "fixture-room", "Fixture"))
        store = ReceiverStore(folder / "inbox.sqlite3", archive_index=ArchiveIndex(archive))
        server = create_server(store, "local-association-fixture-token")
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        uploader = Uploader("http://127.0.0.1:" + str(server.server_port) + API_PATH,
                            "local-association-fixture-token", allow_loopback_http=True)
        original = archive.read_bytes()
        try:
            yield store, uploader, sha
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(archive.read_bytes(), original)

    def test_selection_is_required_and_preparation_does_not_write_or_queue(self):
        with self.assertRaisesRegex(ValueError, "requires_selection"):
            self.prepare(user_selected=False)
        payload = self.prepare()
        self.assertEqual(payload["client_replay_sha256"],
                         hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(payload["session_id"], "session-one")
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.outbox.due(), [])
        evidence = payload["client_context_evidence"]
        self.assertEqual(evidence["association"], "user_selected_local_replay")
        self.assertIs(evidence["full_replay_sha256_verified"], True)
        self.assertNotIn("file_body_io_observed", evidence)
        self.assertNotIn("file_operation", evidence)
        self.assertNotIn(str(self.path), json.dumps(payload))

    def test_existing_session_build_and_full_id_side_roster_must_match(self):
        with self.assertRaisesRegex(ValueError, "session_mismatch"):
            self.prepare(session_id="another-session")
        for change in ({"game_build": 1},
                       {"native_players": [{**self.report["native_players"][0],
                                            "player_user_id": "456"}]},
                       {"native_players": [{**self.report["native_players"][0],
                                            "alliance": 1}]},
                       {"native_players": self.report["native_players"] * 2}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "roster_or_build_mismatch"):
                prepare_submission({**self.report, **change}, self.path,
                                   self.outbox.installation_id, user_selected=True)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.outbox.due(), [])

    def test_incomplete_or_later_changed_file_is_not_queued(self):
        original = self.path.read_bytes()
        self.path.write_bytes(original[:-2])
        with self.assertRaises(ReplayFileError):
            self.enqueue()
        self.path.write_bytes(original)
        snapshot = inspect_replay_file(self.path)
        self.path.write_bytes(original + b"changed")
        with patch("companion.replay_association.inspect_replay_file", return_value=snapshot):
            with self.assertRaisesRegex(ValueError, "server_replay_changed"):
                self.enqueue()
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.outbox.due(), [])

    def test_source_report_and_local_link_are_unchanged_and_private_annotations_omitted(self):
        report_path = self.directory / "unbound-replays" / "one.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps({"schema": LOCAL_REPORT_SCHEMA,
                                          "report": self.report}), encoding="utf-8")
        link_path = self.directory / "local-replay-links" / "one.json"
        link_path.parent.mkdir()
        link_path.write_text('{"association":"exact_native_session"}', encoding="utf-8")
        before = report_path.read_bytes(), link_path.read_bytes(), deepcopy(self.report)
        self.report["private_path"] = str(self.path)
        payload = self.enqueue(session_id="session-one")
        self.assertEqual(report_path.read_bytes(), before[0])
        self.assertEqual(link_path.read_bytes(), before[1])
        self.assertEqual({k: v for k, v in self.report.items() if k != "private_path"}, before[2])
        self.assertNotIn("private_path", payload)
        self.assertEqual(self.outbox.due()[0]["payload"], payload)
        export = self.directory / "exports" / payload["client_replay_sha256"] / (payload["submission_id"] + ".json")
        self.assertEqual(json.loads(export.read_text(encoding="utf-8")), payload)

    def test_repeat_after_restart_and_upgrade_reuses_original_immutable_payload(self):
        first = self.enqueue()
        with patch("companion.replay_association.__version__", "99.0.0"):
            restarted = Outbox(self.outbox.path)
            second = enqueue_association(self.directory, self.report, self.path, restarted,
                                         user_selected=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.outbox.due()), 1)
        self.assertEqual(len(list((self.directory / "exports").rglob("*.json"))), 1)

    def test_matching_automatic_export_is_reused_without_overwriting_its_evidence(self):
        previous = self.prepare()
        previous["submission_id"] = "a" * 64
        previous["extractor_version"] = "0.5.2"
        previous["client_context_evidence"] = {
            "association": "observed_game_file_read", "file_operation": "read",
            "file_body_io_observed": True,
            "roster_check": "exact_player_user_id_alliance_multiset"}
        target = self.directory / "exports" / previous["client_replay_sha256"] / (previous["submission_id"] + ".json")
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(previous), encoding="utf-8")
        original = target.read_bytes()
        self.outbox.enqueue(previous)
        result = self.enqueue()
        self.assertEqual(result, previous)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(len(self.outbox.due()), 1)

    def test_damaged_immutable_export_is_not_replaced(self):
        payload = self.prepare()
        target = self.directory / "exports" / payload["client_replay_sha256"] / (payload["submission_id"] + ".json")
        target.parent.mkdir(parents=True)
        target.write_text("damaged", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "export_content_conflict"):
            self.enqueue()
        self.assertEqual(target.read_text(encoding="utf-8"), "damaged")
        self.assertEqual(self.outbox.due(), [])

    def test_manual_helper_outbox_and_http_receiver_match_exact_server_archive(self):
        payload = self.enqueue(session_id="session-one")
        job = self.outbox.due()[0]
        with self.http_archive() as (store, uploader, sha):
            receipt = uploader.send(job["payload"])
            self.assertEqual(receipt["status"], "accepted")
            self.assertTrue(self.outbox.mark_sent(job["id"], receipt))
            self.assertEqual(self.outbox.status()["sent"], 1)
            self.assertEqual(uploader.send(payload)["status"], "duplicate")
            with closing(sqlite3.connect(store.path)) as db:
                row = db.execute("SELECT room_hint,map,server_sha256,binding_evidence,payload FROM submissions").fetchone()
            self.assertEqual(row[:4], ("fixture-room", "map-one", sha, "exact_replay_sha256_archive"))
            self.assertEqual(json.loads(row[4]), payload)

    def test_http_receiver_matches_different_bytes_only_through_verified_server_session(self):
        payload = self.enqueue(session_id="session-one")
        with self.http_archive(sha="f" * 64) as (store, uploader, sha):
            self.assertEqual(uploader.send(payload)["status"], "accepted")
            stored = store.report(payload["submission_id"])
            self.assertEqual(stored["server_sha256"], sha)
            self.assertEqual(stored["binding_evidence"], "server_finished_archive")
            self.assertEqual(stored["report"], payload)
            self.assertNotEqual(payload["client_replay_sha256"], sha)

    def test_http_receiver_keeps_wrong_archive_metadata_pending(self):
        payload = self.enqueue(session_id="session-one")
        job = self.outbox.due()[0]
        cases = (({"session": "different-session"}, "archive_replay_metadata_mismatch"),
                 ({"map_id": "different-map"}, "archive_replay_metadata_mismatch"))
        for options, reason in cases:
            with self.subTest(options=options), self.http_archive(**options) as (store, uploader, _):
                receipt = uploader.send(job["payload"])
                self.assertEqual(receipt["status"], "pending_match")
                with closing(sqlite3.connect(store.path)) as db:
                    row = db.execute("SELECT status,server_sha256,pending_reason,payload FROM submissions").fetchone()
                self.assertEqual(row[:3], ("pending_match", None, reason))
                self.assertEqual(json.loads(row[3]), payload)


if __name__ == "__main__":
    unittest.main()
