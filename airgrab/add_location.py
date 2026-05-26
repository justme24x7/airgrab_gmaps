#!/usr/bin/env python3
"""Add address.gps to GAPI output by matching ids from raw input."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RAW_INPUT = SCRIPT_DIR / "raw_input" / "raw_provider_details.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
BATCH_GLOB = "batch_*.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich each restaurant JSON in p2_batched_gapi_details/output with "
            "address.gps copied from raw_input/raw_provider_details.json."
        )
    )
    parser.add_argument(
        "--raw-input",
        type=Path,
        default=DEFAULT_RAW_INPUT,
        help=f"Raw provider details JSON (default: {DEFAULT_RAW_INPUT.relative_to(SCRIPT_DIR)})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"GAPI output directory to update in place (default: {DEFAULT_OUTPUT_DIR.relative_to(SCRIPT_DIR)})",
    )
    return parser.parse_args()


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return data


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_batch_files(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        raise FileNotFoundError(f"Output directory not found: {output_dir}")
    batch_files = sorted(output_dir.glob(BATCH_GLOB))
    if not batch_files:
        raise FileNotFoundError(f"No files matching {BATCH_GLOB} in {output_dir}")
    return batch_files


def build_location_lookup(raw_providers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for provider in raw_providers:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        address = provider.get("address")
        if not provider_id or not isinstance(address, dict):
            continue
        gps = address.get("gps")
        if isinstance(gps, dict):
            lookup[str(provider_id)] = deepcopy(gps)
    return lookup


def enrich_batch_file(
    batch_path: Path,
    location_lookup: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    providers = load_json_array(batch_path)
    updated_count = 0
    missing_location_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue

        provider_id = str(provider.get("id") or "").strip()
        address = provider.get("address")
        location = location_lookup.get(provider_id)

        if not provider_id or not isinstance(address, dict) or location is None:
            missing_location_count += 1
            continue

        address["gps"] = deepcopy(location)
        updated_count += 1

    write_json(batch_path, providers)
    return updated_count, missing_location_count


def main() -> int:
    args = parse_args()
    raw_input = args.raw_input.resolve()
    output_dir = args.output_dir.resolve()

    try:
        raw_providers = load_json_array(raw_input)
        location_lookup = build_location_lookup(raw_providers)
        batch_files = list_batch_files(output_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_updated = 0
    total_missing_location = 0

    for batch_path in batch_files:
        updated_count, missing_location_count = enrich_batch_file(
            batch_path,
            location_lookup,
        )
        total_updated += updated_count
        total_missing_location += missing_location_count
        print(
            f"{batch_path.name}: updated {updated_count}, "
            f"missing_location {missing_location_count}"
        )

    print(
        f"\nDone. Updated {total_updated} restaurant(s); "
        f"missing location for {total_missing_location} restaurant(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
