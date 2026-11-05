"""Automatic replay observation never needs a server connection or room ID."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.config import Config, ConfigError, example_config, from_dict
from companion.file_access import FileMonitorError, FileReadEvent
from companion.replay_runtime import ReplayWindowsObserver
from companion.windows import ProcessInfo


class FakeMonitor:
    def __init__(self):
        self.status = 'file_monitor_running'
        self.events = []
        self.selections = []
        self.started = self.closed = 0

    def start(self):self.started += 1
    def set_process(self, pid, key):self.selections.append((pid, key))
    def poll(self):
        events, self.events = self.events, []
        return events
    def close(self):self.closed += 1


class ReplayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.monitor = FakeMonitor()
        self.config = Config(rooms=(), replay_directories=(), data_directory=Path('unused'), collection_mode='replay')
        self.process = ProcessInfo(101, 10000, 'SteelDivision2.exe', 'a' * 64, 0x140000000)
        self.memory = SimpleNamespace(reset_budget=lambda: None, close=lambda: None)
        self.reader = SimpleNamespace(baseline_ready=True, status='waiting_for_published_report', poll=lambda: None)
        self.patches = [patch('companion.windows.list_game_processes', return_value=[self.process]),
                        patch('companion.windows.WindowsMemory', return_value=self.memory),
                        patch('companion.native_report.NativeReportReader', return_value=self.reader),
                        patch('companion.windows.tcp_connections', side_effect=AssertionError('TCP is not replay identity'))]
        for patcher in self.patches:patcher.start();self.addCleanup(patcher.stop)
        self.observer = ReplayWindowsObserver(self.config, self.monitor)
        self.addCleanup(self.observer.close)

    def test_first_empty_result_is_confirmed_without_tcp_or_room(self):
        result = self.observer.poll(10)
        self.assertTrue(result['baseline_ready'])
        self.assertTrue(result['report_identity_clear'])
        self.assertEqual(result['process_key'], '101:10000')
        self.assertNotIn('room_id', result)
        self.assertEqual(self.monitor.selections[-1], (101, '101:10000'))

    def test_read_and_write_ranges_survive_observer_unchanged(self):
        self.observer.poll(10)
        self.monitor.events = [FileReadEvent('101:10000', 'C:\\Replays\\match.rpl3', 10.1, 1, 10000, 8192, 'write')]
        self.reader.status = 'native_report_read'
        native = object();self.reader.poll = lambda: native
        result = self.observer.poll(10.5)
        self.assertIs(result['report'], native)
        self.assertEqual(result['file_events'][0]['operation'], 'write')
        self.assertEqual(result['file_events'][0]['byte_offset'], 10000)
        self.assertFalse(result['report_identity_clear'])

    def test_partial_scan_does_not_claim_no_report_or_clear(self):
        self.reader.baseline_ready = False
        self.reader.status = 'locating_report_controller'
        result = self.observer.poll(10)
        self.assertFalse(result['baseline_ready'])
        self.assertFalse(result['report_identity_clear'])

    def test_event_loss_invalidates_association_but_still_captures_report(self):
        self.observer.poll(10)
        self.monitor.status = 'file_monitor_events_lost'
        native = object();self.reader.poll = lambda: native
        result = self.observer.poll(10.5)
        self.assertTrue(result['access_uncertain'])
        self.assertIs(result['report'], native)
        self.assertEqual(result['error'], 'file_monitor_events_lost')

    def test_monitor_requires_elevation_has_actionable_error(self):
        def denied():raise FileMonitorError('file_monitor_requires_elevation')
        self.monitor.start = denied
        result = self.observer.poll(10)
        self.assertEqual(result['error'], 'file_monitor_requires_elevation')
        self.assertTrue(result['access_uncertain'])
        self.assertIsNone(self.observer.memory)

    def test_initial_existing_report_is_baseline_not_new_capture(self):
        native = object();self.reader.poll = lambda: native
        self.reader.status = 'native_report_read'
        result = self.observer.poll(10)
        self.assertIs(result['baseline_report'], native)
        self.assertNotIn('report', result)
        self.observer.close();self.observer.close()
        self.assertEqual(self.monitor.closed, 1)


class ReplayConfigurationTests(unittest.TestCase):
    def test_legacy_rooms_do_not_silently_select_live_collection(self):
        raw = {'schema': 1, 'rooms': [{'id': 'old-room', 'endpoints': [{'host': '127.0.0.1', 'port': 7552}]}],
               'replay_directories': ['old-replays']}
        self.assertEqual(from_dict(raw).collection_mode, 'auto')
        self.assertEqual(from_dict({**raw, 'collection_mode': 'live'}).collection_mode, 'live')

    def test_default_needs_no_room_or_replay_directory(self):
        config = from_dict(example_config())
        self.assertEqual(config.collection_mode, 'auto')
        self.assertEqual(config.rooms, ())
        self.assertEqual(config.replay_directories, ())

    def test_live_needs_no_local_file_or_room_and_unknown_mode_is_rejected(self):
        self.assertEqual(from_dict({'schema': 1, 'collection_mode': 'live'}).collection_mode, 'live')
        with self.assertRaises(ConfigError):from_dict({'schema': 1, 'collection_mode': 'guess'})


if __name__ == '__main__':unittest.main()
