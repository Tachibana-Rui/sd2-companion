"""Optional Windows desktop entry; imports do not create UI or open a process."""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata

from .config import ConfigError, catalog_url_for_upload, decode_config, example_config, load, save_raw
from . import __version__
from . import maps
from . import replay_evidence
from .local_replay_library import (LocalReplayLibrary, associate, default_replay_directories,
                                   load_link, report_fingerprint)
from .service import LOCAL_REPORT_SCHEMA, TASK_REPORT_SCHEMA


_CODE = re.compile(r"[a-z][a-z0-9_]{0,100}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TITLE = "SD2 录像时间表采集 " + __version__
_TEXT = {
    # English is the default language; every visible string goes through here.
    "title": ("SD2 Online match / Local replay Analysis Tool", "SD2 录像时间表采集 {version}"),
    "intro_1": ("Keep this window open while you play Steel Division 2.",
                "进行 Steel Division 2 对局时请保持本窗口运行。"),
    "intro_2": ("When a match ends, the companion reads the debrief (game timeline) from game memory and "
                "uploads matches played on the [ASIA-JP] server for analysis.",
                "对局结束后，本程序从游戏内存读取任务报告-游戏时间表，并把在 [ASIA-JP] 服务器上进行的对局"
                "自动上传到服务器做数据分析。"),
    "intro_3": ("If the companion was not running during a match, start it first, play the entire local replay "
                "in game, and leave the debrief open afterward. The companion reads the timeline and matches "
                "it to the server record automatically. Local analysis also works offline.",
                "若对局时忘开助手，请先运行本程序，再在游戏里完整播放本地录像；结束后停留在任务报告界面，"
                "软件自动读取时间表并匹配服务器对局。本地分析也可离线使用。"),
    "menu_file": ("File", "文件"),
    "menu_open_reports": ("Open report folder", "打开报告目录"),
    "menu_open_replays": ("Open SD2 local replay folder", "打开 SD2 本地录像目录"),
    "replays_missing": ("The SD2 local replay folder does not exist yet. Start the game and save a replay first.",
                        "SD2 本地录像目录尚不存在。请先启动游戏并保存一份录像。"),
    "replays_open_failed": ("The SD2 local replay folder could not be opened.", "无法打开 SD2 本地录像目录。"),
    "online_enabled": ("Record matches online automatically", "自动在线记录对局"),
    "online_restart": ("Restart the companion to apply this change.", "重新启动助手后此更改生效。"),
    "menu_link_replay": ("Add local replay to selected analysis…", "为所选分析补充本地录像…"),
    "menu_link_server": ("Link local replay to server match…", "将本地录像关联到服务器对局…"),
    "server_link_select": ("Select a captured analysis first. If you did not run the companion during the match, "
                           "keep it running while you play the entire local replay in game, then leave the debrief open. "
                           "Its timeline will be read and matched automatically.",
                           "请先选中已采集的分析记录。若对局时忘开助手，请保持助手运行，在游戏里完整播放本地录像，"
                           "结束后停留在任务报告界面，软件会自动读取时间表并匹配服务器对局。"),
    "menu_delete_record": ("Delete selected local record…", "删除所选本机记录…"),
    "delete_select": ("Select the local record to delete first.", "请先选中需要删除的本机记录。"),
    "delete_confirm": ("Delete this record from this computer and cancel its pending uploads? "
                       "A recovery copy is kept. Game replay files and records already stored on the website are kept.",
                       "是否删除所选本机记录并取消其待发送任务？会保留回收副本。游戏录像文件与网站已收录的数据会保留。"),
    "delete_working": ("Deleting the selected local record…", "正在删除所选本机记录…"),
    "delete_done": ("Local record deleted.", "本机记录已删除。"),
    "delete_failed": ("The local record could not be deleted completely. Try again.",
                      "本机记录未能完整删除，请重试。"),
    "menu_settings": ("Settings", "设置"),
    "menu_exit": ("Exit", "退出"),
    "menu_help": ("Help", "帮助"),
    "menu_usage": ("Usage", "使用说明"),
    "menu_diagnostics": ("Diagnostics", "诊断信息"),
    "menu_version": ("Version", "版本"),
    "menu_updates": ("Check for updates", "自动更新"),
    "updates_automatic": ("Check for updates automatically", "自动检查软件更新"),
    "updates_current": ("Version {version} is up to date.", "当前版本 {version} 已是最新。"),
    "updates_failed": ("Update check or download failed. Capture continues; try again from Help → Check for updates.",
                       "更新检查或下载失败。采集继续运行，可从“帮助 → 自动更新”重试。"),
    "updates_offer": ("Version {version} is available. Download, verify and restart now? Your configuration and reports will be kept.",
                      "发现新版本 {version}。是否下载、校验并重启更新？配置和报告会保留。"),
    "updates_source": ("Version {version} is available. Download the Windows executable from:\n{url}",
                       "发现新版本 {version}，可下载 Windows 程序：\n{url}"),
    "updates_install_failed": ("The update could not be installed. Close the companion and replace the executable manually; reports have been kept.",
                               "更新无法安装。请关闭助手后手动替换程序，报告已保留。"),
    "settings_upload_address": ("Upload HTTPS address (blank keeps the current address)", "上传 HTTPS 地址（留空保留原地址）"),
    "settings_upload_token": ("Upload credential (blank keeps the stored credential)", "上传凭据（留空保留已存凭据）"),
    "settings_upload_save": ("Save upload settings", "保存上传设置"),
    "settings_upload_saved": ("Upload settings saved. Restart the companion to connect. The credential is stored in Windows Credential Manager.",
                              "上传设置已保存，请重新启动助手连接。凭据保存在 Windows 凭据管理器中。"),
    "settings_upload_failed": ("Upload settings could not be saved. Use a valid HTTPS address and check Windows Credential Manager access.",
                               "上传设置保存失败。请检查 HTTPS 地址及 Windows 凭据管理器访问权限。"),
    "counts": ("Analyzed: {analyzed}　Local only: {unlinked}　Queued: {queued}　"
               "Received by server: {received}　Matched: {matched}　Server pending: {server_pending}　Upload failed: {failed}",
               "已分析：{analyzed}　仅本地：{unlinked}　待传：{queued}　"
               "服务器已收件：{received}　已匹配：{matched}　服务器待匹配：{server_pending}　上传失败：{failed}"),
    "counts_pending": ("Analyzed: —　Local only: —　Queued: —　Received by server: —　Matched: —　Server pending: —　Upload failed: —",
                       "已分析：—　仅本地：—　待传：—　服务器已收件：—　已匹配：—　服务器待匹配：—　上传失败：—"),
    "filter_label": ("Show:", "显示："),
    "filter_all": ("All", "全部"),
    "filter_local": ("Local replay", "本地录像"),
    "filter_online": ("Online match", "在线对局"),
    "filter_hint": ("Double-click a row for the local analysis", "双击一行查看该局本地分析"),
    "column_kind": ("Category", "类别"),
    "column_server_status": ("Server status", "服务器状态"),
    "record_local_only": ("Local only", "仅本地"),
    "record_queued": ("Waiting for upload", "待上传"),
    "record_rejected": ("Upload failed", "上传失败"),
    "record_received": ("Received; checking", "已收件，待确认"),
    "record_pending_match": ("Server match pending", "服务器待匹配"),
    "record_accepted": ("Matched", "已匹配"),
    "column_duration": ("Duration", "时长"),
    "column_analyzed": ("Analyzed", "分析时间"),
    "column_map": ("Map", "地图"),
    "column_players": ("Players", "参与玩家"),
    "kind_local": ("Local replay", "本地录像"),
    "kind_online": ("Online match", "在线对局"),
    "empty": ("No debrief has been analyzed yet.", "尚未分析到本地或在线任务报告。"),
    "row_count": ("{count} matches; double-click one for the local analysis.",
                  "共 {count} 局；双击一行查看本地分析。"),
    "row_count_one": ("1 match; double-click it for the local analysis.", "共 1 局；双击该行查看本地分析。"),
    "read_errors": ("　Read failures: {count}", "　读取失败：{count}"),
    "status_starting": ("Starting live and local replay capture…", "正在启动实时完赛与本地重放采集…"),
    "close_wait": ("Stopping capture and releasing the replay monitor…", "正在停止采集并释放录像监控，请稍候…"),
    "close_long": ("Still releasing the replay monitor and local saves…", "仍在释放录像监控和本地保存任务，请稍候…"),
}
_LOCAL_REPORT_SCHEMA = LOCAL_REPORT_SCHEMA
_EXPORT_SCHEMAS = frozenset((TASK_REPORT_SCHEMA,))
# Each capture directory proves where a match came from: "live" rows were played on
# a server, "replay" rows come from watching a local file, "export" is the bound
# snapshot written after a successful link.
_SOURCES = (("unbound-live", "live"), ("unbound-replays", "replay"),
            ("unbound", "replay"), ("exports", "export"))
_STATUS_EN = {
    "client_registration_pending": "Connecting to the default server automatically; local capture continues.",
    "client_registration_retry": "The default server is temporarily unavailable; connecting again automatically.",
    "automatic_registration_retry": "The default server is temporarily unavailable; connecting again automatically.",
    "automatic_registration_invalid_response": "The connection could not be verified; local capture continues and the companion will retry.",
    "automatic_credential_storage_failed": "The automatic credential could not be saved; local capture continues and the companion will retry.",
    "automatic_credential_invalid": "The saved automatic credential cannot be used; local capture continues.",
    "installation_credential_conflict": "The automatic credential does not match this installation; local capture continues.",
    "online_services_ready": "Server connection ready; matches will be recorded automatically.",
    "desktop_starting": "Starting live and local replay capture…",
    "waiting_for_replay_file_access": "Waiting for the game to open a local replay.",
    "game_not_running": "Waiting for Steel Division 2 to start.",
    "replay_file_access_observed": "Game replay access detected; monitoring the debrief timeline.",
    "replay_file_identified": "Replay identified; waiting for the debrief.",
    "replay_report_queued": "Debrief timeline saved.",
    "replay_report_waiting_for_stable_file": "The replay is still being written; waiting for it to settle.",
    "replay_report_waiting_for_unique_file": "Several replay candidates; the report is kept until one is confirmed.",
    "replay_report_waiting_for_matching_file": "Report kept; waiting for the replay that was actually opened.",
    "replay_access_uncertain_report_retained": "Replay access evidence is incomplete; the report stays local.",
    "replay_evidence_unavailable": "Replay parsed without investment evidence; kills and losses still work.",
    "upload_acknowledged": "The receiver confirmed delivery; linking continues on the server.",
    "upload_retry": "Upload did not succeed yet; stored data will retry automatically.",
    "upload_rejected": "The receiver rejected the upload; check the address and credentials.",
    "local_persistence_retry": "Local save did not succeed yet; retrying.",
    "file_monitor_running": "Replay monitor running; waiting for the game to open a replay.",
    "file_monitor_requires_elevation": "Replay monitoring needs administrator rights; live capture continues.",
    "live_session_observed": "Live session recognised in game memory; waiting for the finish report.",
    "live_report_queued": "Live debrief saved and queued for upload.",
    "task_report_captured": "Debrief captured locally; session link and upload show separately.",
    "live_report_waiting_for_session": "Waiting for the live session from game memory; no replay file needed.",
    "live_report_waiting_for_server": "Waiting for the server to confirm this match; delivery is not acceptance.",
    "live_report_source_unknown": "Match source not confirmed yet; waiting for clear live or replay evidence.",
    "live_report_wrong_session": "The report does not match the current live session; waiting for the right link.",
    "unsupported_executable": "This game build is not supported yet; no substitute data was produced.",
    "locating_report_controller": "Locating the debrief.",
    "waiting_for_report_controller": "No report yet; stay on the report screen and the companion keeps watching.",
    "native_scan_budget_exceeded": "Report scan hit its limit; update the program and restart the companion.",
    "multiple_game_processes": "Several game instances detected; keep one, or set the game file in settings.",
    "companion_already_running": "Another companion is running; close the old window and start again.",
    "upload_token_environment_variable_missing": "Upload credential missing; check the settings and restart.",
    "companion_stopped": "Capture stopped.",
    "desktop_capture_failed": "Capture failed and stopped; check the settings and restart the program.",
}


def _language(value):
    """Only English and Chinese exist; anything else falls back to English."""
    return "zh" if value == "zh" else "en"


def _status_text(model, language):
    if language != "en":
        return model.status
    code = model.code if isinstance(model.code, str) else ""
    if code in ("upload_acknowledged", "server_receipt_updated"):
        return {"accepted": "The server matched this report to its recorded match.",
                "pending_match": "Received by server; waiting for a matching recorded match.",
                "duplicate": "Already received; checking the server's current match status."}.get(
                    getattr(model, "receipt_status", ""), "Checking server match status…")
    return _STATUS_EN.get(code, "Working…" if not code else "Capture status: " + code)


_CATALOG_TEXT = {
    "ok": ("Server rooms: synced {time} ({rooms})", "服务器房间目录：已同步 {time}（{rooms} 个）"),
    "unreachable": ("Server rooms: offline; local capture continues", "服务器房间目录：暂时离线，本地采集继续"),
    "unauthorized": ("Server rooms: credential rejected", "服务器房间目录：凭证被拒绝"),
    "invalid": ("Server rooms: server sent an unusable table", "服务器房间目录：服务器内容不可用"),
    "no_credentials": ("Server rooms: upload credential missing", "服务器房间目录：缺少上传凭证"),
    "pending": ("Server rooms: fetching", "服务器房间目录：正在获取"),
    "connecting": ("Server connection: retrying automatically; local capture continues",
                   "服务器连接：正在自动重试，本地采集继续运行"),
    "disabled": ("Server rooms: not configured (local capture only)", "服务器房间目录：未配置上传（仅本地采集）"),
}


def catalog_text(model, language, *, now=None):
    """One short line describing the remote room table, or empty when unused."""
    state = getattr(model, "catalog_state", "")
    if not state:
        return ""
    last_success = getattr(model, "catalog_last_success", None)
    rooms = getattr(model, "catalog_rooms", 0)
    if state != "ok" and last_success is not None and rooms:
        minutes = max(0, int(((time.time() if now is None else now) - last_success) // 60))
        return (f"Server rooms: offline {minutes} min; using the last verified table ({rooms})" if language == "en"
                else f"服务器房间目录：离线 {minutes} 分钟，沿用上次核验的目录（{rooms} 个）")
    english, chinese = _CATALOG_TEXT.get(state, _CATALOG_TEXT["invalid"])
    synced = time.strftime("%H:%M", time.localtime(last_success)) if last_success is not None else "—"
    return (english if language == "en" else chinese).format(rooms=rooms, time=synced)


_MESSAGES = {
    "desktop_requires_64bit_windows": "请使用 Windows 64 位 Python 运行本程序。",
    "desktop_elevation_cancelled": "未获得录像文件监控授权，本地重放自动识别暂不可用。\n\n实时完赛仍会尝试从游戏内存采集，不需要本地录像。需要自动识别重放时，可重新打开程序并允许 Windows 授权。",
    "desktop_elevation_failed": "无法取得录像文件监控权限，本地重放自动识别暂不可用。\n\n将继续尝试读取实时对局状态；实时完赛不需要本地录像。",
    "desktop_elevation_not_granted": "程序仍未取得录像文件监控权限，已停止重复请求授权。\n\n本地重放自动识别暂不可用；将继续尝试从游戏内存采集实时完赛。",
    "desktop_tk_unavailable": "图形界面组件 tkinter 不可用。\n\n请使用带 tkinter 的 Windows 64 位 Python。",
}
_STATUS = {
    "client_registration_pending": "正在自动连接默认服务器，本地采集继续运行。",
    "client_registration_retry": "默认服务器暂时不可用，正在自动重试连接。",
    "automatic_registration_retry": "默认服务器暂时不可用，正在自动重试连接。",
    "automatic_registration_invalid_response": "连接暂未通过核验，本地采集继续运行，稍后自动重试。",
    "automatic_credential_storage_failed": "自动连接凭据暂未保存成功，本地采集继续运行，稍后自动重试。",
    "automatic_credential_invalid": "已存的自动连接凭据暂不可用，本地采集继续运行。",
    "installation_credential_conflict": "自动连接凭据与当前安装不一致，本地采集继续运行。",
    "online_services_ready": "服务器连接就绪，将自动记录对局。",
    "desktop_starting": "正在启动实时完赛与本地重放采集…",
    "waiting_for_replay_file_access": "等待游戏打开本地录像。",
    "game_not_running": "等待 Steel Division 2 启动。",
    "replay_file_access_observed": "已检测到游戏读取录像，正在监测任务报告时间表。",
    "replay_file_identified": "已识别录像，等待任务报告。",
    "replay_report_queued": "任务报告时间表已保存。",
    "replay_report_waiting_for_stable_file": "录像正在保存，等待文件稳定。",
    "replay_report_waiting_for_unique_file": "检测到多个候选录像，报告已保留，等待明确关联。",
    "replay_report_waiting_for_matching_file": "报告已保留，等待匹配实际打开的录像。",
    "replay_access_uncertain_report_retained": "录像访问证据不完整，报告已保留在本地。",
    "upload_acknowledged": "接收端已确认收件；数据库关联由接收端继续处理。",
    "upload_retry": "上传暂未成功，已保存的数据会自动重试。",
    "upload_rejected": "接收端拒绝上传，请检查上传地址和凭证设置。",
    "local_persistence_retry": "本地保存暂未成功，正在重试。",
    "file_monitor_running": "录像监控已启动，等待游戏打开录像。",
    "file_monitor_requires_elevation": "本地重放文件监控需要管理员权限；实时完赛仍继续监测，不需要本地录像。",
    "live_session_observed": "已识别游戏内存中的实时会话，等待完赛任务报告。",
    "live_report_queued": "实时任务报告时间表已保存并进入待传队列。",
    "task_report_captured": "任务报告已采集到本地；会话关联与上传状态单独显示。",
    "live_report_waiting_for_session": "等待从游戏内存确认实时会话，不需要本地录像。",
    "live_report_waiting_for_server": "等待服务器核对本局；已收件不代表已经验收。",
    "live_report_source_unknown": "对局来源尚未确认，等待明确的实时或重放证据。",
    "live_report_wrong_session": "报告与当前实时会话不一致，等待正确关联。",
    "unsupported_executable": "当前游戏版本尚未适配，未生成替代数据。",
    "locating_report_controller": "正在定位任务报告。",
    "waiting_for_report_controller": "当前未发现报告，继续监测；请在任务报告界面停留。",
    "native_scan_budget_exceeded": "报告扫描达到旧版上限，尚未读取报告。请更新程序、关闭旧实例后重新启动；能否找到报告仍需确认。",
    "multiple_game_processes": "检测到多个游戏实例，请保留一个实例，或在设置中指定游戏文件。",
    "companion_already_running": "另一个采集程序正在运行；升级后请先关闭旧窗口，再重新启动。",
    "upload_token_environment_variable_missing": "上传凭证未设置；请检查设置后重新打开程序。",
    "companion_stopped": "采集已停止。",
    "desktop_capture_failed": "采集遇到错误并已停止，请检查设置后重新打开程序。",
}


class DesktopError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def prepare_config(path):
    """Create first-run defaults without replacing an existing user's file."""
    path = Path(path).resolve()
    if not path.exists():
        # The envelope keeps room endpoints and the upload URL out of casual
        # sight; a legacy plain-JSON file is only ever read, never rewritten.
        save_raw(path, example_config())
    # Desktop always selects both capture paths automatically. Older settings remain
    # intact on disk, including their rooms, paths and upload preferences.
    return replace(load(path), collection_mode="auto")


def _is_admin():
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    function = shell.IsUserAnAdmin
    function.argtypes = []
    function.restype = ctypes.c_int
    return bool(function())


def _shell_execute(verb, executable, parameters, directory):
    # Microsoft: runas triggers UAC; a ShellExecuteW result > 32 means success.
    # https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shellexecutew
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    function = shell.ShellExecuteW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                         ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    function.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    value = function(None, verb, str(executable), parameters, str(directory), 1)
    return int(value or 0), ctypes.get_last_error()


def elevation_command(config_path, *, executable=None, entrypoint=None):
    config_path = Path(config_path).resolve()
    executable = Path(executable or sys.executable).resolve()
    if getattr(sys, "frozen", False):
        # A packaged build has no Python script to hand over: relaunch this
        # executable, which is already the companion entry point.
        arguments = subprocess.list2cmdline(
            ["desktop", "--config", str(config_path), "--elevated"])
        return executable, arguments, config_path.parent
    pythonw = executable.with_name("pythonw.exe")
    if executable.name.casefold() == "python.exe" and pythonw.is_file():
        executable = pythonw
    entrypoint = Path(entrypoint or Path(__file__).resolve().parents[1] / "player_companion.py").resolve()
    arguments = ["-B", str(entrypoint), "desktop", "--config", str(config_path), "--elevated"]
    return executable, subprocess.list2cmdline(arguments), entrypoint.parent


def request_elevation(config_path):
    executable, arguments, directory = elevation_command(config_path)
    result, error = _shell_execute("runas", executable, arguments, directory)
    if result <= 32:
        raise DesktopError("desktop_elevation_cancelled" if error == 1223 else "desktop_elevation_failed")


def _message(text):
    """A pythonw-safe fallback when Tk cannot open; never print raw exceptions."""
    if os.name != "nt":
        return
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        function = user.MessageBoxW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        function(None, text, _TITLE, 0x10)
    except (OSError, AttributeError):
        pass


def _safe_event(value):
    if not isinstance(value, dict):
        return {"event": "desktop_unknown_status"}
    event = value.get("event")
    result = {"event": event if isinstance(event, str) and _CODE.fullmatch(event) else "desktop_unknown_status"}
    code = value.get("code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        result["code"] = code
    if result["event"] == "catalog_state":
        state = value.get("state")
        if isinstance(state, str) and state in _CATALOG_TEXT:
            result["state"] = state
        rooms = value.get("room_count")
        if type(rooms) is int and 0 <= rooms <= 16:
            result["room_count"] = rooms
        stamp = value.get("last_success")
        if (not isinstance(stamp, bool) and isinstance(stamp, (int, float))
                and math.isfinite(stamp) and 0 < stamp <= 253402300799):
            result["last_success"] = stamp
    for key in ("total", "pending", "sent", "rejected", "rows", "exit_code",
                "analyzed", "unbound", "bound", "inventory_errors", "matched", "server_pending",
                "official_score_players", "total_players", "resolved_actors", "total_actors",
                "scan_mib", "scan_total_mib", "scan_rounds_completed"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 1_000_000_000:
            result[key] = number
    fingerprint = value.get("replay_sha256")
    if isinstance(fingerprint, str) and _HASH.fullmatch(fingerprint):
        result["replay_sha256"] = fingerprint
    name = value.get("replay_name")
    if (isinstance(name, str) and 0 < len(name) <= 256 and name not in (".", "..")
            and "/" not in name and "\\" not in name
            and not any(unicodedata.category(char) == "Cc" for char in name)):
        result["replay_name"] = name
    receipt = value.get("receipt_status")
    if receipt in ("accepted", "pending_match", "duplicate"):
        result["receipt_status"] = receipt
    mode = value.get("collection_mode")
    if mode in ("auto", "live", "replay"):
        result["collection_mode"] = mode
    session_id = value.get("session_id")
    if (isinstance(session_id, str) and 0 < len(session_id) <= 128
            and not any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in session_id)):
        result["session_id"] = session_id
    return result


def _replay_digest(report):
    """The client replay digest when the payload carries a valid one."""
    value = report.get("client_replay_sha256")
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _first_text(*values):
    """First non-empty string in the list, else None."""
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text[:128]
    return None


def _map_label(report, metadata, replay, directory):
    """Prefer a readable battlefield name over the raw map id.

    The debrief itself never carries a map, so the name comes from the export
    metadata, the local snapshot wrapper, or — for a replay — the parsed evidence
    that the companion already stores under its digest. A raw id is translated
    through the same map table the website uses.
    """
    named = _first_text(report.get("map_name"), metadata.get("map_name") if metadata is not report else None)
    if named:
        return named
    identifier = _first_text(report.get("map"), metadata.get("map") if metadata is not report else None)
    if replay:
        evidence = replay_evidence.load(directory, replay)
        match = (evidence or {}).get("match") if isinstance(evidence, dict) else None
        if isinstance(match, dict):
            resolved = _first_text(match.get("map_name"), match.get("map"))
            if resolved:
                return resolved
    return maps.display_name(identifier) if identifier else None


def _scope_metrics(player):
    """Pick the scope a local (archive-free) analysis can actually fill.

    Without the replay's production requests every unit has an unknown role, so
    the main and transport scopes stay empty; all components still counts every
    event. The website defaults to the same scope for the same reason.
    """
    scopes = player.get("economy_by_scope") if isinstance(player, dict) else None
    if isinstance(scopes, dict):
        for scope in ("all_components", "main"):
            value = scopes.get(scope)
            if isinstance(value, dict) and value.get("kill_count"):
                return scope, value
        value = scopes.get("all_components") or scopes.get("main")
        if isinstance(value, dict):
            return "all_components" if scopes.get("all_components") else "main", value
    economy = player.get("economy") if isinstance(player, dict) else None
    return "main", economy if isinstance(economy, dict) else {}


def _player_names(players):
    """Bounded nickname list for the results list, in native roster order."""
    names = []
    for player in players or []:
        if not isinstance(player, dict):
            continue
        name = player.get("name")
        if not isinstance(name, str):
            continue
        name = "".join(char for char in name if unicodedata.category(char) != "Cc").strip()
        if name:
            names.append(name[:64])
        if len(names) >= 32:
            break
    return tuple(names)


def _row_duration(rows):
    """Battle length from the native timeline; the last event is the end."""
    longest = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        seconds = row.get("game_time_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            continue
        if not math.isfinite(seconds) or not 0 <= seconds <= 24 * 3600:
            continue
        longest = seconds if longest is None else max(longest, seconds)
    return int(round(longest)) if longest is not None else None


class LocalReportInventory:
    """Count captured reports independently of the upload queue.

    Every automatic export retains its original local snapshot. Pair those
    copies by their complete native report values, with multiplicity, so the
    same capture is counted once. No native rows are modified or deduplicated.
    This inventory never supplies evidence for session binding or upload.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.cache = {}
        self.delivery = None

    def _deleted(self):
        from .local_deletion import deleted_fingerprints
        return deleted_fingerprints(self.directory)

    def _delivery_statuses(self):
        from .outbox import Outbox
        try:
            if self.delivery is None and (self.directory / "outbox.sqlite3").is_file():
                self.delivery = Outbox(self.directory / "outbox.sqlite3")
            return self.delivery.report_statuses() if self.delivery else {}
        except (OSError, ValueError, sqlite3.Error):
            return {}

    def _read(self, path, kind):
        before = path.stat()
        signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        previous = self.cache.get(path)
        if previous is not None and previous[0] == signature:
            return previous[1]
        if before.st_size > 64 * 1024 * 1024:
            raise ValueError("local_report_too_large")
        value = json.loads(path.read_text(encoding="utf-8"))
        after = path.stat()
        if signature != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("local_report_changed")
        if not isinstance(value, dict):
            raise ValueError("invalid_local_report")
        if kind == "export":
            if value.get("schema") not in _EXPORT_SCHEMAS:
                self.cache[path] = signature, None
                return None
            report = value
            generation, players = report.get("report_generation"), report.get("native_players")
            submission = report.get("submission_id")
            metadata = report
            if not isinstance(submission, str) or not submission:
                raise ValueError("invalid_local_export")
            # An export that carries a replay digest came from a local replay; the
            # rest are live server matches captured through the game's session.
            evidence = report.get("client_context_evidence") or {}
            replay_source = (evidence.get("native_source_kind") == "replay"
                             or evidence.get("association") in ("native_replay_session", "user_selected_local_replay",
                                 "observed_game_file_read", "observed_game_file_write"))
            source = "replay" if replay_source or _replay_digest(report) else "live"
        else:
            if value.get("schema") != _LOCAL_REPORT_SCHEMA:
                self.cache[path] = signature, None
                return None
            if not isinstance(value.get("report"), dict):
                raise ValueError("invalid_local_report")
            report = value["report"]
            generation, players = report.get("report_generation"), report.get("native_players")
            submission = None
            metadata = value
            source = ("replay" if metadata.get("source_kind") == "replay"
                      or metadata.get("session_origin") == "native_replay_context"
                      else "live" if kind == "live" or metadata.get("source_kind") == "network_live" else "unverified")
        build = report.get("game_build")
        rows = report.get("rows")
        if (type(build) is not int or type(generation) is not int
                or not isinstance(players, list) or not isinstance(rows, list)):
            raise ValueError("invalid_local_report")
        fingerprint = report_fingerprint(report)
        replay = _replay_digest(report)
        result = {"fingerprint": fingerprint, "submission": submission,
                  "players": len(players), "rows": len(rows),
                  "names": _player_names(players), "duration": _row_duration(rows),
                  "map": _map_label(report, metadata, replay, self.directory),
                  "map_named": bool(_first_text(report.get("map_name"), metadata.get("map_name"))),
                  "server": _first_text(report.get("server_name"), metadata.get("server_name")),
                  "session": _first_text(report.get("session_id"), metadata.get("session_id")),
                  "session_origin": ("verified_export" if kind == "export" else metadata.get("session_origin")),
                  "replay": replay, "source": source,
                  "path": path}
        self.cache[path] = signature, result
        return result

    def poll(self):
        local, exported, submissions = Counter(), Counter(), set()
        deleted = self._deleted()
        seen, errors = set(), 0
        for name, kind in _SOURCES:
            try:
                paths = list((self.directory / name).rglob("*.json"))
            except OSError:
                errors += 1
                continue
            for path in paths:
                seen.add(path)
                try:
                    item = self._read(path, kind)
                    if item is None or item["fingerprint"] in deleted:
                        continue
                    fingerprint, submission = item["fingerprint"], item["submission"]
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    errors += 1
                    continue
                if kind == "export":
                    if submission in submissions:
                        continue
                    submissions.add(submission)
                    exported[fingerprint] += 1
                else:
                    local[fingerprint] += 1
        self.cache = {path: value for path, value in self.cache.items() if path in seen}
        result = {"event": "local_report_inventory", "analyzed": sum((local | exported).values()),
                "unbound": sum((local - exported).values()), "bound": sum(exported.values()),
                "inventory_errors": errors}
        revisions = []
        for name in ("local-replay-links", "replay-evidence", "deleted-reports", "outbox.sqlite3", "outbox.sqlite3-wal"):
            try:
                revisions.append((self.directory / name).stat().st_mtime_ns)
            except OSError:
                pass
        if revisions:
            result["local_replay_revision"] = max(revisions)
        return result

    def entries(self):
        """One row per analyzed report, for the results list.

        `ended_at` is the moment the capture file was written (the match or the
        replay had just ended); `analyzed_at` is the later export snapshot when
        the report was bound and valued. Nothing here is uploaded or matched -
        the list is local evidence only.
        """
        reports = {}
        deleted = self._deleted()
        delivery = self._delivery_statuses()
        for name, kind in _SOURCES:
            try:
                paths = list((self.directory / name).rglob("*.json"))
            except OSError:
                continue
            for path in paths:
                try:
                    item = self._read(path, kind)
                    if item is None or item["fingerprint"] in deleted:
                        continue
                    stamp = path.stat().st_mtime
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    continue
                entry = reports.setdefault(item["fingerprint"], {
                    "fingerprint": item["fingerprint"], "players": item["players"], "rows": item["rows"],
                    "map": None, "server": None, "session": None, "replay": None,
                    "map_named": False,
                    "session_origin": None,
                    "names": (), "duration": None,
                    "kinds": set(), "sources": set(), "ended_at": stamp, "analyzed_at": stamp,
                    "path": item["path"]})
                entry["kinds"].add(kind)
                entry["map_named"] = entry["map_named"] or item["map_named"]
                entry["sources"].add(item["source"])
                if kind != "export":
                    entry["ended_at"] = min(entry["ended_at"], stamp)
                    entry["path"] = item["path"]
                else:
                    entry["analyzed_at"] = max(entry["analyzed_at"], stamp)
                if item["duration"] is not None:
                    entry["duration"] = max(entry["duration"] or 0, item["duration"])
                if not entry["names"] and item["names"]:
                    entry["names"] = item["names"]
                for key in ("map", "server", "session", "replay", "session_origin"):
                    if entry[key] is None and item[key]:
                        entry[key] = item[key]
        rows = []
        for entry in reports.values():
            link = load_link(self.directory, entry["fingerprint"])
            if link and (not entry["replay"] or entry["replay"] == link["replay_sha256"]):
                entry["replay"] = link["replay_sha256"]
                entry["session"] = entry["session"] or link.get("session_id")
                entry["map"] = maps.display_name(link.get("map")) or entry["map"]
                entry["server"] = entry["server"] or link.get("server_name")
            if entry["replay"] and not entry["map_named"]:
                entry["map"] = _map_label({}, {}, entry["replay"], self.directory) or entry["map"]
            kinds = entry.pop("kinds")
            sources = entry.pop("sources", set())
            # Playback identity comes from the native source, independently of
            # whether a file hash was found. A live match may acquire the same
            # .rpl3 later for production P without becoming a replay capture.
            entry["kind"] = ("online" if "live" in sources else "local" if "replay" in sources
                             or entry.get("replay") else "online")
            entry["linked"] = "export" in kinds
            entry.update(delivery.get(entry["fingerprint"], {"server_status": "local_only", "pending_reason": None}))
            rows.append(entry)
        rows.sort(key=lambda item: (item["ended_at"], item["fingerprint"]), reverse=True)
        return rows


@dataclass
class StatusModel:
    status: str = "正在启动实时完赛与本地重放采集…"
    code: str = "desktop_starting"
    replay_hash: str = ""
    replay_name: str = ""
    session_id: str = ""
    saved: int = 0
    sent: int = 0
    matched: int = 0
    server_pending: int = 0
    pending: int = 0
    rejected: int = 0
    queued: int = 0
    unbound: int = 0
    bound: int = 0
    inventory_ready: bool = False
    inventory_errors: int = 0
    official_score_players: int | None = None
    total_players: int | None = None
    resolved_actors: int | None = None
    total_actors: int | None = None
    rows: int | None = None
    receipt_status: str = ""
    collection_mode: str = "auto"
    scan_mib: int | None = None
    scan_total_mib: int | None = None
    scan_rounds_completed: int = 0
    catalog_state: str = ""
    catalog_rooms: int = 0
    catalog_error: str | None = None
    catalog_last_success: float | None = None

    def update(self, event):
        event = _safe_event(event)
        if event["event"] == "catalog_state":
            # The remote room table is remote configuration, not capture status.
            self.catalog_state = str(event.get("state") or "")
            self.catalog_rooms = event.get("room_count") if type(event.get("room_count")) is int else 0
            self.catalog_error = event.get("code") if isinstance(event.get("code"), str) else None
            if "last_success" in event:
                self.catalog_last_success = event["last_success"]
            return
        if event["event"] == "local_report_inventory":
            # An inventory refresh must not hide the current capture/error status.
            if "analyzed" in event:
                self.saved = event["analyzed"]
                self.inventory_ready = True
            for key in ("unbound", "bound", "inventory_errors"):
                if key in event:
                    setattr(self, key, event[key])
            return
        code = event.get("code", event["event"])
        if (code == "desktop_worker_finished" and event.get("exit_code") != 0
                and self.code != "desktop_starting"):
            # The queued finished marker must not hide the actual failure
            # emitted immediately before it, even within one UI refresh.
            if not self.status.endswith("采集已停止。"):
                self.status += " 采集已停止。"
            return
        self.code = code
        self.status = _STATUS.get(code, "正在等待任务报告或录像状态更新。")
        if code.startswith("file_monitor_") and code not in ("file_monitor_running", "file_monitor_requires_elevation"):
            self.status = "本地重放文件监控暂不可用；实时完赛仍继续监测，不需要本地录像。"
        if "collection_mode" in event:
            self.collection_mode = event["collection_mode"]
        for key in ("official_score_players", "total_players", "resolved_actors", "total_actors"):
            if key in event:
                setattr(self, key, event[key])
        for key in ("scan_mib", "scan_total_mib", "scan_rounds_completed"):
            if key in event:
                setattr(self, key, event[key])
        if code in ("locating_report_controller", "waiting_for_report_controller"):
            if self.scan_mib is not None and self.scan_total_mib is not None:
                progress = (f"已扫描 {self.scan_mib}/{self.scan_total_mib} MiB"
                            f"（完成 {self.scan_rounds_completed} 轮）")
                self.status = ("正在定位任务报告，" + progress + "。" if code == "locating_report_controller"
                               else _STATUS[code] + " " + progress + "。")
        if code == "companion_started":
            self.status = {"auto": "实时完赛 / 本地重放自动采集已启动，正在定位任务报告。",
                           "replay": "自动重放采集已启动，正在定位任务报告。",
                           "live": "联机采集已启动，等待实时完赛任务报告。"}[self.collection_mode]
        if event["event"] == "companion_error" and code not in _STATUS and not code.startswith("file_monitor_"):
            self.status = "采集出现错误，请检查设置后重新打开程序。"
        if code == "desktop_worker_finished":
            self.status = "采集已停止。" if event.get("exit_code") == 0 else "采集未能继续，请查看上方诊断状态后重新打开程序。"
        if event["event"] in ("upload_acknowledged", "server_receipt_updated") and "receipt_status" in event:
            self.receipt_status = event["receipt_status"]
            self.status = {
                "accepted": "接收端已收件，并已匹配对应的服务器对局记录。",
                "pending_match": "接收端已收件，正在等待匹配本局记录；收件不代表服务器已验收。",
                "duplicate": "接收端确认此前已收件；匹配状态请以接收端为准。",
            }[self.receipt_status]
        if "session_id" in event:
            self.session_id = event["session_id"]
        if "replay_sha256" in event:
            if event["replay_sha256"] != self.replay_hash and "replay_name" not in event:
                self.replay_name = ""
            self.replay_hash = event["replay_sha256"]
        if "replay_name" in event:
            self.replay_name = event["replay_name"]
        if (not self.inventory_ready and event["event"] in ("replay_report_queued", "live_report_queued")
                and "total" not in event):
            self.saved += 1
        if "total" in event:
            self.queued = event["total"]
            if not self.inventory_ready:
                self.saved = event["total"]
        for key, attribute in (("sent", "sent"), ("pending", "pending"), ("rejected", "rejected"), ("rows", "rows"),
                               ("matched", "matched"), ("server_pending", "server_pending")):
            if key in event:
                setattr(self, attribute, event[key])


class CaptureController:
    """Bounded, thread-safe bridge: worker threads never call Tk methods."""
    def __init__(self, config, runner):
        self.config, self.runner = config, runner
        self.events = queue.Queue(maxsize=128)
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.thread = None
        self.result = None
        directory = getattr(config, "data_directory", None)
        self.inventory = LocalReportInventory(directory) if isinstance(directory, (str, Path)) else None
        self.replay_library = (LocalReplayLibrary(directory, (*default_replay_directories(),
            *getattr(config, "replay_directories", ()))) if self.inventory is not None else None)
        self.inventory_thread = None
        self.inventory_stop = threading.Event()
        self.last_inventory = None

    def _refresh_inventory(self):
        if self.replay_library is not None:
            self.replay_library.reconcile(self.inventory.entries())
        event = self.inventory.poll()
        if event != self.last_inventory:
            self.publish(event)
            self.last_inventory = event

    def _watch_inventory(self):
        while not self.inventory_stop.is_set():
            self._refresh_inventory()
            if self.inventory_stop.wait(1):
                return

    def publish(self, value):
        event = _safe_event(value)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def start(self):
        if self.thread is not None:
            raise ValueError("desktop_capture_already_started")
        if self.stop_event.is_set():
            self.result = 0
            self.finished.set()
            return
        self.thread = threading.Thread(target=self._run, name="sd2-companion-capture", daemon=False)
        self.thread.start()

    def _run(self):
        try:
            if self.inventory is not None:
                self.inventory_thread = threading.Thread(target=self._watch_inventory,
                    name="sd2-companion-local-reports", daemon=True)
                self.inventory_thread.start()
            self.result = self.runner(self.config, emit_fn=self.publish, stop_event=self.stop_event)
        except Exception as exc:
            code = getattr(exc, "code", str(exc))
            if not isinstance(code, str) or not _CODE.fullmatch(code):
                code = "desktop_capture_failed"
            self.publish({"event": "companion_error", "code": code})
            self.result = 2
        finally:
            self.inventory_stop.set()
            if self.inventory_thread is not None:
                self.inventory_thread.join(timeout=2)
                if not self.inventory_thread.is_alive():
                    self._refresh_inventory()
            self.publish({"event": "desktop_worker_finished", "exit_code": self.result if type(self.result) is int else 2})
            self.finished.set()

    def request_stop(self):
        self.stop_event.set()

    def is_alive(self):
        return ((self.thread is not None and self.thread.is_alive())
                or (self.inventory_thread is not None and self.inventory_thread.is_alive()))

    def drain(self):
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result


class _MenuEntry:
    """Menu item that can be disabled on shutdown exactly like a button."""

    def __init__(self, menu, index):
        self.menu, self.index = menu, index

    def configure(self, **options):
        self.menu.entryconfigure(self.index, **options)


class _SortableTable:
    """Sort raw values while keeping formatting and row identity independent."""

    def __init__(self, tree, headings, *, column, reverse=True):
        self.tree, self.headings = tree, dict(headings)
        self.column, self.reverse = column, reverse
        self.rows = []
        for name in self.headings:
            tree.heading(name, command=lambda column=name: self.sort(column))
        self._headings()

    def _headings(self):
        for column, label in self.headings.items():
            marker = (" ▼" if self.reverse else " ▲") if column == self.column else ""
            self.tree.heading(column, text=label + marker)

    def set_rows(self, rows):
        """Rows contain a stable id, displayed values and raw column values."""
        self.rows = list(rows)
        self._render()

    def sort(self, column):
        self.reverse = not self.reverse if column == self.column else True
        self.column = column
        self._headings()
        self._render()

    def _render(self):
        selected, focus = self.tree.selection(), self.tree.focus()
        available, missing = [], []
        for row in self.rows:
            value = row[2].get(self.column)
            (missing if value is None else available).append(row)
        available.sort(key=lambda row: (row[2][self.column].casefold()
                                        if isinstance(row[2][self.column], str)
                                        else row[2][self.column]), reverse=self.reverse)
        self.tree.delete(*self.tree.get_children())
        for row_id, values, _raw in available + missing:
            self.tree.insert("", "end", iid=row_id, values=values)
        retained = [row_id for row_id in selected if self.tree.exists(row_id)]
        if retained:
            self.tree.selection_set(retained)
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)


class DesktopApp:
    """Standard desktop shell: menu bar, header note, counters, results list.

    Summary text and delivery/matching counters live in the header, the
    analyzed captures are one list, and every diagnostic detail is one menu
    click away. Nothing in this class opens a network connection; the worker
    thread owns capture and upload and never calls Tk directly.
    """

    COLUMNS = (("kind", "column_kind", 100, "w"), ("server_status", "column_server_status", 150, "w"),
               ("duration", "column_duration", 80, "w"), ("analyzed", "column_analyzed", 145, "w"),
               ("map", "column_map", 170, "w"), ("players", "column_players", 330, "w"))
    # Defaults keep partially built instances (and tests) usable without __init__.
    language = "en"
    sort_column, sort_reverse = "analyzed", True
    visible_entries = ()
    pump_id = None

    def __init__(self, config_path, config, runner):
        try:
            import tkinter as tk
            from tkinter import ttk
            self.root = tk.Tk()
        except (ImportError, RuntimeError) as exc:
            raise DesktopError("desktop_tk_unavailable") from exc
        except Exception as exc:
            # TclError is not available if importing tkinter itself failed.
            raise DesktopError("desktop_tk_unavailable") from exc
        self.tk, self.ttk = tk, ttk
        self.config_path, self.config = config_path, config
        self.controller = CaptureController(config, runner)
        self.model = StatusModel()
        self.model.catalog_state = ("pending" if getattr(config, "catalog_url", None)
                                    or getattr(config, "upload_url", None) else "disabled")
        self.closing = False
        self.close_started = None
        self.destroyed = False
        self.entries = []
        self.visible_entries = []
        self.filter_kind = "all"
        self.sort_column, self.sort_reverse = "analyzed", True
        self.language = _language(getattr(config, "language", "en"))
        self.status_var = tk.StringVar(value=self._t("status_starting"))
        self.counts_var = tk.StringVar(value=self._t("counts_pending"))
        # Diagnostics live in their own window; the variables stay updated so it
        # always shows the newest values without re-deriving anything.
        self.session_var = tk.StringVar(value="")
        self.hash_var = tk.StringVar(value="")
        self.quality_var = tk.StringVar(value="")
        self.catalog_var = tk.StringVar(value=catalog_text(self.model, self.language))
        self.code_var = tk.StringVar(value="")
        self.buttons = []
        self.update_events = queue.Queue()
        self.update_busy = False
        self.next_update_at = time.monotonic() + 5
        self.dismissed_update = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump_id = None
        self.root.after(0, self.controller.start)
        self.pump_id = self.root.after(100, self._pump)

    # -- language ---------------------------------------------------------
    def _t(self, key, **values):
        texts = _TEXT.get(key)
        if texts is None:
            return key
        template = texts[0] if self.language == "en" else texts[1]
        return template.format(**values) if values else template

    def set_language(self, language):
        """Switch the interface language and rebuild the window in place."""
        language = _language(language)
        if language == self.language:
            return
        self.language = language
        self._write_config(language=language)
        self._build_ui()

    def _build_ui(self):
        """(Re)build every widget; safe to call again after a language change."""
        previous_filter = self.filter_var.get() if getattr(self, "filter_var", None) is not None else "all"
        for child in self.root.winfo_children():
            child.destroy()
        self.buttons = []
        self.root.title(self._t("title", version=__version__))
        self.root.geometry("980x640")
        self.root.minsize(760, 520)
        self._build_menu()
        self._build_header()
        self._build_list()
        self._build_status()
        self.filter_var.set(previous_filter if previous_filter in ("all", "local", "online") else "all")
        self.filter_kind = self.filter_var.get()
        self.counts_var.set(self._t("counts_pending"))
        self.status_var.set(_status_text(self.model, self.language))
        self._refresh_entries()

    # -- layout -----------------------------------------------------------
    def _build_menu(self):
        menu = self.tk.Menu(self.root)
        file_menu = self.tk.Menu(menu, tearoff=False)
        self._add_command(file_menu, self._t("menu_open_reports"), self.open_exports)
        self._add_command(file_menu, self._t("menu_open_replays"), self.open_replays_directory)
        self._add_command(file_menu, self._t("menu_link_server"), self.link_selected_server_match)
        self._add_command(file_menu, self._t("menu_link_replay"), self.link_selected_replay)
        self._add_command(file_menu, self._t("menu_delete_record"), self.delete_selected_entry)
        self._add_command(file_menu, self._t("menu_settings"), self.open_settings)
        file_menu.add_separator()
        self._add_command(file_menu, self._t("menu_exit"), self.on_close)
        help_menu = self.tk.Menu(menu, tearoff=False)
        self._add_command(help_menu, self._t("menu_usage"), self.show_usage)
        self._add_command(help_menu, self._t("menu_diagnostics"), self.show_diagnostics)
        self._add_command(help_menu, self._t("menu_version"), self.show_version)
        self._add_command(help_menu, self._t("menu_updates"), self.check_updates)
        menu.add_cascade(label=self._t("menu_file"), menu=file_menu)
        menu.add_cascade(label=self._t("menu_help"), menu=help_menu)
        self.root.config(menu=menu)
        self.menu, self.file_menu, self.help_menu = menu, file_menu, help_menu

    def _add_command(self, menu, label, command):
        """Menu entries cannot be `configure`d, so shutdown disables them here."""
        menu.add_command(label=label, command=command)
        self.buttons.append(_MenuEntry(menu, menu.index("end")))

    def _build_header(self):
        ttk = self.ttk
        frame = ttk.Frame(self.root, padding=(20, 16, 20, 10))
        frame.pack(fill="x")
        # No heading: the window title carries the product name, so the body starts
        # straight with the three sentences. Each sentence is its own paragraph and
        # every paragraph re-wraps to the window width instead of a pixel guess.
        self.intro_labels = []
        for key in ("intro_1", "intro_2", "intro_3"):
            label = ttk.Label(frame, justify="left", wraplength=920, text=self._t(key))
            label.pack(anchor="w", pady=(0, 6))
            self.intro_labels.append(label)
        self.counts_label = ttk.Label(frame, textvariable=self.counts_var, justify="left", wraplength=920,
                                     font=("Microsoft YaHei UI", 10))
        self.counts_label.pack(anchor="w", pady=(8, 0))
        self.header_frame = frame
        frame.bind("<Configure>", self._fit_header)

    def _fit_header(self, event):
        """Keep the paragraphs readable at any window width."""
        width = max(240, int(event.width) - 44)
        for label in getattr(self, "intro_labels", ()):
            label.configure(wraplength=width)
        if getattr(self, "counts_label", None) is not None:
            self.counts_label.configure(wraplength=width)

    def _build_status(self):
        frame = self.ttk.Frame(self.root, padding=(20, 0, 20, 14))
        frame.pack(fill="x")
        self.ttk.Label(frame, textvariable=self.status_var, wraplength=920,
                       foreground="#555555").pack(anchor="w")
        self.ttk.Label(frame, textvariable=self.catalog_var, wraplength=920,
                       foreground="#555555").pack(anchor="w", pady=(4, 0))

    def _build_list(self):
        ttk, tk = self.ttk, self.tk
        frame = ttk.Frame(self.root, padding=(20, 0, 20, 10))
        frame.pack(fill="both", expand=True)
        self.filter_var = tk.StringVar(value="all")
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(4, 6))
        ttk.Label(filters, text=self._t("filter_label")).pack(side="left")
        for value, key in (("all", "filter_all"), ("local", "filter_local"), ("online", "filter_online")):
            ttk.Radiobutton(filters, text=self._t(key), value=value, variable=self.filter_var,
                            command=self._refresh_entries).pack(side="left", padx=(0, 12))
        ttk.Label(filters, text=self._t("filter_hint")).pack(side="right")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[name for name, *_ in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for name, heading_key, width, anchor in self.COLUMNS:
            self.tree.heading(name, text=self._t(heading_key), anchor=anchor,
                              command=lambda column=name: self._sort_entries(column))
            self.tree.column(name, width=width, anchor=anchor, stretch=name in ("map", "players"))
        self._update_entry_headings()
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.open_selected_entry())
        self.tree.bind("<Delete>", self.delete_selected_entry)
        self.empty_note = ttk.Label(frame, text=self._t("empty"), foreground="#666666")
        self.empty_note.pack(anchor="w", pady=(6, 0))

    # -- results list -----------------------------------------------------
    def _entry_server_status(self, entry):
        status = entry.get("server_status")
        key = "record_" + status if status in ("queued", "rejected", "received", "pending_match", "accepted") else "record_local_only"
        return self._t(key)

    def _refresh_entries(self):
        """Rebuild the list from the inventory; report contents stay untouched."""
        selected_fingerprint = None
        tree = getattr(self, "tree", None)
        if tree is not None:
            selection = tree.selection()
            if selection:
                try:
                    selected_fingerprint = self.visible_entries[int(selection[0])]["fingerprint"]
                except (ValueError, IndexError, KeyError):
                    pass
        inventory = getattr(self.controller, "inventory", None)
        if inventory is not None:
            try:
                self.entries = inventory.entries()
            except (OSError, ValueError, TypeError, RecursionError):
                self.entries = []
        self.filter_kind = self.filter_var.get() if getattr(self, "filter_var", None) else "all"
        rows = self._sorted_entries([entry for entry in self.entries
                                     if self.filter_kind == "all" or entry.get("kind") == self.filter_kind])
        self.visible_entries = rows
        if getattr(self, "tree", None) is not None:
            self.tree.delete(*self.tree.get_children())
            for index, entry in enumerate(rows):
                self.tree.insert("", "end", iid=str(index), values=(
                    self._t("kind_local") if entry.get("kind") == "local" else self._t("kind_online"),
                    self._entry_server_status(entry),
                    _duration_text(entry.get("duration")), _stamp(entry.get("analyzed_at")),
                    entry.get("map") or "—", _names_text(entry.get("names"))))
                if selected_fingerprint and entry.get("fingerprint") == selected_fingerprint:
                    self.tree.selection_set(str(index))
                    self.tree.focus(str(index))
        if getattr(self, "empty_note", None) is not None:
            if not rows:
                note = self._t("empty")
            elif len(rows) == 1:
                note = self._t("row_count_one")
            else:
                note = self._t("row_count", count=len(rows))
            self.empty_note.configure(text=note)

    def _sorted_entries(self, rows):
        key = getattr(self, "sort_column", "analyzed")
        accessors = {"kind": lambda item: item.get("kind") or None,
                     "duration": lambda item: item.get("duration"),
                     "analyzed": lambda item: item.get("analyzed_at") or None,
                     "map": lambda item: (item.get("map") or "").casefold() or None,
                     "players": lambda item: ", ".join(item.get("names") or ()).casefold() or None,
                     "server_status": lambda item: {"local_only": 0, "queued": 1, "rejected": 2,
                         "received": 3, "pending_match": 4, "accepted": 5}.get(item.get("server_status"))}
        accessor = accessors.get(key)
        if accessor is None:
            return list(rows)
        known = [item for item in rows if accessor(item) is not None]
        unknown = [item for item in rows if accessor(item) is None]
        return sorted(known, key=lambda item: (accessor(item), item.get("fingerprint", "")),
                      reverse=bool(getattr(self, "sort_reverse", False))) + unknown

    def _sort_entries(self, column):
        """Header click: reverse the active column, otherwise start descending."""
        self.sort_reverse = (not self.sort_reverse) if column == self.sort_column else True
        self.sort_column = column
        self._update_entry_headings()
        self._refresh_entries()

    def _update_entry_headings(self):
        for name, heading_key, _width, _anchor in self.COLUMNS:
            marker = (" ▼" if self.sort_reverse else " ▲") if name == self.sort_column else ""
            self.tree.heading(name, text=self._t(heading_key) + marker)

    # -- help menu --------------------------------------------------------
    def show_usage(self):
        english = self.language == "en"
        _message("\n".join((
            "Usage" if english else "使用说明",
            "",
            ("1. Keep this window open while you play Steel Division 2." if english else
             "1. 进行 Steel Division 2 对局时保持本窗口运行。"),
            ("2. When the match ends, open the debrief; the companion reads it from game memory." if english else
             "2. 对局结束并打开任务报告后，程序从游戏内存读取「任务报告-游戏时间表」。"),
            ("3. Matches on the [ASIA-JP] server upload automatically for analysis;" if english else
             "3. 在 [ASIA-JP] 服务器上进行的对局会自动上传服务器做数据分析；"),
            ("   other matches stay local and show as local only." if english else
             "   其他来源的对局只保存在本机，显示为仅本地。"),
            ("4. If you forgot to run the companion during the match, start it first, play the entire local replay in game, "
             "then leave the debrief open. The timeline is read and matched to the server record automatically." if english else
             "4. 若对局时忘开助手，请先启动助手，在游戏里完整播放本地录像，结束后停留在任务报告界面。"
             "软件会自动读取时间表并匹配服务器对局。"),
            ("5. Double-click any row to open that match's local analysis." if english else
             "5. 列表里的每一行都可以双击，查看该局的本地分析结果。"),
            "",
            ("6. Local analysis also works offline. File → Link local replay to server match can resubmit an older "
             "captured timeline; selecting a replay file alone cannot recover an uncaptured timeline." if english else
             "6. 本地分析也可离线使用。“文件 → 将本地录像关联到服务器对局”可重新提交已采集的旧时间表；"
             "仅选择录像文件不能补出尚未采集的时间表。"),
            ("\"Local only\" means no upload has been prepared for that analysis; it does not mean a replay file is missing." if english else
             "「仅本地」表示该局分析尚未准备上传，不表示录像文件缺失。"),
            ("\"Received by server\" only confirms delivery. Check Server status for \"Server match pending\" or \"Matched\"." if english else
             "「服务器已收件」只表示送达；请在“服务器状态”列查看“服务器待匹配”或“已匹配”。"),
        )))

    def show_diagnostics(self):
        ttk = self.ttk
        window = getattr(self, "diagnostics_window", None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        english = self.language == "en"
        window = self.tk.Toplevel(self.root)
        window.title(self._t("menu_diagnostics"))
        window.geometry("760x420")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        for variable in (self.status_var, self.session_var, self.hash_var, self.counts_var,
                         self.quality_var, self.catalog_var, self.code_var):
            ttk.Label(frame, textvariable=variable, wraplength=700, justify="left").pack(anchor="w", pady=3)
        ttk.Label(frame, justify="left", wraplength=700,
                  text=("Server addresses, the upload endpoint and credentials are never shown here. "
                        "The config file is base64-encoded and the credential comes from the environment; "
                        "this window only reports capture state and counters." if english else
                        "服务器地址、上传接口与凭证不会在此显示。配置文件以 base64 保存，"
                        "凭证只从环境变量读取；此处只显示采集状态与计数。")).pack(anchor="w", pady=(12, 0))
        ttk.Button(frame, text=("Close" if english else "关闭"),
                   command=window.destroy).pack(anchor="e", pady=(12, 0))
        self.diagnostics_window = window

    def show_version(self):
        english = self.language == "en"
        mode = ({"auto": "live / replay", "live": "live", "replay": "replay"} if english else
                {"auto": "实时完赛 / 本地重放", "live": "实时完赛", "replay": "本地重放"})[self.model.collection_mode]
        lines = ([f"SD2 Replay Timeline Capture {__version__}", f"Report schema: {TASK_REPORT_SCHEMA}",
                  f"Local snapshot schema: {LOCAL_REPORT_SCHEMA}", f"Python: {sys.version.split()[0]}",
                  f"Mode: {mode}"] if english else
                 [f"SD2 录像时间表采集 {__version__}", f"报告协议：{TASK_REPORT_SCHEMA}",
                  f"本地快照协议：{LOCAL_REPORT_SCHEMA}", f"Python：{sys.version.split()[0]}", f"模式：{mode}"])
        _message("\n".join(lines))

    def check_updates(self):
        self._start_update_check(manual=True)

    def _start_update_check(self, *, manual=False):
        if getattr(self, "update_busy", False) or self.closing:
            return
        from .updater import UPDATE_URL, UpdateError, check_for_update
        self.update_busy = True
        self.next_update_at = time.monotonic() + getattr(self.config, "update_interval_seconds", 21600)
        address = getattr(self.config, "update_url", UPDATE_URL)

        def check():
            try:
                release = check_for_update(__version__, manifest_url=address)
            except UpdateError:
                self.update_events.put(("failed", None, manual))
            else:
                self.update_events.put(("checked", release, manual))
        threading.Thread(target=check, name="companion-update-check", daemon=True).start()

    def _pump_updates(self):
        events = getattr(self, "update_events", None)
        if events is None:
            return
        from .updater import UPDATE_URL, UpdateError, download_release, install_on_exit
        while True:
            try:
                action, value, manual = events.get_nowait()
            except queue.Empty:
                break
            self.update_busy = False
            if self.closing:
                continue
            if action == "failed":
                if manual:
                    _message(self._t("updates_failed"))
                continue
            if action == "downloaded":
                release, staged = value
                try:
                    install_on_exit(release, staged, self.config_path)
                except (OSError, UpdateError):
                    _message(self._t("updates_install_failed"))
                else:
                    self.on_close()
                continue
            release = value
            if release is None:
                if manual:
                    _message(self._t("updates_current", version=__version__))
                continue
            if not manual and self.dismissed_update == release.version:
                continue
            self.dismissed_update = release.version
            if not getattr(sys, "frozen", False):
                if manual:
                    _message(self._t("updates_source", version=release.version, url=release.url))
                continue
            from tkinter import messagebox
            if not messagebox.askyesno(self._t("menu_updates"),
                                       self._t("updates_offer", version=release.version), parent=self.root):
                continue
            self.update_busy = True
            address = getattr(self.config, "update_url", UPDATE_URL)
            directory = Path(sys.executable).resolve().parent / ".sd2-companion-updates"

            def download(release=release):
                try:
                    staged = download_release(release, directory, manifest_url=address)
                except (OSError, UpdateError):
                    events.put(("failed", None, True))
                else:
                    events.put(("downloaded", (release, staged), True))
            threading.Thread(target=download, name="companion-update-download", daemon=True).start()
        if (not self.closing and not self.update_busy and getattr(self.config, "automatic_updates", True)
                and time.monotonic() >= self.next_update_at):
            self._start_update_check()

    # -- local analysis ---------------------------------------------------
    def delete_selected_entry(self, _event=None):
        """Remove one captured analysis locally, with explicit user intent."""
        selection = self.tree.selection()
        if not selection:
            _message(self._t("delete_select"))
            return "break"
        try:
            entry = self.visible_entries[int(selection[0])]
            fingerprint = entry["fingerprint"]
        except (ValueError, IndexError, KeyError):
            return "break"
        from tkinter import messagebox
        if not messagebox.askyesno(self._t("menu_delete_record"), self._t("delete_confirm"), parent=self.root):
            return "break"
        results = queue.Queue(maxsize=1)

        def work():
            try:
                from .local_deletion import delete_record
                from .outbox import Outbox
                delete_record(self.config.data_directory, fingerprint,
                              outbox=Outbox(self.config.data_directory / "outbox.sqlite3"))
                results.put(True)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, sqlite3.Error):
                results.put(False)

        def finish():
            if self.destroyed or self.closing:
                return
            try:
                success = results.get_nowait()
            except queue.Empty:
                self.root.after(150, finish)
                return
            inventory = getattr(getattr(self, "controller", None), "inventory", None)
            if inventory is not None:
                self.model.update(inventory.poll())
            self._refresh_entries()
            self.status_var.set(self._t("delete_done" if success else "delete_failed"))
            if not success:
                _message(self._t("delete_failed"))

        threading.Thread(target=work, name="sd2-delete-local-record", daemon=True).start()
        self.status_var.set(self._t("delete_working"))
        self.root.after(150, finish)
        return "break"

    def link_selected_server_match(self):
        selection = self.tree.selection()
        if not selection:
            _message(self._t("server_link_select"))
            return
        try:
            entry = self.visible_entries[int(selection[0])]
        except (ValueError, IndexError):
            return
        self.link_replay_to_server(entry)

    def link_replay_to_server(self, entry):
        """Explicit opt-in to submit an existing local timeline for verification."""
        from tkinter import filedialog, messagebox
        english = self.language == "en"
        if not getattr(self.config, "online_enabled", True) or not getattr(self.config, "upload_url", None):
            _message("Enable online recording in File → Settings, then restart the companion and try again."
                     if english else "请先在“文件 → 设置”启用在线记录，重启软件后再关联。")
            return
        path = filedialog.askopenfilename(parent=self.root,
            title="Choose the local replay to match with a server record" if english else
                  "选择需要关联服务器对局的本地录像",
            initialdir=str(default_replay_directories()[0]),
            filetypes=[("Steel Division 2 replay", "*.rpl3 *.rpl")])
        if not path:
            return
        report = _read_local_report(entry.get("path"))
        if report is None:
            _message("The report cannot be read." if english else "无法读取报告。")
            return
        if not messagebox.askyesno(self._t("menu_link_server"),
            ("Submit this captured debrief timeline to match it with the server record? "
             "The .rpl3 file itself is not uploaded."
             if english else "是否提交已采集的任务报告时间表，以关联服务器对局记录？"
             "不会上传 .rpl3 文件本体。"), parent=self.root):
            return
        results = queue.Queue(maxsize=1)

        def work():
            try:
                from .outbox import Outbox
                from .replay_association import enqueue_association
                outbox = Outbox(self.config.data_directory / "outbox.sqlite3")
                enqueue_association(self.config.data_directory, report, path, outbox,
                                    session_id=entry.get("session"), user_selected=True)
                results.put(True)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, sqlite3.Error):
                results.put(False)

        def finish():
            if self.destroyed or self.closing:
                return
            try:
                success = results.get_nowait()
            except queue.Empty:
                self.root.after(150, finish)
                return
            self._refresh_entries()
            self.status_var.set(("Server match association queued; waiting for server verification." if success else
                                 "Server match association could not be queued.") if english else
                                ("服务器对局关联已入队，等待服务器核验。" if success else "服务器对局关联未能入队。"))
            _message(("Queued for upload. The server will verify this timeline before linking the match." if success else
                      "Could not queue this analysis. Select the complete replay belonging to this match and try again.")
                     if english else ("已加入上传队列。服务器核验时间表后，会关联对应对局。" if success else
                                      "无法加入上传队列。请确认所选录像完整且属于该局，再重试。"))

        threading.Thread(target=work, name="sd2-server-replay-link", daemon=True).start()
        self.status_var.set("Preparing server match association…" if english else "正在准备关联服务器对局…")
        self.root.after(150, finish)

    def link_selected_replay(self):
        selection = self.tree.selection()
        if not selection:
            _message("Select the match to link first." if self.language == "en" else "请先选中要关联的对局。")
            return
        try:
            entry = self.visible_entries[int(selection[0])]
        except (ValueError, IndexError):
            return
        self.link_downloaded_replay(entry)

    def link_downloaded_replay(self, entry):
        from tkinter import filedialog
        english = self.language == "en"
        path = filedialog.askopenfilename(parent=self.root,
            title="Add this analysis's local replay" if english else "为该局分析补充本地录像",
            initialdir=str(default_replay_directories()[0]),
            filetypes=[("Steel Division 2 replay", "*.rpl3 *.rpl")])
        if not path:
            return
        report = _read_local_report(entry.get("path"))
        if report is None:
            _message("The report cannot be read." if english else "无法读取报告。")
            return
        results = queue.Queue(maxsize=1)

        def work():
            try:
                associate(self.config.data_directory, report, path,
                          session_id=entry.get("session"), user_selected=True)
                results.put(True)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError):
                results.put(False)

        def finish():
            if self.destroyed or self.closing:
                return
            try:
                success = results.get_nowait()
            except queue.Empty:
                self.root.after(150, finish)
                return
            self._refresh_entries()
            _message(("Local replay added to this analysis. Reopen it to view the map and investment P. "
                      "This does not confirm a match in the server archive." if success else
                      "The replay could not be linked. Check that it is complete and belongs to this match.")
                     if english else ("本地录像已补充到该局分析，重新打开即可查看地图和投入 P。"
                                      "此操作不代表已关联服务器对局记录。" if success else
                                      "无法关联录像。请确认文件已下载完成，且为该局的录像。"))

        threading.Thread(target=work, name="sd2-local-replay-link", daemon=True).start()
        self.status_var.set("Parsing downloaded replay…" if english else "正在解析已下载录像…")
        self.root.after(150, finish)

    def open_selected_entry(self):
        rows = getattr(self, "visible_entries", [])
        selection = self.tree.selection() if getattr(self, "tree", None) is not None else ()
        if not selection:
            return
        try:
            entry = rows[int(selection[0])]
        except (ValueError, IndexError):
            return
        self.show_match_details(entry)

    def show_match_details(self, entry):
        """Local kill/loss analysis for one captured report; nothing is uploaded."""
        ttk = self.ttk
        english = self.language == "en"
        window = self.tk.Toplevel(self.root)
        untitled = "Untitled match" if english else "未命名对局"
        window.title(("Local analysis · " if english else "本地分析 · ") + (entry.get("map") or untitled))
        window.geometry("880x560")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=entry.get("map") or untitled,
                  font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        kind = ("Local replay" if entry.get("kind") == "local" else "Online match") if english else \
               ("本地录像" if entry.get("kind") == "local" else "在线对局")
        summary = ((f"Category: {kind}　Duration: {_duration_text(entry.get('duration'))}　"
                    f"Analyzed: {_stamp(entry.get('analyzed_at'))}　Players: {entry.get('players', 0)}"
                    f"　Events: {entry.get('rows', 0)}") if english else
                   (f"类别：{kind}　时长：{_duration_text(entry.get('duration'))}　"
                    f"分析：{_stamp(entry.get('analyzed_at'))}　参与玩家：{entry.get('players', 0)} 人"
                    f"　事件行：{entry.get('rows', 0)}"))
        if entry.get("server"):
            summary += ("　Server: " if english else "　服务器：") + str(entry["server"])
        summary += ("　Status: " if english else "　状态：") + self._entry_server_status(entry)
        ttk.Label(frame, justify="left", wraplength=820, foreground="#555555",
                  text=summary).pack(anchor="w", pady=(6, 10))
        payload = _read_local_report(entry.get("path"))
        if payload is None:
            ttk.Label(frame, text=("This report file cannot be read; capture it again." if english else
                                   "无法读取该局报告文件，请重新采集。"), foreground="#a04040").pack(anchor="w")
            ttk.Button(frame, text=("Close" if english else "关闭"),
                       command=window.destroy).pack(anchor="e", pady=(12, 0))
            return
        archive = replay_evidence.load(self.config.data_directory, entry.get("replay"))
        if not entry.get("linked"):
            ttk.Button(frame, text=self._t("menu_link_server"),
                       command=lambda: self.link_replay_to_server(entry)).pack(anchor="w", pady=(0, 8))
        derived, error = _local_analysis(payload, archive)
        if derived is None:
            ttk.Label(frame, text=(("Cannot value this match: " if english else "该局暂无法计算击杀估值：") + error),
                      foreground="#a04040").pack(anchor="w")
            ttk.Button(frame, text=("Close" if english else "关闭"),
                       command=window.destroy).pack(anchor="e", pady=(12, 0))
            return
        players = derived.get("players") or []
        if archive is None:
            ttk.Label(frame, justify="left", wraplength=820, foreground="#a07040",
                      text=("This analysis has no local replay evidence yet. The companion checks the SD2 replay "
                            "folder automatically. If the file is already there but was not identified, choose "
                            "this match's .rpl3 below to fill its map, investment P and K / P locally." if english else
                            "该局分析尚未取得本地录像证据。程序会自动检查 SD2 录像目录；若文件已存在但未识别，"
                            "可在下方选择该局的 .rpl3，在本机补齐地图、投入 P 和 K / P。")
                      ).pack(anchor="w", pady=(0, 8))
            ttk.Button(frame, text=("Add this match's local .rpl3…" if english else "补充该局本地 .rpl3…"),
                       command=lambda: self.link_downloaded_replay(entry)).pack(anchor="w", pady=(0, 8))
        top = ttk.LabelFrame(frame, text=("Players (official scoring and all-component valuation)" if english else
                                          "参与玩家（官方计分与全部组件估值）"), padding=8)
        top.pack(fill="both", expand=True)
        player_table = ttk.Treeview(top, columns=("name", "official_k", "official_l", "K", "L"),
                                    show="headings", height=8)
        headings = ((("name", "Player"), ("official_k", "Official kill points"),
                     ("official_l", "Official loss points"), ("K", "Reported K"), ("L", "Reported L"))
                    if english else (("name", "玩家"), ("official_k", "原版击杀分"), ("official_l", "原版损失分"),
                                     ("K", "报告击杀 K"), ("L", "报告损失 L")))
        for column, heading in headings:
            player_table.heading(column, text=heading)
            player_table.column(column, width=240 if column == "name" else 100, anchor="w")
        player_table.pack(fill="both", expand=True)
        player_sorter = _SortableTable(player_table, headings, column="official_k")
        bottom = ttk.LabelFrame(frame, text=("Selected player by model (all components, same valuation as the site)"
                                             if english else "该玩家按型号（全部组件，与网站同一套估值口径）"), padding=8)
        bottom.pack(fill="both", expand=True, pady=(10, 0))
        model_table = ttk.Treeview(bottom, columns=("model", "P", "K", "L", "conversion", "exchange"),
                                   show="headings", height=8)
        model_headings = ((("model", "Model"), ("P", "Investment P"), ("K", "Reported K"), ("L", "Reported L"),
                           ("conversion", "K / P"), ("exchange", "K / L")) if english else
                          (("model", "型号"), ("P", "投入 P"), ("K", "报告击杀 K"), ("L", "报告损失 L"),
                           ("conversion", "K / P"), ("exchange", "K / L")))
        for column, heading in model_headings:
            model_table.heading(column, text=heading)
            model_table.column(column, width=260 if column == "model" else 85, anchor="w")
        model_table.pack(fill="both", expand=True)
        model_sorter = _SortableTable(model_table, model_headings, column="K")
        player_rows = []
        for index, player in enumerate(players):
            _scope, metrics = _scope_metrics(player)
            player_rows.append((str(index), (
                player.get("name") or ("Unnamed" if english else "未命名"),
                _points(player.get("official_kill_points")), _points(player.get("official_loss_points")),
                _value(metrics.get("K")), _value(metrics.get("L"))),
                {"name": player.get("name") or "",
                 "official_k": player.get("official_kill_points"), "official_l": player.get("official_loss_points"),
                 "K": metrics.get("K"), "L": metrics.get("L")}))
        player_sorter.set_rows(player_rows)

        def fill_models(_event=None):
            selection = player_table.selection()
            if not selection:
                model_sorter.set_rows([])
                return
            player = players[int(selection[0])]
            scope, _metrics = _scope_metrics(player)
            model_rows = []
            for index, unit in enumerate(player.get("units") or []):
                if unit.get("role_scope") != scope:
                    continue
                model_rows.append((str(index), (
                    unit.get("name") or ("Unidentified model" if english else "未确认型号"),
                    _value(unit.get("P_est")), _value(unit.get("K")), _value(unit.get("L")),
                    _ratio(unit.get("conversion_ratio")), _ratio(unit.get("exchange_ratio"))),
                    {"model": unit.get("name") or "",
                     "P": unit.get("P_est"), "K": unit.get("K"), "L": unit.get("L"),
                     "conversion": unit.get("conversion_ratio"), "exchange": unit.get("exchange_ratio")}))
            model_sorter.set_rows(model_rows)

        player_table.bind("<<TreeviewSelect>>", fill_models)
        if players:
            player_table.selection_set(player_table.get_children()[0])
            fill_models()
        ttk.Button(frame, text=("Close" if english else "关闭"),
                   command=window.destroy).pack(anchor="e", pady=(10, 0))
    def open_replays_directory(self):
        """Open the game's Saved Games folder, including relocated folders."""
        try:
            path = default_replay_directories()[0]
            if not path.is_dir():
                _message(self._t("replays_missing"))
                return
            os.startfile(str(path))
        except (OSError, AttributeError, IndexError):
            _message(self._t("replays_open_failed"))

    def open_exports(self):
        try:
            path = Path(self.config.data_directory)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except (OSError, AttributeError):
            _message("无法打开报告目录。\n\n" + str(self.config.data_directory))

    def open_settings(self):
        """Edit the non-secret options; the upload address is write-only here."""
        ttk = self.ttk
        window = self.tk.Toplevel(self.root)
        window.title(self._t("menu_settings"))
        window.transient(self.root)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        english = self.language == "en"
        ttk.Label(frame, text="Interface language / 界面语言").pack(anchor="w")
        language_var = self.tk.StringVar(value=self.language)
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=(4, 10))
        for value, label in (("en", "English"), ("zh", "中文")):
            ttk.Radiobutton(row, text=label, value=value, variable=language_var,
                            command=lambda: self.set_language(language_var.get())).pack(side="left", padx=(0, 14))
        updates_var = self.tk.BooleanVar(value=getattr(self.config, "automatic_updates", True))

        def save_updates():
            enabled = bool(updates_var.get())
            if self._write_config(automatic_updates=enabled):
                self.config = replace(self.config, automatic_updates=enabled)
                if enabled:
                    self.next_update_at = time.monotonic()
        ttk.Checkbutton(frame, text=self._t("updates_automatic"), variable=updates_var,
                        command=save_updates).pack(anchor="w", pady=(0, 10))
        online_var = self.tk.BooleanVar(value=getattr(self.config, "online_enabled", True))

        def save_online():
            enabled = bool(online_var.get())
            if self._write_config(online_enabled=enabled):
                self.config = load(self.config_path)
                _message(self._t("online_restart"))
        ttk.Checkbutton(frame, text=self._t("online_enabled"), variable=online_var,
                        command=save_online).pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text=("Capture mode: automatic (online match + local replay)" if english else
                               "采集模式：自动（在线游戏 + 本地重放）")).pack(anchor="w")
        ttk.Label(frame, text=(("Report folder: " if english else "报告目录：") + str(self.config.data_directory))
                  ).pack(anchor="w", pady=(8, 0))
        configured = (("configured" if english else "已配置")
                      if self.config.upload_url else
                      ("not configured (local only)" if english else "未配置（只保存到本地）"))
        upload_label = ttk.Label(frame, text=(("Server upload: " if english else "服务器上传：") + configured))
        upload_label.pack(anchor="w", pady=(8, 0))

        ttk.Label(frame, text=self._t("settings_upload_address")).pack(anchor="w", pady=(6, 0))
        entry = ttk.Entry(frame, width=52, show="•")
        entry.pack(anchor="w", pady=(6, 6))
        ttk.Label(frame, text=self._t("settings_upload_token")).pack(anchor="w")
        token_entry = ttk.Entry(frame, width=52, show="•")
        token_entry.pack(anchor="w", pady=(6, 6))

        def apply_address():
            if self._save_upload_settings(entry.get().strip(), token_entry.get().strip()):
                entry.delete(0, "end")
                token_entry.delete(0, "end")
                upload_label.configure(text=(("Server upload: configured" if english else
                                              "服务器上传：已配置")))

        ttk.Button(frame, text=self._t("settings_upload_save"),
                   command=apply_address).pack(anchor="w")
        ttk.Button(frame, text=("Close" if english else "关闭"),
                   command=window.destroy).pack(anchor="e", pady=(14, 0))

    def _save_upload_settings(self, address, token):
        """The GUI never retrieves or writes a credential into its JSON config."""
        from .config import from_dict
        from .credentials import CredentialError, store_upload_token
        try:
            raw = decode_config(Path(self.config_path).read_text(encoding="utf-8-sig"))
            endpoint = address or raw.get("upload_url") or self.config.upload_url
            if not endpoint:
                raise ConfigError("upload_url_not_configured")
            raw["upload_url"] = endpoint
            raw["online_enabled"] = True
            if endpoint.endswith("/task-reports"):
                raw["catalog_url"] = catalog_url_for_upload(endpoint)
            updated = from_dict(raw, base=Path(self.config_path).parent)
            if token:
                store_upload_token(endpoint, token)
            save_raw(self.config_path, raw)
        except (OSError, ConfigError, CredentialError, ValueError):
            _message(self._t("settings_upload_failed"))
            return False
        self.config = updated
        _message(self._t("settings_upload_saved"))
        return True

    def _write_config(self, **changes):
        """Rewrite the encoded envelope; never prints the endpoint it stores."""
        try:
            raw = decode_config(Path(self.config_path).read_text(encoding="utf-8-sig"))
        except (OSError, ConfigError, ValueError):
            _message("配置文件无法读取，未保存本次修改。")
            return False
        raw.update(changes)
        try:
            save_raw(self.config_path, raw)
        except OSError:
            _message("配置文件无法写入，未保存本次修改。")
            return False
        return True

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        self.close_started = time.monotonic()
        self.controller.request_stop()
        self.status_var.set("正在停止采集并释放录像监控，请稍候…")
        for button in self.buttons:
            button.configure(state="disabled")

    def _pump(self):
        if self.destroyed:
            return
        self._pump_updates()
        refreshed = False
        for event in self.controller.drain():
            self.model.update(event)
            if event.get("event") == "local_report_inventory":
                refreshed = True
        if self.closing:
            if not self.controller.is_alive():
                self.destroyed = True
                if getattr(self, "pump_id", None) is not None:
                    try:
                        self.root.after_cancel(self.pump_id)
                    except Exception:
                        pass
                    self.pump_id = None
                self.root.destroy()
                return
            if time.monotonic() - self.close_started > 5:
                self.status_var.set(self._t("close_long"))
        else:
            self.status_var.set(_status_text(self.model, getattr(self, "language", "en")))
            self._set_var("session_var", "Live session (game memory): " + self.model.session_id
                          if self.language == "en" and self.model.session_id
                          else ("实时会话（游戏内存）：" + self.model.session_id if self.model.session_id
                                else ("Waiting for the live session (no replay file needed)" if self.language == "en"
                                      else "实时会话：等待识别（不需要本地录像）")))
            if self.language == "en":
                name = "Replay: " + (self.model.replay_name or "no file identified yet")
                fingerprint = "SHA-256: " + (self.model.replay_hash or "not needed for a live finish")
            else:
                name = "本地重放：" + (self.model.replay_name or "尚未识别文件名")
                fingerprint = "SHA-256：" + (self.model.replay_hash or "尚未计算；实时完赛不需要录像 SHA")
            self._set_var("hash_var", name + "\n" + fingerprint)
            # Delivery acknowledgement and server matching are separate states.
            counts = self._t("counts", analyzed=self.model.saved, unlinked=self.model.unbound,
                             queued=self.model.pending, received=self.model.sent,
                             matched=getattr(self.model, "matched", 0),
                             server_pending=getattr(self.model, "server_pending", 0),
                             failed=self.model.rejected)
            if self.model.inventory_errors:
                counts += self._t("read_errors", count=self.model.inventory_errors)
            self._set_var("counts_var", counts)
            if self.model.total_players is not None and self.model.total_actors is not None:
                if self.language == "en":
                    quality = (f"Official scores: {self.model.official_score_players}/{self.model.total_players} players; "
                               f"model identity: {self.model.resolved_actors}/{self.model.total_actors} event actors resolved")
                else:
                    quality = (f"原版总分：已采集 {self.model.official_score_players}/{self.model.total_players} 位玩家；"
                               f"型号身份：已确认 {self.model.resolved_actors}/{self.model.total_actors} 个事件参与者")
                self._set_var("quality_var", quality)
            mode = {"auto": "live / replay", "replay": "replay", "live": "live"}[self.model.collection_mode] \
                if self.language == "en" else \
                {"auto": "实时完赛 / 本地重放", "replay": "本地重放", "live": "实时完赛"}[self.model.collection_mode]
            prefix = "Mode: " if self.language == "en" else "模式："
            label = "diagnostic: " if self.language == "en" else "　诊断："
            self._set_var("code_var", prefix + mode + label + self.model.code)
            catalog = catalog_text(self.model, getattr(self, "language", "en"))
            if catalog:
                self._set_var("catalog_var", catalog)
            if refreshed:
                self._refresh_entries()
        self.pump_id = self.root.after(100, self._pump)

    def _set_var(self, name, value):
        """Update a diagnostics variable when the widget exists (tests omit some)."""
        variable = getattr(self, name, None)
        if variable is not None:
            variable.set(value)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.controller.request_stop()
            if self.controller.thread is not None:
                self.controller.thread.join(timeout=10)
        return self.controller.result if type(self.controller.result) is int else 0


def _duration_text(value):
    """`mm:ss` (or `h:mm:ss`) for the results list; unknown stays a dash."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "—"
    total = int(round(value))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _names_text(names):
    """Every participant nickname, separated by plain English commas."""
    return ", ".join(name for name in names or () if name) or "—"


def _stamp(value):
    """Local wall-clock label for a capture or export timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _points(value):
    return str(value) if type(value) is int and 0 <= value <= 0xFFFFFFFF else "—"


def _value(value):
    return str(value) if type(value) is int and 0 <= value <= 1_000_000_000 else "—"


def _ratio(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.2f}"


def _read_local_report(path):
    """Read one captured report file; the caller only needs the native payload."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    report = value.get("report") if isinstance(value.get("report"), dict) else value
    if not isinstance(report.get("rows"), list) or not isinstance(report.get("native_players"), list):
        return None
    return report


def _local_analysis(payload, archive=None):
    """Reuse the published valuation module; no upload, no writes to the archive.

    `archive` is the compact replay evidence kept by `replay_evidence`; without
    it the report still yields kills, losses and official points, but not P.
    """
    try:
        from .aggregation import derive_match
        from .pricing import Catalog
        return derive_match(payload, archive or {}, Catalog()), ""
    except (ImportError, OSError, ValueError, KeyError, TypeError) as error:
        return None, str(error)[:120]


def _update_manifest_path():
    """The manifest shipped next to a packaged build, if any."""
    candidates = [Path(sys.executable).resolve().parent / "update.json"]
    if not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).resolve().parents[1] / "update.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _version_tuple(value):
    """Compare dotted versions; returns None for anything unexpected."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value.strip()):
        return None
    return tuple(int(part) for part in value.strip().split("."))


def _supported_platform():
    return os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8


def launch(config_path, elevated=False):
    """Request replay-monitor permission once; live capture can still proceed."""
    try:
        if not _supported_platform():
            raise DesktopError("desktop_requires_64bit_windows")
        config_path = Path(config_path).resolve()
        config = prepare_config(config_path)
        if not _is_admin():
            if elevated:
                _message(_MESSAGES["desktop_elevation_not_granted"] + "\n\n诊断：desktop_elevation_not_granted")
            else:
                try:
                    request_elevation(config_path)
                    return 0
                except DesktopError as exc:
                    _message(_MESSAGES.get(exc.code, _MESSAGES["desktop_elevation_failed"]) + "\n\n诊断：" + exc.code)
        from .cli import _run_auto
        return DesktopApp(config_path, config, _run_auto).run()
    except DesktopError as exc:
        _message(_MESSAGES.get(exc.code, "无法启动图形采集程序。") + "\n\n诊断：" + exc.code)
    except (OSError, ValueError, ConfigError):
        _message("无法读取或创建配置。请检查文件后重新打开程序：\n\n" + str(config_path))
    return 2
