"""Small, explicit companion configuration. Credentials stay out of JSON.

The file on disk is a base64 envelope so an operator reading it casually never
sees the room endpoints or the upload URL. Base64 is encoding, not encryption:
it stops shoulder-surfing and naive grep, not a determined reader. Upload
credentials are never stored here at all - they stay in the environment
variable named by `token_env`.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from . import UPDATE_URL


class ConfigError(ValueError):
    pass


ENVELOPE_PREFIX = "sd2c1:"
DEFAULT_UPLOAD_URL = "https://sd2.rui-blog.link/api/companion/v3/task-reports"
DEFAULT_CATALOG_URL = "https://sd2.rui-blog.link/api/companion/v3/rooms"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


@dataclass(frozen=True)
class Room:
    id: str
    endpoints: tuple[Endpoint, ...]
    server_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    rooms: tuple[Room, ...]
    replay_directories: tuple[Path, ...]
    data_directory: Path
    game_executable: Path | None = None
    upload_url: str | None = None
    catalog_url: str | None = None
    catalog_interval_seconds: float = 600.0
    token_env: str = "SD2_COMPANION_TOKEN"
    poll_seconds: float = 0.5
    process_poll_seconds: float = 3.0
    report_timeout_seconds: float = 7200.0
    max_replay_files: int = 2000
    allow_loopback_http: bool = False
    collection_mode: str = "auto"
    language: str = "en"
    automatic_updates: bool = True
    update_url: str = UPDATE_URL
    update_interval_seconds: float = 21600.0
    online_enabled: bool = True


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("duplicate_config_property")
        result[key] = value
    return result


def _number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError("invalid_" + label)
    return float(value)


def _path(value, base):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError("invalid_path")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def parse_rooms(raw_rooms):
    """Validate a room table; shared by the config file and the server catalog."""
    if not isinstance(raw_rooms, list):
        raise ConfigError("invalid_rooms")
    rooms = []
    for room in raw_rooms:
        if not isinstance(room, dict) or set(room) - {"id", "endpoints", "server_names"}:
            raise ConfigError("invalid_room")
        identifier = room.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier):
            raise ConfigError("invalid_room_id")
        endpoints = []
        raw_endpoints = room.get("endpoints", [])
        if not isinstance(raw_endpoints, list):
            raise ConfigError("invalid_endpoints")
        for item in raw_endpoints:
            if not isinstance(item, dict) or set(item) != {"host", "port"}:
                raise ConfigError("invalid_endpoint")
            host, port = item["host"], item["port"]
            if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() or c in "/\\@?#" for c in host):
                raise ConfigError("invalid_endpoint_host")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ConfigError("invalid_endpoint_port")
            endpoints.append(Endpoint(host, port))
        if not endpoints:
            raise ConfigError("room_requires_endpoint")
        names = room.get("server_names", [])
        if not isinstance(names, list) or any(not isinstance(x, str) or not x or len(x) > 256 for x in names):
            raise ConfigError("invalid_server_names")
        rooms.append(Room(identifier, tuple(endpoints), tuple(names)))
    if len(rooms) > 16 or len({r.id for r in rooms}) != len(rooms):
        raise ConfigError("invalid_room_count_or_duplicate")
    endpoint_keys = [(e.host.casefold(), e.port) for r in rooms for e in r.endpoints]
    if len(endpoint_keys) != len(set(endpoint_keys)):
        raise ConfigError("endpoint_assigned_to_multiple_rooms")
    return rooms


def _https_endpoint(value, *, allow_loopback_http, label):
    """Require the final HTTPS endpoint; loopback HTTP only for local testing."""
    if not isinstance(value, str) or not value:
        raise ConfigError(label)
    parsed = urlsplit(value)
    allowed_http = False
    if allow_loopback_http and parsed.scheme == "http":
        try:
            allowed_http = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            allowed_http = (parsed.hostname or "").lower() == "localhost"
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme != "https" and not allowed_http):
        raise ConfigError(label)
    return value


def catalog_url_for_upload(upload_url):
    """Derive the room API in memory; never copy game endpoints into config."""
    if not upload_url:
        return None
    parsed = urlsplit(upload_url)
    return parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/rooms").geturl()


def from_dict(raw, *, base=Path.cwd()):
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ConfigError("unsupported_config_schema")
    allowed = {"schema", "rooms", "replay_directories", "data_directory", "game_executable", "upload_url",
               "catalog_url", "catalog_interval_seconds", "token_env", "poll_seconds", "process_poll_seconds",
               "report_timeout_seconds", "max_replay_files", "allow_loopback_http", "collection_mode", "language",
               "automatic_updates", "update_url", "update_interval_seconds", "online_enabled"}
    if set(raw) - allowed:
        raise ConfigError("unknown_config_property")
    base = Path(base).resolve()
    mode = raw.get("collection_mode", "auto")
    if mode not in ("auto", "live", "replay"):
        raise ConfigError("invalid_collection_mode")
    language = raw.get("language", "en")
    if language not in ("en", "zh"):
        raise ConfigError("invalid_language")
    automatic_updates = raw.get("automatic_updates", True)
    if type(automatic_updates) is not bool:
        raise ConfigError("invalid_automatic_updates")
    update_url = _https_endpoint(raw.get("update_url", UPDATE_URL), allow_loopback_http=False,
                                 label="update_requires_https_without_credentials_or_query")
    rooms = parse_rooms(raw.get("rooms", []))
    paths = raw.get("replay_directories", [])
    if not isinstance(paths, list) or len(paths) > 16:
        raise ConfigError("replay_directories_required")
    online_enabled = raw.get("online_enabled", True)
    if type(online_enabled) is not bool:
        raise ConfigError("invalid_online_enabled")
    # The old desktop template wrote null URLs. They now mean the standard
    # service, including on upgrade; local-only use has an explicit switch.
    # Resolve defaults in memory without replacing a user's config or paths.
    url = (raw.get("upload_url") or DEFAULT_UPLOAD_URL) if online_enabled else None
    loopback = raw.get("allow_loopback_http", False)
    if type(loopback) is not bool:
        raise ConfigError("invalid_loopback_option")
    if url is not None:
        _https_endpoint(url, allow_loopback_http=loopback,
                        label="upload_requires_https_without_credentials_or_query")
    catalog = (raw.get("catalog_url") or catalog_url_for_upload(url)) if online_enabled else None
    if catalog is not None:
        _https_endpoint(catalog, allow_loopback_http=loopback,
                        label="catalog_requires_https_without_credentials_or_query")
    token_env = raw.get("token_env", "SD2_COMPANION_TOKEN")
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token_env):
        raise ConfigError("invalid_token_env")
    limit = raw.get("max_replay_files", 2000)
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ConfigError("invalid_max_replay_files")
    return Config(tuple(rooms), tuple(_path(x, base) for x in paths), _path(raw.get("data_directory", "companion-data-v3"), base),
                  _path(raw["game_executable"], base) if raw.get("game_executable") else None,
                  url, catalog,
                  _number(raw.get("catalog_interval_seconds", 600), "catalog_interval_seconds", 30, 86400),
                  token_env,
                  _number(raw.get("poll_seconds", 0.5), "poll_seconds", 0.1, 60),
                  _number(raw.get("process_poll_seconds", 3), "process_poll_seconds", 0.5, 120),
                  _number(raw.get("report_timeout_seconds", 7200), "report_timeout_seconds", 10, 86400), limit, loopback, mode,
                  language, automatic_updates, update_url,
                  _number(raw.get("update_interval_seconds", 21600), "update_interval_seconds", 300, 604800),
                  online_enabled)


def encode_config(raw):
    """Serialize a config mapping into the base64 envelope written to disk."""
    if not isinstance(raw, dict):
        raise ConfigError("invalid_config_document")
    document = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return ENVELOPE_PREFIX + base64.b64encode(document.encode("utf-8")).decode("ascii") + "\n"


def decode_config(text):
    """Read the current envelope or a legacy plain JSON document."""
    if not isinstance(text, str):
        raise ConfigError("invalid_config_document")
    document = text.strip()
    if document.startswith(ENVELOPE_PREFIX):
        try:
            document = base64.b64decode(document[len(ENVELOPE_PREFIX):].encode("ascii"),
                                         validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError, ValueError):
            raise ConfigError("invalid_config_envelope") from None
    try:
        return json.loads(document, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ConfigError("invalid_config_number")))
    except json.JSONDecodeError:
        raise ConfigError("invalid_config_document") from None


def save_raw(path, raw):
    """Write the config envelope atomically with owner-only permissions."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = encode_config(raw)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".companion-config-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load(path):
    path = Path(path).resolve()
    try:
        if path.stat().st_size > 64 * 1024:
            raise ConfigError("config_too_large")
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError("configuration_missing") from None
    return from_dict(decode_config(text), base=path.parent)


def example_config():
    return {"schema": 1, "collection_mode": "auto", "language": "en", "rooms": [],
            "game_executable": None,
            "replay_directories": [],
            "data_directory": "companion-data-v3", "online_enabled": True,
            "upload_url": DEFAULT_UPLOAD_URL, "catalog_url": DEFAULT_CATALOG_URL,
            "catalog_interval_seconds": 600,
            "token_env": "SD2_COMPANION_TOKEN", "poll_seconds": 0.5,
            "automatic_updates": True, "update_url": UPDATE_URL, "update_interval_seconds": 21600}

