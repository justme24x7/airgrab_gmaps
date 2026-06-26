#!/usr/bin/env python3
"""One-time migration: rename is_manually_verified -> is_url_manually_verified in gapi_cache shards."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
OLD_KEY = "is_manually_verified"
NEW_KEY = "is_url_manually_verified"
SHARD_GLOB = "shard_*.json"


def rename_key_in_result(result: dict[str, Any]) -> bool:
    if OLD_KEY not in result:
        return False
    if NEW_KEY not in result:
        result[NEW_KEY] = result[OLD_KEY]
    del result[OLD_KEY]
    return True


def migrate_shard(path: Path) -> tuple[int, int]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a top-level JSON object")

    entries_changed = 0
    keys_renamed = 0
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        if rename_key_in_result(result):
            entries_changed += 1
            keys_renamed += 1

    if keys_renamed:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    return entries_changed, keys_renamed


def main() -> int:
    cache_dir = DEFAULT_CACHE_DIR.resolve()
    if not cache_dir.is_dir():
        print(f"Error: cache directory does not exist: {cache_dir}", file=sys.stderr)
        return 1

    shard_paths = sorted(
        path
        for path in cache_dir.glob(SHARD_GLOB)
        if path.is_file() and path.name != ".DS_Store"
    )
    if not shard_paths:
        print(f"No shard files found in {cache_dir}")
        return 0

    total_entries = 0
    total_keys = 0
    shards_changed = 0

    for path in shard_paths:
        try:
            entries_changed, keys_renamed = migrate_shard(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Error migrating {path.name}: {exc}", file=sys.stderr)
            return 1

        if keys_renamed:
            shards_changed += 1
            print(f"{path.name}: renamed {keys_renamed} result key(s)")
        total_entries += entries_changed
        total_keys += keys_renamed

    print(
        f"\nDone. {total_keys} key rename(s) across "
        f"{total_entries} entr{'y' if total_entries == 1 else 'ies'} "
        f"in {shards_changed} shard file(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
