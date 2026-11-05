"""Battlefield names for map ids, reusing the same table as the website.

The game stores an id like `_3x2_Mogilev_LD_10v10`; players read the display name
(`Mogilev #10vs10`). The companion only needs this for labels, so the table is
loaded lazily and a missing file simply keeps the raw id.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

_SUFFIXES = ("_BKT", "_CQC", "_DEST", "_DEST_CQC")
_cache = {"loaded": False, "names": {}}


def _candidates():
    """Workspace checkout first, then a copy shipped next to a packaged build."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        yield Path(sys._MEIPASS) / "map-selector" / "maps.json"
    yield Path(__file__).resolve().parents[2] / "map-selector" / "maps.json"
    yield Path(sys.executable).resolve().parent / "maps.json"


def _load():
    names = {}
    for path in _candidates():
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        entries = document.get("maps") if isinstance(document, dict) else document
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            identifier, name = item.get("id"), item.get("name")
            if isinstance(identifier, str) and identifier and isinstance(name, str) and name:
                names[identifier] = name
        if names:
            break
    return names


def display_name(identifier):
    """Friendly battlefield name for a map id; the id itself when unknown."""
    if not isinstance(identifier, str) or not identifier:
        return identifier
    if not _cache["loaded"]:
        _cache["names"] = _load()
        _cache["loaded"] = True
    names = _cache["names"]
    if identifier in names:
        return names[identifier]
    for suffix in _SUFFIXES:
        if identifier.endswith(suffix) and identifier[: -len(suffix)] in names:
            return names[identifier[: -len(suffix)]]
    return identifier
