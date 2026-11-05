"""Real native byte parsers -> v3 HTTP -> official catalog -> derived economy.

Only OS process discovery/access and optional file monitoring are replaced.
Native data is a shared byte address space, not a mocked exported JSON report.
The HTTP endpoint is loopback only; no game, ETW session or replay file exists.
"""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.aggregation import CombatStore
from companion.live_capture import AutomaticCaptureService
from companion.live_runtime import AutomaticWindowsObserver
from companion.outbox import Outbox
from companion.pricing import Catalog
from companion.receiver import API_PATH, COMBAT_PATH, ArchiveIndex, ReceiverStore, create_server
from companion.upload import Uploader
from companion.windows import ProcessInfo
from test_companion_combat import save_archive
from test_companion_live_integration import DeniedFileMonitor
from test_companion_native_stats import StatsFixture
from test_companion_registry import RegistryFixture


LORRAINE = 'c6731ffa05d64c55bc7eb4307e77764e'
PE2_110 = 'ca4872c0725b44c590749a95869ab99a'
SERVER_SHA = 'c' * 64


class NativeStatsEndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sd2-native-stats-v3-e2e-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.native = StatsFixture()
        self.memory = m = self.native.memory
        # Build the proven manager/lazy-cache ABI into the same address space.
        # This is fixture allocation composition, not a production-reader mock.
        with patch('test_companion_native_report.ByteMemory', return_value=m):
            self.report = RegistryFixture()
        self.catalog = Catalog()
        self.assertEqual(self.catalog.unit(LORRAINE, 168061)['cost'], 130)
        self.assertEqual(self.catalog.unit(PE2_110, 168061)['cost'], 110)
        self.assertGreater(len({u['cost'] for u in self.catalog.by_name['Pe-2-83']}), 1)
        # GUIDs are obtained from these actual descriptor bytes. Display name
        # alone cannot select which Pe-2 loadout and price were present.
        m.write(self.native.descriptors[0] + 0x18, bytes.fromhex(PE2_110))
        m.write(self.native.descriptors[1] + 0x18, bytes.fromhex(LORRAINE))
        self.report.dictionary({0x10: 'Pe-2-83', 0x20: self.catalog.unit(LORRAINE, 168061)['name']},
                               group=self.report.dictionary_group)
        for index, kill_points, loss_points in ((0, 42, 101), (1, 777, 13)):
            row = self.report.result + 0x40 + index * 0x88
            m.u32(row + 0x38, kill_points);m.u32(row + 0x40, loss_points)
        self.raw_report = self.report.reader().poll()
        self.victim_id = self.raw_report.players[0]['player_user_id']
        self.killer_id = self.raw_report.players[1]['player_user_id']
        m.write(self.report.variant, struct.pack('<QI4xQ', 7, 0, 0))
        m.close = lambda: None
        self.clock = 100.0
        game = ProcessInfo(101, 10000, 'unused-native-fixture/SteelDivision2.exe', m.exe_sha256, m.image_base)
        for target, options in (
            ('companion.windows.list_game_processes', {'return_value': [game]}),
            ('companion.windows.WindowsMemory', {'return_value': m}),
            ('companion.windows.tcp_connections', {'side_effect': AssertionError('native SID is not a TCP heuristic')}),
        ):
            patcher = patch(target, **options)
            patcher.start();self.addCleanup(patcher.stop)
        self.config = SimpleNamespace(data_directory=self.base / 'data', report_timeout_seconds=1000,
                                      process_poll_seconds=1, game_executable=None, max_replay_files=2000,
                                      rooms=(), replay_directories=(), collection_mode='auto')
        self.monitor = DeniedFileMonitor()
        self.observer = AutomaticWindowsObserver(self.config, monitor=self.monitor)
        self.outbox = Outbox(self.base / 'outbox.sqlite3', clock=lambda: self.clock)
        self.archive = self.base / 'finished-server-analysis.sqlite3'
        save_archive(self.archive, self.archive_document())
        self.archive_hash = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.inbox = self.base / 'inbox.sqlite3'
        self.receiver = ReceiverStore(self.inbox, archive_index=ArchiveIndex(self.archive))
        self.combat = CombatStore(self.base / 'combat.sqlite3', catalog=self.catalog)
        self.token = 'native-stats-v3-loopback-fixture'
        self.server = create_server(self.receiver, self.token, combat_store=self.combat)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start();self.addCleanup(self.stop_server)
        self.uploader = Uploader(f'http://127.0.0.1:{self.server.server_port}' + API_PATH,
                                 self.token, allow_loopback_http=True)
        self.service = AutomaticCaptureService(self.config, self.observer, self.outbox,
                                               uploader=self.uploader, clock=lambda: self.clock)
        self.addCleanup(self.service.close)

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def archive_document(self):
        players = []
        for index, player in enumerate(self.raw_report.players):
            guid, count = (PE2_110, 3) if index == 0 else (LORRAINE, 8)
            players.append({'id': player['player_user_id'], 'name': player['name'], 'alliance': index,
                'is_observer': False, 'participation_verified': True, 'stream_source_slot': index,
                'slot_binding': {'status': 'cross_validated'},
                'stats': {'production_orders': {'evidence': 'production_requests',
                    'cancellation_adjustment': {'status': 'approximate', 'unmatched_count': 0},
                    'units': [{'guid': guid, 'role': 'unit', 'requested_count': count + 1,
                               'cancelled_count': 1, 'net_count': count, 'catalog_cost': 999999}]}}})
        return {'schema': 1, 'source': {'origin': 'server_collection', 'sha256': SERVER_SHA},
                'match': {'session_id': self.native.session.session_id, 'version': 168061,
                          'map': self.native.session.map, 'completion': {'status': 'finished'}},
                'players': players, 'catalog': {'sha256': self.catalog.catalog_sha256},
                'identity_binding': {'validated': True}, 'capabilities': {'production_orders': True}}

    def tick(self):
        self.clock += 0.25
        return self.service.tick()

    def capture(self):
        observation = self.tick()
        self.assertTrue(observation['baseline_ready'])
        self.assertFalse(observation['native_session']['game_ended'])
        self.assertEqual(observation['file_monitor_error'], 'file_monitor_requires_elevation')
        self.assertEqual(self.outbox.status()['total'], 0)
        self.memory.write(self.native.session_fixture.context + 0x59, b'\x01')
        self.memory.write(self.report.variant, struct.pack('<QI4xQ', 8, 1, self.report.result))
        observation = self.tick()
        self.assertTrue(observation['report_context_stable'])
        self.assertEqual(self.outbox.status()['total'], 1)
        payload = self.outbox.due(now=self.clock + 1)[0]['payload']
        self.assertEqual(payload['schema'], 'sd2-task-report-v4')
        self.assertEqual(payload['binding_mode'], 'native_session')
        self.assertEqual(payload['session_id'], self.native.session.session_id)
        self.assertEqual([r['row_index'] for r in payload['rows']], [0, 1])
        self.assertEqual(payload['rows'][0]['game_time_seconds'], payload['rows'][1]['game_time_seconds'])
        for row, raw in zip(payload['rows'], self.raw_report.rows):
            self.assertEqual(row['game_time_seconds'], raw['game_time_seconds'])
            for side in ('killer', 'victim'):
                for key, value in raw[side].items():
                    if key not in ('unit_guid', 'identity_status'):
                        self.assertEqual(row[side][key], value)
        def check_public(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertFalse(any(term in key for term in ('address', 'pointer', 'common_id', 'source_rank')))
                    check_public(child)
            elif isinstance(value, list):
                for child in value:check_public(child)
        check_public(payload)
        self.assertFalse({'client_replay_sha256', 'client_header_sha256', 'room_hint'} & payload.keys())
        exports = list((self.config.data_directory / 'exports' / 'live').glob('*/*.json'))
        self.assertEqual(len(exports), 1)
        self.assertEqual(json.loads(exports[0].read_text(encoding='utf-8')), payload)
        self.assertEqual(list(self.base.rglob('*.rpl3')), [])
        return payload, observation

    def deliver_and_derive(self, payload):
        self.assertEqual(self.service.flush(), 1)
        self.assertEqual(self.outbox.status()['sent'], 1)
        saved = self.receiver.report(payload['submission_id'])
        self.assertEqual(saved['status'], 'accepted')
        self.assertEqual(saved['report'], payload)
        self.combat.refresh(self.inbox, [self.archive])
        result = self.combat.match(SERVER_SHA)
        self.assertTrue(result['available'])
        self.assertEqual(result['selection']['submission_id'], payload['submission_id'])
        self.assertEqual(len(result['events']), 2)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.archive_hash)
        return result

    def get_json(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + self.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:connection.close()

    def test_native_guids_select_real_loadout_price_and_keep_duplicate_events(self):
        payload, observation = self.capture()
        self.assertEqual(observation['native_stats_status'], 'native_stats_read')
        self.assertEqual(payload['report_data'], {'identity_source': 'native_stats_history',
                         'identity_status': 'complete', 'matched_rows': 2, 'total_rows': 2})
        self.assertTrue(all(row['victim']['unit_guid'] == PE2_110 for row in payload['rows']))
        self.assertTrue(all(row['killer']['unit_guid'] == LORRAINE for row in payload['rows']))
        result = self.deliver_and_derive(payload)
        player = next(p for p in result['players'] if p['player_user_id'] == self.killer_id)
        self.assertEqual((player['official_kill_points'], player['official_loss_points']), (777, 13))
        self.assertEqual(player['economy']['P_est'], 8 * 130)
        self.assertEqual(player['economy']['K'], 2 * 110)
        self.assertEqual(player['economy']['kill_count'], 2)
        self.assertAlmostEqual(player['economy']['conversion_ratio'], 220 / 1040)
        self.assertTrue(all(row['victim']['valuation']['price_status'] == 'resolved_native_stats' for row in result['events']))
        self.assertTrue(all(not row['victim']['valuation']['price_inferred'] for row in result['events']))
        by_http = self.get_json(COMBAT_PATH + '/players/' + self.killer_id)
        self.assertEqual(by_http['official_totals']['kill_points'], 777)
        self.assertEqual(by_http['economy']['P_est'], 1040)
        self.assertEqual(by_http['economy']['K'], 220)
        self.assertEqual(self.get_json(API_PATH + '/' + payload['submission_id'])['report'], payload)
        self.assertEqual(self.uploader.send(payload)['status'], 'duplicate')
        self.combat.refresh(self.inbox, [self.archive])
        self.assertEqual(self.combat.player(self.killer_id)['match_count'], 1)

    def test_unknown_native_selector_keeps_ambiguous_loadout_value_unknown(self):
        descriptor = self.native.descriptors[0]
        table = struct.unpack('<Q', self.memory.read(descriptor + 0x68, 8))[0]
        selector = struct.unpack('<Q', self.memory.read(table + 0x23 * 8, 8))[0]
        self.memory.u64(selector, self.memory.image_base + 0x123456)
        payload, _ = self.capture()
        self.assertTrue(all(row['victim']['unit_guid'] is None for row in payload['rows']))
        self.assertEqual(payload['report_data']['matched_rows'], 0)
        result = self.deliver_and_derive(payload)
        player = next(p for p in result['players'] if p['player_user_id'] == self.killer_id)
        self.assertEqual(player['economy']['P_est'], 1040)
        self.assertIsNone(player['economy']['K'])
        self.assertEqual(player['economy']['K_known_value'], 0)
        self.assertEqual(player['economy_by_scope']['all_components']['unknown_kill_value_count'], 2)
        self.assertIsNone(player['economy']['conversion_ratio'])
        self.assertTrue(all(row['victim']['valuation']['cost'] is None for row in result['events']))
        self.assertEqual(player['official_kill_points'], 777)

    def test_unmatched_ui_projection_does_not_attach_native_guid_or_invent_zero_value(self):
        self.memory.write(self.report.rows_address + 0x00, struct.pack('<d', 60.10000000001))
        self.memory.write(self.report.rows_address + 0xB8, struct.pack('<d', 60.10000000001))
        self.memory.write(self.report.variant, struct.pack('<QI4xQ', 7, 1, self.report.result))
        self.raw_report = self.report.reader().poll()
        self.memory.write(self.report.variant, struct.pack('<QI4xQ', 7, 0, 0))
        payload, _ = self.capture()
        self.assertTrue(all(row['victim']['identity_status'] == 'unresolved' for row in payload['rows']))
        result = self.deliver_and_derive(payload)
        player = next(p for p in result['players'] if p['player_user_id'] == self.killer_id)
        self.assertIsNone(player['economy_by_scope']['all_components']['K'])
        self.assertEqual(player['economy_by_scope']['all_components']['unknown_kill_value_count'], 2)
        self.assertEqual(player['official_kill_points'], 777)


if __name__ == '__main__':unittest.main()
