# WireDeck AI / DSH Extension & CLI Specification (`v1`)

WireDeck is built to be **AI-operable out of the box**. When an unfamiliar laptop hardware quirk, campus network policy, or third-party proxy breaks console streaming, an AI assistant (such as **DSH** or any local coding agent) can inspect WireDeck's sanitized state, run headless diagnostics, and hot-load new check/fix rules into `extensions/` **without recompiling `WireDeck.exe`**.

---

## 1. Headless CLI (`wiredeck-cli.exe`)

Packaged alongside `WireDeck.exe` (subsystem `WINDOWS_CUI`, UTF-8 stdout, optionally added to user `PATH` by the installer).

### Commands

```powershell
wiredeck-cli.exe diagnose [--json] [--console xbox|playstation|switch|handheld|other] [--no-extensions] [--no-network]
wiredeck-cli.exe status [--json] [--check-nat-active]
wiredeck-cli.exe apply <action> [--id <ext_id>] [--force] [--elevate] [--json]
wiredeck-cli.exe export-bundle [--out <path>] [--json]
wiredeck-cli.exe ext list [--json]
wiredeck-cli.exe ext validate <ext_dir_or_id> [--json]
wiredeck-cli.exe ext run <ext_id> [--mode check|fix] [--elevate] [--json]
wiredeck-cli.exe toolkit list [--preset xbox|playstation|switch|handheld|other] [--all] [--json]
wiredeck-cli.exe toolkit run <tool_id> [--json]
```

> Legacy aliases `ps5 -> playstation` and `generic -> other` are accepted by `--console` and `--preset`.

### Supported `apply` Actions

| Action | Description | Guard / Safety Boundary |
| :--- | :--- | :--- |
| `enable-direct` | Snapshot NIC backup (once), set static IPv4 without gateway, enable IPv4 Forwarding, create `WireDeckNat` & scoped firewall rules | Hard-blocked (exit `2`) if VPN TUN holds `0.0.0.0/0`, ICS active, UDP 67 or ICS UDP 53 occupied, or foreign `NetNat` exists |
| `disable-direct` | Remove `WireDeckNat`, remove WinNAT firewall rules, disable Forwarding | Preserves local static IP (`192.168.50.2/24`, no gateway) for instant re-toggle |
| `reset-nic` | Clean WinNAT + restore wired NIC to factory DHCP & reset DNS | Handles missing/corrupt `nic_backup.json` safely |
| `fix-nic` | Re-enable disabled parent PCIe Root Port + Ethernet NIC, turn off `*EEE` / `PowerSavingMode` / `EnableGreenEthernet` / `GigaLite` | Dynamic PnP / adapter lookup |
| `fix-uwp` | Add AppContainer loopback exemptions for 17 core Microsoft Store / Xbox / Auth UWP packages | Idempotent `CheckNetIsolation.exe` calls |
| `clear-ics` | Disable `HNetCfg.HNetShare` ICS flags; stop `SharedAccess` only if ICS/UDP 53/67 active | Never kills third-party non-ICS DNS processes (SmartDNS / AdGuard) |
| `clear-netnat` | Remove non-`WireDeckNat` Windows `NetNat` entries | Refuses without `--force` (exit `2`, `CONFIRM_REQUIRED`) |
| `run-ext` | Run `<ext_id>`'s `fix_script` | Path-traversal guarded, timeout clamped `1..15s` |

### Stable Exit Codes

| Exit Code | Meaning |
| :---: | :--- |
| `0` | Success |
| `1` | Execution / validation error |
| `2` | Safety gate blocked (`VPN_TUN_ACTIVE`, `ICS_ACTIVE`, `FOREIGN_NETNAT_EXISTS`, `CONFIRM_REQUIRED`) |
| `3` | Administrator privilege required (`ADMIN_REQUIRED` without `--elevate`, or `UAC_CANCELLED`) |

> **Headless Safety:** Calling `wiredeck-cli.exe apply ... --json` from a non-elevated shell **never hangs on an unexpected UAC dialog** — it exits immediately with code `3` and JSON `{"ok": false, "code": "ADMIN_REQUIRED"}`. Pass `--elevate` only when interactive UAC is intended, or invoke from an already-elevated shell for zero-popup execution.

---

## 2. Hot-Reload Extensions (`wiredeck-ext/v1`)

### Directory Location

- **Standard Install:** `%LOCALAPPDATA%\WireDeck\extensions\<ext_id>\`
- **Portable Mode (`portable.mode` beside EXE):** `<install_dir>\data\extensions\<ext_id>\`

Every valid extension folder contains `ext.json` plus relative `.ps1` script(s) strictly inside that folder.

### `ext.json` Schema

```json
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

### Script Execution Contract

- WireDeck runs the `.ps1` in an isolated UTF-8 PowerShell subprocess with `cwd = <ext_dir>` and environment variables:
  - `WIREDECK_EXT_ID`
  - `WIREDECK_EXT_MODE` (`check` or `fix`)
  - `WIREDECK_EXT_DIR`
- Hard timeout is enforced and clamped to **`1..15` seconds**.
- Script stdout must emit a single UTF-8 JSON object:

```json
{
  "status": "fail",
  "code": "EXT_INTEL_I225_EEE",
  "title": "I225 节能异常",
  "message": "检测到 I225-V 节能注册表项未关闭，点击一键修复",
  "details": { "adapter": "Ethernet" }
}
```

- Valid `status` values: `"ok"`, `"warn"`, `"fail"`, `"idle"`, `"skipped"`.
- If `auto_check: true` and `enabled: true`, WireDeck runs `check_script` on every `diagnose`, merges any `warn`/`fail` into the declared `layer` (`L1..L6`) with an **`[AI 扩展]`** badge, and exposes `fix_script` as a 1-click button.
- If `overrides_faults` lists built-in fault codes (e.g. `["NIC_POWERSAVE_ENABLED"]`), those built-in faults on the target layer are replaced by the extension's conclusion.
- Any path traversal (`..\`), missing file, timeout, crash, or invalid JSON is safely quarantined as a non-blocking `EXT_ERROR` warning and never breaks core L1–L6 diagnostics.

---

## 3. Optional Local HTTP Bridge (OFF by Default)

When enabled manually from **AI 接口 (AI Port)** in the GUI:
- Binds strictly to `127.0.0.1:<ephemeral_port>` with a 32-byte random Bearer token written to `<data_dir>\runtime\bridge.json`.
- Endpoints (all require `Authorization: Bearer <token>`):
  - `GET /api/v1/health`
  - `GET /api/v1/diagnose`
  - `GET /api/v1/extensions`
  - `GET /api/v1/toolkits` — lists bundled read-only toolkits (optional `?all=1` or `?preset=<id>`)
  - `POST /api/v1/toolkits/run` — body `{"tool_id": "<scope/name>"}`, executes read-only bundled probe immediately without elevating
  - `POST /api/v1/apply` — returns `202 Accepted` + `{"job_id": "...", "status": "pending_approval"}` and prompts the human user in-app before any privileged write runs.
  - `GET /api/v1/jobs/<job_id>` — poll final status (`pending_approval` | `running` | `completed` | `rejected` | `failed`).
- Turning the toggle OFF or closing WireDeck immediately stops the server and removes `runtime/bridge.json`.

---

## 4. Bundled Universal & Per-Preset Toolkits (`wiredeck-toolkits/v1`)

WireDeck ships **16 read-only PowerShell toolkits** (universal host probes under `common/` plus tailored scripts under each preset) callable from the GUI **AI 接口** panel, HTTP Bridge `/api/v1/toolkits`, and `wiredeck-cli.exe toolkit run <tool_id> --json`.

| Tool ID | Scope | Purpose |
| :--- | :--- | :--- |
| `common/probe-route-table` | Universal | Dump IPv4 default routes, metrics, and VPN TUN interfaces |
| `common/probe-nic-pcie-tree` | Universal | Inspect wired NICs, driver versions, link speed & power-save flags |
| `common/probe-udp-ports` | Universal | Check listeners/services holding UDP 53/67 and streaming ports |
| `common/probe-mtu-path` | Universal | DF-ping upstream path MTU probe |
| `common/probe-winnat-firewall` | Universal | Inspect `NetNat` entries, IPv4 forwarding, and WireDeck firewall rules |
| `common/bench-peer-jitter` | Universal | 8-packet direct RTT & jitter check to current preset `peer_ip` |
| `xbox/check-xbox-endpoints` | `xbox` | DNS + HTTPS latency for Xbox Live / XSTS / Store endpoints |
| `xbox/audit-uwp-loopback` | `xbox` | Verify AppContainer loopback exemptions for Store / Xbox packages |
| `xbox/check-xbox-ports` | `xbox` | Inspect local listeners on Xbox ports (UDP 3074 / 5050 / 9002) |
| `playstation/check-psn-endpoints` | `playstation` | Probe PSN auth, account, and PlayStation Store HTTPS reachability |
| `playstation/check-ps-remote-ports` | `playstation` | Check PS Remote Play / Chiaki-ng ports (9295-9297 / 9302 / 987) |
| `switch/check-nintendo-endpoints` | `switch` | Verify `ctest.cdn.nintendo.net` and `conntest.nintendowifi.net` |
| `switch/bench-switch-dns` | `switch` | Compare DNS lookup latency across 223.5.5.5 / 119.29.29.29 / 8.8.8.8 / 168.126.63.1 |
| `handheld/check-stream-host-ports` | `handheld` | Inspect Sunshine/Moonlight (47984-48000) and Steam stream listeners |
| `handheld/emit-linux-nmcli-script` | `handheld` | Generate ready-to-paste `nmcli` & policy-route commands for SteamOS/Linux |
| `other/emit-peer-config-snippets` | `other` | Generate Linux, macOS, and Windows peer static IP commands for the active subnet |

Each script receives `WIREDECK_PRESET`, `WIREDECK_HOST_IP`, `WIREDECK_PEER_IP`, `WIREDECK_PREFIX_LENGTH`, `WIREDECK_SUBNET_MASK`, `WIREDECK_SUBNET_CIDR`, and `WIREDECK_DNS_SERVERS`.

---

## 5. Sanitized Diagnostic Export

`wiredeck-cli.exe export-bundle` (or **复制现场给 AI** in the GUI) writes `<data_dir>\exports\diagnostic_latest.json`:
- Redacts Windows profile usernames (`C:\Users\<USER>`) and strips keys matching `token`, `password`, `secret`, `api_key`, `credential`, `cookie`, and `Bearer` headers.
- Never collects Wi-Fi PSKs or browser cookies.
