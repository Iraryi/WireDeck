"""Single source of truth for WireDeck version, identity, and protocol constants."""

from __future__ import annotations

APP_NAME = "WireDeck"
APP_VERSION = "0.2.3"
APP_VERSION_TUPLE = (0, 2, 3, 0)
APP_ID = "{8E4F7A21-6C3D-4B9E-9A12-5D8C1F4E2B70}"
PUBLISHER = "WireDeck Contributors"
REPO_URL = "https://github.com/Iraryi/WireDeck"
SCHEMA_VERSION = "1.0"
EXT_SCHEMA_VERSION = "wiredeck-ext/v1"
PLUGIN_SCHEMA_VERSION = "wiredeck-plugin/v2"
PLUGIN_REGISTRY_SCHEMA = "wiredeck-registry/v1"
UPDATE_MANIFEST_SCHEMA = "wiredeck-update/v1"
DEFAULT_PLUGIN_REGISTRY_URL = "https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/index.json"
DEFAULT_UPDATE_API_URL = "https://api.github.com/repos/Iraryi/WireDeck/releases/latest"
DEFAULT_UPDATE_RELEASES_URL = "https://api.github.com/repos/Iraryi/WireDeck/releases"

NAT_NAME = "WireDeckNat"
FW_RULE_NAT = "WireDeck-WinNAT-Allow"
FW_RULE_DIRECT = "WireDeck-Direct-Allow"
