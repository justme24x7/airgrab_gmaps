#!/usr/bin/env python3
"""Apply manual gapi_cache edits from airgrab/manual_inputs JSON files.

Reads, in order:
  1. manual_gmaps_uri.json   — formatted_google_maps_uri + URL verified flags
  2. manual_ratings.json   — rating, total_reviews + MANUAL rating_type
  3. manual_block.json       — block_reason + is_manually_blocked

When a provider appears in multiple files, later files override earlier ones
(block > ratings > gmaps_uri). Existing cache fields (e.g. gapi_response) are
preserved unless overwritten by these patches.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
MANUAL_INPUTS_DIR = SCRIPT_DIR / "manual_inputs"
MANUAL_GMAPS_URI_PATH = MANUAL_INPUTS_DIR / "manual_gmaps_uri.json"
MANUAL_RATINGS_PATH = MANUAL_INPUTS_DIR / "manual_ratings.json"
MANUAL_BLOCK_PATH = MANUAL_INPUTS_DIR / "manual_block.json"

sys.path.insert(0, str(SCRIPT_DIR))
from gapi_cache import GapiCache, RATING_TYPE_MANUAL


def load_manual_providers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Manual input file not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")

    providers: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{path.name}[{index}]: expected a provider object")
        provider_id = str(item.get("id") or "").strip()
        if not provider_id:
            raise ValueError(f"{path.name}[{index}]: missing provider id")
        providers[provider_id] = item
    return providers


def _results_dict(record: dict[str, Any]) -> dict[str, Any]:
    results = record.get("results")
    return results if isinstance(results, dict) else {}


def build_base_entry(
    cache: GapiCache,
    provider_id: str,
    manual_record: dict[str, Any],
) -> dict[str, Any]:
    existing = cache.get_entry(provider_id)
    entry = deepcopy(existing) if existing else {}
    entry["id"] = provider_id

    local_id = manual_record.get("local_id")
    if local_id is not None:
        entry["local_id"] = local_id
    elif "local_id" not in entry:
        entry["local_id"] = None

    result = entry.get("result")
    if not isinstance(result, dict):
        entry["result"] = {}
    return entry


def apply_gmaps_uri_patch(entry: dict[str, Any], manual_record: dict[str, Any]) -> None:
    results = _results_dict(manual_record)
    uri = results.get("formatted_google_maps_uri")
    if uri is None:
        return

    uri = str(uri).strip()
    if not uri:
        return

    result = entry.setdefault("result", {})
    if not isinstance(result, dict):
        result = {}
        entry["result"] = result

    result["formatted_google_maps_uri"] = uri
    result["is_url_manually_verified"] = True
    entry["is_gapi_called"] = True


def apply_ratings_patch(entry: dict[str, Any], manual_record: dict[str, Any]) -> None:
    results = _results_dict(manual_record)

    result = entry.setdefault("result", {})
    if not isinstance(result, dict):
        result = {}
        entry["result"] = result

    if "rating" in results:
        result["rating"] = results["rating"]
    if "total_reviews" in results:
        result["total_reviews"] = results["total_reviews"]

    result["rating_type"] = RATING_TYPE_MANUAL
    entry["is_gapi_called"] = True


def apply_block_patch(entry: dict[str, Any], manual_record: dict[str, Any]) -> None:
    block_reason = manual_record.get("block_reason")
    if block_reason is None:
        block_reason = _results_dict(manual_record).get("block_reason")
    if block_reason is not None:
        entry["block_reason"] = block_reason

    entry["is_manually_blocked"] = True


def apply_manual_edits(cache: GapiCache) -> dict[str, int]:
    gmaps_providers = load_manual_providers(MANUAL_GMAPS_URI_PATH)
    ratings_providers = load_manual_providers(MANUAL_RATINGS_PATH)
    block_providers = load_manual_providers(MANUAL_BLOCK_PATH)

    all_provider_ids = sorted(
        set(gmaps_providers) | set(ratings_providers) | set(block_providers)
    )

    counts = {
        "gmaps_uri": 0,
        "ratings": 0,
        "block": 0,
        "total": 0,
    }

    for provider_id in all_provider_ids:
        manual_record = (
            block_providers.get(provider_id)
            or ratings_providers.get(provider_id)
            or gmaps_providers.get(provider_id)
            or {}
        )
        entry = build_base_entry(cache, provider_id, manual_record)

        if provider_id in gmaps_providers:
            apply_gmaps_uri_patch(entry, gmaps_providers[provider_id])
            counts["gmaps_uri"] += 1

        if provider_id in ratings_providers:
            apply_ratings_patch(entry, ratings_providers[provider_id])
            counts["ratings"] += 1

        if provider_id in block_providers:
            apply_block_patch(entry, block_providers[provider_id])
            counts["block"] += 1

        cache.import_entry(provider_id, entry)
        counts["total"] += 1

    return counts


def main() -> int:
    cache_dir = DEFAULT_CACHE_DIR.resolve()
    if not cache_dir.is_dir():
        print(f"Error: cache directory does not exist: {cache_dir}", file=sys.stderr)
        return 1

    cache = GapiCache(cache_dir)
    try:
        counts = apply_manual_edits(cache)
        cache.flush()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error applying manual edits: {exc}", file=sys.stderr)
        return 1

    print(
        f"Applied manual edits to {counts['total']} provider(s) in {cache_dir.name}/ "
        f"(gmaps_uri={counts['gmaps_uri']}, ratings={counts['ratings']}, "
        f"block={counts['block']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
