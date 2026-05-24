#!/usr/bin/env python3
"""Run Google Places Text Search for batched provider files."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from call_gapi import DEFAULT_FIELD_MASK, GapiError, search_text

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BATCH_DIR = SCRIPT_DIR / "batched_raw_providers_p1"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "batched_gapi_details_p2/output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "batched_gapi_details_p2/output_errors"
MANIFEST_PATH = SCRIPT_DIR / "batched_gapi_details_p2/gapi_run_mainfest.json"
BATCH_GLOB = "batch_*.json"
CID_URI_TEMPLATE = "https://maps.google.com/?cid={cid}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Call Google Places Text Search for each restaurant in batch files "
            "and write enriched JSON to output/ and output_errors/."
        )
    )
    parser.add_argument(
        "--batch-dir",
        type=Path,
        default=DEFAULT_BATCH_DIR,
        help=f"Input batch directory (default: {DEFAULT_BATCH_DIR.name})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Successful results directory (default: {DEFAULT_OUTPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Failed results directory (default: {DEFAULT_ERROR_DIR.name})",
    )
    parser.add_argument(
        "--field-mask",
        default=DEFAULT_FIELD_MASK,
        help="X-Goog-FieldMask value for Places API",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to wait between API calls (default: 0.2)",
    )
    parser.add_argument(
        "--limit-batches",
        type=int,
        default=None,
        help="Process only the first N batch files (for testing)",
    )
    return parser.parse_args()


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _contains(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    return needle.casefold() in haystack.casefold()


def format_places_text_query(provider: dict) -> str:
    """Build a textQuery string for Google Places Text Search (New)."""
    name = _normalize_whitespace(str(provider.get("name") or ""))
    address = provider.get("address")
    if not isinstance(address, dict):
        address = {}

    street = _normalize_whitespace(str(address.get("street") or ""))
    locality = _normalize_whitespace(str(address.get("locality") or ""))
    city = _normalize_whitespace(str(address.get("city") or ""))
    state = _normalize_whitespace(str(address.get("state") or ""))
    area_code = _normalize_whitespace(str(address.get("area_code") or ""))

    address_parts: list[str] = []
    combined = ""

    def append_if_new(part: str) -> None:
        nonlocal combined
        if not part or _contains(combined, part):
            return
        address_parts.append(part)
        combined = ", ".join(address_parts)

    append_if_new(street)
    append_if_new(locality)
    append_if_new(city)

    state_line = f"{state} {area_code}".strip() if state or area_code else ""
    append_if_new(state_line)
    append_if_new("India")

    if name and address_parts:
        return f"{name}, {', '.join(address_parts)}"
    if name:
        return name
    return ", ".join(address_parts)


def list_batch_files(batch_dir: Path) -> list[Path]:
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"Batch directory not found: {batch_dir}")
    files = sorted(batch_dir.glob(BATCH_GLOB))
    if not files:
        raise FileNotFoundError(
            f"No batch files matching {BATCH_GLOB} in {batch_dir}"
        )
    return files


def load_batch(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def batch_id_from_path(batch_path: Path) -> str:
    return batch_path.stem


def relative_to_script(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(SCRIPT_DIR))
    except ValueError:
        return str(path.resolve())


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
        raise ValueError(f"{path.name}: expected a JSON object")
    if not isinstance(data.get("runs"), list):
        data["runs"] = []
    return data


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    write_json(path, manifest)


def extract_cid_from_google_maps_uri(google_maps_uri: str) -> str | None:
    """Parse cid query param from a Google Maps URI."""
    uri = google_maps_uri.strip()
    if not uri:
        return None

    query = parse_qs(urlparse(uri).query)
    cid_values = query.get("cid")
    if cid_values and cid_values[0]:
        return cid_values[0]

    match = re.search(r"[?&]cid=([^&]+)", uri)
    return match.group(1) if match else None


def build_place_result(place: dict[str, Any]) -> dict[str, str] | None:
    """Build {cid, formatted_google_maps_uri} from a single Places API place object."""
    google_maps_uri = str(place.get("googleMapsUri") or "")
    cid = extract_cid_from_google_maps_uri(google_maps_uri)
    if not cid:
        return None
    return {
        "cid": cid,
        "formatted_google_maps_uri": CID_URI_TEMPLATE.format(cid=cid),
    }


def build_results_from_gapi_response(
    gapi_response: dict[str, Any],
) -> dict[str, str] | None:
    """
    Build results from places[0] only (top Text Search match).

    Returns None when places[0] is missing or has no parseable googleMapsUri/cid.
    """
    places = gapi_response.get("places")
    if not isinstance(places, list) or not places:
        return None

    first_place = places[0]
    if not isinstance(first_place, dict):
        return None

    return build_place_result(first_place)


def process_provider(
    provider: dict,
    *,
    field_mask: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """
    Returns (success_record, error_record).
    Exactly one of the tuple elements will be non-None for a valid provider dict.
    """
    record = deepcopy(provider)
    text_query = format_places_text_query(provider)
    record["text_query"] = text_query

    try:
        gapi_response = search_text(text_query, field_mask=field_mask)
        record["gapi_response"] = gapi_response
        record["results"] = build_results_from_gapi_response(gapi_response)
        return record, None
    except GapiError as exc:
        record["gapi_error"] = exc.to_dict()
        return None, record


def process_batch_file(
    batch_path: Path,
    *,
    output_dir: Path,
    error_dir: Path,
    field_mask: str,
    delay: float,
) -> dict[str, Any]:
    batch_started = utc_now_iso()
    batch_id = batch_id_from_path(batch_path)
    input_file = relative_to_script(batch_path)

    providers = load_batch(batch_path)
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            errors.append(
                {
                    "index": index,
                    "gapi_error": {
                        "message": "Provider entry is not a JSON object",
                        "status_code": None,
                        "response_body": provider,
                    },
                }
            )
            continue

        success, error = process_provider(provider, field_mask=field_mask)
        if success is not None:
            successes.append(success)
        if error is not None:
            errors.append(error)

        if delay > 0 and index < len(providers) - 1:
            time.sleep(delay)

    output_path = output_dir / batch_path.name
    error_path = error_dir / batch_path.name

    if successes:
        write_json(output_path, successes)
    elif output_path.exists():
        output_path.unlink()

    if errors:
        write_json(error_path, errors)
    elif error_path.exists():
        error_path.unlink()

    success_count = len(successes)
    error_count = len(errors)
    if error_count == 0:
        status = "success"
    elif success_count == 0:
        status = "error"
    else:
        status = "partial"

    return {
        "batch_id": batch_id,
        "input_file": input_file,
        "output_file": relative_to_script(output_path) if successes else None,
        "error_file": relative_to_script(error_path) if errors else None,
        "status": status,
        "started_at": batch_started,
        "finished_at": utc_now_iso(),
        "restaurant_count": len(providers),
        "success_count": success_count,
        "error_count": error_count,
    }


def main() -> int:
    args = parse_args()
    batch_dir = args.batch_dir.resolve()
    output_dir = args.output_dir.resolve()
    error_dir = args.error_dir.resolve()

    try:
        batch_files = list_batch_files(batch_dir)
    except (OSError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.limit_batches is not None:
        batch_files = batch_files[: args.limit_batches]

    run_started = utc_now_iso()
    batch_results: list[dict[str, Any]] = []

    for batch_path in batch_files:
        print(f"Processing {batch_path.name} ...")
        try:
            result = process_batch_file(
                batch_path,
                output_dir=output_dir,
                error_dir=error_dir,
                field_mask=args.field_mask,
                delay=args.delay,
            )
        except (OSError, json.JSONDecodeError, ValueError, GapiError) as exc:
            result = {
                "batch_id": batch_id_from_path(batch_path),
                "input_file": relative_to_script(batch_path),
                "output_file": None,
                "error_file": None,
                "status": "error",
                "started_at": utc_now_iso(),
                "finished_at": utc_now_iso(),
                "restaurant_count": 0,
                "success_count": 0,
                "error_count": 0,
                "batch_error": str(exc),
            }
            print(f"  batch failed: {exc}", file=sys.stderr)

        batch_results.append(result)
        print(
            f"  {result['status']}: "
            f"{result['success_count']} ok, {result['error_count']} error(s)"
        )

    successful_batches = sum(1 for b in batch_results if b["status"] == "success")
    error_batches = len(batch_results) - successful_batches

    run_record = {
        "run_id": run_started,
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "total_batches_count": len(batch_results),
        "successful_batches_count": successful_batches,
        "error_batches_count": error_batches,
        "batches": batch_results,
    }

    try:
        manifest = load_manifest(MANIFEST_PATH)
        manifest["runs"].append(run_record)
        save_manifest(MANIFEST_PATH, manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Warning: could not update manifest: {exc}", file=sys.stderr)

    print(
        f"\nRun complete: {successful_batches}/{len(batch_results)} batch(es) fully "
        f"successful. Manifest: {MANIFEST_PATH.name}"
    )
    return 0 if error_batches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
