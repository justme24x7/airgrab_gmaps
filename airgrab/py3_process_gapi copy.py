#!/usr/bin/env python3
"""Process raw GAPI output into final results objects."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p3_batched_processed_gapi_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p3_batched_processed_gapi_details" / "output_errors"
MANIFEST_PATH = SCRIPT_DIR / "p3_batched_processed_gapi_details" / "processed_gapi_run_mainfest.json"
PROCESSED_DICT_PATH = SCRIPT_DIR / "p3_batched_processed_gapi_details" / "gapi_processed_dict.json"
BATCH_GLOB = "batch_*.json"
CID_URI_TEMPLATE = "https://maps.google.com/?cid={cid}"
MAX_PLACES_FOR_UNIQUE_MATCH = 5
MAX_DISTANCE_FOR_UNIQUE_MATCH = 200.0
EXCLUDED_PLACE_TYPES = frozenset(
    {
        "restaurant",
        "point_of_interest",
        "food",
        "food_store",
        "store",
        "establishment",
        "service"
    }
)


class ProcessGapiError(Exception):
    """Raised when a raw GAPI record cannot be processed."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "details": self.details}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read raw GAPI batch files, build results objects, and write "
            "processed output/ and output_errors/."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Input directory (default: {DEFAULT_INPUT_DIR.name})",
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
        "--limit-batches",
        type=int,
        default=None,
        help="Process only the first N batch files (for testing)",
    )
    return parser.parse_args(argv)


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


def batch_number_from_path(batch_path: Path) -> int:
    match = re.search(r"(\d+)$", batch_path.stem)
    return int(match.group(1)) if match else 0


def relative_to_script(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(SCRIPT_DIR))
    except ValueError:
        return str(path.resolve())


def _empty_manifest() -> dict[str, Any]:
    return {"runs": []}


def load_processed_lookup(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(provider_id): entry
        for provider_id, entry in data.items()
        if isinstance(entry, dict)
    }


def is_manually_verified_result(result: Any) -> bool:
    return isinstance(result, dict) and result.get("is_manually_verified") is True


def is_manually_verified_entry(entry: dict[str, Any]) -> bool:
    return is_manually_verified_result(entry.get("result"))


def has_successful_gapi_response(provider: dict[str, Any]) -> bool:
    if provider.get("gapi_error"):
        return False
    gapi_response = provider.get("gapi_response")
    if not isinstance(gapi_response, dict):
        return False
    return "places" in gapi_response


def should_mark_gapi_called(
    provider_id: str,
    provider: dict[str, Any],
    processed_dict_ids: set[str],
) -> bool:
    if provider_id and provider_id in processed_dict_ids:
        return True
    return has_successful_gapi_response(provider)


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


def build_processed_lookup_entry(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    provider_id = str(record.get("id") or "").strip()
    if not provider_id:
        return None

    entry = {
        "id": provider_id,
        "local_id": record.get("local_id", record.get("localid")),
        "gapi_response": record.get("gapi_response"),
        "result": record.get("results"),
    }
    return provider_id, entry


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def address_gps(provider: dict[str, Any]) -> tuple[float | None, float | None]:
    address = provider.get("address")
    if not isinstance(address, dict):
        return None, None

    gps = address.get("gps")
    if not isinstance(gps, dict):
        return None, None

    latitude = _to_float(gps.get("lat"))
    longitude = _to_float(gps.get("long"))
    return latitude, longitude


def place_location(place: dict[str, Any]) -> tuple[float | None, float | None]:
    location = place.get("location")
    if not isinstance(location, dict):
        return None, None

    latitude = _to_float(location.get("latitude"))
    longitude = _to_float(location.get("longitude"))
    return latitude, longitude


def crow_fly_distance_meters(
    start_lat: float | None,
    start_lng: float | None,
    end_lat: float | None,
    end_lng: float | None,
) -> float | None:
    if None in (start_lat, start_lng, end_lat, end_lng):
        return None

    earth_radius_m = 6_371_000.0
    lat1 = math.radians(start_lat)
    lng1 = math.radians(start_lng)
    lat2 = math.radians(end_lat)
    lng2 = math.radians(end_lng)

    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_m * c


def extract_cid_from_google_maps_uri(google_maps_uri: str) -> str | None:
    uri = google_maps_uri.strip()
    if not uri:
        return None

    query = parse_qs(urlparse(uri).query)
    cid_values = query.get("cid")
    if cid_values and cid_values[0]:
        return cid_values[0]

    match = re.search(r"[?&]cid=([^&]+)", uri)
    return match.group(1) if match else None


def empty_place_result() -> dict[str, Any]:
    return {"cid": "", "formatted_google_maps_uri": ""}


def format_place_types(place: dict[str, Any]) -> list[str]:
    types = place.get("types")
    if not isinstance(types, list):
        return []
    return [
        str(place_type).strip()
        for place_type in types
        if place_type is not None
        and str(place_type).strip()
        and str(place_type).strip() not in EXCLUDED_PLACE_TYPES
    ]


def build_place_result(place: dict[str, Any]) -> dict[str, Any] | None:
    google_maps_uri = str(place.get("googleMapsUri") or "")
    cid = extract_cid_from_google_maps_uri(google_maps_uri)
    if not cid:
        return None
    result: dict[str, Any] = {
        "cid": cid,
        "formatted_google_maps_uri": CID_URI_TEMPLATE.format(cid=cid),
        "place_types": format_place_types(place),
    }
    return result


def finalize_results(
    results: dict[str, Any] | None,
    *,
    is_gapi_called: bool,
) -> dict[str, Any] | None:
    if not is_gapi_called and results is None:
        return None
    finalized = dict(results) if results else {}
    if is_gapi_called:
        finalized["is_gapi_called"] = True
    return finalized or None


def build_results_from_gapi_response(
    provider: dict[str, Any],
    gapi_response: dict[str, Any],
    *,
    max_places_for_match: int = MAX_PLACES_FOR_UNIQUE_MATCH,
    max_distance_for_unique_match: float = MAX_DISTANCE_FOR_UNIQUE_MATCH,
) -> dict[str, Any] | None:
    places = gapi_response.get("places")
    if not isinstance(places, list) or not places:
        return None
    if len(places) > max_places_for_match:
        return empty_place_result()

    first_place = places[0]
    if not isinstance(first_place, dict):
        return None

    source_lat, source_lng = address_gps(provider)
    place_lat, place_lng = place_location(first_place)
    distance_meters = crow_fly_distance_meters(
        source_lat,
        source_lng,
        place_lat,
        place_lng,
    )
    if distance_meters is None or distance_meters > max_distance_for_unique_match:
        return None

    return build_place_result(first_place)


def process_provider(
    provider: dict,
    *,
    processed_lookup: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    record = deepcopy(provider)
    provider_id = str(record.get("id") or "").strip()
    cached_entry = processed_lookup.get(provider_id) if provider_id else None
    if cached_entry and is_manually_verified_entry(cached_entry):
        cached_result = cached_entry.get("result")
        if isinstance(cached_result, dict):
            record["results"] = deepcopy(cached_result)
            return record, None

    processed_dict_ids = set(processed_lookup)
    gapi_response = record.get("gapi_response")
    if not isinstance(gapi_response, dict):
        record["process_error"] = ProcessGapiError(
            "Missing or invalid gapi_response",
            details=gapi_response,
        ).to_dict()
        return None, record

    try:
        is_gapi_called = should_mark_gapi_called(
            provider_id,
            record,
            processed_dict_ids,
        )

        places = gapi_response.get("places")
        distance_meters: float | None = None
        if isinstance(places, list) and places and isinstance(places[0], dict):
            source_lat, source_lng = address_gps(record)
            place_lat, place_lng = place_location(places[0])
            distance_meters = crow_fly_distance_meters(
                source_lat,
                source_lng,
                place_lat,
                place_lng,
            )
        print(f"    crow_fly_distance_meters: {distance_meters}")

        results = build_results_from_gapi_response(record, gapi_response)
        results = finalize_results(results, is_gapi_called=is_gapi_called)
        if results is not None:
            record["results"] = results
        return record, None
    except Exception as exc:  # noqa: BLE001
        record["process_error"] = ProcessGapiError(str(exc)).to_dict()
        return None, record


def process_batch_file(
    batch_path: Path,
    *,
    output_dir: Path,
    error_dir: Path,
    processed_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    batch_started = utc_now_iso()
    batch_id = batch_id_from_path(batch_path)
    batch_number = batch_number_from_path(batch_path)
    input_file = relative_to_script(batch_path)

    providers = load_batch(batch_path)
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            errors.append(
                {
                    "index": index,
                    "process_error": {
                        "message": "Provider entry is not a JSON object",
                        "details": provider,
                    },
                }
            )
            continue

        provider_name = provider.get("name", "<unnamed>")
        print(
            f"  Processing batch {batch_number} provider: {provider_name} "
            f"at index: {index}"
        )
        success, error = process_provider(
            provider,
            processed_lookup=processed_lookup,
        )
        if success is not None:
            successes.append(success)
        if error is not None:
            errors.append(error)

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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    error_dir = args.error_dir.resolve()

    try:
        batch_files = list_batch_files(input_dir)
    except (OSError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.limit_batches is not None:
        batch_files = batch_files[: args.limit_batches]

    run_started = utc_now_iso()
    batch_results: list[dict[str, Any]] = []
    run_processed_lookup: dict[str, dict[str, Any]] = {}
    existing_processed_lookup = load_processed_lookup(PROCESSED_DICT_PATH)
    manually_verified_count = sum(
        1 for entry in existing_processed_lookup.values() if is_manually_verified_entry(entry)
    )
    if existing_processed_lookup:
        print(
            f"Loaded {len(existing_processed_lookup)} id(s) from {PROCESSED_DICT_PATH.name} "
            "for is_gapi_called"
        )
    if manually_verified_count:
        print(
            f"Preserving {manually_verified_count} manually verified result(s) "
            f"from {PROCESSED_DICT_PATH.name}"
        )

    for batch_path in batch_files:
        print(f"Processing {batch_path.name} ...")
        try:
            result = process_batch_file(
                batch_path,
                output_dir=output_dir,
                error_dir=error_dir,
                processed_lookup=existing_processed_lookup,
            )
        except (OSError, json.JSONDecodeError, ValueError, ProcessGapiError) as exc:
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
        if result.get("output_file"):
            output_batch_path = output_dir / batch_path.name
            try:
                processed_records = load_batch(output_batch_path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                print(
                    f"  warning: could not read processed output for dictionary: {exc}",
                    file=sys.stderr,
                )
            else:
                for record in processed_records:
                    if not isinstance(record, dict):
                        continue
                    lookup_entry = build_processed_lookup_entry(record)
                    if lookup_entry is None:
                        continue
                    provider_id, entry = lookup_entry
                    run_processed_lookup[provider_id] = entry
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
    except OSError as exc:
        print(f"Warning: could not update manifest: {exc}", file=sys.stderr)

    try:
        merged_lookup = dict(existing_processed_lookup)
        for provider_id, entry in run_processed_lookup.items():
            existing_entry = merged_lookup.get(provider_id)
            if existing_entry and is_manually_verified_entry(existing_entry):
                continue
            merged_lookup[provider_id] = entry
        write_json(PROCESSED_DICT_PATH, merged_lookup)
    except OSError as exc:
        print(f"Warning: could not write processed dictionary: {exc}", file=sys.stderr)

    print(
        f"\nRun complete: {successful_batches}/{len(batch_results)} batch(es) fully "
        f"successful. Manifest: {MANIFEST_PATH.name}"
    )
    return 0 if error_batches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
