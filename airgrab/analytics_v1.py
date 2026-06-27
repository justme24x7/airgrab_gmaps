#!/usr/bin/env python3
"""Filter p5 imputed-rating outputs and summarize providers by rating type."""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "p5_imputed_ratings" / "output"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "analytics" / "analytics_v1_output.json"
BATCH_GLOB = "*.json"

# Comma-separated filters. Leave empty to skip that filter.
# RATING_BETWEEN: two values as "min,max" (e.g. "3.5,4.5").
# CENTER_LAT_LNG: one "lat,lng" pair (e.g. "12.85002284,77.65752131").
# RADIUS_METERS: crow-fly radius in meters from CENTER_LAT_LNG (e.g. "5000").
# PINCODES = "560087, 560048"
# PROVIDER_NAMES = "Chef Bakers"
# RATING_TYPES = "GOOGLE"
# RATING_BETWEEN = "4.2,4.4"
# CENTER_LAT_LNG = "12.85002284,77.65752131"
# RADIUS_METERS = "5000"
PINCODES = ""
PROVIDER_NAMES = ""
RATING_TYPES = ""
RATING_BETWEEN = ""
CENTER_LAT_LNG = "12.8851354,77.563325"
RADIUS_METERS = "4000"


# ---- Constants ----
RATING_TYPE_GOOGLE = "GOOGLE"
RATING_TYPE_IMPUTED = "IMPUTED"
RATING_TYPE_NA = "NA"
RATING_TYPE_MANUAL = "MANUAL"

VALID_RATING_TYPES = {
    RATING_TYPE_GOOGLE,
    RATING_TYPE_IMPUTED,
    RATING_TYPE_NA,
    RATING_TYPE_MANUAL,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_csv_config(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


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


def pincode_from_record(record: dict[str, Any]) -> str:
    address = record.get("address")
    if not isinstance(address, dict):
        return ""
    return str(address.get("area_code") or "").strip()


def parse_rating_between_config(value: str) -> tuple[float, float] | None:
    parts = parse_csv_config(value)
    if len(parts) != 2:
        return None
    bounds: list[float] = []
    for part in parts:
        try:
            rating = float(part.replace(",", "."))
        except ValueError:
            return None
        if not 0 <= rating <= 5:
            return None
        bounds.append(rating)
    return min(bounds), max(bounds)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def parse_center_lat_lng_config(value: str) -> tuple[float, float] | None:
    parts = parse_csv_config(value)
    if len(parts) != 2:
        return None
    lat = _to_float(parts[0])
    lng = _to_float(parts[1])
    if lat is None or lng is None:
        return None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return lat, lng


def parse_radius_meters_config(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "," in text:
        return None
    radius = _to_float(text)
    if radius is None or radius < 0:
        return None
    return radius


def address_gps_from_record(record: dict[str, Any]) -> tuple[float | None, float | None]:
    address = record.get("address")
    if not isinstance(address, dict):
        return None, None

    gps = address.get("gps")
    if not isinstance(gps, dict):
        return None, None

    return _to_float(gps.get("lat")), _to_float(gps.get("long"))


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


def rating_from_record(record: dict[str, Any]) -> float | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    return parse_rating(result.get("rating"))


def rating_type_from_record(record: dict[str, Any]) -> str:
    result = record.get("result")
    if not isinstance(result, dict):
        return RATING_TYPE_NA
    rating_type = str(result.get("rating_type") or "").strip().upper()
    if rating_type in VALID_RATING_TYPES:
        return rating_type
    return RATING_TYPE_NA


def load_unique_providers(input_dir: Path, glob_pattern: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []

    for path in list_batch_files(input_dir, glob_pattern):
        for record in load_batch_file(path):
            provider_id = provider_id_from_record(record)
            if provider_id:
                unique[provider_id] = record
            else:
                unkeyed.append(record)

    return list(unique.values()) + unkeyed


def matches_pincode_filter(record: dict[str, Any], pincodes: list[str]) -> bool:
    if not pincodes:
        return True
    pincode = pincode_from_record(record)
    return pincode in pincodes


def matches_provider_name_filter(
    record: dict[str, Any],
    provider_names: list[str],
) -> bool:
    if not provider_names:
        return True
    name = normalize_text(record.get("name"))
    if not name:
        return False
    normalized_filters = [normalize_text(value) for value in provider_names]
    return any(
        filter_name == name or filter_name in name or name in filter_name
        for filter_name in normalized_filters
    )


def matches_rating_type_filter(
    record: dict[str, Any],
    rating_types: list[str],
) -> bool:
    if not rating_types:
        return True
    normalized_filters = {
        value.strip().upper()
        for value in rating_types
        if value.strip().upper() in VALID_RATING_TYPES
    }
    if not normalized_filters:
        return True
    return rating_type_from_record(record) in normalized_filters


def matches_rating_between_filter(
    record: dict[str, Any],
    rating_between: tuple[float, float] | None,
) -> bool:
    if rating_between is None:
        return True
    rating = rating_from_record(record)
    if rating is None:
        return False
    low, high = rating_between
    return low <= rating <= high


def matches_radius_filter(
    record: dict[str, Any],
    center_lat_lng: tuple[float, float] | None,
    radius_meters: float | None,
) -> bool:
    if center_lat_lng is None or radius_meters is None:
        return True

    center_lat, center_lng = center_lat_lng
    provider_lat, provider_lng = address_gps_from_record(record)
    distance_meters = crow_fly_distance_meters(
        center_lat,
        center_lng,
        provider_lat,
        provider_lng,
    )
    if distance_meters is None:
        return False
    return distance_meters <= radius_meters


def filter_providers(
    providers: list[dict[str, Any]],
    *,
    pincodes: list[str],
    provider_names: list[str],
    rating_types: list[str],
    rating_between: tuple[float, float] | None,
    center_lat_lng: tuple[float, float] | None,
    radius_meters: float | None,
) -> list[dict[str, Any]]:
    return [
        provider
        for provider in providers
        if matches_pincode_filter(provider, pincodes)
        and matches_provider_name_filter(provider, provider_names)
        and matches_rating_type_filter(provider, rating_types)
        and matches_rating_between_filter(provider, rating_between)
        and matches_radius_filter(provider, center_lat_lng, radius_meters)
    ]


def build_analytics(
    providers: list[dict[str, Any]],
    *,
    pincodes: list[str],
    provider_names: list[str],
    rating_types: list[str],
    rating_between: tuple[float, float] | None,
    center_lat_lng: tuple[float, float] | None,
    radius_meters: float | None,
    input_dir: Path,
) -> dict[str, Any]:
    google_providers: list[dict[str, Any]] = []
    imputed_providers: list[dict[str, Any]] = []
    manual_providers: list[dict[str, Any]] = []
    na_providers: list[dict[str, Any]] = []

    for provider in providers:
        rating_type = rating_type_from_record(provider)
        if rating_type == RATING_TYPE_GOOGLE:
            google_providers.append(provider)
        elif rating_type == RATING_TYPE_IMPUTED:
            imputed_providers.append(provider)
        elif rating_type == RATING_TYPE_MANUAL:
            manual_providers.append(provider)
        else:
            na_providers.append(provider)

    return {
        "generated_at": utc_now_iso(),
        "input_dir": str(input_dir),
        "filters": {
            "pincodes": pincodes,
            "provider_names": provider_names,
            "rating_types": rating_types,
            "rating_between": (
                {"min": rating_between[0], "max": rating_between[1]}
                if rating_between is not None
                else None
            ),
            "pincodes_applied": bool(pincodes),
            "provider_names_applied": bool(provider_names),
            "rating_types_applied": bool(rating_types),
            "rating_between_applied": rating_between is not None,
            "center_lat_lng": (
                {"lat": center_lat_lng[0], "lng": center_lat_lng[1]}
                if center_lat_lng is not None
                else None
            ),
            "radius_meters": radius_meters,
            "radius_filter_applied": (
                center_lat_lng is not None and radius_meters is not None
            ),
        },
        "summary": {
            "total_providers": len(providers),
            "providers_with_google_rating": len(google_providers),
            "providers_with_imputed_rating": len(imputed_providers),
            "providers_with_manual_rating": len(manual_providers),
            "providers_with_na_rating": len(na_providers),
        },
        "groups": {
            "google_rating": google_providers,
            "imputed_rating": imputed_providers,
            "manual_rating": manual_providers,
            "na_rating": na_providers,
        },
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    input_dir = DEFAULT_INPUT_DIR.resolve()
    output_path = DEFAULT_OUTPUT_PATH.resolve()
    pincodes = parse_csv_config(PINCODES)
    provider_names = parse_csv_config(PROVIDER_NAMES)
    rating_types = parse_csv_config(RATING_TYPES)
    rating_between = parse_rating_between_config(RATING_BETWEEN)
    center_lat_lng = parse_center_lat_lng_config(CENTER_LAT_LNG)
    radius_meters = parse_radius_meters_config(RADIUS_METERS)

    if CENTER_LAT_LNG.strip() and center_lat_lng is None:
        print(
            "Error: CENTER_LAT_LNG must be a single lat,lng pair "
            '(e.g. "12.85,77.65")',
            file=sys.stderr,
        )
        return 1
    if RADIUS_METERS.strip() and radius_meters is None:
        print(
            'Error: RADIUS_METERS must be a single non-negative number '
            '(e.g. "5000")',
            file=sys.stderr,
        )
        return 1

    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    try:
        all_providers = load_unique_providers(input_dir, BATCH_GLOB)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error loading providers: {exc}", file=sys.stderr)
        return 1

    if not all_providers:
        print("No provider records found.", file=sys.stderr)
        return 1

    filtered = filter_providers(
        all_providers,
        pincodes=pincodes,
        provider_names=provider_names,
        rating_types=rating_types,
        rating_between=rating_between,
        center_lat_lng=center_lat_lng,
        radius_meters=radius_meters,
    )
    analytics = build_analytics(
        filtered,
        pincodes=pincodes,
        provider_names=provider_names,
        rating_types=rating_types,
        rating_between=rating_between,
        center_lat_lng=center_lat_lng,
        radius_meters=radius_meters,
        input_dir=input_dir,
    )

    try:
        write_json(output_path, analytics)
    except OSError as exc:
        print(f"Error writing output: {exc}", file=sys.stderr)
        return 1

    summary = analytics["summary"]
    print(f"Wrote analytics to {output_path}")
    print(f"Total providers returned: {summary['total_providers']}")
    print(
        f"  GOOGLE: {summary['providers_with_google_rating']}, "
        f"IMPUTED: {summary['providers_with_imputed_rating']}, "
        f"MANUAL: {summary['providers_with_manual_rating']}, "
        f"NA: {summary['providers_with_na_rating']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
