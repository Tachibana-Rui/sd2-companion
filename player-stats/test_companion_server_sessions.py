"""Server session association uses exact authoritative IDs, never client files."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from companion.server_sessions import (
    ServerObservation, ServerSessionError, ServerSessionIndex, ServerSessionLedger,
    adapt_raw_server_state,
)


PLAYERS = [{"id": "123", "alliance": 0}, {"id": "456", "alliance": 1}]
EPOCH = "a" * 64
SID = "native-session-one"


def raw_state(phase="53"):
    return {"variables": {"GameState": phase, "Map": "map-one"},
            "sources": {"GameState": "serverlog.txt", "Map": "serverlog.txt"},
            "partial": False, "settings_scan": {"complete": True},
            "players": {"123": {"RequestState": phase, "PlayerAlliance": "0"},
                        "456": {"RequestState": phase, "PlayerAlliance": "1"},
                        "999": {"RequestState": "0", "PlayerAlliance": "1"}}}


def identity():
    return {"source": "server_replay_header", "session_id": SID, "map": "map-one",
            "game_build": 168061, "participants": PLAYERS}


def observation(phase=53, at=100, sequence=1, **values):
    return replace(ServerObservation("room-one", EPOCH, SID, phase, "map-one", 168061,
                                    (("123", 0), ("456", 1)), at, sequence,
                                    "server_native_game_setup"), **values)


def payload(**values):
    return {"session_id": SID, "game_build": 168061,
            "native_players": [{"player_user_id": p["id"], "alliance": p["alliance"]} for p in PLAYERS],
            **values}


class RawServerAdapterTests(unittest.TestCase):
    def adapt(self, raw=None, **values):
        arguments = {"server_id": "room-one", "server_epoch": EPOCH, "session_identity": identity(),
                     "online_player_ids": ["123", "456", "999"], "observed_at": 100, "sequence": 1}
        arguments.update(values)
        return adapt_raw_server_state(raw or raw_state(), **arguments)

    def test_actual_raw_shape_retains_complete_identity_and_excludes_outside_lobby(self):
        result = self.adapt()
        self.assertEqual(result.game_state, 53)
        self.assertEqual(result.participants, (("123", 0), ("456", 1)))
        self.assertEqual(result.session_id, SID)
        self.assertEqual(result.server_epoch, EPOCH)

    def test_rcon_phase_cannot_invent_game_session_or_build(self):
        for supplied in (None, {**identity(), "source": "rcon"},
                         {**identity(), "session_id": None}, {**identity(), "game_build": None}):
            with self.subTest(identity=supplied), self.assertRaises(ServerSessionError):
                self.adapt(session_identity=supplied)

    def test_startup_settings_partial_and_public_view_are_not_authoritative(self):
        variants = [{**raw_state(), "partial": True}, {**raw_state(), "settings_scan": None},
                    {**raw_state(), "settings_scan": {"complete": False}},
                    {**raw_state(), "sources": {"GameState": "variables.ini", "Map": "variables.ini"}},
                    {"connected": True, "state_fresh": True, "variables": {}, "players": []}]
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ServerSessionError):self.adapt(raw)

    def test_map_and_current_participant_alliance_must_agree(self):
        changed = raw_state();changed["players"]["123"]["PlayerAlliance"] = "1"
        bad_map = raw_state();bad_map["variables"]["Map"] = "other-map"
        for raw in (changed, bad_map):
            with self.subTest(raw=raw), self.assertRaises(ServerSessionError):self.adapt(raw)

    def test_disconnected_round_participants_stay_in_frozen_server_roster(self):
        self.assertEqual(self.adapt(online_player_ids=["123", "999"]).participants,
                         (("123", 0), ("456", 1)))

    def test_unavailable_online_details_unknown_phase_and_bad_epoch_are_rejected(self):
        for arguments in ({"online_player_ids": None}, {"online_player_ids": ["111"]},
                          {"server_epoch": ""}, {"sequence": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ServerSessionError):self.adapt(**arguments)
        with self.assertRaisesRegex(ServerSessionError, "unknown_server_phase"):
            self.adapt(raw_state("999"))


class ServerLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.temp.cleanup)
        self.clock = [100]
        self.ledger = ServerSessionLedger(Path(self.temp.name) / "server-ledger.sqlite3", clock=lambda: self.clock[0])

    def observe(self, phase, at, sequence, **values):
        self.clock[0] = at
        return self.ledger.observe(observation(phase, at, sequence, **values))

    def finished(self, **values):return self.ledger.finished_session("room-one", SID, **values)

    def test_server_observed_command_to_debrief_is_durable_finished(self):
        self.observe(53, 100, 1);self.observe(101, 102, 2)
        result = self.finished(server_epoch=EPOCH)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["finished_at"], 102)
        self.assertNotEqual(result["server_round_id"], SID)
        reloaded = ServerSessionLedger(self.ledger.path, clock=lambda: self.clock[0])
        self.assertEqual(reloaded.finished_session("room-one", SID)["server_round_id"], result["server_round_id"])

    def test_debrief_without_observed_running_is_not_finished(self):
        self.observe(101, 100, 1)
        self.assertIsNone(self.finished())
        self.observe(53, 101, 2);self.observe(101, 102, 3)
        self.assertIsNone(self.finished())  # The same ID cannot silently be reused.

    def test_loading_lobby_and_unknown_gap_do_not_confirm_completion(self):
        for middle in (0, 50, 51, 52):
            sid = "sid-" + str(middle)
            self.observe(53, 100, middle + 1, session_id=sid)
            self.observe(middle, 101, middle + 2, session_id=sid)
            self.observe(101, 102, middle + 3, session_id=sid)
            self.assertIsNone(self.ledger.finished_session("room-one", sid))
        self.observe(53, 110, 100)
        self.observe(101, 140, 101)
        self.assertIsNone(self.finished())

    def test_epoch_change_or_observer_gap_cannot_finish_previous_running_game(self):
        self.observe(53, 100, 1)
        self.observe(101, 101, 0, server_epoch="b" * 64)
        self.assertIsNone(self.finished())
        self.observe(53, 102, 1, server_epoch="b" * 64, session_id="another-session")
        self.ledger.invalidate("room-one")
        self.observe(101, 103, 2, server_epoch="b" * 64, session_id="another-session")
        self.assertIsNone(self.ledger.finished_session("room-one", "another-session"))

    def test_stale_or_reordered_events_cannot_create_new_completion(self):
        self.observe(53, 100, 5)
        self.assertFalse(self.observe(101, 101, 4))
        self.assertIsNone(self.finished())
        self.clock[0] = 110
        with self.assertRaisesRegex(ServerSessionError, "server_snapshot_stale"):
            self.ledger.observe(observation(101, 101, 6))
        self.assertIsNone(self.finished())

    def test_same_roster_consecutive_sessions_are_distinct_not_latest_guesses(self):
        self.observe(53, 100, 1);self.observe(101, 101, 2)
        original = self.finished()["server_round_id"]
        self.observe(53, 102, 3, session_id="second-session")
        self.observe(101, 103, 4, session_id="second-session")
        self.assertEqual(self.finished()["server_round_id"], original)
        self.assertNotEqual(self.ledger.finished_session("room-one", "second-session")["server_round_id"], original)

    def test_duplicate_completion_does_not_extend_expiry_and_metadata_changes_block(self):
        self.observe(53, 100, 1);self.observe(101, 101, 2);self.observe(101, 102, 3)
        self.assertEqual(self.finished()["finished_at"], 101)
        self.clock[0] = 101 + 86401
        self.assertIsNone(self.finished())
        self.observe(53, 200000, 10, session_id="changed")
        self.observe(53, 200001, 11, session_id="changed", map="wrong-map")
        self.observe(101, 200002, 12, session_id="changed")
        self.assertIsNone(self.ledger.finished_session("room-one", "changed"))

    def test_session_reuse_across_server_epochs_needs_exact_epoch(self):
        self.observe(53, 100, 1);self.observe(101, 101, 2)
        self.observe(53, 102, 1, server_epoch="b" * 64)
        self.assertIsNone(self.finished())
        self.assertIsNotNone(self.finished(server_epoch=EPOCH))
        self.assertIsNone(self.finished(server_epoch="b" * 64))

    def test_missing_observation_database_is_not_recreated_by_read_only_lookup(self):
        self.ledger.path.unlink()
        with self.assertRaises(sqlite3.Error):self.ledger.session_evidence('room-one', SID)
        self.assertFalse(self.ledger.path.exists())

    def test_manual_observation_objects_still_require_validated_fields(self):
        for values in ({"game_state": 999}, {"sequence": True}, {"game_build": True},
                       {"identity_source": "client"}, {"participants": (("123", None),)}):
            with self.subTest(values=values), self.assertRaises(ServerSessionError):
                self.ledger.observe(observation(**values))


class ServerArchiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.base = Path(self.temp.name);self.path = self.base / "stats.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT);"
                             "CREATE TABLE report_sources(sha256 TEXT,server_id TEXT);")
        self.sha = "a" * 64
        self.add_report()

    def add_report(self, *, sha=None, session=SID, room="room-one", origin="server_collection", finished=True,
                   players=None, map_id="map-one", build=168061):
        sha = sha or self.sha
        report = {"schema": 1, "source": {"origin": origin, "sha256": sha},
                  "match": {"session_id": session, "map": map_id, "version": build,
                            "completion": {"status": "finished" if finished else "unverified"}},
                  "players": players or PLAYERS}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", (sha, "server:" + room + ":" + session,
                       json.dumps(report), "finished" if finished else "unverified"))
            db.execute("INSERT INTO report_sources VALUES(?,?)", (sha, room))
        return report

    def test_native_session_needs_no_client_file_sha_room_or_epoch(self):
        original = self.path.read_bytes()
        result = ServerSessionIndex(self.path)(payload())
        self.assertEqual(result["server_sha256"], self.sha)
        self.assertEqual(result["archive_rooms"], ["room-one"])
        self.assertEqual(result["archive_map"], "map-one")
        self.assertEqual(result["binding_evidence"], "server_finished_archive")
        self.assertFalse(result["server_state_verified"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_server_session_ledger_cannot_modify_archive_or_receiver_database(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ServerSessionError, 'separate_database'):
            ServerSessionLedger(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        inbox = self.base / 'inbox.sqlite3'
        with closing(sqlite3.connect(inbox)) as db, db:
            db.execute('CREATE TABLE submissions(id TEXT)')
        before = inbox.read_bytes()
        with self.assertRaisesRegex(ServerSessionError, 'separate_database'):
            ServerSessionLedger(inbox)
        self.assertEqual(inbox.read_bytes(), before)

    def test_client_import_unfinished_wrong_map_build_roster_never_bind(self):
        variants = [{"origin": "local_import"}, {"finished": False}, {"map_id": "wrong"},
                    {"build": 1}, {"players": [{"id": "123", "alliance": 1}, {"id": "456", "alliance": 1}]}]
        for index, variant in enumerate(variants):
            sid = "bad-" + str(index)
            self.add_report(sha=str(index) * 64, session=sid, **variant)
            result = ServerSessionIndex(self.path)(payload(session_id=sid, map="map-one"))
            self.assertNotIn("server_sha256", result)

    def test_wrong_room_and_missing_session_cannot_use_same_roster(self):
        index = ServerSessionIndex(self.path)
        self.assertNotIn("server_sha256", index(payload(room_hint="other-room")))
        self.assertNotIn("server_sha256", index(payload(session_id="not-the-session")))
        self.add_report(sha="b" * 64, session="next-session")
        self.assertEqual(index(payload())["server_sha256"], self.sha)
        self.assertEqual(index(payload(session_id="next-session"))["server_sha256"], "b" * 64)

    def test_duplicate_exact_session_archives_are_ambiguous_not_newest(self):
        self.add_report(sha="b" * 64)
        result = ServerSessionIndex(self.path)(payload())
        self.assertEqual(result["pending_reason"], "server_session_archive_ambiguous")

    def test_same_file_multiple_rooms_preserves_all_sources_and_filters_observers(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO report_sources VALUES(?,?)", (self.sha, "room-two"))
            report = json.loads(db.execute("SELECT payload FROM reports").fetchone()[0])
            report["players"].append({"id": "999", "alliance": None, "is_observer": True})
            db.execute("UPDATE reports SET payload=?", (json.dumps(report),))
        result = ServerSessionIndex(self.path)(payload())
        self.assertEqual(result["archive_rooms"], ["room-one", "room-two"])

    def test_unique_server_transition_derives_epoch_without_any_client_epoch(self):
        clock = [100]
        ledger = ServerSessionLedger(self.base / "ledger.sqlite3", clock=lambda: clock[0])
        ledger.observe(observation(53, 100, 1));clock[0] = 101;ledger.observe(observation(101, 101, 2))
        index = ServerSessionIndex(self.path, ledger=ledger)
        automatic = index(payload())
        self.assertTrue(automatic["server_state_verified"])
        self.assertEqual(automatic["server_epoch"], EPOCH)
        self.assertEqual(automatic["server_observations"][0]["server_epoch"], EPOCH)
        self.assertNotIn("server_sha256", index(payload(server_epoch="b" * 64)))
        exact = index(payload(server_epoch=EPOCH))
        self.assertTrue(exact["server_state_verified"])
        self.assertEqual(exact["server_observations"][0]["server_epoch"], EPOCH)
        required = ServerSessionIndex(self.path, ledger=ledger, require_live_observation=True)
        self.assertEqual(required(payload())["server_sha256"], self.sha)
        self.assertEqual(required(payload(server_epoch=EPOCH))["server_sha256"], self.sha)

    def test_no_related_ledger_record_allows_only_optional_archive_evidence(self):
        clock = [100]
        ledger = ServerSessionLedger(self.base / 'unrelated-ledger.sqlite3', clock=lambda: clock[0])
        ledger.observe(observation(53, 100, 1, session_id='another-session'))
        index = ServerSessionIndex(self.path, ledger=ledger)
        result = index(payload())
        self.assertEqual(result['server_sha256'], self.sha)
        self.assertFalse(result['server_state_verified'])
        self.assertIsNone(result['server_epoch'])
        strict = ServerSessionIndex(self.path, ledger=ledger, require_live_observation=True)
        self.assertNotIn('server_sha256', strict(payload()))

    def test_same_native_session_across_epochs_is_pending_not_latest_finished(self):
        clock = [100]
        ledger = ServerSessionLedger(self.base / 'reused-ledger.sqlite3', clock=lambda: clock[0])
        ledger.observe(observation(53, 100, 1));clock[0] = 101;ledger.observe(observation(101, 101, 2))
        clock[0] = 102;ledger.observe(observation(53, 102, 1, server_epoch='b' * 64))
        evidence = ledger.session_evidence('room-one', SID)
        self.assertEqual(evidence['status'], 'ambiguous')
        index = ServerSessionIndex(self.path, ledger=ledger)
        self.assertEqual(index(payload())['pending_reason'], 'server_live_state_ambiguous')
        clock[0] = 103;ledger.observe(observation(101, 103, 2, server_epoch='b' * 64))
        self.assertEqual(index(payload())['pending_reason'], 'server_live_state_ambiguous')
        self.assertEqual(index(payload(server_epoch=EPOCH))['server_epoch'], EPOCH)
        self.assertEqual(index(payload(server_epoch='b' * 64))['server_epoch'], 'b' * 64)

    def test_known_unfinished_interrupted_expired_or_conflicting_state_blocks_even_optional_mode(self):
        cases = (({}, 'running', 'server_live_session_not_finished'),
                 ({}, 'gap', 'server_live_session_interrupted'),
                 ({}, 'expired', 'server_live_observation_expired'),
                 ({'map': 'another-map'}, 'finished', 'server_live_metadata_mismatch'),
                 ({'game_build': 123}, 'finished', 'server_live_metadata_mismatch'),
                 ({'participants': (('123', 1), ('456', 1))}, 'finished', 'server_live_metadata_mismatch'))
        for number, (changes, state, reason) in enumerate(cases):
            with self.subTest(state=state, changes=changes):
                clock = [100]
                ledger = ServerSessionLedger(self.base / ('conflict-' + str(number) + '.sqlite3'), clock=lambda: clock[0])
                ledger.observe(observation(53, 100, 1, **changes))
                if state != 'running':
                    at = 130 if state == 'gap' else 101
                    clock[0] = at
                    ledger.observe(observation(101, at, 2, **changes))
                if state == 'expired':clock[0] = 101 + 86401
                for strict in (False, True):
                    result = ServerSessionIndex(self.path, ledger=ledger, require_live_observation=strict)(payload())
                    self.assertEqual(result['pending_reason'], reason)
                    self.assertNotIn('server_sha256', result)

    def test_archived_multiple_rooms_derive_only_one_observed_source_or_reject_multiple(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO report_sources VALUES(?,?)', (self.sha, 'room-two'))
        clock = [100]
        ledger = ServerSessionLedger(self.base / 'multi-room-ledger.sqlite3', clock=lambda: clock[0])
        ledger.observe(observation(53, 100, 1));clock[0] = 101;ledger.observe(observation(101, 101, 2))
        index = ServerSessionIndex(self.path, ledger=ledger, require_live_observation=True)
        result = index(payload())
        self.assertTrue(result['server_state_verified'])
        self.assertEqual(result['server_observations'][0]['server_id'], 'room-one')
        clock[0] = 102;ledger.observe(observation(53, 102, 1, server_id='room-two'))
        clock[0] = 103;ledger.observe(observation(101, 103, 2, server_id='room-two'))
        self.assertEqual(index(payload())['pending_reason'], 'server_live_state_ambiguous')
        self.assertTrue(index(payload(room_hint='room-one'))['server_state_verified'])

    def test_pending_archive_is_rechecked_when_server_completion_arrives(self):
        sid = "later-session";index = ServerSessionIndex(self.path)
        self.assertNotIn("server_sha256", index(payload(session_id=sid)))
        self.add_report(sha="b" * 64, session=sid)
        self.assertEqual(index(payload(session_id=sid))["server_sha256"], "b" * 64)


if __name__ == '__main__':unittest.main()
