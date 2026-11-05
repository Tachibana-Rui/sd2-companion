"""Endpoint-scoped upload tokens in the current user's Windows credential store."""
from __future__ import annotations

import ctypes
import hashlib
import os
from urllib.parse import urlsplit
import uuid


class CredentialError(ValueError):
    """Safe error codes only: credential bytes never appear in diagnostics."""


DWORD = ctypes.c_uint32
LPBYTE = ctypes.POINTER(ctypes.c_ubyte)


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", DWORD), ("Type", DWORD),
                ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
                ("LastWritten", _FILETIME), ("CredentialBlobSize", DWORD),
                ("CredentialBlob", LPBYTE), ("Persist", DWORD),
                ("AttributeCount", DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
_GENERIC = 1
_LOCAL_MACHINE = 2  # Same user, survives logoff; never a machine-wide secret.
_NOT_FOUND = 1168
_MAX_TOKEN = 2560


def _target(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise CredentialError("invalid_credential_endpoint")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except ValueError:
        raise CredentialError("invalid_credential_endpoint") from None
    # Full endpoint scope: a different upload path never receives this token.
    return "SD2.Companion.Upload/" + hashlib.sha256(url.encode("utf-8")).hexdigest()


def _token_bytes(token):
    if (not isinstance(token, str) or not 1 <= len(token) <= _MAX_TOKEN
            or any(not 33 <= ord(c) <= 126 for c in token)):
        raise CredentialError("invalid_upload_credential")
    return token.encode("ascii")


def _windows_api():
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD,
                              ctypes.POINTER(_PCREDENTIALW)]
    api.CredReadW.restype = ctypes.c_int32
    api.CredWriteW.argtypes = [_PCREDENTIALW, DWORD]
    api.CredWriteW.restype = ctypes.c_int32
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def read_upload_token(url):
    """Return the current user's token for exactly this HTTPS endpoint, or None."""
    return _read_target(_target(url))


def _installation_target(url, installation_id):
    _target(url)  # Apply the same endpoint restrictions as operator credentials.
    try:
        if str(uuid.UUID(installation_id)) != installation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CredentialError("invalid_installation_id") from None
    return "SD2.Companion.Installation/" + hashlib.sha256(
        (url + "\0" + installation_id).encode("utf-8")).hexdigest()


def read_installation_token(url, installation_id):
    """Keep copied executables with different local installation IDs separate."""
    return _read_target(_installation_target(url, installation_id))


def _read_target(target):
    api = _windows_api()
    if api is None:
        return None
    credential = _PCREDENTIALW()
    if not api.CredReadW(target, _GENERIC, 0, ctypes.byref(credential)):
        if ctypes.get_last_error() == _NOT_FOUND:
            return None
        raise CredentialError("upload_credential_read_failed")
    try:
        value = credential.contents
        if (value.Type != _GENERIC or value.TargetName != target
                or not value.CredentialBlob or not 1 <= value.CredentialBlobSize <= _MAX_TOKEN):
            raise CredentialError("invalid_upload_credential")
        try:
            token = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("ascii")
        except UnicodeError:
            raise CredentialError("invalid_upload_credential") from None
        _token_bytes(token)
        return token
    finally:
        api.CredFree(credential)


def store_upload_token(url, token):
    """Store a token without copying it into config JSON or an environment file."""
    return _store_target(_target(url), token)


def store_installation_token(url, installation_id, token):
    """Persist an automatic client credential in the user's Windows store."""
    return _store_target(_installation_target(url, installation_id), token)


def _store_target(target, token):
    encoded = _token_bytes(token)
    api = _windows_api()
    if api is None:
        raise CredentialError("windows_credential_store_unavailable")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = _CREDENTIALW(Type=_GENERIC, TargetName=target,
                              CredentialBlobSize=len(encoded), CredentialBlob=blob,
                              Persist=_LOCAL_MACHINE, UserName="SD2 Companion Upload")
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError("upload_credential_write_failed")


def get_upload_token(url, token_env="SD2_COMPANION_TOKEN"):
    """Explicit process environment wins; the Windows store is a desktop fallback."""
    value = os.environ.get(token_env)
    if value:
        return value
    # Loopback HTTP remains an explicit, environment-only test configuration.
    if urlsplit(url).scheme != "https":
        return None
    return read_upload_token(url)
