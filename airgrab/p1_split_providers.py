#!/usr/bin/env python3
"""Split raw provider details into numbered batch JSON files."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "raw_input" / "raw_provider_details.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "batched_raw_providers_p1"
DEFAULT_BATCH_SIZE = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split raw_provider_details.json into numbered batch files."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of restaurants per batch file (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input JSON file (default: {DEFAULT_INPUT.relative_to(SCRIPT_DIR)})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            f"Directory for batch files "
            f"(default: {DEFAULT_OUTPUT_DIR.relative_to(SCRIPT_DIR)})"
        ),
    )
    return parser.parse_args()


def load_providers(input_path: Path) -> list[dict]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with input_path.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Input JSON must be a top-level array of provider objects")

    return data


def batch_padding_width(total_items: int, batch_size: int) -> int:
    batch_count = max(1, math.ceil(total_items / batch_size)) if total_items else 1
    return max(3, len(str(batch_count)))


def write_batches(
    providers: list[dict],
    output_dir: Path,
    batch_size: int,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    if not providers:
        print("No providers in input; nothing to write.")
        return []

    pad = batch_padding_width(len(providers), batch_size)
    written: list[Path] = []

    for batch_index, start in enumerate(range(0, len(providers), batch_size), start=1):
        chunk = providers[start : start + batch_size]
        output_path = output_dir / f"batch_{batch_index:0{pad}d}.json"
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(chunk, f, indent=2, ensure_ascii=False)
            f.write("\n")
        written.append(output_path)

    return written


def main() -> int:
    args = parse_args()

    if args.batch_size < 1:
        print("Error: --batch-size must be at least 1", file=sys.stderr)
        return 1

    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()

    try:
        providers = load_providers(input_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    written = write_batches(providers, output_dir, args.batch_size)

    print(
        f"Split {len(providers)} provider(s) from {input_path.name} "
        f"into {len(written)} batch file(s) "
        f"({args.batch_size} per batch) in {output_dir}"
    )
    for path in written:
        print(f"  - {path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
