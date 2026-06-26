#!/usr/bin/env python3
"""Pipeline step 7: normalize p5 outputs for Mongo import.

What it does:
  Reads ``p5_imputed_ratings/output`` and ``output_errors/``, merges same-named batch
  files, normalizes each provider to a slim schema (id, cid, rating, rating_type,
  etc.), and writes to ``p7_for_mongo/``.

Overall logic:
  - Load block list from ``p6_block_list/block_list.json``.
  - For each batch file name present in either input dir, concatenate providers.
  - Drop block-listed ids, normalize remaining records.
  - Force ``is_gapi_called`` and ``is_gmaps_checked`` to ``true`` on output.

Exclude from output (not written to ``p7_for_mongo``):
  - Provider ``id`` present in ``p6_block_list/block_list.json`` ``provider_ids``

No other skip logic. All non-blocked providers are normalized and included.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_INPUT_DIR = SCRIPT_DIR / "p5_imputed_ratings" / "output"
DEFAULT_ERROR_DIR = SCRIPT_DIR / "p5_imputed_ratings" / "output_errors"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "p7_for_mongo"
DEFAULT_BLOCK_LIST_PATH = SCRIPT_DIR / "p6_block_list" / "block_list.json"

CID_URI_TEMPLATE = "https://maps.google.com/?cid={cid}"
EXCLUDED_PLACE_TYPES = frozenset(
    {
        "restaurant",
        "point_of_interest",
        "food",
        "food_store",
        "store",
        "establishment",
        "service",
    }
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert p5 imputed-rating batch outputs into Mongo-friendly JSON by keeping only "
            "the required fields, filling missing keys with null, forcing "
            "is_gapi_called/is_gmaps_checked=true, excluding block-listed provider ids, "
            "and merging same-named files "
            "from output/ + output_errors/ into a single output file."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Input directory (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Error directory (default: {DEFAULT_ERROR_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--glob",
        type=str,
        default="*.json",
        help="Glob pattern for batch files (default: *.json)",
    )
    parser.add_argument(
        "--block-list",
        type=Path,
        default=DEFAULT_BLOCK_LIST_PATH,
        help=f"Block list JSON path (default: {DEFAULT_BLOCK_LIST_PATH})",
    )
    return parser.parse_args(argv)


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a top-level JSON array")
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"_raw": item})
    return out


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _provider_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def load_block_list(path: Path) -> set[str]:
    if not path.is_file():
        print(f"Warning: block list not found at {path}; no providers excluded")
        return set()

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("provider_ids"), list):
        ids = data["provider_ids"]
    elif isinstance(data, list):
        ids = data
    else:
        raise ValueError(f"{path}: expected provider_ids list or block-list object")

    return {str(provider_id).strip() for provider_id in ids if str(provider_id).strip()}


def _safe_get(d: Any, key: str) -> Any:
    if isinstance(d, dict) and key in d:
        return d.get(key)
    return None


def _cid_from_google_maps_uri(uri: Any) -> str | None:
    if not isinstance(uri, str) or not uri.strip():
        return None
    try:
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query)
        cid_vals = qs.get("cid")
        if not cid_vals:
            return None
        cid = str(cid_vals[0]).strip()
        return cid or None
    except Exception:
        return None


def _normalize_result_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}

    # Support two shapes:
    # - already-processed `results[]` item (has cid/place_types/etc)
    # - GAPI "place" object (has googleMapsUri, types, userRatingCount)
    google_maps_uri = _safe_get(raw, "googleMapsUri") or _safe_get(raw, "google_maps_uri")
    cid = _safe_get(raw, "cid") or _cid_from_google_maps_uri(google_maps_uri)

    place_types = (
        _safe_get(raw, "place_types")
        or _safe_get(raw, "placeTypes")
        or _safe_get(raw, "types")
        or None
    )
    if not isinstance(place_types, list):
        place_types = None
    else:
        place_types = [t for t in place_types if t not in EXCLUDED_PLACE_TYPES]

    rating = _safe_get(raw, "rating")
    total_reviews = (
        _safe_get(raw, "total_reviews")
        or _safe_get(raw, "totalReviews")
        or _safe_get(raw, "userRatingCount")
        or _safe_get(raw, "user_ratings_total")
    )

    rating_type = _safe_get(raw, "rating_type")
    if isinstance(rating_type, str):
        rating_type = rating_type.strip().upper() or None
    else:
        rating_type = None

    formatted_google_maps_uri = (
        _safe_get(raw, "formatted_google_maps_uri")
        or _safe_get(raw, "formattedGoogleMapsUri")
        or google_maps_uri
        or (CID_URI_TEMPLATE.format(cid=cid) if cid else None)
    )

    return {
        "cid": cid,
        "formatted_google_maps_uri": formatted_google_maps_uri,
        "place_types": place_types,
        "is_gapi_called": True,
        "rating": rating,
        "total_reviews": total_reviews,
        "rating_type": rating_type,
        "is_gmaps_checked": True,
    }


def _extract_results(restaurant: dict[str, Any]) -> list[dict[str, Any]] | None:
    results = _safe_get(restaurant, "results")
    if isinstance(results, dict):
        return [_normalize_result_item(results)]
    if isinstance(results, list):
        return [_normalize_result_item(r) for r in results]

    gapi_response = _safe_get(restaurant, "gapi_response")
    if isinstance(gapi_response, dict):
        places = _safe_get(gapi_response, "places")
        if isinstance(places, list):
            return [_normalize_result_item(p) for p in places]

    return None


def normalize_restaurant(restaurant: Any) -> dict[str, Any]:
    if not isinstance(restaurant, dict):
        restaurant = {}

    results = _extract_results(restaurant) or []
    first = results[0] if results else None

    return {
        "id": _safe_get(restaurant, "id"),
        "local_id": _safe_get(restaurant, "local_id"),
        "gmaps_text_query": _safe_get(restaurant, "text_query"),
        "cid": _safe_get(first, "cid") if isinstance(first, dict) else None,
        "formatted_google_maps_uri": (
            _safe_get(first, "formatted_google_maps_uri") if isinstance(first, dict) else None
        ),
        "place_types": _safe_get(first, "place_types") if isinstance(first, dict) else None,
        "is_gapi_called": True,
        "rating": _safe_get(first, "rating") if isinstance(first, dict) else None,
        "total_reviews": _safe_get(first, "total_reviews") if isinstance(first, dict) else None,
        "rating_type": _safe_get(first, "rating_type") if isinstance(first, dict) else None,
        "is_gmaps_checked": True,
    }


def list_json_files(directory: Path, glob_pattern: str) -> list[Path]:
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.glob(glob_pattern) if p.is_file())
    return [p for p in files if p.name != ".DS_Store"]


def merge_restaurant_lists(lists: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for lst in lists:
        merged.extend(lst)
    return merged


def run(
    *,
    input_dir: Path,
    error_dir: Path,
    output_dir: Path,
    glob_pattern: str,
    block_list: set[str],
) -> None:
    input_files = {p.name: p for p in list_json_files(input_dir, glob_pattern)}
    error_files = {p.name: p for p in list_json_files(error_dir, glob_pattern)}
    all_names = sorted(set(input_files) | set(error_files))

    if not all_names:
        raise FileNotFoundError(
            f"No JSON files found in {input_dir} or {error_dir} (glob: {glob_pattern})"
        )

    if block_list:
        print(f"Loaded {len(block_list)} block-listed provider id(s)")

    total_excluded = 0

    for name in all_names:
        print(name)
        sources: list[list[dict[str, Any]]] = []

        if name in input_files:
            sources.append(_load_json_array(input_files[name]))
        if name in error_files:
            sources.append(_load_json_array(error_files[name]))

        merged = merge_restaurant_lists(sources)
        kept = [
            record
            for record in merged
            if _provider_id(record) not in block_list
        ]
        total_excluded += len(merged) - len(kept)
        normalized = [normalize_restaurant(r) for r in kept]

        out_path = output_dir / name
        _write_json(out_path, normalized)

    if block_list:
        print(f"Excluded {total_excluded} block-listed provider record(s)")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    block_list = load_block_list(args.block_list.resolve())
    run(
        input_dir=args.input_dir,
        error_dir=args.error_dir,
        output_dir=args.output_dir,
        glob_pattern=args.glob,
        block_list=block_list,
    )


if __name__ == "__main__":
    main()
