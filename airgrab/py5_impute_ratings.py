#!/usr/bin/env python3
"""Impute missing provider ratings from same-name peers in p4 outputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output_errors"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p5_imputed_ratings" / "output"
DEFAULT_OUTPUT_ERROR_DIR = SCRIPT_DIR / "p5_imputed_ratings" / "output_errors"
MANIFEST_PATH = SCRIPT_DIR / "p5_imputed_ratings" / "imputed_ratings_run_manifest.json"
BATCH_GLOB = "*.json"
RATING_TYPE_GOOGLE = "GOOGLE"
RATING_TYPE_IMPUTED = "IMPUTED"
RATING_TYPE_NA = "NA"
RATING_TYPE_MANUAL = "MANUAL"
MINIMUM_PEERS_TO_IMPUTE_RATING = 2
GROUP_BY_CITY = False


@dataclass(frozen=True)
class PeerRating:
    provider_id: str
    rating: float
    total_reviews: float | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read p4 batch outputs, impute missing results.rating from peers with "
            "the same name (and city when GROUP_BY_CITY is enabled), merge each batch from output/ and "
            "output_errors/ into p5_imputed_ratings/output, and write imputation "
            "failures only to p5_imputed_ratings/output_errors."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"p4 successful output directory (default: {DEFAULT_INPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"p4 error output directory (default: {DEFAULT_ERROR_DIR.name})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Imputed output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--output-error-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ERROR_DIR,
        help=f"Imputed error directory (default: {DEFAULT_OUTPUT_ERROR_DIR})",
    )
    parser.add_argument(
        "--glob",
        default=BATCH_GLOB,
        help=f"Batch file glob (default: {BATCH_GLOB})",
    )
    return parser.parse_args(argv)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


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


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def provider_id_from_record(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def group_key(record: dict[str, Any]) -> tuple[str, str]:
    name = normalize_text(record.get("name"))
    if not GROUP_BY_CITY:
        return name, ""
    city = ""
    address = record.get("address")
    if isinstance(address, dict):
        city = normalize_text(address.get("city"))
    return name, city


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


def parse_total_reviews(value: Any) -> float | None:
    if value is None:
        return None
    try:
        count = float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    if count >= 0:
        return count
    return None


def has_rating(record: dict[str, Any]) -> bool:
    results = record.get("results")
    if not isinstance(results, dict):
        return False
    return parse_rating(results.get("rating")) is not None


def is_manual_rating_record(record: dict[str, Any]) -> bool:
    results = record.get("results")
    return (
        isinstance(results, dict)
        and str(results.get("rating_type") or "").strip().upper() == RATING_TYPE_MANUAL
    )


def ensure_results(record: dict[str, Any]) -> dict[str, Any]:
    results = record.get("results")
    if not isinstance(results, dict):
        results = {}
        record["results"] = results
    return results


def build_peer_index(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], list[PeerRating]]:
    index: dict[tuple[str, str], list[PeerRating]] = defaultdict(list)

    for record in records:
        provider_id = provider_id_from_record(record)
        if not provider_id:
            continue

        results = record.get("results")
        if not isinstance(results, dict):
            continue

        rating = parse_rating(results.get("rating"))
        if rating is None:
            continue

        name, city = group_key(record)
        if not name:
            continue

        index[(name, city)].append(
            PeerRating(
                provider_id=provider_id,
                rating=rating,
                total_reviews=parse_total_reviews(results.get("total_reviews")),
            )
        )

    return dict(index)


def impute_record(
    record: dict[str, Any],
    peer_index: dict[tuple[str, str], list[PeerRating]],
    *,
    min_peers: int,
) -> str:
    results = ensure_results(record)
    provider_id = provider_id_from_record(record)

    if is_manual_rating_record(record):
        results["rating_type"] = RATING_TYPE_MANUAL
        return RATING_TYPE_MANUAL

    if has_rating(record):
        results["rating_type"] = RATING_TYPE_GOOGLE
        return RATING_TYPE_GOOGLE

    peers = peer_index.get(group_key(record), [])
    rated_peers = [peer for peer in peers if peer.provider_id != provider_id]

    if len(rated_peers) < min_peers:
        results["rating_type"] = RATING_TYPE_NA
        return RATING_TYPE_NA

    ratings = [peer.rating for peer in rated_peers]
    review_counts = [
        peer.total_reviews
        for peer in rated_peers
        if peer.total_reviews is not None
    ]

    results["rating"] = round(sum(ratings) / len(ratings), 2)
    if review_counts:
        results["total_reviews"] = int(round(sum(review_counts) / len(review_counts)))
    results["rating_type"] = RATING_TYPE_IMPUTED
    return RATING_TYPE_IMPUTED


def list_input_batch_names(
    output_files: dict[str, Path],
    error_files: dict[str, Path],
) -> list[str]:
    return sorted(set(output_files) | set(error_files))


def merge_batch_records(
    batch_name: str,
    output_files: dict[str, Path],
    error_files: dict[str, Path],
) -> tuple[list[dict[str, Any]], list[str]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    sources: list[str] = []

    for source, files in (("output", output_files), ("output_errors", error_files)):
        batch_path = files.get(batch_name)
        if batch_path is None:
            continue
        sources.append(source)
        for record in load_batch_file(batch_path):
            provider_id = provider_id_from_record(record)
            if provider_id:
                if provider_id in seen_ids:
                    continue
                seen_ids.add(provider_id)
            merged.append(record)

    return merged, sources


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


def process_merged_batch(
    batch_name: str,
    records: list[dict[str, Any]],
    *,
    input_sources: list[str],
    peer_index: dict[tuple[str, str], list[PeerRating]],
    min_peers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    google_count = 0
    imputed_count = 0
    na_count = 0
    manual_count = 0

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(
                {
                    "index": index,
                    "impute_error": {
                        "message": "Provider entry is not a JSON object",
                        "details": record,
                    },
                }
            )
            continue

        try:
            copy = deepcopy(record)
            rating_type = impute_record(copy, peer_index, min_peers=min_peers)
        except Exception as exc:  # noqa: BLE001
            failed = deepcopy(record)
            failed["impute_error"] = {"message": str(exc)}
            errors.append(failed)
            continue

        if rating_type == RATING_TYPE_IMPUTED:
            imputed_count += 1
        elif rating_type == RATING_TYPE_GOOGLE:
            google_count += 1
        elif rating_type == RATING_TYPE_MANUAL:
            manual_count += 1
        else:
            na_count += 1
        successes.append(copy)

    batch_stats = {
        "batch_file": batch_name,
        "input_sources": input_sources,
        "provider_count": len(records),
        "success_count": len(successes),
        "impute_error_count": len(errors),
        "rating_type_google_count": google_count,
        "rating_type_imputed_count": imputed_count,
        "rating_type_manual_count": manual_count,
        "rating_type_na_count": na_count,
    }
    return successes, errors, batch_stats


def _empty_manifest() -> dict[str, Any]:
    return {"runs": []}


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_manifest()
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return _empty_manifest()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()
    if not isinstance(data.get("runs"), list):
        data["runs"] = []
    return data


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    write_json(path, manifest)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if MINIMUM_PEERS_TO_IMPUTE_RATING < 1:
        print(
            "Error: MINIMUM_PEERS_TO_IMPUTE_RATING must be at least 1",
            file=sys.stderr,
        )
        return 1

    input_dir = args.input_dir.resolve()
    error_dir = args.error_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_error_dir = args.output_error_dir.resolve()

    if not input_dir.is_dir() and not error_dir.is_dir():
        print(
            f"Error: neither {input_dir} nor {error_dir} exists.",
            file=sys.stderr,
        )
        return 1

    run_started = utc_now_iso()

    output_files = {path.name: path for path in list_batch_files(input_dir, args.glob)}
    error_files = {path.name: path for path in list_batch_files(error_dir, args.glob)}
    batch_names = list_input_batch_names(output_files, error_files)

    if not batch_names:
        print("No batch files found to process.", file=sys.stderr)
        return 1

    try:
        unique_providers = collect_unique_providers(output_files, error_files)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error loading input: {exc}", file=sys.stderr)
        return 1

    if not unique_providers:
        print("No provider records found to process.", file=sys.stderr)
        return 1

    peer_index = build_peer_index(unique_providers)
    group_label = "name/city" if GROUP_BY_CITY else "name"
    print(
        f"Built peer index with {len(peer_index)} {group_label} group(s) "
        f"from {len(unique_providers)} unique provider record(s)"
    )

    batch_results: list[dict[str, Any]] = []
    total_google = 0
    total_imputed = 0
    total_manual = 0
    total_na = 0
    total_impute_errors = 0
    missing_rating_after = 0

    for batch_name in batch_names:
        sources = [
            source
            for source, files in (
                ("output", output_files),
                ("output_errors", error_files),
            )
            if batch_name in files
        ]
        print(f"Processing {batch_name} from {', '.join(sources)} ...")
        try:
            records, input_sources = merge_batch_records(
                batch_name,
                output_files,
                error_files,
            )
            successes, errors, batch_stats = process_merged_batch(
                batch_name,
                records,
                input_sources=input_sources,
                peer_index=peer_index,
                min_peers=MINIMUM_PEERS_TO_IMPUTE_RATING,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"  batch failed: {exc}", file=sys.stderr)
            batch_results.append(
                {
                    "batch_file": batch_name,
                    "input_sources": sources,
                    "status": "error",
                    "error": str(exc),
                }
            )
            write_json(
                output_error_dir / batch_name,
                [{"batch_error": str(exc), "input_sources": sources}],
            )
            continue

        if successes:
            write_json(output_dir / batch_name, successes)
        elif (output_dir / batch_name).exists():
            (output_dir / batch_name).unlink()

        if errors:
            write_json(output_error_dir / batch_name, errors)
        elif (output_error_dir / batch_name).exists():
            (output_error_dir / batch_name).unlink()

        batch_stats["status"] = "success"
        batch_results.append(batch_stats)

        total_google += batch_stats["rating_type_google_count"]
        total_imputed += batch_stats["rating_type_imputed_count"]
        total_manual += batch_stats["rating_type_manual_count"]
        total_na += batch_stats["rating_type_na_count"]
        total_impute_errors += batch_stats["impute_error_count"]
        for record in successes:
            results = record.get("results")
            if not isinstance(results, dict):
                missing_rating_after += 1
                continue
            if parse_rating(results.get("rating")) is None:
                missing_rating_after += 1

        print(
            f"  {batch_stats['rating_type_imputed_count']} imputed, "
            f"{batch_stats['rating_type_google_count']} google, "
            f"{batch_stats['rating_type_manual_count']} manual, "
            f"{batch_stats['rating_type_na_count']} na, "
            f"{batch_stats['impute_error_count']} impute error(s)"
        )

    run_record = {
        "run_id": run_started,
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "min_peers": MINIMUM_PEERS_TO_IMPUTE_RATING,
        "group_by_city": GROUP_BY_CITY,
        "input_dir": str(input_dir),
        "error_dir": str(error_dir),
        "output_dir": str(output_dir),
        "output_error_dir": str(output_error_dir),
        "total_providers_count": len(unique_providers),
        "rating_type_google_count": total_google,
        "rating_type_imputed_count": total_imputed,
        "rating_type_manual_count": total_manual,
        "rating_type_na_count": total_na,
        "impute_error_count": total_impute_errors,
        "missing_rating_after_count": missing_rating_after,
        "peer_group_count": len(peer_index),
        "total_merged_batch_files": len(batch_names),
        "total_input_output_files": len(output_files),
        "total_input_error_files": len(error_files),
        "batches": batch_results,
    }

    try:
        manifest = load_manifest(MANIFEST_PATH)
        manifest["runs"].append(run_record)
        save_manifest(MANIFEST_PATH, manifest)
    except OSError as exc:
        print(f"Warning: could not update manifest: {exc}", file=sys.stderr)

    print(
        f"\nDone. {total_imputed} IMPUTED, {total_google} GOOGLE, "
        f"{total_manual} MANUAL, {total_na} NA, "
        f"{total_impute_errors} impute error(s), "
        f"{missing_rating_after} still missing rating. "
        f"Manifest: {MANIFEST_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
