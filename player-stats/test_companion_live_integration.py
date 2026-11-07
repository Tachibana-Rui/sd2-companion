"""Cloud live match -> native bytes -> automatic capture -> server association.

Only Windows process discovery/access and the optional file monitor are test
doubles. Session/report decoders, lifecycle policy, local persistence, HTTP
transport and server archive matching are production components. No replay
file, game process, ETW session, UAC prompt or external network is used.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.file_access import FileMonitorError
from companion.live_capture import AutomaticCaptureService
from companion.live_runtime import AutomaticWindowsObserver
from companion.native_session import REPLAY_SOURCE_VTABLE_RVA
from companion.outbox import Outbox
from companion.receiver import API_PATH, ArchiveIndex, ReceiverStore, create_server
from companion.upload import Uploader
from companion.windows import ProcessInfo
from test_companion_native_session import SessionFixture
from test_companion_registry import RegistryFixture


class DeniedFileMonitor:
    """A cloud-only player need not grant the optional local-file monitor."""
    status = 'file_monitor_not_started'

    def __init__(self):
        self.start_count = self.poll_count = self.close_count = 0
        self.selected = None

    def start(self):
        self.start_count += 1
        self.status = 'file_monitor_requires_elevation'
        raise FileMonitorError(self.status)

    def set_process(self, pid, key):self.selected = (pid, key)

    def poll(self):
        self.poll_count += 1
        raise AssertionError('live cloud capture attempted to use file events')

    def close(self):self.close_count += 1


class NativeLiveIntegrationTests(unittest.TestCase):
    SESSION = 'native:live-cloud-session-A'
    MAP = 'map-live-integration'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sd2-live-integration-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.native = RegistryFixture()
        self.expected = self.native.reader().poll()
        self.assertIsNotNone(self.expected)
        # Empty native result baseline while the actual network round runs.
        self.native.memory.write(self.native.variant, struct.pack('<QI4xQ', 7, 0, 0))
        players = [(index, [('PlayerUserId', p['player_user_id']), ('PlayerName', p['name']),
                            ('PlayerAlliance', str(p['alliance'])), ('PlayerObserver', '0')])
                   for index, p in enumerate(self.expected.players)]
        # Reuse the byte memory allocator so narrow GameSetup strings and wide
        # report strings occupy distinct valid addresses in one process image.
        with patch('test_companion_native_session.ByteMemory', return_value=self.native.memory):
            self.session = SessionFixture(common=[('UniqueSessionId', self.SESSION), ('Map', self.MAP),
                                                  ('ServerName', 'cloud native server')], players=players)
        self.native.memory.close = lambda: None
        self.clock = 100.0
        game = ProcessInfo(101, 10000, 'unused-fixture/SteelDivision2.exe',
                           self.native.memory.exe_sha256, self.native.memory.image_base)
        for target, options in (
            ('companion.windows.list_game_processes', {'return_value': [game]}),
            ('companion.windows.WindowsMemory', {'return_value': self.native.memory}),
            ('companion.windows.tcp_connections', {'side_effect': AssertionError('live SID is not a TCP heuristic')}),
        ):
            patcher = patch(target, **options)
            patcher.start();self.addCleanup(patcher.stop)
        self.config = SimpleNamespace(data_directory=self.base / 'data', report_timeout_seconds=1000,
                                      process_poll_seconds=1, game_executable=None, max_replay_files=2000,
                                      rooms=(), replay_directories=(), collection_mode='auto')
        self.monitor = DeniedFileMonitor()
        self.observer = AutomaticWindowsObserver(self.config, monitor=self.monitor)
        self.box = Outbox(self.base / 'outbox.sqlite3', clock=lambda: self.clock)
        self.archive = self.base / 'server-analysis.sqlite3'
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute('CREATE TABLE reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT)')
            db.execute('CREATE TABLE report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)')
        self.store = ReceiverStore(self.base / 'inbox.sqlite3', archive_index=ArchiveIndex(self.archive))
        self.server = create_server(self.store, 'live-integration-fixture-token')
        self.http_thread = threading.Thread(target=self.server.serve_forever,
                                            kwargs={'poll_interval': 0.02}, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.stop_server)
        self.uploader = Uploader(f'http://127.0.0.1:{self.server.server_port}' + API_PATH,
                                 'live-integration-fixture-token', allow_loopback_http=True)
        self.events = []
        self.service = AutomaticCaptureService(self.config, self.observer, self.box, uploader=self.uploader,
                                               emit=self.events.append, clock=lambda: self.clock)
        self.addCleanup(self.service.close)
        observation = self.tick()
        self.assertTrue(observation['baseline_ready'])
        self.assertTrue(observation['report_context_stable'])
        self.assertEqual(observation['native_session']['source_kind'], 'network_live')
        self.assertFalse(observation['native_session']['game_ended'])
        self.assertEqual(observation['file_monitor_error'], 'file_monitor_requires_elevation')
        self.assertEqual(self.box.status()['total'], 0)

    def stop_server(self):
        self.server.shutdown();self.server.server_close()
        self.http_thread.join(timeout=2)

    def tick(self):
        self.clock += 0.25
        return self.service.tick()

    def publish_finished_report(self):
        self.native.memory.write(self.session.context + 0x59, b'\x01')
        self.native.memory.write(self.native.variant, struct.pack('<QI4xQ', 8, 1, self.native.result))
        observation = self.tick()
        self.assertTrue(observation['report_context_stable'])
        self.assertTrue(observation['native_session']['game_ended'])
        self.assertEqual(observation['report'].rows, self.expected.rows)
        self.assertEqual(self.box.status()['total'], 1)
        payload = self.box.due(now=self.clock + 1)[0]['payload']
        self.assertEqual(payload['schema'], 'sd2-task-report-v4')
        self.assertEqual(payload['session_id'], self.SESSION)
        self.assertEqual(payload['native_players'], self.expected.players)
        self.assertEqual(payload['rows'], self.expected.rows)
        self.assertFalse({'room_hint', 'client_header_sha256', 'client_replay_sha256'} & payload.keys())
        exported = list((self.config.data_directory / 'exports' / 'live').glob('*/*.json'))
        self.assertEqual(len(exported), 1)
        self.assertEqual(json.loads(exported[0].read_text(encoding='utf-8')), payload)
        # Deliberately never create a local replay or replay-header input.
        self.assertEqual(list(self.base.rglob('*.rpl3')), [])
        self.assertEqual(list(self.base.rglob('*.rpl')), [])
        self.assertEqual(self.monitor.start_count, 1)
        self.assertEqual(self.monitor.poll_count, 0)
        return payload

    def archive_report(self, *, session_id=SESSION, sha='c' * 64, finished=True):
        document = {'schema': 1, 'source': {'origin': 'server_collection', 'sha256': sha},
                    'match': {'session_id': session_id, 'map': self.MAP, 'version': 168061,
                              'completion': {'status': 'finished' if finished else 'unverified'}},
                    'players': [{'id': p['player_user_id'], 'alliance': p['alliance'], 'is_observer': False}
                                for p in self.expected.players]}
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute('INSERT INTO reports VALUES(?,?,?,?)',
                       (sha, 'server:managed-room:' + session_id, json.dumps(document),
                        'finished' if finished else 'unverified'))
            db.execute('INSERT INTO report_sources VALUES(?,?,?)', (sha, 'managed-room', 'server archive'))
        return sha

    def saved(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM submissions').fetchone())

    def test_cloud_live_without_local_file_or_monitor_permission_matches_server_session(self):
        sha = self.archive_report()
        archive_before = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        payload = self.publish_finished_report()
        self.assertEqual(self.service.flush(), 1)
        self.assertEqual(self.box.status()['sent'], 1)
        stored = self.saved()
        self.assertEqual(stored['status'], 'accepted')
        self.assertEqual(stored['server_sha256'], sha)
        self.assertEqual(stored['room_hint'], 'managed-room')
        self.assertEqual(stored['binding_evidence'], 'server_finished_archive')
        self.assertFalse(stored['server_state_verified'])
        self.assertEqual(json.loads(stored['payload']), payload)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), archive_before)
        self.tick()
        self.assertEqual(self.box.status()['total'], 1)
        self.assertEqual(self.uploader.send(payload)['status'], 'duplicate')
        self.assertEqual(self.store.status()['submissions']['total'], 1)

    def test_forgotten_live_capture_is_recovered_by_full_replay_without_file_monitor(self):
        # The real match ended before this companion saw it. Playing its replay
        # supplies a different, verified native source and a fresh lifecycle.
        sha = self.archive_report()
        archive_before = self.archive.read_bytes()
        self.native.memory.u64(self.session.source,
                               self.native.memory.image_base + REPLAY_SOURCE_VTABLE_RVA)
        self.native.memory.write(self.session.manager + 0x75, b'\x01')
        playback = self.tick()
        self.assertEqual(playback['native_session']['source_kind'], 'replay')
        self.assertFalse(playback['native_session']['game_ended'])
        self.assertEqual(self.box.status()['total'], 0)
        self.native.memory.write(self.session.context + 0x59, b'\x01')
        self.native.memory.write(self.native.variant, struct.pack('<QI4xQ', 8, 1, self.native.result))
        finished = self.tick()
        self.assertTrue(finished['report_context_stable'])
        payload = self.box.due(now=self.clock + 1)[0]['payload']
        self.assertEqual(payload['client_context_evidence']['association'], 'native_replay_session')
        self.assertEqual(payload['client_context_evidence']['native_source_kind'], 'replay')
        self.assertEqual(payload['rows'], self.expected.rows)
        self.assertNotIn('client_replay_sha256', payload)
        self.assertNotIn('file_body_io_observed', payload['client_context_evidence'])
        local = list((self.config.data_directory / 'unbound-replays').glob('*.json'))
        self.assertEqual(len(local), 1)
        self.assertEqual(json.loads(local[0].read_text())['source_kind'], 'replay')
        self.assertFalse(list((self.config.data_directory / 'unbound-live').glob('*.json')))
        self.assertEqual(self.service.flush(), 1)
        stored = self.saved()
        self.assertEqual(stored['status'], 'accepted')
        self.assertEqual(stored['server_sha256'], sha)
        self.assertFalse(stored['server_state_verified'])
        self.assertEqual(self.archive.read_bytes(), archive_before)
        self.assertEqual(self.monitor.poll_count, 0)
        self.assertFalse(list(self.base.rglob('*.rpl3')))
        self.tick()
        self.assertEqual(self.box.status()['total'], 1)

    def test_same_roster_of_another_session_never_substitutes_for_exact_native_sid(self):
        other_sha = self.archive_report(session_id='native:live-cloud-session-B', sha='b' * 64)
        payload = self.publish_finished_report()
        self.service.flush()
        stored = self.saved()
        self.assertEqual(stored['status'], 'pending_match')
        self.assertIsNone(stored['server_sha256'])
        actual_sha = self.archive_report()
        self.assertEqual(self.store.reconcile(), {'checked': 1, 'accepted': 1})
        stored = self.saved()
        self.assertEqual(stored['server_sha256'], actual_sha)
        self.assertNotEqual(stored['server_sha256'], other_sha)
        self.assertEqual(json.loads(stored['payload']), payload)

    def test_server_running_archive_stays_pending_until_finished_without_client_changes(self):
        sha = self.archive_report(finished=False)
        payload = self.publish_finished_report()
        self.service.flush()
        self.assertEqual(self.saved()['status'], 'pending_match')
        self.assertEqual(self.store.reconcile(), {'checked': 1, 'accepted': 0})
        with closing(sqlite3.connect(self.archive)) as db, db:
            document = json.loads(db.execute('SELECT payload FROM reports WHERE sha256=?', (sha,)).fetchone()[0])
            document['match']['completion']['status'] = 'finished'
            db.execute("UPDATE reports SET completion_status='finished',payload=? WHERE sha256=?",
                       (json.dumps(document), sha))
        self.assertEqual(self.store.reconcile(), {'checked': 1, 'accepted': 1})
        self.assertEqual(self.saved()['server_sha256'], sha)
        self.assertEqual(json.loads(self.saved()['payload']), payload)


if __name__ == '__main__':
    unittest.main()
