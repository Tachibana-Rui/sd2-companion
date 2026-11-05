"""Exact-build manager registry and lazy result cache ABI regression tests.

Fixtures contain process-style bytes, not pre-decoded report JSON. No game,
Windows process API, UAC, or network is used by these tests.
"""
import struct
import unittest

from companion.native_report import NativeReportError, NativeReportReader, UnstableSnapshotError
from test_companion_native_report import ReportFixture


class RegistryFixture(ReportFixture):
    REGISTRY_RVA = 0x02AF5490
    MANAGER_VTABLE_RVA = 0x01E81B18
    INTERFACE_VTABLE_RVA = 0x01EB4B40
    LAZY_GETTER_RVA = 0x015371D0

    def __init__(self, count=2):
        super().__init__(count=count)
        m = self.memory
        self.root = m.alloc(0x200)
        self.container = self.root + 0x1A0
        self.global_address = m.image_base + self.REGISTRY_RVA
        m.map(self.global_address, struct.pack('<Q', self.root))
        self.interface_vtable = m.image_base + self.INTERFACE_VTABLE_RVA
        self.lazy_getter = m.image_base + self.LAZY_GETTER_RVA
        m.map(self.interface_vtable, bytes(0x80))
        m.u64(self.interface_vtable + 0x70, self.lazy_getter)
        # Real lazy-method prologue: deliberately outside the leaf whitelist.
        # The production reader must use its proven storage field, not read or
        # interpret the method's allocation branch.
        m.map(self.lazy_getter, bytes.fromhex(
            '40574883ec3048c7442420feffffff48895c2448488bd9488b41484885c07551') + bytes(128))
        self.service_getter = m.getter(0x001652D0, b'\x48\x8b\x41\x08\xc3')
        self.service_vtable = m.alloc(0x10)
        m.u64(self.service_vtable + 8, self.service_getter)
        self.manager, self.interface, self.variant = self.add_manager(generation=7)
        self.entries = m.alloc(8, struct.pack('<Q', self.manager))
        self.pending_entries = m.alloc(8, struct.pack('<Q', self.manager))
        m.write(self.container, struct.pack('<QIIQII', self.entries, 1, 1, 0, 0, 0))

    def add_manager(self, generation=8):
        m = self.memory
        interface = m.alloc(0x50)
        m.u64(interface, self.interface_vtable)
        data = bytearray(0x30)
        struct.pack_into('<QI4xQ', data, 0, generation, 1, self.result)
        struct.pack_into('<Q', data, 0x28, interface)
        variant = m.alloc(0x30, data)
        m.u64(interface + 0x48, variant)
        service = m.alloc(0x10)
        m.u64(service, self.service_vtable)
        m.u64(service + 8, interface)
        manager = m.alloc(0x50)
        m.u64(manager, m.image_base + self.MANAGER_VTABLE_RVA)
        m.u64(manager + 8, self.container)
        m.u64(manager + 0x48, service)
        return manager, interface, variant

    def reader(self):
        return NativeReportReader(self.memory)


class NativeRegistryTests(unittest.TestCase):
    def test_real_lazy_cache_reads_report_without_heap_scan_or_method_interpretation(self):
        f = RegistryFixture()
        def no_heap_scan():
            raise AssertionError('registry locator unexpectedly scanned the heap')
        f.memory.iter_readable_regions = no_heap_scan
        reader = f.reader()
        report = reader.poll()
        self.assertEqual(report.generation, 7)
        self.assertEqual(len(report.rows), 2)
        self.assertEqual(report.rows[0]['victim']['unit_name'], 'Т-34/85')
        self.assertEqual(report.metadata['locator'], 'native_manager_registry')
        self.assertEqual(reader.diagnostics()['scan_bytes_total'], 0)
        self.assertFalse(any(address <= f.lazy_getter < address + size
                             for address, size in f.memory.reads))
        self.assertIsNone(reader.poll())
        self.assertEqual(reader.status, 'unchanged_report')

    def test_null_lazy_cache_is_waiting_not_an_empty_timeline(self):
        f = RegistryFixture()
        f.memory.u64(f.interface + 0x48, 0)
        reader = f.reader()
        self.assertIsNone(reader.poll())
        self.assertTrue(reader.baseline_ready)
        self.assertEqual(reader.status, 'waiting_for_published_report')
        self.assertEqual(reader.diagnostics()['scan_bytes_total'], 0)
        self.assertFalse(any(address == f.result for address, _ in f.memory.reads))
        f.memory.u64(f.interface + 0x48, f.variant)
        self.assertEqual(len(reader.poll().rows), 2)

    def test_unpublished_kind_zero_does_not_become_successful_zero_rows(self):
        f = RegistryFixture()
        f.memory.u32(f.variant + 8, 0)
        reader = f.reader()
        self.assertIsNone(reader.poll())
        self.assertTrue(reader.baseline_ready)
        self.assertEqual(reader.status, 'waiting_for_published_report')
        f.memory.u32(f.variant + 8, 1)
        self.assertEqual(len(reader.poll().rows), 2)

    def test_valid_published_empty_timeline_remains_a_real_report(self):
        reader = RegistryFixture(count=0).reader()
        report = reader.poll()
        self.assertEqual(report.rows, [])
        self.assertEqual(len(report.players), 2)
        self.assertEqual(report.metadata['locator'], 'native_manager_registry')

    def test_manager_and_variant_backlinks_must_both_match(self):
        for kind in ('manager', 'variant'):
            with self.subTest(kind=kind):
                f = RegistryFixture()
                location = f.manager + 8 if kind == 'manager' else f.variant + 0x28
                f.memory.u64(location, f.root)
                with self.assertRaisesRegex(NativeReportError,
                                            'invalid_native_manager_owner' if kind == 'manager'
                                            else 'invalid_native_result_owner'):
                    f.reader().poll()

    def test_same_manager_in_primary_and_pending_vectors_is_deduplicated(self):
        f = RegistryFixture()
        f.memory.write(f.container + 0x10, struct.pack('<QII', f.pending_entries, 1, 1))
        report = f.reader().poll()
        self.assertEqual(report.variant_address, f.variant)
        self.assertEqual(len(report.rows), 2)

    def test_pending_vector_alone_can_locate_a_manager(self):
        f = RegistryFixture()
        f.memory.write(f.container, struct.pack('<QIIQII', 0, 0, 0, f.pending_entries, 1, 1))
        self.assertEqual(f.reader().poll().variant_address, f.variant)

    def test_different_live_result_variants_remain_ambiguous(self):
        f = RegistryFixture()
        second, _, _ = f.add_manager()
        entries = f.memory.alloc(16, struct.pack('<QQ', f.manager, second))
        f.memory.write(f.container, struct.pack('<QII', entries, 2, 2))
        with self.assertRaisesRegex(NativeReportError, 'ambiguous_native_report'):
            f.reader().poll()

    def test_result_cache_switch_during_report_copy_never_delivers_old_snapshot(self):
        f = RegistryFixture()
        _, _, replacement = f.add_manager()
        # A replacement must belong to the selected interface to be a valid
        # later report, rather than failing only an owner validation.
        f.memory.u64(replacement + 0x28, f.interface)
        changed = False
        def mutation(address, size):
            nonlocal changed
            if address == f.result and size == 0xBE0 and not changed:
                changed = True
                f.memory.u64(f.interface + 0x48, replacement)
        f.memory.before_read = mutation
        reader = f.reader()
        with self.assertRaises(UnstableSnapshotError):reader.poll()
        f.memory.before_read = None
        self.assertEqual(reader.poll().generation, 8)

    def test_registry_change_during_double_read_is_rejected_and_retryable(self):
        f = RegistryFixture()
        reads = 0
        def mutation(address, size):
            nonlocal reads
            if address == f.container and size == 0x20:
                reads += 1
                if reads == 2:
                    f.memory.u32(f.container + 8, 0)
        f.memory.before_read = mutation
        reader = f.reader()
        with self.assertRaises(UnstableSnapshotError):reader.poll()
        f.memory.before_read = None
        f.memory.u32(f.container + 8, 1)
        self.assertEqual(reader.poll().generation, 7)

    def test_invalid_vector_count_is_rejected_before_reading_pointer_array(self):
        f = RegistryFixture()
        f.memory.u32(f.container + 8, 0xFFFFFFFF)
        reader = f.reader()
        with self.assertRaisesRegex(NativeReportError, 'invalid_native_manager_registry'):
            reader.poll()
        self.assertFalse(any(address == f.entries for address, _ in f.memory.reads))
        self.assertEqual(reader.diagnostics()['scan_bytes_total'], 0)

    def test_missing_global_retains_valid_controller_fallback(self):
        f = RegistryFixture()
        f.memory.u64(f.global_address, 0)
        f.controller()
        reader = f.reader()
        report = reader.poll()
        self.assertEqual(report.metadata['locator'], 'native_report_controller')
        self.assertEqual(report.variant_address, f.variant)
        self.assertGreater(reader.diagnostics()['scan_bytes_total'], 0)


if __name__ == '__main__':
    unittest.main()
