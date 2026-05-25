#!/usr/bin/env python3
"""Google Places Text Search (New) API client."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
# Project root .env is the primary source (see repo .env.example).
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
CID_URI_TEMPLATE = "https://maps.google.com/?cid={cid}"
# If len(places) is greater than this, cid / Maps URI are left empty (ambiguous match).
MAX_PLACES_FOR_UNIQUE_MATCH = 2


def normalize_field_mask(field_mask: str | tuple[str, ...] | list[str]) -> str:
    """Ensure X-Goog-FieldMask is a single comma-separated string (no spaces)."""
    if isinstance(field_mask, (tuple, list)):
        return ",".join(field_mask)
    return str(field_mask).replace(" ", "")


API_KEY_ENV = "GOOGLE_PLACES_API_KEY"


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


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overwriting)."""
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
    """Load env vars from project root .env, then optional airgrab/.env."""
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


def search_text(
    text_query: str,
    *,
    api_key: str | None = None,
    field_mask: str = DEFAULT_FIELD_MASK,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Call Google Places Text Search (New) for a single textQuery.

    https://developers.google.com/maps/documentation/places/web-service/text-search
    """
    query = text_query.strip()
    if not query:
        raise GapiError("textQuery must not be empty")

    key = api_key or get_api_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": normalize_field_mask(field_mask),
    }
    payload = {"textQuery": query}

    try:
        response = requests.post(
            PLACES_SEARCH_URL,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GapiError(f"HTTP request failed: {exc}") from exc

    if response.ok:
        return response.json()

    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text

    raise GapiError(
        f"Places API error ({response.status_code})",
        status_code=response.status_code,
        response_body=body,
    )


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


def empty_place_result() -> dict[str, str]:
    return {"cid": "", "formatted_google_maps_uri": ""}


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
    *,
    max_places_for_match: int = MAX_PLACES_FOR_UNIQUE_MATCH,
) -> dict[str, str] | None:
    """
    Build results from places[0] when the match is unambiguous.

    If len(places) > max_places_for_match (default > 2), returns empty cid and URI.
    Otherwise uses places[0] only.
    """
    places = gapi_response.get("places")
    if not isinstance(places, list) or not places:
        return None

    if len(places) > max_places_for_match:
        return empty_place_result()

    first_place = places[0]
    if not isinstance(first_place, dict):
        return None

    return build_place_result(first_place)
