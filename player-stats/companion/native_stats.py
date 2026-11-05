"""Bounded, read-only identity material from the report builder's Stats+110.

The fixed current-participant list and typed registries are the same sources
used by build168061's normal result builder. No heap scan, native method call,
other history perspective, missing-event reconstruction or name-to-GUID lookup
occurs. CommonIDs are retained as native event evidence, not promised to be
globally unique or never reused. Public snapshots never contain addresses.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import math
import struct
import time

from .native_report import NativeReportError, NativeReportReader, UnstableSnapshotError
from .native_session import NativeSessionReader

PARTICIPANT_MANAGER_RVA = 0x02B03618
ENTITY_REGISTRY_RVA = 0x02B04620
MODULE_REGISTRY_RVA = 0x02B08F80
ENTITY_VTABLE_RVA = 0x01C70150
STATS_VTABLE_RVA = 0x01CFD7A8
TEAM_VTABLE_RVA = 0x01D061C8
DESCRIPTOR_VTABLE_RVA = 0x01C6FFB8
SELECTOR_VTABLE_RVA = 0x01C74138
SELECTOR_GETTER_RVA = 0x006AC870
UI_DESCRIPTOR_VTABLE_RVA = 0x01D4DFF0
STATS_KIND, POOL_SELECTOR = 0x97, 2
COUNT_MASK = 0x07FFFFFF
INVALID = 0xFFFFFFFF
MAX_PARTICIPANTS = 128
MAX_MODULES = 256
MAX_POOL_SLOTS = 1_000_000
MAX_EVENTS = 20_000
MAX_OPERATIONS = 100_000
MAX_SECONDS = 0.5


def _dict(value):
    return value.to_dict() if hasattr(value, 'to_dict') else value


def _session_key(value):
    value = _dict(value)
    if not isinstance(value, dict):return None
    if (value.get('game_build') != 168061 or not value.get('session_id') or not value.get('context_epoch')
            or (value.get('source_kind'), value.get('is_replay')) not in (('network_live', False), ('replay', True))):
        return None
    return (value['session_id'], value['context_epoch'], value['source_kind'], value['is_replay'],
            value.get('map'), value.get('game_build'), value.get('participants'))


@dataclass(frozen=True)
class NativeStatsSnapshot:
    session_id: str
    context_epoch: str
    game_build: int
    events: list[dict]
    players: list[dict]
    source: str = 'native_stats_history_f110'

    def to_dict(self):return asdict(self)


class NativeStatsReader:
    def __init__(self, memory):
        self._reader = NativeReportReader(memory)  # exact executable gate
        self._raw_read = self._reader.read
        self._reader.read = self._read
        self._session_reader = NativeSessionReader(memory)
        self.memory, self.image_base = memory, self._reader.image_base
        self.status = 'not_polled'
        self.snapshot = None
        self._key = None
        self._world_key = None
        self._operations = 0
        self._deadline = 0.0

    def reset(self):
        self.snapshot = None
        self._key = None
        self._world_key = None
        self._reader._strings = {}
        self._reader._translations = {}
        self._reader._dictionary = None

    def _read(self, address, size):
        self._operations += 1
        if self._operations > MAX_OPERATIONS or time.monotonic() > self._deadline:
            raise NativeReportError('native_stats_budget_exceeded')
        return self._raw_read(address, size)

    def _u32(self, address):return struct.unpack('<I', self._read(address, 4))[0]
    def _u64(self, address):return struct.unpack('<Q', self._read(address, 8))[0]

    def _entity(self, reference):
        if reference == INVALID:raise NativeReportError('invalid_native_stats_reference')
        slot = self.image_base + ENTITY_REGISTRY_RVA + (reference & 255) * 0x90
        vector = slot + (0x30 if reference >> 31 else 0x20)
        pointer, count, capacity = struct.unpack('<QII', self._read(vector, 16))
        index = (reference >> 8) & 0x7FFFFF
        if not index < count <= capacity <= MAX_POOL_SLOTS:
            raise NativeReportError('invalid_native_stats_registry')
        entity = self._u64(pointer + index * 8)
        if (self._u64(entity) != self.image_base + ENTITY_VTABLE_RVA
                or self._u32(entity + 0x30) != reference):
            raise NativeReportError('invalid_native_stats_entity_binding')
        return entity

    def _pool_stats(self, reference):
        index = POOL_SELECTOR * 255 + STATS_KIND
        root = self.image_base + MODULE_REGISTRY_RVA
        if not self._u32(root + 0x20 + (index >> 5) * 4) & (1 << (index & 31)):
            raise NativeReportError('native_stats_pool_unavailable')
        pool = root + 0x220 + index * 0xD0
        offset = 0x18 if reference >> 31 else 8
        pointer = self._u64(pool + offset)
        count = self._u32(pool + offset + 8)
        element = (reference >> 8) & 0x7FFFFF
        if not element < count <= MAX_POOL_SLOTS:
            raise NativeReportError('invalid_native_stats_pool')
        flagged = self._u64(pointer + element * 8)
        if not flagged & 1:raise NativeReportError('invalid_native_stats_pool_presence')
        return flagged & ~3

    def _player(self, reference):
        entity = self._entity(reference)
        count = self._u32(entity + 0x10) & COUNT_MASK
        if not 1 <= count <= MAX_MODULES:raise NativeReportError('invalid_native_stats_modules')
        entries = self._read(self._u64(entity + 0x18), count * 16)
        found = {}
        for offset in range(0, len(entries), 16):
            pointer = struct.unpack_from('<Q', entries, offset + 8)[0]
            if not pointer:continue
            vtable = self._u64(pointer)
            if vtable not in (self.image_base + STATS_VTABLE_RVA, self.image_base + TEAM_VTABLE_RVA):continue
            if vtable in found:raise NativeReportError('duplicate_native_stats_module')
            if self._u32(pointer + 8) != reference:raise NativeReportError('invalid_native_stats_module_owner')
            found[vtable] = pointer
        stats, team = (found.get(self.image_base + value) for value in (STATS_VTABLE_RVA, TEAM_VTABLE_RVA))
        if not stats or not team or self._pool_stats(reference) != stats:
            raise NativeReportError('native_stats_module_binding_mismatch')
        alliance = self._u32(team + 0x10)
        if alliance not in (0, 1):raise NativeReportError('unsupported_native_stats_alliance')
        # +0x20 / +0x24 are the official per-player kill and loss unit counts
        # (the point totals at +0x2c / +0x28 are a different pair of fields).
        kill_units, loss_units = self._u32(stats + 0x20), self._u32(stats + 0x24)
        if kill_units > MAX_EVENTS or loss_units > MAX_EVENTS:
            raise NativeReportError('native_stats_count_limit')
        return {'common_id': self._u32(entity + 8), 'source_rank': self._u32(team + 0x30),
                'name': self._reader._wstring(self._u64(team + 0x38)), 'alliance': alliance,
                'official_kill_unit_count': kill_units, 'official_loss_unit_count': loss_units,
                '_stats': stats}

    def _descriptor(self, pointer):
        if pointer in self._descriptors:return self._descriptors[pointer]
        if self._u64(pointer) != self.image_base + DESCRIPTOR_VTABLE_RVA:
            raise NativeReportError('unsupported_native_stats_descriptor')
        guid = self._read(pointer + 0x18, 16).hex()
        if guid == '0' * 32:raise NativeReportError('invalid_native_stats_guid')
        projection = None
        # The builder calls selector.virtual88(edx=0). For this exact known
        # selector and a non-null Default, static code returns [selector+30].
        # We only copy that branch; no method is executed or interpreted.
        if self._u32(pointer + 0x34) & (1 << 3):
            selector = self._u64(self._u64(pointer + 0x68) + 0x23 * 8)
            vtable = self._u64(selector)
            if (vtable == self.image_base + SELECTOR_VTABLE_RVA
                    and self._u64(vtable + 0x88) == self.image_base + SELECTOR_GETTER_RVA):
                ui = self._u64(selector + 0x30)
                if ui and self._u64(ui) == self.image_base + UI_DESCRIPTOR_VTABLE_RVA:
                    projection = {'unit_name': self._reader._unit_name(self._u64(ui + 0x48)),
                                  'unit_label_texture_token': self._u32(ui + 0x6C),
                                  'is_ace': bool(self._read(ui + 0x7C, 1)[0] & 1)}
        self._descriptors[pointer] = (guid, projection)
        return guid, projection

    def _event(self, raw, group, player, owners):
        victim_name, killer_name = self._reader._wstring(struct.unpack_from('<Q', raw, 8)[0]), self._reader._wstring(struct.unpack_from('<Q', raw)[0])
        victim_desc, killer_desc = struct.unpack_from('<QQ', raw, 0x10)
        victim_owner, killer_owner, victim_id, killer_id = struct.unpack_from('<IIII', raw, 0x20)
        seconds, victim_level, killer_level, reason = struct.unpack_from('<dIII', raw, 0x30)
        if (not math.isfinite(seconds) or not 0 <= seconds <= self._reader.profile.max_game_time_seconds
                or killer_owner != player['common_id'] or killer_id != group
                or killer_id == INVALID or victim_id == INVALID):
            raise NativeReportError('invalid_native_stats_event_identity')
        result = {'game_time_seconds': seconds, 'reason': reason}
        for side, descriptor, owner, unit_id, level, soldier in (
                ('killer', killer_desc, killer_owner, killer_id, killer_level, killer_name),
                ('victim', victim_desc, victim_owner, victim_id, victim_level, victim_name)):
            guid, projection = self._descriptor(descriptor)
            owner_row = owners.get(owner)
            if projection is not None and owner_row is not None:
                projection = {**projection, 'player_name': owner_row['name'], 'soldier_name': soldier,
                              'display_veterancy': level}
            else:projection = None
            result[side] = {'unit_guid': guid, 'owner_common_id': owner, 'unit_common_id': unit_id,
                            'display_veterancy': level, 'soldier_name': soldier, 'projection': projection}
        return result

    def poll(self, session, *, process_key=''):
        key = _session_key(session)
        if key is None:
            self.reset();self.status = 'native_stats_waiting_for_session';return None
        if key != self._key:self.reset();self._key = deepcopy(key)
        try:
            before = self._session_reader.poll(process_key=process_key)
            if _session_key(before) != key:raise NativeReportError('native_stats_session_mismatch')
            self._reader._begin()
            self._reader._observations = {}
            self._reader._strings = {};self._reader._translations = {};self._reader._dictionary = None
            self._descriptors = {}
            self._operations = 0;self._deadline = time.monotonic() + MAX_SECONDS
            source = self._u64(self.image_base + PARTICIPANT_MANAGER_RVA)
            if not source:
                self.status = 'native_stats_waiting_for_participants';return None
            count = self._u32(source + 0x40)
            if not 1 <= count <= MAX_PARTICIPANTS:raise NativeReportError('native_stats_participant_limit')
            references = struct.unpack('<' + 'I' * count, self._read(self._u64(source + 0x38), count * 4))
            excluded = {self._u32(source + 0x70), self._u32(source + 0x74)}
            selected = [value for value in references if value not in excluded]
            if not selected or len(selected) != len(set(selected)):
                raise NativeReportError('invalid_native_stats_participants')
            players = [self._player(reference) for reference in selected]
            owners = {p['common_id']: p for p in players}
            if len(owners) != len(players):raise NativeReportError('duplicate_native_stats_owner')
            world_key = (source, tuple((ref, p['common_id'], p['_stats']) for ref, p in zip(selected, players)))
            if self._world_key is not None and self._world_key != world_key:
                self.snapshot = None
            self._world_key = world_key
            # Same native roster relation as the published UI. Name/alliance is
            # corroboration only; it is never used as an entity or GUID locator.
            expected = sorted((p['name'], p['alliance']) for p in _dict(session)['participants'])
            if sorted((p['name'], p['alliance']) for p in players) != expected:
                raise NativeReportError('native_stats_roster_mismatch')
            events = []
            for player in players:
                stats = player['_stats']
                groups = self._u32(stats + 0x110) & COUNT_MASK
                if groups > MAX_EVENTS:raise NativeReportError('native_stats_history_limit')
                entries = self._read(self._u64(stats + 0x118), groups * 0x14) if groups else b''
                group_ids = set()
                for offset in range(0, len(entries), 0x14):
                    group, pointer, length, capacity = struct.unpack_from('<IQII', entries, offset)
                    if group in group_ids or not length <= capacity <= MAX_EVENTS or len(events) + length > MAX_EVENTS:
                        raise NativeReportError('native_stats_history_limit')
                    group_ids.add(group)
                    rows = self._read(pointer, length * 0x48) if length else b''
                    for pos in range(0, len(rows), 0x48):events.append(self._event(rows[pos:pos + 0x48], group, player, owners))
            observations = self._reader._observations;self._reader._observations = None
            for (address, size), expected_bytes in observations.items():
                if self._read(address, size) != expected_bytes:raise UnstableSnapshotError()
            after = self._session_reader.poll(process_key=process_key)
            if _session_key(after) != key:raise NativeReportError('native_stats_session_mismatch')
            snapshot = NativeStatsSnapshot(key[0], key[1], 168061, events,
                                           [{k: v for k, v in p.items() if not k.startswith('_')} for p in players])
            if self.snapshot is None or len(snapshot.events) >= len(self.snapshot.events):
                self.snapshot = snapshot
            self.status = 'native_stats_read'
            return self.snapshot
        except NativeReportError as exc:
            self.status = exc.code
            if exc.code == 'native_stats_session_mismatch':self.reset()
            raise
        finally:self._reader._observations = None

    def bind_timeline(self, report, session):
        return bind_timeline(report, self.snapshot if _session_key(session) == self._key else None)


_PROJECTION_FIELDS = ('player_name', 'unit_name', 'soldier_name', 'unit_label_texture_token', 'display_veterancy', 'is_ace')


def _projection_key(seconds, killer, victim):
    # Compare the exact original f64 representation and exact copied native
    # display fields. XP image/player color are not type IDs and are not read
    # from mutable UI style managers. Conflicting same-projection GUIDs remain
    # ambiguous; no nearest-time, name-to-price, or row-position pairing occurs.
    return (struct.pack('<d', seconds), *(tuple(a.get(k) for k in _PROJECTION_FIELDS) for a in (killer, victim)))


def bind_timeline(report, snapshot):
    if snapshot is None:return report
    is_mapping = isinstance(report, dict)
    rows = deepcopy(report['rows'] if is_mapping else report.rows)
    build = report.get('game_build') if is_mapping else report.game_build
    players = deepcopy((report.get('players') if is_mapping else report.players) or [])
    # Official per-player unit counts (Stats +0x20/+0x24) are attached only when
    # one snapshot participant matches this roster entry by name and alliance.
    counts, duplicated = {}, set()
    for person in snapshot.players or []:
        key = (person.get('name'), person.get('alliance'))
        if key in counts:duplicated.add(key)
        counts[key] = (person.get('official_kill_unit_count'), person.get('official_loss_unit_count'))
    attached = 0
    for entry in players:
        key = (entry.get('name'), entry.get('alliance'))
        if key in counts and key not in duplicated:
            entry['official_kill_unit_count'], entry['official_loss_unit_count'] = counts[key]
            attached += 1
    candidates = defaultdict(list)
    if snapshot.game_build == build:
        for event in snapshot.events:
            killer, victim = event['killer']['projection'], event['victim']['projection']
            if killer is not None and victim is not None:
                candidates[_projection_key(event['game_time_seconds'], killer, victim)].append(event)
    roster = defaultdict(list)
    for p in players:
        if p.get('player_user_id') is not None:roster[p['name']].append(p['player_user_id'])
    by_key = defaultdict(list)
    for row in rows:by_key[_projection_key(row['game_time_seconds'], row['killer'], row['victim'])].append(row)
    matched = 0;ambiguous = False;any_resolved = False
    for key, grouped_rows in by_key.items():
        events = candidates.get(key, [])
        # Multiplicity is evidence: an incomplete cache cannot identify all
        # identical UI rows. Same-projection distinct GUIDs are never assigned
        # arbitrarily by order, even when counts agree.
        usable = len(events) == len(grouped_rows)
        for row in grouped_rows:
            resolved = True
            for side in ('killer', 'victim'):
                actor = row[side]
                values = {e[side]['unit_guid'] for e in events} if usable else set()
                status = 'resolved_native_stats' if len(values) == 1 else 'ambiguous' if len(values) > 1 else 'unresolved'
                actor['unit_guid'] = next(iter(values)) if len(values) == 1 else None
                if actor.get('player_user_id') is None:
                    actor['player_user_id'] = roster[actor['player_name']][0] if len(roster[actor['player_name']]) == 1 else None
                actor['identity_status'] = status
                resolved &= status == 'resolved_native_stats'
                any_resolved |= status == 'resolved_native_stats'
                ambiguous |= status == 'ambiguous'
            matched += int(resolved)
    status = ('complete' if matched == len(rows) and snapshot is not None else 'ambiguous' if ambiguous
              else 'partial' if any_resolved else 'unavailable')
    metadata = deepcopy(report.get('metadata', {}) if is_mapping else report.metadata)
    metadata['report_data'] = {'identity_source': 'native_stats_history' if snapshot is not None else 'unavailable',
                               'identity_status': status, 'matched_rows': matched, 'total_rows': len(rows)}
    metadata['official_unit_counts_attached'] = attached
    return {**report, 'players': players, 'rows': rows, 'metadata': metadata} if is_mapping else \
        replace(report, players=players, rows=rows, metadata=metadata)
