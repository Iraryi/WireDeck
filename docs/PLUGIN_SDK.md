# WireDeck Plugin & Patch SDK (`wiredeck-plugin/v2`)

WireDeck 保持轻量化核心：高刷补帧、实时翻译、OBS 录制/回放、音频混音、手柄按键映射、悬浮叠层等能力**不内置于主程序二进制包中**，而是作为按需从 GitHub 拉取并经 SHA-256 校验安装的 **外挂式补丁/插件** 运行。

---

## 1. 目录结构

每个插件安装在 `<data_dir>/plugins/<plugin-id>/` 下（独立用户配置持久化于 `<data_dir>/plugin_configs/<plugin-id>.json`，升级覆盖插件文件时用户配置不会丢失）：

```text
<plugin-id>/
  plugin.json          # 清单与设置表单 Schema（必填）
  check.ps1            # 可选：插件自检脚本
  hooks/
    before_launch.ps1  # 可选：生命周期钩子脚本
    after_launch.ps1
    on_exit.ps1
  actions/
    my_action.ps1      # 可选：用户可在配置弹窗手动点击的动作按钮
```

---

## 2. 完整生命周期钩子（13 个 Hook 事件）

插件在 `plugin.json` 的 `"hooks"` 中订阅事件后，WireDeck 会在对应节点将上下文 JSON 写入临时文件并传入环境变量：

- `WIREDECK_HOOK_CONTEXT_FILE`：JSON 上下文路径，包含 `event`、`preset`、`transport`、`submode`、`peer_ip`、`host_ip`、`language`、`silent`、`client_pid`、`client_exe`、`client_exe_path`、`client_name`、`data_dir`、`plugin_id`、`plugin_dir`、`plugin_settings`、`plugin_overrides`、`system_env`（含本机显卡型号、NVIDIA 驱动版本、Smooth Motion 支持状态、双显卡状态、屏幕最高刷新率、电池状态等）、`wiredeck_version`。
- `WIREDECK_PLUGIN_ID`、`WIREDECK_PLUGIN_DIR`、`WIREDECK_HOOK_EVENT`、`WIREDECK_DATA_DIR`。

| Hook 事件 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `on_app_start` | WireDeck GUI 主窗口就绪后 | 检查配套工具版本、刷新本地缓存 |
| `on_app_exit` | WireDeck GUI 主窗口关闭前 | 清理临时辅助进程 |
| `before_quick_play` | 点击「进入串流 / 一键开玩」最前 | 预热环境、记录开玩前系统状态 |
| `before_transport_enable` | 开启有线直连 / 5G 热点 / 防抖前 | 开网前校验外部依赖或切换配置文件 |
| `after_transport_enable` | 网络已打通或防抖生效后 | 启动仅依赖局域网通畅的伴侣服务 |
| `before_client_launch` | 拉起 XStreaming / Chiaki / Moonlight 前 | **静默拉起补帧器、OBS、翻译器或输入映射** |
| `after_client_launch` | 串流客户端进程已拉起（含 `client_pid` / `client_exe`） | **等待串流窗口出现、聚焦并发送补帧/锐化/翻译热键** |
| `on_client_exit` | `--silent` 快捷方式监控到串流端退出后 | **自动关闭配套增强器或触发收尾恢复** |
| `after_quick_play` | 一键开玩全部流程完毕后 | 发送完成通知或记录战绩/会话 |
| `before_transport_disable` | 关闭直连 / 热点 / 防抖前 | 停止依赖直连的伴侣进程 |
| `after_transport_disable` | 恢复日常网络后 | 切回默认桌面声卡/分辨率设置 |
| `before_quick_restore` | 触发 `--quick-restore` 前 | 预备份/停止附加叠加层 |
| `after_quick_restore` | 触发 `--quick-restore` 完毕后 | 发送系统恢复通知 |

---

## 3. 声明式宿主指令（`host_commands`）

插件脚本向标准输出打印一行 JSON。如果包含 `"host_commands"` 数组，WireDeck 主控进程会用内置原生 Win32 API 安全代执行（无需插件自己手写 `DllImport` 或 P/Invoke）：

- `launch_process`: `{"cmd": "launch_process", "exe": "...", "args": [], "cwd": "", "minimized": true, "if_not_running": true}`
- `restart_process`: `{"cmd": "restart_process", "exe": "...", "args": [], "cwd": "", "minimized": true}`（先安全结束旧进程再后台重启）
- `wait_window`: `{"cmd": "wait_window", "pid": 1234, "process_name": "XStreaming.exe", "fallback_process_name": "...", "title_contains": "", "timeout_sec": 15}`
- `sleep`: `{"cmd": "sleep", "seconds": 2.0}`
- `focus_window`: `{"cmd": "focus_window", "process_name": "XStreaming.exe"}`
- `send_hotkey`: `{"cmd": "send_hotkey", "keys": "Ctrl+Alt+S"}`（支持 `Ctrl` / `Alt` / `Shift` / `Win` + 字母/数字/`F1-F24`/`Enter`/`Space`/`Tab`/`Esc`）
- `notify`: `{"cmd": "notify", "title": "WireDeck", "message": "补帧已启用"}`
- `open_url`: `{"cmd": "open_url", "url": "https://..."}`（仅允许 http/https）
- `stop_process`: `{"cmd": "stop_process", "process_name": "LosslessScaling.exe"}`
- `keep_awake`: `{"cmd": "keep_awake", "enable": true}`（通过 `SetThreadExecutionState` 在串流期间防止息屏与系统休眠）
- `set_gpu_preference`: `{"cmd": "set_gpu_preference", "exe": "...", "mode": "high_performance|power_saving|default"}`（免 UAC 读写 HKCU DirectX 独显偏好，并自动原子备份原值）
- `download_file`: `{"cmd": "download_file", "url": "https://*.download.nvidia.com/...", "filename": "...", "sha256": "", "open_after": true}`（仅允许白名单官方 HTTPS 域名下载到「下载」文件夹）

脚本输出中还可附带 `"save_settings": { ... }`，WireDeck 会自动合并写入 `plugin_configs/<plugin-id>.json`。

---

## 4. 自动配置表单（`settings_schema`）

支持字段类型：`text`、`select`、`dynamic_select`（通过 `options_script` 实时从本机 XML/系统读取下拉选项）、`bool`、`hotkey`、`file_path`（自动附带「浏览 .exe」原生文件框）、`number`、`multi_preset`。WireDeck 前端会自动按 Schema 生成原生设置对话框，开发者无需编写任何 HTML/JS。

---

## 5. GitHub 补丁包分发格式

支持两种拉取方式：
1. **官方/第三方索引（`wiredeck-registry/v1`）**：在插件目录 UI 中浏览并一键安装。
2. **自定义 GitHub 仓库或直链**：输入 `owner/repo`、`https://github.com/owner/repo` 或 `plugin-package.json` 直链。

`plugin-package.json`（与官方 `index.json` 的每个 entry 相同）结构如下，**每个文件均强制要求 64 位 SHA-256 校验**：

```json
{
  "id": "my-plugin",
  "name": "My Streaming Addon",
  "version": "1.0.0",
  "files": [
    {
      "path": "plugin.json",
      "url": "https://raw.githubusercontent.com/owner/repo/main/plugin.json",
      "sha256": "<64-hex>"
    },
    {
      "path": "hooks/after_launch.ps1",
      "url": "https://raw.githubusercontent.com/owner/repo/main/hooks/after_launch.ps1",
      "sha256": "<64-hex>"
    }
  ]
}
```
