"""Verified SD2 ProductionUnite wire decoding; orders are not deployments.

Layout cross-checked against the official build 168061 serializers and real
native replay packets. This module deliberately does not execute the game.
"""

from __future__ import annotations

import math
import struct
from collections import Counter
from typing import Any


MAX_VECTOR_ITEMS = 4096
MAX_ORDER_STRING_BYTES = 1024
ACTION_NAMES = {
    2: "Surrender", 3: "CommitOptionPlayer", 5: "GiveOrderStandard",
    6: "PlayerMission", 8: "ChangeOrderConfig", 11: "LaunchFumigene",
    12: "CancelFumigene", 13: "AnnuleProduction", 14: "ChangeProduction",
    15: "ProductionUnite", 16: "SpawnBunker", 17: "ItemFinished",
    18: "Kill", 19: "AddMaterial", 20: "SpawnUnit",
    21: "SpawnUnitWithTransport", 22: "PlayerSynchroServiceAction",
    39: "DeactivateWeapon", 40: "DeactivateSupply", 41: "SetOutmapShootMode",
    42: "Toggle",
}


class ReplayOrderError(ValueError):
    def __init__(self, code: str, offset: int):
        self.code = code
        self.offset = offset
        super().__init__(f"{code} at order byte {offset}")


class _Reader:
    def __init__(self, data: bytes | memoryview, origin: int = 0):
        self.data = memoryview(data)
        self.pos = 0
        self.origin = origin

    def fail(self, code: str) -> None:
        raise ReplayOrderError(code, self.origin + self.pos)

    def take(self, size: int) -> memoryview:
        if size < 0 or size > len(self.data) - self.pos:
            self.fail("truncated_order_field")
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def count(self, minimum_item_bytes: int = 1) -> int:
        count = self.u32()
        if count > MAX_VECTOR_ITEMS or count * minimum_item_bytes > len(self.data) - self.pos:
            self.fail("oversized_order_vector")
        return count

    def blob(self) -> "_Reader":
        if self.u32() != 11:
            self.fail("unsupported_serializer_blob_tag")
        size = self.u32()
        start = self.origin + self.pos
        return _Reader(self.take(size), start)

    def finish(self) -> None:
        if self.pos != len(self.data):
            self.fail("unexpected_order_trailing_bytes")


def _guid(reader: _Reader) -> str | None:
    raw = bytes(reader.take(16))
    return raw.hex() if any(raw) else None


def production_order_size(data: bytes | memoryview) -> int:
    """Return a verified outer ProductionUnite boundary inside a byte stream."""
    if len(data) < 13 or data[0] != 15:
        raise ReplayOrderError("not_a_production_order", 0)
    if struct.unpack_from("<I", data, 5)[0] != 11:
        raise ReplayOrderError("unsupported_production_blob_tag", 5)
    size = 13 + struct.unpack_from("<I", data, 9)[0]
    if size > len(data):
        raise ReplayOrderError("truncated_production_blob", 9)
    return size


def decode_production_order(data: bytes | memoryview) -> dict[str, Any]:
    """Read one exact ProductionUnite order without publishing positions.

    GUIDs identify game assets, and object IDs identify network objects. Neither
    field is a player Eugen ID. ``requested_count`` counts requested production
    groups; it does not assert successful payment, deployment, or combat value.
    """
    reader = _Reader(data)
    if reader.u8() != 15:
        reader.fail("not_a_production_order")
    subtype = reader.u32()
    if subtype not in (1, 2, 3):
        reader.fail("unsupported_production_subtype")
    body = reader.blob()
    reader.finish()
    base = body.blob()
    if base.u32() != subtype:
        base.fail("production_subtype_mismatch")
    base.finish()
    object_id = [body.u32(), body.u32()]
    body.blob().finish()  # the verified owner-object context is empty
    pack = body.blob()
    guid_count = pack.count(16)
    if guid_count == 0:
        pack.fail("empty_production_pack")
    guids = [_guid(pack) for _ in range(guid_count)]
    if any(value is None for value in guids):
        pack.fail("null_unit_guid")
    transport = _guid(pack)
    super_transport = _guid(pack)
    pack_values = [pack.u32() for _ in range(4)]
    pack.finish()

    production_groups: list[list[int]] = []
    placement_count: int | None = None
    if subtype == 2:
        values = [body.u32(), body.u32()]
        if values[0] not in (1, 2):
            body.fail("unsupported_air_production_state")
    else:
        placement_count = body.count(16)
        for _ in range(placement_count):
            x, y, z, _placement_value = struct.unpack("<fffI", body.take(16))
            if not all(math.isfinite(v) for v in (x, y, z)):
                body.fail("nonfinite_production_placement")
        values = [body.u32(), body.u32()]
        _flags = [body.u8(), body.u8()]
        for _ in range(body.count(4)):
            production_groups.append([body.u32() for _ in range(body.count(4))])
        _flag = body.u8()
        _order_token = body.u32()
        string_size = body.u32()
        if string_size > MAX_ORDER_STRING_BYTES:
            body.fail("oversized_order_string")
        # The string is a tactical order name (e.g. None/MoveFast), not a unit
        # descriptor. It is validated and discarded, as are coordinates.
        try:
            bytes(body.take(string_size)).decode("utf-8", "strict")
        except UnicodeDecodeError:
            body.fail("invalid_order_string")
        if placement_count != values[0] or len(production_groups) != values[0]:
            body.fail("production_quantity_mismatch")
    body.finish()
    # Out-world (air) production's first field is the world state, not a
    # quantity. Its official execution path buys exactly one pack per action.
    # Ground production's first field multiplies pack cost and controls the
    # production loop, so it remains the requested group count.
    requested_count = 1 if subtype == 2 else values[0]
    if not 1 <= requested_count <= MAX_VECTOR_ITEMS:
        body.fail("invalid_production_quantity")
    return {
        "action_type": 15,
        "subtype": subtype,
        "owner_object_id": object_id,
        "unit_guids": guids,
        "transport_guid": transport,
        "super_transport_guid": super_transport,
        "requested_count": requested_count,
        "production_groups": production_groups,
        "placement_count": placement_count,
        "pack_values": pack_values,
        "factory_key": pack_values[1],
        "production_values": values,
        "air_production_state": values[0] if subtype == 2 else None,
        "evidence": "production_request",
        "deployment_verified": False,
    }


def decode_cancel_order(data: bytes | memoryview) -> dict[str, Any]:
    """Read the producer key and production-object cancellation reference.

    The official execute path searches the producer's active queue using the
    first blob's object ID. Only an ID of zero selects the fallback reference;
    neither ID is a member of ProductionUnite's per-slot production_groups.
    Recording the request does not establish a completed refund.
    """
    reader = _Reader(data)
    if reader.u8() != 13:
        reader.fail("not_a_cancel_order")
    object_id = reader.u32()
    opaque_fields = [reader.u32() for _ in range(4)]
    value = reader.u32()
    first = reader.blob()
    first_value = first.u32()
    first.finish()
    second = reader.blob()
    second_value = second.u32()
    second.finish()
    reader.finish()
    return {
        "action_type": 13,
        "owner_object_id": object_id,
        "opaque_fields": opaque_fields,
        "request_values": [value, first_value, second_value],
        "factory_key": value,
        "production_object_id": first_value,
        "fallback_reference": second_value,
        "evidence": "cancellation_request",
        "refund_verified": False,
    }


def decode_item_finished_order(data: bytes | memoryview) -> dict[str, Any]:
    """Read ItemFinished without assuming it confirms a unit production.

    The two scalar fields and the 16-byte receiving-object context follow the
    official serializer. Their application meaning is a separate verification
    step; a class name alone does not establish a completed deployment.
    """
    reader = _Reader(data)
    if reader.u8() != 17:
        reader.fail("not_an_item_finished_order")
    item_value = reader.u32()
    receiver = reader.u32()
    context = [reader.u32() for _ in range(4)]
    reader.finish()
    return {
        "action_type": 17, "item_value": item_value,
        "receiver_object_id": receiver, "receiver_context": context,
        "evidence": "item_finished_instruction", "deployment_verified": False,
    }


def decode_smoke_cancel_order(data: bytes | memoryview) -> dict[str, Any]:
    reader = _Reader(data)
    if reader.u8() != 12:
        reader.fail("not_a_smoke_cancel_order")
    receiver = reader.u32()
    context = [reader.u32() for _ in range(4)]
    value = reader.u32()
    reader.finish()
    return {
        "action_type": 12, "receiver_object_id": receiver,
        "receiver_context": context, "reference_value": value,
        "evidence": "smoke_cancellation_instruction", "deployment_verified": False,
    }


def decode_change_production_order(data: bytes | memoryview) -> dict[str, Any]:
    reader = _Reader(data)
    if reader.u8() != 14:
        reader.fail("not_a_change_production_order")
    receiver = reader.u32()
    context = [reader.u32() for _ in range(4)]
    references = []
    for _ in range(reader.count(12)):
        value = reader.blob()
        references.append(value.u32())
        value.finish()
    secondary = [reader.u32() for _ in range(reader.count(4))]
    placement_count = reader.count(16)
    reader.take(placement_count * 16)
    parameter_count = reader.count(4)
    reader.take(parameter_count * 4)
    trailing_value = reader.u32()
    trailing_context = [reader.u32() for _ in range(4)]
    reader.finish()
    return {
        "action_type": 14, "receiver_object_id": receiver,
        "receiver_context": context, "reference_values": references,
        "secondary_values": secondary, "placement_count": placement_count,
        "parameter_count": parameter_count, "trailing_value": trailing_value,
        "trailing_context": trailing_context,
        "evidence": "production_change_instruction", "deployment_verified": False,
    }


def decode_synchro_service_order(data: bytes | memoryview) -> dict[str, Any]:
    reader = _Reader(data)
    if reader.u8() != 22:
        reader.fail("not_a_synchro_service_order")
    values = [reader.u32() for _ in range(3)]
    reader.finish()
    return {
        "action_type": 22, "service_values": values,
        "evidence": "player_synchro_service_instruction", "deployment_verified": False,
    }


def _vector(reader: _Reader, item_bytes: int = 4) -> None:
    reader.take(reader.count(item_bytes) * item_bytes)


def _order_size(data: memoryview) -> int:
    """Official serializer boundaries; unidentified actions stop the walker."""
    reader = _Reader(data)
    kind = reader.u8()
    # Action 40 (TNetworkActionDeactivateSupply) serializes u8 0x28, u32, u32,
    # u8: a fixed 10-byte body fenced by the official serializer.
    # Action 9 is not named in the wire enum yet, but every observed record is
    # the same 9-byte shape: u8 0x09 followed by two little-endian u32 fields
    # (first seen as the constant 0xC000002A, second an ascending id, e.g.
    # "09 2a0000c0 03000000"). Replays that mix it into a multi-command packet
    # otherwise stop the walk, void identity checks and never import, so only
    # its boundary is decoded here - it is never treated as a purchase or death.
    fixed = {2: 22, 3: 37, 9: 9, 12: 25, 17: 25, 22: 13, 40: 10, 41: 9, 42: 10}

    if kind in fixed:
        reader.take(fixed[kind] - 1)
    elif kind == 5:  # GiveOrderStandard
        _vector(reader)
        _vector(reader)
        reader.take(12 + 8 + 36)
        reader.u32()
        value = reader.blob()
        value.u32()
        value.finish()
        reader.take(reader.u16())
    elif kind == 6:  # PlayerMission
        reader.u32()
        reader.take(12)
        _vector(reader)
    elif kind == 8:  # ChangeOrderConfig
        _vector(reader)
        reader.u32()
    elif kind == 11:  # LaunchFumigene
        reader.u32()
        reader.take(16 + 12)
        reader.u32()
        reader.take(reader.u16())
    elif kind == 13:  # AnnuleProduction
        reader.u32()
        reader.take(16)
        reader.u32()
        for _ in range(2):
            value = reader.blob()
            value.u32()
            value.finish()
    elif kind == 14:  # ChangeProduction, not a new production purchase
        reader.u32()
        reader.take(16)
        for _ in range(reader.count(12)):
            value = reader.blob()
            value.u32()
            value.finish()
        _vector(reader)
        _vector(reader, 16)
        _vector(reader)
        reader.take(4 + 16)
    elif kind == 15:
        return production_order_size(data)
    elif kind == 39:  # DeactivateWeapon
        reader.u32()
        _vector(reader, 8)
        reader.u8()
    else:
        reader.fail("unsupported_player_action")
    return reader.pos


def walk_player_orders(data: bytes | memoryview, command_count: int) -> dict[str, Any]:
    """Traverse all declared commands, stopping at unsupported wire schemas.

    Each known command's boundary is read from its official serialization
    routine. Searching for an opcode inside arbitrary payload bytes is unsafe
    and is never used. No unknown action is guessed to be a purchase or death.
    """
    if not isinstance(command_count, int) or isinstance(command_count, bool) or not 0 <= command_count <= MAX_VECTOR_ITEMS:
        raise ValueError("invalid command_count")
    view = memoryview(data)
    offset = 0
    parsed = 0
    actions: Counter[int] = Counter()
    errors: Counter[str] = Counter()
    production: list[dict[str, Any]] = []
    cancellations: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    smoke_cancellations: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    service_actions: list[dict[str, Any]] = []
    decoders = {
        12: (decode_smoke_cancel_order, smoke_cancellations),
        13: (decode_cancel_order, cancellations),
        14: (decode_change_production_order, changes),
        15: (decode_production_order, production),
        17: (decode_item_finished_order, items),
        22: (decode_synchro_service_order, service_actions),
    }
    for _ in range(command_count):
        try:
            size = _order_size(view[offset:])
        except ReplayOrderError as exc:
            errors[exc.code] += 1
            break
        raw = view[offset:offset + size]
        kind = raw[0]
        actions[kind] += 1
        pair = decoders.get(kind)
        if pair is not None:
            decoder, target = pair
            try:
                decoded = decoder(raw)
            except ReplayOrderError as exc:
                errors[exc.code] += 1
            else:
                decoded["packet_command_index"] = parsed
                target.append(decoded)
        parsed += 1
        offset += size
    if parsed == command_count and offset != len(view):
        errors["unexpected_packet_trailing_bytes"] += 1
    return {
        "parsed_command_count": parsed,
        "parsed_bytes": offset,
        "complete": parsed == command_count and offset == len(view) and not errors,
        "action_counts": dict(actions),
        "production_orders": production,
        "cancellation_orders": cancellations,
        "item_finished_orders": items,
        "smoke_cancellation_orders": smoke_cancellations,
        "production_change_orders": changes,
        "synchro_service_orders": service_actions,
        "errors": dict(errors),
    }
