# WireDeck v0.1.0 发布说明 / Release Notes

`WireDeck v0.1.0` 是首个完整封包版本，定位为面向 Windows 笔记本与主机/掌机的极简串流网络助手，原生支持 **有线直连（Wired Gigabit Direct）** 与 **无线模式（5 GHz Hotspot & Same-LAN）** 两大平行板块，并提供 **电脑 + 主机交互式连机教程** 与 **桌面一键开玩/恢复快捷方式**。

---

## 核心特性 Highlights

### 1. 双连接模式（Dual Transport Pillars）
- **有线直连（Wired Gigabit Direct）**
  - 一键开启/关闭 `WireDeckNat`（WinNAT）有线直连共享，自动配置直连网口与放行规则，支持一键恢复日常上网。
  - 6 层有线链路故障诊断与自动修复引导，涵盖网口链接、速率协商、WinNAT、防火墙与对端连通性。
- **无线模式（Wireless Mode）**
  - **5 GHz 电脑热点（5 GHz PC Mobile Hotspot）**：基于 WinRT `NetworkOperatorTetheringManager` 开关热点与 5 GHz 优先配置，内置与有线 `WireDeckNat` 的共享冲突拦截、掩码热点口令查看/复制，以及 `192.168.137.0/24` 已连接客户端自动发现。
  - **同 Wi-Fi 局域网防抖（Same-LAN Anti-Stutter Tuning）**：在 `runtime/wifi_backup.json` 原子备份原始网卡属性后，安全调优 `MIMOPowerSaveMode`、`*PacketCoalescing`、`PowerSavingMode`、`uAPSDSupport` 与 `RoamAggressiveness`；仅在 Wi-Fi 已连接时暂缓 `netsh wlan set autoconfig` 后台扫描，并支持一键无损还原。支持 2.4 GHz / 弱信号 / AP 隔离检测与多包 ICMP 抖动测速。

### 2. 电脑 + 主机交互式连机教程与一键开玩入口（Setup Guide & Quick-Play）
- **分步连机教程（Setup Guide）**：覆盖 **有线直连**、**5 GHz 电脑热点**、**同 Wi-Fi 防抖** 3 种连接路径与 5 套设备预设，结合电脑端自动化操作（完成后自动推进）与主机/掌机端分步图文核对引导，顺着步骤逐项完成即可联通。
- **智能连通性诊断与避障**：内置对端 IP 嗅探与连通测试；在同 Wi-Fi 连不通时结合 L2/W2 与无线状态智能识别原因，并在疑似路由器 AP 隔离时给出 **一键切换至 5 GHz 电脑热点** 逃生入口。
- **自定义本地串流客户端与桌面快捷方式**：支持按预设单独指定本地串流程序 `.exe`（`custom_client_path`）与一键拉起串流（如 XStreaming / Chiaki / Moonlight 等）；教程完成后可在底部一键生成 **桌面一键开玩快捷方式（`--quick-play`）** 与 **一键恢复日常上网快捷方式（`--quick-restore`）**，即使后台已有 WireDeck 实例运行也可直接生效。

### 3. 多语言与隔离式设备预设（Multi-Language & Isolated Presets）
- **4 种界面语言**：简体中文（`zh-CN`）、繁體中文（`zh-TW`）、English（`en`）、日本語（`ja`）。
- **5 个独立设备预设**：`xbox`、`playstation`、`switch`、`handheld`、`other`，各预设的配置、本地客户端路径与对端 IP（包含有线对端与无线对端）按预设隔离持久化。
- **首次启动引导与 Fluent 原生桌面 UI**：提供紧凑无滚动条的首次引导弹窗（支持直达连机教程）、顶栏设备预设切换、连接向导与设置面板，适配高 DPI 与明暗主题。

### 4. AI / CLI / 热重载扩展生态（AI, CLI & Extension Ecosystem）
- **双可执行程序架构**：`WireDeck.exe`（Windows GUI）与 `wiredeck-cli.exe`（CLI 控制台），支持 `diagnose --transport wired|wireless` 等无头指令；GUI 同时支持 `--wizard-step`、`--quick-play` 与 `--quick-restore` 启动参数。
- **20 个内置只读 AI 工具包**：`16` 个有线/通用与设备预设工具 + `4` 个无线专项排查工具（射频扫描、热点客户端探测、Wi-Fi 抖动基准、高级网卡属性检查）。
- **本地 HTTP 桥接与脱敏交接包**：支持 `GET /api/v1/diagnose?transport=wired|wireless`；导出的 AI 诊断交接包严格脱敏并剔除 Wi-Fi `passphrase` / `wifi_password`。
- **热重载扩展（Hot-Reload Extensions）**：按统一协议接入自定义诊断/探测插件。

---

## 封包产物 Release Artifacts

本版本构建产出位于 `release/` 目录：
- `WireDeck-v0.1.0-win64-setup.exe`（Inno Setup 6 安装包，含可选 PATH 集成与便携模式开关）
- `WireDeck-v0.1.0-win64-portable.zip`（免安装便携包，内置 `portable.mode`）
- `SHA256SUMS.txt`（以上两个发行压缩/安装包的官方 SHA-256 校验清单）
