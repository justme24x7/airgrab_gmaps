#!/usr/bin/env python3
"""Pipeline step 6: build block list of blocked provider ids.

What it does:
  Scans all ``gapi_cache/shard_*.json`` files, collects provider ids where the cache
  entry has top-level ``is_permanently_closed`` == ``true`` or
  ``is_manually_blocked`` == ``true``, and writes ``p6_block_list/block_list.json``.

Overall logic:
  - Iterate every shard file.
  - For each entry, check top-level ``is_permanently_closed`` or ``is_manually_blocked``.
  - Output sorted, deduplicated ``provider_ids`` list with metadata.

Skip / short-circuit logic:
  None. This is a read-only export utility (no per-provider processing pipeline).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p6_block_list"
DEFAULT_OUTPUT_PATH = DEFAULT_OUTPUT_DIR / "block_list.json"
SHARD_GLOB = "shard_*.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_shard(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a top-level JSON object")
    return {
        str(provider_id): entry
        for provider_id, entry in data.items()
        if provider_id and isinstance(entry, dict)
    }


def is_blocked_cache_entry(entry: dict[str, Any]) -> bool:
    return (
        entry.get("is_permanently_closed") is True
        or entry.get("is_manually_blocked") is True
    )


def collect_blocked_provider_ids(cache_dir: Path) -> list[str]:
    provider_ids: list[str] = []

    for shard_path in sorted(cache_dir.glob(SHARD_GLOB)):
        if not shard_path.is_file() or shard_path.name == ".DS_Store":
            continue
        shard = load_shard(shard_path)
        for provider_id, entry in shard.items():
            if is_blocked_cache_entry(entry):
                provider_ids.append(provider_id)

    return sorted(set(provider_ids))


def main() -> int:
    cache_dir = DEFAULT_CACHE_DIR.resolve()
    output_path = DEFAULT_OUTPUT_PATH.resolve()

    if not cache_dir.is_dir():
        print(f"Error: cache directory does not exist: {cache_dir}", file=sys.stderr)
        return 1

    try:
        provider_ids = collect_blocked_provider_ids(cache_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error reading gapi cache: {exc}", file=sys.stderr)
        return 1

    payload = {
        "generated_at": utc_now_iso(),
        "cache_dir": str(cache_dir),
        "count": len(provider_ids),
        "provider_ids": provider_ids,
    }

    try:
        write_json(output_path, payload)
    except OSError as exc:
        print(f"Error writing output: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {len(provider_ids)} blocked provider id(s) "
        f"(permanently closed and/or manually blocked) to {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
