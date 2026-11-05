"""Keep the valuation evidence a replay carries, next to the local captures.

`aggregation.derive_match` needs the replay's production requests, the slot
binding and the catalog identity to estimate investment P. The server keeps
those in its archive; a companion that only has the task report cannot. This
module parses the replay once (read only), keeps the small subset that the
calculation reads, and never copies the replay itself.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .service import atomic_json

MAX_REPLAY_BYTES = 512 * 1024 * 1024
_SCHEMA = "sd2-companion-replay-evidence-v1"
_HASH_LENGTH = 64


def evidence_path(directory, sha256):
    return Path(directory) / "replay-evidence" / (sha256 + ".json")


def _orders(value):
    return value if isinstance(value, list) else []


def compact(report):
    """Only the fields `derive_match` reads; native rows stay in the archive.

    Missing sections are omitted rather than stored as null: `derive_match`
    treats a present-but-null mapping as a mapping and would fail on it.
    """
    stream = ((report.get("format") or {}).get("command_stream") or {})
    players = []
    for player in report.get("players") or []:
        if not isinstance(player, dict):
            continue
        stats = player.get("stats") if isinstance(player.get("stats"), dict) else {}
        players.append({"id": player.get("id"),
                        "stream_source_slot": player.get("stream_source_slot"),
                        "slot_binding": player.get("slot_binding"),
                        "stats": {"production_orders": stats.get("production_orders")}})
    match = report.get("match") if isinstance(report.get("match"), dict) else {}
    value = {"schema": _SCHEMA,
             "match": {"version": match.get("version"),
                       # The replay knows its own map and session; keeping them here
                       # is what lets the results list show a name instead of a blank.
                       "map": match.get("map") if isinstance(match.get("map"), str) else None,
                       "map_name": match.get("map_name") if isinstance(match.get("map_name"), str) else None,
                       "session_id": (match.get("session_id") if isinstance(match.get("session_id"), str)
                                      else None)},
             "format": {"command_stream": {
                 "production_orders": _orders(stream.get("production_orders")),
                 "cancellation_orders": _orders(stream.get("cancellation_orders"))}},
             "players": players}
    for section in ("identity_binding", "capabilities", "catalog"):
        if isinstance(report.get(section), dict):
            value[section] = report[section]
    return value


def extract(path, sha256, directory, *, parser=None):
    """Parse one replay and store its valuation evidence under `sha256`."""
    if not isinstance(sha256, str) or len(sha256) != _HASH_LENGTH:
        raise ValueError("invalid_replay_digest")
    path = Path(path)
    before = path.stat()
    if not 0 < before.st_size <= MAX_REPLAY_BYTES:
        raise ValueError("replay_size_limit")
    if parser is None:
        from replay_parser import parse_file as parser  # imported lazily: heavy module
    report = parser(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("replay_changed_while_parsing")
    # Trust the caller's digest only when the file has not changed since hashing.
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != sha256:
        raise ValueError("replay_digest_mismatch")
    value = compact(report)
    atomic_json(evidence_path(directory, sha256), value)
    return value


def load(directory, sha256):
    """Read stored evidence; anything unreadable simply means "no P here"."""
    if not isinstance(sha256, str) or len(sha256) != _HASH_LENGTH:
        return None
    try:
        value = json.loads(evidence_path(directory, sha256).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        return None
    return value
