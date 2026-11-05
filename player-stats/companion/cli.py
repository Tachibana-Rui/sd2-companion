"""User-invoked CLI. No game launch, input automation, or process writes."""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading

from . import __version__
from .config import ConfigError, catalog_url_for_upload, example_config, load
from .credentials import get_upload_token
from .outbox import Outbox
from .service import DeliveryWorker, atomic_json
from .upload import Uploader
from .native_report import NativeReportError


def default_config_path():
    """Locate the private config for source runs and for a frozen executable.

    A packaged single-file build runs from a temporary extraction directory, so
    its config must live beside the .exe instead of inside that scratch folder.
    """
    base = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])
    return base / "private-reports" / "companion.json"


DEFAULT_CONFIG = default_config_path()
_PRINT_LOCK = threading.Lock()


def emit(value):
    with _PRINT_LOCK:
        if sys.stdout is not None:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


class RunLock:
    def __init__(self, directory):
        self.path = Path(directory) / ".companion-run.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ValueError("companion_already_running") from exc
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def _uploader(config):
    if not getattr(config, "online_enabled", True) or config.upload_url is None:
        return None
    token = get_upload_token(config.upload_url, config.token_env)
    if not token:
        raise ConfigError("upload_token_environment_variable_missing")
    return Uploader(config.upload_url, token=token, token_env=config.token_env,
                    allow_loopback_http=config.allow_loopback_http)


def _catalog(config):
    """Server-issued room table and remote tunables, or None when disabled."""
    if not getattr(config, "online_enabled", True):
        return None
    address = config.catalog_url or catalog_url_for_upload(config.upload_url)
    if not address:
        return None
    from .catalog import RemoteCatalog
    token = get_upload_token(config.upload_url or address, config.token_env)
    return RemoteCatalog(address, token=token or None, token_env=config.token_env,
                         interval=config.catalog_interval_seconds,
                         allow_loopback_http=config.allow_loopback_http)


def _catalog_event(catalog, registration=None):
    """The UI receives status and counts, never room addresses or the table."""
    state = catalog.state if catalog else "disabled"
    if registration is not None and registration.token_if_ready() is None:
        state = "connecting" if getattr(registration, "state", "pending") == "retry" else "pending"
    return {"event": "catalog_state", "state": state,
            "room_count": len(catalog.rooms) if catalog else 0,
            "last_success": catalog.last_success if catalog else None,
            "code": catalog.last_error if catalog else None}


def _capture_uploader(config, emit_fn):
    """A missing credential disables delivery while local capture continues."""
    try:
        return _uploader(config)
    except ConfigError as exc:
        if str(exc) != "upload_token_environment_variable_missing":
            raise
        from .bootstrap import can_register
        emit_fn({"event": "client_registration_pending" if can_register(config)
                           else "upload_token_environment_variable_missing"})
        return None


def _start_registration(config, outbox, service, emit_fn):
    if service.uploader is not None:
        return None
    from .bootstrap import BootstrapClient, RegistrationWorker, can_register
    if not can_register(config):
        return None
    worker = RegistrationWorker(BootstrapClient(config, outbox.installation_id), emit=emit_fn)
    worker.start()
    return worker


def _activate_registration(worker, config, catalog, service, delivery, emit_fn):
    """Attach only on the capture thread, after enrollment's worker succeeds."""
    if worker is None or service.uploader is not None:
        return delivery
    token = worker.token_if_ready()
    if token is None:
        return delivery
    if catalog is not None:
        catalog.token = token
        catalog.next_refresh = 0.0
    service.uploader = Uploader(config.upload_url, token=token, token_env=config.token_env,
                                allow_loopback_http=config.allow_loopback_http)
    delivery = DeliveryWorker(service)
    delivery.start()
    emit_fn({"event": "online_services_ready"})
    return delivery


def _run(config, *, once=False):
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("live_capture_requires_64bit_windows_python")
    if config.collection_mode == "replay":
        return _run_replay(config, once=once)
    return _run_auto(config, once=once)


def _run_auto(config, *, once=False, emit_fn=emit, stop_event=None):
    """Live GameSetup/session binding and local replay SHA binding coexist."""
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("live_capture_requires_64bit_windows_python")
    from .live_runtime import AutomaticWindowsObserver
    from .live_capture import AutomaticCaptureService, LiveCaptureService
    stop_event = stop_event or threading.Event()
    live_only = config.collection_mode == "live"
    with RunLock(config.data_directory):
        outbox = Outbox(config.data_directory / "outbox.sqlite3")
        catalog = _catalog(config)
        observer = AutomaticWindowsObserver(config, catalog=catalog, with_replays=not live_only)
        service_type = LiveCaptureService if live_only else AutomaticCaptureService
        service = service_type(config, observer, outbox, uploader=_capture_uploader(config, emit_fn), emit=emit_fn)
        delivery = DeliveryWorker(service) if service.uploader is not None else None
        registration = _start_registration(config, outbox, service, emit_fn)
        emit_fn({"event": "companion_started", "version": __version__, "upload_enabled": service.uploader is not None,
                 "collection_mode": "live" if live_only else "auto", "process_access": "read_only",
                 "catalog_configured": catalog is not None})
        try:
            if delivery:
                delivery.start()
            last_catalog = None
            while not stop_event.is_set():
                delivery = _activate_registration(registration, config, catalog, service, delivery, emit_fn)
                try:
                    service.tick()
                except (OSError, sqlite3.Error):
                    # In-memory snapshots and uncommitted state are retained;
                    # do not turn a transient disk/SQLite error into a lost round.
                    emit_fn({"event": "local_persistence_retry"})
                state = _catalog_event(catalog, registration)
                if state != last_catalog:
                    last_catalog = state
                    emit_fn(state)
                if once:
                    break
                stop_event.wait(observer.config.poll_seconds)
        except KeyboardInterrupt:
            emit_fn({"event": "companion_stopped", **outbox.status()})
        finally:
            if registration:
                registration.close()
            if delivery:
                delivery.close()
            service.close()
    return 0


def _run_replay(config, *, once=False, emit_fn=emit, stop_event=None):
    """Shared automatic replay loop for the CLI and the desktop window."""
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("live_capture_requires_64bit_windows_python")
    from .replay_capture import ReplayCaptureService
    from .live_runtime import AutomaticWindowsObserver
    stop_event = stop_event or threading.Event()
    with RunLock(config.data_directory):
        outbox = Outbox(config.data_directory / "outbox.sqlite3")
        catalog = _catalog(config)
        observer = AutomaticWindowsObserver(config, catalog=catalog, with_replays=True)
        service = ReplayCaptureService(config, observer, outbox, uploader=_capture_uploader(config, emit_fn), emit=emit_fn)
        delivery = DeliveryWorker(service) if service.uploader is not None else None
        registration = _start_registration(config, outbox, service, emit_fn)
        emit_fn({"event": "companion_started", "version": __version__, "collection_mode": "replay",
                 "upload_enabled": service.uploader is not None, "process_access": "read_only"})
        try:
            if delivery:
                delivery.start()
            last_catalog = None
            while not stop_event.is_set():
                delivery = _activate_registration(registration, config, catalog, service, delivery, emit_fn)
                try:
                    service.tick()
                except (OSError, sqlite3.Error):
                    emit_fn({"event": "local_persistence_retry"})
                state = _catalog_event(catalog, registration)
                if state != last_catalog:
                    last_catalog = state
                    emit_fn(state)
                if once:
                    break
                stop_event.wait(observer.config.poll_seconds)
        except KeyboardInterrupt:
            emit_fn({"event": "companion_stopped", **outbox.status()})
        finally:
            if registration:
                registration.close()
            if delivery:
                delivery.close()
            service.close()
    return 0






def parser():
    root = argparse.ArgumentParser(description="SD2 玩家伴随程序：实时内存会话／本地重放自动采集与上传。")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="生成自动录像采集配置；无需房间 ID")
    init.add_argument("--output", type=Path, default=DEFAULT_CONFIG)
    init.add_argument("--force", action="store_true")
    resources = commands.add_parser("check-resources", help="检查打包地图、估值目录与录像解析器，不读取游戏进程")
    resources.add_argument("--output", type=Path, required=True)
    resources.add_argument("--replay", type=Path)
    resources.add_argument("--test-restart", action="store_true", help=argparse.SUPPRESS)
    connection = commands.add_parser("check-connection", help="检查默认连接和自动安装注册，不读取游戏进程")
    connection.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    connection.add_argument("--output", type=Path, required=True)
    connection.add_argument("--fresh-installation", action="store_true",
                            help="检查安装专用凭据，不使用已有管理员凭据")
    for name, help_text in (("desktop", "打开自动录像采集窗口，无需命令行"),
                            ("doctor", "检查配置和游戏文件，不读取游戏进程"),
                            ("run", "自动跟踪游戏和任务报告"),
                            ("status", "查询本地待传队列"),
                            ("upload", "发送本地已落盘的待传报告，不读取游戏"),
                            ("retry", "修正配置后，将已拒绝的报告放回待传队列")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "desktop":
            sub.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
        if name == "run":
            sub.add_argument("--once", action="store_true", help="只轮询一次；不会等待对局结束")
            sub.add_argument("--mode", choices=("auto", "replay", "live"), help="覆盖配置中的采集模式")
        if name == "retry":
            sub.add_argument("--limit", type=int, default=100)
    return root


def default_arguments(argv, *, stream=sys.stdout, frozen=None, arguments=None):
    """Let a packaged windowed build open the desktop window on double-click.

    A windowed executable has no console to show an argparse usage error, and a
    player should not need to type a subcommand, so an argument-free launch of a
    frozen build starts the desktop window instead.
    """
    if argv is not None:
        return argv
    if arguments is None:
        arguments = sys.argv[1:]
    if arguments:
        return None
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    console = stream is not None and hasattr(stream, "write")
    return ["desktop"] if frozen and not console else None


def main(argv=None):
    args = parser().parse_args(default_arguments(argv))
    try:
        if args.command == "check-resources":
            from .resources import check_resources, stage_restart_test
            if args.test_restart:
                stage_restart_test(args.output)
            else:
                atomic_json(args.output, check_resources(args.replay))
            return 0
        if args.command == "init":
            if args.output.exists() and not args.force:
                raise ConfigError("configuration_exists_use_force_to_replace")
            atomic_json(args.output, example_config())
            emit({"event": "configuration_template_written", "path": str(args.output.resolve()),
                  "next": "可直接双击启动，默认自动连接服务器；可在设置中关闭在线服务。"})
            return 0
        if args.command == "desktop":
            from .desktop import launch
            return launch(args.config, elevated=args.elevated)
        config = load(args.config)
        if args.command == "check-connection":
            from .bootstrap import check_connection
            atomic_json(args.output, check_connection(config, fresh_installation=args.fresh_installation))
            return 0
        if args.command == "doctor":
            from .native_report import SUPPORTED_EXE_SHA256
            result = {"event": "configuration_checked", "room_count": len(config.rooms),
                      "catalog_configured": config.catalog_url is not None,
                      "catalog_interval_seconds": config.catalog_interval_seconds,
                      "collection_mode": config.collection_mode,
                      "windows_64bit": os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8,
                      "target_hosts_configured": bool(config.rooms) and all(e.host != "YOUR_GAME_SERVER_IP" for r in config.rooms for e in r.endpoints),
                      "replay_directory_count_existing": sum(p.is_dir() for p in config.replay_directories),
                      "upload_enabled": config.upload_url is not None,
                      "token_present": bool(get_upload_token(config.upload_url, config.token_env)) if config.upload_url else bool(os.environ.get(config.token_env)),
                      "game_process_accessed": False, "game_started": False}
            if config.game_executable:
                actual = hashlib.sha256(config.game_executable.read_bytes()).hexdigest()
                result["executable_supported"] = actual == SUPPORTED_EXE_SHA256
                result["executable_sha256"] = actual
            emit(result)
            return 0
        if args.command == "run":
            if args.mode:
                config = replace(config, collection_mode=args.mode)
            return _run(config, once=args.once)
        outbox = Outbox(config.data_directory / "outbox.sqlite3")
        if args.command == "status":
            emit({"event": "outbox_status", **outbox.status()})
            return 0
        if args.command == "retry":
            with RunLock(config.data_directory):
                count = outbox.retry_rejected(limit=args.limit)
            emit({"event": "rejected_reports_requeued", "count": count, **outbox.status()})
            return 0
        try:
            uploader = _uploader(config)
        except ConfigError as exc:
            from .bootstrap import BootstrapClient, can_register
            if str(exc) != "upload_token_environment_variable_missing" or not can_register(config):
                raise
            token = BootstrapClient(config, outbox.installation_id).register()
            uploader = Uploader(config.upload_url, token=token, token_env=config.token_env,
                                allow_loopback_http=config.allow_loopback_http)
        if uploader is None:
            raise ConfigError("upload_url_not_configured")
        from .upload import UploadError
        with RunLock(config.data_directory):
            sent = 0
            for job in outbox.due(limit=100):
                try:
                    receipt = uploader.send(job["payload"])
                except UploadError as exc:
                    if exc.retryable:
                        outbox.mark_retry(job["id"], exc.code, retry_after=exc.retry_after)
                    else:
                        outbox.mark_rejected(job["id"], exc.code)
                else:
                    outbox.mark_sent(job["id"], receipt)
                    sent += 1
            emit({"event": "outbox_upload_pass_finished", "acknowledged": sent, **outbox.status()})
        return 0
    except (OSError, ValueError, NativeReportError, sqlite3.Error) as exc:
        # Avoid printing arbitrary input/JSON/token/OS path contents.
        message = str(exc)
        code = message if re.fullmatch(r"[a-z][a-z0-9_]{0,100}", message) else type(exc).__name__
        error = {"event": "companion_error", "code": code}
        if code in ("configuration_missing", "configure_target_game_server_host_first"):
            error["config_path"] = str(args.config.resolve())
            error["next"] = ("配置文件不存在。先运行 init，或重新双击 run_companion.cmd 自动生成配置。"
                             if code == "configuration_missing" else
                             "请在配置中填写服务器游戏 IP/端口和录像目录，保存后重新启动。")
            emit(error)
            return 3  # The Windows launcher opens the configuration for editing.
        emit(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

