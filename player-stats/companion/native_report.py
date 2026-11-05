"""Read SD2's published native mission-report timeline without executing it.

This module never reconstructs StatisticsModule histories, sends game input,
injects code, or calls an address in the game. ``memory`` supplies read-only
bytes and readable private regions. Offsets are gated by the exact researched
executable SHA-256. A supported file layout is not a live-game validation claim.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import struct
from typing import Any, Iterable, Protocol


SUPPORTED_EXE_SHA256 = "2986600db813a24a92f59698ac1b250d4ae7a96b84f2000bc1ad5a53bbfd1cf6"
MIB = 1024 * 1024


class ReadOnlyMemory(Protocol):
    image_base: int
    exe_sha256: str

    def read(self, address: int, size: int) -> bytes: ...
    def iter_readable_regions(self) -> Iterable[tuple[int, int]]: ...


class NativeReportError(RuntimeError):
    """Safe diagnostic code; messages never include memory or player contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class UnsupportedBuildError(NativeReportError):
    def __init__(self):
        super().__init__("unsupported_executable")


class UnstableSnapshotError(NativeReportError):
    def __init__(self):
        super().__init__("unstable_native_report")


@dataclass(frozen=True)
class NativeReportProfile:
    exe_sha256: str = SUPPORTED_EXE_SHA256
    game_build: int = 168061
    controller_vtable_rva: int = 0x01E9BD58
    manager_registry_rva: int = 0x02AF5490
    manager_vtable_rva: int = 0x01E81B18
    result_interface_vtable_rva: int = 0x01EB4B40
    result_lazy_getter_rva: int = 0x015371D0
    localisation_manager_rva: int = 0x02AE58C0
    unit_dictionary_token_rva: int = 0x02AF7678
    result_size: int = 0xBE0
    viewmodel_offset: int = 0xAFC
    row_stride: int = 0xB8
    max_rows: int = 20_000
    max_text_characters: int = 16_384
    max_dictionary_entries: int = 500_000
    max_dictionary_layers: int = 128
    max_scan_regions: int = 32_768
    # Legacy limit now bounds scan I/O in one poll, never address coverage.
    # Large heaps are traversed incrementally across as many polls as needed.
    max_scan_bytes: int = 2 * 1024 * MIB
    scan_bytes_per_poll: int = 32 * MIB
    scan_chunk_bytes: int = MIB
    max_read_bytes_per_poll: int = 64 * MIB
    max_candidates: int = 64
    max_game_time_seconds: float = 7 * 24 * 3600
    # Diagnostic overrides still require the matching executable. No guessed
    # layout or user-provided event JSON is accepted as a native report.
    explicit_controller_address: int | None = None
    explicit_variant_address: int | None = None


@dataclass(frozen=True)
class NativeReport:
    generation: int
    result_address: int
    rows: list[dict[str, Any]]
    variant_address: int
    game_build: int = 168061
    kind: str = "normal"
    players: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Local object; upload callers must omit local memory addresses."""
        return asdict(self)


def _pointer(value: int) -> bool:
    return isinstance(value, int) and 0x10000 <= value < 0x0000800000000000


def native_time_display(seconds: float) -> str:
    """Match Timer's f64->f32->truncated seconds, minutes-mode formatter.

    The native caller passes both minute-mode flags, uses at least two minute
    digits and up to three, and wraps the minutes field modulo 1000.
    """
    value = struct.unpack("<f", struct.pack("<f", seconds))[0]
    whole = int(value)
    return f"{(whole // 60) % 1000:02d}:{whole % 60:02d}"


class NativeReportReader:
    def __init__(self, memory: ReadOnlyMemory, profile: NativeReportProfile | None = None):
        self.memory = memory
        self.profile = profile or NativeReportProfile()
        if (memory.exe_sha256.lower() != self.profile.exe_sha256.lower()
                or self.profile.exe_sha256 != SUPPORTED_EXE_SHA256):
            raise UnsupportedBuildError()
        if not _pointer(memory.image_base):
            raise NativeReportError("invalid_image_base")
        limits = (self.profile.max_rows, self.profile.max_text_characters,
                  self.profile.max_dictionary_entries, self.profile.max_dictionary_layers,
                  self.profile.max_scan_regions, self.profile.max_scan_bytes,
                  self.profile.scan_bytes_per_poll, self.profile.scan_chunk_bytes,
                  self.profile.max_read_bytes_per_poll, self.profile.max_candidates)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise NativeReportError("invalid_native_reader_limits")
        if (self.profile.result_size != 0xBE0 or self.profile.viewmodel_offset != 0xAFC
                or self.profile.row_stride != 0xB8 or self.profile.game_build != 168061):
            raise NativeReportError("unsupported_native_result_profile")
        if (self.profile.manager_registry_rva != 0x02AF5490 or self.profile.manager_vtable_rva != 0x01E81B18
                or self.profile.result_interface_vtable_rva != 0x01EB4B40
                or self.profile.result_lazy_getter_rva != 0x015371D0):
            raise NativeReportError("unsupported_native_result_profile")
        self.image_base = memory.image_base
        self.exe_sha256 = memory.exe_sha256
        self.status = "not_polled"
        self.baseline_ready = False
        self._controller: int | None = self.profile.explicit_controller_address
        self._variant: int | None = self.profile.explicit_variant_address
        self._regions: list[tuple[int, int]] | None = None
        self._region_index = self._region_offset = self._scan_total = 0
        self._candidates_seen = 0
        self._scan_bytes_total = self._scan_rounds_completed = 0
        self._scan_last_round_bytes = self._last_region_count = self._last_regions_completed = 0
        self._scan_bytes_planned = 0
        self._scan_tail = b""
        self._scan_block: tuple[int, bytes, int, dict[int | None, int]] | None = None
        self._bytes_read = 0
        self._observations: dict[tuple[int, int], bytes] | None = None
        self._strings: dict[int, str] = {}
        self._translations: dict[int, str] = {}
        self._dictionary: int | None = None
        self._last_delivered: tuple[int, int, int] | None = None
        self._registry_selected = False

    def diagnostics(self) -> dict[str, int | str | bool]:
        """Safe locator progress, without process addresses or report contents."""
        return {"status": self.status, "baseline_ready": self.baseline_ready,
                "scan_bytes_total": self._scan_bytes_total,
                "scan_bytes_in_round": self._scan_total,
                "scan_bytes_last_round": self._scan_last_round_bytes,
                "scan_bytes_planned": self._scan_bytes_planned,
                "scan_rounds_completed": self._scan_rounds_completed,
                "regions_total": len(self._regions) if self._regions is not None else self._last_region_count,
                "regions_completed": self._region_index if self._regions is not None else self._last_regions_completed,
                "candidates_seen": self._candidates_seen,
                "read_bytes_last_poll": self._bytes_read}

    def _begin(self) -> None:
        reset = getattr(self.memory, "reset_budget", None)
        if reset is not None:
            reset()
        self._bytes_read = 0
        self._observations = None

    def read(self, address: int, size: int) -> bytes:
        """Budgeted read, also used by the pure getter interpreter."""
        if size == 0:
            return b""
        if not _pointer(address) or size < 0 or address + size >= 0x0000800000000000:
            raise NativeReportError("invalid_memory_range")
        if self._bytes_read + size > self.profile.max_read_bytes_per_poll:
            raise NativeReportError("native_read_budget_exceeded")
        self._bytes_read += size
        try:
            value = self.memory.read(address, size)
        except Exception as exc:
            raise NativeReportError("native_memory_unreadable") from exc
        if not isinstance(value, bytes) or len(value) != size:
            raise NativeReportError("native_memory_short_read")
        if self._observations is not None:
            key = (address, size)
            previous = self._observations.setdefault(key, value)
            if previous != value:
                raise UnstableSnapshotError()
        return value

    def _u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def _u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def _read_budget_remaining(self) -> int:
        available = self.profile.max_read_bytes_per_poll - self._bytes_read
        memory_budget = getattr(self.memory, "max_read_bytes", None)
        if type(memory_budget) is int:
            available = min(available, memory_budget - getattr(self.memory, "bytes_read", 0))
        return max(0, available)

    def _get(self, this: int, slot: int) -> int:
        if not _pointer(this):
            raise NativeReportError("invalid_native_interface")
        vtable = self._u64(this)
        function = self._u64(vtable + slot)
        try:
            from .getters import resolve_getter
            result = resolve_getter(self, this, function)
        except NativeReportError:
            raise
        except Exception as exc:
            raise NativeReportError("unsupported_native_getter") from exc
        if not _pointer(result):
            raise NativeReportError("invalid_native_getter_result")
        return result

    def _result_variant(self, interface: int) -> int | None:
        """Read an already initialized lazy-result cache, never initialize it.

        The supported native virtual+0x70 is not a pure getter: its null branch
        allocates a result container. The exact interface/function identities
        let us read the existing cache at+0x48 without executing that method.
        """
        if not _pointer(interface):
            raise NativeReportError("invalid_native_interface")
        vtable = self._u64(interface)
        function = self._u64(vtable + 0x70)
        if vtable == self.image_base + self.profile.result_interface_vtable_rva:
            if function != self.image_base + self.profile.result_lazy_getter_rva:
                raise NativeReportError("unsupported_native_result_interface")
            variant = self._u64(interface + 0x48)
            if variant == 0:
                return None
            if not _pointer(variant) or self._u64(variant + 0x28) != interface:
                raise NativeReportError("invalid_native_result_owner")
            return variant
        # Preserve only the independently safe pure-getter interpretation for
        # other wrapper interfaces; a complex method is still rejected.
        return self._get(interface, 0x70)

    def _controller_variant(self, controller: int) -> int | None:
        if self._u64(controller) != self.image_base + self.profile.controller_vtable_rva:
            raise NativeReportError("native_controller_expired")
        context = self._u64(controller + 0x28)
        service = self._u64(context + 0x48)
        interface = self._get(service, 0x08)
        return self._result_variant(interface)

    def _registry_variant(self) -> tuple[bool, int | None]:
        """Return whether an exact native manager exists and its result cache."""
        try:
            root = self._u64(self.image_base + self.profile.manager_registry_rva)
        except NativeReportError as exc:
            if exc.code == "native_memory_unreadable":
                return False, None  # Registry not mapped/available yet.
            raise
        if root == 0:
            return False, None
        if not _pointer(root) or not _pointer(root + 0x1A0):
            raise NativeReportError("invalid_native_registry_root")
        container = root + 0x1A0
        vectors = self.read(container, 0x20)
        managers = set()
        for offset in (0, 0x10):
            pointer, count, capacity = struct.unpack_from("<QII", vectors, offset)
            if count > capacity or capacity > 1024 or (count and not _pointer(pointer)):
                raise NativeReportError("invalid_native_manager_registry")
            raw = self.read(pointer, count * 8) if count else b""
            for index in range(count):
                manager = struct.unpack_from("<Q", raw, index * 8)[0]
                if manager == 0 or manager in managers:
                    continue
                if not _pointer(manager):
                    raise NativeReportError("invalid_native_registry_entry")
                if self._u64(manager) == self.image_base + self.profile.manager_vtable_rva:
                    managers.add(manager)
        variants = set()
        for manager in managers:
            if self._u64(manager + 8) != container:
                raise NativeReportError("invalid_native_manager_owner")
            service = self._u64(manager + 0x48)
            interface = self._get(service, 0x08)
            variant = self._result_variant(interface)
            if variant is not None:
                self._header(variant)
            variants.add(variant)
        if len(variants) > 1:
            raise NativeReportError("ambiguous_native_report")
        return bool(managers), next(iter(variants)) if variants else None

    def _stable_registry_variant(self) -> tuple[bool, int | None]:
        """Double-read registry headers, contents and all selected owner links."""
        previous = self._observations
        observations = {}
        self._observations = observations
        try:
            outcome = self._registry_variant()
        finally:
            self._observations = previous
        for (address, size), expected in observations.items():
            if self.read(address, size) != expected:
                raise UnstableSnapshotError()
        return outcome

    def _header(self, variant: int) -> tuple[bytes, int, int, int]:
        raw = self.read(variant, 0x18)
        generation, kind, result = struct.unpack_from("<QI4xQ", raw)
        if kind not in (0, 1, 2):
            raise NativeReportError("invalid_native_result_kind")
        if kind and not _pointer(result if kind == 1 else self._u64(variant + 0x18)):
            raise NativeReportError("invalid_native_result_pointer")
        return raw, generation, kind, result

    def _locate(self) -> int | None:
        self._registry_selected = False
        self.baseline_ready = False
        if self.profile.explicit_variant_address is not None:
            self._header(self.profile.explicit_variant_address)
            self.baseline_ready = True
            return self.profile.explicit_variant_address
        if self.profile.explicit_controller_address is None:
            found, variant = self._stable_registry_variant()
            if found:
                self._registry_selected = True
                self._controller = None
                self._variant = variant
                self.baseline_ready = True
                if variant is None:
                    self.status = "waiting_for_published_report"
                return variant
        if self._controller is not None:
            try:
                variant = self._controller_variant(self._controller)
                if variant is not None:
                    self._header(variant)
                self._variant = variant
                self.baseline_ready = True
                if variant is None:
                    self.status = "waiting_for_published_report"
                return variant
            except NativeReportError:
                if self.profile.explicit_controller_address is not None:
                    raise
                self._controller = self._variant = None
                self._regions = None
                self._region_index = self._region_offset = self._scan_total = 0
                self._scan_tail = b""
                self._scan_block = None
                self.baseline_ready = False
        if self._regions is None:
            self.baseline_ready = False
            regions = []
            for region in self.memory.iter_readable_regions():
                if len(regions) >= self.profile.max_scan_regions:
                    raise NativeReportError("native_region_limit")
                address, size = region
                if _pointer(address) and size > 0:
                    regions.append((address, size))
            self._regions = regions
            self._last_region_count = len(regions)
            self._scan_bytes_planned = sum(size for _, size in regions)
            self._last_regions_completed = 0
        pattern = struct.pack("<Q", self.image_base + self.profile.controller_vtable_rva)
        remaining = min(self.profile.scan_bytes_per_poll, self.profile.max_scan_bytes)
        candidates_this_poll = 0
        # A suspended block keeps its search offset, so exhausting candidate
        # or read budgets cannot skip a genuine controller in that block.
        while True:
            if self._scan_block is None:
                while self._region_index < len(self._regions):
                    base, total = self._regions[self._region_index]
                    if self._region_offset < total:
                        break
                    self._region_index += 1
                    self._region_offset = 0
                    self._scan_tail = b""
                if self._region_index >= len(self._regions):
                    break
                available = self._read_budget_remaining()
                size = min(total - self._region_offset, self.profile.scan_chunk_bytes, remaining,
                           available, getattr(self.memory, "max_single_read", self.profile.scan_chunk_bytes))
                if size <= 0:
                    break
                address = base + self._region_offset
                self._region_offset += size
                self._scan_total += size
                self._scan_bytes_total += size
                remaining -= size
                try:
                    data = self.read(address, size)
                except NativeReportError as exc:
                    self._scan_tail = b""
                    if exc.code == "native_memory_unreadable":
                        continue  # A region may have been freed after enumeration.
                    raise
                # Cache the boundary bytes rather than rereading overlap.
                # A later candidate is still checked against current memory.
                block_address = address - len(self._scan_tail)
                block = self._scan_tail + data
                self._scan_tail = block[-7:]
                self._scan_block = (block_address, block, 0, {})
            block_address, block, start, valid = self._scan_block
            while (offset := block.find(pattern, start)) >= 0:
                start = offset + 1
                candidate = block_address + offset
                if candidate % 8:
                    continue
                if candidates_this_poll >= self.profile.max_candidates:
                    self._scan_block = (block_address, block, offset, valid)
                    self.status = "locating_report_controller"
                    return None
                # Getter validation is bounded but can require several reads.
                # Reserve its maximum supported footprint when possible.
                available = self._read_budget_remaining()
                if available < min(1024, self.profile.max_read_bytes_per_poll):
                    self._scan_block = (block_address, block, offset, valid)
                    self.status = "locating_report_controller"
                    return None
                candidates_this_poll += 1
                self._candidates_seen += 1
                try:
                    variant = self._controller_variant(candidate)
                    if variant is not None:
                        self._header(variant)
                except NativeReportError as exc:
                    if exc.code == "unsupported_native_getter":
                        raise
                    if exc.code == "native_read_budget_exceeded":
                        self._scan_block = (block_address, block, offset, valid)
                        self.status = "locating_report_controller"
                        return None
                    continue
                valid[variant] = candidate
                if len(valid) > 1:
                    self._scan_block = (block_address, block, 0, {})
                    raise NativeReportError("ambiguous_native_report")
            self._scan_block = None
            if valid:
                variant, self._controller = next(iter(valid.items()))
                self._variant = variant
                self.baseline_ready = True
                if variant is None:
                    self.status = "waiting_for_published_report"
                return variant
        if self._region_index >= len(self._regions):
            # A new controller can be allocated after the current scan.
            self._scan_rounds_completed += 1
            self._scan_last_round_bytes = self._scan_total
            self._last_regions_completed = len(self._regions)
            self._regions = None
            self._region_index = self._region_offset = self._scan_total = 0
            self._scan_tail = b""
            self.status = "waiting_for_report_controller"
            self.baseline_ready = True
        else:
            self.status = "locating_report_controller"
        return None

    def peek_identity(self) -> tuple[int, int, int] | None:
        """Read only the current published identity; never acknowledge rows.

        ``status=locating_report_controller`` means a scan is still in progress;
        callers must not treat that as proof that no baseline report exists.
        """
        self._begin()
        variant = self._locate()
        if variant is None:
            return None
        raw, generation, kind, result = self._header(variant)
        if self.read(variant, 0x18) != raw:
            raise UnstableSnapshotError()
        if kind == 0 or generation == 0:
            self.status = "waiting_for_published_report"
            return None
        if kind != 1:
            raise NativeReportError("unsupported_native_result_kind")
        self.status = "published_report"
        return variant, generation, result

    def _wstring(self, handle: int) -> str:
        if handle == 0:
            return ""
        if handle in self._strings:
            return self._strings[handle]
        # Refcount +0 changes during rendering and is intentionally not read.
        header = self.read(handle + 0x10, 0x20)
        length, capacity = struct.unpack_from("<QQ", header, 0x10)
        if length > capacity or capacity > self.profile.max_text_characters:
            raise NativeReportError("unsupported_native_text_layout")
        if capacity < 8:
            if capacity != 7:
                raise NativeReportError("unsupported_native_text_layout")
            raw = header[: (length + 1) * 2]
        else:
            pointer = struct.unpack_from("<Q", header)[0]
            raw = self.read(pointer, (length + 1) * 2)
        if raw[-2:] != b"\0\0":
            raise NativeReportError("invalid_native_text_terminator")
        try:
            value = raw[:-2].decode("utf-16-le", errors="strict")
        except UnicodeError as exc:
            raise NativeReportError("invalid_native_utf16") from exc
        if "\0" in value:
            raise NativeReportError("invalid_native_text_contents")
        self._strings[handle] = value
        return value

    def _unit_dictionary(self) -> int:
        if self._dictionary is not None:
            return self._dictionary
        manager = self._u64(self.image_base + self.profile.localisation_manager_rva)
        token = self._u64(self.image_base + self.profile.unit_dictionary_token_rva)
        header = self.read(manager + 0xC0, 0x10)
        count = struct.unpack_from("<I", header)[0] & 0x07FFFFFF
        pointer = struct.unpack_from("<Q", header, 8)[0]
        if not token or count > 128:
            raise NativeReportError("invalid_native_dictionary_registry")
        entries = self.read(pointer, count * 0x10)
        matches = [struct.unpack_from("<I", entries, i * 16 + 8)[0]
                   for i in range(count) if struct.unpack_from("<Q", entries, i * 16)[0] == token]
        if len(matches) != 1 or matches[0] >= 128:
            raise NativeReportError("native_unit_dictionary_unavailable")
        self._dictionary = self._u64(manager + 0xD8 + matches[0] * 8)
        return self._dictionary

    def _unit_name(self, token: int) -> str:
        if token == 0:
            return ""
        if token in self._translations:
            return self._translations[token]
        dictionary = self._unit_dictionary()
        header = self.read(dictionary + 0x30, 0x10)
        layers, count, capacity = struct.unpack("<QII", header)
        if count > capacity or capacity > self.profile.max_dictionary_layers:
            raise NativeReportError("invalid_native_dictionary_layers")
        layer_bytes = self.read(layers, count * 8)
        # The engine searches most recently added dictionaries first.
        for index in range(count - 1, -1, -1):
            layer = struct.unpack_from("<Q", layer_bytes, index * 8)[0]
            resource = self._u64(layer)
            data = self._get(resource, 0x48)
            size = self._u32(data + 4)
            if size > self.profile.max_dictionary_entries:
                raise NativeReportError("native_dictionary_entry_limit")
            low, high = 0, size
            while low < high:
                mid = (low + high) // 2
                entry = self.read(data + 8 + mid * 16, 16)
                key, offset, length = struct.unpack("<QII", entry)
                if key < token:
                    low = mid + 1
                else:
                    high = mid
            if low >= size:
                continue
            key, offset, length = struct.unpack("<QII", self.read(data + 8 + low * 16, 16))
            if key != token:
                continue
            if offset < 8 + size * 16 or length > self.profile.max_text_characters:
                raise NativeReportError("invalid_native_translation_range")
            try:
                value = self.read(data + offset, length * 2).decode("utf-16-le", errors="strict")
            except UnicodeError as exc:
                raise NativeReportError("invalid_native_translation_utf16") from exc
            if "\0" in value:
                raise NativeReportError("invalid_native_translation_contents")
            self._translations[token] = value
            return value
        raise NativeReportError("native_unit_translation_missing")

    def _actor(self, row: bytes, offset: int) -> dict[str, Any]:
        token, soldier = struct.unpack_from("<QQ", row, offset)
        owner_name = struct.unpack_from("<Q", row, offset + 0x48)[0]
        return {
            "player_name": self._wstring(owner_name),
            "unit_name": self._unit_name(token),
            "soldier_name": self._wstring(soldier),
            "unit_label_texture_token": struct.unpack_from("<I", row, offset + 0x38)[0],
            "xp_texture_token": struct.unpack_from("<I", row, offset + 0x3C)[0],
            "display_veterancy": struct.unpack_from("<I", row, offset + 0x40)[0],
            "player_label_color_token": struct.unpack_from("<I", row, offset + 0x50)[0],
            "is_ace": bool(row[offset + 0x54] & 1),
            "unit_guid": None,
            "player_user_id": None,
            "identity_status": "unresolved",
        }

    def _players(self, model: bytes, count: int) -> list[dict[str, Any]]:
        """Read the published roster; never infer game IDs from display names.

        Builder 0x140bdb10a..111 copies TeamDescription+0x38's native
        wide-string name to row+0. Native alliance display values 0/1 are
        proven; the builder maps other source values to4. Row+8 comes from the
        exact setup key PlayerUserId, parsed as uint64 in base10. Its account
        authority is not inferred; UINT64_MAX is the native invalid sentinel.
        """
        players = []
        for index in range(count):
            offset = 0x40 + index * 0x88
            name = self._wstring(struct.unpack_from("<Q", model, offset)[0])
            user_id = struct.unpack_from("<Q", model, offset + 8)[0]
            raw_alliance = struct.unpack_from("<I", model, offset + 0x30)[0]
            if raw_alliance not in (0, 1, 4):
                raise NativeReportError("unsupported_native_player_alliance")
            players.append({"row_index": index, "name": name,
                            "player_user_id": None if user_id == 0xFFFFFFFFFFFFFFFF else str(user_id),
                            "alliance": raw_alliance if raw_alliance in (0, 1) else None,
                            "alliance_display_value": raw_alliance,
                            "official_kill_points": struct.unpack_from("<I", model, offset + 0x38)[0],
                            "official_loss_points": struct.unpack_from("<I", model, offset + 0x40)[0]})
        return players

    def poll(self) -> NativeReport | None:
        self._begin()
        variant = self._locate()
        if variant is None:
            return None
        self._observations = {}
        self._strings = {}
        self._translations = {}
        self._dictionary = None
        try:
            _, generation, kind, result = self._header(variant)
            if kind == 0 or generation == 0:
                self.status = "waiting_for_published_report"
                return None
            if kind != 1:
                raise NativeReportError("unsupported_native_result_kind")
            identity = (variant, generation, result)
            if identity == self._last_delivered:
                self.status = "unchanged_report"
                return None
            model = self.read(result, self.profile.result_size)
            count_players = struct.unpack_from("<I", model, 0x3C)[0]
            if count_players > 20:
                raise NativeReportError("invalid_native_player_count")
            players = self._players(model, count_players)
            offset = self.profile.viewmodel_offset + 0x84
            pointer, count, capacity = struct.unpack_from("<QII", model, offset)
            if count > capacity or capacity > self.profile.max_rows:
                raise NativeReportError("invalid_native_timeline_size")
            if count and not _pointer(pointer):
                raise NativeReportError("invalid_native_timeline_pointer")
            raw = self.read(pointer, count * self.profile.row_stride) if count else b""
            rows = []
            previous = -math.inf
            for index in range(count):
                row = raw[index * self.profile.row_stride:(index + 1) * self.profile.row_stride]
                seconds = struct.unpack_from("<d", row)[0]
                if not math.isfinite(seconds) or not 0 <= seconds <= self.profile.max_game_time_seconds:
                    raise NativeReportError("invalid_native_timeline_time")
                if seconds < previous:
                    raise NativeReportError("unordered_native_timeline")
                previous = seconds
                rows.append({"row_index": index, "game_time_seconds": seconds,
                             "time_display": native_time_display(seconds),
                             "victim": self._actor(row, 0x08),
                             "killer": self._actor(row, 0x60)})
            # The published timeline discarded owner CommonIDs. A unique exact
            # display-name match may still recover a roster ID independently
            # of the descriptor identity. Names remain unchanged and repeated
            # names never choose an arbitrary player.
            by_name = {}
            for player in players:
                by_name.setdefault(player["name"], []).append(player["player_user_id"])
            for row in rows:
                for role in ("killer", "victim"):
                    identifiers = by_name.get(row[role]["player_name"], [])
                    if len(identifiers) == 1:
                        row[role]["player_user_id"] = identifiers[0]
            observations = self._observations
            self._observations = None
            # Include contents as well as container headers: headers alone do
            # not detect in-place changes or replaced string/dictionary data.
            for (address, size), expected in observations.items():
                if self.read(address, size) != expected:
                    raise UnstableSnapshotError()
            if self._registry_selected:
                found, current = self._stable_registry_variant()
                if not found or current != variant:
                    raise UnstableSnapshotError()
            if self._controller is not None and self._controller_variant(self._controller) != variant:
                raise UnstableSnapshotError()
            if self.read(variant, 0x18) != observations[(variant, 0x18)]:
                raise UnstableSnapshotError()
            self.status = "native_report_read"
            self._last_delivered = identity
            return NativeReport(generation=generation, result_address=result, rows=rows,
                                variant_address=variant,
                                game_build=self.profile.game_build,
                                players=players,
                                metadata={"game_build": self.profile.game_build,
                                          "exe_sha256": self.exe_sha256,
                                          "source": "published_native_mission_report",
                                          "native_player_count": count_players,
                                          "player_identity_fields": "native_setup_player_user_id_name_alliance",
                                          "row_order": "native_vector_order",
                                          "report_data": {"identity_source": "unavailable",
                                                          "identity_status": "unavailable",
                                                          "matched_rows": 0,
                                                          "total_rows": len(rows)},
                                          "locator": "manual_variant" if self.profile.explicit_variant_address
                                          else "native_manager_registry" if self._registry_selected
                                          else "native_report_controller"})
        finally:
            self._observations = None
