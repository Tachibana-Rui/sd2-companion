"""Summarize production requests after conservatively matching cancellations."""
import hashlib
import json
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path

from style_model import CATEGORY_IDS


CANCELLATION_MODEL_VERSION = "sd2-production-cancellation-v1"


@lru_cache(maxsize=1)
def guid_map():
    return json.loads((Path(__file__).parent / "data/unit-guid-map.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def transport_classification():
    return json.loads((Path(__file__).parent / "data/transport-classification.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def production_timing():
    directory = Path(__file__).parent / "data"
    metadata = json.loads((directory / "production-timing.json").read_text(encoding="utf-8"))
    mapped_bytes = (directory / "unit-guid-map.json").read_bytes()
    times = metadata.get("seconds_by_guid", {})
    verified = (metadata.get("schema") == "sd2-production-timing-v1"
                and metadata.get("unit_guid_map_sha256") == hashlib.sha256(mapped_bytes).hexdigest()
                and set(times) == set(json.loads(mapped_bytes).get("units", {}))
                and all(_price(value) is not None for value in times.values()))
    return {**metadata, "mapping_verified": verified, "seconds_by_guid": times if verified else {}}


def _uint(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF


def reconcile_cancellations(orders, cancellations, *, tick_rate, battle_started_tick=None,
                            game_version=None, stream_complete=True):
    """Match individual queue groups without treating arbitrary IDs as counts.

    Production IDs share an allocator with AI activity. Sequential IDs are only
    candidates: owner, source slot, producer and a still-active timing window
    must also agree. No nearest-order or per-player running counter is used.
    Returned copies keep the recorded orders untouched for future backfills.
    """
    adjusted = []
    for order in orders:
        # Old decoded archives sometimes counted the air world's state (2) as
        # a quantity. Each air action creates only one production pack.
        quantity = 1 if order.get("subtype") == 2 else order["requested_count"]
        adjusted.append({**order, "requested_count": quantity, "cancelled_count": 0, "net_count": quantity})
    annotated = [dict(cancel) for cancel in cancellations]
    metadata = {"version": CANCELLATION_MODEL_VERSION, "status": "approximate",
                "method": "global_sequence_or_unique_active_queue", "refund_verified": False,
                "deployment_verified": False, "sequence_drift_detected": False,
                "notice": "以全局生产编号候选交叉核对来源、所有权、工厂及队列时间；编号因AI偏移时只关联唯一未到期战斗组。属于近似匹配，无法关联时不扣。"}
    if not stream_complete or not _uint(tick_rate) or tick_rate == 0:
        for cancel in annotated:
            cancel["cancellation_status"] = "unavailable"
        return adjusted, annotated, {**metadata, "status": "unavailable"}

    events = [(order, "production", index) for index, order in enumerate(adjusted)]
    events += [(cancel, "cancel", index) for index, cancel in enumerate(annotated)]
    indexes = [event.get("stream_command_index") for event, _, _ in events]
    if (any(not _uint(index) for index in indexes) or len(indexes) != len(set(indexes))
            or any(not _uint(event.get("tick")) or not _uint(event.get("source_slot")) for event, _, _ in events)):
        for cancel in annotated:
            cancel["cancellation_status"] = "unavailable"
        return adjusted, annotated, {**metadata, "status": "unavailable"}
    events.sort(key=lambda item: item[0]["stream_command_index"])
    if any(events[index][0]["tick"] > events[index + 1][0]["tick"] for index in range(len(events) - 1)):
        for cancel in annotated:
            cancel["cancellation_status"] = "unavailable"
        return adjusted, annotated, {**metadata, "status": "unavailable"}

    timing = production_timing()
    timing_compatible = str(game_version) == str(timing.get("game_build"))
    metadata["timing_game_build"] = timing.get("game_build")
    metadata["timing_compatible"] = timing_compatible
    metadata["timing_approximation"] = not timing_compatible
    metadata["timing_mapping_verified"] = timing.get("mapping_verified", True)
    if metadata["timing_mapping_verified"] is not True:
        metadata["notice"] += "生产时长目录与GUID映射未对齐，战斗取消不进行时长近似扣减。"
    if not timing_compatible:
        metadata["notice"] += "录像版本不一致，战斗队列时长按当前同GUID目录近似；未知时长不扣。"
    next_id = 0
    groups = {}
    cancelled_references = set()
    sequence_drift = False

    def active(group, cancel):
        order = group["order"]
        if order.get("in_battle") is False:
            # Deployment queues remain editable throughout deployment. A
            # later battle cancellation cannot remove a unit already deployed.
            return cancel.get("in_battle") is False and (battle_started_tick is None or cancel["tick"] < battle_started_tick)
        if order.get("in_battle") is not True or cancel.get("in_battle") is not True:
            return None
        seconds = [timing.get("seconds_by_guid", {}).get(guid) for guid in order.get("unit_guids", [])]
        if not seconds or any(_price(second) is None for second in seconds) or len(set(seconds)) != 1:
            return None
        # A zero-duration aircraft/defence pack has no cancellable battle queue.
        deadline = order["tick"] + seconds[0] * tick_rate
        return cancel["tick"] < deadline

    def identity(group, cancel, factory):
        order = group["order"]
        owner = order.get("owner_object_id")
        pack = order.get("pack_values")
        return (isinstance(owner, (list, tuple)) and owner and owner[0] == cancel.get("owner_object_id")
                and order.get("source_slot") == cancel.get("source_slot")
                and isinstance(pack, (list, tuple)) and len(pack) > 1 and pack[1] == factory)

    for event, kind, _ in events:
        if kind == "production":
            quantity = quantity_for_statistics(event)
            for _ in range(quantity):
                next_id += 1
                groups[next_id] = {"order": event, "cancelled": False}
            continue
        values = event.get("request_values")
        if not isinstance(values, (list, tuple)) or len(values) != 3 or any(not _uint(value) for value in values):
            event["cancellation_status"] = "unsupported"
            continue
        factory, object_id, _fallback = values
        reference = (event["source_slot"], event.get("owner_object_id"), factory, object_id)
        if object_id == 0:
            event["cancellation_status"] = "unsupported"
            continue
        if reference in cancelled_references:
            event["cancellation_status"] = "duplicate"
            continue
        if object_id > next_id:
            # Once a real reference runs ahead of the recorded allocator,
            # later numeric coincidences cannot restore sequence confidence.
            sequence_drift = True
        group = None if sequence_drift else groups.get(object_id)
        method = "global_sequence_candidate"
        if group is None or not identity(group, event, factory):
            # Additional AI allocations can put real IDs ahead of recorded
            # productions. Only a unique current battle queue group gives an
            # unambiguous unit/transport deduction; never choose the nearest.
            candidates = [candidate for candidate in groups.values()
                          if not candidate["cancelled"] and candidate["order"].get("in_battle") is True
                          and identity(candidate, event, factory) and active(candidate, event) is True]
            if sequence_drift and len(candidates) == 1:
                group = candidates[0]
                method = "unique_active_queue_candidate"
            else:
                event["cancellation_status"] = "ambiguous" if sequence_drift and len(candidates) > 1 else "unmatched"
                continue
        if group["cancelled"]:
            event["cancellation_status"] = "duplicate"
            continue
        eligible = active(group, event)
        if eligible is not True:
            event["cancellation_status"] = "expired" if eligible is False else "unsupported"
            continue
        group["cancelled"] = True
        cancelled_references.add(reference)
        order = group["order"]
        order["cancelled_count"] += 1
        order["net_count"] -= 1
        event["cancellation_status"] = "matched"
        event["cancellation_match_method"] = method
        event["matched_production_stream_command_index"] = order["stream_command_index"]
    metadata["sequence_drift_detected"] = sequence_drift
    return adjusted, annotated, metadata


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def quantity_for_statistics(row):
    """Keep the recorded quantity auditable while consuming the adjusted one."""
    requested = row.get("requested_count", 0)
    cancelled = row.get("cancelled_count", 0)
    net = row.get("net_count", requested)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (requested, cancelled, net)):
        raise ValueError("production quantities must be non-negative integers")
    if net + cancelled != requested:
        raise ValueError("net quantity plus cancelled quantity must equal requested quantity")
    return net


def summarize_mechanization(summary):
    """Classify purchased transport components; supply vehicles are separate."""
    result = {"eligible_transport_count": 0, "paid_armed_transport_count": 0,
              "paid_armed_transport_value": 0.0, "free_transport_count": 0,
              "supply_transport_count": 0, "other_transport_count": 0,
              "unknown_transport_count": 0, "ratio_denominator_count": 0,
              "ratio": None, "denominator_label": "已识别的非补给运输车辆",
              "classification_version": None,
              "catalog_approximation": summary.get("mapping_compatible") is not True}
    metadata = None
    for row in summary.get("units", []):
        if row.get("role") != "transport":
            continue
        quantity = quantity_for_statistics(row)
        if quantity == 0:
            continue
        if metadata is None:
            metadata = transport_classification()
            result["classification_version"] = metadata.get("classifier_version")
        unit = metadata.get("units", {}).get(row.get("guid"), {})
        if unit.get("metadata_verified") is not True or unit.get("is_transporter") is not True:
            result["unknown_transport_count"] += quantity
            continue
        # Supply wins even when a supply truck also has a mounted weapon.
        if unit.get("is_supply") is True:
            result["supply_transport_count"] += quantity
            continue
        cost = _price(row.get("catalog_cost"))
        if cost is None and summary.get("mapping_compatible") is not True:
            cost = _price(unit.get("catalog_cost"))
        if cost is None or not isinstance(unit.get("is_armed"), bool):
            result["unknown_transport_count"] += quantity
            continue
        if cost == 0:
            result["free_transport_count"] += quantity
        elif unit["is_armed"]:
            result["paid_armed_transport_count"] += quantity
            result["paid_armed_transport_value"] += cost * quantity
        else:
            result["other_transport_count"] += quantity
    denominator = sum(result[key] for key in ("paid_armed_transport_count", "free_transport_count", "other_transport_count"))
    result["eligible_transport_count"] = result["ratio_denominator_count"] = denominator
    result["ratio"] = result["paid_armed_transport_count"] / denominator if denominator else None
    return result


def summarize_orders(orders, game_version, catalog_hash, cancellations=(), *, reconciliation=None):
    mapping = guid_map()
    compatible = str(game_version) == str(mapping["game_build"]) and catalog_hash == mapping["catalog_sha256"]
    counts = dict.fromkeys(CATEGORY_IDS, 0)
    values = dict.fromkeys(CATEGORY_IDS, 0)
    requested_counts = dict.fromkeys(CATEGORY_IDS, 0)
    requested_values = dict.fromkeys(CATEGORY_IDS, 0)
    unknown = 0
    rows = {}
    main_count = 0
    requested_main_count = 0
    for order in orders:
        requested = order["requested_count"]
        quantity = quantity_for_statistics(order)
        cancelled = requested - quantity
        for guid, role in [(guid, "unit") for guid in order["unit_guids"]] + [
                (order[key], "transport") for key in ("transport_guid", "super_transport_guid") if order.get(key)]:
            unit = mapping["units"].get(guid) if compatible else None
            category = "transport" if role == "transport" and unit else unit["category"] if unit else "unknown"
            if category not in counts:
                category = "unknown"
            counts[category] += quantity
            requested_counts[category] += requested
            main_count += quantity if role == "unit" else 0
            requested_main_count += requested if role == "unit" else 0
            if unit is None:
                unknown += quantity
                if quantity:
                    values[category] = None
                requested_values[category] = None
            elif unit.get("cost") is None:
                if quantity:
                    values[category] = None
                requested_values[category] = None
            elif values[category] is not None:
                values[category] += quantity * unit["cost"]
            if unit is not None and unit.get("cost") is not None and requested_values[category] is not None:
                requested_values[category] += requested * unit["cost"]
            key = (guid, role)
            row = rows.setdefault(key, {"guid": guid, "role": role, "name": unit["name"] if unit else "未映射单位 GUID",
                                        "catalog_id": unit["catalog_id"] if unit else None, "category": category,
                                        "requested_count": 0, "cancelled_count": 0, "net_count": 0,
                                        "catalog_cost": unit.get("cost") if unit else None})
            row["requested_count"] += requested
            row["cancelled_count"] += cancelled
            row["net_count"] += quantity
    adjustment = dict(reconciliation or {"status": "unavailable", "method": "not_reconciled",
                                         "matched_count": 0, "unmatched_count": len(cancellations),
                                         "duplicate_count": 0, "expired_count": 0, "unsupported_count": 0})
    if reconciliation is not None:
        statuses = Counter(cancel.get("cancellation_status", "unavailable") for cancel in cancellations)
        methods = Counter(cancel.get("cancellation_match_method") for cancel in cancellations)
        adjustment.update(matched_count=statuses["matched"], unmatched_count=len(cancellations) - statuses["matched"],
                          duplicate_count=statuses["duplicate"], expired_count=statuses["expired"],
                          unsupported_count=statuses["unsupported"], ambiguous_count=statuses["ambiguous"],
                          unavailable_count=statuses["unavailable"],
                          sequence_matched_count=methods["global_sequence_candidate"],
                          queue_matched_count=methods["unique_active_queue_candidate"])
    summary = {"decoded_order_count": len(orders), "requested_unit_count": sum(requested_counts.values()),
            "requested_main_unit_count": requested_main_count, "net_unit_count": sum(counts.values()),
            "net_main_unit_count": main_count,
            "cancelled_unit_count": sum(requested_counts.values()) - sum(counts.values()),
            "cancelled_main_unit_count": requested_main_count - main_count, "unmapped_guid_count": unknown,
            "category_counts": counts, "estimated_values": values,
            "requested_category_counts": requested_counts, "requested_estimated_values": requested_values,
            "mapping_version": mapping["game_build"], "mapping_compatible": compatible,
            "cancellation_request_count": len(cancellations),
            "cancellation_adjustment": adjustment,
            "units": sorted(rows.values(), key=lambda row: (-row["net_count"], -row["requested_count"], row["guid"])),
            "evidence": "production_requests", "payment_verified": False, "deployment_verified": False,
            "notice": "数量按生产请求减去已匹配取消计算，取消一组会同时扣除主单位与绑定运输。未匹配取消不扣；点数为目录估值，支付及实际成功部署仍未验证。"}
    summary["mechanization"] = summarize_mechanization(summary)
    return summary
