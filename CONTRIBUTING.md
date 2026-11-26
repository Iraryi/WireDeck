# Contributing to WireDeck

Thank you for helping improve WireDeck!

## Development Setup

1. Requires **Windows 10 / 11 (x64)**, **Python 3.11+**, and **Inno Setup 6** (for installer builds).
2. Create an isolated virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install pywebview pillow pytest pyinstaller
```

3. Run the unit & contract test suite:

```powershell
.\.venv\Scripts\pytest.exe tests/ -v
```

## Coding & Safety Standards

- **Zero hardcoded machine values**: never hardcode adapter names, IPs, SSIDs, usernames, or single VPN brands.
- **Minimalist UI copy**: keep Home screen card titles ≤ 12 CJK chars and breakpoint messages ≤ 28 CJK chars; every status badge must combine icon + text.
- **Strict UAC & network hygiene**: never use ICS or L2 Bridge as the sharing backend; never silently delete foreign `NetNat` entries; never kill third-party non-ICS DNS processes; never bypass Windows UAC.
- **UTF-8 everywhere**: `.ps1` files and saved logs use UTF-8 with BOM + CRLF; CLI stdout and JSON use UTF-8.

## Reproducible Local Build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

This builds `dist\WireDeck\` (`WireDeck.exe` + `wiredeck-cli.exe`), `release\WireDeck-v<version>-win64-setup.exe`, `release\WireDeck-v<version>-win64-portable.zip`, and `release\SHA256SUMS.txt`.
