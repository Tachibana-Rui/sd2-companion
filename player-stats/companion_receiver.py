"""Standalone inbox for player companions; no hosted/admin integration."""

import argparse
import json
import os
from pathlib import Path
import re

from companion.receiver import (API_PATH, ArchiveIndex, CatalogSource, CompositeArchiveIndex,
                                ReceiverStore, create_server)
from companion.pricing import Catalog, DEFAULT_CATALOG
from companion.aggregation import CombatStore
from companion.enrollment import InstallationRegistry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parent / "private-reports/companion-inbox-v3.sqlite3")
    parser.add_argument("--combat-db", type=Path, help="Independent derived combat database; defaults beside the inbox")
    parser.add_argument("--catalog-path", type=Path, default=DEFAULT_CATALOG, help="Versioned unit GUID price catalog")
    parser.add_argument("--archive-db", type=Path, action="append",
                        help="Read-only player-stats archive; repeat for independent room databases")
    parser.add_argument("--catalog", type=Path,
                        help="Server-owned room table served to companions; edit the file to move a room")
    parser.add_argument("--catalog-interval", type=int, default=600,
                        help="Seconds the served catalog stays fresh before clients are asked to refresh")
    parser.add_argument("--server-sessions-db", type=Path, help="Server-owned live session observation ledger")
    parser.add_argument("--require-live-observation", action="store_true", help="Require exact live server epoch evidence in addition to finished archive")
    parser.add_argument("--listen", default="127.0.0.1", help="Explicit bind address; default is loopback only")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--token-env", default="SD2_COMPANION_TOKEN")
    parser.add_argument("--allow-registration", action="store_true",
                        help="Allow automatic installation credentials; operator token remains separate")
    args = parser.parse_args(argv)
    combat_path = (args.combat_db or args.database.with_name("combat-stats-v3.sqlite3")).resolve()
    archive_paths = {path.resolve() for path in (args.archive_db or [])}
    protected = {args.database.resolve(), *archive_paths}
    if combat_path in protected or args.database.resolve() in archive_paths:
        parser.error("inbox, combat and archive databases must be separate")
    if args.server_sessions_db is not None:
        state_path = args.server_sessions_db.resolve()
        other_paths = {args.database.resolve(), combat_path}
        if args.archive_db is not None:
            other_paths.update(path.resolve() for path in args.archive_db)
        if state_path in other_paths:
            parser.error("server session ledger must be separate from inbox and archive databases")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", args.token_env):
        parser.error("invalid token environment variable name")
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("receiver token environment variable is required")
    if args.require_live_observation and (args.archive_db is None or args.server_sessions_db is None):
        parser.error("live observation verification requires archive and server session databases")
    ledger = None
    if args.server_sessions_db is not None:
        from companion.server_sessions import ServerSessionLedger
        ledger = ServerSessionLedger(args.server_sessions_db)
    archive = None
    if args.archive_db:
        paths = tuple(dict.fromkeys(path.resolve() for path in args.archive_db))
        index_type, index_paths = ((ArchiveIndex, paths[0]) if len(paths) == 1 else
                                  (CompositeArchiveIndex, paths))
        archive = index_type(index_paths, server_sessions=ledger,
                             require_live_observation=args.require_live_observation)
    store = ReceiverStore(args.database, archive_index=archive)
    combat = CombatStore(combat_path, catalog=Catalog(args.catalog_path))
    catalog = None
    if args.catalog is not None:
        if isinstance(args.catalog_interval, bool) or not 30 <= args.catalog_interval <= 86400:
            parser.error("invalid catalog interval")
        catalog = CatalogSource(args.catalog, interval_seconds=args.catalog_interval)
    installations = InstallationRegistry(args.database) if args.allow_registration else None
    server = create_server(store, token, host=args.listen, port=args.port, combat_store=combat,
                           catalog=catalog, installations=installations)
    print(json.dumps({"receiver_ready": True, "listen": args.listen, "port": server.server_port,
                      "path": API_PATH, "archive_configured": archive is not None,
                      "combat_configured": True, "catalog_configured": catalog is not None,
                      "automatic_registration": installations is not None}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print(json.dumps({"receiver_failed": True, "reason": "startup_or_storage_error"}), flush=True)
        raise SystemExit(1)
