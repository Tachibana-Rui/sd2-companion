"""Local valuation links, separate from verified exports and the upload queue.

Downloaded replays can arrive after the task report, without observable game
file I/O. Match those by a captured native session AND the full ID/side roster.
Old reports without a session require the player's explicit file selection.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import replay_evidence
from .replay_capture import _header_prefix, inspect_replay_file
from .replays import parse_header
from .service import atomic_json
from .state import roster_matches

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = "sd2-local-replay-link-v1"


def report_fingerprint(report):
    material = {"game_build": report["game_build"], "generation": report["report_generation"],
                "players": report["native_players"], "rows": report["rows"]}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_link(directory, fingerprint):
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        return None
    try:
        value = json.loads((Path(directory) / "local-replay-links" / (fingerprint + ".json"))
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("schema") != _SCHEMA
            or value.get("report_fingerprint") != fingerprint
            or value.get("association") not in ("user_selected", "exact_native_session")
            or not isinstance(value.get("replay_sha256"), str)
            or not _HASH.fullmatch(value["replay_sha256"])):
        return None
    return value


def associate(directory, report, path, *, session_id=None, map_id=None,
              user_selected=False, parser=None):
    """Validate and persist a local-only link; never create an export or enqueue."""
    snapshot = inspect_replay_file(path)
    identity = snapshot.identity
    if (identity.game_build != report.get("game_build")
            or not roster_matches(report.get("native_players"), identity.participants)):
        raise ValueError("local_replay_roster_or_build_mismatch")
    if session_id and session_id != identity.session_id:
        raise ValueError("local_replay_session_mismatch")
    if map_id and map_id != identity.map:
        raise ValueError("local_replay_map_mismatch")
    if not session_id and not user_selected:
        raise ValueError("local_replay_requires_session_or_selection")
    evidence = replay_evidence.load(directory, snapshot.sha256)
    if evidence is None:
        replay_evidence.extract(path, snapshot.sha256, directory, parser=parser)
    # Recheck the file even when cached evidence was available.
    current = Path(path).stat()
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
            current.st_ctime_ns) != snapshot.signature:
        raise ValueError("local_replay_changed")
    fingerprint = report_fingerprint(report)
    value = {"schema": _SCHEMA, "report_fingerprint": fingerprint,
             "replay_sha256": snapshot.sha256, "session_id": identity.session_id,
             "map": identity.map, "server_name": identity.server_name,
             "association": "user_selected" if user_selected else "exact_native_session"}
    atomic_json(Path(directory) / "local-replay-links" / (fingerprint + ".json"), value)
    return value


def default_replay_directories():
    saved = Path.home() / "Saved Games"
    if os.name == "nt":
        # Honor relocated Windows Saved Games folders as well as the usual path.
        import ctypes
        from ctypes import wintypes
        import uuid
        folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(
            uuid.UUID("4C5C32FF-BB9D-43B0-BF3A-4D8A9B2EFDCD").bytes_le)
        output = ctypes.c_wchar_p()
        shell = ctypes.WinDLL("shell32")
        function = shell.SHGetKnownFolderPath
        function.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                             ctypes.POINTER(ctypes.c_wchar_p)]
        function.restype = ctypes.c_long
        if function(ctypes.byref(folder_id), 0, None, ctypes.byref(output)) == 0:
            try:
                saved = Path(output.value)
            finally:
                free = ctypes.WinDLL("ole32").CoTaskMemFree
                free.argtypes = [ctypes.c_void_p]
                free(ctypes.cast(output, ctypes.c_void_p))
    return (saved / "EugenSystems" / "SteelDivision2",)


class LocalReplayLibrary:
    """Bounded header discovery on the inventory worker, independent of ETW."""
    def __init__(self, directory, directories, *, clock=time.monotonic):
        self.directory = Path(directory)
        self.directories = tuple(dict.fromkeys(Path(p) for p in directories))
        self.clock, self.next_scan = clock, 0
        self.headers = {}
        self.failures = {}

    def reconcile(self, entries):
        now = self.clock()
        if now < self.next_scan:
            return
        self.next_scan = now + 15
        wanted = [entry for entry in entries if entry.get("session")
                  and entry.get("session_origin") in ("native_replay_context", "verified_export")
                  and (not entry.get("replay") or
                       replay_evidence.load(self.directory, entry["replay"]) is None)]
        if not wanted:
            return
        candidates, seen = {}, set()
        count = 0
        for directory in self.directories:
            try:
                paths = directory.iterdir()
                for path in paths:
                    if path.suffix.lower() not in (".rpl3", ".rpl") or path.is_symlink():
                        continue
                    count += 1
                    if count > 2000:
                        return  # An incomplete search cannot prove uniqueness.
                    try:
                        stat = path.stat()
                        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                        cached = self.headers.get(path)
                        if cached is None or cached[0] != signature:
                            with path.open("rb") as source:
                                identity = parse_header(_header_prefix(source), source_path=str(path))
                            self.headers[path] = signature, identity
                        identity = self.headers[path][1]
                        candidates.setdefault(identity.session_id, []).append(path)
                        seen.add(path)
                    except (OSError, ValueError):
                        continue
            except OSError:
                continue
        self.headers = {path: value for path, value in self.headers.items() if path in seen}
        for entry in wanted:
            matches = candidates.get(entry["session"], [])
            if not matches:
                continue
            try:
                document = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
                report = document.get("report", document)
                if report_fingerprint(report) != entry["fingerprint"]:
                    continue
                compatible = [path for path in matches if
                    self.headers[path][1].game_build == report["game_build"]
                    and roster_matches(report["native_players"], self.headers[path][1].participants)]
                snapshots = [inspect_replay_file(path) for path in compatible]
                if entry.get("replay"):
                    pairs = [(path, snapshot) for path, snapshot in zip(compatible, snapshots)
                             if snapshot.sha256 == entry["replay"]]
                    compatible = [path for path, _ in pairs]
                    snapshots = [snapshot for _, snapshot in pairs]
                if len({snapshot.sha256 for snapshot in snapshots}) != 1:
                    continue
                key = (entry["fingerprint"], snapshots[0].sha256)
                if self.failures.get(key, 0) > now:
                    continue
                try:
                    associate(self.directory, report, compatible[0], session_id=entry["session"])
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError):
                    self.failures[key] = now + 300
            except (OSError, ValueError, KeyError, TypeError):
                continue
