# WireDeck v0.1.0 发布说明 / Release Notes

`WireDeck v0.1.0` 是首个完整封包版本，定位为面向 Windows 笔记本与主机/掌机的极简串流网络助手，原生支持 **有线直连（Wired Gigabit Direct）** 与 **无线模式（5 GHz Hotspot & Same-LAN）** 两大平行板块。

---

## 核心特性 Highlights

### 1. 双连接模式（Dual Transport Pillars）
- **有线直连（Wired Gigabit Direct）**
  - 一键开启/关闭 `WireDeckNat`（WinNAT）有线直连共享，自动配置直连网口与放行规则，支持一键恢复日常上网。
  - 6 层有线链路故障诊断与自动修复引导，涵盖网口链接、速率协商、WinNAT、防火墙与对端连通性。
- **无线模式（Wireless Mode）**
  - **5 GHz 电脑热点（5 GHz PC Mobile Hotspot）**：基于 WinRT `NetworkOperatorTetheringManager` 开关热点与 5 GHz 优先配置，内置与有线 `WireDeckNat` 的共享冲突拦截、掩码热点口令查看/复制，以及 `192.168.137.0/24` 已连接客户端自动发现。
  - **同 Wi-Fi 局域网抗卡顿调优（Same-LAN Anti-Stutter Tuning）**：在 `runtime/wifi_backup.json` 原子备份原始网卡属性后，安全调优 `MIMOPowerSaveMode`、`*PacketCoalescing`、`PowerSavingMode`、`uAPSDSupport` 与 `RoamAggressiveness`；仅在 Wi-Fi 已连接时暂缓 `netsh wlan set autoconfig` 后台扫描，并支持一键无损还原。支持 2.4 GHz / 弱信号 / AP 隔离检测与多包 ICMP 抖动测速。

### 2. 多语言与隔离式设备预设（Multi-Language & Isolated Presets）
- **4 种界面语言**：简体中文（`zh-CN`）、繁體中文（`zh-TW`）、English（`en`）、日本語（`ja`）。
- **5 个独立设备预设**：`xbox`、`playstation`、`switch`、`handheld`、`other`，各预设的配置与对端 IP（包含有线对端与无线对端）按预设隔离持久化。
- **首次启动引导与 Fluent 原生桌面 UI**：提供紧凑无滚动条的首次引导弹窗、连接向导与设置面板，适配高 DPI 与明暗主题。

### 3. AI / CLI / 热重载扩展生态（AI, CLI & Extension Ecosystem）
- **双可执行程序架构**：`WireDeck.exe`（Windows GUI）与 `wiredeck-cli.exe`（CLI 控制台），支持 `diagnose --transport wired|wireless` 等无头指令。
- **20 个内置只读 AI 工具包**：`16` 个有线/通用与设备预设工具 + `4` 个无线专项排查工具（射频扫描、热点客户端探测、Wi-Fi 抖动基准、高级网卡属性检查）。
- **本地 HTTP 桥接与脱敏交接包**：支持 `GET /api/v1/diagnose?transport=wired|wireless`；导出的 AI 诊断交接包严格脱敏并剔除 Wi-Fi `passphrase` / `wifi_password`。
- **热重载扩展（Hot-Reload Extensions）**：按统一协议接入自定义诊断/探测插件。

---

## 封包产物 Release Artifacts

本版本构建产出位于 `release/` 目录：
- `WireDeck-v0.1.0-win64-setup.exe`（Inno Setup 6 安装包，含可选 PATH 集成与便携模式开关）
- `WireDeck-v0.1.0-win64-portable.zip`（免安装便携包，内置 `portable.mode`）
- `SHA256SUMS.txt`（以上两个发行压缩/安装包的官方 SHA-256 校验清单）
