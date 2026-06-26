#!/usr/bin/env python3
"""Call Google Places Text Search and store raw GAPI responses."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gapi_cache import GapiCache

REPO_ROOT = SCRIPT_DIR.parent
ENV_FILES = (REPO_ROOT / ".env", SCRIPT_DIR / ".env")
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DEFAULT_FIELD_MASK = (
    "places.id,places.accessibilityOptions,places.addressComponents,"
    "places.addressDescriptor,places.adrFormatAddress,places.businessStatus,"
    "places.containingPlaces,places.displayName,places.formattedAddress,"
    "places.googleMapsLinks,places.googleMapsUri,places.iconBackgroundColor,"
    "places.iconMaskBaseUri,places.location,places.openingDate,places.plusCode,"
    "places.postalAddress,places.primaryType,places.primaryTypeDisplayName,"
    "places.pureServiceAreaBusiness,places.shortFormattedAddress,"
    "places.subDestinations,places.timeZone,places.types,places.utcOffsetMinutes,"
    "places.viewport"
)
API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

DEFAULT_BATCH_DIR = SCRIPT_DIR / "p1_batched_raw_providers"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output_errors"
MANIFEST_PATH = SCRIPT_DIR / "p2_batched_gapi_details" / "gapi_run_mainfest.json"
GAPI_CACHE_DIR = SCRIPT_DIR / "gapi_cache"
BATCH_GLOB = "batch_*.json"
LOCATION_BIAS_RADIUS = 200.0


class GapiError(Exception):
    """Raised when the Places Text Search API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "status_code": self.status_code,
            "response_body": self.response_body,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Call Google Places Text Search for each restaurant in batch files "
            "and write raw GAPI responses to output/ and output_errors/."
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
    return parser.parse_args(argv)


def normalize_field_mask(field_mask: str | tuple[str, ...] | list[str]) -> str:
    if isinstance(field_mask, (tuple, list)):
        return ",".join(field_mask)
    return str(field_mask).replace(" ", "")


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_dotenv() -> None:
    for path in ENV_FILES:
        _load_env_file(path)


def get_api_key() -> str:
    load_dotenv()
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise GapiError(
            f"{API_KEY_ENV} is not set. Add it to {REPO_ROOT / '.env'} "
            f"(see {REPO_ROOT / '.env.example'})."
        )
    return api_key


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _contains(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    return needle.casefold() in haystack.casefold()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def build_location_bias(provider: dict) -> dict[str, Any] | None:
    address = provider.get("address")
    if not isinstance(address, dict):
        return None

    gps = address.get("gps")
    if not isinstance(gps, dict):
        return None

    latitude = _to_float(gps.get("lat"))
    longitude = _to_float(gps.get("long"))
    if latitude is None or longitude is None:
        return None

    return {
        "circle": {
            "center": {
                "latitude": latitude,
                "longitude": longitude,
            },
            "radius": LOCATION_BIAS_RADIUS,
        }
    }


def format_places_text_query(provider: dict) -> str:
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


def search_text(
    text_query: str,
    *,
    api_key: str | None = None,
    field_mask: str = DEFAULT_FIELD_MASK,
    location_bias: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    query = text_query.strip()
    if not query:
        raise GapiError("textQuery must not be empty")

    key = api_key or get_api_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": normalize_field_mask(field_mask),
    }
    payload_data: dict[str, Any] = {"textQuery": query}
    if location_bias is not None:
        payload_data["locationBias"] = location_bias
    payload = json.dumps(payload_data).encode("utf-8")
    request = urllib_request.Request(
        PLACES_SEARCH_URL,
        data=payload,
        headers=headers,
        method="POST",
    )

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


def list_batch_files(batch_dir: Path) -> list[Path]:
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"Batch directory not found: {batch_dir}")
    files = sorted(batch_dir.glob(BATCH_GLOB))
    if not files:
        raise FileNotFoundError(f"No batch files matching {BATCH_GLOB} in {batch_dir}")
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


def process_provider(
    provider: dict,
    *,
    field_mask: str,
    cache: GapiCache,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    record = deepcopy(provider)
    text_query = format_places_text_query(provider)
    record["text_query"] = text_query
    location_bias = build_location_bias(provider)
    provider_id = str(provider.get("id") or "").strip()

    if provider_id and cache.should_skip_gapi(provider_id):
        cached_entry = cache.get_entry(provider_id)
        if cached_entry:
            gapi_response = cached_entry.get("gapi_response")
            if isinstance(gapi_response, dict):
                record["gapi_response"] = deepcopy(gapi_response)
        return record, None, True

    try:
        record["gapi_response"] = search_text(
            text_query,
            field_mask=field_mask,
            location_bias=location_bias,
        )
        if provider_id:
            cache.record_gapi_call(
                provider,
                record["gapi_response"],
                called_at=utc_now_iso(),
            )
        return record, None, False
    except GapiError as exc:
        record["gapi_error"] = exc.to_dict()
        return None, record, False


def process_batch_file(
    batch_path: Path,
    *,
    output_dir: Path,
    error_dir: Path,
    field_mask: str,
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

        provider_name = provider.get("name", "<unnamed>")
        print(
            f"  Processing batch {batch_number} provider: {provider_name} "
            f"at index: {index}"
        )
        success, error, was_skipped = process_provider(
            provider,
            field_mask=field_mask,
            cache=cache,
        )
        if was_skipped:
            api_skipped_count += 1
            print("    google api: skipped (reused gapi_response from gapi_cache)")
        else:
            api_called_count += 1
            print("    google api: called")
        if success is not None:
            successes.append(success)
        if error is not None:
            errors.append(error)

        if delay > 0 and index < len(providers) - 1:
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
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
                field_mask=args.field_mask,
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
    api_called_count = sum(b.get("api_called_count", 0) for b in batch_results)
    api_skipped_count = sum(b.get("api_skipped_count", 0) for b in batch_results)

    run_record = {
        "run_id": run_started,
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "total_batches_count": len(batch_results),
        "successful_batches_count": successful_batches,
        "error_batches_count": error_batches,
        "api_called_count": api_called_count,
        "api_skipped_count": api_skipped_count,
        "batches": batch_results,
    }

    try:
        manifest = load_manifest(MANIFEST_PATH)
        manifest["runs"].append(run_record)
        save_manifest(MANIFEST_PATH, manifest)
    except OSError as exc:
        print(f"Warning: could not update manifest: {exc}", file=sys.stderr)

    print(
        f"\nRun complete: {successful_batches}/{len(batch_results)} batch(es) fully "
        f"successful. Manifest: {MANIFEST_PATH.name}"
    )
    print(f"Google API summary: called={api_called_count}, skipped={api_skipped_count}")
    return 0 if error_batches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
