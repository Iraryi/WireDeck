"""Single source of truth for WireDeck version, identity, and protocol constants."""

from __future__ import annotations

APP_NAME = "WireDeck"
APP_VERSION = "0.1.0"
APP_VERSION_TUPLE = (0, 1, 0, 0)
APP_ID = "{8E4F7A21-6C3D-4B9E-9A12-5D8C1F4E2B70}"
PUBLISHER = "WireDeck Contributors"
REPO_URL = "https://github.com/wiredeck/WireDeck"
SCHEMA_VERSION = "1.0"
EXT_SCHEMA_VERSION = "wiredeck-ext/v1"

NAT_NAME = "WireDeckNat"
FW_RULE_NAT = "WireDeck-WinNAT-Allow"
FW_RULE_DIRECT = "WireDeck-Direct-Allow"
