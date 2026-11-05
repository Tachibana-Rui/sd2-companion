"""Analyze real SD2 ESAV v3 files without executing replay contents."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import struct
import sys
from functools import lru_cache
from pathlib import Path

from replay_stream import ReplayStreamError, inspect_stream
from production_summary import guid_map, reconcile_cancellations, summarize_orders, summarize_mechanization
from replay_identity import allowed_deck_guids, bind_players
from tendency_model import estimate_tendency

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else ROOT.parent
PARSER_VERSION = "sd2-real-replay-v6"
MAX_REPLAY_BYTES = 128 * 1024 * 1024
MAX_HEADER_BYTES = 2 * 1024 * 1024
MAX_PLAYERS = 64
BLOCK = struct.Struct(">4sQI")
MODES = {(2, 2): "征服", (2, 1): "歼灭", (3, 2): "近距离征服", (3, 1): "近距离歼灭", (5, 2): "突破"}


class ReplayError(ValueError):
    """A safe diagnostic; never includes file contents or private metadata."""


def _text(value, limit=200):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if ord(char) >= 32)[:limit]


def _integer(value, minimum=0, maximum=2**64 - 1):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"-?[0-9]{1,20}", value):
        return None
    parsed = int(value)
    return parsed if minimum <= parsed <= maximum else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError("录像 JSON 存在重复字段。")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _catalog():
    path = WORKSPACE / "deck-analyzer/data/catalog.json"
    raw = path.read_bytes()
    value = json.loads(raw)
    return value, hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def _deck_analyzer():
    spec = importlib.util.spec_from_file_location("sd2_stats_deck_analyzer", WORKSPACE / "deck-analyzer/analyzer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings(game):
    """Per-match economy settings snapshot taken from the replay identity header.

    The header stores every lobby variable as text. Only the values that the
    economy estimate needs are kept, and an unreadable value stays null instead
    of being guessed.
    """
    return {
        "income_rate": _integer(game.get("IncomeRate"), 0, 5),
        "init_money": _integer(game.get("InitMoney"), 0, 100000),
        "phase_a_seconds": _integer(game.get("PhaseADuration"), -1, 86400),
        "phase_b_seconds": _integer(game.get("PhaseBDuration"), -1, 86400),
        "deployment_seconds": _integer(game.get("DeploiementTimeMax"), -1, 86400),
        "loading_seconds": _integer(game.get("LoadingTimeMax"), -1, 86400),
        "victory_cond": _integer(game.get("VictoryCond"), 0, 99),
        "combat_rule": _integer(game.get("CombatRule"), 0, 99),
        "max_team_size": _integer(game.get("MaxTeamSize"), 0, 64),
        "time_limit": _integer(game.get("TimeLimit"), 0, 86400 * 7),
    }


def _map_name(value):
    try:
        maps = json.loads((WORKSPACE / "map-selector/maps.json").read_text(encoding="utf-8"))
        if isinstance(maps, dict):
            maps = maps.get("maps", [])
        for item in maps:
            identifier = item.get("id", "")
            if value == identifier or any(value == identifier + suffix for suffix in ("_BKT", "_CQC", "_DEST", "_DEST_CQC")):
                return item.get("name", value)
    except (OSError, ValueError, TypeError):
        pass
    return value


def _avatar(value):
    result = {"provider": None, "platform_id": None}
    if not isinstance(value, str):
        return result
    match = re.fullmatch(r"VirtualData/(Steam|GOG)GamerPicture/([0-9]{1,20})", value)
    if match:
        platform_id = match.group(2)
        if match.group(1) == "Steam" and (len(platform_id) != 17 or not 76561197960265728 <= int(platform_id) <= 76561202255233023):
            return result
        if 0 < int(platform_id) <= 18446744073709551615 and not platform_id.startswith("0"):
            result.update(provider="steam" if match.group(1) == "Steam" else "gog", platform_id=platform_id)
    return result


def _deck(value, game_version, catalog_version):
    empty = {"status": "unavailable", "division_name": None, "division_id": None, "faction": None,
             "categories": [], "income_id": None, "income_name": None, "income_phases": None}
    if not isinstance(value, str) or not value:
        return empty
    try:
        parsed = _deck_analyzer().analyze_deck(value)
    except (ValueError, OSError, ImportError):
        return {**empty, "status": "invalid"}
    compatible = game_version == catalog_version and not parsed.get("modded")
    categories = []
    for group in parsed["categories"]:
        cards = group["cards"]
        priced = compatible and all(card.get("quantity") is not None and card.get("total_cost") is not None for card in cards)
        categories.append({"id": group["id"], "label": group["name"], "card_count": group["card_count"],
                           "available_unit_count": group["unit_count"] if compatible else None,
                           "total_available_cost": sum(card["quantity"] * card["total_cost"] for card in cards) if priced else None})
    division = parsed["division"]
    income = parsed.get("income") or {}
    phases = income.get("phases")
    if not compatible or not isinstance(phases, list) or len(phases) != 3:
        phases = None
    return {"status": "available" if compatible else "catalog_version_mismatch",
            "division_name": division.get("name"), "division_id": division.get("id"),
            "faction": division.get("faction"), "categories": categories,
            "income_id": income.get("id"), "income_name": income.get("name"),
            "income_phases": phases if all(isinstance(item, int) and item >= 0 for item in phases or []) else None,
            "catalog_compatible": compatible, "catalog_version": catalog_version,
            "notice": "携带卡组构成和可用量，不是实际购买或杀伤。"}


AI_TAKEOVER_GAP_SECONDS = 60


def _control(activity, stream, completed):
    """Flag players whose real orders stopped well before the battle ended.

    A disconnected player's units keep fighting under AI control while no
    further production commands are recorded, so unit-level P/K is not
    applicable to that player even though the world simulation continues.
    """
    last = activity.get("last_nonempty_packet_tick") if isinstance(activity, dict) else None
    end_tick = stream.get("battle_ended_tick")
    rate = stream.get("tick_rate")
    if not completed or type(last) is not int or type(end_tick) is not int or type(rate) is not int or rate <= 0:
        return {"status": "unverified", "last_input_tick": None, "gap_seconds": None,
                "threshold_seconds": AI_TAKEOVER_GAP_SECONDS}
    gap = max(0, end_tick - last) / rate
    suspected = bool(activity.get("battle_packet_count")) and gap >= AI_TAKEOVER_GAP_SECONDS
    return {"status": "ai_takeover_suspected" if suspected else "input_until_battle_end",
            "last_input_tick": last, "gap_seconds": round(gap, 1),
            "threshold_seconds": AI_TAKEOVER_GAP_SECONDS}


def _bind_slots(slots, stream, game_version, catalog_hash):
    """Official GameSetup rank plus all production/owner cross-checks."""
    membership = None
    if stream.get("production_orders"):
        try:
            membership = allowed_deck_guids(slots, guid_map(), _deck_analyzer().decode_deck)
        except ValueError:
            pass
    return bind_players(slots, stream, allowed_unit_guids=membership)


def attach_tendencies(report):
    """Add useful deployment estimates without changing precise P/K/L facts."""
    stream = report.get("format", {}).get("command_stream", {})
    raw_orders = stream.get("production_orders") if isinstance(stream, dict) else None
    if isinstance(raw_orders, list) and raw_orders:
        # Cancellation IDs are global, including other players' productions.
        # Rebuild once in global order, then partition by verified source slot;
        # archived streams work even after the original RPL rotates away.
        raw_cancels = stream.get("cancellation_orders", [])
        if isinstance(raw_cancels, list):
            orders, cancels, reconciliation = reconcile_cancellations(
                raw_orders, raw_cancels, tick_rate=stream.get("tick_rate"),
                battle_started_tick=stream.get("battle_started_tick"),
                game_version=report.get("match", {}).get("version"),
                stream_complete=stream.get("production_decoding_status") == "complete_production_request_stream")
            game_version = report.get("match", {}).get("version")
            catalog_hash = report.get("catalog", {}).get("sha256")
            report["production_orders"] = summarize_orders(orders, game_version, catalog_hash, cancels, reconciliation=reconciliation)
            for player in report.get("players", []):
                source_slot = player.get("stream_source_slot")
                if source_slot is not None:
                    player.setdefault("stats", {})["production_orders"] = summarize_orders(
                        [order for order in orders if order.get("source_slot") == source_slot],
                        game_version, catalog_hash,
                        [cancel for cancel in cancels if cancel.get("source_slot") == source_slot],
                        reconciliation=reconciliation)
            report.setdefault("format", {})["production_cancellation"] = reconciliation
    available = False
    for player in report.get("players", []):
        stats = player.setdefault("stats", {})
        summary = stats.get("production_orders") or {}
        summary["mechanization"] = summarize_mechanization(summary)
        tendency = estimate_tendency(summary)
        stats["tendency"] = tendency
        if tendency["status"] == "approximate":
            available = True
            stats.update(status="approximate_tendency", radar=tendency["radar"],
                         shares=tendency["shares"], reason=tendency["method_label"])
        elif stats.get("status") == "approximate_tendency":
            stats.update(status="unavailable", radar=None, shares=None, reason="暂无可分类的主单位部署记录。")
    report.setdefault("capabilities", {})["approximate_tendency"] = available
    return report


def parse_replay(data: bytes, name="replay.rpl3") -> dict:
    """Validate the envelope and the entire framed stream, then normalize identity."""
    if not isinstance(data, bytes) or len(data) < 32:
        raise ReplayError("文件过短，不能作为完整录像分析。")
    if len(data) > MAX_REPLAY_BYTES:
        raise ReplayError("录像超过 128 MiB 分析上限。")
    magic, version, declared_size, checksum = struct.unpack_from(">4sIII", data)
    if magic != b"ESAV":
        raise ReplayError("文件不是受支持的 ESAV 录像。")
    if version != 3:
        raise ReplayError("当前仅支持经过核验的 ESAV v3 录像。")
    if declared_size != len(data):
        raise ReplayError("录像声明长度与实际长度不一致，文件可能被截断或仍在写入。")
    offset, sections, header = 16, [], None
    for _ in range(16):
        if offset + BLOCK.size > len(data):
            raise ReplayError("录像头部块被截断。")
        tag, length, reserved = BLOCK.unpack_from(data, offset)
        if reserved or length > MAX_HEADER_BYTES or offset + BLOCK.size + length > len(data):
            raise ReplayError("录像头部长度或保留字段无效。")
        payload_offset = offset + BLOCK.size
        if tag == b"rply":
            try:
                header = json.loads(data[payload_offset:payload_offset + length].decode("utf-8"),
                                    object_pairs_hook=_unique_object,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ReplayError("录像 JSON 数值无效。")))
            except (UnicodeError, ValueError, RecursionError) as error:
                raise ReplayError("录像身份头 JSON 无效。") from error
            header_offset, header_length = payload_offset, length
            offset = payload_offset + length
            break
        if tag != b"modd":
            raise ReplayError("不支持此录像头部块布局。")
        sections.append({"tag": "modd", "payload_bytes": length})
        offset = payload_offset + length
    if not isinstance(header, dict) or not isinstance(header.get("game"), dict):
        raise ReplayError("录像缺少游戏身份头。")
    game = header["game"]
    slots = []
    for key, value in header.items():
        match = re.fullmatch(r"player_([0-9]{1,10})", key)
        if match:
            if not isinstance(value, dict):
                raise ReplayError("录像玩家头格式无效。")
            number = int(match.group(1))
            if number > 2**32 - 1:
                raise ReplayError("录像玩家头编号超出范围。")
            slots.append((number, value))
    slots.sort(key=lambda item: item[0])
    if not 1 <= len(slots) <= MAX_PLAYERS or len({slot for slot, _ in slots}) != len(slots):
        raise ReplayError("录像玩家数量或槽位无效。")
    tick_rate = _integer(game.get("TickRate"), 1, 1000)
    try:
        stream = inspect_stream(data, offset, tick_rate=tick_rate, expected_player_count=len(slots))
    except (ReplayStreamError, ValueError) as error:
        raise ReplayError("录像指令流校验失败：" + str(error)) from error
    catalog, catalog_hash = _catalog()
    catalog_version = _integer(catalog.get("metadata", {}).get("verified_game_build"))
    game_version = _integer(game.get("Version"))
    completion = stream["completion_evidence"]
    completed = completion != "unverified"
    identity_binding = _bind_slots(slots, stream, game_version, catalog_hash)
    bindings = identity_binding["bindings"]
    inverse_bindings = {header_slot: source_slot for source_slot, header_slot in bindings.items()}
    global_orders = summarize_orders(stream.get("production_orders", []), game_version, catalog_hash, stream.get("cancellation_orders", []))
    warnings = ["录像头中的卡组只表示携带组成，不能当作实际购买。",
                "作战倾向按生产请求减去已匹配取消后的净数量近似部署投入，排除运输；未纳入击杀和损失。",
                "ESAV 自带校验字段算法尚未核验；已校验文件长度、块边界和时序，并记录 SHA-256。"]
    warnings.append("指令来源按游戏 GameSetup 排序关联录像头；完整生产 GUID、ACE 关联与所有权同时交叉校验，编号不会直接等同。")
    if not completed:
        warnings.append("此文件尚无可靠的正常结束证据，不累计完成对局。")
    if game_version != catalog_version:
        warnings.append("录像版本与单位目录不同，卡组价格及可用量不可确认。")
    players, identities = [], set()
    for slot, raw in slots:
        identifier = _integer(raw.get("PlayerUserId"))
        if identifier is None:
            raise ReplayError("录像玩家游戏 ID 无效。")
        identifier = str(identifier)
        if identifier in identities:
            raise ReplayError("录像存在重复游戏 ID，不能可靠关联玩家。")
        identities.add(identifier)
        ai_level = _integer(raw.get("PlayerIALevel"), -1, 6)
        auto_ai = _integer(raw.get("PlayerIsAIAutoFilled"), 0, 1)
        observer = _integer(raw.get("PlayerObserver"), 0, 1)
        is_ai = bool(int(identifier) & ((1 << 56) | (1 << 57))) or ai_level is not None and ai_level >= 0 or auto_ai == 1
        is_human_verified = ai_level == -1 and auto_ai in (0, None) and observer == 0 and not is_ai
        source_slot = inverse_bindings.get(slot)
        activity = stream["player_slots"].get(str(source_slot), {}) if source_slot is not None else {}
        control = _control(activity, stream, completed)
        participation = completed and is_human_verified and activity.get("battle_packet_count", 0) > 0
        player_orders = [order for order in stream.get("production_orders", []) if order["source_slot"] == source_slot] if source_slot is not None else []
        player_cancels = [order for order in stream.get("cancellation_orders", []) if order["source_slot"] == source_slot] if source_slot is not None else []
        players.append({"slot": slot, "id": identifier, "name": _text(raw.get("PlayerName"), 128),
                        "avatar": _avatar(raw.get("PlayerAvatar")), "is_ai": bool(is_ai),
                        "is_observer": observer != 0, "alliance": _integer(raw.get("PlayerAlliance"), 0, 1),
                        "identity_verified": is_human_verified, "participation_verified": bool(participation),
                        "stream_source_slot": source_slot,
                        "slot_binding": {"status": "cross_validated" if source_slot is not None else "unverified",
                                         "evidence": identity_binding["method"] if source_slot is not None else None},
                        "participation_evidence": "completed_round_and_battle_packets" if participation else None,
                        "activity": activity, "control": control,
                        "deck": _deck(raw.get("PlayerDeckContent"), game_version, catalog_version),
                        "stats": {"status": "orders_only" if player_orders else "unavailable", "P": None, "K": None, "L": None, "radar": None,
                                  "production_orders": summarize_orders(player_orders, game_version, catalog_hash, player_cancels),
                                  "shares": None, "reason": "真实生产请求已解码并扣除可关联取消；支付和成功部署仍不能由指令直接确认。"}})
    result = stream.get("result")
    duration = stream.get("battle_duration_seconds")
    if duration is None and result:
        duration = result.get("Duration")
    map_id = _text(game.get("Map"))
    mode = MODES.get((_integer(game.get("VictoryCond")), _integer(game.get("CombatRule"))), "未识别模式")
    return attach_tendencies({"schema": 1, "parser_version": PARSER_VERSION,
            "source": {"kind": "real_replay", "name": _text(Path(name).name, 200), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            "format": {"magic": "ESAV", "container_version": version, "header_offset": header_offset,
                       "header_length": header_length, "preceding_sections": sections, "checksum_status": "unverified",
                       "checksum_field": f"{checksum:08x}", "command_stream": stream},
            "match": {"session_id": _text(game.get("UniqueSessionId"), 128), "map": map_id, "map_name": _map_name(map_id),
                      "version": game_version, "mode": mode, "player_count": len(players),
                      "declared_player_count": _integer(game.get("NbPlayersAndIA"), 0, MAX_PLAYERS),
                      "duration_seconds": duration, "recorded_timeline_seconds": stream.get("recorded_timeline_seconds"),
                      "settings": _settings(game),
                      "completion": {"status": "finished" if completed else "unverified", "evidence": [completion]},
                      "result": result},
            "players": players, "production_orders": global_orders, "identity_binding": identity_binding,
            "catalog": {"version": catalog_version, "sha256": catalog_hash},
            "capabilities": {"identity": True, "loadout": True, "binary_framing": True,
                             "production_orders": stream.get("production_decoding_status") == "complete_production_request_stream",
                             "purchases": False, "kills": False, "losses": False}, "warnings": warnings})


def parse_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_REPLAY_BYTES:
        raise ReplayError("输入必须是大小不超过 128 MiB 的普通录像文件。")
    before = path.stat()
    with path.open("rb") as source:
        data = source.read(MAX_REPLAY_BYTES + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ReplayError("录像在分析时仍在变化，请等待文件保存完成。")
    return parse_replay(data, path.name)


def runtime_html(template, *, base_path="/", hosted=False, login_path="/rcon/", upload_enabled=True):
    for value in (base_path, login_path):
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", value):
            raise ValueError("Invalid statistics URL prefix")
    attributes = (f'data-stats-base-path="{base_path}" data-stats-hosted="{str(bool(hosted)).lower()}" '
                  f'data-stats-login-path="{login_path}" data-stats-upload-enabled="{str(bool(upload_enabled)).lower()}"')
    return template.replace('<html lang="zh-CN">', '<html lang="zh-CN" ' + attributes + '>')


def render_html(report: dict, *, inline_assets=True, base_path="/", hosted=False, login_path="/rcon/", upload_enabled=True) -> str:
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = runtime_html((ROOT / "real_report.html").read_text(encoding="utf-8"), base_path=base_path,
                            hosted=hosted, login_path=login_path, upload_enabled=upload_enabled).replace("__REPORT_DATA__", payload)
    if inline_assets:
        template = template.replace('<link rel="stylesheet" href="real_report.css">', "<style>" + (ROOT / "real_report.css").read_text(encoding="utf-8") + "</style>")
        template = template.replace('<script src="real_report.js"></script>', "<script>" + (ROOT / "real_report.js").read_text(encoding="utf-8") + "</script>")
        template = template.replace('<script src="radar-options.js"></script>', "<script>" + (ROOT / "radar-options.js").read_text(encoding="utf-8") + "</script>")
    else:
        template = template.replace('href="real_report.css"', f'href="{base_path}real_report.css"').replace('src="real_report.js"', f'src="{base_path}real_report.js"').replace('src="radar-options.js"', f'src="{base_path}radar-options.js"')
    return template
