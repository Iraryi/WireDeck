<div align="center">
  <img src="assets/logo.svg" alt="WireDeck Logo" width="112" height="112" />
  <h1>WireDeck</h1>
  <p><strong>笔记本与游戏机千兆有线直连助手 · 4 语言 · 5 设备预设 · 6 层断点定位 · 原生 AI / DSH 扩展与工具库</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011%20x64-0ea5e9?style=flat-square" alt="Windows 10/11 x64" />
    <img src="https://img.shields.io/badge/Version-v0.1.0-10b981?style=flat-square" alt="v0.1.0" />
    <img src="https://img.shields.io/badge/Languages-简体_%7C_繁體_%7C_EN_%7C_日本語-2563eb?style=flat-square" alt="4 Languages" />
    <img src="https://img.shields.io/badge/Backend-Pure%20WinNAT%20(No%20ICS)-06d6a0?style=flat-square" alt="Pure WinNAT" />
    <img src="https://img.shields.io/badge/AI%20%2F%20DSH-Toolkits%20%2B%20Hot--Reload%20Ext-38bdf8?style=flat-square" alt="AI / DSH Ready" />
  </p>
</div>

---

一根千兆网线直连笔记本与游戏机/掌机，免采集卡、免额外路由器，避开 Windows ICS 冲突、VPN TUN 断流、PCIe 网卡休眠与微软商店回环隔离，看着状态灯点两下即可稳定 60fps 串流开玩。

- **4 种界面语言**：简体中文 / 繁體中文 / English / 日本語（开局弹窗即选，设置内实时切换）。
- **5 套独立预设流程**：各预设的 IP、DNS 与向导状态互相独立保存，共享电脑网卡与直连设置：
  - **Xbox Series X|S / One**：完整 4 步（含 UWP 回环豁免与 XStreaming / Xbox 检测）。
  - **PlayStation 5 / PS4**：自动跳过微软商店与 UWP，支持 Chiaki-ng / PS Remote Play 与 PSN 检测。
  - **Nintendo Switch / Switch 2**：底座有线加速，自动跳过串流客户端，测速任天堂连通性。
  - **Steam Deck / ROG 掌机**：有线 Moonlight / Steam 高带宽串流 + 一键生成 `nmcli` 脚本。
  - **其他有线设备（Linux / 树莓派 / Mini-PC）**：极简静态直连 + 自动生成多 OS 对端设置命令。

## 实机界面一览

<p align="center">
  <img src="assets/screenshots/home_ready.png" alt="WireDeck 主控台 - 千兆直连运行中" width="88%" />
</p>

| 开局语言与设备预设选择 | PlayStation 专属向导（自动隐藏无关微软项） |
| :---: | :---: |
| <img src="assets/screenshots/onboarding.png" alt="开局语言与预设弹窗" width="100%" /> | <img src="assets/screenshots/wizard_ps.png" alt="PlayStation 向导" width="100%" /> |

| 精准定位断点（零废话一句提示 + 一键修复） | Xbox 4 步首次准备向导（自动跳过已达标项） |
| :---: | :---: |
| <img src="assets/screenshots/home_breakpoint.png" alt="断点拦截视图" width="100%" /> | <img src="assets/screenshots/wizard.png" alt="首次准备向导" width="100%" /> |

<details>
<summary>展开查看 AI / DSH 协作接口与内置工具库、中性深灰模式截图</summary>
<p align="center">
  <img src="assets/screenshots/ai_drawer.png" alt="AI / DSH 协作接口面板" width="48%" />
  <img src="assets/screenshots/home_dark.png" alt="中性深灰主题" width="48%" />
</p>
</details>

---

## 3 步极简上手

1. **插好千兆网线，开局选语言与设备**：首次启动在弹窗中选择界面语言与直连设备（如 Xbox / PS5 / Switch / 掌机 / 其他）。
2. **按当前预设完成首次准备**：切到左侧 **首次向导**，程序自动隐藏无关步骤（例如 PS5 自动跳过微软 UWP），按绿灯指引在设备端填入显示的 4 行静态参数。
3. **日常一键切换**：
   - 要玩游戏 → 断开 VPN 后点 **开启游戏直连** → 启动对应串流客户端；
   - 玩完回归工作 → 点 **恢复日常上网**（1 秒无痕恢复，零重启）。

---

## 为什么做 WireDeck？（实测数据）

同台笔记本 + 宿舍/随身热点环境下，**30 秒 18 Mbps · 60 fps 模拟串流**实测对比：

| 指标 | 2.4G Wi-Fi（常规） | 千兆网线直连 + WinNAT（WireDeck） |
| :--- | :---: | :---: |
| 接收帧率 / 丢帧 | 存在波动 | **1800 / 1800 帧（0% 丢帧）** |
| 直连往返延迟（RTT） | 波动大 | **0.41 ms** |
| 帧间抖动（Jitter） | 高 | **0.043 ms** |
| P99 / 最大帧间隔 | 频繁尖刺 | **17.3 ms / 20.93 ms** |
| `>50 ms` 卡顿次数 | **40 次** | **0 次** |
| `>100 ms` 明显定格 | **4 次** | **0 次** |

同时彻底绕开 **Windows ICS（`SharedAccess`）** 死占 `UDP 53/67` 导致 VPN TUN 失效必须重启的顽疾，全程使用纯净 `WinNAT` + 自动防冲突门禁。

---

## 6 层断点精准定位

| 层级 | 节点 | 自动识别的典型问题 |
| :---: | :--- | :--- |
| **L1** | 网卡硬件 | 网卡禁用、**上级 PCIe 根端口被禁用（Code 45 / 22）**、EEE / GigaLite 节能微卡顿、多物理网卡优选 |
| **L2** | 物理网线 | 网线未插、劣质线降速 **100 Mbps**、硬件错包偏高 |
| **L3** | 冲突门禁 | 任意 VPN TUN（sing-tun / Wintun / Clash / WireGuard）接管默认路由、ICS 激活、`UDP 53/67` 冲突（第三方 DNS 自动降级提醒不误伤）、外部 `NetNat` 防误删保护 |
| **L4** | WinNAT 共享 | `WireDeckNat` 状态、静态 IP、IP Forwarding、子网作用域防火墙规则 |
| **L5** | 主机与出口 | 目标设备 `<1 ms` 连通性、笔记本公网出口、按预设检测 Xbox Live / PSN / Nintendo CDN |
| **L6** | 串流就绪 | Xbox（17 项 UWP 豁免 + XStreaming/Xbox）、PlayStation（Chiaki-ng / PS Remote Play）、掌机（Moonlight / Steam）；Switch/其他档自动跳过 |

---

## 原生 AI / DSH 协作接口与内置 16 项工具库

遇到陌生网络问题时，在 **AI 接口** 页一键 **「复制现场发给 AI」** 即可导出脱敏诊断包，**16 个内置只读工具脚本**（6 个通用环境探针 + 各预设专属工具）可直接在图形界面运行，也支持 **DSH / 终端** 直接调用与热写修复扩展：

```powershell
# 1. 无头 JSON 诊断与内置工具库调用（同步返回严格 UTF-8 JSON）
wiredeck-cli.exe diagnose --console playstation --json
wiredeck-cli.exe toolkit list --preset playstation --json
wiredeck-cli.exe toolkit run common/probe-route-table --json
wiredeck-cli.exe toolkit run other/emit-peer-config-snippets --json

# 2. 导出脱敏现场包（自动列出可用工具）
wiredeck-cli.exe export-bundle --json

# 3. 校验并热加载扩展（放进 <data_dir>\extensions\<ext_id>\ 即自动融入主界面）
wiredeck-cli.exe ext validate <ext_dir> --json
wiredeck-cli.exe ext list --json
```

完整 CLI 退出码契约、内置 16 项工具库清单、`wiredeck-ext/v1` 热加载协议与可选本地 HTTP Bridge 说明见 [**docs/AI_EXTENSION_SPEC.md**](docs/AI_EXTENSION_SPEC.md)。

---

## 安装与校验

提供两种交付形态（均默认**无需管理员权限安装**，仅在修改网络时按需触发标准 Windows UAC；卸载时严格保护用户配置与 AI 扩展）：

- **一键安装器**：`WireDeck-v0.1.0-win64-setup.exe`（支持简体中文 / English 安装引导，应用内支持 4 语言、可选加入用户 `PATH`、可选便携模式、自带 `unins000.exe` 干净卸载与活跃 WinNAT 清理提醒）
- **免安装便携版**：`WireDeck-v0.1.0-win64-portable.zip`（内置 `portable.mode`，数据全部保存在目录内 `data\` 下）

下载后可用 PowerShell 核对 `SHA256SUMS.txt`：

```powershell
Get-FileHash .\WireDeck-v0.1.0-win64-setup.exe -Algorithm SHA256
```

## 开源协议

基于 [MIT License](LICENSE) 开源。
