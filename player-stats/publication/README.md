# SD2 Companion

[English](#english) · [中文](#中文)

## English

SD2 Companion is a Windows companion for Steel Division 2. It reads the game's published debrief timeline, records locally captured reports, and provides kill/loss and production-investment analysis. It can also analyze local replay evidence.

### Run

1. Download `SD2-Companion.exe` from [GitHub Releases](https://github.com/Tachibana-Rui/sd2-companion/releases/latest) or the [official Windows download](https://sd2.rui-blog.link/downloads/sd2-companion/SD2-Companion.exe).
2. Run the application with administrator permission so it can monitor the game and read the supported game process. No API address, account or token setup is required.
3. Keep it running during your match. After the match, leave the debrief open while the companion reads the game timeline.
4. Double-click a captured match for local analysis. **File → Open SD2 local replay folder** opens the game's local replay directory.

The current native reader targets game build **168061**. An unsupported executable is rejected instead of being read with guessed offsets. The application does not start or stop the game, inject code, modify game memory, or control game input.

### Data and permissions

The companion enumerates process names to locate Steel Division 2, then queries and reads the verified game process. It does not read other applications' memory or browse personal documents. Local replay access is read-only. Its own configuration, reports, queue and replay evidence are saved beside the configuration file; automatic installation credentials use the current user's Windows credential store.

Default online recording registers an installation-specific credential with the official HTTPS service and uploads eligible captured reports. The server verifies the native session or full replay hash against its archive. Reports can include participant names/IDs, native scores and timeline events. **File → Settings → Record matches online automatically** can disable online recording. Directory responses remain in memory. Update checks ask before installing and preserve configuration and reports.

### Run from source

Use 64-bit Python 3.10+ on Windows with Tk support:

```powershell
python -B player-stats/player_companion.py desktop
```

The runtime uses Python's standard library. The repository preserves `player-stats/`, `deck-analyzer/` and `map-selector/` because the parser shares their public data files.

### Build the Windows executable

```powershell
python -m venv player-stats/build-env
& player-stats/build-env/Scripts/python.exe -m pip install -r player-stats/requirements-build.txt
& player-stats/build-env/Scripts/python.exe -B player-stats/build_companion.py
```

Output: `player-stats/dist/sd2-companion/SD2-Companion.exe`. The builder verifies packaged resources and exercises an actual frozen executable replacement/restart. An optional `--test-replay <path>` additionally parses a local replay without copying it into the release.

### Tests

```powershell
python -B -m unittest discover -s player-stats -p 'test_*.py'
```

Tests use synthetic native layouts, temporary files and loopback HTTP. Windows-specific tests may use a Python child process owned by the test itself; they do not inspect the game. A real replay test runs only when explicitly configured.

### Repository scope

This is a clean public source snapshot, not the game server repository or its Git history. `SOURCE_MANIFEST.json` records the published files and hashes. Shared protocol validation and local HTTP test support (`receiver`, `enrollment`, `server_sessions`) are included because the client reuses them; private server deployment scripts, operator configuration, credentials, recordings, reports, databases and game binaries are excluded.

Third-party data provenance is retained in the JSON resources and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). A license for the project's own source code has not been selected; public visibility does not itself grant a software license.

## 中文

SD2 Companion 是 Steel Division 2 的 Windows 伴随程序，用于读取任务报告中的游戏时间表、保存本地战报并分析击杀、损失和生产投入，也支持本地录像证据分析。

### 使用

1. 从 [GitHub Releases](https://github.com/Tachibana-Rui/sd2-companion/releases/latest) 或[官方 Windows 下载](https://sd2.rui-blog.link/downloads/sd2-companion/SD2-Companion.exe)下载程序。
2. 运行时允许管理员权限，以便监测游戏并只读访问已适配的游戏进程。无需填写 API 地址、账号或凭据。
3. 对局期间保持运行，结束后停留在任务报告界面，软件自动读取游戏时间表。
4. 双击已记录的对局查看本地分析；通过 **文件 → 打开 SD2 本地录像目录** 打开录像文件夹。

当前原生读取器适配游戏构建 **168061**，未适配的游戏文件会被拒绝，不会使用猜测的偏移读取。程序不会启动或关闭游戏，不注入代码、不修改游戏内存、不操作游戏输入。

### 数据与权限

程序会枚举进程名称以找到 Steel Division 2，然后查询并读取经过核验的游戏进程，不读取其他应用的内存，不浏览个人文档。本地录像以只读方式访问。软件自己的配置、报告、上传队列及录像证据保存在配置旁；安装专用凭据由当前用户 Windows 凭据管理器保存。

默认在线记录会向官方 HTTPS 服务自动注册安装专用凭据，并发送具备关联依据的战报。服务器按原生会话或完整录像 SHA 核验归属。战报可包含参战昵称／ID、原版分数及时间表事件；可在 **文件 → 设置 → 自动在线记录对局** 中关闭在线记录。房间目录只保留在内存，软件更新安装前会询问，并保留配置与报告。

### 源码与构建

需要 Windows 64 位 Python 3.10+（含 Tk）。在仓库根目录执行：

```powershell
python -B player-stats/player_companion.py desktop
```

运行时只依赖 Python 标准库。构建命令和测试命令见上方英文说明；单文件 exe 输出到 `player-stats/dist/sd2-companion/`。保留的三个顶层目录仅用于复用解析器和公开数据，不含原服务器仓库的 Git 历史或私人部署文件。

这是公开源码快照。项目自身尚未选择软件许可证；第三方数据声明及来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
