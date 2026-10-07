# OAS 网页面板

独立的浏览器面板，用于连接本机 OAS 后端，查看配置、任务、日志、统计、异常归档和每日反馈，并通过后端支持的接口操作任务及导入导出配置。

项目地址：[luchi5/OAS-WebPanel](https://github.com/luchi5/OAS-WebPanel)。配套后端：[luchi5/OnmyojiAutoScript 的 luchi 分支](https://github.com/luchi5/OnmyojiAutoScript/tree/luchi)；桌面前端：[luchi5/OASX](https://github.com/luchi5/OASX)。面板、后端和桌面前端各自独立运行。请先安装并启动后端，面板启动脚本只管理面板及可选 HTTPS 网关。

## 安装

使用已经安装的 Python 3.10 或更新版本。建议为面板建立单独的虚拟环境；项目不附带 Python、Caddy 或第三方二进制运行环境。

Windows PowerShell，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item settings.example.json settings.json
```

修改 `settings.json` 中的 `backend_root`，填写已经安装的 OAS 后端根目录的绝对路径，例如 `C:/Apps/OnmyojiAutoScript`。示例中的 `C:/path/to/OnmyojiAutoScript` 是占位路径，需替换后才能启动。使用 POSIX 系统时可填写 `/path/to/OnmyojiAutoScript`，并通过 `python app.py --settings settings.json` 启动。

## 配置

| 字段 | 默认值 / 含义 |
| --- | --- |
| `host` | `127.0.0.1`，只允许监听回环地址 |
| `port` | `22300`，面板本地端口，可配置为 1024–65535 |
| `backend_url` | `http://127.0.0.1:22289`；允许 `127.0.0.1`、`localhost` 或 `[::1]` 的带端口 HTTP 地址 |
| `backend_root` | 必填，后端根目录的绝对路径，用于读取该后端的标签、统计、归档和反馈数据 |
| `public_origin` | 本地默认 `http://127.0.0.1:22300`；配置远程入口时填写自己的完整 HTTPS 来源，例如 `https://panel.example.com` |
| `authentication_required` | 默认 `true`；明确设为 JSON 布尔值 `false` 可关闭登录 |
| `state_dir` | 默认 `state`，相对路径以面板目录为基准，保存认证数据库和运行状态 |
| `backend_python` | 可选，仅供 Windows 生命周期监控使用；填写后端 Python 可执行文件的绝对路径，否则识别后端 `toolkit/python.exe` 或 `pythonw.exe` |

修改面板端口时，同时更新显式配置的本地 `public_origin`。省略 `public_origin` 时，程序会根据面板端口生成本地来源。远程来源必须使用 HTTPS，不能包含用户名、密码、路径、查询参数或片段。后端地址始终限制为本机 HTTP，也不会使用系统 HTTP 代理或跟随后端重定向。

认证开启时，首次启动会随机生成管理员初始密码，保存在配置的状态目录内的 `initial-login.txt`。请在本机查看文件并登录；首次登录后必须修改密码，修改完成后初始凭据文件会删除。凭据不会打印到日志中。Windows 启动脚本会限制状态目录权限。

关闭认证后，所有能访问面板入口的访客都能使用操作接口。来源检查、CSRF 校验和任务状态约束仍然生效。公开 HTTPS 入口应保留认证。

`settings.json`、状态目录、日志、证书和本机工具均在 `.gitignore` 中。自定义状态目录应放在仓库之外，或自行加入忽略规则。

## 启动与停止

本地启动：

```powershell
.\Start-Panel.ps1
```

也可以双击 `start-local.cmd`。脚本优先使用 `.venv/Scripts/python.exe`，其次使用 PATH 中的 Python；使用其他环境可传入 `-PythonPath`。`-NoBrowser` 可禁用自动打开浏览器，`-SettingsPath` 可指定配置文件。

默认本地地址为 `http://127.0.0.1:22300`。直接运行 Python：

```powershell
.\.venv\Scripts\python.exe -B app.py --settings settings.json
```

通过启动脚本启动的面板，可用下列命令停止：

```powershell
.\Stop-Panel.ps1
```

脚本会核对 PID 对应的面板进程和网关进程身份。直接运行 Python 时使用终端的 Ctrl+C。后端和游戏任务由后端自身管理。

## 可选 HTTPS 入口

安装 Caddy，配置自己的域名和 HTTPS 来源，再复制网关模板：

```powershell
Copy-Item Caddyfile.example Caddyfile
.\Start-Panel.ps1 -Public -CaddyPath 'C:\Apps\Caddy\caddy.exe'
```

Caddy 已在 PATH 中时可省略 `-CaddyPath`，也可双击 `start-public.cmd`。脚本按 `settings.json` 为网关传入来源和本地面板端口。示例没有指定私人域名、证书或专用公网端口；HTTPS 使用标准端口，或使用 `public_origin` 中明确配置的端口。DNS、证书签发条件及对应端口的防火墙/路由设置由部署者配置。

模板将访问代理到本机回环面板，并覆盖 `Host`、`X-Forwarded-Proto` 和单跳客户端地址。面板只接受本机代理传来的匹配 HTTPS 来源；请在修改代理配置时保留这些校验要求。后端原始端口应继续仅供本机访问。

## 可选 Windows 生命周期监控

```powershell
.\Ensure-PanelSupervisor.ps1
```

监控使用配置的后端端口、Python 运行环境和 `server.py` 入口识别后端进程。识别到后端运行时启动面板；确认后端退出后停止面板及其网关。仅使用本地进程身份和监听状态，HTTP 请求缓慢不会触发停止。远程来源配置为 HTTPS 时，监控会使用 PATH 中的 Caddy。

监控是可选功能，适合后端和面板都在同一台 Windows 机器上的安装。监控的进程发现使用 IPv4 监听表；仅使用 IPv6 的后端请手动启动面板。停止常驻监控后再手动停止面板，可避免监控在后端仍运行时重启面板。状态目录中的 `supervisor.pid` 可用于在任务管理器中识别监控进程。只读检查：

```powershell
.\.venv\Scripts\python.exe -B panel_supervisor.py --check
```

## 离线验证

测试使用临时目录、模拟后端及回环测试服务器，不连接实际 OAS 或启动游戏任务。统计解析器源码夹具位于 `tests/fixtures/log_stats.py`，来自配套后端发布源码的 `module/server/log_stats.py`。

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -t .
Get-ChildItem tests/test_*_ui.js | ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw 'Frontend test failed' } }
```

## 许可与来源

项目使用 GPLv3，见 [LICENSE](LICENSE)。OASX 图标与视觉布局来源、字体及图标字体的独立许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。请保留相关许可和原始版权说明。
