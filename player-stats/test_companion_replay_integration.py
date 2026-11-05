"""Native byte memory -> automatic replay observer -> hash -> HTTP -> archive.

All processes/file-I/O notifications are test doubles. The native reader,
background file hashing, export/outbox persistence, HTTP transport, receiver,
and read-only archive association are production modules. No SD2, ETW session,
administrator prompt, or external network endpoint is used.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.file_access import FileReadEvent
from companion.outbox import Outbox
from companion.receiver import API_PATH, ArchiveIndex, ReceiverStore, create_server
from companion.replay_capture import ReplayCaptureService
from companion.replay_runtime import ReplayWindowsObserver
from companion.upload import Uploader
from companion.windows import ProcessInfo
from test_companion_native_report import ReportFixture


class FileMonitorFixture:
    def __init__(self):
        self.status = "file_monitor_not_started"
        self.events = []
        self.selected = None
        self.closed = False

    def start(self):
        self.status = "file_monitor_running"

    def set_process(self, pid, key):
        self.selected = pid, key

    def poll(self):
        result, self.events = self.events, []
        return result

    def close(self):
        self.closed = True
        self.status = "file_monitor_closed"


class AutomaticReplayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-replay-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "任意目录" / "game.rpl3"
        self.path.parent.mkdir()
        self.native = ReportFixture()
        self.native.controller()
        self.expected = self.native.reader().poll()
        self.assertIsNotNone(self.expected)
        # The game first has no published report. Later writes publish the
        # real byte-addressable native fixture, not pre-exported event JSON.
        self.native.memory.write(self.native.variant, struct.pack("<QI4xQ", 7, 0, 0))
        self.native.memory.close = lambda: None
        self.clock = 100.0
        self.sequence = 0
        self.key = "101:10000"
        self.game = ProcessInfo(101, 10000, "unused-fixture/SteelDivision2.exe",
                                self.native.memory.exe_sha256, self.native.memory.image_base)
        for target, options in (
            ("companion.windows.list_game_processes", {"return_value": [self.game]}),
            ("companion.windows.WindowsMemory", {"return_value": self.native.memory}),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.monitor = FileMonitorFixture()
        # There are deliberately no room IDs, TCP endpoints or replay folder
        # configuration: association derives from the full observed file hash.
        self.config = SimpleNamespace(data_directory=self.base / "data", report_timeout_seconds=1000,
                                      process_poll_seconds=1, game_executable=None)
        self.observer = ReplayWindowsObserver(self.config, monitor=self.monitor)
        self.box = Outbox(self.base / "outbox.sqlite3", clock=lambda: self.clock)
        self.archive = self.base / "analyzed.sqlite3"
        with closing(sqlite3.connect(self.archive)) as db:
            db.execute("CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)")
            db.execute("CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
            db.commit()
        self.inbox = self.base / "inbox.sqlite3"
        self.store = ReceiverStore(self.inbox, archive_index=ArchiveIndex(self.archive))
        self.server = create_server(self.store, "integration-fixture-token")
        self.http_thread = threading.Thread(target=self.server.serve_forever,
                                            kwargs={"poll_interval": 0.02}, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.stop_server)
        uploader = Uploader(f"http://127.0.0.1:{self.server.server_port}" + API_PATH,
                            "integration-fixture-token", allow_loopback_http=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="replay-integration-hash")
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)
        self.events = []
        self.service = ReplayCaptureService(self.config, self.observer, self.box, uploader=uploader,
                                            emit=self.events.append, clock=lambda: self.clock,
                                            executor=self.executor)
        self.addCleanup(self.service.close)
        self.tick()
        self.assertEqual(self.monitor.selected, (101, self.key))
        self.assertTrue(self.observer.baseline_ready)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join(timeout=2)

    def tick(self):
        self.clock += 0.25
        return self.service.tick()

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.tick()
            if condition():
                return
            time.sleep(0.005)
        self.fail("automatic replay integration timed out: " + self.service.status)

    def replay_bytes(self, marker=b"command-body-" * 256):
        raw = {"game": {"UniqueSessionId": "native-integration-session", "Version": "168061",
                        "Map": "native-integration-map", "ServerName": "original room"}}
        for index, player in enumerate(self.expected.players):
            raw[f"player_{index}"] = {"PlayerUserId": player["player_user_id"],
                                      "PlayerName": player["name"], "PlayerAlliance": str(player["alliance"])}
        header = json.dumps(raw, ensure_ascii=False).encode("utf-8")
        prefix = struct.pack(">4sQI", b"rply", len(header), 0) + header
        body = struct.pack(">4sQI", b"strm", len(marker), 0) + marker
        self.stream_offset = 16 + len(prefix)
        return struct.pack(">4sIII", b"ESAV", 3, 16 + len(prefix) + len(body), 0) + prefix + body

    def analyze(self, data):
        sha = hashlib.sha256(data).hexdigest()
        report = {"schema": 1, "source": {"origin": "local_upload", "sha256": sha},
                  "match": {"session_id": "native-integration-session", "version": 168061,
                            "map": "native-integration-map", "completion": {"status": "finished"}},
                  "players": [{"id": p["player_user_id"], "alliance": p["alliance"], "is_observer": False}
                              for p in self.expected.players]}
        with closing(sqlite3.connect(self.archive)) as db:
            db.execute("INSERT INTO reports VALUES(?,?,?,?)",
                       (sha, report["match"]["session_id"], json.dumps(report), "finished"))
            db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, "derived-archive-room", "archive room"))
            db.commit()
        return sha

    def io(self, operation="read", *, offset=None, size=None):
        self.sequence += 1
        offset = self.stream_offset if offset is None else offset
        size = self.path.stat().st_size - offset if size is None else size
        self.monitor.events.append(FileReadEvent(self.key, str(self.path), self.clock + 0.25,
                                                 self.sequence, offset, size, operation))

    def publish(self, generation=8):
        self.native.memory.write(self.native.variant, struct.pack("<QI4xQ", generation, 1, self.native.result))

    def assert_export_and_delivery(self, sha, operation):
        self.wait_for(lambda: self.box.status()["pending"] == 1)
        queued = self.box.due()[0]["payload"]
        self.assertNotIn("room_hint", queued)
        self.assertEqual(queued["schema"], "sd2-task-report-v4")
        self.assertEqual(queued["client_replay_sha256"], sha)
        self.assertEqual(queued["rows"], self.expected.rows)
        self.assertEqual(queued["native_players"], self.expected.players)
        self.assertEqual(queued["client_context_evidence"]["file_operation"], operation)
        self.assertEqual(queued["source"], "player_client_official_task_report")
        self.assertNotIn("client_header_sha256", queued)
        exports = list((self.config.data_directory / "exports" / sha).glob("*.json"))
        self.assertEqual(len(exports), 1)
        self.assertEqual(json.loads(exports[0].read_text(encoding="utf-8")), queued)
        serialized = json.dumps(queued)
        for local in (str(self.path), 'variant_address', 'result_address', 'process_key'):
            self.assertNotIn(local, serialized)
        archive_fingerprint = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.assertEqual(self.service.flush(), 1)
        self.assertEqual(self.box.status()["sent"], 1)
        self.assertEqual(self.store.status()["submissions"]["accepted"], 1)
        with closing(sqlite3.connect(self.inbox)) as db:
            row = db.execute("SELECT server_sha256,room_hint,archive_rooms,payload FROM submissions").fetchone()
        self.assertEqual(row[0], sha)
        self.assertEqual(row[1], "derived-archive-room")
        self.assertEqual(json.loads(row[2]), ["derived-archive-room"])
        self.assertEqual(json.loads(row[3]), queued)
        self.assertEqual(archive_fingerprint, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        for _ in range(3):self.tick()
        self.assertEqual(self.box.status()["total"], 1)
        self.assertEqual(len(list((self.config.data_directory / "exports").glob("*/*.json"))), 1)
        self.assertEqual(self.service.uploader.send(queued)["status"], "duplicate")
        self.assertEqual(self.store.status()["submissions"]["total"], 1)

    def test_native_report_from_observed_read_matches_full_analyzed_sha_without_room(self):
        data = self.replay_bytes()
        self.path.write_bytes(data)
        sha = self.analyze(data)
        self.io();self.tick()
        self.publish();self.tick()
        self.assert_export_and_delivery(sha, "read")

    def test_cloud_local_save_write_only_waits_for_complete_file_and_matches_archive(self):
        data = self.replay_bytes()
        sha = self.analyze(data)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(data[:self.stream_offset + 8])
        self.io("write", offset=0, size=self.path.stat().st_size);self.tick()
        self.wait_for(lambda: self.service.status == "incomplete_replay_file")
        self.assertEqual(self.box.status()["total"], 0)
        self.path.write_bytes(data)
        self.io("write", offset=0, size=len(data));self.tick()
        self.publish();self.tick()
        self.assert_export_and_delivery(sha, "write")

    def test_changed_file_cannot_bind_old_report_even_after_later_read_rehashes_new_bytes(self):
        data = self.replay_bytes()
        self.path.write_bytes(data)
        self.analyze(data)
        self.io();self.tick()
        self.wait_for(lambda: any(c.snapshot is not None for s in self.service.processes.values()
                                 for c in s.candidates.values()))
        stamp = self.path.stat().st_mtime_ns
        changed = data[:-1] + bytes([data[-1] ^ 0x01])
        self.path.write_bytes(changed)
        os.utime(self.path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
        new_sha = self.analyze(changed)  # Both versions exist: equality must use the read version.
        self.publish();self.tick()
        self.assertEqual(self.box.status()["total"], 0)
        self.io();self.tick()  # Access after the report cannot repair its old byte identity.
        self.wait_for(lambda: all(c.future is None for state in self.service.processes.values()
                                 for c in state.candidates.values()))
        self.assertEqual(self.box.status()["total"], 0)
        self.assertEqual(list((self.config.data_directory / "exports").glob("*/*.json")), [])
        self.assertEqual(len(list((self.config.data_directory / "unbound-replays").glob("*.json"))), 1)
        # A genuinely subsequent report can use the subsequent file access.
        self.publish(generation=9);self.tick()
        self.assert_export_and_delivery(new_sha, "read")


if __name__ == "__main__":
    unittest.main()
