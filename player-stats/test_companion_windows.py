"""Synthetic Win32 layouts plus an owned Python child; never inspect the game."""
import ctypes
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest

from companion import windows as win


def region(base, size, *, state=win.MEM_COMMIT, protect=4, kind=win.MEM_PRIVATE):
    result = win.MEMORY_BASIC_INFORMATION64()
    result.BaseAddress = base
    result.RegionSize = size
    result.State = state
    result.Protect = protect
    result.Type = kind
    return result


class FakeAPI:
    def __init__(self, path):
        self.path = str(path)
        self.created = 123456789012345678
        self.base = 0x140000000
        self.is_alive = True
        self.access = []
        self.closed = []
        self.reads = []
        self.regions = []
        self.payload = b"safe-read-only-fixture"
        self.table = {win.AF_INET_WIN: struct.pack("<I", 0),
                      win.AF_INET6_WIN: struct.pack("<I", 0)}

    def open_process(self, pid, access):
        self.access.append((pid, access))
        return pid + 1000

    def close(self, handle):
        self.closed.append(handle)

    def creation_time(self, handle):
        return self.created

    def alive(self, handle):
        return self.is_alive

    def image_path(self, handle):
        return self.path

    def image_base(self, pid):
        return self.base

    def processes(self):
        return iter([(77, "SteelDivision2.EXE"), (78, "unrelated.exe")])

    def read(self, handle, address, size):
        self.reads.append((handle, address, size))
        return self.payload[:size]

    def region(self, handle, address):
        return next((value for value in self.regions
                     if value.BaseAddress <= address < value.BaseAddress + value.RegionSize), None)

    def tcp_table(self, family):
        return self.table[family]


class WindowsFakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "SteelDivision2.exe"
        self.path.write_bytes(b"synthetic executable, not runnable")
        self.api = FakeAPI(self.path)

    def memory(self, **kwargs):
        reader = win.WindowsMemory(77, _api=self.api, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def test_native_structure_offsets_are_explicit(self):
        self.assertEqual(ctypes.sizeof(win.MIB_TCPROW_OWNER_PID), 24)
        self.assertEqual(ctypes.sizeof(win.MIB_TCP6ROW_OWNER_PID), 56)
        self.assertEqual(win.MIB_TCP6ROW_OWNER_PID.dwRemotePort.offset, 44)
        self.assertEqual(win.MIB_TCP6ROW_OWNER_PID.dwOwningPid.offset, 52)
        self.assertEqual(ctypes.sizeof(win.MEMORY_BASIC_INFORMATION64), 48)
        self.assertEqual(win.MEMORY_BASIC_INFORMATION64.RegionSize.offset, 24)
        self.assertEqual(win.MEMORY_BASIC_INFORMATION64.Protect.offset, 36)

    def test_identification_has_exact_start_time_and_only_query_rights(self):
        found = win.get_process_info(77, _api=self.api)
        self.assertEqual(found.creation_time_100ns, self.api.created)
        self.assertEqual(found.exe_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(found.exe_path, str(self.path.resolve()))
        self.assertEqual(found.path, found.exe_path)
        self.assertEqual(self.api.access, [(77, win.PROCESS_QUERY_LIMITED_INFORMATION)])
        self.assertEqual(self.api.closed, [1077])
        self.assertEqual(self.api.reads, [])

    def test_discovery_filters_name_path_and_hash_without_virtual_memory_reads(self):
        found = win.list_game_processes(executable_path=self.path, _api=self.api)
        self.assertEqual([value.pid for value in found], [77])
        self.assertEqual(win.list_game_processes(expected_sha256="0" * 64, _api=self.api), [])
        self.assertEqual(win.list_game_processes(executable_path=self.path.parent / "other.exe",
                                               _api=self.api), [])
        self.assertEqual(self.api.reads, [])

    def test_open_rejects_reused_identity_and_closes_handle(self):
        info = win.get_process_info(77, _api=self.api)
        self.api.closed.clear()
        with self.assertRaisesRegex(win.WindowsAccessError, "identity"):
            win.WindowsMemory(replace(info, creation_time_100ns=info.creation_time_100ns - 1), _api=self.api)
        self.assertEqual(self.api.closed, [1077])
        self.assertEqual(self.api.reads, [])

    def test_open_rejects_fingerprint_or_base_mismatch(self):
        for kwargs in ({"exe_sha256": "0" * 64}, {"image_base": 0x180000000},
                       {"expected_creation_time": 0}):
            with self.assertRaises(win.WindowsAccessError):
                win.WindowsMemory(77, _api=self.api, **kwargs)
        self.assertEqual(len(self.api.closed), 3)

    def test_read_is_bounded_and_minimal_rights(self):
        reader = self.memory(max_read_bytes=8, max_single_read=5)
        self.assertEqual(self.api.access, [(77, win.PROCESS_QUERY_INFORMATION | win.PROCESS_VM_READ)])
        self.assertEqual(reader.read(0, 0), b"")
        self.assertEqual(reader.read(0x1000, 5), b"safe-")
        self.assertEqual(reader.bytes_read, 5)
        with self.assertRaises(win.BudgetExceeded):
            reader.read(0x1000, 4)
        self.assertEqual(len(self.api.reads), 1)
        with self.assertRaises(ValueError):
            reader.read(0, 1)
        with self.assertRaises(ValueError):
            reader.read(0x1000, True)
        with self.assertRaises(win.WindowsAccessError):
            reader.read(win.MAX_ADDRESS, 2)

    def test_failed_or_partial_read_spends_budget(self):
        reader = self.memory(max_read_bytes=8)
        self.api.payload = b"x"
        with self.assertRaisesRegex(win.WindowsAccessError, "Incomplete"):
            reader.read(0x1000, 8)
        self.assertEqual(reader.bytes_read, 8)
        with self.assertRaises(win.BudgetExceeded):
            reader.read(0x1000, 1)

    def test_budget_reset_is_explicit_and_preserves_lifetime_counts(self):
        reader = self.memory(max_read_bytes=4)
        self.assertEqual(reader.read(0x1000, 4), b"safe")
        with self.assertRaises(win.BudgetExceeded):
            reader.read(0x1000, 1)
        reader.reset_budget()
        self.assertEqual(reader.bytes_read, 0)
        self.assertEqual(reader.total_bytes_read, 4)
        self.assertEqual(reader.read(0x1000, 4), b"safe")
        self.assertEqual(reader.total_bytes_read, 8)

    def test_readable_scan_skips_guard_free_unreadable_and_images_by_default(self):
        self.api.regions = [region(0, 0x1000, state=0x10000),
                            region(0x1000, 0x1000, protect=0x104),
                            region(0x2000, 0x1000, protect=0x01),
                            region(0x3000, 0x1000, protect=0x02),
                            region(0x4000, 0x1000, protect=0x20, kind=0x1000000)]
        reader = self.memory()
        self.assertEqual(list(reader.iter_readable_regions()), [(0x3000, 0x1000)])
        self.assertEqual(list(reader.iter_readable_regions(private_only=False)),
                         [(0x3000, 0x1000), (0x4000, 0x1000)])
        self.assertEqual(reader.bytes_read, 0)

    def test_region_limits_raise_instead_of_reporting_complete_empty_scan(self):
        self.api.regions = [region(0, 0x1000), region(0x1000, 0x1000)]
        reader = self.memory(max_region_queries=1)
        with self.assertRaises(win.BudgetExceeded):
            list(reader.iter_readable_regions())
        second = self.memory(max_regions=1)
        with self.assertRaises(win.BudgetExceeded):
            list(second.iter_readable_regions())

    def test_invalid_region_does_not_loop_forever(self):
        reader = self.memory()
        self.api.region = lambda handle, address: region(0, 0)
        with self.assertRaisesRegex(win.WindowsAccessError, "bounds"):
            list(reader.iter_readable_regions())

    def test_close_is_idempotent_and_prevents_reads(self):
        reader = self.memory()
        reader.close()
        reader.close()
        self.assertEqual(self.api.closed, [1077])
        with self.assertRaisesRegex(win.WindowsAccessError, "closed"):
            reader.read(0x1000, 1)

    def test_ipv4_table_filters_pid_and_state_and_decodes_network_order(self):
        def row(pid, state):
            return struct.pack("<IIIIII", state,
                               struct.unpack("<I", socket.inet_pton(socket.AF_INET, "127.0.0.1"))[0],
                               socket.htons(50000),
                               struct.unpack("<I", socket.inet_pton(socket.AF_INET, "203.0.113.8"))[0],
                               socket.htons(7552), pid)
        self.api.table[win.AF_INET_WIN] = struct.pack("<I", 3) + row(77, 5) + row(77, 8) + row(88, 5)
        values = win.tcp_connections(77, _api=self.api)
        self.assertEqual(len(values), 1)
        self.assertEqual((values[0].remote_address, values[0].remote_port), ("203.0.113.8", 7552))
        self.assertEqual((values[0].local_address, values[0].local_port), ("127.0.0.1", 50000))
        self.assertEqual(len(win.tcp_connections(77, False, _api=self.api)), 2)

    def test_ipv6_layout_and_scope_ids(self):
        row = win.MIB_TCP6ROW_OWNER_PID()
        row.ucLocalAddr[:] = socket.inet_pton(socket.AF_INET6, "::1")
        row.ucRemoteAddr[:] = socket.inet_pton(socket.AF_INET6, "fe80::1234")
        row.dwLocalPort = socket.htons(50001)
        row.dwRemotePort = socket.htons(10804)
        row.dwRemoteScopeId = socket.htonl(3)
        row.dwState = 5
        row.dwOwningPid = 77
        self.api.table[win.AF_INET6_WIN] = struct.pack("<I", 1) + bytes(row)
        value = win.tcp_connections(77, _api=self.api)[0]
        self.assertEqual((value.family, value.remote_address, value.remote_port),
                         ("ipv6", "fe80::1234", 10804))
        self.assertEqual(value.remote_scope_id, 3)

    def test_truncated_or_oversized_tcp_rows_fail_closed(self):
        for data in (b"", struct.pack("<I", 1), struct.pack("<I", 0xFFFFFFFF) + bytes(24)):
            with self.assertRaises(win.WindowsAccessError):
                win._parse_tcp_table(data, win.AF_INET_WIN, 77)

    @unittest.skipIf(os.name == "nt", "non-Windows import boundary")
    def test_native_calls_are_unavailable_without_import_side_effects(self):
        with self.assertRaises(win.WindowsAccessError):
            win.get_process_info(77)


@unittest.skipUnless(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8,
                     "requires 64-bit Windows; only an owned Python child is opened")
class OwnedChildWindowsTests(unittest.TestCase):
    def test_real_native_read_and_region_query_on_own_python_child(self):
        code = """import ctypes,json,os,sys
payload=ctypes.create_string_buffer(b'companion-owned-child-fixture')
print(json.dumps({'pid':os.getpid(),'address':ctypes.addressof(payload),'size':len(payload)}),flush=True)
sys.stdin.read(1)
"""
        child = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW, text=True)
        try:
            fixture = json.loads(child.stdout.readline())
            self.assertEqual(fixture["pid"], child.pid)
            info = win.get_process_info(child.pid)
            self.assertEqual(Path(info.exe_path).resolve(), Path(sys.executable).resolve())
            self.assertGreater(info.creation_time_100ns, 0)
            self.assertGreater(info.image_base, 0)
            self.assertTrue(all(value.pid == child.pid for value in win.tcp_connections(child.pid)))
            with win.WindowsMemory(info, max_read_bytes=1024, max_regions=10000) as reader:
                self.assertEqual(reader.read(fixture["address"], fixture["size"]),
                                 b"companion-owned-child-fixture\0")
                found = False
                for base, size in reader.iter_readable_regions():
                    if base <= fixture["address"] < base + size:
                        found = True
                        break
                self.assertTrue(found)
        finally:
            if child.poll() is None:
                child.stdin.write("q")
                child.stdin.flush()
            child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
