# WireDeck Wireless Mode Design (`无线模式`)

**Date:** 2026-11-26
**Approach:** Approach 1 — Peer left-rail **有线直连 (`Wired Direct`)** and **无线模式 (`Wireless Mode`)** pillars sharing 4 languages (`zh-CN`, `zh-TW`, `en`, `ja`), 5 isolated device presets (`xbox`, `playstation`, `switch`, `handheld`, `other`), Setup Wizard, AI/DSH Port, and Settings.

## 1. Goals & Non-Goals
- **Goals:**
  - Support two practical wireless workflows inside one clean Windows 11 Fluent / PowerToys / WireGuard interface:
    1. **5 GHz Mobile Hotspot Direct (`hotspot`)**: Windows 10/11 WinRT `NetworkOperatorTetheringManager` 5 GHz hotspot creation, SSID/masked-passphrase/client-count inspection, auto-discovery of connected peer IPs on `192.168.137.0/24`, and conflict detection with wired `WireDeckNat`.
    2. **Same-LAN Wi-Fi Optimization (`lan`)**: Safe reversible anti-stutter tuning (`wifi_backup.json` snapshot, disable `MIMOPowerSaveMode` / `*PacketCoalescing` / aggressive roaming, pause Windows background `autoconfig` scanning only while connected), plus AP Isolation detection, band/signal/speed checks, and multi-packet jitter benchmarking.
  - Keep Wired Direct 100% intact and isolated; expose full CLI, HTTP Bridge, and 4 new read-only wireless AI toolkits (`20` bundled scripts total).
- **Non-Goals / Guardrails:**
  - Never pause `netsh wlan set autoconfig` when Wi-Fi is disconnected.
  - Never log or export hotspot `passphrase` in diagnostic bundles or daily logs (`SECRET_KEY_RE` redacts `passphrase` and `wifi_password`).

## 2. Architecture
- `src/wiredeck/models.py`: Add `transport: str = "wired"` on `DiagnosticReport`.
- `src/wiredeck/config.py`: Add `active_transport`, `wireless_submode`, `wifi_interface`, `hotspot_prefer_5ghz`, and isolated per-preset `wireless_peer_ip`.
- `src/wiredeck/wireless_probes.py`: Parse `netsh wlan show interfaces` & `autoconfig`, collect WinRT Mobile Hotspot metadata & `192.168.137.*` ARP neighbors, collect Wi-Fi advanced properties, run parallel internet/preset/jitter/gateway pings, and expose GUI-only `get_hotspot_credentials()`.
- `src/wiredeck/wireless_diagnostics.py`: Classify `W1..W6` layers + merge AI extensions into `DiagnosticReport(transport="wireless")`.
- `src/wiredeck/wireless_actions.py` & `actions.py`: Privileged actions `enable-hotspot`, `disable-hotspot`, `optimize-wifi`, `restore-wifi` with atomic rollback backups and UTF-8 BOM logs.
- `src/wiredeck/toolkits/`: 4 new universal wireless read-only scripts (`probe-wifi-rf-scan`, `probe-hotspot-clients`, `bench-wifi-jitter`, `probe-wifi-advanced`).
- `src/wiredeck/web/`: Peer left-rail nav item `无线模式`, `[5GHz 电脑热点 | 同 Wi-Fi 局域网]` control bar + compact strip + 5-row diagnostics table, onboarding transport selector, wireless-aware Wizard & Settings, and 4-language parity across all 16 wireless fault codes.
