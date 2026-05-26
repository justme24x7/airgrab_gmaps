#!/usr/bin/env python3
"""Find GAPI output records where places[] length falls within a range."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
# DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p2_batched_gapi_details" / "output"
DEFAULT_MIN_PLACES_LENGTH = 1  # len(places) > 0
DEFAULT_MAX_PLACES_LENGTH = 5
DEFAULT_MIN_DISTANCE = 0.0
DEFAULT_MAX_DISTANCE = 200.0
# DEFAULT_MIN_PLACES_LENGTH = 2  # len(places) > 1
# DEFAULT_MIN_PLACES_LENGTH = 3  # len(places) > 2
BATCH_GLOB = "batch_*.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List restaurants whose Google Places Text Search response has "
            "a places[] length within a configured range."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            f"Directory with GAPI batch output JSON files "
            f"(default: {DEFAULT_OUTPUT_DIR.relative_to(SCRIPT_DIR)})"
        ),
    )
    parser.add_argument(
        "--min-places-length",
        type=int,
        default=DEFAULT_MIN_PLACES_LENGTH,
        help=(
            "Include restaurants with at least this many entries in "
            f"places[] (default: {DEFAULT_MIN_PLACES_LENGTH})"
        ),
    )
    parser.add_argument(
        "--max-places-length",
        type=int,
        default=DEFAULT_MAX_PLACES_LENGTH,
        help=(
            "Include restaurants with at most this many entries in "
            f"places[] (default: {DEFAULT_MAX_PLACES_LENGTH})"
        ),
    )
    return parser.parse_args()


def list_batch_files(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        raise FileNotFoundError(f"Output directory not found: {output_dir}")
    files = sorted(output_dir.glob(BATCH_GLOB))
    if not files:
        raise FileNotFoundError(f"No files matching {BATCH_GLOB} in {output_dir}")
    return files


def load_batch(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a top-level JSON array")
    return data


def places_count(provider: dict[str, Any]) -> int:
    gapi_response = provider.get("gapi_response")
    if not isinstance(gapi_response, dict):
        return 0
    places = gapi_response.get("places")
    if not isinstance(places, list):
        return 0
    return len(places)


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
    lat = _to_float(gps.get("lat"))
    lng = _to_float(gps.get("long"))
    return lat, lng


def place_location(place: dict[str, Any]) -> tuple[float | None, float | None]:
    location = place.get("location")
    if not isinstance(location, dict):
        return None, None
    lat = _to_float(location.get("latitude"))
    lng = _to_float(location.get("longitude"))
    return lat, lng


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


def distance_summary(values: list[float]) -> str:
    if not values:
        return "min=n/a, max=n/a, median=n/a"
    return (
        f"min={min(values):.1f} m, "
        f"max={max(values):.1f} m, "
        f"median={statistics.median(values):.1f} m"
    )


def first_place_distance_meters(provider: dict[str, Any]) -> float | None:
    gapi_response = provider.get("gapi_response")
    if not isinstance(gapi_response, dict):
        return None
    places = gapi_response.get("places")
    if not isinstance(places, list) or not places or not isinstance(places[0], dict):
        return None

    source_lat, source_lng = address_gps(provider)
    place_lat, place_lng = place_location(places[0])
    return crow_fly_distance_meters(source_lat, source_lng, place_lat, place_lng)


def iter_matching_providers(
    output_dir: Path,
    min_places_length: int,
    max_places_length: int,
) -> Iterator[tuple[Path, dict[str, Any], int]]:
    for batch_path in list_batch_files(output_dir):
        for provider in load_batch(batch_path):
            if not isinstance(provider, dict):
                continue
            count = places_count(provider)
            if min_places_length <= count <= max_places_length:
                yield batch_path, provider, count


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()

    if args.min_places_length < 1:
        print("Error: --min-places-length must be at least 1", file=sys.stderr)
        return 1
    if args.max_places_length < args.min_places_length:
        print(
            "Error: --max-places-length must be greater than or equal to "
            "--min-places-length",
            file=sys.stderr,
        )
        return 1

    try:
        batch_files = list_batch_files(output_dir)
        total_restaurants_scanned = 0
        matches: list[tuple[Path, dict[str, Any], int]] = []

        for batch_path in batch_files:
            providers = load_batch(batch_path)
            total_restaurants_scanned += sum(
                1 for provider in providers if isinstance(provider, dict)
            )
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                count = places_count(provider)
                if args.min_places_length <= count <= args.max_places_length:
                    matches.append((batch_path, provider, count))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    filtered_matches: list[tuple[Path, dict[str, Any], int]] = []
    for batch_path, provider, count in matches:
        distance_meters = first_place_distance_meters(provider)
        if distance_meters is None:
            continue
        if DEFAULT_MIN_DISTANCE <= distance_meters <= DEFAULT_MAX_DISTANCE:
            filtered_matches.append((batch_path, provider, count))

    print(
        f"Scanning {output_dir.relative_to(SCRIPT_DIR)} "
        f"(places[] length between {args.min_places_length} and "
        f"{args.max_places_length}, places[0] distance between "
        f"{DEFAULT_MIN_DISTANCE:.1f} m and {DEFAULT_MAX_DISTANCE:.1f} m)\n"
    )

    first_place_distances_meters: list[float] = []
    all_places_distances_meters: list[float] = []

    for batch_path, provider, count in filtered_matches:
        provider_id = provider.get("id", "<unknown>")
        name = provider.get("name", "<unnamed>")
        text_query = str(provider.get("text_query") or "").strip()
        gapi_response = provider.get("gapi_response")
        places = gapi_response.get("places") if isinstance(gapi_response, dict) else []
        source_lat, source_lng = address_gps(provider)

        print(f"[{batch_path.name}] {provider_id} | {name} | places: {count}")
        if text_query:
            print(f"  text_query: {text_query}")
        print(f"  address.gps: lat={source_lat}, long={source_lng}")

        if isinstance(places, list):
            for place_index, place in enumerate(places):
                if not isinstance(place, dict):
                    continue
                place_lat, place_lng = place_location(place)
                distance_meters = crow_fly_distance_meters(
                    source_lat,
                    source_lng,
                    place_lat,
                    place_lng,
                )
                display_name = place.get("displayName")
                if isinstance(display_name, dict):
                    place_name = display_name.get("text", "<unnamed place>")
                else:
                    place_name = "<unnamed place>"
                formatted_address = str(place.get("formattedAddress") or "").strip()
                distance_text = (
                    f"{distance_meters:.1f} m"
                    if distance_meters is not None
                    else "n/a"
                )
                google_maps_uri = str(place.get("googleMapsUri") or "").strip()
                if distance_meters is not None:
                    all_places_distances_meters.append(distance_meters)
                    if place_index == 0:
                        first_place_distances_meters.append(distance_meters)
                line = (
                    f"    place[{place_index}] {place_name} | "
                    f"location: lat={place_lat}, long={place_lng} | "
                    f"distance: {distance_text}"
                )
                if formatted_address:
                    line += f" | formattedAddress: {formatted_address}"
                if google_maps_uri:
                    line += f" | googleMapsUri: {google_maps_uri}"
                print(line)
        print()

    print("Global distance summary")
    print(
        "  places[0] across restaurants: "
        f"{distance_summary(first_place_distances_meters)}"
    )
    print(
        "  all places[] across restaurants: "
        f"{distance_summary(all_places_distances_meters)}"
    )
    print(
        "  restaurants with places[0] distance <= 50 m: "
        f"{sum(1 for d in first_place_distances_meters if d <= 50)}"
    )
    print(
        "  restaurants with places[0] distance <= 100 m: "
        f"{sum(1 for d in first_place_distances_meters if 50 < d <= 100)}"
    )
    print(
        "  restaurants with places[0] distance <= 200 m: "
        f"{sum(1 for d in first_place_distances_meters if 100 < d <= 200)}"
    )
    print(
        "  restaurants with places[0] distance <= 1000 m: "
        f"{sum(1 for d in first_place_distances_meters if 200 < d <= 1000)}"
    )
    print(
        "  restaurants with places[0] distance > 1000 m: "
        f"{sum(1 for d in first_place_distances_meters if d > 1000)}"
    )
    print()
    print(
        f"\nTotal: {len(filtered_matches)} / {total_restaurants_scanned} restaurant(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
