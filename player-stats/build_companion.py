"""Build a public, credential-free Windows executable and HTTPS release manifest.

Run with the build virtualenv's Python. Only the listed public JSON resources
and code are bundled; private configs/replays/reports never enter the archive.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time

from companion import __version__
from companion.updater import UPDATE_URL, parse_manifest

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
RESOURCE_FILES = (
    (ROOT / "data/unit-guid-map.json", "data"),
    (ROOT / "data/transport-classification.json", "data"),
    (ROOT / "data/production-timing.json", "data"),
    (ROOT / "data/offmap-map.json", "data"),
    (ROOT / "data/action-names.json", "data"),
    (WORKSPACE / "deck-analyzer/data/catalog.json", "deck-analyzer/data"),
    (WORKSPACE / "deck-analyzer/analyzer.py", "deck-analyzer"),
    (WORKSPACE / "map-selector/maps.json", "map-selector"),
    (ROOT / "companion/install-update.ps1", "companion"),
)


def smoke(executable, *, replay=None):
    with tempfile.TemporaryDirectory(prefix="sd2-companion-smoke-") as temporary:
        directory = Path(temporary)
        config = directory / "companion.json"
        report = directory / "resources.json"
        subprocess.run([str(executable), "init", "--output", str(config)], check=True, timeout=60)
        if json.loads(config.read_text(encoding="utf-8"))["schema"] != 1:
            raise ValueError("frozen_init_failed")
        for command in ("doctor", "status"):
            subprocess.run([str(executable), command, "--config", str(config)], check=True, timeout=60)
        command = [str(executable), "check-resources", "--output", str(report)]
        if replay:
            command += ["--replay", str(Path(replay).resolve())]
        subprocess.run(command, check=True, timeout=180)
        result = json.loads(report.read_text(encoding="utf-8"))
        if result.get("version") != __version__:
            raise ValueError("frozen_version_mismatch")
        restart_directory = directory / "restart"
        restart_directory.mkdir()
        restart_exe = restart_directory / executable.name
        restarted_report = restart_directory / "restarted-resources.json"
        shutil.copyfile(executable, restart_exe)
        subprocess.run([str(restart_exe), "check-resources", "--test-restart", "--output", str(restarted_report)],
                       check=True, timeout=60)
        deadline = time.monotonic() + 90
        while not restarted_report.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        restarted = json.loads(restarted_report.read_text(encoding="utf-8"))
        if restarted.get("version") != __version__ or not restarted.get("production_timing_verified"):
            raise ValueError("frozen_update_restart_failed")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        for process_id in (restarted["process_id"], restarted["parent_process_id"]):
            handle = kernel.OpenProcess(0x00100000, False, process_id)
            if handle:
                try:
                    if kernel.WaitForSingleObject(handle, 30000) != 0:
                        raise ValueError("frozen_update_restart_exit_timeout")
                finally:
                    kernel.CloseHandle(handle)
        result["update_replace_restart_verified"] = True
        return result


def build(*, output=ROOT / "dist/sd2-companion", replay=None):
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("build_requires_64bit_windows_python")
    output = Path(output).resolve()
    build_directory = ROOT / "build/companion"
    build_directory.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx",
               "--name", "SD2-Companion", "--paths", str(ROOT), "--collect-submodules", "companion",
               "--workpath", str(build_directory / "work"), "--specpath", str(build_directory),
               "--distpath", str(output)]
    for source, destination in RESOURCE_FILES:
        if not source.is_file():
            raise FileNotFoundError(source)
        command += ["--add-data", str(source) + os.pathsep + destination]
    for module in ("replay_parser", "replay_stream", "replay_orders", "replay_identity", "production_summary",
                   "style_model", "tendency_model"):
        command += ["--hidden-import", module]
    command.append(str(ROOT / "player_companion.py"))
    subprocess.run(command, check=True, cwd=ROOT)
    executable = output / "SD2-Companion.exe"
    checks = smoke(executable, replay=replay)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = {"schema": 1, "version": __version__,
                "url": UPDATE_URL.rsplit("/", 1)[0] + "/" + __version__ + "/SD2-Companion.exe",
                "sha256": digest, "size": executable.stat().st_size}
    parse_manifest(manifest)
    version_directory = output / __version__
    version_directory.mkdir(exist_ok=True)
    shutil.copyfile(executable, version_directory / executable.name)
    (output / "update.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Local diagnostics are intentionally outside the public release directory.
    (build_directory / "smoke.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/sd2-companion")
    parser.add_argument("--test-replay", type=Path)
    options = parser.parse_args()
    print(json.dumps(build(output=options.output, replay=options.test_replay), indent=2))
