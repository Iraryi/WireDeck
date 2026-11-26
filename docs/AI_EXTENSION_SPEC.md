# WireDeck AI / DSH Extension & CLI Specification (`v1`)

WireDeck is built to be **AI-operable out of the box**. When an unfamiliar laptop hardware quirk, campus network policy, or third-party proxy breaks console streaming, an AI assistant (such as **DSH** or any local coding agent) can inspect WireDeck's sanitized state, run headless diagnostics, and hot-load new check/fix rules into `extensions/` **without recompiling `WireDeck.exe`**.

---

## 1. Headless CLI (`wiredeck-cli.exe`)

Packaged alongside `WireDeck.exe` (subsystem `WINDOWS_CUI`, UTF-8 stdout, optionally added to user `PATH` by the installer).

### Commands

```powershell
wiredeck-cli.exe diagnose [--json] [--console xbox|ps5|generic] [--no-extensions] [--no-network]
wiredeck-cli.exe status [--json] [--check-nat-active]
wiredeck-cli.exe apply <action> [--id <ext_id>] [--force] [--elevate] [--json]
wiredeck-cli.exe export-bundle [--out <path>] [--json]
wiredeck-cli.exe ext list [--json]
wiredeck-cli.exe ext validate <ext_dir_or_id> [--json]
wiredeck-cli.exe ext run <ext_id> [--mode check|fix] [--elevate] [--json]
```

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
  - `POST /api/v1/apply` — returns `202 Accepted` + `{"job_id": "...", "status": "pending_approval"}` and prompts the human user in-app before any privileged write runs.
  - `GET /api/v1/jobs/<job_id>` — poll final status (`pending_approval` | `running` | `completed` | `rejected` | `failed`).
- Turning the toggle OFF or closing WireDeck immediately stops the server and removes `runtime/bridge.json`.

---

## 4. Sanitized Diagnostic Export

`wiredeck-cli.exe export-bundle` (or **复制现场给 AI** in the GUI) writes `<data_dir>\exports\diagnostic_latest.json`:
- Redacts Windows profile usernames (`C:\Users\<USER>`) and strips keys matching `token`, `password`, `secret`, `api_key`, `credential`, `cookie`, and `Bearer` headers.
- Never collects Wi-Fi PSKs or browser cookies.
