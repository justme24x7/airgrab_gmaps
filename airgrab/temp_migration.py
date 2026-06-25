#!/usr/bin/env python3
"""One-off migration: p4 batch outputs -> sharded gapi_cache."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gapi_cache import GapiCache, utc_now_iso

DEFAULT_INPUT_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output_errors"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
BATCH_GLOB = "*.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate providers from p4 batch output files into sharded gapi_cache."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Successful p4 output directory (default: {DEFAULT_INPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Failed p4 output directory (default: {DEFAULT_ERROR_DIR.name})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"gapi_cache directory (default: {DEFAULT_CACHE_DIR.name})",
    )
    parser.add_argument(
        "--glob",
        default=BATCH_GLOB,
        help=f"Batch file glob (default: {BATCH_GLOB})",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete existing gapi_cache contents before migrating",
    )
    return parser.parse_args(argv)


def load_batch_file(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return [item for item in data if isinstance(item, dict)]


def list_batch_files(directory: Path, glob_pattern: str) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(glob_pattern))


def provider_id_from_record(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def build_result(record: dict[str, Any]) -> dict[str, Any]:
    results = record.get("results")
    result: dict[str, Any] = {
        "cid": None,
        "formatted_google_maps_uri": None,
        "place_types": None,
        "is_manually_verified": False,
    }
    if not isinstance(results, dict):
        return result

    result["cid"] = results.get("cid")
    result["formatted_google_maps_uri"] = results.get("formatted_google_maps_uri")
    place_types = results.get("place_types")
    if isinstance(place_types, list):
        result["place_types"] = deepcopy(place_types)
    else:
        result["place_types"] = place_types
    return result


def build_cache_entry(record: dict[str, Any], *, migrated_at: str) -> dict[str, Any]:
    provider_id = provider_id_from_record(record)
    gapi_response = record.get("gapi_response")
    return {
        "id": provider_id,
        "local_id": record.get("local_id", record.get("localid")),
        "is_gapi_called": True,
        "gapi_called_at": migrated_at,
        "gapi_response": deepcopy(gapi_response) if isinstance(gapi_response, dict) else None,
        "result": build_result(record),
    }


def load_providers_by_id(
    output_dir: Path,
    error_dir: Path,
    glob_pattern: str,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    providers: dict[str, dict[str, Any]] = {}
    files_read = 0
    records_seen = 0

    for directory in (error_dir, output_dir):
        for path in list_batch_files(directory, glob_pattern):
            files_read += 1
            try:
                records = load_batch_file(path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                print(f"Warning: skipping {path}: {exc}", file=sys.stderr)
                continue

            for record in records:
                records_seen += 1
                provider_id = provider_id_from_record(record)
                if not provider_id:
                    continue
                providers[provider_id] = record

    return providers, files_read, records_seen


def clear_cache_dir(cache_dir: Path) -> None:
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file() and path.suffix == ".json":
            path.unlink()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.input_dir.resolve()
    error_dir = args.error_dir.resolve()
    cache_dir = args.cache_dir.resolve()

    if not output_dir.is_dir() and not error_dir.is_dir():
        print(
            f"Error: neither {output_dir} nor {error_dir} exists.",
            file=sys.stderr,
        )
        return 1

    providers, files_read, records_seen = load_providers_by_id(
        output_dir,
        error_dir,
        args.glob,
    )
    if not providers:
        print("No providers with id found to migrate.", file=sys.stderr)
        return 1

    if args.clear_cache:
        clear_cache_dir(cache_dir)

    migrated_at = utc_now_iso()
    cache = GapiCache(cache_dir)
    skipped_no_id = records_seen - len(providers)

    entries = {
        provider_id: build_cache_entry(record, migrated_at=migrated_at)
        for provider_id, record in providers.items()
    }
    shard_files = cache.import_entries_bulk(entries)

    try:
        cache.flush()
    except OSError as exc:
        print(f"Error: could not write gapi_cache: {exc}", file=sys.stderr)
        return 1

    print(
        f"Migrated {len(providers)} provider(s) from {files_read} file(s) "
        f"({records_seen} record(s) read) into {cache_dir}"
    )
    if skipped_no_id:
        print(f"Skipped {skipped_no_id} record(s) without id")
    print(f"gapi_cache index: {len(cache)} provider id(s), {shard_files} shard file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
