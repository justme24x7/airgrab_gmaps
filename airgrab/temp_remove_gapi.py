#!/usr/bin/env python3
"""One-off: remove gapi_response from p4 batch files in place."""

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
            "Remove gapi_response from each provider record in p4 batch files "
            "(output/ and output_errors/)."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Successful p4 output directory (default: {DEFAULT_OUTPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Failed p4 output directory (default: {DEFAULT_ERROR_DIR.name})",
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


def load_batch(path: Path) -> list[Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return data


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def strip_gapi_response(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if "gapi_response" not in record:
        return False
    record.pop("gapi_response", None)
    return True


def process_batch_file(batch_path: Path) -> tuple[int, int]:
    providers = load_batch(batch_path)
    removed_count = 0
    scanned_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        scanned_count += 1
        if strip_gapi_response(provider):
            removed_count += 1

    write_json(batch_path, providers)
    return removed_count, scanned_count


def process_directory(directory: Path, glob_pattern: str, label: str) -> tuple[int, int, int]:
    batch_files = list_batch_files(directory, glob_pattern)
    if not batch_files:
        print(f"{label}: no files found in {directory}")
        return 0, 0, 0

    total_removed = 0
    total_scanned = 0

    for batch_path in batch_files:
        try:
            removed_count, scanned_count = process_batch_file(batch_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"{label}/{batch_path.name}: error: {exc}", file=sys.stderr)
            continue

        total_removed += removed_count
        total_scanned += scanned_count
        print(
            f"{label}/{batch_path.name}: removed gapi_response from "
            f"{removed_count} / {scanned_count} provider(s)"
        )

    return len(batch_files), total_removed, total_scanned


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

    file_count = 0
    total_removed = 0
    total_scanned = 0

    for directory, label in ((output_dir, "output"), (error_dir, "output_errors")):
        files, removed, scanned = process_directory(directory, args.glob, label)
        file_count += files
        total_removed += removed
        total_scanned += scanned

    if file_count == 0:
        print("No batch files processed.", file=sys.stderr)
        return 1

    print(
        f"\nDone. Processed {file_count} file(s). "
        f"Removed gapi_response from {total_removed} / {total_scanned} provider(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
