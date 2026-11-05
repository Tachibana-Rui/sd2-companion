"""Read a supported SD2 runtime session without opening a replay file.

Only the statically verified build's LaunchMapHelper/GameSetup and network
manager/source chain is accepted. A source is classified positively as network
or replay; an unfamiliar source is never treated as a live multiplayer game.
All reads are bounded and repeated before publishing a snapshot. No native
method is called and no game memory is written.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import struct
import unicodedata

from .native_report import NativeReportError, NativeReportReader, UnstableSnapshotError


LAUNCH_MAP_HELPER_RVA = 0x02B03638
LAUNCH_MAP_HELPER_VTABLE_RVA = 0x01DAB558
RUNTIME_CONTEXT_RVA = 0x02B02C60
NETWORK_MANAGER_VTABLE_RVAS = (0x01CE00E8, 0x01C74868)
NETWORK_SOURCE_VTABLE_RVA = 0x01CE07F0
REPLAY_SOURCE_VTABLE_RVA = 0x01CE0808
COUNT_MASK = 0x07FFFFFF
GAME_BUILD = 168061

# These are defensive program budgets, not claims about the game's limits.
MAX_STRING_BYTES = 16_384
MAX_COMMON_SETTINGS = 256
MAX_PLAYERS = 64
MAX_PLAYER_SETTINGS = 128
COMMON_KEYS = frozenset(("UniqueSessionId", "Map", "Version", "ServerName"))
PLAYER_KEYS = frozenset(("PlayerUserId", "PlayerName", "PlayerAlliance", "PlayerObserver"))


@dataclass(frozen=True)
class NativeSessionSnapshot:
    session_id: str
    map: str
    game_build: int
    server_name: str
    participants: list[dict]
    is_replay: bool
    source_kind: str
    game_ended: bool
    context_epoch: str

    def to_dict(self):
        """Public metadata only: no pointers, decks, passwords or game handles."""
        return asdict(self)


def _pointer(value):
    return type(value) is int and 0x10000 <= value < 0x0000800000000000


def _text(value, maximum, *, nonempty=False, forbid_format=False):
    if (not isinstance(value, str) or len(value) > maximum or (nonempty and not value)
            or any(unicodedata.category(char) in ("Cc", "Cs")
                   or forbid_format and unicodedata.category(char) == "Cf" for char in value)):
        raise NativeReportError("invalid_native_session_text")
    return value


def _decimal(value, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise NativeReportError("invalid_native_session_integer")
    result = int(value)
    if result > maximum:
        raise NativeReportError("invalid_native_session_integer")
    return result


class NativeSessionReader:
    def __init__(self, memory):
        # Reuse the established fingerprint gate, bounded reads and transient
        # byte-observation collection. This reader never calls report.poll().
        self._reader = NativeReportReader(memory)
        self.memory = memory
        self.image_base = self._reader.image_base
        self.status = "not_polled"
        self._strings = {}

    def _read(self, address, size):
        return self._reader.read(address, size)

    def _u64(self, address):
        return struct.unpack("<Q", self._read(address, 8))[0]

    def _u8(self, address):
        return self._read(address, 1)[0]

    def _required_pointer(self, value):
        if not _pointer(value):
            raise NativeReportError("invalid_native_session_pointer")
        return value

    def _cstring(self, handle):
        if handle == 0:
            return ""
        self._required_pointer(handle)
        if handle in self._strings:
            return self._strings[handle]
        # This is TEugString<char>, not the task report's wide string. The
        # refcount at +0 is intentionally excluded because it changes normally.
        header = self._read(handle + 0x10, 0x20)
        length, capacity = struct.unpack_from("<QQ", header, 0x10)
        if length > capacity or length > MAX_STRING_BYTES or capacity > MAX_STRING_BYTES:
            raise NativeReportError("invalid_native_session_string_size")
        if capacity < 16:
            if length >= 16 or header[length] != 0:
                raise NativeReportError("invalid_native_session_string_layout")
            encoded = header[:length]
        else:
            payload = self._required_pointer(struct.unpack_from("<Q", header)[0])
            encoded = self._read(payload, length + 1)
            if encoded[-1] != 0:
                raise NativeReportError("invalid_native_session_string_layout")
            encoded = encoded[:-1]
        try:
            value = encoded.decode("utf-8", "strict")
        except UnicodeError:
            raise NativeReportError("invalid_native_session_utf8") from None
        self._strings[handle] = value
        return value

    def _dense(self, address, stride, maximum):
        header = self._read(address, 0x10)
        count = struct.unpack_from("<I", header)[0] & COUNT_MASK
        pointer = struct.unpack_from("<Q", header, 8)[0]
        if count > maximum:
            raise NativeReportError("native_session_container_limit")
        if count == 0:
            return b"", 0
        self._required_pointer(pointer)
        return self._read(pointer, count * stride), count

    def _settings(self, address, maximum, allowed):
        return self._settings_from_header(self._read(address, 0x10), maximum, allowed)

    def _participants(self, setup):
        entries, count = self._dense(setup, 0x18, MAX_PLAYERS)
        result = []
        slots = set()
        identities = set()
        for index in range(count):
            offset = index * 0x18
            slot = struct.unpack_from("<I", entries, offset)[0]
            if slot in slots:
                raise NativeReportError("duplicate_native_session_slot")
            slots.add(slot)
            # The inline map header is part of the copied native entries; the
            # final second read checks it together with the entire player map.
            player_header = entries[offset + 8:offset + 0x18]
            fields = self._settings_from_header(player_header, MAX_PLAYER_SETTINGS, PLAYER_KEYS)
            observer = _decimal(fields.get("PlayerObserver", "0"), 1)
            if observer:
                continue
            identifier = str(_decimal(fields.get("PlayerUserId"), 2**64 - 2))
            alliance = _decimal(fields.get("PlayerAlliance"), 1)
            if identifier in identities:
                raise NativeReportError("duplicate_native_session_player_id")
            identities.add(identifier)
            result.append({"id": identifier, "name": _text(fields.get("PlayerName", ""), 256),
                           "alliance": alliance})
        return result

    def _settings_from_header(self, header, maximum, allowed):
        count = struct.unpack_from("<I", header)[0] & COUNT_MASK
        pointer = struct.unpack_from("<Q", header, 8)[0]
        if count > maximum:
            raise NativeReportError("native_session_container_limit")
        if not count:
            return {}
        self._required_pointer(pointer)
        entries = self._read(pointer, count * 16)
        result = {}
        for index in range(count):
            key_handle, value_handle = struct.unpack_from("<QQ", entries, index * 16)
            key = self._cstring(key_handle)
            if key not in allowed:
                # Do not dereference values such as Password or deck content.
                continue
            if key in result:
                raise NativeReportError("duplicate_native_session_key")
            result[key] = self._cstring(value_handle)
        return result

    def _validate_snapshot(self):
        observations = self._reader._observations
        self._reader._observations = None
        for (address, size), expected in observations.items():
            if self._read(address, size) != expected:
                raise UnstableSnapshotError()

    def poll(self, process_key=""):
        if not isinstance(process_key, str) or len(process_key) > 256:
            raise NativeReportError("invalid_native_session_process_key")
        self._reader._begin()
        self._reader._observations = {}
        self._strings = {}
        self.status = "reading_native_session"
        try:
            owner = self._u64(self.image_base + LAUNCH_MAP_HELPER_RVA)
            context = self._u64(self.image_base + RUNTIME_CONTEXT_RVA)
            if owner == 0 or context == 0:
                self._validate_snapshot()
                self.status = "waiting_for_native_session"
                return None
            self._required_pointer(owner)
            self._required_pointer(context)
            if self._u64(owner) != self.image_base + LAUNCH_MAP_HELPER_VTABLE_RVA:
                raise NativeReportError("unsupported_native_session_owner")
            setup = self._u64(owner + 0x78)
            manager = self._u64(context + 0xB0)
            if setup == 0 or manager == 0:
                self._validate_snapshot()
                self.status = "waiting_for_native_session"
                return None
            self._required_pointer(setup)
            self._required_pointer(manager)
            if self._u64(manager) not in tuple(self.image_base + rva for rva in NETWORK_MANAGER_VTABLE_RVAS):
                raise NativeReportError("unsupported_native_session_manager")
            if self._u64(manager + 0xE0) != setup:
                raise NativeReportError("native_session_setup_mismatch")
            source = self._required_pointer(self._u64(manager + 0x60))
            source_vtable = self._u64(source)
            is_replay = self._u8(manager + 0x75)
            game_ended = self._u8(context + 0x59)
            if is_replay not in (0, 1) or game_ended not in (0, 1):
                raise NativeReportError("invalid_native_session_flags")
            if source_vtable == self.image_base + NETWORK_SOURCE_VTABLE_RVA and is_replay == 0:
                source_kind = "network_live"
            elif source_vtable == self.image_base + REPLAY_SOURCE_VTABLE_RVA and is_replay == 1:
                source_kind = "replay"
            else:
                raise NativeReportError("native_session_source_unknown")
            common = self._settings(setup + 0x10, MAX_COMMON_SETTINGS, COMMON_KEYS)
            session_id = _text(common.get("UniqueSessionId", ""), 128, forbid_format=True)
            map_id = _text(common.get("Map", ""), 512)
            if not session_id or not map_id:
                self._validate_snapshot()
                self.status = "waiting_for_native_session_identity"
                return None
            # Preserve the exact opaque native ID. Normalizing a UUID only on
            # the client could break equality with an existing server archive.
            if "Version" in common and _decimal(common["Version"], 2**31 - 1) != GAME_BUILD:
                raise NativeReportError("native_session_build_mismatch")
            server_name = _text(common.get("ServerName", ""), 256)
            participants = self._participants(setup)
            immutable = {"process_key": process_key, "setup": setup, "manager": manager,
                         "session_id": session_id}
            epoch = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode("utf-8")).hexdigest()
            self._validate_snapshot()
            self.status = "native_session_read"
            return NativeSessionSnapshot(session_id, map_id, GAME_BUILD, server_name, participants,
                                         bool(is_replay), source_kind, bool(game_ended), epoch)
        except NativeReportError as exc:
            self.status = exc.code
            raise
        finally:
            self._reader._observations = None
