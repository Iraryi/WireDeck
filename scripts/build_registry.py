"""Build `registry/index.json` with SHA-256 digests for all plugins under `registry/plugins/`."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wiredeck.version import PLUGIN_REGISTRY_SCHEMA, REPO_URL

RAW_BASE_DEFAULT = "https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/plugins"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def build_registry(
    repo_root: Path | None = None,
    *,
    raw_base: str = RAW_BASE_DEFAULT,
) -> dict[str, Any]:
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    plugins_root = root / "registry" / "plugins"
    entries: list[dict[str, Any]] = []

    if plugins_root.is_dir():
        for pdir in sorted(plugins_root.iterdir(), key=lambda p: p.name.lower()):
            if not pdir.is_dir():
                continue
            manifest_path = pdir / "plugin.json"
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            plugin_id = str(manifest["id"])

            files: list[dict[str, Any]] = []
            for fpath in sorted(pdir.rglob("*"), key=lambda p: p.as_posix().lower()):
                if not fpath.is_file():
                    continue
                rel = fpath.relative_to(pdir).as_posix()
                if rel.startswith("_"):
                    continue
                files.append(
                    {
                        "path": rel,
                        "url": f"{raw_base.rstrip('/')}/{plugin_id}/{rel}",
                        "sha256": _sha256_file(fpath),
                        "size_bytes": fpath.stat().st_size,
                    }
                )

            entries.append(
                {
                    "id": plugin_id,
                    "name": manifest["name"],
                    "version": manifest["version"],
                    "author": manifest.get("author", "WireDeck Community"),
                    "category": manifest.get("category", "automation"),
                    "capabilities": manifest.get("capabilities", []),
                    "preset_scope": manifest.get("preset_scope", ["all"]),
                    "description": manifest.get("description", ""),
                    "homepage": manifest.get("homepage") or f"{REPO_URL}/tree/main/registry/plugins/{plugin_id}",
                    "requirements": manifest.get("requirements", {}),
                    "hooks": list((manifest.get("hooks") or {}).keys()),
                    "files": files,
                }
            )

    payload = {
        "schema": PLUGIN_REGISTRY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plugins": entries,
    }
    out_file = root / "registry" / "index.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    res = build_registry()
    print(f"Generated registry/index.json with {len(res['plugins'])} plugin(s).")
