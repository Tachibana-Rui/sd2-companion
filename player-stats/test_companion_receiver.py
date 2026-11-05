"""Standalone inbox contract and delivery integration over loopback only."""

import contextlib
import copy
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from urllib import request

from companion.outbox import Outbox
from companion.receiver import API_PATH, ArchiveIndex, MAX_ROWS, ReceiverError, ReceiverStore, create_server
from companion.upload import MAX_PAYLOAD_BYTES, UploadError, Uploader
from companion.server_sessions import ServerObservation, ServerSessionLedger


def native_player(index=0, identifier="123", alliance=0, name="fixture player"):
    return {"row_index": index, "name": name, "player_user_id": identifier,
            "alliance": alliance, "alliance_display_value": 4 if alliance is None else alliance,
            "official_kill_points": 100, "official_loss_points": 50}


def payload(identifier="a" * 64):
    actor = {"player_name": "fixture player", "unit_name": "fixture unit", "soldier_name": "fixture soldier",
             "unit_label_texture_token": 4, "xp_texture_token": 5, "display_veterancy": 2,
             "player_label_color_token": 123, "is_ace": False, "unit_guid": None,
             "player_user_id": None, "identity_status": "unresolved"}
    row = {"row_index": 0, "game_time_seconds": 1.5, "time_display": "00:01",
           "killer": dict(actor), "victim": dict(actor)}
    return {"schema": "sd2-task-report-v3", "submission_id": identifier,
            "installation_id": str(uuid.UUID("d4ee54a2-0123-4567-89ab-0123456789ab")),
            "room_hint": "test-room", "session_id": "fixture-session", "game_build": 168061,
            "map": "fixture-map", "server_name": "fixture server", "source": "player_client_official_task_report",
            "binding_mode": "native_session", "native_players": [native_player()], "rows": [row],
            "exe_sha256": "e" * 64, "report_generation": 1,
            "report_scope": "native_published_participant_timeline",
            "report_data": {"identity_source": "unavailable", "identity_status": "unavailable", "matched_rows": 0, "total_rows": 1},
            "client_context_evidence": {"association": "native_game_session", "native_source_kind": "network_live",
                "native_game_ended": True, "observed_running": True, "context_epoch": "d" * 64,
                "report_observed_after_end": True, "session_origin": "native_game_setup"}}


def replay_payload(identifier="a" * 64, *, sha="c" * 64, operation="read"):
    result = payload(identifier)
    del result["room_hint"]
    result.update(binding_mode="replay_sha256", client_replay_sha256=sha,
                  client_context_evidence={"association": "observed_game_file_" + operation,
                      "file_operation": operation, "file_body_io_observed": True})
    return result


def native_live_payload(identifier="a" * 64, *, session_id="fixture-session"):
    result = payload(identifier)
    for key in ("room_hint", "map", "server_name"):
        del result[key]
    result["session_id"] = session_id
    return result


def native_archive(path, *, sha="c" * 64, session="fixture-session", room="archive-room",
                   finished=True, origin="server_collection", build=168061, players=None):
    document = {"schema": 1, "source": {"origin": origin, "sha256": sha},
                "match": {"session_id": session, "map": "fixture-map", "version": build,
                          "completion": {"status": "finished" if finished else "unverified"}},
                "players": players if players is not None else [{"id": "123", "alliance": 0}]}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
        db.execute("INSERT INTO reports VALUES(?,?,?,?)", (sha, "server:" + room + ":" + session, json.dumps(document),
                   "finished" if finished else "unverified"))
        db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, room, "archive server"))
    return sha


class ReceiverStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-companion-receiver-")
        self.base = Path(self.temporary.name)
        self.path = self.base / "inbox.sqlite3"
        self.store = ReceiverStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def saved(self, identifier="a" * 64):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM submissions WHERE submission_id=?", (identifier,)).fetchone())

    def test_native_live_without_client_files_room_or_map_matches_server_finished_record(self):
        archive = self.base / 'native-archive.sqlite3'
        sha = native_archive(archive)
        before = archive.read_bytes()
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = native_live_payload()
        receipt = store.submit(report)
        self.assertEqual(receipt, {'submission_id': report['submission_id'], 'status': 'accepted',
                                  'binding_evidence': 'server_finished_archive', 'server_state_verified': False})
        saved = self.saved()
        self.assertEqual(saved['server_sha256'], sha)
        self.assertEqual(saved['room_hint'], 'archive-room')
        self.assertEqual(saved['map'], 'fixture-map')
        self.assertEqual(saved['binding_evidence'], 'server_finished_archive')
        self.assertEqual(saved['server_state_verified'], 0)
        self.assertEqual(json.loads(saved['payload']), report)
        self.assertFalse({'map', 'room_hint', 'client_header_sha256', 'client_replay_sha256'} & report.keys())
        self.assertEqual(archive.read_bytes(), before)

    def test_native_live_requires_running_then_ended_native_source_evidence(self):
        variants = []
        base = native_live_payload()
        for key in ('native_players', 'binding_mode', 'client_context_evidence'):
            candidate = copy.deepcopy(base);del candidate[key];variants.append(candidate)
        for key, value in (('native_source_kind', 'local_replay'), ('native_source_kind', 'unknown'),
                           ('native_game_ended', False), ('native_game_ended', 1), ('observed_running', False),
                           ('report_observed_after_end', False), ('session_origin', 'replay_header'),
                           ('association', 'observed_game_file_read'), ('context_epoch', 'not-an-epoch')):
            candidate = copy.deepcopy(base);candidate['client_context_evidence'][key] = value;variants.append(candidate)
        for key in ('native_game_ended', 'observed_running', 'context_epoch', 'report_observed_after_end', 'session_origin'):
            candidate = copy.deepcopy(base);del candidate['client_context_evidence'][key];variants.append(candidate)
        for key, value in (('client_replay_sha256', 'f' * 64), ('client_header_sha256', 'f' * 64),
                           ('source', 'player_client_replay_task_report'), ('match_binding', 'exact_replay_sha256_pending_archive')):
            candidate = copy.deepcopy(base);candidate[key] = value;variants.append(candidate)
        candidate = copy.deepcopy(base)
        candidate['native_players'].append(native_player(1))
        variants.append(candidate)
        for index, candidate in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ReceiverError):self.store.submit(candidate)
        self.assertEqual(self.store.status()['submissions']['total'], 0)

    def test_native_live_rejects_old_null_hash_fields(self):
        report = native_live_payload()
        report.update(client_replay_sha256=None, client_header_sha256=None)
        with self.assertRaises(ReceiverError):
            self.store.submit(report)
        self.assertEqual(self.store.status()['submissions']['total'], 0)

    def test_reconcile_keeps_going_when_one_stored_payload_read_fails(self):
        archive = self.base / 'unfinished-native.sqlite3'
        native_archive(archive, finished=False)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        store.submit(native_live_payload('a' * 64, session_id='fixture-session'))
        store.submit(native_live_payload('b' * 64, session_id='second-native-session'))
        native_archive(archive, sha='e' * 64, session='second-native-session')
        original = store._stored_payload

        def flaky(submission_id):
            # The first read fails exactly as an inbox WAL race does; the batch
            # must still process the other pending submission.
            if submission_id == 'a' * 64:
                return None
            return original(submission_id)

        with patch.object(store, "_stored_payload", side_effect=flaky):
            self.assertEqual(store.reconcile(), {'checked': 1, 'accepted': 1})
        self.assertEqual(self.saved('b' * 64)['status'], 'accepted')
        self.assertEqual(self.saved('a' * 64)['status'], 'pending_match')

    def test_status_reports_the_last_reconcile_error(self):
        self.assertIsNone(ReceiverStore(self.path).status()['last_reconcile_error'])
        self.store.last_reconcile_error = 'OperationalError'
        self.assertEqual(self.store.status()['last_reconcile_error'], 'OperationalError')


    def test_native_live_report_waits_for_server_completion_then_reconciles_without_payload_changes(self):
        archive = self.base / 'unfinished-native.sqlite3'
        native_archive(archive, finished=False)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = native_live_payload()
        self.assertEqual(store.submit(report)['status'], 'pending_match')
        self.assertEqual(self.saved()['map'], '')
        with contextlib.closing(sqlite3.connect(archive)) as db, db:
            document = json.loads(db.execute('SELECT payload FROM reports').fetchone()[0])
            document['match']['completion']['status'] = 'finished'
            db.execute("UPDATE reports SET completion_status='finished',payload=?", (json.dumps(document),))
        self.assertEqual(store.reconcile(), {'checked': 1, 'accepted': 1})
        self.assertEqual(self.saved()['map'], 'fixture-map')
        self.assertEqual(json.loads(self.saved()['payload']), report)
        duplicate = store.submit(report)
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['binding_evidence'], 'server_finished_archive')
        self.assertFalse(duplicate['server_state_verified'])

    def test_native_live_same_roster_consecutive_rounds_match_exact_session_not_latest(self):
        archive = self.base / 'two-native-rounds.sqlite3'
        native_archive(archive)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        second = native_live_payload(session_id='second-native-session')
        self.assertEqual(store.submit(second)['status'], 'pending_match')
        native_archive(archive, sha='e' * 64, session='second-native-session')
        self.assertEqual(store.reconcile(), {'checked': 1, 'accepted': 1})
        self.assertEqual(self.saved()['server_sha256'], 'e' * 64)
        self.assertEqual(store.submit(native_live_payload('b' * 64))['status'], 'accepted')
        self.assertEqual(self.saved('b' * 64)['server_sha256'], 'c' * 64)

    def test_native_live_unknown_server_source_and_mismatched_metadata_stay_pending(self):
        cases = ({'origin': 'manual_import'}, {'build': 1}, {'players': [{'id': '999', 'alliance': 0}]})
        for index, options in enumerate(cases):
            archive = self.base / ('invalid-native-' + str(index) + '.sqlite3')
            native_archive(archive, **options)
            inbox = self.base / ('invalid-native-inbox-' + str(index) + '.sqlite3')
            store = ReceiverStore(inbox, archive_index=ArchiveIndex(archive))
            self.assertEqual(store.submit(native_live_payload())['status'], 'pending_match')
        archive = self.base / 'native-map.sqlite3';native_archive(archive)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        for index, additions in enumerate(({'map': 'other-map'}, {'room_hint': 'other-room'}, {'session_id': 'wrong-session'})):
            report = native_live_payload(str(index) * 64);report.update(additions)
            self.assertEqual(store.submit(report)['status'], 'pending_match')

    def test_native_live_server_epoch_is_derived_without_player_input(self):
        archive = self.base / 'native-observed.sqlite3';native_archive(archive)
        now = [100]
        ledger = ServerSessionLedger(self.base / 'server-sessions.sqlite3', clock=lambda: now[0])
        ledger.observe(ServerObservation('archive-room', 'a' * 64, 'fixture-session', 53, 'fixture-map', 168061,
                                        (('123', 0),), 100, 1, 'server_native_game_setup'))
        now[0] = 101
        ledger.observe(ServerObservation('archive-room', 'a' * 64, 'fixture-session', 101, 'fixture-map', 168061,
                                        (('123', 0),), 101, 2, 'server_native_game_setup'))
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive, server_sessions=ledger))
        without_epoch = store.submit(native_live_payload())
        self.assertEqual(without_epoch['status'], 'accepted')
        self.assertTrue(without_epoch['server_state_verified'])
        report = native_live_payload('b' * 64);report['server_epoch'] = 'a' * 64
        receipt = store.submit(report)
        self.assertTrue(receipt['server_state_verified'])
        self.assertEqual(self.saved('b' * 64)['server_state_verified'], 1)
        required = ReceiverStore(self.base / 'require-state.sqlite3', archive_index=ArchiveIndex(
            archive, server_sessions=ledger, require_live_observation=True))
        self.assertEqual(required.submit(native_live_payload())['status'], 'accepted')
        self.assertEqual(required.submit(report)['status'], 'accepted')
        wrong = native_live_payload('c' * 64);wrong['server_epoch'] = 'b' * 64
        rejected = store.submit(wrong)
        self.assertEqual(rejected['status'], 'pending_match')
        self.assertFalse(rejected['server_state_verified'])

    def test_immutable_report_preserves_original_order_and_duplicate_event_rows(self):
        report = payload()
        report["rows"].append({**copy.deepcopy(report["rows"][0]), "row_index": 1})
        report["report_data"]["total_rows"] = 2
        original = copy.deepcopy(report)
        self.assertEqual(self.store.submit(report)["status"], "pending_match")
        report["rows"][0]["killer"]["unit_name"] = "changed locally after submit"
        self.assertEqual(json.loads(self.saved()["payload"]), original)
        self.assertEqual(self.store.submit(original)["status"], "duplicate")
        self.assertEqual(self.store.status()["submissions"]["total"], 1)

    def test_same_id_different_content_is_conflict_and_never_overwrites(self):
        original = payload()
        self.store.submit(original)
        changed = copy.deepcopy(original)
        changed["rows"][0]["victim"]["unit_name"] = "different row"
        with self.assertRaises(ReceiverError) as caught:
            self.store.submit(changed)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(json.loads(self.saved()["payload"]), original)

    def test_empty_report_retains_required_roster_and_zero_identity_counts(self):
        report = payload()
        report["rows"] = []
        report["report_data"]["total_rows"] = 0
        self.store.submit(report)
        self.assertEqual(json.loads(self.saved()["payload"])["rows"], [])

    def test_two_clients_are_retained_as_candidates_and_never_merged(self):
        first, second = payload(), payload("b" * 64)
        second["installation_id"] = str(uuid.uuid4())
        second["rows"][0]["killer"]["unit_name"] = "localized fixture unit"
        self.store.submit(first)
        self.store.submit(second)
        self.assertEqual(self.store.status()["submissions"]["total"], 2)
        self.assertEqual(len(json.loads(self.saved()["payload"])["rows"]), 1)
        self.assertEqual(len(json.loads(self.saved("b" * 64)["payload"])["rows"]), 1)

    def test_invalid_schema_source_time_index_and_private_fields_are_rejected(self):
        examples = []
        item = payload(); item["schema"] = "something-else"; examples.append(item)
        item = payload(); item["source"] = "server_collection"; examples.append(item)
        item = payload(); item["rows"][0]["game_time_seconds"] = -1; examples.append(item)
        item = payload(); item["rows"][0]["game_time_seconds"] = float("nan"); examples.append(item)
        item = payload(); item["rows"][0]["row_index"] = 1; examples.append(item)
        item = payload(); item["rows"][0]["killer"]["pointer"] = 123456; examples.append(item)
        item = payload(); item["rows"][0]["victim"]["player_name"] = 123; examples.append(item)
        item = payload(); item["rows"] = [item["rows"][0]] * (MAX_ROWS + 1); examples.append(item)
        item = payload(); item["origin"] = "server_collection"; examples.append(item)
        for index, item in enumerate(examples):
            with self.subTest(case=index):
                with self.assertRaises(ReceiverError):
                    self.store.submit(item)
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def test_official_unit_counts_are_optional_and_validated(self):
        report = payload()
        report["schema"] = "sd2-task-report-v4"
        report["native_players"][0].update(official_kill_unit_count=3, official_loss_unit_count=4)
        self.store.submit(report)
        self.assertEqual(self.store.status()["submissions"]["total"], 1)
        for value in (-1, "3", True, 2**32):
            bad = payload("b" * 64)
            bad["schema"] = "sd2-task-report-v4"
            bad["native_players"][0]["official_kill_unit_count"] = value
            with self.assertRaises(ReceiverError):
                self.store.submit(bad)

    def test_unrecognized_display_fields_are_rejected_by_v3(self):
        report = payload()
        report["rows"][0]["killer"]["display_fields"] = {"type_display_id": 6}
        with self.assertRaises(ReceiverError):
            self.store.submit(report)

    def test_native_players_preserve_order_unicode_and_unknown_identity(self):
        report = payload()
        report["native_players"] = [native_player(name="原版玩家 🚩"),
                                    native_player(1, None, None, "未识别玩家"),
                                    native_player(2, "18446744073709551614", 1, "large ID")]
        self.store.submit(report)
        self.assertEqual(json.loads(self.saved()["payload"]), report)

    def test_invalid_native_roster_fields_indices_ids_and_alliances_are_rejected(self):
        bad_rosters = [None, {}, [native_player()] * 21]
        for key, value in (("row_index", 1), ("row_index", True), ("row_index", -1),
                           ("name", 123), ("name", "bad\nname"),
                           ("player_user_id", 123), ("player_user_id", True),
                           ("player_user_id", ""), ("player_user_id", "-1"),
                           ("player_user_id", "0123"), ("player_user_id", "1.0"),
                           ("player_user_id", "18446744073709551615"),
                           ("player_user_id", "18446744073709551616"),
                           ("alliance", True), ("alliance", 1), ("alliance", None),
                           ("alliance_display_value", True), ("alliance_display_value", 2),
                           ("alliance_display_value", 4), ("address", 123456)):
            player = native_player()
            player[key] = value
            bad_rosters.append([player])
        missing = native_player()
        del missing["player_user_id"]
        bad_rosters.append([missing])
        bad_rosters.append([native_player(), native_player(0, "456", 1)])
        for index, roster in enumerate(bad_rosters):
            with self.subTest(case=index):
                report = payload()
                report["native_players"] = roster
                with self.assertRaises(ReceiverError):
                    self.store.submit(report)
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def archive(self):
        path = self.base / "archive.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT DEFAULT 'finished')")
            db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
            db.commit()
        return path

    def add_archive_report(self, path, *, server_id="test-room", session_id="fixture-session", map_id="fixture-map", version=168061,
                           players=None):
        sha = "c" * 64
        report = {"schema": 1, "source": {"origin": "server_collection", "sha256": sha},
                  "match": {"session_id": session_id, "map": map_id, "version": version,
                            "completion": {"status": "finished"}}}
        report["players"] = players if players is not None else [{"id": "123", "alliance": 0}]
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO reports(sha256,match_key,payload) VALUES(?,?,?)", (sha, session_id, json.dumps(report)))
            db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, server_id, "fixture server"))
            db.commit()
        return sha

    def test_late_archive_binding_is_read_only_and_does_not_upgrade_client_source(self):
        archive = self.archive()
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = payload()
        self.assertEqual(store.submit(report)["status"], "pending_match")
        sha = self.add_archive_report(archive)
        before = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(store.reconcile(), {"checked": 1, "accepted": 1})
        self.assertEqual(before, hashlib.sha256(archive.read_bytes()).hexdigest())
        row = self.saved()
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["server_sha256"], sha)
        self.assertEqual(json.loads(row["payload"])["source"], "player_client_official_task_report")
        with contextlib.closing(sqlite3.connect(archive)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='submissions'").fetchone())

    def test_archive_wrong_room_map_build_and_session_never_bind(self):
        for index, options in enumerate(({"server_id": "other-room"}, {"map_id": "other-map"},
                                          {"version": 12345}, {"session_id": "other-session"})):
            with self.subTest(options=options):
                archive = self.base / ("archive-" + str(index) + ".sqlite3")
                with contextlib.closing(sqlite3.connect(archive)) as db:
                    db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT DEFAULT 'finished')")
                    db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
                    db.commit()
                self.add_archive_report(archive, **options)
                store = ReceiverStore(self.base / ("inbox-" + str(index) + ".sqlite3"), archive_index=ArchiveIndex(archive))
                self.assertEqual(store.submit(payload())["status"], "pending_match")

    def test_native_roster_archive_match_ignores_names_order_and_observers(self):
        archive = self.archive()
        self.add_archive_report(archive, players=[
            {"id": "456", "name": "different localized name", "alliance": 1, "is_observer": False},
            {"id": "observer", "alliance": None, "is_observer": True},
            {"id": "123", "name": "server name differs", "alliance": 0, "is_observer": False},
        ])
        report = payload()
        report["native_players"] = [native_player(), native_player(1, "456", 1, "second player")]
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        self.assertEqual(store.submit(report)["status"], "accepted")
        self.assertEqual(json.loads(self.saved()["payload"])["native_players"], report["native_players"])

    def test_native_roster_requires_complete_matching_archive_metadata(self):
        archive = self.archive()
        self.add_archive_report(archive)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = payload()
        report["native_players"] = [native_player(), native_player(1, "456", 1, "second player")]
        self.assertEqual(store.submit(report)["status"], "pending_match")
        variants = (
            [],
            [{"id": "123", "alliance": 0}],
            [{"id": "123", "alliance": 0}, {"id": "789", "alliance": 1}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": 0}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": None}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": True}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": 1, "is_observer": True}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": 1, "is_observer": 1}],
            [{"id": "123", "alliance": 0}, {"id": "456", "alliance": 1}, {"id": "456", "alliance": 1}],
            [{"id": "123", "alliance": 0}, {"id": 456, "alliance": 1}],
        )
        for players in variants:
            with self.subTest(players=players):
                with contextlib.closing(sqlite3.connect(archive)) as db:
                    document = json.loads(db.execute("SELECT payload FROM reports").fetchone()[0])
                    document["players"] = players
                    db.execute("UPDATE reports SET payload=?", (json.dumps(document),))
                    db.commit()
                self.assertEqual(store.reconcile(), {"checked": 1, "accepted": 0})
        self.assertEqual(self.saved()["status"], "pending_match")

    def test_unknown_native_id_or_alliance_and_empty_roster_never_bind(self):
        archive = self.archive()
        self.add_archive_report(archive, players=[{"id": "123", "alliance": 0}])
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        for index, players in enumerate(([], [native_player(identifier=None)], [native_player(alliance=None)])):
            with self.subTest(players=players):
                report = payload(str(index) * 64)
                report["native_players"] = players
                self.assertEqual(store.submit(report)["status"], "pending_match")
        self.assertEqual(store.reconcile(), {"checked": 3, "accepted": 0})

    def test_receiver_database_cannot_be_the_read_only_archive(self):
        archive = self.archive()
        with self.assertRaisesRegex(ValueError, "separate"):
            ReceiverStore(archive, archive_index=ArchiveIndex(archive))

    def add_replay_analysis(self, path, *, sha="c" * 64, rooms=("database-room",),
                            completion="finished", source_sha=None, schema=1, players=None,
                            session_id="fixture-session", map_id="fixture-map", version=168061):
        document = {"schema": schema, "source": {"origin": "local_upload", "sha256": source_sha or sha},
                    "match": {"session_id": session_id, "map": map_id, "version": version,
                              "completion": {"status": completion}},
                    "players": players if players is not None else [{"id": "123", "alliance": 0}]}
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO reports(sha256,match_key,payload) VALUES(?,?,?)",
                       (sha, session_id, json.dumps(document)))
            for room in rooms:
                db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, room, "database server"))
            db.commit()
        return sha

    def test_replay_hash_matches_finished_analysis_without_declared_room_or_server_origin(self):
        archive = self.archive()
        sha = self.add_replay_analysis(archive)
        before = hashlib.sha256(archive.read_bytes()).hexdigest()
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = replay_payload(sha=sha)
        self.assertEqual(store.submit(report), {"submission_id": report["submission_id"], "status": "accepted"})
        row = self.saved()
        self.assertEqual(row["server_sha256"], sha)
        self.assertEqual(row["room_hint"], "database-room")
        self.assertEqual(json.loads(row["archive_rooms"]), ["database-room"])
        self.assertIsNone(row["pending_reason"])
        self.assertEqual(json.loads(row["payload"]), report)
        self.assertNotIn("room_hint", report)
        self.assertEqual(before, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_replay_hash_allows_no_room_and_keeps_multiple_archive_sources(self):
        archive = self.archive()
        self.add_replay_analysis(archive, sha="c" * 64, rooms=())
        self.add_replay_analysis(archive, sha="d" * 64, rooms=("room-two", "room-one"))
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        for identifier, sha, rooms in (("a" * 64, "c" * 64, []),
                                       ("b" * 64, "d" * 64, ["room-one", "room-two"])):
            with self.subTest(rooms=rooms):
                self.assertEqual(store.submit(replay_payload(identifier, sha=sha))["status"], "accepted")
                row = self.saved(identifier)
                self.assertEqual(row["room_hint"], "")
                self.assertEqual(json.loads(row["archive_rooms"]), rooms)

    def test_replay_schema_requires_full_hash_native_roster_access_evidence_and_no_room(self):
        bad = []
        for key in ("client_replay_sha256", "native_players", "binding_mode", "client_context_evidence"):
            item = replay_payload(); del item[key]; bad.append(item)
        for key, value in (("client_replay_sha256", None), ("client_replay_sha256", "header-only"),
                           ("client_replay_sha256", "C" * 64), ("room_hint", "Bad Room"),
                           ("source", "player_client_replay_task_report"),
                           ("match_binding", "candidate_client_header_pending_server_archive"),
                           ("client_context_evidence", "observed_game_file_read"),
                           ("client_context_evidence", {"association": "latest_file_mtime"})):
            item = replay_payload(); item[key] = value; bad.append(item)
        for index, item in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(ReceiverError):
                self.store.submit(item)
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def test_replay_cloud_local_write_evidence_is_retained(self):
        report = replay_payload(operation="write")
        receipt = self.store.submit(report)
        self.assertEqual(receipt["status"], "pending_match")
        self.assertEqual(receipt["pending_reason"], "archive_not_configured")
        self.assertEqual(json.loads(self.saved()["payload"]), report)

    def test_replay_hash_does_not_fallback_to_session_header_hash_or_roster(self):
        archive = self.archive()
        self.add_replay_analysis(archive)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = replay_payload(sha="d" * 64)
        report["exe_sha256"] = "c" * 64
        receipt = store.submit(report)
        self.assertEqual(receipt["status"], "pending_match")
        self.assertEqual(receipt["pending_reason"], "replay_sha256_not_analyzed")
        self.assertEqual(self.saved()["pending_reason"], "replay_sha256_not_analyzed")
        self.assertEqual(store.status()["pending_reasons"], {"replay_sha256_not_analyzed": 1})

    def test_replay_finished_analysis_late_arrival_reconciles_and_deduplicates(self):
        archive = self.archive()
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = replay_payload()
        store.submit(report)
        self.add_replay_analysis(archive)
        self.assertEqual(store.reconcile(), {"checked": 1, "accepted": 1})
        self.assertEqual(store.reconcile(), {"checked": 0, "accepted": 0})
        self.assertEqual(store.submit(report)["status"], "duplicate")
        self.assertEqual(self.saved()["room_hint"], "database-room")
        self.assertIsNone(self.saved()["pending_reason"])
        changed = copy.deepcopy(report)
        changed["rows"] = []
        changed["report_data"]["total_rows"] = 0
        with self.assertRaises(ReceiverError) as caught:
            store.submit(changed)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(json.loads(self.saved()["payload"]), report)

    def test_replay_unfinished_invalid_metadata_and_roster_keep_precise_pending_reason(self):
        cases = (({"completion": "unverified"}, "archive_replay_not_finished"),
                 ({"source_sha": "d" * 64}, "archive_replay_invalid_report"),
                 ({"schema": 2}, "archive_replay_invalid_report"),
                 ({"session_id": "other-session"}, "archive_replay_metadata_mismatch"),
                 ({"map_id": "other-map"}, "archive_replay_metadata_mismatch"),
                 ({"version": 12345}, "archive_replay_metadata_mismatch"),
                 ({"version": 168061.0}, "archive_replay_metadata_mismatch"),
                 ({"players": [{"id": "456", "alliance": 0}]}, "archive_replay_roster_mismatch"))
        for index, (options, reason) in enumerate(cases):
            with self.subTest(options=options):
                archive = self.base / ("replay-archive-" + str(index) + ".sqlite3")
                with contextlib.closing(sqlite3.connect(archive)) as db:
                    db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT DEFAULT 'finished')")
                    db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
                    db.commit()
                self.add_replay_analysis(archive, **options)
                inbox = self.base / ("replay-inbox-" + str(index) + ".sqlite3")
                store = ReceiverStore(inbox, archive_index=ArchiveIndex(archive))
                receipt = store.submit(replay_payload())
                self.assertEqual(receipt["status"], "pending_match")
                self.assertEqual(receipt["pending_reason"], reason)

    def test_replay_archive_completion_column_cannot_claim_unfinished_row_complete(self):
        archive = self.archive()
        self.add_replay_analysis(archive)
        with contextlib.closing(sqlite3.connect(archive)) as db:
            db.execute("UPDATE reports SET completion_status='unverified'")
            db.commit()
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        self.assertEqual(store.submit(replay_payload())["pending_reason"], "archive_replay_not_finished")
        with contextlib.closing(sqlite3.connect(archive)) as db:
            db.execute("UPDATE reports SET completion_status='finished'")
            db.commit()
        self.assertEqual(store.reconcile(), {"checked": 1, "accepted": 1})

    def test_replay_incomplete_native_roster_never_binds_even_with_exact_hash(self):
        archive = self.archive()
        self.add_replay_analysis(archive)
        store = ReceiverStore(self.path, archive_index=ArchiveIndex(archive))
        report = replay_payload()
        report["native_players"] = [native_player(identifier=None)]
        self.assertEqual(store.submit(report)["pending_reason"], "native_roster_incomplete")

    def test_replay_temporarily_missing_archive_is_retained_without_recreating_database(self):
        archive = self.archive()
        index = ArchiveIndex(archive)
        archive.unlink()
        store = ReceiverStore(self.path, archive_index=index)
        receipt = store.submit(replay_payload())
        self.assertEqual(receipt["status"], "pending_match")
        self.assertEqual(receipt["pending_reason"], "archive_temporarily_unavailable")
        self.assertFalse(archive.exists())



class ReceiverHttpTests(unittest.TestCase):
    token = "local-fixture-upload-token"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-companion-http-")
        self.base_path = Path(self.temporary.name)
        self.store = ReceiverStore(self.base_path / "inbox.sqlite3")
        self.server = create_server(self.store, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.uploader = Uploader(self.base + API_PATH, self.token, allow_loopback_http=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def post(self, body, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest("POST", API_PATH)
            for key, value in (headers or []):
                connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def headers(self, body):
        return [("Authorization", "Bearer " + self.token), ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))), ("Idempotency-Key", "a" * 64)]

    def test_native_live_no_local_replay_through_outbox_http_and_authoritative_server_index(self):
        archive = self.base_path / 'server-native-archive.sqlite3'
        sha = native_archive(archive)
        self.store.archive_index = ArchiveIndex(archive)
        box = Outbox(self.base_path / 'native-live-outbox.sqlite3')
        report = native_live_payload()
        box.enqueue(report)
        job = box.due()[0]
        receipt = self.uploader.send(job['payload'])
        self.assertEqual(receipt['status'], 'accepted')
        self.assertTrue(box.mark_sent(job['id'], receipt))
        self.assertEqual(self.uploader.send(report)['status'], 'duplicate')
        with contextlib.closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT room_hint,map,server_sha256,binding_evidence,server_state_verified,payload FROM submissions').fetchone()
        self.assertEqual(row[:5], ('archive-room', 'fixture-map', sha, 'server_finished_archive', 0))
        self.assertEqual(json.loads(row[5]), report)
        self.assertFalse(any(key.startswith('client_') and key.endswith('sha256') for key in report))
        self.assertEqual(box.status()['sent'], 1)

    def test_end_to_end_outbox_uploader_receiver_and_duplicate_acknowledgement(self):
        box = Outbox(self.base_path / "outbox.sqlite3")
        report = payload()
        self.assertTrue(box.enqueue(report))
        job = box.due()[0]
        receipt = self.uploader.send(job["payload"])
        self.assertEqual(receipt["status"], "pending_match")
        self.assertTrue(box.mark_sent(job["id"], receipt))
        self.assertEqual(box.status()["sent"], 1)
        self.assertEqual(self.uploader.send(report)["status"], "duplicate")
        self.assertEqual(self.store.status()["submissions"]["total"], 1)

    def test_replay_hash_schema_over_http_matches_existing_analysis_without_room(self):
        archive = self.base_path / "analyzed.sqlite3"
        document = {"schema": 1, "source": {"origin": "manual_import", "sha256": "c" * 64},
                    "match": {"session_id": "fixture-session", "map": "fixture-map", "version": 168061,
                              "completion": {"status": "finished"}},
                    "players": [{"id": "123", "alliance": 0}]}
        with contextlib.closing(sqlite3.connect(archive)) as db:
            db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)")
            db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", ("c" * 64, "fixture-session", json.dumps(document), "finished"))
            db.execute("INSERT INTO report_sources VALUES(?,?,?)", ("c" * 64, "archive-room", "archive server"))
            db.commit()
        self.store.archive_index = ArchiveIndex(archive)
        box = Outbox(self.base_path / "replay-outbox.sqlite3")
        report = replay_payload()
        box.enqueue(report)
        job = box.due()[0]
        receipt = self.uploader.send(job["payload"])
        self.assertEqual(receipt["status"], "accepted")
        self.assertTrue(box.mark_sent(job["id"], receipt))
        self.assertEqual(self.uploader.send(report)["status"], "duplicate")
        with contextlib.closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute("SELECT room_hint,server_sha256,payload FROM submissions").fetchone()
        self.assertEqual(row[:2], ("archive-room", "c" * 64))
        self.assertEqual(json.loads(row[2]), report)
        self.assertNotIn("room_hint", json.loads(row[2]))
        unmatched = replay_payload("b" * 64, sha="d" * 64)
        self.assertEqual(self.uploader.send(unmatched)["status"], "pending_match")
        self.assertEqual(self.store.status()["pending_reasons"], {"replay_sha256_not_analyzed": 1})


    def test_missing_and_wrong_token_are_rejected_without_storing(self):
        body = json.dumps(payload()).encode()
        headers = self.headers(body)
        for auth in ([], [("Authorization", "Bearer wrong-fixture-token")]):
            with self.subTest(auth_present=bool(auth)):
                code, response = self.post(body, headers=auth + headers[1:])
                self.assertEqual(code, 401)
                self.assertEqual(response, {"error": "unauthorized"})
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def test_http_conflict_is_permanent_failure_and_keeps_original_payload(self):
        original = payload()
        self.uploader.send(original)
        changed = copy.deepcopy(original)
        changed["rows"] = []
        changed["report_data"]["total_rows"] = 0
        with self.assertRaises(UploadError) as caught:
            self.uploader.send(changed)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.store.status()["submissions"]["total"], 1)

    def test_content_length_missing_duplicated_and_oversized_are_rejected(self):
        body = b"{}"
        headers = self.headers(body)
        for replacements, expected in (([], 411), ([("Content-Length", "2"), ("Content-Length", "2")], 400),
                                        ([("Content-Length", str(MAX_PAYLOAD_BYTES + 1))], 413)):
            with self.subTest(expected=expected):
                filtered = [item for item in headers if item[0] != "Content-Length"]
                code, _ = self.post(body, headers=filtered + replacements)
                self.assertEqual(code, expected)

    def test_duplicate_json_fields_nonfinite_time_and_wrong_idempotency_are_rejected(self):
        report = payload()
        valid = json.dumps(report).encode()
        duplicate_key = valid[:-1] + b',"source":"server_collection"}'
        code, _ = self.post(duplicate_key, headers=self.headers(duplicate_key))
        self.assertEqual(code, 400)
        report["rows"][0]["game_time_seconds"] = float("nan")
        invalid = json.dumps(report).encode()
        code, _ = self.post(invalid, headers=self.headers(invalid))
        self.assertEqual(code, 400)
        headers = [(key, "b" * 64 if key == "Idempotency-Key" else value) for key, value in self.headers(valid)]
        code, _ = self.post(valid, headers=headers)
        self.assertEqual(code, 400)
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def test_health_contains_only_aggregates(self):
        self.uploader.send(payload())
        with request.urlopen(self.base + "/health", timeout=3) as response:
            health = json.loads(response.read())
        self.assertEqual(health["submissions"], {"pending_match": 1, "accepted": 0, "total": 1})
        text = json.dumps(health)
        self.assertNotIn(self.token, text)
        self.assertNotIn("fixture player", text)
        self.assertNotIn("fixture-session", text)


if __name__ == "__main__":
    unittest.main()
