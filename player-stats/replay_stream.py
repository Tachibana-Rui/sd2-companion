"""Strict framing inspection of native SD2 ESAV v3 replay command streams.

This parses the observed binary structure and verified production requests,
not the game's simulation. A count of orders is never a count of purchases.
See REPLAY_FORMAT.md for real-file evidence and the supported boundary.
"""

from __future__ import annotations

import json
import math
import struct
from collections import Counter
from typing import Any

from replay_orders import ACTION_NAMES, MAX_VECTOR_ITEMS, walk_player_orders


STREAM_PARSER_VERSION = "sd2-esav3-stream-v2"
MAX_BLOCKS = 5_000_000
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_TICKS = 1_000_000
MAX_RESULT_BYTES = 64 * 1024
SYSTEM_SLOT = 0xFFFFFFFF
KNOWN_TAGS = {b"star", b"pact", b"endt", b"rslt"}
_BLOCK = struct.Struct(">4sQI")
_STAR = struct.Struct("<II")
_PACT = struct.Struct("<IIIH")
_SYSTEM_COMMAND = struct.Struct("<IHiiiii")
_ENDT = struct.Struct("<III")


class ReplayStreamError(ValueError):
    """A malformed/unsupported stream, with safe, non-content diagnostics."""

    def __init__(self, code: str, offset: int):
        self.code = code
        self.offset = offset
        super().__init__(f"{code} at byte {offset}")


def _fail(code: str, offset: int) -> None:
    raise ReplayStreamError(code, offset)


def _result(payload: memoryview, offset: int) -> dict[str, Any]:
    if len(payload) > MAX_RESULT_BYTES:
        _fail("result_too_large", offset)
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        _fail("invalid_result_json", offset)
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        _fail("invalid_result_shape", offset)
    # Do not propagate arbitrary strings, names, or extension data from a file.
    result: dict[str, int | float] = {}
    for key in ("Duration", "Victory", "Score"):
        raw = value["result"].get(key)
        try:
            parsed = float(raw) if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) else math.nan
        except (ValueError, OverflowError):
            parsed = math.nan
        if not math.isfinite(parsed):
            _fail("invalid_result_value", offset)
        result[key] = int(parsed) if parsed.is_integer() else parsed
    return result


def _action_summary(
    action_counts: Counter[int],
    action_ticks: dict[int, list[int]],
    action_phases: dict[int, Counter[str]],
    production: list[dict[str, Any]],
    items: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    declared_commands: int,
    parsed_commands: int,
    incomplete_packets: int,
) -> dict[str, Any]:
    first_production = min((event["tick"] for event in production), default=None)
    owner_ids = {event["owner_object_id"][0] for event in production}
    # These are raw values from the placement/group wire field. They are not
    # presumed to use the runtime global production-object ID namespace.
    earliest_group_value: dict[tuple[int, int], int] = {}
    for event in production:
        for group in event["production_groups"]:
            for value in group:
                key = (event["owner_object_id"][0], value)
                sequence = event["stream_command_index"]
                earliest_group_value[key] = min(earliest_group_value.get(key, sequence), sequence)
    item_overlap = sum(
        earliest_group_value.get((event["receiver_object_id"], event["item_value"]), math.inf) < event["stream_command_index"]
        for event in items
    )
    change_references = sum(len(event["reference_values"]) for event in changes)
    change_overlap = sum(
        earliest_group_value.get((event["receiver_object_id"], value), math.inf) < event["stream_command_index"]
        for event in changes for value in event["reference_values"]
    )
    complete = parsed_commands == declared_commands and incomplete_packets == 0
    return {
        "declared_command_count": declared_commands,
        "wire_decoded_command_count": parsed_commands,
        "wire_decode_complete": complete,
        "rows": [
            {
                "action_type": kind, "name": ACTION_NAMES.get(kind, "Unknown"),
                "count": action_counts[kind],
                "tick_range": action_ticks.get(kind),
                "phase_counts": dict(action_phases.get(kind, {})),
            }
            for kind in sorted(set(ACTION_NAMES) | set(action_counts))
        ],
        "requested_production_group_count": sum(event["requested_count"] for event in production),
        "first_production_tick": first_production,
        "item_finished": {
            "record_count": len(items),
            "tick_range": [min(event["tick"] for event in items), max(event["tick"] for event in items)] if items else None,
            "distinct_receiver_count": len({event["receiver_object_id"] for event in items}),
            "zero_item_value_count": sum(event["item_value"] == 0 for event in items),
            "before_first_production_count": sum(first_production is not None and event["tick"] < first_production for event in items),
            "receiver_equal_to_production_owner_count": sum(event["receiver_object_id"] in owner_ids for event in items),
            "raw_value_overlap_with_prior_ground_groups": item_overlap,
            "verified_unit_completion_count": None,
        },
        "production_change": {
            "record_count": len(changes), "reference_value_count": change_references,
            "raw_value_overlap_with_prior_ground_groups": change_overlap,
            "reference_namespaces_equated": False,
        },
        "explicit_spawn_instruction_count": sum(action_counts[kind] for kind in (16, 20, 21)) if complete else None,
        "explicit_kill_instruction_count": action_counts[18] if complete else None,
        "unit_spawn_count": None,
        "spawn_association_status": "not_established",
    }


def inspect_stream(
    data: bytes | bytearray | memoryview,
    stream_offset: int,
    tick_rate: int | float | None = None,
    expected_player_count: int | None = None,
) -> dict[str, Any]:
    """Validate every block and return small JSON-safe forensic metadata.

    ``stream_offset`` must come from the validated ESAV header's byte length,
    never from searching for a marker in a player nickname. No raw command
    payloads or private player metadata are returned. EOF at an ``endt`` is a
    valid server-native stream even when it has no client ``rslt`` footer.
    Actual purchases, kill attribution, and losses are deliberately unavailable.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    if not isinstance(stream_offset, int) or isinstance(stream_offset, bool) or not 0 <= stream_offset < len(data):
        raise ValueError("stream_offset must identify a byte inside data")
    if tick_rate is not None:
        if isinstance(tick_rate, bool) or not isinstance(tick_rate, (int, float)) or not math.isfinite(tick_rate) or not 0 < tick_rate <= 1000:
            raise ValueError("tick_rate must be finite and between 0 and 1000")
    if expected_player_count is not None:
        if not isinstance(expected_player_count, int) or isinstance(expected_player_count, bool) or not 1 <= expected_player_count <= 256:
            raise ValueError("expected_player_count must be an integer from 1 to 256")

    view = memoryview(data)
    offset = stream_offset
    counts: Counter[str] = Counter()
    system_counts: Counter[int] = Counter()
    players: dict[int, dict[str, int]] = {}
    # Header binding of slot -> player ID belongs to the metadata parser. These
    # slots are network-order sources, not necessarily player_N suffixes.
    current_tick: int | None = None
    tick_open = False
    tick_count = 0
    seen_slots: set[int] = set()
    transitions: list[dict[str, int]] = []
    result: dict[str, Any] | None = None
    total_commands = 0
    total_command_bytes = 0
    nonempty_packets = 0
    unknown_system_commands = 0
    max_payload_bytes = 0
    battle_started_tick: int | None = None
    battle_ended_tick: int | None = None
    native_completed = False
    known_game_state: int | None = None
    battle_active = False
    production_orders: list[dict[str, Any]] = []
    cancel_orders: list[dict[str, Any]] = []
    order_decode_errors: Counter[str] = Counter()
    single_action_counts: Counter[int] = Counter()
    undecoded_multicommand_packets = 0
    action_counts: Counter[int] = Counter()
    parsed_commands = 0
    complete_order_packets = 0
    incomplete_order_packets = 0
    item_finished_orders: list[dict[str, Any]] = []
    smoke_cancellation_orders: list[dict[str, Any]] = []
    production_change_orders: list[dict[str, Any]] = []
    synchro_service_orders: list[dict[str, Any]] = []
    action_ticks: dict[int, list[int]] = {}
    action_phases: dict[int, Counter[str]] = {}

    while offset < len(view):
        if sum(counts.values()) >= MAX_BLOCKS:
            _fail("too_many_blocks", offset)
        if len(view) - offset < _BLOCK.size:
            _fail("truncated_block_header", offset)
        tag, size, reserved = _BLOCK.unpack_from(view, offset)
        if tag not in KNOWN_TAGS:
            _fail("unsupported_block_tag", offset)
        if reserved != 0:
            _fail("unsupported_block_reserved", offset)
        if size > MAX_PAYLOAD_BYTES:
            _fail("block_too_large", offset)
        payload_start = offset + _BLOCK.size
        next_offset = payload_start + size
        if next_offset > len(view):
            _fail("truncated_block_payload", offset)
        payload = view[payload_start:next_offset]
        if result is not None:
            _fail("data_after_result", offset)
        counts[tag.decode("ascii")] += 1
        max_payload_bytes = max(max_payload_bytes, size)

        if tag == b"star":
            if tick_open:
                _fail("missing_endt", offset)
            if size < _STAR.size:
                _fail("invalid_star_size", offset)
            tick, command_count = _STAR.unpack_from(payload)
            if size != _STAR.size + command_count * _SYSTEM_COMMAND.size:
                _fail("invalid_system_command_length", offset)
            if tick != tick_count:
                _fail("nonsequential_tick", offset)
            if tick_count >= MAX_TICKS:
                _fail("too_many_ticks", offset)
            current_tick = tick
            tick_open = True
            tick_count += 1
            seen_slots.clear()
            for index in range(command_count):
                kind, short, scope, before, after, parameter, repeated_tick = _SYSTEM_COMMAND.unpack_from(
                    payload, _STAR.size + index * _SYSTEM_COMMAND.size
                )
                if repeated_tick != tick:
                    _fail("system_command_tick_mismatch", offset)
                system_counts[kind] += 1
                if short != 0 or scope != -1 or kind not in (5, 6):
                    unknown_system_commands += 1
                # This precise fixed structure is seen in native server files
                # and independently agrees with native GameState log changes.
                if kind == 5 and short == 0 and scope == -1 and parameter == -1:
                    if known_game_state is not None and before != known_game_state:
                        _fail("inconsistent_game_state_transition", offset)
                    known_game_state = after
                    transitions.append({"tick": tick, "before": before, "after": after})
                    if after == 53 and battle_started_tick is None:
                        battle_started_tick = tick
                    battle_active = after == 53
                    if before == 53 and after == 101 and battle_started_tick is not None:
                        battle_ended_tick = tick
                        native_completed = True

        elif tag == b"pact":
            if not tick_open or size < _PACT.size:
                _fail("unexpected_or_short_pact", offset)
            tick, slot, command_count, command_bytes = _PACT.unpack_from(payload)
            if tick != current_tick:
                _fail("pact_tick_mismatch", offset)
            if size != _PACT.size + command_bytes:
                _fail("invalid_player_command_length", offset)
            if (command_count == 0) != (command_bytes == 0) or command_count > command_bytes:
                _fail("invalid_player_command_count", offset)
            if command_count > MAX_VECTOR_ITEMS:
                _fail("player_command_count_limit", offset)
            if slot != SYSTEM_SLOT and (slot >= (expected_player_count or 256)):
                _fail("player_slot_out_of_range", offset)
            # Server-native shutdown can emit several empty sentinel sources
            # at its final tick. They are not duplicate human-player orders.
            if slot != SYSTEM_SLOT and slot in seen_slots:
                _fail("duplicate_player_slot_in_tick", offset)
            seen_slots.add(slot)
            stats = players.setdefault(slot, {
                "packet_count": 0, "nonempty_packet_count": 0,
                "raw_command_count": 0, "raw_command_bytes": 0,
                "battle_packet_count": 0, "battle_raw_command_count": 0,
                "first_packet_tick": tick, "last_packet_tick": tick,
                # A player who disconnects stops sending real orders while the
                # world keeps simulating under AI control until the battle ends.
                "last_nonempty_packet_tick": None,
            })
            stats["packet_count"] += 1
            stats["nonempty_packet_count"] += int(command_count > 0)
            stats["raw_command_count"] += command_count
            stats["raw_command_bytes"] += command_bytes
            stats["last_packet_tick"] = tick
            if command_count > 0:
                stats["last_nonempty_packet_tick"] = tick
            stats["battle_packet_count"] += int(battle_active)
            stats["battle_raw_command_count"] += command_count if battle_active else 0
            total_commands += command_count
            total_command_bytes += command_bytes
            nonempty_packets += int(command_count > 0)
            if command_count:
                raw = payload[_PACT.size:]
                if command_count == 1:
                    single_action_counts[raw[0]] += 1
                decoded_packet = walk_player_orders(raw, command_count)
                action_counts.update(decoded_packet["action_counts"])
                phase = {0: "inactive", 51: "loading", 52: "deployment", 53: "battle", 101: "debrief"}.get(known_game_state, "unverified")
                for kind, amount in decoded_packet["action_counts"].items():
                    if kind not in action_ticks:
                        action_ticks[kind] = [tick, tick]
                    else:
                        action_ticks[kind][1] = tick
                    action_phases.setdefault(kind, Counter())[phase] += amount
                order_decode_errors.update(decoded_packet["errors"])
                parsed_commands += decoded_packet["parsed_command_count"]
                complete_order_packets += int(decoded_packet["complete"])
                incomplete_order_packets += int(not decoded_packet["complete"])
                if command_count > 1 and not decoded_packet["complete"]:
                    undecoded_multicommand_packets += 1
                for key, target in (
                    ("production_orders", production_orders), ("cancellation_orders", cancel_orders),
                    ("item_finished_orders", item_finished_orders), ("smoke_cancellation_orders", smoke_cancellation_orders),
                    ("production_change_orders", production_change_orders), ("synchro_service_orders", synchro_service_orders),
                ):
                    for decoded in decoded_packet[key]:
                        decoded.update({
                            "tick": tick, "source_slot": slot, "in_battle": battle_active,
                            "stream_command_index": total_commands - command_count + decoded["packet_command_index"],
                        })
                        target.append(decoded)

        elif tag == b"endt":
            if not tick_open or size != _ENDT.size:
                _fail("unexpected_or_invalid_endt", offset)
            tick, _opaque_flags, _opaque_value = _ENDT.unpack_from(payload)
            if tick != current_tick:
                _fail("endt_tick_mismatch", offset)
            tick_open = False

        else:  # rslt
            if tick_open or tick_count == 0:
                _fail("unexpected_result", offset)
            result = _result(payload, offset)
        offset = next_offset

    if tick_open:
        _fail("missing_final_endt", offset)
    if tick_count == 0:
        _fail("empty_tick_stream", offset)
    native_orders = players.pop(SYSTEM_SLOT, None)
    completion_evidence = (
        "native_state_53_to_101" if native_completed else
        "client_result_footer" if result is not None else "unverified"
    )
    timeline_seconds = (current_tick / tick_rate) if tick_rate is not None else None
    battle_seconds = (
        (battle_ended_tick - battle_started_tick) / tick_rate
        if tick_rate is not None and battle_started_tick is not None and battle_ended_tick is not None else None
    )
    return {
        "parser_version": STREAM_PARSER_VERSION,
        "framing_status": "validated",
        "stream_offset": stream_offset,
        "stream_bytes": len(view) - stream_offset,
        "parsed_bytes": offset - stream_offset,
        "frame_count": sum(counts.values()),
        "block_counts": dict(counts),
        "tick_count": tick_count,
        "first_tick": 0,
        "last_tick": current_tick,
        "tick_rate": tick_rate,
        "recorded_timeline_seconds": timeline_seconds,
        "max_payload_bytes": max_payload_bytes,
        "raw_command_count": total_commands,
        "raw_command_bytes": total_command_bytes,
        "nonempty_player_packet_count": nonempty_packets,
        "system_command_counts": {str(k): v for k, v in sorted(system_counts.items())},
        "unknown_system_command_count": unknown_system_commands,
        "player_slots": {str(k): v for k, v in sorted(players.items())},
        "system_slot_orders": native_orders,
        "game_state_transitions": transitions,
        "single_command_action_counts": {str(k): v for k, v in sorted(single_action_counts.items())},
        "player_action_counts": {str(k): v for k, v in sorted(action_counts.items())},
        "parsed_player_command_count": parsed_commands,
        "complete_order_packet_count": complete_order_packets,
        "incomplete_order_packet_count": incomplete_order_packets,
        "production_orders": production_orders,
        "cancellation_orders": cancel_orders,
        "item_finished_orders": item_finished_orders,
        "smoke_cancellation_orders": smoke_cancellation_orders,
        "production_change_orders": production_change_orders,
        "synchro_service_orders": synchro_service_orders,
        "action_stats": _action_summary(action_counts, action_ticks, action_phases, production_orders, item_finished_orders, production_change_orders, total_commands, parsed_commands, incomplete_order_packets),
        "order_decode_errors": dict(order_decode_errors),
        "undecoded_multicommand_packets": undecoded_multicommand_packets,
        "production_decoding_status": "complete_production_request_stream" if incomplete_order_packets == 0 else "partial_production_request_stream",
        "battle_started_tick": battle_started_tick,
        "battle_ended_tick": battle_ended_tick,
        "battle_duration_seconds": battle_seconds,
        "completion_evidence": completion_evidence,
        "result": result,
        "capabilities": {
            "binary_framing": True, "game_state_transitions": True,
            "player_order_counts": True, "production_requests": incomplete_order_packets == 0,
            "actual_purchases": False,
            "kill_attribution": False, "combat_losses": False,
        },
        "missing_components": ["purchases", "kill_attribution", "combat_losses"],
    }
