"""Read-only packaged-resource diagnostics, usable without a game process."""
from . import __version__, maps
from .pricing import Catalog


def check_resources(replay=None):
    import os
    from replay_parser import _catalog, _deck_analyzer, parse_file
    from production_summary import production_timing, transport_classification
    catalog = Catalog()
    deck_catalog, digest = _catalog()
    _deck_analyzer()
    timing = production_timing()
    result = {"version": __version__, "process_id": os.getpid(), "parent_process_id": os.getppid(),
              "price_units": len(catalog.units),
              "catalog_sha256": digest, "map_names": len(maps._load()),
              "production_timing_verified": timing.get("mapping_verified") is True,
              "transport_units": len(transport_classification().get("units", {})),
              "deck_catalog_loaded": bool(deck_catalog)}
    if (not result["price_units"] or not result["map_names"] or not result["production_timing_verified"]
            or not result["transport_units"] or not result["deck_catalog_loaded"]):
        raise ValueError("packaged_resources_incomplete")
    if replay is not None:
        report = parse_file(replay)
        stream = report.get("format", {}).get("command_stream", {})
        result["replay"] = {"map": report.get("match", {}).get("map"),
                            "production_orders": len(stream.get("production_orders", [])),
                            "players": len(report.get("players", []))}
    return result


def stage_restart_test(output):
    """Exercise the real frozen exit/replace/fresh-extraction path without GUI."""
    import hashlib
    from pathlib import Path
    import shutil
    import sys
    from .updater import UPDATE_URL, Release, install_on_exit
    if not getattr(sys, "frozen", False):
        raise ValueError("restart_test_requires_frozen_executable")
    target = Path(sys.executable).resolve()
    staged = target.parent / ".sd2-companion-updates" / "restart-test.exe"
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(target, staged)
    release = Release(__version__, UPDATE_URL.rsplit("/", 1)[0] + "/" + __version__ + "/SD2-Companion.exe",
                      hashlib.sha256(staged.read_bytes()).hexdigest(), staged.stat().st_size)
    install_on_exit(release, staged, target.parent / "companion.json",
                    restart_arguments=["check-resources", "--output", str(Path(output).resolve())])
