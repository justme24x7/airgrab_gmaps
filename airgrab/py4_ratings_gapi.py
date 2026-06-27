#!/usr/bin/env python3
"""Pipeline step 4 (GAPI): fetch ratings via Google Places Place Details.

What it does:
  Reads ``p3_batched_processed_gapi_details/output``, calls Places Place Details
  for each provider using ``result.place_id``, and writes to
  ``p4_batched_scraper_details/output`` (same layout as the Selenium scraper).

Overall logic:
  - Require ``result.place_id`` and a formatted Google Maps URI.
  - Call Place Details (New) with ``result.place_id``.
  - Merge ``result.rating``, ``result.total_reviews``, ``result.is_gmaps_checked``.
  - Update ``gapi_cache`` with rating-GAPI flags and synced ``result``; flush at end.
  - Print providers that are permanently closed, temporarily closed, or missing
    rating/review counts.

Skip GAPI call (return record unchanged):
  - ``result.rating_type`` == ``"MANUAL"`` on the provider record
  - Missing ``result.place_id`` or formatted Google Maps URI
  - ``gapi_cache`` entry top-level ``is_permanently_closed`` == ``true`` → set
    ``record.is_permanently_closed`` and skip
  - ``gapi_cache`` entry top-level ``is_manually_blocked`` == ``true`` → set
    ``record.is_manually_blocked`` and skip
  - ``gapi_cache`` entry top-level ``skip_rating_gapi`` == ``true`` → set
    ``record.skip_rating_gapi`` and skip

Side effects after a successful Place Details call:
  - Set ``gapi_cache.is_rating_gapi_called`` == ``true``
  - On permanently or temporarily closed: ``gapi_cache.is_permanently_closed`` == ``true``
  - On no rating/reviews: ``gapi_cache.skip_rating_gapi`` == ``true`` with reason
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gapi_cache import (
    GapiCache,
    SKIP_RATING_GAPI_REASON_NO_RATINGS,
    is_manually_blocked_entry,
    is_permanently_closed_entry,
    is_skip_rating_gapi_entry,
    utc_now_iso,
)
from py2_gapi import GapiError, get_api_key
from run_manifest import append_run_to_manifest, load_manifest, save_manifest

PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/"
DEFAULT_RATINGS_DETAILS_FIELD_MASK = (
    "id,rating,userRatingCount,businessStatus,googleMapsUri"
)

DEFAULT_INPUT_DIR = SCRIPT_DIR / "p3_batched_processed_gapi_details" / "output"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p4_batched_scraper_details" / "output_errors"
MANIFEST_PATH = SCRIPT_DIR / "p4_batched_scraper_details" / "ratings_gapi_run_mainfest.json"
GAPI_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
BATCH_GLOB = "batch_*.json"
API_DELAY_SECONDS = 0.2
RATING_TYPE_MANUAL = "MANUAL"

BUSINESS_STATUS_PERMANENTLY_CLOSED = "CLOSED_PERMANENTLY"
BUSINESS_STATUS_TEMPORARILY_CLOSED = "CLOSED_TEMPORARILY"


def get_place_details(
    place_id: str,
    *,
    api_key: str | None = None,
    field_mask: str = DEFAULT_RATINGS_DETAILS_FIELD_MASK,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch a single place by Place ID (Places API Place Details New)."""
    place_id = str(place_id or "").strip()
    if place_id.startswith("places/"):
        place_id = place_id.removeprefix("places/").strip()
    if not place_id:
        raise GapiError("place_id must not be empty")

    key = api_key or get_api_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": str(field_mask).replace(" ", ""),
    }
    url = f"{PLACES_DETAILS_URL}{quote(place_id, safe='')}"
    request = urllib_request.Request(url, headers=headers, method="GET")

    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body_bytes = response.read()
            body_text = body_bytes.decode("utf-8")
            return json.loads(body_text)
    except urllib_error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(body_text)
        except json.JSONDecodeError:
            body = body_text
        raise GapiError(
            f"Places API error ({exc.code})",
            status_code=exc.code,
            response_body=body,
        ) from exc
    except urllib_error.URLError as exc:
        raise GapiError(f"HTTP request failed: {exc}") from exc


class RatingsGapiError(Exception):
    """Raised when ratings cannot be fetched or parsed for a provider."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "details": self.details}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch result.rating and result.total_reviews from Google Places "
            "Place Details for each provider in p3 batch output files."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"p3 processed output directory (default: {DEFAULT_INPUT_DIR.name})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Successful output directory (default: {DEFAULT_OUTPUT_DIR.name})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Error output directory (default: {DEFAULT_ERROR_DIR.name})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=API_DELAY_SECONDS,
        help=f"Seconds between API calls (default: {API_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--limit-batches",
        type=int,
        default=None,
        help="Process only the first N batch files (for testing)",
    )
    return parser.parse_args(argv)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_batch_files(batch_dir: Path) -> list[Path]:
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {batch_dir}")
    files = sorted(batch_dir.glob(BATCH_GLOB))
    if not files:
        raise FileNotFoundError(f"No batch files matching {BATCH_GLOB} in {batch_dir}")
    return files


def load_batch(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return [item for item in data if isinstance(item, dict)]


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


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rating = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if 0 <= rating <= 5:
        return rating
    return None


def _to_int_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        count = int(float(str(value).strip().replace(",", "")))
    except ValueError:
        return None
    if count >= 0:
        return count
    return None


def maps_url_from_record(record: dict[str, Any]) -> str | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    uri = result.get("formatted_google_maps_uri") or result.get(
        "formatted_googleMapsUri"
    )
    return str(uri).strip() if uri else None


def place_id_from_record(record: dict[str, Any]) -> str:
    result = record.get("result")
    if not isinstance(result, dict):
        return ""
    return str(result.get("place_id") or "").strip()


def cid_from_record(record: dict[str, Any]) -> str:
    result = record.get("result")
    if not isinstance(result, dict):
        return ""
    return str(result.get("cid") or "").strip()


def is_manual_rating_record(record: dict[str, Any]) -> bool:
    result = record.get("result")
    return (
        isinstance(result, dict)
        and str(result.get("rating_type") or "").strip().upper() == RATING_TYPE_MANUAL
    )


def has_ratings_gapi_requirements(record: dict[str, Any]) -> bool:
    return bool(
        place_id_from_record(record)
        and maps_url_from_record(record)
    )


def ensure_result(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result")
    if not isinstance(result, dict):
        result = {}
        record["result"] = result
    return result


def merge_place_details_into_record(
    record: dict[str, Any],
    place: dict[str, Any],
) -> dict[str, str]:
    """Apply Place Details onto record. Returns status labels to print."""
    result = ensure_result(record)
    labels: dict[str, str] = {}

    rating = _to_float(place.get("rating"))
    total_reviews = _to_int_count(place.get("userRatingCount"))
    if rating is not None:
        result["rating"] = rating
    if total_reviews is not None:
        result["total_reviews"] = total_reviews
    result["is_gmaps_checked"] = True

    business_status = str(place.get("businessStatus") or "").strip().upper()
    if business_status == BUSINESS_STATUS_PERMANENTLY_CLOSED:
        record["is_permanently_closed"] = True
        labels["permanently_closed"] = business_status
    elif business_status == BUSINESS_STATUS_TEMPORARILY_CLOSED:
        record["is_temporarily_closed"] = True
        labels["temporarily_closed"] = business_status
        labels["cache_permanently_closed"] = business_status

    if rating is None and total_reviews is None:
        labels["missing_rating_reviews"] = "no rating or review count in GAPI place"

    return labels


def process_provider(
    provider: dict[str, Any],
    *,
    cache: GapiCache,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    record = deepcopy(provider)

    if is_manual_rating_record(record):
        return record, None, "skipped_manual"

    if not has_ratings_gapi_requirements(record):
        return record, None, "skipped_missing_requirements"

    provider_id = str(record.get("id") or "").strip()
    cached_entry = cache.get_entry(provider_id) if provider_id else None

    if cached_entry and is_permanently_closed_entry(cached_entry):
        record["is_permanently_closed"] = True
        return record, None, "skipped_permanently_closed"

    if cached_entry and is_manually_blocked_entry(cached_entry):
        record["is_manually_blocked"] = True
        return record, None, "skipped_manually_blocked"

    if cached_entry and is_skip_rating_gapi_entry(cached_entry):
        record["skip_rating_gapi"] = True
        reason = cached_entry.get("skip_rating_gapi_reason")
        if reason is not None:
            record["skip_rating_gapi_reason"] = reason
        return record, None, "skipped_rating_gapi"

    place_id = place_id_from_record(record)

    try:
        place = get_place_details(
            place_id,
            field_mask=DEFAULT_RATINGS_DETAILS_FIELD_MASK,
        )
    except GapiError as exc:
        record["ratings_gapi_error"] = exc.to_dict()
        return None, record, "error"

    status_labels = merge_place_details_into_record(record, place)

    cache.record_rating_gapi_called(record, called_at=utc_now_iso())

    if status_labels.get("permanently_closed") or status_labels.get(
        "cache_permanently_closed"
    ):
        cache.mark_permanently_closed(record, marked_at=utc_now_iso())

    if status_labels.get("missing_rating_reviews") and not (
        status_labels.get("permanently_closed")
        or status_labels.get("cache_permanently_closed")
    ):
        cache.mark_skip_rating_gapi(
            record,
            SKIP_RATING_GAPI_REASON_NO_RATINGS,
            marked_at=utc_now_iso(),
        )

    record["_ratings_gapi_status"] = status_labels
    return record, None, "api_called"


def _print_provider_status(
    provider_name: str,
    status_labels: dict[str, str],
) -> None:
    if status_labels.get("permanently_closed"):
        print(f"    permanently closed: {provider_name}")
    if status_labels.get("temporarily_closed"):
        print(f"    temporarily closed: {provider_name}")
    if status_labels.get("missing_rating_reviews"):
        print(f"    no rating/reviews: {provider_name}")


def _strip_internal_fields(record: dict[str, Any]) -> dict[str, Any]:
    record.pop("_ratings_gapi_status", None)
    return record


def process_batch_file(
    batch_path: Path,
    *,
    output_dir: Path,
    error_dir: Path,
    delay: float,
    cache: GapiCache,
) -> dict[str, Any]:
    batch_started = utc_now_iso()
    batch_id = batch_id_from_path(batch_path)
    batch_number = batch_number_from_path(batch_path)
    input_file = relative_to_script(batch_path)

    providers = load_batch(batch_path)
    provider_ids = [
        str(provider.get("id") or "").strip()
        for provider in providers
        if isinstance(provider, dict)
    ]
    cache.prefetch(provider_ids)

    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    api_called_count = 0
    api_skipped_count = 0
    permanently_closed_count = 0
    temporarily_closed_count = 0
    missing_rating_reviews_count = 0

    for index, provider in enumerate(providers):
        provider_name = provider.get("name", "<unnamed>")
        print(
            f"  Processing batch {batch_number} provider: {provider_name} "
            f"at index: {index}"
        )

        try:
            success, error, outcome = process_provider(provider, cache=cache)
        except Exception as exc:  # noqa: BLE001
            failed = deepcopy(provider)
            failed["ratings_gapi_error"] = RatingsGapiError(str(exc)).to_dict()
            errors.append(_strip_internal_fields(failed))
            print(f"    error: {exc}")
            continue

        if outcome == "api_called":
            api_called_count += 1
            print("    google api: called")
        else:
            api_skipped_count += 1
            print(f"    google api: skipped ({outcome})")

        if success is not None:
            status_labels = success.pop("_ratings_gapi_status", {})
            if status_labels:
                _print_provider_status(provider_name, status_labels)
            if status_labels.get("permanently_closed"):
                permanently_closed_count += 1
            if status_labels.get("temporarily_closed"):
                temporarily_closed_count += 1
            if status_labels.get("missing_rating_reviews"):
                missing_rating_reviews_count += 1
            successes.append(_strip_internal_fields(success))
        if error is not None:
            errors.append(_strip_internal_fields(error))
            err_msg = (error.get("ratings_gapi_error") or {}).get("message", "failed")
            print(f"    error: {err_msg}")

        if delay > 0 and index < len(providers) - 1 and outcome == "api_called":
            time.sleep(delay)

    try:
        cache.flush()
    except OSError as exc:
        print(f"  warning: could not flush gapi cache: {exc}", file=sys.stderr)

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
        "api_called_count": api_called_count,
        "api_skipped_count": api_skipped_count,
        "permanently_closed_count": permanently_closed_count,
        "temporarily_closed_count": temporarily_closed_count,
        "missing_rating_reviews_count": missing_rating_reviews_count,
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
    cache = GapiCache(GAPI_CACHE_DIR)
    if cache:
        print(
            f"Loaded {len(cache)} provider id(s) from {GAPI_CACHE_DIR.name}/index.json"
        )

    for batch_path in batch_files:
        print(f"Processing {batch_path.name} ...")
        try:
            result = process_batch_file(
                batch_path,
                output_dir=output_dir,
                error_dir=error_dir,
                delay=args.delay,
                cache=cache,
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
                "api_called_count": 0,
                "api_skipped_count": 0,
                "permanently_closed_count": 0,
                "temporarily_closed_count": 0,
                "missing_rating_reviews_count": 0,
                "batch_error": str(exc),
            }
            print(f"  batch failed: {exc}", file=sys.stderr)

        batch_results.append(result)
        print(
            f"  {result['status']}: {result['success_count']} ok, "
            f"{result['error_count']} error(s), "
            f"api_called={result.get('api_called_count', 0)}, "
            f"api_skipped={result.get('api_skipped_count', 0)}, "
            f"permanently_closed={result.get('permanently_closed_count', 0)}, "
            f"temporarily_closed={result.get('temporarily_closed_count', 0)}, "
            f"missing_rating_reviews={result.get('missing_rating_reviews_count', 0)}"
        )

    successful_batches = sum(1 for b in batch_results if b["status"] == "success")
    error_batches = len(batch_results) - successful_batches
    api_called_count = sum(b.get("api_called_count", 0) for b in batch_results)
    api_skipped_count = sum(b.get("api_skipped_count", 0) for b in batch_results)
    permanently_closed_count = sum(
        b.get("permanently_closed_count", 0) for b in batch_results
    )
    temporarily_closed_count = sum(
        b.get("temporarily_closed_count", 0) for b in batch_results
    )
    missing_rating_reviews_count = sum(
        b.get("missing_rating_reviews_count", 0) for b in batch_results
    )

    run_record = {
        "run_id": run_started,
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "error_dir": str(error_dir),
        "total_batches_count": len(batch_results),
        "successful_batches_count": successful_batches,
        "error_batches_count": error_batches,
        "api_called_count": api_called_count,
        "api_skipped_count": api_skipped_count,
        "permanently_closed_count": permanently_closed_count,
        "temporarily_closed_count": temporarily_closed_count,
        "missing_rating_reviews_count": missing_rating_reviews_count,
        "batches": batch_results,
    }

    try:
        manifest = load_manifest(MANIFEST_PATH)
        append_run_to_manifest(manifest, run_record)
        save_manifest(MANIFEST_PATH, manifest)
    except OSError as exc:
        print(f"Warning: could not update manifest: {exc}", file=sys.stderr)

    print(
        f"\nRun complete: {successful_batches}/{len(batch_results)} batch(es) fully "
        f"successful. Manifest: {MANIFEST_PATH.name}"
    )
    print(
        f"Google API: called={api_called_count}, skipped={api_skipped_count}, "
        f"permanently_closed={permanently_closed_count}, "
        f"temporarily_closed={temporarily_closed_count}, "
        f"missing_rating_reviews={missing_rating_reviews_count}"
    )
    return 0 if error_batches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
