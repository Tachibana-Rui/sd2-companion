"""Exact-process ETW I/O tests. Never opens or controls an SD2 process."""
from __future__ import annotations

import ctypes
import os
import threading
import time
import unittest
import uuid

from companion.file_access import (FileAccessMonitor, FileMonitorError, PROVIDER_ID, SESSION_NAME,
    _EVENT_HEADER, _EVENT_RECORD, _EVENT_TRACE_LOGFILEW, _EVENT_TRACE_PROPERTIES, _GUID,
    _NativeAPI, _NativeSession, _PROPERTY_DATA_DESCRIPTOR, _replay_path)


class FakeSession:
    def __init__(self):
        self.stopped = threading.Event()
        self.close_count = 0

    def consume(self):
        self.stopped.wait()

    def close(self):
        self.close_count += 1
        self.stopped.set()


class FakeAPI:
    device_paths = [(r"\Device\HarddiskVolume7", "D:")]

    def __init__(self, error=None):
        self.error = error
        self.session = FakeSession()
        self.start_count = 0

    def start(self, name, accept, on_event, on_loss):
        self.start_count += 1
        self.name, self.accept, self.on_event, self.on_loss = name, accept, on_event, on_loss
        if self.error:
            raise FileMonitorError(self.error)
        return self.session

    def emit(self, event_id, pid=101, timestamp=101, **properties):
        self.on_event({"event_id": event_id, "pid": pid, "timestamp": timestamp, **properties})


class FileMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = [100]
        self.api = FakeAPI()
        self.monitor = FileAccessMonitor(clock=lambda: self.now[0], _api=self.api)
        self.monitor.start()
        self.monitor.set_process(101, "101:1000")
        self.addCleanup(self.monitor.close)

    def open(self, **changes):
        self.api.emit(12, **{"FileObject": 0x100, "FileName": r"\Device\HarddiskVolume7\回放\match.rpl3", **changes})

    def read(self, **changes):
        self.api.emit(15, **{"FileObject": 0x100, "ByteOffset": 10000, "IOSize": 65536, **changes})

    def test_create_and_filename_records_are_not_reported_as_playback(self):
        self.open()
        self.api.emit(10, pid=4, FileKey=0x200, FileName=r"D:\other.rpl3")
        self.assertEqual(self.monitor.poll(), [])
        self.assertEqual(self.api.name, SESSION_NAME)

    def test_read_preserves_absolute_unicode_path_pid_identity_and_extent(self):
        self.open();self.read()
        events = self.monitor.poll()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.path, "D:\\回放\\match.rpl3")
        self.assertEqual(event.process_key, "101:1000")
        self.assertEqual((event.byte_offset, event.read_size), (10000, 65536))
        self.assertEqual((event.offset, event.size, event.operation), (10000, 65536, "read"))
        self.assertEqual(event.observed_at, 101)
        self.assertEqual(self.monitor.poll(), [])

    def test_write_to_localized_cloud_replay_is_distinct_io_evidence(self):
        self.open()
        self.api.emit(16, FileObject=0x100, ByteOffset=2048, IOSize=8192)
        event = self.monitor.poll()[0]
        self.assertEqual((event.operation, event.byte_offset, event.read_size), ("write", 2048, 8192))

    def test_system_name_mapping_requires_actual_target_pid_read(self):
        self.api.emit(10, pid=4, FileKey=0x200, FileName=r"D:\file.rpl3")
        self.read(FileObject=0x999, FileKey=0x200, pid=202)
        self.assertEqual(self.monitor.poll(), [])
        self.read(FileObject=0x999, FileKey=0x200)
        self.assertEqual(self.monitor.poll()[0].path, r"D:\file.rpl3")

    def test_old_buffered_events_and_other_pids_are_not_relabelled(self):
        self.open(pid=202)
        self.read(pid=202)
        self.open(timestamp=99)
        self.read(timestamp=99)
        self.assertEqual(self.monitor.poll(), [])
        self.assertFalse(self.api.accept(15, 101, 99))
        self.assertFalse(self.api.accept(15, 202, 101))

    def test_pid_reuse_clears_pending_events_and_old_objects(self):
        self.open();self.read()
        self.now[0] = 110
        self.monitor.set_process(101, "101:2000")
        self.assertEqual(self.monitor.poll(), [])
        self.read(timestamp=111)
        self.assertEqual(self.monitor.poll(), [])
        self.open(timestamp=111);self.read(timestamp=112)
        self.assertEqual(self.monitor.poll()[0].process_key, "101:2000")
        self.monitor.set_process(None, None)
        self.read(timestamp=113)
        self.assertEqual(self.monitor.poll(), [])

    def test_close_name_delete_and_object_reuse_do_not_keep_stale_paths(self):
        self.open()
        self.api.emit(14, FileObject=0x100)
        self.read()
        self.assertEqual(self.monitor.poll(), [])
        self.open()
        self.open(FileName=r"D:\not-a-replay.txt")
        self.read()
        self.assertEqual(self.monitor.poll(), [])
        self.api.emit(10, pid=4, FileKey=0x200, FileName=r"D:\old.rpl3")
        self.api.emit(11, pid=4, FileKey=0x200, FileName=r"D:\old.rpl3")
        self.read(FileKey=0x200)
        self.assertEqual(self.monitor.poll(), [])

    def test_missing_read_extent_invalidates_monitor_instead_of_guessing(self):
        self.open();self.read()
        self.api.emit(15, FileObject=0x100, IOSize=4096)
        self.assertEqual(self.monitor.status, "file_monitor_event_schema_unsupported")
        self.assertEqual(self.monitor.poll(), [])

    def test_queue_overflow_and_etw_loss_clear_incomplete_evidence(self):
        self.monitor.max_events = 1
        self.open();self.read();self.read()
        self.assertEqual(self.monitor.status, "file_monitor_events_lost")
        self.assertEqual(self.monitor.poll(), [])
        self.api.on_loss("file_monitor_events_lost")
        self.assertEqual(self.monitor.poll(), [])

    def test_path_cache_capacity_failure_is_explicit(self):
        self.monitor.max_names = 1
        self.open()
        self.open(FileObject=0x101, FileName=r"D:\second.rpl3")
        self.assertEqual(self.monitor.status, "file_monitor_name_capacity_exceeded")
        self.assertEqual(self.monitor.poll(), [])

    def test_permission_denial_is_an_explicit_start_failure(self):
        api = FakeAPI("file_monitor_requires_elevation")
        monitor = FileAccessMonitor(_api=api)
        with self.assertRaises(FileMonitorError) as caught:
            monitor.start()
        self.assertEqual(caught.exception.code, "file_monitor_requires_elevation")
        self.assertEqual(monitor.status, "file_monitor_requires_elevation")
        self.assertIsNone(monitor.thread)
        monitor.close();monitor.close()
        self.assertEqual(api.session.close_count, 0)  # Never stops somebody else's session.

    def test_close_is_idempotent_and_ends_the_consumer(self):
        self.monitor.close();self.monitor.close()
        self.assertEqual(self.api.session.close_count, 1)
        self.assertFalse(self.monitor.thread.is_alive())
        self.assertEqual(self.monitor.status, "file_monitor_closed")


class WindowsAbiTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and ctypes.sizeof(ctypes.c_void_p) == 8, 'Windows 64-bit thread query')
    def test_real_issuing_thread_identity_is_read_only_and_rejects_reused_tid_time(self):
        api = _NativeAPI()
        current_thread_id = api.kernel.GetCurrentThreadId
        current_thread_id.argtypes, current_thread_id.restype = [], ctypes.c_uint32
        tid = current_thread_id()
        now_100ns = int((time.time() + 11644473600) * 10_000_000)
        self.assertEqual(api.thread_owner(tid, now_100ns), os.getpid())
        self.assertIsNone(api.thread_owner(tid, 0))
        self.assertIsNone(api.thread_owner(0, now_100ns))

    def test_64_bit_sdk_layouts_have_exact_sizes_and_offsets(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("64-bit ABI layouts")
        self.assertEqual(ctypes.sizeof(_EVENT_HEADER), 80)
        self.assertEqual(ctypes.sizeof(_EVENT_RECORD), 112)
        self.assertEqual(ctypes.sizeof(_EVENT_TRACE_PROPERTIES), 120)
        self.assertEqual(ctypes.sizeof(_EVENT_TRACE_LOGFILEW), 448)
        self.assertEqual(ctypes.sizeof(_PROPERTY_DATA_DESCRIPTOR), 16)
        self.assertEqual(_EVENT_TRACE_LOGFILEW.EventRecordCallback.offset, 424)
        self.assertEqual(_EVENT_RECORD.UserData.offset, 96)

    def test_nt_paths_and_extended_paths_are_translated_without_filesystem_search(self):
        self.assertEqual(_replay_path(r"\Device\HarddiskVolume7\match.rpl3", FakeAPI.device_paths), r"D:\match.rpl3")
        self.assertEqual(_replay_path(r"\??\D:\match.rpl3"), r"D:\match.rpl3")
        self.assertEqual(_replay_path(r"\\?\D:\match.rpl3"), r"D:\match.rpl3")
        self.assertEqual(_replay_path(r"\Device\Mup\server\share\match.rpl3"), r"\\server\share\match.rpl3")
        self.assertEqual(_replay_path(r"\\?\UNC\server\share\match.rpl3"), r"\\server\share\match.rpl3")
        for invalid in (r"relative.rpl3", r"D:relative.rpl3", r"\Device\Unknown\x.rpl3", r"D:\x.txt", "D:\\bad\0.rpl3"):
            self.assertIsNone(_replay_path(invalid, FakeAPI.device_paths))

    def test_native_callback_uses_tdh_property_names_and_preserves_read_write_extent(self):
        class API:
            def __init__(self):self.names=[]
            def property(self, pointer, name):
                self.names.append(name)
                return {"FileObject": 1, "FileKey": 2, "ByteOffset": 8192, "IOSize": 4096}.get(name)
        session = _NativeSession.__new__(_NativeSession)
        session.api = API()
        session.accept = lambda *args: True
        captured, failures = [], []
        session.on_event, session.on_loss = captured.append, failures.append
        record = _EVENT_RECORD()
        record.EventHeader.ProviderId = _GUID.from_buffer_copy(uuid.UUID(PROVIDER_ID).bytes_le)
        record.EventHeader.ProcessId = 101
        record.EventHeader.TimeStamp = int((11644473600 + 123) * 10_000_000)
        for event_id in (15, 16):
            record.EventHeader.EventDescriptor.Id = event_id
            session._record(ctypes.pointer(record))
        self.assertEqual(failures, [])
        self.assertEqual([event["event_id"] for event in captured], [15, 16])
        self.assertEqual(captured[0]["timestamp"], 123)
        self.assertEqual(captured[0]["ByteOffset"], 8192)
        self.assertEqual(session.api.names[:4], ["FileObject", "FileKey", "ByteOffset", "IOSize"])

    def test_buffer_callback_reports_lost_events_and_accepts_clean_buffers(self):
        session = _NativeSession.__new__(_NativeSession)
        session.stopping = False
        failures = []
        session.on_loss = failures.append
        log = _EVENT_TRACE_LOGFILEW()
        self.assertEqual(session._buffer(ctypes.pointer(log)), 1)
        self.assertEqual(failures, [])
        log.LogfileHeader.EventsLost = 1
        self.assertEqual(session._buffer(ctypes.pointer(log)), 1)
        self.assertEqual(failures, ["file_monitor_events_lost"])

    def test_system_context_read_requires_verified_issuing_thread_owner(self):
        class API:
            def property(self, pointer, name):
                return {"IssuingThreadId": 222, "FileObject": 1, "FileKey": 2,
                        "ByteOffset": 8192, "IOSize": 4096}.get(name)
            def thread_owner(self, tid, event_time):
                self.asserted = tid, event_time
                return self.owner
        session = _NativeSession.__new__(_NativeSession)
        session.api = API()
        session.api.owner = 101
        session.accept = lambda event_id, pid, timestamp: event_id == 0 or pid == 101
        captured, failures = [], []
        session.on_event, session.on_loss = captured.append, failures.append
        record = _EVENT_RECORD()
        record.EventHeader.ProviderId = _GUID.from_buffer_copy(uuid.UUID(PROVIDER_ID).bytes_le)
        record.EventHeader.ProcessId = 4
        record.EventHeader.TimeStamp = int((11644473600 + 123) * 10_000_000)
        record.EventHeader.EventDescriptor.Id = 15
        session._record(ctypes.pointer(record))
        self.assertEqual(captured[0]["pid"], 101)
        self.assertEqual(session.api.asserted[0], 222)
        session.api.owner = None
        session._record(ctypes.pointer(record))
        session.api.owner = 202
        session._record(ctypes.pointer(record))
        self.assertEqual(len(captured), 1)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
