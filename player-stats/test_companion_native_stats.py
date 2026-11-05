"""Actual Stats/registry byte layouts only; no game, PID, UI or network."""
from copy import deepcopy
from dataclasses import replace
import json
import struct
import unittest
from unittest.mock import patch

from companion.native_report import NativeReportError, UnsupportedBuildError
from companion.native_stats import (
    DESCRIPTOR_VTABLE_RVA, ENTITY_REGISTRY_RVA, ENTITY_VTABLE_RVA,
    MAX_EVENTS, MAX_PARTICIPANTS, MODULE_REGISTRY_RVA, PARTICIPANT_MANAGER_RVA,
    POOL_SELECTOR, SELECTOR_GETTER_RVA, SELECTOR_VTABLE_RVA, STATS_KIND,
    STATS_VTABLE_RVA, TEAM_VTABLE_RVA, UI_DESCRIPTOR_VTABLE_RVA,
    NativeStatsReader, bind_timeline,
)
from test_companion_native_report import ReportFixture
from test_companion_native_session import SessionFixture


class StatsFixture:
    def __init__(self, count=2):
        self.report_fixture = f = ReportFixture(count)
        self.memory = m = f.memory
        self.report = f.reader().poll()
        players = [(i, [('PlayerUserId', p['player_user_id']), ('PlayerName', p['name']),
                        ('PlayerAlliance', str(p['alliance'])), ('PlayerObserver', '0')])
                   for i, p in enumerate(self.report.players)]
        with patch('test_companion_native_session.ByteMemory', return_value=m):
            self.session_fixture = SessionFixture(players=players)
        self.session = self.session_fixture.reader().poll(process_key='stats-fixture')
        self.entities, self.stats, self.teams = [], [], []
        self.references = [0x1E, 0x11E]
        self.common_ids = [0xC000001E, 0xC000001F]
        for i in range(2):
            entity, stats, team = m.alloc(0x38), m.alloc(0x2B0), m.alloc(0x80)
            self.entities.append(entity);self.stats.append(stats);self.teams.append(team)
            m.u64(entity, m.image_base + ENTITY_VTABLE_RVA)
            m.u32(entity + 8, self.common_ids[i]);m.u32(entity + 0x30, self.references[i])
            modules = m.alloc(32, struct.pack('<B7xQB7xQ', 0x97, stats, 0xAC, team))
            m.u32(entity + 0x10, 0xA8000002);m.u64(entity + 0x18, modules)
            m.u64(stats, m.image_base + STATS_VTABLE_RVA);m.u32(stats + 8, self.references[i])
            m.u64(team, m.image_base + TEAM_VTABLE_RVA);m.u32(team + 8, self.references[i])
            m.u32(team + 0x10, i);m.u32(team + 0x30, i)
            m.u64(team + 0x38, f.owner_victim if i == 0 else f.owner_killer)
        pointers = m.alloc(16, struct.pack('<QQ', *self.entities))
        self.entity_registry = m.image_base + ENTITY_REGISTRY_RVA + 0x1E * 0x90
        m.map(self.entity_registry, bytes(0x50));m.write(self.entity_registry + 0x20, struct.pack('<QII', pointers, 2, 2))
        self.source = m.alloc(0x80)
        self.reference_array = m.alloc(8, struct.pack('<II', *self.references))
        m.u64(self.source + 0x38, self.reference_array);m.u32(self.source + 0x40, 2)
        m.u32(self.source + 0x70, 0xFFFFFFFF);m.u32(self.source + 0x74, 0xFFFFFFFF)
        m.map(m.image_base + PARTICIPANT_MANAGER_RVA, struct.pack('<Q', self.source))
        self.pool_root = m.image_base + MODULE_REGISTRY_RVA
        index = POOL_SELECTOR * 255 + STATS_KIND
        self.pool = self.pool_root + 0x220 + index * 0xD0
        m.map(self.pool_root, bytes(0x220 + index * 0xD0 + 0x30))
        m.u32(self.pool_root + 0x20 + (index >> 5) * 4, 1 << (index & 31))
        stats_pointers = m.alloc(16, struct.pack('<QQ', *(p | 1 for p in self.stats)))
        m.write(self.pool + 8, struct.pack('<QI', stats_pointers, 2))
        m.map(m.image_base + SELECTOR_VTABLE_RVA + 0x88, struct.pack('<Q', m.image_base + SELECTOR_GETTER_RVA))
        self.descriptors = [self.descriptor('12' * 16, 0x10, 11, True),
                            self.descriptor('34' * 16, 0x20, 22, False)]
        event = bytearray(0x48)
        struct.pack_into('<QQQQ', event, 0, f.soldier_killer, f.soldier_victim, *self.descriptors)
        struct.pack_into('<IIII', event, 0x20, *self.common_ids, 0x1000, 0x2000)
        struct.pack_into('<dIII', event, 0x30, self.report.rows[0]['game_time_seconds'] if count else 60.099998474121094, 2, 1, 3)
        self.event_address = m.alloc(max(count, 1) * 0x48, bytes(event) * count)
        self.groups_address = m.alloc(0x14, struct.pack('<IQII', 0x2000, self.event_address, count, count))
        m.write(self.stats[1] + 0x110, struct.pack('<I4xQ', 0xA8000001, self.groups_address))
        m.iter_readable_regions = lambda: (_ for _ in ()).throw(AssertionError('heap scan prohibited'))
        self.reader = NativeStatsReader(m)

    def descriptor(self, guid, token, label, ace):
        m = self.memory
        descriptor, selector, ui = m.alloc(0x70), m.alloc(0x38), m.alloc(0x80)
        m.u64(descriptor, m.image_base + DESCRIPTOR_VTABLE_RVA)
        m.write(descriptor + 0x18, bytes.fromhex(guid));m.u32(descriptor + 0x34, 8)
        table = m.alloc(0x24 * 8);m.u64(table + 0x23 * 8, selector);m.u64(descriptor + 0x68, table)
        m.u64(selector, m.image_base + SELECTOR_VTABLE_RVA);m.u64(selector + 0x30, ui)
        m.u64(ui, m.image_base + UI_DESCRIPTOR_VTABLE_RVA);m.u64(ui + 0x48, token)
        m.u32(ui + 0x6C, label);m.write(ui + 0x7C, bytes([int(ace)]))
        return descriptor

    def poll(self):return self.reader.poll(self.session, process_key='stats-fixture')


class NativeStatsTests(unittest.TestCase):
    def test_fixed_native_registries_decode_only_f110_and_bind_exact_report(self):
        f = StatsFixture();snapshot = f.poll()
        self.assertEqual(len(snapshot.events), 2)
        self.assertEqual(snapshot.events[0]['victim']['unit_guid'], '12' * 16)
        self.assertEqual(snapshot.events[0]['killer']['unit_guid'], '34' * 16)
        self.assertEqual(snapshot.events[0]['killer']['unit_common_id'], 0x2000)
        report = f.reader.bind_timeline(f.report, f.session)
        self.assertEqual(report.metadata['report_data'], {'identity_source': 'native_stats_history',
                         'identity_status': 'complete', 'matched_rows': 2, 'total_rows': 2})
        self.assertEqual([r['row_index'] for r in report.rows], [0, 1])
        for row in report.rows:
            self.assertEqual(row['victim']['unit_guid'], '12' * 16)
            self.assertEqual(row['killer']['unit_guid'], '34' * 16)
            self.assertEqual(row['killer']['player_user_id'], f.report.players[1]['player_user_id'])
            self.assertEqual(row['victim']['identity_status'], 'resolved_native_stats')
        self.assertIsNone(f.report.rows[0]['victim']['unit_guid'])
        encoded = json.dumps(snapshot.to_dict())
        self.assertNotIn('address', encoded);self.assertNotIn('pointer', encoded)
        self.assertTrue(all('_stats' not in player for player in snapshot.to_dict()['players']))
        # Other perspectives are all zero/unmapped; reading or merging them
        # would fail the fixture. Official totals do not synthesize events.
        self.assertFalse(any(a == f.stats[0] + 0x120 or a == f.stats[1] + 0x150 for a, _ in f.memory.reads))

    def test_same_projection_different_guid_is_ambiguous_without_order_pairing(self):
        f = StatsFixture()
        other = f.descriptor('56' * 16, 0x10, 11, True)
        f.memory.u64(f.event_address + 0x48 + 0x10, other)
        f.poll();report = f.reader.bind_timeline(f.report, f.session)
        self.assertEqual(report.metadata['report_data']['identity_status'], 'ambiguous')
        self.assertTrue(all(r['victim']['unit_guid'] is None for r in report.rows))
        self.assertTrue(all(r['killer']['unit_guid'] == '34' * 16 for r in report.rows))

    def test_nearby_time_and_incomplete_duplicate_multiplicity_never_bind(self):
        f = StatsFixture();snapshot = f.poll()
        changed = deepcopy(f.report.rows)
        for row in changed:row['game_time_seconds'] += 0.0000001
        self.assertTrue(all(r['killer']['unit_guid'] is None for r in bind_timeline(replace(f.report, rows=changed), snapshot).rows))
        smaller = replace(snapshot, events=snapshot.events[:1])
        self.assertTrue(all(r['killer']['unit_guid'] is None for r in bind_timeline(f.report, smaller).rows))

    def test_mutation_during_copy_rejected_and_prior_stable_cache_survives(self):
        f = StatsFixture();previous = f.poll();calls = 0
        def change(address, size):
            nonlocal calls
            if address == f.event_address:
                calls += 1
                if calls == 2:f.memory.write(address + 0x30, struct.pack('<d', 61.0))
        f.memory.before_read = change
        with self.assertRaises(NativeReportError):f.poll()
        self.assertIs(f.reader.snapshot, previous)

    def test_official_unit_counts_are_read_and_attached_to_the_roster(self):
        # Stats +0x20 / +0x24 are the official kill/loss unit counts; the point
        # totals come from a different pair (+0x2c / +0x28).
        f = StatsFixture()
        f.memory.u32(f.stats[0] + 0x20, 2)
        f.memory.u32(f.stats[0] + 0x24, 4)
        f.memory.u32(f.stats[1] + 0x20, 5)
        f.memory.u32(f.stats[1] + 0x24, 3)
        snapshot = f.poll()
        for person in snapshot.players:
            self.assertIsInstance(person['official_kill_unit_count'], int)
            self.assertIsInstance(person['official_loss_unit_count'], int)
        report = f.reader.bind_timeline(f.report, f.session)
        self.assertEqual(report.metadata['official_unit_counts_attached'], 2)
        counts = sorted((p['name'], p['official_kill_unit_count'], p['official_loss_unit_count'])
                        for p in report.players)
        self.assertIn((report.players[0]['name'], 2, 4), counts)
        self.assertIn((report.players[1]['name'], 5, 3), counts)

    def test_owner_module_and_typed_registry_roundtrips_are_required(self):
        for location in ('entity', 'module', 'pool', 'event'):
            with self.subTest(location=location):
                f = StatsFixture()
                if location == 'entity':f.memory.u32(f.entities[0] + 0x30, 0x21E)
                elif location == 'module':f.memory.u32(f.teams[1] + 8, 0x21E)
                elif location == 'pool':f.memory.u64(f.pool + 8, f.reference_array)
                else:f.memory.u32(f.event_address + 0x24, 777)
                with self.assertRaises(NativeReportError):f.poll()
                self.assertIsNone(f.reader.snapshot)

    def test_container_counts_rejected_before_unbounded_payload_read(self):
        for location, value in ((0x40, MAX_PARTICIPANTS + 1), (0x110, MAX_EVENTS + 1)):
            with self.subTest(location=location):
                f = StatsFixture()
                f.memory.u32((f.source if location == 0x40 else f.stats[1]) + location, value)
                with self.assertRaises(NativeReportError):f.poll()
                self.assertIsNone(f.reader.snapshot)

    def test_scene_epoch_changes_clear_cache_and_never_bind_old_material(self):
        f = StatsFixture();f.poll()
        changed = replace(f.session, context_epoch='f' * 64)
        self.assertIs(f.reader.bind_timeline(f.report, changed), f.report)
        with self.assertRaisesRegex(NativeReportError, 'native_stats_session_mismatch'):
            f.reader.poll(changed, process_key='stats-fixture')
        self.assertIsNone(f.reader.snapshot)

    def test_smaller_stable_history_does_not_replace_same_epoch_complete_cache(self):
        f = StatsFixture();previous = f.poll()
        f.memory.u32(f.groups_address + 12, 1)
        current = f.poll()
        self.assertIs(current, previous)
        self.assertEqual(len(current.events), 2)

    def test_current_native_participant_source_replacement_clears_larger_cache(self):
        f = StatsFixture();previous = f.poll()
        replacement = f.memory.alloc(0x80, f.memory.read(f.source, 0x80))
        f.memory.u64(f.memory.image_base + PARTICIPANT_MANAGER_RVA, replacement)
        f.memory.u32(f.groups_address + 12, 1)
        current = f.poll()
        self.assertIsNot(current, previous)
        self.assertEqual(len(current.events), 1)

    def test_session_changes_during_history_copy_cannot_publish_cached_guids(self):
        f = StatsFixture();f.poll();changed = False
        def change(address, size):
            nonlocal changed
            if address == f.event_address and not changed:
                changed = True
                replacement = f.session_fixture.narrow('different-session')
                f.memory.u64(f.session_fixture.common_data + 8, replacement)
        f.memory.before_read = change
        with self.assertRaisesRegex(NativeReportError, 'native_stats_session_mismatch'):f.poll()
        self.assertIsNone(f.reader.snapshot)

    def test_no_snapshot_preserves_dict_and_independent_player_identity(self):
        f = StatsFixture();value = f.report.to_dict()
        value['rows'][0]['victim']['player_user_id'] = '1234567890'
        self.assertIs(bind_timeline(value, None), value)
        f.poll();result = f.reader.bind_timeline(value, f.session)
        self.assertEqual(result['rows'][0]['victim']['player_user_id'], '1234567890')
        self.assertEqual(result['metadata']['report_data']['matched_rows'], 2)

    def test_unknown_selector_keeps_guid_local_but_does_not_guess_projection(self):
        f = StatsFixture()
        descriptor = f.descriptors[0]
        table = struct.unpack('<Q', f.memory.read(descriptor + 0x68, 8))[0]
        selector = struct.unpack('<Q', f.memory.read(table + 0x23 * 8, 8))[0]
        f.memory.u64(selector, f.memory.image_base + 0x123456)
        snapshot = f.poll()
        self.assertEqual(snapshot.events[0]['victim']['unit_guid'], '12' * 16)
        result = f.reader.bind_timeline(f.report, f.session)
        self.assertTrue(all(r['victim']['unit_guid'] is None for r in result.rows))
        self.assertTrue(all(r['killer']['unit_guid'] is None for r in result.rows))
        self.assertEqual(result.metadata['report_data']['matched_rows'], 0)
        self.assertEqual(result.metadata['report_data']['identity_status'], 'unavailable')

    def test_unsupported_executable_and_none_session_fail_closed(self):
        f = StatsFixture();f.memory.exe_sha256 = '0' * 64
        with self.assertRaises(UnsupportedBuildError):NativeStatsReader(f.memory)
        f = StatsFixture();f.poll();self.assertIsNone(f.reader.poll(None))
        self.assertIsNone(f.reader.snapshot)

    def test_total_operation_budget_stops_dictionary_and_text_reads_too(self):
        f = StatsFixture()
        with patch('companion.native_stats.MAX_OPERATIONS', 1):
            with self.assertRaisesRegex(NativeReportError, 'native_stats_budget_exceeded'):f.poll()
        self.assertIsNone(f.reader.snapshot)


if __name__ == '__main__':unittest.main()
