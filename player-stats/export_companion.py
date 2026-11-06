"""Export the public companion sources from an explicit, reviewed allowlist.

This never copies repository history, credentials, deployment configuration,
recordings, reports, databases, compiled binaries or a developer environment.
The destination must be empty. Run the exported tests/build before publishing.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent

COMPANION_MODULES = (
    "__init__.py", "aggregation.py", "bootstrap.py", "catalog.py", "cli.py", "config.py",
    "credentials.py", "desktop.py", "enrollment.py", "file_access.py", "getters.py", "http_transport.py",
    "live_capture.py", "live_runtime.py", "local_replay_library.py", "maps.py",
    "native_report.py", "native_session.py", "native_stats.py", "outbox.py", "pricing.py",
    "receiver.py", "replay_association.py", "replay_capture.py", "replay_evidence.py", "replay_runtime.py",
    "replays.py", "resources.py", "runtime.py", "server_sessions.py", "service.py",
    "state.py", "updater.py", "upload.py", "windows.py", "install-update.ps1",
)
SUPPORT_FILES = (
    "player-stats/player_companion.py", "player-stats/build_companion.py",
    "player-stats/export_companion.py", "player-stats/run_companion.cmd",
    "player-stats/requirements-build.txt", "player-stats/companion.example.json",
    "player-stats/replay_parser.py", "player-stats/replay_stream.py",
    "player-stats/replay_orders.py", "player-stats/replay_identity.py",
    "player-stats/production_summary.py", "player-stats/style_model.py",
    "player-stats/tendency_model.py", "deck-analyzer/analyzer.py",
    "player-stats/data/unit-guid-map.json", "player-stats/data/transport-classification.json",
    "player-stats/data/production-timing.json", "player-stats/data/offmap-map.json",
    "player-stats/data/action-names.json", "deck-analyzer/data/catalog.json",
    "map-selector/maps.json",
)
TEST_MODULES = (
    "test_companion_attribution.py", "test_companion_bootstrap.py", "test_companion_catalog.py",
    "test_companion_catalog_client.py", "test_companion_combat.py", "test_companion_core.py",
    "test_companion_credentials.py", "test_companion_defaults.py", "test_companion_delivery.py",
    "test_companion_http_transport.py",
    "test_companion_desktop.py", "test_companion_enrollment.py", "test_companion_file_access.py",
    "test_companion_live_capture.py", "test_companion_live_integration.py", "test_companion_live_runtime.py",
    "test_companion_local_replay_library.py", "test_companion_native_report.py",
    "test_companion_native_session.py", "test_companion_native_stats.py",
    "test_companion_readonly_database.py", "test_companion_receiver.py", "test_companion_registry.py",
    "test_companion_replay_association.py",
    "test_companion_replay_capture.py", "test_companion_replay_evidence.py",
    "test_companion_replay_integration.py", "test_companion_replay_runtime.py",
    "test_companion_report_payload.py", "test_companion_runtime.py", "test_companion_server_sessions.py",
    "test_companion_stats_integration.py", "test_companion_updater.py", "test_companion_windows.py",
    "test_replay_stream.py", "test_replay_orders.py", "test_replay_identity.py",
    "test_production_summary.py", "test_production_cancellations.py", "test_tendency_model.py",
)
DOCUMENTS = {
    "player-stats/publication/README.md": "README.md",
    "player-stats/publication/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "player-stats/publication/.gitignore": ".gitignore",
    "player-stats/publication/.gitattributes": ".gitattributes",
}

FORBIDDEN_PARTS = {".git", "private-reports", "private-replays", "__pycache__", "build-env",
                   "build", "dist", "diagnostics", "settings", "research"}
SENSITIVE_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(rb"C:[/\\]+Users[/\\]+white(?:[/\\]|\b)", re.I),
    re.compile(rb"/home/" + rb"bitnami(?:/|\b)"),
)


def source_inventory():
    files = [("player-stats/companion/" + name,) * 2 for name in COMPANION_MODULES]
    files += [(name, name) for name in SUPPORT_FILES]
    files += [("player-stats/" + name,) * 2 for name in TEST_MODULES]
    # Include the small public templates so the export remains reproducible.
    files += [(name, name) for name in DOCUMENTS]
    files += list(DOCUMENTS.items())
    return files


def _checked_file(workspace, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or any(part in FORBIDDEN_PARTS for part in path.parts):
        raise ValueError("unsafe_public_source_path")
    source = workspace / path
    if any((workspace / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
        raise ValueError("public_source_symlink")
    if not source.is_file() or not source.resolve().is_relative_to(workspace):
        raise ValueError("public_source_missing")
    return source


def export(destination, *, workspace=WORKSPACE):
    workspace = Path(workspace).resolve()
    destination = Path(destination).resolve()
    if destination == workspace or workspace.is_relative_to(destination):
        raise ValueError("invalid_public_destination")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("public_destination_must_be_empty")
    prepared = []
    for relative, target in source_inventory():
        source = _checked_file(workspace, relative)
        content = source.read_bytes()
        if any(pattern.search(content) for pattern in SENSITIVE_PATTERNS):
            # Only the relative location escapes; never a matched secret value.
            raise ValueError("sensitive_public_source:" + relative)
        if source.suffix == ".py":
            ast.parse(content.decode("utf-8-sig"), filename=relative)
        elif source.suffix == ".json":
            json.loads(content.decode("utf-8-sig"))
        prepared.append((target, content))
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for relative, content in prepared:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    manifest = {"schema": 1, "scope": "sd2-companion-public-source", "files": sorted(records, key=lambda r: r["path"])}
    (destination / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"files": len(records) + 1, "bytes": sum(record["bytes"] for record in records),
            "destination": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=WORKSPACE / "diagnostics/publication/sd2-companion")
    args = parser.parse_args()
    print(json.dumps(export(args.output), indent=2))
