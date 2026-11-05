"""Public HTTPS releases; checking never sends a companion upload credential."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import UPDATE_URL
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class UpdateError(ValueError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2,3}", value):
        raise UpdateError("invalid_update_version")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def _origin(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise UpdateError("update_requires_https")
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        raise UpdateError("update_requires_https") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise UpdateError("update_requires_https")
    return parsed.hostname.casefold(), port


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    size: int


def parse_manifest(document, *, manifest_url=UPDATE_URL):
    if (not isinstance(document, dict) or document.get("schema") != 1
            or set(document) - {"schema", "version", "url", "sha256", "size"}):
        raise UpdateError("invalid_update_manifest")
    version_tuple(document.get("version"))
    if _origin(document.get("url")) != _origin(manifest_url):
        raise UpdateError("update_download_origin_mismatch")
    digest, size = document.get("sha256"), document.get("size")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or not 1024 <= size <= MAX_DOWNLOAD_BYTES):
        raise UpdateError("invalid_update_manifest")
    return Release(document["version"], document["url"], digest, size)


def _open(url, *, opener=None):
    _origin(url)
    request = Request(url, headers={"User-Agent": "SD2-Companion-Updater/1", "Cache-Control": "no-cache"})
    return (opener or build_opener(_NoRedirect())).open(request, timeout=20)


def check_for_update(current_version, *, manifest_url=UPDATE_URL, opener=None):
    try:
        with _open(manifest_url, opener=opener) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise UpdateError("update_manifest_too_large")
        document = json.loads(raw)
        release = parse_manifest(document, manifest_url=manifest_url)
        return release if version_tuple(release.version) > version_tuple(current_version) else None
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError("update_check_failed") from None


def download_release(release, directory, *, manifest_url=UPDATE_URL, opener=None):
    """Stream into a private sibling file, and only publish verified bytes."""
    release = parse_manifest({"schema": 1, **release.__dict__}, manifest_url=manifest_url)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("SD2-Companion-" + release.version + ".exe")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".download-", suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            digest, size, prefix = hashlib.sha256(), 0, b""
            with _open(release.url, opener=opener) as response:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > release.size:
                        raise UpdateError("update_size_mismatch")
                    prefix = (prefix + block)[:2]
                    output.write(block)
                    digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if size != release.size or digest.hexdigest() != release.sha256 or prefix != b"MZ":
            raise UpdateError("update_integrity_failed")
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError:
        raise UpdateError("update_download_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_on_exit(release, staged, config_path, *, executable=None, process_id=None, restart_arguments=None):
    """Launch the bundled helper. The UI then stops capture and exits normally."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UpdateError("update_install_requires_windows_exe")
    target = Path(executable or sys.executable).resolve()
    staged = Path(staged).resolve()
    update_dir = target.parent / ".sd2-companion-updates"
    if staged.parent != update_dir or target.suffix.casefold() != ".exe":
        raise UpdateError("invalid_update_staging_path")
    if staged.stat().st_size != release.size or hashlib.sha256(staged.read_bytes()).hexdigest() != release.sha256:
        raise UpdateError("update_integrity_failed")
    helper = update_dir / "install-update.ps1"
    shutil.copyfile(Path(__file__).with_name("install-update.ps1"), helper)
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper),
               "-Target", str(target), "-Staged", str(staged), "-Sha256", release.sha256,
               "-ParentProcessId", str(process_id or os.getpid()), "-ConfigPath", str(Path(config_path).resolve())]
    if restart_arguments is not None:
        command += ["-RestartArguments", subprocess.list2cmdline(restart_arguments)]
    # A system PowerShell process needs the system DLL search path, rather than
    # the application's bundled DLL directory inherited from the bootloader.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    kernel.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    finally:
        kernel.SetDllDirectoryW(getattr(sys, "_MEIPASS", None))
