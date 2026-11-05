"""Reproducible player-style reference model; not a production collector.

Only Python 3.10+ standard-library modules are required. Values are deployment
costs, enemy destruction values credited to the attacking unit's category, and
own loss values. An omitted category in a PRESENT metric means an observed zero;
an absent metric, a null metric, or any null category value means missing data.
Callers must explicitly supply ages and a trusted, comparable cohort baseline.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

CATEGORIES = (
    ("recon", "侦察"),
    ("infantry", "步兵"),
    ("tank", "坦克"),
    ("support", "支援"),
    ("anti_tank", "反坦克"),
    ("anti_air", "防空"),
    ("artillery", "炮兵"),
    ("air", "航空"),
    ("defense", "防御"),
    ("transport", "运输"),
    ("unknown", "未知"),
)
CATEGORY_IDS = tuple(category for category, _ in CATEGORIES)
RADAR_IDS = ("infantry", "tank", "anti_tank", "support", "artillery", "anti_air")
WEIGHTS = {"P": 0.70, "K": 0.20, "L": 0.10}
HALF_LIFE_DAYS = 14.0
MAX_AGE_DAYS = 90.0
MAX_MATCHES = 20
PRIOR_MATCHES = 5.0
MIN_CLASSIFICATION_COVERAGE = 0.95


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite, non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return number


def _zeros() -> dict[str, float]:
    return dict.fromkeys(CATEGORY_IDS, 0.0)


def _metric(value: object, name: str) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a category mapping or null")
    unexpected = set(value) - set(CATEGORY_IDS)
    if unexpected:
        raise ValueError(f"{name} has unsupported categories: {sorted(unexpected)}")
    # Validate every supplied non-null value even if another value is missing.
    parsed = {
        category: None if amount is None else _number(amount, f"{name}.{category}")
        for category, amount in value.items()
    }
    if any(amount is None for amount in parsed.values()):
        return None
    result = _zeros()
    result.update(parsed)
    return result


def _baseline(value: object) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(CATEGORY_IDS):
        raise ValueError("baseline must explicitly contain all 11 categories")
    result = {category: _number(value[category], f"baseline.{category}") for category in CATEGORY_IDS}
    if not math.isclose(math.fsum(result.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("baseline shares must sum to 1")
    return result


def _shares(metric: dict[str, float] | None) -> dict[str, float] | None:
    if metric is None:
        return None
    total = math.fsum(metric.values())
    if not math.isfinite(total):
        raise ValueError("metric total overflowed")
    if total == 0:
        return _zeros()
    return {category: metric[category] / total for category in CATEGORY_IDS}


def _omitted(shares: dict[str, float] | None) -> list[dict]:
    return [
        {"id": category, "label": label, "share": None if shares is None else shares[category]}
        for category, label in CATEGORIES
        if category not in RADAR_IDS
    ]


def score_match(metricsByP_K_L: Mapping) -> dict:
    """Compute observed composition for one complete, finished player-match.

    Required metric keys are P, K, and L. Missing tactical metrics never become
    observed zero. Lifecycle/identity/patch compatibility checks belong to the
    future collector; passing numbers here does not establish those facts.
    """
    if not isinstance(metricsByP_K_L, Mapping):
        raise ValueError("match metrics must be a mapping")
    unexpected = set(metricsByP_K_L) - set(WEIGHTS)
    if unexpected:
        raise ValueError(f"unsupported metrics: {sorted(unexpected)}")
    metrics = {key: _metric(metricsByP_K_L.get(key), key) for key in WEIGHTS}
    metric_shares = {key: _shares(value) for key, value in metrics.items()}
    totals = {key: None if value is None else math.fsum(value.values()) for key, value in metrics.items()}
    missing = [key for key, value in metrics.items() if value is None]
    coverage = {
        key: None if value is None else (1.0 if totals[key] == 0 else 1.0 - value["unknown"] / totals[key])
        for key, value in metrics.items()
    }
    result = {
        "status": "incomplete",
        "complete": not missing,
        "eligible": False,
        "metrics": metrics,
        "totals": totals,
        "metricShares": metric_shares,
        "purchaseShares": metric_shares["P"],
        "shares": None,
        "metricWeights": None,
        "missingMetrics": missing,
        "knownZeroMetrics": [key for key, total in totals.items() if total == 0],
        "classificationCoverage": coverage,
    }
    if missing:
        return result
    if totals["P"] == 0:
        result["status"] = "no_deployment"
        return result
    active_weight = math.fsum(WEIGHTS[key] for key, total in totals.items() if total > 0)
    applied = {key: WEIGHTS[key] / active_weight if totals[key] > 0 else 0.0 for key in WEIGHTS}
    composition = {
        category: math.fsum(applied[key] * metric_shares[key][category] for key in WEIGHTS)
        for category in CATEGORY_IDS
    }
    result.update(shares=composition, metricWeights=applied)
    if any(coverage[key] < MIN_CLASSIFICATION_COVERAGE for key in WEIGHTS):
        result["status"] = "insufficient_classification"
        return result
    result.update(status="complete", eligible=True)
    return result


def _sum_metrics(results: Sequence[dict]) -> dict:
    return {
        key: None if any(result["metrics"][key] is None for result in results)
        else {
            category: math.fsum(result["metrics"][key][category] for result in results)
            for category in CATEGORY_IDS
        }
        for key in WEIGHTS
    }


def score_profile(matches: Sequence[Mapping], baseline: Mapping | None = None, ageDays: Sequence | None = None) -> dict:
    """Score latest 20 usable matches from at most 90 days, with a 14-day half-life.

    `ageDays` is required for non-empty input; no timestamps are fabricated.
    `baseline=None` preserves observed raw shares but returns no relative radar.
    Zero-baseline axes are also null, not an invented average or perfect score.
    The single five-match prior is applied once, before relative-score mapping.
    """
    if not isinstance(matches, Sequence) or isinstance(matches, (str, bytes)):
        raise ValueError("matches must be a sequence")
    cohort = _baseline(baseline)
    if ageDays is None:
        if matches:
            raise ValueError("ageDays is required for non-empty match input")
        ageDays = []
    if not isinstance(ageDays, Sequence) or isinstance(ageDays, (str, bytes)) or len(ageDays) != len(matches):
        raise ValueError("ageDays must contain exactly one age for each match")
    ages = [_number(age, f"ageDays[{index}]") for index, age in enumerate(ageDays)]
    scored = [score_match(match) for match in matches]
    candidates, excluded = [], []
    for index in sorted(range(len(matches)), key=lambda item: (ages[item], item)):
        if ages[index] > MAX_AGE_DAYS:
            excluded.append({"index": index, "reason": "older_than_90_days"})
        elif not scored[index]["eligible"]:
            excluded.append({"index": index, "reason": scored[index]["status"]})
        elif len(candidates) >= MAX_MATCHES:
            excluded.append({"index": index, "reason": "match_limit"})
        else:
            candidates.append(index)
    # Preserve purchase-only diagnostics when no complete tactical match exists.
    diagnostic_indices = sorted(
        (index for index, age in enumerate(ages) if age <= MAX_AGE_DAYS),
        key=lambda index: (ages[index], index),
    )[:MAX_MATCHES]
    included = [scored[index] for index in candidates]
    diagnostics = included if included else [scored[index] for index in diagnostic_indices]
    raw_metrics = _sum_metrics(diagnostics)
    missing = sorted({key for result in diagnostics for key in result["missingMetrics"]})
    result = {
        "status": "no_valid_matches",
        "complete": bool(included),
        "relativeStatus": "unavailable",
        "shares": None,
        "radar": dict.fromkeys(CATEGORY_IDS),
        "metrics": raw_metrics,
        "totals": {key: None if value is None else math.fsum(value.values()) for key, value in raw_metrics.items()},
        "purchaseShares": _shares(raw_metrics["P"]),
        "matchCount": len(included),
        "inputMatchCount": len(matches),
        "selectedIndices": candidates,
        "n_eff": 0.0,
        "stability": 0.0,
        "priorMatches": PRIOR_MATCHES,
        "baseline": cohort,
        "missingMetrics": missing,
        "excludedMatches": excluded,
        "omittedShares": _omitted(None),
    }
    if not included:
        return result
    time_weights = [2.0 ** (-ages[index] / HALF_LIFE_DAYS) for index in candidates]
    weight_sum = math.fsum(time_weights)
    n_eff = weight_sum ** 2 / math.fsum(weight ** 2 for weight in time_weights)
    stability = n_eff / (n_eff + PRIOR_MATCHES)
    observed = {
        category: math.fsum(weight * match["shares"][category] for weight, match in zip(time_weights, included)) / weight_sum
        for category in CATEGORY_IDS
    }
    purchase_shares = {
        category: math.fsum(weight * match["purchaseShares"][category] for weight, match in zip(time_weights, included)) / weight_sum
        for category in CATEGORY_IDS
    }
    radar = dict.fromkeys(CATEGORY_IDS)
    relative_status = "no_baseline"
    if cohort is not None:
        for category in CATEGORY_IDS:
            if cohort[category] > 0:
                estimate = stability * observed[category] + (1.0 - stability) * cohort[category]
                radar[category] = 100.0 * estimate / (estimate + cohort[category])
        relative_status = "available" if all(cohort[category] > 0 for category in RADAR_IDS) else "partial_baseline"
    result.update(
        status="ready" if n_eff >= 5.0 else "provisional",
        relativeStatus=relative_status,
        shares=observed,
        radar=radar,
        purchaseShares=purchase_shares,
        n_eff=n_eff,
        stability=stability,
        omittedShares=_omitted(observed),
    )
    return result


def build_demo(fixtures: Mapping) -> dict:
    """Expand explicit synthetic templates and emit 1/5/20-match comparisons."""
    if fixtures.get("source") != "synthetic":
        raise ValueError("this demo generator accepts synthetic fixtures only")
    cohort = _baseline(fixtures["baseline"])
    profiles = []
    for profile in fixtures["profiles"]:
        count = profile["matchCount"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 20:
            raise ValueError("demo profiles require at least 20 synthetic matches")
        matches = [profile["matchTemplate"] for _ in range(count)]
        ages = [profile["ageDays"] for _ in range(count)]
        profiles.append({
            "id": profile["id"], "name": profile["name"], "matches": matches, "ageDays": ages,
            "resultsByCount": {
                str(amount): score_profile(matches[:amount], cohort, ages[:amount])
                for amount in (1, 5, 20)
            },
        })
    validation = []
    for fixture in fixtures.get("validationFixtures", []):
        validation.append({
            "id": fixture["id"], "name": fixture["name"],
            "match": fixture["match"],
            "result": score_profile([fixture["match"]], cohort, [fixture["ageDays"]]),
        })
    return {
        "schemaVersion": 1,
        "source": "synthetic",
        "notice": "全部为合成示例，非真实玩家数据；基准亦为虚构演示基准。",
        "categories": [{"id": category, "label": label, "radar": category in RADAR_IDS} for category, label in CATEGORIES],
        "weights": dict(WEIGHTS),
        "baseline": cohort,
        "parameters": {"halfLifeDays": HALF_LIFE_DAYS, "maxAgeDays": MAX_AGE_DAYS, "maxMatches": MAX_MATCHES, "priorMatches": PRIOR_MATCHES},
        "profiles": profiles,
        "validationFixtures": validation,
    }


def main() -> None:
    directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=directory / "demo_profiles.json")
    parser.add_argument("--output", type=Path, default=directory / "demo_results.js")
    args = parser.parse_args()
    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    payload = build_demo(fixtures)
    args.output.write_text(
        "// Generated by style_model.py. SYNTHETIC DEMO ONLY; not real player records.\n"
        + "window.PLAYER_STYLE_DEMO = " + json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + ";\n",
        encoding="utf-8",
    )
    print(f"Generated {args.output.name}: {len(payload['profiles'])} synthetic profiles; no production data.")


if __name__ == "__main__":
    main()
