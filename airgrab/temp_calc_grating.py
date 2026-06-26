#!/usr/bin/env python3
"""Print provider and valid-rating counts from p4 batch outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output_errors"
BATCH_GLOB = "*.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Count providers and valid ratings across p4 batch output files."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"p4 successful output directory (default: {DEFAULT_OUTPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"p4 error output directory (default: {DEFAULT_ERROR_DIR.name})",
    )
    parser.add_argument(
        "--glob",
        default=BATCH_GLOB,
        help=f"Batch file glob (default: {BATCH_GLOB})",
    )
    return parser.parse_args(argv)


def list_batch_files(directory: Path, glob_pattern: str) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob(glob_pattern)
        if path.is_file() and path.name != ".DS_Store"
    )


def load_batch_file(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return [item for item in data if isinstance(item, dict)]


def provider_id_from_record(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def parse_rating(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rating = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if 0 <= rating <= 5:
        return rating
    return None


def has_valid_rating(record: dict[str, Any]) -> bool:
    results = record.get("results")
    if not isinstance(results, dict):
        return False
    return parse_rating(results.get("rating")) is not None


def collect_unique_providers(
    output_files: dict[str, Path],
    error_files: dict[str, Path],
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []

    for files in (error_files, output_files):
        for path in files.values():
            for record in load_batch_file(path):
                provider_id = provider_id_from_record(record)
                if provider_id:
                    unique[provider_id] = record
                else:
                    unkeyed.append(record)

    return list(unique.values()) + unkeyed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.resolve()
    error_dir = args.error_dir.resolve()

    if not output_dir.is_dir() and not error_dir.is_dir():
        print(
            f"Error: neither {output_dir} nor {error_dir} exists.",
            file=sys.stderr,
        )
        return 1

    output_files = {path.name: path for path in list_batch_files(output_dir, args.glob)}
    error_files = {path.name: path for path in list_batch_files(error_dir, args.glob)}

    if not output_files and not error_files:
        print("No batch files found.", file=sys.stderr)
        return 1

    try:
        providers = collect_unique_providers(output_files, error_files)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    with_valid_rating = sum(1 for provider in providers if has_valid_rating(provider))

    print(f"Files scanned (output): {len(output_files)}")
    print(f"Files scanned (output_errors): {len(error_files)}")
    print(f"Total providers: {len(providers)}")
    print(f"Providers with valid rating: {with_valid_rating}")
    print(f"Providers without valid rating: {len(providers) - with_valid_rating}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
