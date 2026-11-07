"""File-independent live debrief capture and automatic routing invariants."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.live_capture import AutomaticCaptureService, LiveCaptureService
from companion.outbox import Outbox
from companion.receiver import validate_payload
from test_companion_replay_capture import ImmediateExecutor, Observer, native_report, replay_bytes


def session(*, sid="session-one", ended=False, replay=False, epoch=None, player_id="123", map_id="map-one"):
    return {"session_id": sid, "map": map_id, "game_build": 168061, "server_name": "Test Room",
            "participants": [{"id": player_id, "name": "Player", "alliance": 0}],
            "is_replay": replay, "source_kind": "replay" if replay else "network_live",
            "game_ended": ended, "context_epoch": epoch or hashlib.sha256(sid.encode()).hexdigest()}


class LiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = SimpleNamespace(data_directory=self.base / "data", report_timeout_seconds=1000)
        self.observer = Observer();self.clock = 10.0;self.events = []
        self.outbox = Outbox(self.base / "outbox.sqlite3", clock=lambda: self.clock)
        self.service = LiveCaptureService(self.config, self.observer, self.outbox,
                                          emit=self.events.append, clock=lambda: self.clock)
        self.addCleanup(self.service.close)
        self.tick(native_session=session(), baseline_ready=True)

    def tick(self, **observation):
        self.clock += 1
        self.observer.observation = {"process_key": "p", "report_context_stable": True, **observation}
        return self.service.tick()

    def exports(self):return list((self.config.data_directory / "exports" / "live").glob("*/*.json"))
    def unbound(self):return list((self.config.data_directory / "unbound-live").glob("*.json"))
    def payloads(self):return [job["payload"] for job in self.outbox.due(now=1e12)]

    def test_live_report_uses_native_session_without_files_sha_room_or_tcp(self):
        row = {"row_index": 0, "game_time_seconds": 1.0,
               "killer": {"player_name": "Player", "unit_name": "Tank"},
               "victim": {"player_name": "Other", "unit_name": "AT"}}
        rows = [row, {**row, "row_index": 1}]
        with patch("companion.replays.parse_header", side_effect=AssertionError("live must not use replay headers")), \
                patch("companion.replay_capture.inspect_replay_file", side_effect=AssertionError("live must not hash files")), \
                patch("companion.windows.tcp_connections", side_effect=AssertionError("native SID is independent of TCP")):
            self.tick(native_session=session(ended=True), report=native_report(rows=rows))
        payload = self.payloads()[0]
        validate_payload(payload)
        self.assertEqual(payload["schema"], "sd2-task-report-v4")
        self.assertEqual(payload["binding_mode"], "native_session")
        self.assertEqual(payload["session_id"], "session-one")
        self.assertEqual(payload["rows"], native_report(rows=rows)["rows"])
        self.assertFalse({"room_hint", "client_replay_sha256", "client_header_sha256", "result_address", "variant_address"} & payload.keys())
        self.assertEqual(payload["client_context_evidence"]["session_origin"], "native_game_setup")
        self.assertTrue(payload["client_context_evidence"]["observed_running"])
        self.assertEqual(json.loads(self.exports()[0].read_text(encoding="utf-8")), payload)
        self.assertEqual(len(self.unbound()), 1)
        local = json.loads(self.unbound()[0].read_text(encoding="utf-8"))
        self.assertEqual(local["schema"], "sd2-local-unbound-task-report-v4")
        self.assertEqual(local["report"]["rows"], payload["rows"])
        self.assertEqual(local["report"]["native_players"], payload["native_players"])
        self.assertEqual(local["source_kind"], "network_live")
        self.assertFalse({"variant_address", "result_address", "generation", "metadata"} & local["report"].keys())
        self.assertFalse({"match_binding", "report_scope"} & payload.keys())
        self.assertEqual(self.outbox.status()["pending"], 1)

    def test_valid_native_empty_timeline_is_distinct_from_missing_report(self):
        self.tick(native_session=session(ended=True))
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=session(ended=True), report=native_report(rows=[]))
        self.assertEqual(self.payloads()[0]["rows"], [])

    def test_existing_baseline_report_never_becomes_new_endgame_report(self):
        self.service = LiveCaptureService(self.config, self.observer, self.outbox, clock=lambda: self.clock)
        old = native_report()
        self.tick(native_session=session(), baseline_ready=True, baseline_report=old)
        self.tick(native_session=session(ended=True), report=old)
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=session(ended=True), report=native_report(2))
        self.assertEqual(self.payloads()[0]["report_generation"], 2)

    def test_first_report_with_unknown_baseline_is_not_later_reused(self):
        self.tick(process_key="fresh", native_session=session(), report=native_report())
        self.tick(process_key="fresh", native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_starting_only_after_game_end_does_not_claim_observed_running(self):
        self.tick(process_key="late", native_session=session(ended=True), baseline_ready=True, report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(self.unbound()), 1)

    def test_report_first_seen_before_end_cannot_acquire_future_end_evidence(self):
        self.tick(native_session=session(), report=native_report())
        self.tick(native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(self.unbound()), 1)
        self.tick(native_session=session(ended=True), report=native_report(2))
        self.assertEqual(self.payloads()[0]["report_generation"], 2)

    def test_unknown_source_report_stays_unbound_after_source_becomes_known(self):
        self.tick(native_session=None, report=native_report(), report_context_stable=False)
        self.tick(native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(self.unbound()), 1)

    def test_unbound_snapshot_keeps_the_session_map_server_and_id(self):
        # Without this the results list shows a blank map for a local-only match.
        self.tick(process_key="late-known", baseline_ready=True, native_session=session(ended=True))
        self.tick(process_key="late-known", native_session=session(ended=True), report=native_report())
        snapshot = json.loads(self.unbound()[0].read_text(encoding="utf-8"))
        self.assertEqual(snapshot["map"], "map-one")
        self.assertEqual(snapshot["server_name"], "Test Room")
        self.assertEqual(snapshot["session_id"], "session-one")
        self.assertEqual(snapshot["status"], "local_only_not_session_verified")
        self.assertEqual(snapshot["source_kind"], "network_live")

    def test_unknown_report_metadata_never_borrows_an_earlier_or_future_session(self):
        self.tick(native_session=None, report=native_report(), report_context_stable=False)
        self.tick(native_session=session(sid="another-session", map_id="different-map", ended=True),
                  report=native_report())
        snapshot = json.loads(self.unbound()[0].read_text(encoding="utf-8"))
        self.assertFalse({"session_id", "map", "source_kind", "session_origin"} & snapshot.keys())

    def test_replay_source_never_uses_live_session_even_with_matching_roster(self):
        self.tick(native_session=session(ended=True, replay=True), report=native_report())
        self.tick(native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_same_roster_new_session_requires_new_running_and_new_report(self):
        self.tick(native_session=session(sid="next-session", ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=session(sid="next-session"))
        self.tick(native_session=session(sid="next-session", ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=session(sid="next-session", ended=True), report=native_report(2))
        self.assertEqual(self.payloads()[0]["session_id"], "next-session")

    def test_mid_read_context_switch_wrong_roster_and_wrong_build_stay_local(self):
        cases = [(native_report(), {"report_context_stable": False}),
                 (native_report(player_id="456"), {}),
                 ({**native_report(), "game_build": 1}, {})]
        for index, (report, extra) in enumerate(cases):
            with self.subTest(index=index):
                self.tick(process_key=f"case-{index}", native_session=session(), baseline_ready=True)
                self.tick(process_key=f"case-{index}", native_session=session(ended=True), report=report, **extra)
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(self.unbound()), 3)

    def test_disconnect_does_not_prevent_same_native_ended_world_capture(self):
        self.tick(native_session=session(ended=True), report=native_report(), room_id=None, connection_observed=True)
        self.assertEqual(self.payloads()[0]["session_id"], "session-one")

    def test_same_report_and_consumed_epoch_are_not_submitted_twice(self):
        self.tick(native_session=session(ended=True), report=native_report())
        self.tick(native_session=session(ended=True), report=native_report())
        self.tick(native_session=session(ended=True), report=native_report(2))
        self.assertEqual(self.outbox.status()["total"], 1)
        self.assertEqual(len(self.exports()), 1)

    def test_ended_world_reverting_to_running_cannot_rearm_same_epoch(self):
        self.tick(native_session=session(ended=True))
        self.tick(native_session=session())
        self.tick(native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)

    def test_same_tcp_and_roster_across_two_native_sessions_produce_distinct_ids(self):
        self.tick(native_session=session(ended=True), report=native_report(), room_id="same-room")
        self.tick(native_session=session(sid="next-session"), room_id="same-room")
        self.tick(native_session=session(sid="next-session", ended=True), report=native_report(2), room_id="same-room")
        payloads = self.payloads()
        self.assertEqual({p["session_id"] for p in payloads}, {"session-one", "next-session"})
        self.assertEqual(len({p["submission_id"] for p in payloads}), 2)

    def test_frozen_payload_survives_queue_failure_and_context_clear(self):
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tick(native_session=session(ended=True), report=native_report())
        self.assertEqual(self.outbox.status()["total"], 0)
        self.tick(native_session=session(sid="unrelated"), report_identity_clear=True)
        self.assertEqual(self.payloads()[0]["session_id"], "session-one")
        self.assertEqual(self.payloads()[0]["report_generation"], 1)
        local = json.loads(self.unbound()[0].read_text(encoding="utf-8"))
        self.assertEqual(local["session_id"], "session-one")
        self.assertEqual(local["source_kind"], "network_live")

    def test_unbound_write_failure_survives_process_exit_without_losing_payload(self):
        with patch("companion.live_capture.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tick(native_session=session(ended=True), report=native_report())
        self.tick(process_key=None, native_session=None)
        self.assertEqual(self.payloads()[0]["session_id"], "session-one")
        self.assertEqual(len(self.unbound()), 1)


class AutomaticCaptureRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = SimpleNamespace(data_directory=self.base / "data", report_timeout_seconds=1000)
        self.observer = Observer();self.clock = 10.0;self.events = []
        self.outbox = Outbox(self.base / "outbox.sqlite3", clock=lambda: self.clock)
        self.calls = 0
        original = self.observer.poll
        def counted(now):
            self.calls += 1
            return original(now)
        self.observer.poll = counted
        self.service = AutomaticCaptureService(self.config, self.observer, self.outbox,
                                               emit=self.events.append, clock=lambda: self.clock)
        self.addCleanup(self.service.close)
        self.service.replay._executor.shutdown(wait=False, cancel_futures=True)
        self.service.replay._executor = ImmediateExecutor()
        self.service.replay._owns_executor = False
        self.path = self.base / "match.rpl3";self.path.write_bytes(replay_bytes())
        self.tick(native_session=session(), baseline_ready=True)

    def tick(self, **observation):
        self.clock += 1
        self.observer.observation = {"process_key": "p", "report_context_stable": True, **observation}
        return self.service.tick()

    def file_event(self):
        return {"process_key": "p", "path": str(self.path), "byte_offset": 0,
                "read_size": self.path.stat().st_size, "operation": "read",
                "observed_at": self.clock + 1, "sequence": 1}

    def test_physical_observer_is_polled_once_per_tick(self):
        initial = self.calls
        self.tick(native_session=session())
        self.assertEqual(self.calls, initial + 1)

    def test_file_monitor_permission_failure_does_not_block_live(self):
        self.tick(native_session=session(ended=True), report=native_report(), access_uncertain=True,
                  file_monitor_error="file_monitor_requires_elevation")
        payload = self.outbox.due(now=1e12)[0]["payload"]
        self.assertEqual(payload["schema"], "sd2-task-report-v4")
        self.assertEqual(payload["binding_mode"], "native_session")
        self.assertFalse(list((self.config.data_directory / "exports").glob("*/*.json")))

    def test_live_report_with_body_file_events_still_routes_only_live(self):
        self.tick(native_session=session(), file_events=[self.file_event()])
        self.tick(native_session=session(ended=True), report=native_report())
        for _ in range(3):self.tick(native_session=session(ended=True))
        payloads = [job["payload"] for job in self.outbox.due(now=1e12)]
        self.assertEqual([p["binding_mode"] for p in payloads], ["native_session"])

    def test_known_replay_routes_only_to_full_sha_policy(self):
        self.tick(native_session=session(replay=True), file_events=[self.file_event()])
        self.tick(native_session=session(replay=True, ended=True), report=native_report())
        for _ in range(3):self.tick(native_session=session(replay=True, ended=True))
        payloads = [job["payload"] for job in self.outbox.due(now=1e12)]
        self.assertEqual([p["binding_mode"] for p in payloads], ["replay_sha256"])
        self.assertEqual(payloads[0]["client_replay_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertFalse(list((self.config.data_directory / "exports" / "live").glob("*/*.json")))

    def test_unknown_source_does_not_fall_back_to_replay_even_with_matching_file(self):
        self.tick(native_session=None, file_events=[self.file_event()])
        self.tick(native_session=None, report=native_report(), report_context_stable=False)
        for _ in range(3):self.tick(native_session=session(replay=True, ended=True))
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(list((self.config.data_directory / "unbound-live").glob("*.json"))), 1)

    def test_unstable_after_replay_source_cannot_bind_report_to_replay_sha(self):
        self.tick(native_session=session(), file_events=[self.file_event()])
        self.tick(native_session=session(replay=True, ended=True), report=native_report(), report_context_stable=False)
        for _ in range(3):self.tick(native_session=session(replay=True, ended=True))
        self.assertEqual(self.outbox.status()["total"], 0)
        self.assertEqual(len(list((self.config.data_directory / "unbound-live").glob("*.json"))), 1)

    def test_replay_after_live_without_file_io_uses_its_own_native_playback_evidence(self):
        recorded = self.file_event();recorded["operation"] = "write"
        self.tick(native_session=session(), file_events=[recorded])
        self.tick(native_session=session(ended=True), report=native_report())
        self.tick(native_session=session(replay=True))
        self.tick(native_session=session(replay=True, ended=True), report=native_report(2))
        for _ in range(3):self.tick(native_session=session(replay=True, ended=True))
        payloads = [job["payload"] for job in self.outbox.due(now=1e12)]
        self.assertEqual([payload["binding_mode"] for payload in payloads], ["native_session", "native_session"])
        self.assertEqual({payload["client_context_evidence"]["association"] for payload in payloads},
                         {"native_game_session", "native_replay_session"})
        self.assertFalse(any("client_replay_sha256" in payload for payload in payloads))

    def test_new_replay_file_after_live_round_is_not_ambiguous_with_live_recording(self):
        recorded = self.file_event();recorded["operation"] = "write"
        self.tick(native_session=session(), file_events=[recorded])
        self.tick(native_session=session(ended=True), report=native_report())
        other = self.base / "actually-played.rpl3";other.write_bytes(replay_bytes(marker=b"different-original-bytes"))
        event = {**self.file_event(), "path": str(other), "read_size": other.stat().st_size, "sequence": 2}
        self.tick(native_session=session(replay=True), file_events=[event])
        self.tick(native_session=session(replay=True, ended=True), report=native_report(2))
        for _ in range(3):self.tick(native_session=session(replay=True, ended=True))
        replay_payloads = [job["payload"] for job in self.outbox.due(now=1e12)
                           if job["payload"]["binding_mode"] == "replay_sha256"]
        self.assertEqual(len(replay_payloads), 1)
        self.assertEqual(replay_payloads[0]["client_replay_sha256"], hashlib.sha256(other.read_bytes()).hexdigest())


if __name__ == "__main__":unittest.main()
