"""Automatic replay capture invariants, using synthetic file I/O and reports."""
from concurrent.futures import Future
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.outbox import Outbox
from companion.replay_capture import ReplayCaptureService, ReplayFileError, inspect_replay_file


def replay_bytes(*, session="session-one", marker=b"native-command-data", player_id="123"):
    header = json.dumps({"game": {"UniqueSessionId": session, "Version": "168061", "Map": "map-one"},
                         "player_0": {"PlayerUserId": player_id, "PlayerName": "Player", "PlayerAlliance": "0"}}).encode()
    prefix = struct.pack(">4sQI", b"rply", len(header), 0) + header
    stream = struct.pack(">4sQI", b"strm", len(marker), 0) + marker
    return struct.pack(">4sIII", b"ESAV", 3, 16 + len(prefix) + len(stream), 0) + prefix + stream


def native_report(generation=1, *, player_id="123", rows=None):
    copied_rows = []
    for row in rows or []:
        copied = {**row, "time_display": row.get("time_display", "00:01")}
        for role in ("killer", "victim"):
            actor = row[role]
            copied[role] = {"player_name": "Player", "soldier_name": "", "unit_name": "Tank",
                "unit_label_texture_token": 1, "xp_texture_token": 2, "display_veterancy": 0,
                "player_label_color_token": 3, "is_ace": False, "unit_guid": None,
                "player_user_id": player_id if actor.get("player_name") == "Player" else None,
                "identity_status": "unresolved", **actor}
        copied_rows.append(copied)
    return {"variant_address": 0x100000, "result_address": 0x200000 + generation * 0x1000,
            "generation": generation, "game_build": 168061,
            "players": [{"row_index": 0, "name": "Player", "player_user_id": player_id,
                         "alliance": 0, "alliance_display_value": 0,
                         "official_kill_points": 4321, "official_loss_points": 1234}],
            "metadata": {"exe_sha256": "2986600db813a24a92f59698ac1b250d4ae7a96b84f2000bc1ad5a53bbfd1cf6",
                         "report_data": {"identity_source": "unavailable", "identity_status": "unavailable",
                                         "matched_rows": 0, "total_rows": len(copied_rows)}},
            "rows": copied_rows}


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class DeferredExecutor:
    def __init__(self):self.pending = []
    def submit(self, function, *args, **kwargs):
        future = Future();self.pending.append((future, function, args, kwargs));return future
    def finish(self):
        pending, self.pending = self.pending, []
        for future, function, args, kwargs in pending:
            if future.cancelled():continue
            try:future.set_result(function(*args, **kwargs))
            except Exception as exc:future.set_exception(exc)


class Observer:
    def __init__(self):self.observation = {};self.closed = False
    def poll(self, now):return dict(self.observation)
    def close(self):self.closed = True


class ReplayFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "game.rpl3"
        self.path.write_bytes(replay_bytes())

    def test_full_file_hash_and_metadata_preserve_original_bytes(self):
        snapshot = inspect_replay_file(self.path)
        self.assertEqual(snapshot.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(snapshot.identity.session_id, "session-one")
        self.assertNotEqual(snapshot.sha256, snapshot.identity.header_sha256)
        self.assertLess(snapshot.stream_offset, snapshot.size)

    def test_header_only_access_never_hashes_or_claims_playback(self):
        snapshot = inspect_replay_file(self.path, events=((0, 40, "read", 1.0, 1),))
        self.assertIsNone(snapshot.sha256)

    def test_incomplete_download_wrong_size_and_unsupported_header_are_rejected(self):
        data = self.path.read_bytes()
        for value in (data[:-1], data + b"unexpected tail", b"garbage" * 20):
            with self.subTest(size=len(value)):
                self.path.write_bytes(value)
                with self.assertRaises(ReplayFileError):inspect_replay_file(self.path)

    def test_size_limit_is_checked_before_hashing(self):
        with self.assertRaisesRegex(ReplayFileError, "replay_file_size_out_of_range"):
            inspect_replay_file(self.path, max_bytes=64)

    def test_content_change_during_hash_is_not_published(self):
        original = hashlib.sha256
        path = self.path
        class MutatingDigest:
            def __init__(self):self.digest = original();self.changed = False
            def update(self, block):
                self.digest.update(block)
                if not self.changed:
                    path.write_bytes(path.read_bytes() + b"new bytes")
                    self.changed = True
            def hexdigest(self):return self.digest.hexdigest()
        # Header parsing also calls sha256(data), which must remain ordinary.
        def digest(*args):return original(*args) if args else MutatingDigest()
        with patch("companion.replay_capture.hashlib.sha256", side_effect=digest):
            with self.assertRaisesRegex(ReplayFileError, "replay_file_changed"):
                inspect_replay_file(path)


class ReplayCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name);self.path = self.base / "game.rpl3"
        self.path.write_bytes(replay_bytes())
        # No room IDs, server endpoints or replay directory list are needed.
        self.config = SimpleNamespace(data_directory=self.base / "data", report_timeout_seconds=1000)
        self.observer = Observer();self.clock = 10.0;self.emitted = []
        self.outbox = Outbox(self.base / "outbox.sqlite3", clock=lambda: self.clock)
        self.service = ReplayCaptureService(self.config, self.observer, self.outbox,
                                            emit=self.emitted.append, clock=lambda: self.clock,
                                            executor=ImmediateExecutor())
        self.addCleanup(self.service.close)
        self.tick(baseline_ready=True)

    def tick(self, **observation):
        self.clock += 1
        self.observer.observation = {"process_key": "p", **observation}
        return self.service.tick()

    def event(self, path=None, *, operation="read", offset=0, size=10000, sequence=1, at=None, process_key="p"):
        return {"process_key": process_key, "path": str(path or self.path), "byte_offset": offset,
                "read_size": size, "operation": operation, "sequence": sequence,
                "observed_at": self.clock + 1 if at is None else at}

    def finish(self):
        for _ in range(3):self.tick()

    def payload(self):return self.outbox.due(now=1e12)[0]["payload"]

    def exports(self):return list((self.config.data_directory / "exports").glob("*/*.json"))

    def test_new_native_report_exports_and_enqueues_by_complete_hash_without_room(self):
        row = {"row_index": 0, "game_time_seconds": 1.0,
               "killer": {"player_name": "Player", "unit_name": "Tank"},
               "victim": {"player_name": "Player", "unit_name": "AT"}}
        second = {**row, "row_index": 1}
        self.tick(file_events=[self.event()]);self.tick(report=native_report(rows=[row, second]));self.finish()
        payload = self.payload()
        self.assertEqual(payload["client_replay_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotIn("room_hint", payload)
        self.assertEqual(payload["schema"], "sd2-task-report-v4")
        self.assertEqual(payload["binding_mode"], "replay_sha256")
        self.assertEqual(payload["rows"], native_report(rows=[row, second])["rows"])
        self.assertEqual(payload["native_players"], native_report()["players"])
        self.assertEqual(payload["client_context_evidence"]["association"], "observed_game_file_read")
        self.assertEqual(json.loads(self.exports()[0].read_text(encoding="utf-8")), payload)
        text = json.dumps(payload)
        self.assertNotIn(str(self.path), text)
        self.assertNotIn("result_address", text)
        self.assertEqual(self.outbox.status()["pending"], 1)
        self.assertTrue(any(event.get("replay_sha256") == payload["client_replay_sha256"] for event in self.emitted))

    def test_cloud_local_save_body_write_can_identify_same_exact_bytes(self):
        self.tick(file_events=[self.event(operation="write")]);self.tick(report=native_report());self.finish()
        self.assertEqual(self.payload()["client_context_evidence"]["association"], "observed_game_file_write")

    def native_session(self, session_id="session-one", map_id="map-one", build=168061):
        return {"session_id": session_id, "game_build": build, "map": map_id,
                "source_kind": "replay", "is_replay": True,
                "server_name": "Replay room",
                "participants": [{"id": "123", "name": "Player", "alliance": 0}]}

    def native_playback(self, *, ended=False, session_id="session-one", **changes):
        result = {**self.native_session(session_id=session_id), "game_ended": ended,
                  "context_epoch": hashlib.sha256(session_id.encode()).hexdigest()}
        result.update(changes)
        return result

    def test_full_native_replay_without_file_events_is_queued_with_honest_replay_evidence(self):
        self.tick(native_session=self.native_playback(), report_context_stable=True)
        report = native_report()
        self.tick(native_session=self.native_playback(ended=True), report=report, report_context_stable=True)
        payload = self.payload()
        self.assertEqual(payload["binding_mode"], "native_session")
        self.assertEqual(payload["client_context_evidence"]["association"], "native_replay_session")
        self.assertEqual(payload["client_context_evidence"]["native_source_kind"], "replay")
        self.assertTrue(payload["client_context_evidence"]["observed_running"])
        self.assertTrue(payload["client_context_evidence"]["report_observed_after_end"])
        self.assertNotIn("file_body_io_observed", payload["client_context_evidence"])
        self.assertNotIn("client_replay_sha256", payload)
        self.assertEqual(payload["rows"], report["rows"])
        self.assertEqual(payload["native_players"], report["players"])
        paths = list((self.config.data_directory / "exports" / "replay-session").glob("*/*.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), payload)
        local = json.loads(next((self.config.data_directory / "unbound-replays").glob("*.json")).read_text())
        self.assertEqual(local["source_kind"], "replay")
        self.assertEqual(local["session_origin"], "native_replay_context")

    def test_native_replay_capture_survives_unavailable_or_uncertain_file_monitor(self):
        self.tick(native_session=self.native_playback(), report_context_stable=True, access_uncertain=True)
        self.tick(native_session=self.native_playback(ended=True), report=native_report(),
                  report_context_stable=True, access_uncertain=True)
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(self.payload()["client_context_evidence"]["association"], "native_replay_session")

    def test_report_seen_before_replay_end_cannot_acquire_later_end_evidence(self):
        self.tick(native_session=self.native_playback(), report_context_stable=True, report=native_report())
        self.tick(native_session=self.native_playback(ended=True), report_context_stable=True, report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=self.native_playback(ended=True), report_context_stable=True, report=native_report(2))
        self.assertEqual(self.payload()["report_generation"], 2)

    def test_late_start_or_changed_unstable_wrong_native_replay_stays_local(self):
        cases = [({"ended": True}, {}, True),
                 ({"ended": False}, {"session_id": "next-session"}, True),
                 ({"ended": False}, {}, False),
                 ({"ended": False}, {"game_build": 168062}, True),
                 ({"ended": False}, {"participants": [{"id": "456", "alliance": 0}]}, True),
                 ({"ended": False}, {"source_kind": "network_live", "is_replay": False}, True)]
        for index, (start, changes, stable) in enumerate(cases):
            with self.subTest(index=index):
                process = "native-case-" + str(index)
                self.tick(process_key=process, baseline_ready=True,
                          native_session=self.native_playback(**start), report_context_stable=True)
                self.tick(process_key=process, native_session=self.native_playback(ended=True, **changes),
                          report=native_report(index + 1), report_context_stable=stable)
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_native_replay_queue_retry_keeps_original_report_identity_after_context_change(self):
        self.tick(native_session=self.native_playback(), report_context_stable=True)
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tick(native_session=self.native_playback(ended=True), report_context_stable=True,
                          report=native_report())
        self.tick(process_key=None)
        payload = self.payload()
        self.assertEqual(payload["session_id"], "session-one")
        self.assertEqual(payload["client_context_evidence"]["association"], "native_replay_session")
        self.assertEqual(len(list((self.config.data_directory / "exports").rglob("*.json"))), 1)

    def test_native_replay_epoch_is_consumed_once_and_header_probes_do_not_replace_queued_status(self):
        self.tick(native_session=self.native_playback(), report_context_stable=True)
        self.tick(native_session=self.native_playback(ended=True), report=native_report(), report_context_stable=True)
        self.tick(native_session=self.native_playback(ended=True), file_events=[self.event(size=40)],
                  report_context_stable=True)
        self.assertEqual(self.service.status, "replay_report_queued")
        self.tick(native_session=self.native_playback(ended=True), report=native_report(2), report_context_stable=True)
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_native_replay_identity_is_retained_without_file_events(self):
        row = {"row_index": 0, "game_time_seconds": 3598.56,
               "killer": {"player_name": "Player", "unit_name": "Tank"},
               "victim": {"player_name": "Player", "unit_name": "AT"}}
        report = native_report(rows=[row, {**row, "row_index": 1}])
        self.tick(report=report, native_session=self.native_session(), report_context_stable=True)
        self.finish()
        paths = list((self.config.data_directory / "unbound-replays").glob("*.json"))
        self.assertEqual(len(paths), 1)
        local = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual((local["session_id"], local["map"], local["server_name"]),
                         ("session-one", "map-one", "Replay room"))
        self.assertEqual(local["session_origin"], "native_replay_context")
        self.assertEqual(local["report"]["rows"], report["rows"])
        self.assertNotIn("client_replay_sha256", local["report"])
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(sum(event["event"] == "task_report_captured" for event in self.emitted), 1)

    def test_replay_metadata_requires_stable_context_exact_build_and_roster(self):
        bad_roster = [{"id": "999", "alliance": 0}]
        bad_side = [{"id": "123", "alliance": 1}]
        variants = [({"game_build": 168062}, True), ({"participants": bad_roster}, True),
                    ({"participants": bad_side}, True), ({}, False),
                    ({"source_kind": "network_live", "is_replay": False}, True)]
        for generation, (changes, stable) in enumerate(variants, 1):
            with self.subTest(changes=changes, stable=stable):
                self.tick(report_identity_clear=True)
                self.tick(report=native_report(generation),
                          native_session={**self.native_session(), **changes}, report_context_stable=stable)
                local = next(json.loads(path.read_text(encoding="utf-8"))
                             for path in (self.config.data_directory / "unbound-replays").glob("*.json")
                             if json.loads(path.read_text(encoding="utf-8"))["report"]["report_generation"] == generation)
                self.assertNotIn("session_id", local)
                self.assertNotIn("map", local)
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_native_replay_metadata_wins_over_a_different_listed_header(self):
        self.tick(file_events=[self.event(size=40)])
        self.tick(report=native_report(), native_session=self.native_session("native-session", "native-map"),
                  report_context_stable=True)
        self.finish()
        path = next((self.config.data_directory / "unbound-replays").glob("*.json"))
        local = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((local["session_id"], local["map"]), ("native-session", "native-map"))
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_late_header_metadata_enriches_same_snapshot_without_recapturing(self):
        executor = DeferredExecutor()
        self.service._executor = executor
        self.tick(file_events=[self.event(size=40)], report=native_report())
        path = next((self.config.data_directory / "unbound-replays").glob("*.json"))
        original = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("session_id", original)
        captured_at = path.stat().st_mtime_ns
        original["local_annotation"] = "keep this"
        path.write_text(json.dumps(original), encoding="utf-8")
        import os
        os.utime(path, ns=(path.stat().st_atime_ns, captured_at))
        executor.finish()
        self.tick()
        local = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(local["session_id"], "session-one")
        self.assertEqual(local["map"], "map-one")
        self.assertEqual(local["report"], original["report"])
        self.assertEqual(local["local_annotation"], "keep this")
        self.assertEqual(path.stat().st_mtime_ns, captured_at)
        self.assertEqual(len(list(path.parent.glob("*.json"))), 1)
        self.assertEqual(sum(event["event"] == "task_report_captured" for event in self.emitted), 1)
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_disk_retry_keeps_the_original_native_replay_metadata(self):
        with patch("companion.replay_capture.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tick(report=native_report(), native_session=self.native_session(), report_context_stable=True)
        self.tick(report_identity_clear=True,
                  native_session=self.native_session("future-session", "future-map"), report_context_stable=True)
        path = next((self.config.data_directory / "unbound-replays").glob("*.json"))
        local = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((local["session_id"], local["map"]), ("session-one", "map-one"))
        self.assertEqual(local["session_origin"], "native_replay_context")
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_positive_native_replay_session_selects_exact_sid_among_identical_rosters(self):
        other = self.base / "other-session.rpl3"
        other.write_bytes(replay_bytes(session="other-session", marker=b"different commands"))
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report(), native_session=self.native_session(), report_context_stable=True)
        self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(self.payload()["session_id"], "session-one")
        self.assertEqual(self.payload()["client_replay_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_native_replay_sid_map_and_build_each_constrain_file_association(self):
        for changes in ({"session_id": "different-session"}, {"map_id": "different-map"}, {"build": 168062}):
            with self.subTest(changes=changes):
                generation = 1 + len(list((self.config.data_directory / "unbound-replays").glob("*.json")))
                self.tick(report_identity_clear=True, file_events=[self.event(sequence=generation)])
                self.tick(report=native_report(generation), native_session=self.native_session(**changes),
                          report_context_stable=True)
                self.finish()
                self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(list((self.config.data_directory / "unbound-replays").glob("*.json"))), 3)

    def test_report_without_native_session_cannot_borrow_a_future_replay_identity(self):
        other = self.base / "other-session.rpl3"
        other.write_bytes(replay_bytes(session="other-session", marker=b"different commands"))
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report())
        self.tick(native_session=self.native_session("other-session"), report_context_stable=True)
        self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(self.service.status, "replay_report_waiting_for_unique_file")

    def test_unbound_capture_is_public_v3_and_not_a_queued_upload(self):
        self.tick(report=native_report())
        paths = list((self.config.data_directory / "unbound-replays").glob("*.json"))
        self.assertEqual(len(paths), 1)
        local = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(local["schema"], "sd2-local-unbound-task-report-v4")
        self.assertEqual(local["report"]["report_generation"], 1)
        self.assertEqual(local["report"]["native_players"][0]["official_kill_points"], 4321)
        for private in ("variant_address", "result_address", "metadata", "generation"):
            self.assertNotIn(private, local["report"])
        self.assertEqual(local["report"]["report_data"]["identity_source"], "unavailable")
        self.assertEqual(self.outbox.status()["total"], 0)
        event = next(e for e in self.emitted if e["event"] == "task_report_captured")
        self.assertEqual((event["official_score_players"], event["total_players"]), (1, 1))
        self.assertEqual((event["resolved_actors"], event["total_actors"]), (0, 0))

    def test_listing_header_reads_do_not_trigger_capture(self):
        self.tick(file_events=[self.event(size=40)]);self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(self.exports(), [])

    def test_129_list_headers_do_not_consume_body_candidate_limit(self):
        events = []
        for index in range(129):
            path = self.base / f"listed-{index}.rpl3"
            path.write_bytes(replay_bytes(session=f"listed-{index}"))
            events.append(self.event(path, size=40, sequence=index + 1))
        self.tick(file_events=events);self.tick()
        self.tick(file_events=[self.event(sequence=130)])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(self.payload()["session_id"], "session-one")

    def test_129_buffered_body_probes_allow_the_only_exact_roster_match(self):
        events = []
        for index in range(129):
            path = self.base / f"buffered-{index}.rpl3"
            path.write_bytes(replay_bytes(session=f"buffered-{index}", player_id=str(1000 + index)))
            events.append(self.event(path, size=10000, sequence=index + 1))
        events.append(self.event(sequence=130))
        self.tick(file_events=events);self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(self.payload()["session_id"], "session-one")

    def test_existing_report_is_a_baseline_even_if_same_replay_file_is_accessed(self):
        service = ReplayCaptureService(self.config, self.observer, self.outbox, clock=lambda: self.clock,
                                        executor=ImmediateExecutor())
        self.addCleanup(service.close)
        old = native_report()
        self.observer.observation = {"process_key": "p", "baseline_ready": True, "baseline_report": old,
                                     "file_events": [self.event(at=self.clock)]}
        service.tick()
        self.observer.observation = {"process_key": "p", "report": old}
        service.tick();service.tick()
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_unknown_initial_baseline_does_not_call_first_report_a_new_game(self):
        service = ReplayCaptureService(self.config, self.observer, self.outbox, clock=lambda: self.clock,
                                        executor=ImmediateExecutor())
        self.addCleanup(service.close)
        self.observer.observation = {"process_key": "p", "file_events": [self.event(at=self.clock)], "report": native_report()}
        service.tick();service.tick()
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_wrong_process_and_roster_are_not_guessed(self):
        self.tick(file_events=[self.event(process_key="other-process")])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(file_events=[self.event(sequence=2)])
        self.tick(report=native_report(2, player_id="999"));self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_two_files_with_same_roster_remain_ambiguous_instead_of_picking_latest(self):
        other = self.base / "other.rpl3";other.write_bytes(replay_bytes(session="other-session", marker=b"different commands"))
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(self.service.status, "replay_report_waiting_for_unique_file")

    def test_two_paths_with_identical_bytes_are_one_file_identity(self):
        other = self.base / "copy.rpl3";other.write_bytes(self.path.read_bytes())
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_losing_one_ambiguous_file_does_not_prove_the_other_was_played(self):
        other = self.base / "other.rpl3";other.write_bytes(replay_bytes(session="other-session", marker=b"other"))
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.path.write_bytes(replay_bytes(marker=b"changed bytes"))
        self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_new_native_clear_discards_previous_unresolved_candidates(self):
        other = self.base / "other.rpl3";other.write_bytes(replay_bytes(session="other-session", marker=b"other"))
        self.tick(file_events=[self.event(), self.event(other, sequence=2)])
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(report_identity_clear=True, file_events=[self.event(other, sequence=3)])
        self.tick(report=native_report(2));self.finish()
        self.assertEqual(self.payload()["session_id"], "other-session")

    def test_next_replay_body_read_before_result_clear_is_preserved(self):
        self.tick(file_events=[self.event()]);self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)
        other = self.base / "next.rpl3";other.write_bytes(replay_bytes(session="next-session", marker=b"next commands"))
        self.tick(file_events=[self.event(other, sequence=2)])
        self.tick(report_identity_clear=True)
        self.tick(report=native_report(2));self.finish()
        self.assertEqual(self.outbox.status()["total"], 2)
        self.assertEqual({job["payload"]["session_id"] for job in self.outbox.due(now=1e12)},
                         {"session-one", "next-session"})

    def test_report_can_wait_for_hash_and_complete_after_original_process_exits(self):
        executor = DeferredExecutor();self.service._executor = executor
        self.tick(file_events=[self.event()]);self.tick(report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        executor.finish()
        self.observer.observation = {"error": "game_not_running"}
        self.clock += 1;self.service.tick()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_late_file_access_cannot_claim_a_report_already_observed(self):
        self.tick(report=native_report());self.tick(file_events=[self.event()]);self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_changed_file_after_hash_requires_new_game_file_io(self):
        self.tick(file_events=[self.event()]);self.tick()
        self.path.write_bytes(replay_bytes(session="replacement", marker=b"different"))
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(file_events=[self.event(sequence=2)])
        self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)  # The old report cannot claim replacement bytes.
        self.tick(report=native_report(2));self.finish()
        self.assertEqual(self.payload()["session_id"], "replacement")

    def test_queued_hash_cannot_apply_new_bytes_to_old_read_and_report(self):
        executor = DeferredExecutor();self.service._executor = executor
        self.tick(file_events=[self.event()]);self.tick(report=native_report())
        self.path.write_bytes(replay_bytes(marker=b"replacement commands"))
        executor.finish();self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(file_events=[self.event(sequence=2)])
        executor.finish();self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(report=native_report(2));self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_incomplete_cloud_download_retries_without_losing_native_report(self):
        data = self.path.read_bytes();self.path.write_bytes(data[:-4])
        self.tick(file_events=[self.event(operation="write")]);self.tick(report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.path.write_bytes(data);self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_access_loss_blocks_binding_until_a_new_clear_epoch(self):
        self.tick(file_events=[self.event()], access_uncertain=True)
        self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(report_identity_clear=True, file_events=[self.event(sequence=2)])
        self.tick(report=native_report(2));self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_durable_enqueue_failure_retries_same_report_without_consuming_it(self):
        self.tick(file_events=[self.event()]);self.tick()
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):self.tick(report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(len(self.exports()), 1)

    def test_queue_failure_survives_native_clear_before_retry(self):
        self.tick(file_events=[self.event()]);self.tick()
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):self.tick(report=native_report())
        self.tick(report_identity_clear=True)
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(len(self.exports()), 1)

    def test_local_snapshot_failure_survives_report_clear_before_retry(self):
        with patch("companion.replay_capture.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):self.tick(report=native_report())
        self.tick(report_identity_clear=True)
        self.assertEqual(len(list((self.config.data_directory / "unbound-replays").glob("*.json"))), 1)
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_long_contiguous_read_sequence_is_compacted_without_event_loss(self):
        self.path.write_bytes(replay_bytes(marker=b"x" * 50000))
        events = [self.event(offset=i * 64, size=64, sequence=i + 1) for i in range(600)]
        self.tick(file_events=events);self.tick(report=native_report());self.finish()
        self.assertEqual(self.outbox.status()["total"], 1)

    def test_slow_hash_runs_off_the_capture_thread(self):
        entered = threading.Event();release = threading.Event()
        original = inspect_replay_file
        def slow(*args, **kwargs):
            entered.set();release.wait(timeout=3);return original(*args, **kwargs)
        service = ReplayCaptureService(self.config, self.observer, self.outbox, clock=lambda: self.clock)
        self.addCleanup(service.close);self.addCleanup(release.set)
        self.addCleanup(lambda: (release.set(), service._executor.shutdown(wait=True, cancel_futures=True)))
        self.observer.observation = {"process_key": "p", "baseline_ready": True,
                                     "file_events": [self.event(at=self.clock)]}
        with patch("companion.replay_capture.inspect_replay_file", side_effect=slow):
            started = time.monotonic();service.tick()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(entered.wait(timeout=1))
            self.observer.observation = {"process_key": "p", "report": native_report()}
            started = time.monotonic();service.tick()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(self.outbox.status()["total"], 0)
            release.set()


if __name__ == "__main__":unittest.main()
