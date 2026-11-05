"""Bind command sources through the observed official GameSetup rank algorithm.

Metadata numbers, stream coverage, actor ownership and all production GUIDs
are checked before publishing a binding. No nickname or deck similarity score
is used to reassign a conflicting actor. See REPLAY_IDENTITY.md for evidence.
"""
from __future__ import annotations

import re
from typing import Callable, Mapping


MAX_SUPPORTED_PLAYERS = 20
METHOD = "official_game_setup_rank"
CLIENT_SHA256 = "2986600db813a24a92f59698ac1b250d4ae7a96b84f2000bc1ad5a53bbfd1cf6"
EVIDENCE = [
    "official_client_header_reader_0x140ee36d0",
    "official_client_rank_sort_0x141154a30",
    "official_client_rank_maps_0x141159060",
    "official_client_rank_to_header_0x1405a9330",
    "official_client_pact_reader_0x140916c60",
]
_GUID = re.compile(r"[0-9a-f]{32}\Z")
_DECIMAL = re.compile(r"[0-9]{1,10}\Z")


def _number(value, maximum=0xFFFFFFFF):
    if isinstance(value, bool):
        raise ValueError("boolean is not an identity number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = int(value)
    else:
        raise ValueError("invalid identity number")
    if not 0 <= number <= maximum:
        raise ValueError("identity number out of range")
    return number


def _sort_record(slot, raw):
    slot = _number(slot)
    if not isinstance(raw, dict):
        raise ValueError("invalid player metadata")
    # The official reader leaves absent PlayerSlot/PlayerIsObserver at zero.
    # Present null, boolean, empty or malformed values are not missing values.
    player_slot = _number(raw["PlayerSlot"]) if "PlayerSlot" in raw else 0
    observer = _number(raw["PlayerIsObserver"], 1) if "PlayerIsObserver" in raw else 0
    alliance = _number(raw.get("PlayerAlliance"), 1)
    return player_slot, observer, alliance, slot


def allowed_deck_guids(header_players, guid_map, decode_deck: Callable):
    """Return header-key -> permitted main/transport GUIDs, including ace links.

    ``decode_deck`` is the project's strict deck decoder, passed by the caller.
    This helper does not load files or convert a carried deck into purchases.
    Unknown catalog cards are omitted; a requested unknown unit then conflicts.
    """
    units = guid_map.get("units", guid_map)
    if not isinstance(units, Mapping):
        raise ValueError("invalid GUID mapping")
    by_id = {}
    by_descriptor = {}
    for guid, unit in units.items():
        if not isinstance(guid, str) or not _GUID.fullmatch(guid) or not isinstance(unit, Mapping):
            raise ValueError("invalid GUID mapping entry")
        identifier, descriptor = str(unit.get("catalog_id", "")), unit.get("descriptor")
        if identifier in by_id or descriptor in by_descriptor:
            raise ValueError("ambiguous GUID mapping")
        by_id[identifier] = (guid, unit)
        by_descriptor[descriptor] = guid
    result = {}
    for slot, raw in header_players:
        slot = _number(slot)
        if slot in result:
            raise ValueError("duplicate header player")
        parsed = decode_deck(raw.get("PlayerDeckContent"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("entries"), list):
            raise ValueError("invalid decoded deck")
        allowed = set()
        for card in parsed["entries"]:
            if not isinstance(card, dict):
                raise ValueError("invalid decoded card")
            for field in ("unit_id", "transport_id"):
                identifier = card.get(field)
                if field == "transport_id" and identifier == -1 and not isinstance(identifier, bool):
                    continue
                identifier = _number(identifier)
                found = by_id.get(str(identifier))
                if found is None:
                    continue
                guid, unit = found
                allowed.add(guid)
                target = unit.get("ace_unit_type_descriptor")
                if target:
                    linked_guid = by_descriptor.get(target)
                    if linked_guid is None:
                        raise ValueError("unresolved explicit ace link")
                    allowed.add(linked_guid)
        result[slot] = allowed
    return result


def bind_players(header_players, stream, *, allowed_unit_guids=None):
    """Return verified rank bindings or an empty map with safe conflict codes.

    The supported rank is the unsigned ascending tuple
    (PlayerSlot, PlayerIsObserver, PlayerAlliance, original_header_N).
    For production-bearing files the caller must supply complete allowed GUID
    sets. These sets cross-check the official rank; they never choose the rank.
    """
    conflicts = []
    result = {
        "bindings": {}, "method": METHOD, "validated": False,
        "evidence": EVIDENCE.copy(), "client_sha256": CLIENT_SHA256,
        "conflicts": conflicts, "production_membership_checked": False,
    }
    try:
        if not isinstance(header_players, (list, tuple)) or not 1 <= len(header_players) <= MAX_SUPPORTED_PLAYERS:
            raise ValueError("unsupported_player_count")
        records = []
        seen = set()
        for item in header_players:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("invalid_header_players")
            record = _sort_record(*item)
            if record[3] in seen:
                raise ValueError("duplicate_header_slot")
            seen.add(record[3])
            records.append(record)
        records.sort()
        candidate = {rank: record[3] for rank, record in enumerate(records)}
        if not isinstance(stream, dict) or stream.get("framing_status") != "validated":
            raise ValueError("unvalidated_framing")
        if (stream.get("production_decoding_status") != "complete_production_request_stream"
                or stream.get("incomplete_order_packet_count") != 0
                or stream.get("order_decode_errors")
                or _number(stream.get("parsed_player_command_count")) != _number(stream.get("raw_command_count"))):
            raise ValueError("incomplete_command_decoding")
        sources = stream.get("player_slots")
        if not isinstance(sources, dict):
            raise ValueError("invalid_source_slots")
        for source in sources:
            if _number(source) not in candidate:
                raise ValueError("source_slot_out_of_range")
        productions = stream.get("production_orders")
        cancellations = stream.get("cancellation_orders", [])
        if not isinstance(productions, list) or not isinstance(cancellations, list):
            raise ValueError("invalid_order_list")
        if productions and not isinstance(allowed_unit_guids, Mapping):
            raise ValueError("production_membership_unavailable")

        owners = {}
        production_owners = {}
        owner_sources = {}
        for order in productions + cancellations:
            if not isinstance(order, dict):
                raise ValueError("invalid_order")
            source = _number(order.get("source_slot"))
            if source not in candidate or str(source) not in sources and source not in sources:
                raise ValueError("source_slot_out_of_range")
            owner = order.get("owner_object_id")
            if order.get("action_type") == 13:
                # AnnuleProduction stores the owner handle as one u32; its
                # following 16 bytes are not the production ObjectId factory.
                owner_handle = _number(owner)
            else:
                if not isinstance(owner, (list, tuple)) or len(owner) != 2:
                    raise ValueError("invalid_owner_object_id")
                owner = tuple(_number(value) for value in owner)
                owner_handle = owner[0]
                if source in production_owners and production_owners[source] != owner:
                    conflicts.append({"code": "source_owner_changed", "source_slot": source})
                production_owners[source] = owner
            if source in owners and owners[source] != owner_handle:
                conflicts.append({"code": "source_owner_changed", "source_slot": source})
            if owner_handle in owner_sources and owner_sources[owner_handle] != source:
                conflicts.append({"code": "owner_shared_by_sources", "source_slot": source})
            owners[source] = owner_handle
            owner_sources[owner_handle] = source

        for order in productions:
            source = _number(order["source_slot"])
            header_slot = candidate[source]
            allowed = allowed_unit_guids.get(header_slot)
            if not isinstance(allowed, (set, frozenset, list, tuple)):
                raise ValueError("production_membership_unavailable")
            allowed = set(allowed)
            if any(not isinstance(guid, str) or not _GUID.fullmatch(guid) for guid in allowed):
                raise ValueError("invalid_allowed_guid")
            main = order.get("unit_guids")
            if not isinstance(main, list) or not main:
                raise ValueError("invalid_production_guids")
            components = main.copy()
            for field in ("transport_guid", "super_transport_guid"):
                value = order.get(field)
                if value is not None and value != "0" * 32:
                    components.append(value)
            if any(not isinstance(guid, str) or not _GUID.fullmatch(guid) for guid in components):
                raise ValueError("invalid_production_guids")
            if not set(components) <= allowed:
                conflicts.append({"code": "production_deck_conflict", "source_slot": source,
                                  "header_slot": header_slot})
        if conflicts:
            return result
        result["production_membership_checked"] = bool(productions)
        result["validated"] = True
        result["bindings"] = candidate
        return result
    except (ValueError, TypeError, AttributeError) as error:
        # Do not include file contents or raw invalid metadata in diagnostics.
        code = str(error)
        safe_codes = {
            "unsupported_player_count", "invalid_header_players", "duplicate_header_slot",
            "unvalidated_framing", "incomplete_command_decoding", "invalid_source_slots",
            "source_slot_out_of_range", "invalid_order_list", "production_membership_unavailable",
            "invalid_order", "invalid_owner_object_id", "invalid_allowed_guid", "invalid_production_guids",
        }
        conflicts.append({"code": code if code in safe_codes else "invalid_identity_metadata"})
        return result
