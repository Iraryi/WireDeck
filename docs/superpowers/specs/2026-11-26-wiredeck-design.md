# WireDeck — 笔记本与游戏机千兆有线直连助手 设计文档

- **项目名称**：WireDeck（暂定，可在发布前调整）
- **初始版本**：`0.1.0`（单一版本来源：`src/wiredeck/version.py`）
- **目标平台**：Windows 11 / Windows 10 (x64)
- **开源协议**：MIT
- **仓库位置**：`d:\New\WireDeck\`
- **文档日期**：2026-11-26

---

## 1. 意图与背景（Why）

许多使用笔记本电脑 + 随身热点/宿舍 Wi-Fi 的主机玩家（尤其是 Xbox Series X|S 玩家），希望**不买昂贵采集卡或额外路由器**，仅用一根千兆网线直连笔记本与游戏机，实现低延迟、零丢帧的高画质串流。

然而 Windows 端手动打通这套链路暗坑极多：
1. 板载有线网卡或其上级 PCIe 根端口可能处于系统禁用状态（设备管理器无提示、对端显示 100M Link Up 但 `rx=0`）；
2. Realtek 2.5GbE 等网卡默认开启 EEE/节能休眠，导致串流中途掉线或微卡顿；
3. 开启代理软件系统代理时，微软商店 / Xbox 等 UWP 应用受 AppContainer 回环隔离拦截，永远加载失败；
4. Windows ICS（`SharedAccess`）会死占 `UDP 53/67` 并与主流 VPN 的 TUN 虚拟网卡（sing-tun / mihomo / Wintun）冲突，甚至必须重启电脑才能恢复上网；
5. 即便改用更干净的 `WinNAT（New-NetNat）`，若未先断开 VPN TUN 仍会破坏国内分流直连流量；
6. 普通玩家遇到连不上时，完全不知道究竟断在 **网线 / 网卡 / VPN 冲突 / 53 端口 / WinNAT / 主机 IP / 串流软件** 哪一层。

**WireDeck 的核心价值**：
- **极简傻瓜化**：主界面只有 **5 颗实时状态灯 + 1 句断点大白话 + 2 个主操作大按钮**，引导玩家一步步从首次配置、开启直连一直到打开串流端正式开玩，玩完 1 秒无痕恢复 VPN 工作模式（零重启）。
- **可诊断**：逐层定位故障并给出对应的一键修复动作，拒绝黑盒报错。
- **可被 AI（如 DSH）直接接管扩展**：内置完整的 CLI JSON 接口与明文热加载 `extensions/` 扩展槽，遇到程序规则库未覆盖的陌生网络故障时，无 VPN 环境下也能运行的 AI（如 DSH）可直接读取脱敏现场、执行诊断、写入新的检测/修复脚本并实时出现在界面中，无需重新编译 EXE。

---

## 2. 目标、非目标与反目标

### 目标（Goals）
1. **一屏极简主界面**：普通用户只看状态灯和按钮就能完成「开启游戏直连 ↔ 恢复日常上网」，文案零废话。
2. **6 层断点精准诊断引擎**：准确识别每一层问题（含被禁用的 PCIe 根端口、100M 劣质线降速、任意 VPN TUN 占用默认路由、ICS / UDP 53/67 残留、WinNAT 冲突、UWP 回环隔离、串流客户端缺失）。
3. **贯通到正式游玩的一键向导**：从网卡唤醒与防抖、UWP 回环豁免、串流客户端检测/官方校验下载（XStreamingDesktop），到主机静态参数卡与主机连通性一键验证。
4. **原生 AI / DSH 协作“开口”**：
   - 无窗口 CLI 子命令（`diagnose` / `status` / `apply` / `export-bundle` / `ext`），全部输出严格 JSON；
   - 明文热加载插件槽（`extensions/`），DSH 可直接新增或覆写检测项与修复动作；
   - 界面内「一键复制现场给 AI」按钮，自动导出脱敏诊断包并生成给 DSH 的标准对接引导词；
   - 可选（默认关闭）的本地 `127.0.0.1` HTTP JSON Bridge，带一次性随机 Bearer Token。
5. **专业开源交付标准**（吸收 `exeskill.md` 精华）：
   - 同时产出单目录直跑版（含便携模式 ZIP）与 **Inno Setup 6** 正式安装器（`Setup.exe` + `unins000.exe`）；
   - 默认当前用户安装（安装无需 UAC，仅在修改网络/网卡时按需触发标准 Windows UAC）；
   - 严格文件所有权边界（卸载绝不误删用户配置、日志与 AI 扩展脚本），全产物输出 SHA-256；
   - 高颜值、有象征性的开源 GitHub 主页（自定义 SVG Logo、高 DPI 实机截图、简明 3 步上手文档、Release 资产就绪；暂本地就绪，待 GitHub 恢复后由用户授权推送）。

### 非目标（Non-Goals）
- **不私自打包魔改第三方串流客户端二进制**（如 XStreamingDesktop）：遵循开源许可与体积边界，改为自动探测已安装路径，或通过官方 GitHub Releases 拉取 + 校验官方 SHA-256 后引导安装。
- **不将 Windows ICS 或二层网桥作为主共享方案**：两者已实测与主流 VPN TUN / 4G 随身热点存在不可控冲突，WireDeck 仅用纯净 `WinNAT`，并对残留的 ICS 做主动告警和一键清理。
- **不尝试任何 UAC 绕过技术**：所有特权网络操作严格走 Windows 标准 UAC 提权（已在管理员终端内被 DSH 调用时则直接无弹窗执行）。
- **暂不自动推送 GitHub / 发布线上 Release**（当前 GitHub 存在全局故障，且推送需用户明确指令）。

### 反目标（Anti-Goals — 即使技术跑通也算失败）
- 主界面堆满技术术语、长段落说明或复杂表单，让普通玩家看不懂该点哪。
- 任何代码硬编码开发机的具体环境值（如 `192.168.8.104`、`PortableSSS`、`Realtek 2.5GbE`、用户名 `65428`、`LightXtreme` 专属写死）。必须通用自动探测。
- 失败或卸载后在用户电脑上留下脏网络状态（如忘关 `Forwarding`、忘删 `NetNat`、误删用户自建扩展脚本或无故改动 Wi-Fi 配置）。

---

## 3. 备选技术路线与选型决策

评估了三条本机可落地的技术路线（已预先淘汰高 DPI 模糊、界面陈旧的 Tkinter 与本机缺少 SDK 的 .NET）：

| 路线 | 技术栈 | 优点 | 代价 / 权衡 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **方案一（已选定）** | Python 3.11 + `pywebview`（系统 Edge WebView2）+ 明文 PowerShell/JSON 引擎 + PyInstaller (`onedir`) + Inno Setup 6 | 界面可用现代 HTML/CSS/SVG 做到极高专业质感（高 DPI 100%~200% 锐利、截图极佳）；复用 Win11 内置 WebView2，安装包仅 ~18–25 MB；明文扩展槽天然适配 DSH 热改免重编 | 需安装轻量的 `pywebview`；极老 Win10 无 WebView2 时需提示下载（Win11 全预装） | **用户已批准选用** |
| 方案二 | Electron (Node v24) + 前端 + PowerShell 引擎 | 界面能力同级，Node 生态成熟 | 内置整套 Chromium，安装包 100MB+、内存占用高，偏重 | 放弃 |
| 方案三 | Python + PySide6 (Qt6) + PyInstaller + Inno Setup | 不依赖系统 WebView2 | 现代链路图视觉调校成本高，安装包 50MB+，且需承担 Qt LGPL 合规与 DLL 清点负担 | 放弃 |

---

## 4. 产品交互与极简界面设计

### 4.1 信息架构（仅 3 个清晰视图，拒绝嵌套迷宫）

#### 视图 A：主控台（打开即见，一屏解决 90% 操作）
```text
┌─────────────────────────────────────────────────────────────┐
│  [Logo] WireDeck  v0.1.0        [ ↻ 刷新 ] [ AI 接口 ] [ ⚙ ] │
├─────────────────────────────────────────────────────────────┤
│  状态横幅：准备就绪 / 检测到断点（一句话）                     │
│                                                             │
│   [ 网卡 ] ──▶ [ 网线 ] ──▶ [ 门禁 ] ──▶ [ 共享 ] ──▶ [ 主机 ]  │
│     🟢           🟢           🔴           ⚪           ⚪    │
│    2.5G        1 Gbps      VPN 在线       未开启       待检测  │
│                                                             │
│  ┌─ 断点提示卡片（仅在有异常时出现）──────────────────────┐    │
│  │ 🔴 检测到 VPN 虚拟网卡正在接管网络，开启共享会冲突      │    │
│  │    操作：请先断开 VPN，再点下方「开启游戏直连」         │    │
│  │                                       [ 一键修复 ]*   │    │
│  └───────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────┐  ┌─────────────────────────┐   │
│  │   ▶  开启游戏直连        │  │   ↺  恢复日常上网        │   │
│  └─────────────────────────┘  └─────────────────────────┘   │
│                                                             │
│  [ 首次准备向导 ]     [ 启动串流端 ]     [ 复制现场给 AI ]    │
└─────────────────────────────────────────────────────────────┘
```
- **状态灯颜色语义（固定 4 态，图标+文字双通道，不只靠颜色）**：
  - `🟢 正常`：该层通过
  - `🟡 警告`：可用但非最优（例如 100Mbps 百兆线、网卡节能未关、UWP 未豁免）
  - `🔴 阻断`：明确断点，卡片直接给出 1 句原因 + 对应操作按钮（能自动修的直接放「一键修复」，需用户手动点的如“断开 VPN”直接写明）
  - `⚪ 待命`：当前未开启共享/未接主机时的正常待命态
- **文案纪律**：每个卡片标题 ≤ 12 字，原因说明 ≤ 28 字；技术细节（具体适配器名、路由表、错误码）全部折叠在「查看详情」或诊断包内，绝不污染主界面。

#### 视图 B：首次准备向导（4 步线性卡片，自动跳过已达标项）
1. **第 1 步 · 有线网口体检**：自动检测有线网卡是否在线（若禁用或上级 PCIe 根端口禁用则一键唤醒），关闭 EEE/节能/GigaLite 防卡顿项，检测网线是否跑满千兆（`1 Gbps+`）。
2. **第 2 步 · 解除代理拦截（可选）**：检测微软商店 / Xbox / 账号认证 UWP 应用是否已加入回环豁免，缺失则一键写入（解决开 VPN 时商店/Xbox 加载失败）。
3. **第 3 步 · 准备串流客户端**：自动扫描本机已安装的 **XStreamingDesktop** 或官方 **Xbox 应用**；若未装，提供「一键下载并校验官方 XStreaming 安装包（SHA-256 核验）」与「直接打开安装」按钮。
4. **第 4 步 · 主机一次性设置 & 连通验收**：
   - 卡片大字展示自动适配当前子网的 4 行参数（默认 `IP: 192.168.50.1`、`掩码: 255.255.255.0`、`网关: 192.168.50.2`、`DNS: 223.5.5.5 / 8.8.8.8`）+「一键复制」；
   - 折叠小贴士：首次开全新主机无显示器时，可用旧采集卡/借屏幕 5 分钟开启「启用远程功能」；
   - 点击「测试主机连通」验证 `<1ms` 直连 + 出网正常，绿灯即进入「启动串流端」开玩！

#### 视图 C：AI / DSH 协作面板（抽屉/标签页）
- **一键复制给 AI（DSH）**：导出脱敏诊断包 `exports/diagnostic_latest.json` 并向剪贴板写入一段精炼交接词（含诊断包路径、CLI 用法、`extensions/` 插件规范），用户贴给 DSH 即可无缝接管。
- **已加载扩展列表**：实时列出 `extensions/` 中的自定义检测/修复插件（显示名称、来源、最后状态、单个「运行」按钮、一键打开扩展目录）。
- **本地 HTTP Bridge 开关（默认关闭）**：开启后在 `127.0.0.1` 随机可用端口启动带随机 Bearer Token 的本地 JSON API（端口与 Token 写入 `runtime/bridge.json`），关闭即释放端口，绝不常驻占口。

---

## 5. 核心诊断与执行引擎设计

### 5.1 动态硬件与冲突发现（零硬编码）
所有探测逻辑均通过结构化 PowerShell/Win32 查询动态识别，不硬编码特定品牌或网段：
1. **物理有线网卡识别**：
   - 枚举 `Get-NetAdapter` + `Get-PnpDevice -Class Net`，过滤掉虚拟/无线/蓝牙/隧道适配器（`VMware|Hyper-V|WSL|Wintun|sing-tun|Tailscale|WireGuard|TAP|TUN|Wi-Fi|Wireless|Bluetooth|Loopback`）；
   - 若无在线物理有线网卡，继续检查离线/禁用设备（`CM_PROB_DISABLED` / `CM_PROB_PHANTOM` Code 45），并沿 `DEVPKEY_Device_Parent` 向上追溯父级 PCIe 根端口是否处于 `CM_PROB_DISABLED`！
   - 若存在多块物理有线网卡，在设置中允许用户手动切换，默认优先选 `Status=Up` 或速率最高者。
2. **公网出口网卡识别**：
   - 从当前 IPv4 默认路由（`0.0.0.0/0`）中排除已知虚拟/隧道网卡，选出物理出口（通常是 `WLAN`，也可手动指定）。
3. **VPN TUN 冲突门禁识别（通用，不限闪连）**：
   - 检查所有持有 `0.0.0.0/0` 路由的非物理/虚拟/隧道网卡（如 `sing-tun`、`Wintun`、`Clash`、`Meta`、`Mihomo`、`WireGuard`、`OpenVPN`、`TAP` 等）以及 `LightXtreme` 等已知 TUN 网卡；
   - 只要检测到此类 TUN 网卡处于 `Up` 且持有默认路由，门禁立刻标红并阻止启动 WinNAT（防止用户国内网站/代理断流）。
4. **ICS、端口 53/67 与外来 NetNat 的精确边界（安全防误伤）**：
   - **ICS 激活态与 UDP 67**：通过 `HNetCfg.HNetShare` 检测是否有网卡 `SharingEnabled=True`，或检测 `UDP 67` 是否被占用、或 `UDP 53` 是否被 `SharedAccess` 占用——任一命中即标 `🔴` 硬阻断（CLI 退出码 `2`）；
   - **第三方非 ICS 的 UDP 53 占用**：若 ICS 未开启、且 `UDP 53` 仅由第三方本地 DNS 工具（如 SmartDNS / AdGuard）占用，降级为 `🟡` 提醒（`PORT_53_NON_ICS_WARNING`），**不阻断** `enable-direct`，且 `clear-ics` 绝不终止非 ICS 进程；
   - **非 WireDeck 的现有 `NetNat`（如 Hyper-V / Docker / WSL）**：标 `🔴 FOREIGN_NETNAT_EXISTS` 并精确显示占用实例名；**绝不静默自动删除**，GUI 需二次确认，CLI 需显式执行 `wiredeck-cli apply clear-netnat --force`（若检测到归属 HNS/WSL 将明示风险提示）。
5. **主机档案策略（v0.1.0 范围）**：
   - **`xbox`（默认，满血闭环）**：完整执行 L1~L6 全套探测（含 Xbox Live 端点连通、17 项 UWP 回环豁免、XStreamingDesktop / Xbox App 探测与官方 SHA-256 校验下载）及 4 步首次向导；
   - **`ps5` / `generic`（v0.1.0 轻量档）**：完整执行 L1~L4（网卡唤醒/防抖、千兆线、门禁、WinNAT）与 L5 通用主机直连/公网出口检测，自动将 Xbox 专属检查项标记为 `skipped / N/A`，向导自动跳过第 2~3 步，直接给出通用静态 IP 卡与连通验证。

### 5.2 六层断点诊断模型（结构化 Fault Codes）
每次诊断输出统一 Schema（版本 `schema_version: "1.0"`），涵盖 6 层状态与精确故障码（供 UI 渲染和 DSH 机器消费）：

| 层级 | 节点名 | 识别的典型故障码（举例） | 可一键自动修？ |
| :--- | :--- | :--- | :---: |
| L1 | 网卡硬件 | `NIC_NOT_FOUND`、`NIC_PCIE_PARENT_DISABLED`、`NIC_DISABLED`、`NIC_POWERSAVE_ENABLED` | ✅（需 UAC） |
| L2 | 物理网线 | `CABLE_UNPLUGGED`、`LINK_DEGRADED_100M`、`NIC_HW_ERRORS` | 提示换线/重插 |
| L3 | 冲突门禁 | `VPN_TUN_ACTIVE`、`ICS_ACTIVE`、`PORT_53_67_OCCUPIED`、`FOREIGN_NETNAT_EXISTS` | ICS/NetNat 可一键清理；VPN 提示断开 |
| L4 | WinNAT 共享 | `ETH_IP_MISSING`、`ETH_HAS_UNEXPECTED_GATEWAY`、`WINNAT_INACTIVE`、`FORWARDING_DISABLED`、`FW_RULE_MISSING` | ✅（一键开启/修复） |
| L5 | 主机与出口 | `PEER_UNREACHABLE`、`HOST_INTERNET_DOWN`、`XBOX_LIVE_UNREACHABLE` | 提供主机参数卡与复测 |
| L6 | 串流就绪 | `UWP_LOOPBACK_MISSING`、`STREAM_CLIENT_MISSING` | ✅（一键 UWP 豁免 / 一键官方下载校验） |

### 5.3 双入口子系统、特权提升与状态回滚模型
- **Windows PE 双入口（避免“弹窗闪黑框”或“CLI 拿不到退出码”）**：
  - 同一个 PyInstaller `--onedir` 目录内共享运行库，提供两个轻量入口：
    - **`WireDeck.exe`**（`WINDOWS_GUI` 子系统）：双击打开主界面**零黑框闪烁**；
    - **`wiredeck-cli.exe`**（`WINDOWS_CUI` 子系统）：专供终端与 DSH 调用，**同步阻塞执行、标准输出严格 UTF-8 JSON、准确返回 `$LASTEXITCODE`**（安装器提供可选勾选将其目录加入用户 `PATH`）。
- **特权提升策略**：
  - **GUI**：默认以普通用户权限启动（启动不弹 UAC，WebView2 最稳）。点击需提权的写操作时，将带唯一 ID 的请求写入 `<data_dir>\runtime\req_<id>.json`，通过标准 Windows `ShellExecuteW(..., "runas", ...)` 启动自身 `wiredeck-cli.exe --worker <req_file> --out <res_file>`（**一次操作只弹一次标准 UAC**），原子写回结果 JSON 与 UTF-8 BOM 日志后立即刷新主界面。
  - **CLI（`wiredeck-cli.exe`）**：
    - 已在管理员终端内运行（如 DSH 提权执行）：直接进程内执行，**零 UAC 弹窗**；
    - 非管理员终端运行写操作：**默认快速失败并返回退出码 `3`**（JSON 返回 `{"ok": false, "code": "ADMIN_REQUIRED"}`，防止后台/自动化脚本意外卡死在 UAC 弹窗）；仅当显式传入 `--elevate` 参数时才拉起交互式 UAC。
  - **可选 HTTP Bridge（`POST /api/v1/apply`）**：当外部 AI 通过 localhost HTTP 请求特权写操作且 GUI 为普通权限时，接口立即返回 `202 Accepted` + `job_id`，在 GUI 弹出明确的人工确认框并由用户点 UAC 执行，AI 通过 `GET /api/v1/jobs/<id>` 轮询终态——既不超时，也杜绝静默提权。
- **`disable-direct` 与卸载状态清理**：
  - 首次执行 `enable-direct` 前，自动将有线网卡原有 IPv4/DHCP 配置快照保存至 `<data_dir>\runtime\nic_backup.json`；
  - `disable-direct` 删除 `WireDeckNat`、移除 WinNAT 防火墙规则、关闭 `WLAN`/`以太网` 的 `Forwarding`，默认**保留有线静态 `192.168.50.2/24`（无网关）**以实现 1 秒快切及开 VPN 时维持本地局域网直连；同时在设置页和 CLI 提供 **`reset-nic`（「恢复网卡出厂 DHCP」）** 一键还原快照；
  - 卸载器 `unins000.exe`（`PrivilegesRequired=lowest`）在卸载前自动探测 `WireDeckNat` 是否仍处于激活态：若激活则提示并拉起 `wiredeck-cli.exe apply disable-direct --elevate` 清理（交互卸载时若用户取消 UAC 则中止卸载并提示，静默卸载则记录明确警告日志，绝不伪报“零残留”）。

### 5.4 DSH / AI 开放接口契约（“开口”设计）

#### 接口 1：无头 CLI（`wiredeck-cli.exe`，所有 AI 零门槛可用）
全部支持 `--json` 输出，退出码语义稳定（`0`=成功，`2`=门禁拦截/前置未满足，`3`=权限不足，`1`=执行失败）：
```text
wiredeck-cli.exe diagnose [--json] [--console xbox|ps5|generic] [--no-extensions]
wiredeck-cli.exe status [--json]
wiredeck-cli.exe apply <enable-direct|disable-direct|fix-nic|reset-nic|fix-uwp|clear-ics|clear-netnat|run-ext> [--id <ext_id>] [--force] [--elevate] [--json]
wiredeck-cli.exe export-bundle [--out <path>] [--json]
wiredeck-cli.exe ext list [--json]
wiredeck-cli.exe ext validate <ext_dir_or_id> [--json]
wiredeck-cli.exe ext run <ext_id> [--mode check|fix] [--elevate] [--json]
```

#### 接口 2：明文热加载扩展槽（`extensions/<ext_id>/`，支持自动融入主诊断与安全覆写）
DSH 发现程序未涵盖的新故障（例如某款特殊校园网驱动、某品牌网卡特有节能键、未知代理冲突或自定义路由修复）时，只需往 `<data_dir>\extensions\<ext_id>\` 写入清单和脚本，无需重新编译 EXE，主界面刷新即热加载：
```json
// <data_dir>/extensions/fix-intel-i225/ext.json
{
  "schema": "wiredeck-ext/v1",
  "id": "fix-intel-i225",
  "name": "修复 Intel I225-V 节能断流",
  "author": "DSH",
  "enabled": true,
  "auto_check": true,
  "layer": "L1",
  "overrides_faults": ["NIC_POWERSAVE_ENABLED"],
  "requires_admin": true,
  "timeout_sec": 5,
  "check_script": "check.ps1",
  "fix_script": "fix.ps1",
  "description": "检测并关闭 Intel I225-V 特定节能注册表项"
}
```
- **融入主诊断与安全隔离机制**：
  - `enabled=true` 且 `auto_check=true` 的扩展会在每次 `diagnose` 时在独立子进程中运行（硬超时 `timeout_sec`，强制钳制在 `1~15s`），任何超时、崩溃或非法 JSON 仅生成独立的 `EXT_ERROR` 警告标签，**绝不拖垮或阻塞核心 L1~L6 诊断**；
  - 若扩展返回 `warn` 或 `fail`，其结果会自动合入所声明 `layer`（L1~L6）对应的主状态灯与断点提示卡片，带醒目 **`[AI 扩展]`** 徽标，并直接挂出调用 `fix_script` 的一键修复按钮；
  - 若声明了 `overrides_faults`，允许该扩展的结论覆写对应内置故障码的判定或修复动作（在详情中保留原始内置结论与覆写来源，可随时禁用扩展秒恢复原逻辑）；
  - 严格安全边界：加载前校验 `ext.json` Schema 并禁止路径越界（`check_script` / `fix_script` 必须解析在自身 `<ext_id>` 目录内）；`requires_admin=true` 的修复脚本严格走标准 UAC 确认，绝不静默提权。

#### 接口 3：脱敏诊断包（`export-bundle`）
一键生成单个 `diagnostic_bundle.json` + 剪贴板交接提示词，包含：
- WireDeck 版本、OS 版本、当前配置、6 层诊断结果与原始网卡/路由/PnP/端口/WinNAT/UWP 快照、最近 2 次操作日志；
- **严格脱敏**：不收集 Wi-Fi 密码、不收集任何 Token/环境变量密钥，自动将 Windows 用户名路径替换为 `<USER>`。

#### 接口 4：可选本地 HTTP Bridge（默认关闭）
在「AI 接口」面板手动开启后，仅绑定 `127.0.0.1:<随机空闲端口>`，每次启动生成 32 字节随机 Bearer Token 写入 `<data_dir>\runtime\bridge.json`（只读用户权限），提供 `GET /api/v1/diagnose`、`POST /api/v1/apply`、`GET /api/v1/extensions`；应用退出或开关关闭时立刻释放端口。

---

## 6. 数据所有权、打包与交付规范（落地 `exeskill.md` 精华）

### 6.1 数据目录与便携模式
- **显式标记文件**：`portable.mode`（位于 `WireDeck.exe` 同目录）
  - 存在 `portable.mode` → 数据写入 `<exe_dir>\data\`（便携模式）
  - 不存在 `portable.mode` → 数据写入 `%LOCALAPPDATA%\WireDeck\`（普通安装模式）
- **子目录结构**：
  - `config.json`（网段、主机类型、所选网卡、语言、界面偏好）
  - `extensions\`（DSH / 用户自建热加载扩展）
  - `logs\`（UTF-8 BOM 操作日志，按日滚动保留最近 14 份）
  - `exports\`（脱敏诊断包）
  - `runtime\`（临时提权请求/回执与 Bridge 信息）

### 6.2 文件所有权清单
1. **安装器拥有**（写入 Inno Setup 安装清单，卸载时移除）：`WireDeck.exe`、`wiredeck-cli.exe`、PyInstaller 运行时与静态资源、`web\` 前端资产、`assets\` 图标、开始菜单/桌面快捷方式、`unins000.exe`。
2. **程序与用户生成**（安装器绝不登记，升级与卸载**默认保留**）：`%LOCALAPPDATA%\WireDeck\`（或便携 `data\`）下的 `config.json`、`extensions\`、`logs\`、`exports\`。
3. **开发残留**（严禁进入源码或包）：`.env`、本地测试日志、个人机器调试数据、`build/`、`__pycache__/`。

### 6.3 构建与安装器方案
- **单一版本源**：`src/wiredeck/version.py` 定义 `APP_VERSION = "0.1.0"`、`APP_ID`（固定 GUID，永不随版本改变），由统一构建脚本 `scripts/build.ps1` 自动同步到：
  - PyInstaller 版本资源文件（EXE 右键属性版本号）
  - Inno Setup 6 编译宏（`installer/WireDeck.iss`）
  - 产物文件名：`WireDeck-v0.1.0-win64-setup.exe`、`WireDeck-v0.1.0-win64-portable.zip`、`SHA256SUMS.txt`
- **Inno Setup 6 向导设计**：
  - 默认当前用户安装（`PrivilegesRequired=lowest`，安装目录 `{localappdata}\Programs\WireDeck`，安装过程不弹 UAC）；
  - 支持 **简体中文 / English**（首屏统一语言切换，无前置碎弹窗）；
  - 支持选择「标准安装」或「便携数据模式（生成干净 `portable.mode`）」，并提供可选勾选将 `wiredeck-cli.exe` 目录加入当前用户 `PATH`；
  - 真实 `unins000.exe` 卸载器，卸载前检测活跃 `WireDeckNat` 并拉起提权清理，绝不使用 `{app}\*` 递归通配删除。

### 6.4 四层验收标准
1. **源码层**：`pytest` 全套单测（6 层故障分类、VPN TUN 与非 ICS 53 端口边界、扩展自动融合/覆写/超时/防路径穿越、脱敏器、CLI JSON 与退出码契约、便携/标准路径切换、配置迁移与 NIC 快照）。
2. **直跑 EXE 层**：从构建好的 `dist\WireDeck\` 实测 `wiredeck-cli.exe diagnose --json` / `status --json` / `export-bundle` / `ext list` 以及 `WireDeck.exe` GUI 启动、DPI（100%/125%/150%）真实截图检查。
3. **Setup 安装层**：在独立测试目录验证中文/英文普通安装、便携模式安装、含空格/中文路径安装、安装后启动真实核心功能。
4. **卸载层**：在安装目录与数据目录预埋哨兵文件，运行 `unins000.exe` 后确认程序文件与快捷方式清除、哨兵文件与用户数据/扩展完好无损。

---

## 7. 开源仓库与 GitHub 门面规划
仓库位于 `d:\New\WireDeck\`，包含：
- **品牌象征视觉**：原创极简 SVG 矢量图标（象征笔记本 + 闪电千兆链路 + 主机节点）并生成多尺寸 `.ico`（16~256px）嵌入 EXE 与安装器；
- **高颜值精炼 `README.md`**：顶部 Logo + 一句话定位 + 特性徽章 + **实机高清截图**（主界面正常态、断点精准标红态、首次向导、AI 协作抽屉）+ 3 步极简上手 + DSH/AI 接入速查 + 实测有线 vs 2.4G Wi-Fi 对比表 + 下载与 SHA-256 说明；
- **MIT `LICENSE`**、`CONTRIBUTING.md`、`.gitignore`、`docs/AI_EXTENSION_SPEC.md`（给 DSH 和其他 AI 阅读的标准协议文档）；
- **Release 预置包**：本地输出到 `release/` 目录（不提交进 Git 源码树），等 GitHub 服务恢复且经用户确认后再创建仓库、推送代码并发布首个 GitHub Release。

---

## 8. 架构分层（留给后续实现计划细化）

```text
d:\New\WireDeck\
├── src\wiredeck\
│   ├── version.py            # 单一版本与稳定 AppId 来源
│   ├── paths.py              # 普通/便携模式与安全路径解析
│   ├── config.py             # 配置读写与默认主机档案（Xbox/PS5/Generic）
│   ├── models.py             # 6 层诊断与操作回执的类型化模型
│   ├── ps_runner.py          # 统一 UTF-8 PowerShell 执行器（只读直跑 / 写操作按需 UAC）
│   ├── diagnostics.py        # 6 层断点探测与故障码分类引擎
│   ├── actions.py            # 网卡唤醒防抖 / UWP 豁免 / WinNAT 启停 / ICS 清理 / 串流端获取
│   ├── extensions.py         # 明文 extensions/ 扫描、校验、防越界沙箱与运行器
│   ├── exporter.py           # 脱敏诊断包与给 AI 的交接词生成
│   ├── bridge.py             # 可选（默认关闭）的本地 127.0.0.1 HTTP JSON Bridge
│   ├── cli.py                # 无头 CLI 子命令入口（diagnose/status/apply/ext/export-bundle）
│   ├── app.py                # pywebview 窗口托管、JS-Python API 桥接、单入口路由
│   └── web\                  # 极简现代前端（HTML/CSS/SVG/JS，零构建复杂度，中英双语）
├── tests\                    # pytest 单元测试、契约测试与打包/安装/卸载验证脚本
├── assets\                   # 原创 SVG Logo、多尺寸 ICO、README 实机截图
├── installer\WireDeck.iss    # Inno Setup 6 安装与真实卸载脚本
├── scripts\build.ps1         # 干净构建：图标 -> 版本同步 -> PyInstaller -> Inno Setup -> SHA-256
├── docs\                     # 设计文档、实施计划、AI_EXTENSION_SPEC.md
├── README.md
└── LICENSE
```

### 明确由实现阶段定夺的细节
- 具体视觉配色 token、图标细节和动效时长；
- XStreamingDesktop 官方 GitHub API 查询失败时的优雅降级提示（手动打开发布页链接）；
- `pywebview` 独立干净构建虚拟环境（`d:\New\WireDeck\.venv`）的依赖锁定与 Inno Setup 安装检测。

---

## 9. 工程决策备忘（由架构师代为敲定，供最终审阅复核）
1. **GUI + CLI 双可执行入口**：同目录打包 `WireDeck.exe`（GUI，零黑框）与 `wiredeck-cli.exe`（CUI，同步 JSON 输出与真实退出码，可选加入用户 `PATH`），同时兼顾普通玩家和 DSH 终端调用。
2. **回滚留静态 IP + 出厂 DHCP 备份 + 卸载前检测清理**：`disable-direct` 默认保留静态 `192.168.50.2/24`（无网关）实现 1 秒快切，但首次开启前会备份原 DHCP 配置并提供 `reset-nic` 一键恢复；`unins000.exe` 检测到活跃 `WireDeckNat` 时自动引导提权清理，避免残留脏状态。
3. **防误伤第三方环境**：非 WireDeck 的 `NetNat`（如 Hyper-V/Docker/WSL）绝不静默删除，需显式确认 / `--force`；非 ICS 的第三方 `UDP 53` 占用（如 SmartDNS/AdGuard）仅标黄提醒、不硬阻断且绝不强杀进程。
4. **严格提权安全边界**：非管理员终端调用 CLI 写操作默认返回退出码 `3` 而不意外弹 UAC，显式 `--elevate` 才弹 UAC（管理员终端直接无弹窗执行）；HTTP Bridge 特权写操作采用异步 `job_id` + 界面人工确认 + 标准 UAC。

