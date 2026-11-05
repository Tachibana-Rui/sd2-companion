"""A deliberately approximate combat tendency from real production requests.

No payment, deployed unit, kill, or loss is inferred here. Prices and classes
may come from the current GUID catalog when the replay build differs. Each
match is normalized before aggregation, so a long match gets one vote.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from style_model import CATEGORIES


MODEL_VERSION = "sd2-production-tendency-v3"
MAX_MATCHES = 20
MAIN_IDS = tuple(key for key, _ in CATEGORIES if key not in ("transport", "unknown"))
LABELS = dict(CATEGORIES)
LABELS["mechanization"] = "机械化"
RADAR_IDS = ("recon", "infantry", "tank", "anti_tank", "support", "artillery", "anti_air", "air", "mechanization")
ALL_RADAR_IDS = (*MAIN_IDS, "mechanization")
TRANSPORT_COUNT_KEYS = (
    "paid_armed_transport_count", "free_transport_count", "supply_transport_count",
    "other_transport_count", "unknown_transport_count",
)


def _current_catalog() -> Mapping:
    # Keep the dependency lazy: version-compatible summaries need no extra
    # catalog read, and production_summary need not import this module.
    from production_summary import guid_map
    return guid_map()


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large") from None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError(f"{name} must be a finite non-negative integer")
    return int(value)


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _confidence(count: int) -> str:
    if count == 0:
        return "暂无有效样本"
    if count < 5:
        return f"少量样本（{count} 局）"
    if count < 10:
        return f"初步倾向（{count} 局）"
    if count < 20:
        return f"多局观察（{count} 局）"
    return f"样本较充分（{count} 局）"


def _top_labels(shares: Mapping[str, float] | None) -> list[str]:
    if shares is None:
        return []
    ranked = sorted(MAIN_IDS, key=lambda key: (-shares[key], MAIN_IDS.index(key)))
    highest = shares[ranked[0]]
    if highest < 0.20:
        return ["混合投入"]
    chosen = [key for key in ranked if shares[key] >= 0.20 and shares[key] >= highest * 0.5][:2]
    return [LABELS[key] for key in chosen]


def _coverage(known: int, unknown: int) -> dict[str, int | float | None]:
    total = known + unknown
    return {
        "known_main_unit_count": known,
        "unknown_main_unit_count": unknown,
        "ratio": known / total if total else None,
    }


def _mechanization(raw: Mapping | None = None) -> dict[str, Any]:
    """Normalize the separately classified transport-vehicle summary.

    Eligible means paid armed + free + other recognized non-supply vehicles.
    Supply and unknown vehicles are retained as diagnostics, not denominator
    observations. Root's paired-component summarizer owns de-duplication.
    """
    if raw is not None and not isinstance(raw, Mapping):
        raise ValueError("mechanization must be a mapping or null")
    available = raw is not None and raw.get("available", True) is not False
    raw = raw or {}
    counts = {key: _count(raw.get(key, 0), key) for key in TRANSPORT_COUNT_KEYS}
    eligible = counts["paid_armed_transport_count"] + counts["free_transport_count"] + counts["other_transport_count"]
    if "eligible_transport_count" in raw and _count(raw["eligible_transport_count"], "eligible_transport_count") != eligible:
        raise ValueError("eligible transport count must exclude supply and unknown vehicles")
    if "ratio_denominator_count" in raw and _count(raw["ratio_denominator_count"], "ratio_denominator_count") != eligible:
        raise ValueError("mechanization denominator must equal eligible transport count")
    ratio = counts["paid_armed_transport_count"] / eligible if eligible else None
    known = eligible + counts["supply_transport_count"]
    total = known + counts["unknown_transport_count"]
    return {
        **counts,
        "eligible_transport_count": eligible,
        "paid_armed_transport_value": _price(raw.get("paid_armed_transport_value", 0)),
        "ratio_denominator_count": eligible,
        "ratio": ratio,
        "pooled_ratio": ratio,
        "ratio_valid_match_count": 1 if eligible else 0,
        "denominator_label": "已识别的非补给运输车辆",
        "classification_coverage_ratio": known / total if total else None,
        "available": available,
        "aggregation": "single_match",
        "classification_version": raw.get("classification_version"),
        "catalog_approximation": bool(raw.get("catalog_approximation", False)),
    }


def _aggregate_mechanization(tendencies: list[Mapping]) -> dict[str, Any]:
    rows = [_mechanization(tendency.get("mechanization")) for tendency in tendencies]
    raw: dict[str, Any] = {key: sum(row[key] for row in rows) for key in TRANSPORT_COUNT_KEYS}
    value_rows = [row["paid_armed_transport_value"] for row in rows]
    try:
        total_value = math.fsum(value_rows) if all(value is not None for value in value_rows) else None
    except OverflowError:
        total_value = None
    raw["paid_armed_transport_value"] = total_value
    result = _mechanization(raw)
    applicable = [row for row in rows if row["eligible_transport_count"] > 0]
    versions = {str(row["classification_version"]) for row in rows if row["classification_version"] is not None}
    result.update({
        "ratio": math.fsum(row["ratio"] for row in applicable) / len(applicable) if applicable else None,
        "ratio_valid_match_count": len(applicable),
        "available": any(row["available"] for row in rows),
        "aggregation": "equal_applicable_match",
        "classification_version": next(iter(versions)) if len(versions) == 1 else None,
        "catalog_approximation": any(row["catalog_approximation"] for row in rows),
    })
    return result


def _radar(shares: Mapping[str, float], mechanization: Mapping) -> dict[str, float | None]:
    return {
        **{key: 100 * math.sqrt(shares[key]) for key in MAIN_IDS},
        "mechanization": 100 * math.sqrt(mechanization["ratio"]) if mechanization["ratio"] is not None else None,
    }


def _unit_value(rows):
    if any(row["total_value"] is None for row in rows):
        return None
    try:
        value = math.fsum(row["total_value"] for row in rows)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _calculation(result, units):
    """Explain the same effective catalog rows used for the radar calculation."""
    main = [row for row in units if row["role"] == "unit"]
    transports = [row for row in units if row["role"] == "transport"]
    included = [row for row in main if row["included_in_tendency"]]
    count = sum(row["count"] for row in included)
    weighting = result["weighting"]
    categories = []
    for key in MAIN_IDS:
        rows = [row for row in included if row["category"] == key]
        categories.append({"id": key, "label": LABELS[key], "count": sum(row["count"] for row in rows),
                           "value": _unit_value(rows),
                           "share": result["shares"][key] if result["shares"] is not None else None,
                           "radar": result["radar"][key] if result["radar"] is not None else None})
    return {
        "version": "sd2-tendency-calculation-v2", "weighting": weighting,
        "denominator": count if weighting == "count" else _unit_value(included) if weighting == "price" else None,
        "denominator_unit": "个" if weighting == "count" else "点",
        "main_unit_count": sum(row["count"] for row in main), "classified_unit_count": count,
        "requested_main_unit_count": sum(row["requested_count"] for row in main),
        "cancelled_main_unit_count": sum(row["cancelled_count"] for row in main),
        "unknown_unit_count": sum(row["count"] for row in main if not row["included_in_tendency"]),
        "main_unit_value": _unit_value(main),
        "known_main_unit_value": _unit_value([row for row in main if row["total_value"] is not None]),
        "transport_unit_count": sum(row["count"] for row in transports), "transport_value": _unit_value(transports),
        "total_value": _unit_value(units), "categories": categories,
        "units": sorted(units, key=lambda row: (row["role"] != "unit", -row["count"], row["name"], row["guid"] or "")),
        "mechanization": dict(result["mechanization"]),
        "cancellation_adjustment": dict(result["cancellation_adjustment"]),
        "notice": "以生产请求减去已匹配取消后的净数量近似出兵，金额为目录估值。未匹配取消保留原请求；兵种分母只含已分类主单位，运输仅单列金额和机械化指标。",
    }


def _base() -> dict[str, Any]:
    return {
        "model_version": MODEL_VERSION,
        "status": "insufficient",
        "reason": "no_classified_main_units",
        "method_label": "作战倾向近似：尚无可分类的主单位生产请求",
        "weighting": None,
        "shares": None,
        "radar": None,
        "calculation": None,
        "radar_ids": list(RADAR_IDS),
        "all_radar_ids": list(ALL_RADAR_IDS),
        "top_labels": [],
        "match_count": 0,
        "confidence_label": _confidence(0),
        "coverage": _coverage(0, 0),
        "mechanization": _mechanization(),
        "catalog_approximation": False,
        "catalog_version": None,
        "cancellation_request_count": 0,
        "cancellation_adjustment": {"status": "unavailable", "method": "not_reconciled", "matched_count": 0,
                                    "unmatched_count": 0, "duplicate_count": 0, "expired_count": 0, "unsupported_count": 0},
        "cancelled_main_unit_count": 0,
        "used_weights": {"P": 1, "K": 0, "L": 0},
        "exact_metrics": {"P": None, "K": None, "L": None},
        "kill_value": None,
        "loss_value": None,
        "payment_verified": False,
        "deployment_verified": False,
        "limitations": [
            "以主单位请求减已匹配取消后的净数量近似部署投入，未确认支付及成功部署。",
            "主类构成不含运输，未知分类不进入分母，覆盖率单列。",
            "机械化为付费武装运输车辆占已识别非补给运输车辆的独立比例，不加入主类构成分母；补给和未知车辆排除。",
            "取消须关联既有生产组，绑定运输同步扣除；无法匹配及无效请求不按数量盲扣。击杀、损失及交换比不采集。",
            "雷达采用 100×√占比便于阅读，不是实力、胜率或真实占比。",
        ],
    }


def estimate_tendency(summary: Mapping) -> dict[str, Any]:
    """Estimate one player's one-match tendency from summarize_orders().

    Only role=unit rows enter the calculation. All nine classified main-unit
    categories share a denominator; hiding an axis does not renormalize them.
    If any classified main unit lacks a finite price, every classified main
    unit uses count weighting for this match. Unclassified rows affect coverage.
    """
    if not isinstance(summary, Mapping):
        raise ValueError("summary must be a mapping")
    rows = summary.get("units", [])
    if not isinstance(rows, (list, tuple)):
        raise ValueError("summary.units must be a sequence")
    result = _base()
    result["mechanization"] = _mechanization(summary.get("mechanization"))
    compatible = summary.get("mapping_compatible") is True
    result["catalog_approximation"] = not compatible or result["mechanization"]["catalog_approximation"]
    result["catalog_version"] = summary.get("mapping_version")
    result["cancellation_request_count"] = _count(summary.get("cancellation_request_count", 0), "cancellation_request_count")
    result["cancellation_adjustment"] = dict(summary.get("cancellation_adjustment") or {
        **result["cancellation_adjustment"], "unmatched_count": result["cancellation_request_count"]})
    result["cancelled_main_unit_count"] = _count(summary.get("cancelled_main_unit_count", 0), "cancelled_main_unit_count")
    catalog: Mapping | None = None
    catalog_loaded = False
    counts = dict.fromkeys(MAIN_IDS, 0)
    values = dict.fromkeys(MAIN_IDS, 0.0)
    known = 0
    unknown = 0
    price_complete = True
    effective_units = []

    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("summary.units rows must be mappings")
        role = row.get("role")
        if role not in ("unit", "transport"):
            raise ValueError("summary.units role must be unit or transport")
        requested = _count(row.get("requested_count", 0), "requested_count")
        quantity = _count(row.get("net_count", requested), "net_count")
        cancelled = _count(row.get("cancelled_count", 0), "cancelled_count")
        if quantity + cancelled != requested:
            raise ValueError("net quantity plus cancelled quantity must equal requested quantity")
        if requested == 0:
            continue
        category = row.get("category")
        cost = _price(row.get("catalog_cost"))
        name = row.get("name") if isinstance(row.get("name"), str) and row["name"] else "未识别单位"
        catalog_id = row.get("catalog_id")
        # Old-build summaries intentionally hide incompatible catalog rows.
        # A GUID permits using current classification/price as an approximation.
        if not compatible and isinstance(row.get("guid"), str):
            if not catalog_loaded:
                catalog = _current_catalog()
                catalog_loaded = True
                result["catalog_version"] = catalog.get("game_build", result["catalog_version"])
            current = catalog.get("units", {}).get(row["guid"])
            if isinstance(current, Mapping):
                category = current.get("category")
                cost = _price(current.get("cost"))
                name = current.get("name") or name
                catalog_id = current.get("catalog_id")
        if role == "transport":
            category = "transport"
        total_value = quantity * cost if cost is not None else 0 if quantity == 0 else None
        if total_value is not None and not math.isfinite(total_value):
            total_value = None
        effective_units.append({"guid": row.get("guid"), "catalog_id": catalog_id, "name": str(name),
                                "role": role, "category": category if category in LABELS else "unknown",
                                "count": quantity, "requested_count": requested, "cancelled_count": cancelled,
                                "net_count": quantity, "unit_cost": cost, "total_value": total_value,
                                "requested_value": requested * cost if cost is not None else None,
                                "cancelled_value": cancelled * cost if cost is not None else 0 if cancelled == 0 else None,
                                "catalog_approximation": not compatible,
                                "included_in_tendency": role == "unit" and category in MAIN_IDS})
        if role == "transport" or quantity == 0:
            continue
        if category not in MAIN_IDS:
            unknown += quantity
            continue
        known += quantity
        counts[category] += quantity
        if cost is None:
            price_complete = False
        else:
            values[category] += quantity * cost

    result["coverage"] = _coverage(known, unknown)
    result["cancelled_main_unit_count"] = sum(row["cancelled_count"] for row in effective_units if row["role"] == "unit")
    result["price_complete"] = price_complete if known else False
    if known == 0:
        result["calculation"] = _calculation(result, effective_units)
        return result
    try:
        price_total = math.fsum(values.values())
    except OverflowError:
        price_total = math.inf
    if price_complete and price_total == 0:
        result.update({
            "reason": "zero_value", "weighting": "price",
            "method_label": "作战倾向近似：主单位目录总价值为零，暂无价值构成份额",
        })
        result["calculation"] = _calculation(result, effective_units)
        return result
    use_price = price_complete and math.isfinite(price_total) and price_total > 0
    metric = values if use_price else counts
    denominator = price_total if use_price else known
    shares = {key: metric[key] / denominator for key in MAIN_IDS}
    shares["mechanization"] = result["mechanization"]["ratio"]
    result.update({
        "status": "approximate",
        "reason": None,
        "method_label": "作战倾向近似：扣除匹配取消后的主单位目录价值构成（不含运输）" if use_price else "作战倾向近似：扣除匹配取消后的主单位数量构成（不含运输）",
        "weighting": "price" if use_price else "count",
        "shares": shares,
        "radar": _radar(shares, result["mechanization"]),
        "top_labels": _top_labels(shares),
        "match_count": 1,
        "confidence_label": _confidence(1),
    })
    if result["catalog_approximation"]:
        result["limitations"].append("游戏或目录版本未对齐，分类和目录价使用当前 GUID 目录近似。")
    if not use_price:
        result["limitations"].append("该局已分类主单位的目录价缺失或非法，统一按数量计算；零价格本身是有效价格。")
    if result["cancellation_adjustment"].get("unmatched_count"):
        result["limitations"].append("部分取消未能可靠关联既有生产组，原始请求未扣除，净数量仍可能高于实际部署数量。")
    result["calculation"] = _calculation(result, effective_units)
    return result


def aggregate_tendencies(tendencies: Iterable[Mapping]) -> dict[str, Any]:
    """Average the latest 20 effective matches in caller-supplied order.

    Input order is newest first, as defined by the collector. This function
    does not invent replay dates or advance recency when a file is reuploaded.
    Each accepted match contributes equally regardless of its requested value.
    """
    if isinstance(tendencies, (str, bytes, Mapping)):
        raise ValueError("tendencies must be an iterable of match mappings")
    selected = []
    for tendency in tendencies:
        if not isinstance(tendency, Mapping):
            raise ValueError("each tendency must be a mapping")
        if tendency.get("status") == "insufficient":
            continue
        if tendency.get("status") != "approximate":
            raise ValueError("unsupported tendency status")
        raw = tendency.get("shares")
        if not isinstance(raw, Mapping) or set(raw) != set(ALL_RADAR_IDS):
            raise ValueError("tendency shares must contain every main category and the separate mechanization ratio")
        shares = {key: _price(raw[key]) for key in MAIN_IDS}
        if any(value is None for value in shares.values()) or not math.isclose(math.fsum(shares.values()), 1.0, abs_tol=1e-9, rel_tol=0):
            raise ValueError("tendency shares must be finite, non-negative and sum to one")
        mechanical_share = None if raw["mechanization"] is None else _price(raw["mechanization"])
        expected_mechanical_share = _mechanization(tendency.get("mechanization"))["ratio"]
        if (raw["mechanization"] is not None and (mechanical_share is None or mechanical_share > 1)) or (mechanical_share is None) != (expected_mechanical_share is None):
            raise ValueError("mechanization share must be a separate ratio in [0, 1] or null")
        if mechanical_share is not None and not math.isclose(mechanical_share, expected_mechanical_share, abs_tol=1e-9, rel_tol=0):
            raise ValueError("mechanization share must agree with transport metadata")
        selected.append((tendency, shares))
        if len(selected) == MAX_MATCHES:
            break

    result = _base()
    if not selected:
        return result
    n = len(selected)
    average = {key: math.fsum(shares[key] for _, shares in selected) / n for key in MAIN_IDS}
    mechanization = _aggregate_mechanization([tendency for tendency, _ in selected])
    average["mechanization"] = mechanization["ratio"]
    known = sum(_count(tendency["coverage"]["known_main_unit_count"], "known_main_unit_count") for tendency, _ in selected)
    unknown = sum(_count(tendency["coverage"]["unknown_main_unit_count"], "unknown_main_unit_count") for tendency, _ in selected)
    weightings = {tendency.get("weighting") for tendency, _ in selected}
    versions = {str(tendency.get("catalog_version")) for tendency, _ in selected if tendency.get("catalog_version") is not None}
    result.update({
        "status": "approximate",
        "reason": None,
        "method_label": f"作战倾向近似：最近纳入统计的 {n} 局有效对局，每局等权平均",
        "weighting": next(iter(weightings)) if len(weightings) == 1 else "mixed",
        "shares": average,
        "radar": _radar(average, mechanization),
        "top_labels": _top_labels(average),
        "match_count": n,
        "confidence_label": _confidence(n),
        "coverage": _coverage(known, unknown),
        "mechanization": mechanization,
        "mean_match_coverage_ratio": math.fsum(tendency["coverage"]["ratio"] for tendency, _ in selected) / n,
        "catalog_approximation": any(tendency.get("catalog_approximation") for tendency, _ in selected),
        "catalog_version": next(iter(versions)) if len(versions) == 1 else None,
        "cancellation_request_count": sum(_count(tendency.get("cancellation_request_count", 0), "cancellation_request_count") for tendency, _ in selected),
        "cancelled_main_unit_count": sum(_count(tendency.get("cancelled_main_unit_count", 0), "cancelled_main_unit_count") for tendency, _ in selected),
        "cancellation_adjustment": {"status": "aggregate", "method": "per_match",
                                    **{key: sum(_count(tendency.get("cancellation_adjustment", {}).get(key, 0), key)
                                                for tendency, _ in selected)
                                       for key in ("matched_count", "unmatched_count", "duplicate_count", "expired_count", "unsupported_count",
                                                   "ambiguous_count", "unavailable_count", "sequence_matched_count", "queue_matched_count")},
                                    "timing_approximation": any(tendency.get("cancellation_adjustment", {}).get("timing_approximation") for tendency, _ in selected)},
        "window_limit": MAX_MATCHES,
    })
    if result["catalog_approximation"]:
        result["limitations"].append("部分对局的版本未对齐，使用当前 GUID 目录近似。")
    if result["weighting"] == "mixed":
        result["limitations"].append("目录价完整的对局按价值计算，其余对局按数量计算；先逐局归一再等权平均。")
    return result
