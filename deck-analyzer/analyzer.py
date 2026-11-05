"""Decode SD2 deck strings and resolve cards against a local game-data snapshot.

Wire format independently implemented from sd2-nerd-hq/sd2-utilities
src/parser/deckParser.ts. All fields are MSB first; each of the five header
values has a five-bit length prefix. Unit IDs on the wire are offset by one.
The card count field is a multiplicity, not a deployable-unit quantity.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter
from functools import lru_cache
import json
import math
from pathlib import Path
import re


CATALOG_PATH = Path(__file__).with_name("data") / "catalog.json"
MAX_CODE_LENGTH = 8192
MAX_CARDS = 512
PHASES = ("A", "B", "C")
DEFAULT_CATEGORIES = (
    ("recon", "侦察"), ("infantry", "步兵"), ("tank", "坦克"),
    ("support", "支援"), ("anti_tank", "反坦克"), ("anti_air", "防空"),
    ("artillery", "火炮"), ("air", "飞机"), ("defense", "防御"),
)
INCOME_NAMES = {0: "平衡", 1: "先锋", 2: "独行侠", 3: "巨人", 4: "平稳", 5: "V 型胜利"}

# A real SD2 code published in sd2-utilities/src/parser/deckParser.spec.ts.
EXAMPLE_CODE = (
    "DCTrzRBiEJiBUoACi14ACD8oACpj5jSqEoACqh4rTAn4ACIngACgn4ACgjbcKJtQgqoRpxyhyAAChroAELD4AEAbZxyIJYrSgR6OCEnIACIbRxyIZBjSJj5jSA/grSkNYACCxIACIRpx0DDoACA5orSIbIrSQfgACCqwACCrgACobZxzAbZxwA=="
)


class DeckCodeError(ValueError):
    """The supplied string is not a complete, supported SD2 deck encoding."""


class CatalogError(RuntimeError):
    """The locally installed catalog cannot be read."""


class _BitReader:
    def __init__(self, raw):
        self.bits = "".join(f"{byte:08b}" for byte in raw)
        self.position = 0

    def read(self, width):
        if not 1 <= width <= 31:
            raise DeckCodeError("卡组字段位宽无效。")
        end = self.position + width
        if end > len(self.bits):
            raise DeckCodeError("卡组代码不完整，字段已被截断。")
        value = int(self.bits[self.position:end], 2)
        self.position = end
        return value

    def variable(self):
        return self.read(self.read(5))


def normalize_code(code):
    """Accept pasted whitespace and the conventional optional @ prefix."""
    if not isinstance(code, str):
        raise DeckCodeError("请输入卡组代码。")
    if len(code) > MAX_CODE_LENGTH:
        raise DeckCodeError("卡组代码过长。")
    value = re.sub(r"\s+", "", code)
    if value.startswith("@"):
        value = value[1:]
    if not value or len(value) < 8:
        raise DeckCodeError("请输入完整的 Steel Division 2 卡组代码。")
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        raise DeckCodeError("卡组代码含有无效字符。")
    if "=" in value and len(value) % 4:
        raise DeckCodeError("卡组代码的 Base64 填充无效。")
    value += "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise DeckCodeError("卡组代码的 Base64 编码无效。") from None
    return base64.b64encode(raw).decode("ascii"), raw


def decode_deck(code):
    """Return wire values only; this function needs no game data or network."""
    code, raw = normalize_code(code)
    reader = _BitReader(raw)
    header = [reader.variable() for _ in range(5)]
    version, modded, division_id, entry_count, income_id = header
    if version != 1:
        raise DeckCodeError(f"尚不支持版本 {version} 的卡组代码。")
    if modded not in (0, 1):
        raise DeckCodeError("卡组模组标记无效。")
    if not 0 <= entry_count <= MAX_CARDS:
        raise DeckCodeError("卡组条目数量超出合理范围。")
    widths = [reader.read(5) for _ in range(4)]
    if any(not 1 <= width <= 31 for width in widths):
        raise DeckCodeError("卡组单位字段位宽无效。")
    count_width, phase_width, xp_width, unit_width = widths
    entries = []
    total_cards = 0
    for index in range(entry_count):
        count = reader.read(count_width)
        phase = reader.read(phase_width)
        veterancy = reader.read(xp_width)
        unit_id = reader.read(unit_width) - 1
        transport_id = reader.read(unit_width) - 1
        if not 1 <= count <= MAX_CARDS or total_cards + count > MAX_CARDS:
            raise DeckCodeError("卡组卡片数量无效。")
        if phase not in (0, 1, 2):
            raise DeckCodeError("卡组包含无效的作战阶段。")
        if veterancy not in (0, 1, 2):
            raise DeckCodeError("卡组包含无效的经验等级。")
        if unit_id < 0:
            raise DeckCodeError("卡组包含空单位。")
        total_cards += count
        entries.append({"source_index": index, "count": count, "phase": phase,
                        "veterancy": veterancy, "unit_id": unit_id,
                        "transport_id": transport_id})
    trailing = reader.bits[reader.position:]
    if len(trailing) > 7 or "1" in trailing:
        raise DeckCodeError("卡组末尾含有未识别的数据，请复制完整的原版 SD2 卡组代码。")
    return {"code": code, "version": version, "modded": bool(modded),
            "division_id": division_id, "income_id": income_id,
            "entry_count": entry_count, "card_count": total_cards, "entries": entries}


@lru_cache(maxsize=1)
def load_catalog():
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        if (catalog.get("schema") != 1 or not isinstance(catalog.get("units"), dict)
                or not isinstance(catalog.get("divisions"), dict)
                or not catalog["units"] or not catalog["divisions"]):
            raise ValueError("unexpected catalog structure")
    except (OSError, ValueError, TypeError, AttributeError):
        raise CatalogError("本地卡组数据未安装或已损坏，请重新生成 catalog.json。") from None
    return catalog


def catalog_info():
    catalog = load_catalog()
    return {"name": "deck-analyzer", "schema": 1,
            "metadata": catalog.get("metadata", {}),
            "unit_count": len(catalog["units"]),
            "division_count": len(catalog["divisions"]),
            "example_code": catalog.get("example_code", EXAMPLE_CODE)}


def _sum_known(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(values)


def _quantity(rule, phase, veterancy):
    if not rule:
        return None
    available = rule.get("availability")
    multipliers = rule.get("xp_multipliers")
    if (not isinstance(available, list) or not isinstance(multipliers, list)
            or len(available) <= phase or len(multipliers) <= veterancy):
        return None
    # The snapshot's fractional multipliers encode nearest-integer quantities.
    # Avoid Python's banker's rounding on exact halves.
    return math.floor(available[phase] * multipliers[veterancy] + 0.5)


def _unit_view(identifier, unit):
    return {"id": identifier, "name": unit.get("name", f"未知单位 #{identifier}"),
            "descriptor": unit.get("descriptor"), "cost": unit.get("cost"),
            "nation": unit.get("nation"), "image_url": unit.get("image_url")}


def analyze_deck(code, catalog=None):
    """Resolve a deck into nine category rows, preserving unknown numeric IDs."""
    decoded = decode_deck(code)
    catalog = load_catalog() if catalog is None else catalog
    units = catalog["units"]
    divisions = catalog["divisions"]
    division_id = decoded["division_id"]
    division_data = divisions.get(str(division_id), {})
    warnings = []
    if not division_data:
        warnings.append(f"数据中没有师 #{division_id}，已保留原始单位信息；动员点与单位数量暂不可用。")
    if decoded["modded"]:
        warnings.append("这是模组卡组。名称仅按原版 ID 对照，价格、单位数量与动员点不可核实。")
    division = {key: division_data.get(key) for key in
                ("name", "faction", "nation", "rating", "activation_limit", "image_url")}
    division.update(id=division_id, name=division_data.get("name", f"未知师 #{division_id}"))
    if decoded["modded"]:
        division["activation_limit"] = None
    income_options = division_data.get("income", [])
    if isinstance(income_options, dict):
        income_data = income_options.get(str(decoded["income_id"]), {})
    else:
        income_data = next((item for item in income_options
                            if item.get("id") == decoded["income_id"]), {})
    income = {"id": decoded["income_id"],
              "name": income_data.get("name", INCOME_NAMES.get(decoded["income_id"],
                                      f"未知部署类型 #{decoded['income_id']}")),
              "phases": None if decoded["modded"] else income_data.get("phases")}
    if decoded["income_id"] not in INCOME_NAMES and not income_data:
        warnings.append("当前数据中没有该部署类型的收益信息。")
    category_names = dict(DEFAULT_CATEGORIES)
    category_names.update({item["id"]: item["name"] for item in catalog.get("categories", [])})
    category_names["unknown"] = "未知类别"
    cards = []
    unknown_units = set()
    rules = division_data.get("units", {})
    unit_card_counts = Counter()
    for entry in decoded["entries"]:
        identifier = entry["unit_id"]
        unit = units.get(str(identifier), {})
        rule = rules.get(str(identifier))
        card_warnings = []
        if not unit:
            unknown_units.add(identifier)
            card_warnings.append(f"未识别单位 #{identifier}；可能需要更新数据。")
        if division_data and not rule:
            card_warnings.append("当前师的数据中没有该单位，可能来自其他版本或模组。")
        category = unit.get("category", "unknown")
        if category not in category_names:
            category = "unknown"
        phase = entry["phase"]
        xp = entry["veterancy"]
        quantity = None if decoded["modded"] else _quantity(rule, phase, xp)
        if quantity == 0:
            card_warnings.append("当前数据不支持此单位的阶段／经验组合。")
        transport = None
        transport_id = entry["transport_id"]
        if transport_id >= 0:
            transport_data = units.get(str(transport_id), {})
            transport = _unit_view(transport_id, transport_data)
            if not transport_data:
                card_warnings.append(f"未识别运输单位 #{transport_id}。")
            if rule and "transports" in rule and transport_id not in rule["transports"]:
                card_warnings.append("当前数据中该运输单位不适用于此卡片。")
        cost = None if decoded["modded"] else unit.get("cost")
        if decoded["modded"] and transport:
            transport["cost"] = None
        total_cost = _sum_known([cost, transport.get("cost") if transport else 0])
        for copy_index in range(entry["count"]):
            cards.append({**_unit_view(identifier, unit), "index": len(cards),
                          "source_index": entry["source_index"], "copy_index": copy_index,
                          "unit_id": identifier, "category": category, "cost": cost,
                          "total_cost": total_cost, "phase": PHASES[phase], "phase_index": phase,
                          "veterancy": xp, "quantity": quantity, "transport": transport,
                          "warnings": list(card_warnings)})
        unit_card_counts[identifier] += entry["count"]
    for identifier, count in unit_card_counts.items():
        rule = rules.get(str(identifier), {})
        maximum = rule.get("max_cards")
        if maximum is not None and count > maximum:
            warnings.append(f"{units.get(str(identifier), {}).get('name', '#' + str(identifier))} "
                            f"使用了 {count} 张卡，超过当前数据中的 {maximum} 张上限。")
    categories = []
    slots_by_category = division_data.get("slots", {})
    for category, name in category_names.items():
        group = sorted((card for card in cards if card["category"] == category),
                       key=lambda card: (card["phase_index"], card["index"]))
        if category == "unknown" and not group:
            continue
        slots = slots_by_category.get(category)
        used = len(group)
        points = None
        if slots is not None and not decoded["modded"] and used <= len(slots):
            points = sum(slots[:used])
        if slots is not None and used > len(slots):
            warnings.append(f"{name}使用了 {used} 个卡槽，超过当前数据中的 {len(slots)} 个。")
        categories.append({"id": category, "name": name, "cards": group,
                           "card_count": used, "unit_count": _sum_known(c["quantity"] for c in group),
                           "slots": slots, "used_slots": used, "activation_points": points})
    activation = _sum_known(category["activation_points"] for category in categories)
    limit = division.get("activation_limit")
    if activation is not None and limit is not None and activation > limit:
        warnings.append(f"动员点 {activation} 超过当前上限 {limit}。")
    card_warning_count = sum(bool(card["warnings"]) for card in cards)
    if card_warning_count:
        warnings.append(f"{card_warning_count} 张卡片与当前数据不完全匹配，详情见卡片提示。")
    summary = {"cards": len(cards), "entries": decoded["entry_count"],
               "units": _sum_known(card["quantity"] for card in cards),
               "activation_points": activation, "activation_limit": limit,
               "unknown_units": len(unknown_units),
               "phases": {phase: {"cards": sum(card["phase"] == phase for card in cards),
                                  "units": _sum_known(card["quantity"] for card in cards
                                                     if card["phase"] == phase)} for phase in PHASES}}
    return {"code": decoded["code"], "version": decoded["version"], "modded": decoded["modded"],
            "division": division, "income": income, "cards": cards, "categories": categories,
            "summary": summary, "activation": {"used": activation, "limit": limit},
            "warnings": warnings, "metadata": catalog.get("metadata", {})}
