#!/usr/bin/env python3
"""Remove result objects from p2 GAPI output batch files in place."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
BATCH_GLOB = "batch_*.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove the result object from each restaurant record in "
            "p2_batched_gapi_details/output batch files."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Batch directory to update in place (default: {DEFAULT_INPUT_DIR.name})",
    )
    return parser.parse_args(argv)


def list_batch_files(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    files = sorted(input_dir.glob(BATCH_GLOB))
    if not files:
        raise FileNotFoundError(f"No files matching {BATCH_GLOB} in {input_dir}")
    return files


def load_batch(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return data


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def process_batch_file(batch_path: Path) -> tuple[int, int]:
    providers = load_batch(batch_path)
    removed_count = 0
    scanned_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        scanned_count += 1
        if "result" in provider:
            provider.pop("result", None)
            removed_count += 1
        elif "results" in provider:
            provider.pop("results", None)
            removed_count += 1

    write_json(batch_path, providers)
    return removed_count, scanned_count


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_dir = args.input_dir.resolve()

    try:
        batch_files = list_batch_files(input_dir)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_removed = 0
    total_scanned = 0

    for batch_path in batch_files:
        try:
            removed_count, scanned_count = process_batch_file(batch_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"{batch_path.name}: error: {exc}", file=sys.stderr)
            continue

        total_removed += removed_count
        total_scanned += scanned_count
        print(
            f"{batch_path.name}: removed result from {removed_count} / "
            f"{scanned_count} restaurant(s)"
        )

    print(
        f"\nDone. Removed result from {total_removed} / "
        f"{total_scanned} restaurant(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
