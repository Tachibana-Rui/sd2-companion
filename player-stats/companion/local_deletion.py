"""Remove one local capture without deleting replay files or server records."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from .local_replay_library import report_fingerprint
from .service import atomic_json


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = "sd2-deleted-local-report-v1"
_REPORT_DIRECTORIES = ("unbound-live", "unbound-replays", "unbound", "exports")


def _safe_path(root, path):
    """Refuse both external paths and links/junctions inside the data tree."""
    try:
        relative = path.relative_to(root)
        if not path.resolve().is_relative_to(root):
            return False
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                return False
    except (OSError, ValueError):
        return False
    return True


def deleted_fingerprints(directory):
    root = Path(directory).resolve()
    markers = root / "deleted-reports"
    if not _safe_path(root, markers):
        return set()
    result = set()
    try:
        for path in markers.glob("*.json"):
            if not _HASH.fullmatch(path.stem) or not _safe_path(root, path):
                continue
            try:
                if path.stat().st_size > 4096:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(value, dict) and value.get("schema") == _SCHEMA
                        and value.get("report_fingerprint") == path.stem):
                    result.add(path.stem)
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    return result


def _report_paths(root):
    for folder in _REPORT_DIRECTORIES:
        source = root / folder
        if not _safe_path(root, source):
            continue
        for current, directories, files in os.walk(source, followlinks=False):
            directory = Path(current)
            directories[:] = [name for name in directories if _safe_path(root, directory / name)]
            for name in files:
                path = directory / name
                if path.suffix.lower() == ".json" and _safe_path(root, path):
                    yield path


def _archive(root, path, recovery):
    if not _safe_path(root, path) or not _safe_path(root, recovery):
        raise ValueError("unsafe_local_record_path")
    destination = recovery / path.relative_to(root)
    if not _safe_path(root, destination):
        raise ValueError("unsafe_local_record_path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # Capturing may recreate a deleted snapshot before it sees the marker.
        # Retain a changed copy instead of overwriting the first recovery file.
        content = path.read_bytes()
        if destination.read_bytes() == content:
            path.unlink()
            return
        digest = hashlib.sha256(content).hexdigest()
        destination = destination.with_name(destination.stem + "-" + digest + destination.suffix)
        if destination.exists():
            if destination.read_bytes() != path.read_bytes():
                raise ValueError("local_record_recovery_conflict")
            path.unlink()
            return
    path.rename(destination)


def delete_record(directory, fingerprint, *, outbox=None):
    """Mark a capture deleted, cancel queued copies, and retain recovery files.

    The marker comes first, so a late disk write cannot resurrect the row. The
    exact report fingerprint, rather than session/map/time, is the deletion
    boundary; a second capture of the same match remains independent.
    """
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        raise ValueError("invalid_local_report_fingerprint")
    root = Path(directory).resolve()
    marker = root / "deleted-reports" / (fingerprint + ".json")
    recovery = root / "deleted-reports" / fingerprint / "files"
    if not _safe_path(root, marker) or not _safe_path(root, recovery):
        raise ValueError("unsafe_local_record_path")
    if fingerprint not in deleted_fingerprints(root):
        atomic_json(marker, {"schema": _SCHEMA, "report_fingerprint": fingerprint,
                             "deleted_at": time.time()})
    cancelled = outbox.cancel_report(fingerprint) if outbox is not None else 0
    reports = 0
    for path in _report_paths(root):
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            report = value.get("report", value)
            matches = report_fingerprint(report) == fingerprint
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            continue
        if matches:
            _archive(root, path, recovery)
            reports += 1
    link = root / "local-replay-links" / (fingerprint + ".json")
    if link.exists() and _safe_path(root, link):
        _archive(root, link, recovery)
    return {"reports": reports, "queued_cancelled": cancelled}
