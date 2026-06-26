"""
Pipeline step 4: Selenium scrape of Google Maps rating + review count.

What it does:
  Reads ``p3_batched_processed_gapi_details/output``, visits each provider's Maps URL,
  scrapes ``rating`` and ``total_reviews`` into ``results``, and writes to
  ``p4_batched_scraper_details/output`` and ``output_errors/``.

Overall logic:
  - Require ``results.cid`` and a formatted Google Maps URI to scrape.
  - Navigate with Selenium, parse rating/review summary from the page.
  - On permanently closed detection, mark ``gapi_cache`` and skip future scrapes.
  - Flush cache updates at end of run.

Skip scraping (return record unchanged):
  - ``results.rating_type`` == ``"MANUAL"`` on the provider record
  - ``gapi_cache`` entry top-level ``is_permanently_closed`` == ``true`` → set
    ``record.is_permanently_closed`` and skip
  - ``gapi_cache`` entry top-level ``is_manually_blocked`` == ``true`` → set
    ``record.is_manually_blocked`` and skip

Side effects when permanently closed is detected on the page:
  - Set top-level ``is_permanently_closed`` == ``true`` on the provider record
  - Call ``gapi_cache.mark_permanently_closed()`` (top-level flag on cache shard)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import Driver

log = logging.getLogger("scraper")

REPO_ROOT = Path(__file__).resolve().parent.parent
AIRGRAB_DIR = REPO_ROOT / "airgrab"
GAPI_CACHE_DIR = AIRGRAB_DIR / "gapi_cache"
sys.path.insert(0, str(AIRGRAB_DIR))
from gapi_cache import GapiCache, is_manually_blocked_entry, is_permanently_closed_entry

DEFAULT_INPUT_DIR = AIRGRAB_DIR / "p3_batched_processed_gapi_details" / "output"
DEFAULT_OUTPUT_DIR = AIRGRAB_DIR / "p4_batched_scraper_details" / "output"
DEFAULT_ERROR_DIR = AIRGRAB_DIR / "p4_batched_scraper_details" / "output_errors"
MANIFEST_PATH = AIRGRAB_DIR / "p4_batched_scraper_details" / "scraper_run_mainfest.json"
BATCH_GLOB = "batch_*.json"
SUMMARY_WAIT_TIMEOUT = 10
COOKIE_DISMISS_TIMEOUT = 2
RATING_TYPE_MANUAL = "MANUAL"

COOKIE_BTN = (
    'button[aria-label*="Accept" i],'
    'button[jsname="hZCF7e"],'
    'button[data-mdc-dialog-action="accept"]'
)

_SUMMARY_RATING_REVIEWS_RE = re.compile(r"(\d+[.,]\d+)\s*\(\s*([\d][\d,.\s]*)\s*\)")
_RATING_ARIA_RE = re.compile(
    r"(?:rated\s+)?(\d+[.,]\d+|\d+)\s*(?:out of|/)\s*5|"
    r"(\d+[.,]\d+|\d+)\s*(?:stars?|sterne|étoiles|estrellas|stelle|"
    r"звезд|星|つ星|별)",
    re.IGNORECASE,
)
_REVIEW_COUNT_ARIA_RE = re.compile(
    r"([\d][\d,.\s]*)\s*(?:"
    r"reviews?|ratings?|bewertungen?|avis|opinions?|valoraciones?|"
    r"reseñas?|recensioni?|avaliações?|отзыв|レビュー|리뷰|评论|評論|"
    r"ביקורות|รีวิว|yorumlar?|değerlendirme|beoordelingen?|recenz)",
    re.IGNORECASE,
)


def _parse_decimal(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None


def _parse_int_count(value: str) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_rating_from_aria(label: str) -> Optional[float]:
    if not label:
        return None
    for match in _RATING_ARIA_RE.finditer(label):
        raw = match.group(1) or match.group(2)
        if raw:
            parsed = _parse_decimal(raw)
            if parsed is not None and 0 <= parsed <= 5:
                return parsed
    return None


def _parse_review_count_from_aria(label: str) -> Optional[int]:
    if not label:
        return None
    match = _REVIEW_COUNT_ARIA_RE.search(label)
    if match:
        return _parse_int_count(match.group(1))
    return None


def _parse_summary_rating_reviews_text(
    text: str,
) -> Tuple[Optional[float], Optional[int]]:
    if not text:
        return None, None
    match = _SUMMARY_RATING_REVIEWS_RE.search(text.replace("\n", " "))
    if not match:
        return None, None
    return _parse_decimal(match.group(1)), _parse_int_count(match.group(2))


class ScraperError(Exception):
    """Scrape failed for a single restaurant record."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "details": self.details}


class _DriverSessionLost(Exception):
    """Chrome/WebDriver session died mid-scrape."""


class _RateLimited(Exception):
    """Google served CAPTCHA / 429 / limited-view page."""


class GoogleReviewsScraper:
    """Scrape place-level rating and total review count from Google Maps."""

    _LIMITED_VIEW_STRINGS = (
        "limited view",
        "vue limitée",
        "eingeschränkte ansicht",
        "vista limitada",
        "vista limitata",
        "תצוגה מוגבלת",
        "มุมมองที่จำกัด",
        "ограниченный просмотр",
        "限定ビュー",
        "제한된 보기",
        "受限视图",
        "受限檢視",
        "عرض محدود",
        "sınırlı görünüm",
        "ograniczony widok",
        "beperkte weergave",
    )
    _PERMANENTLY_CLOSED_PHRASES = (
        "permanently closed",
        "closed permanently",
    )

    def __init__(
        self,
        config: Dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        *,
        headless: bool = True,
    ) -> None:
        config = config or {}
        self.headless = bool(config.get("headless", headless))
        self.url = config.get("url")
        self.cancel_event = cancel_event or threading.Event()
        self.place_rating: Optional[float] = None
        self.total_reviews: Optional[int] = None
        self._driver: Chrome | None = None
        self._wait: WebDriverWait | None = None
        self._maps_session_ready = False

    def setup_driver(self, headless: bool | None = None) -> Chrome:
        """Set up SeleniumBase UC Mode Chrome driver."""
        if headless is None:
            headless = self.headless

        log.info("Platform: %s", platform.platform())
        log.info("Python version: %s", platform.python_version())
        log.info("Using SeleniumBase UC Mode (headless=%s)", headless)

        in_container = os.environ.get("CHROME_BIN") is not None
        if in_container:
            chrome_binary = os.environ.get("CHROME_BIN")
            kwargs: dict[str, Any] = {
                "uc": True,
                "headless": headless,
                "page_load_strategy": "normal",
            }
            if chrome_binary and os.path.exists(chrome_binary):
                kwargs["binary_location"] = chrome_binary
            driver = Driver(**kwargs)
        else:
            driver = Driver(
                uc=True,
                headless=headless,
                page_load_strategy="normal",
                incognito=True,
            )

        driver.set_page_load_timeout(20)
        driver.set_window_size(1400, 900)

        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    """,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not apply stealth settings: %s", exc)

        return driver

    def start_driver(self) -> None:
        if self._driver is not None:
            return
        self._driver = self.setup_driver(self.headless)
        self._wait = WebDriverWait(self._driver, 20)

    def stop_driver(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:  # noqa: BLE001
            pass
        self._driver = None
        self._wait = None
        self._maps_session_ready = False

    def restart_driver(self) -> None:
        self.stop_driver()
        self._maps_session_ready = False
        self.start_driver()

    def _wait_for_place_summary(
        self,
        driver: Chrome,
        timeout: float = SUMMARY_WAIT_TIMEOUT,
    ) -> bool:
        """Wait until rating/review summary header is present."""
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'div[role="main"] div.F7nice, div.F7nice')
                )
            )
            return True
        except TimeoutException:
            return False

    def dismiss_cookies(self, driver: Chrome) -> bool:
        """Dismiss cookie consent dialogs if present."""
        try:
            WebDriverWait(driver, COOKIE_DISMISS_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COOKIE_BTN))
            )
            for elem in driver.find_elements(By.CSS_SELECTOR, COOKIE_BTN):
                try:
                    if elem.is_displayed():
                        elem.click()
                        log.info("Cookie dialog dismissed")
                        return True
                except Exception as exc:  # noqa: BLE001
                    log.debug("Error clicking cookie button: %s", exc)
        except TimeoutException:
            log.debug("No cookie consent dialog detected")
        except Exception as exc:  # noqa: BLE001
            log.debug("Error handling cookie dialog: %s", exc)
        return False

    def _place_name_from_url(self, url: str) -> str:
        """Parse place name from a /maps/place/ URL without loading the page."""
        import urllib.parse

        match = re.search(r"/maps/place/([^/@]+)", url)
        if not match:
            return ""
        name = urllib.parse.unquote(match.group(1))
        name = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", name)
        return name if len(name) > 2 else ""

    def _extract_place_coords(self, url: str) -> tuple:
        match = re.search(r"@(-?[\d.]+),(-?[\d.]+)", url)
        if match:
            return match.group(1), match.group(2)
        match = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", url)
        if match:
            return match.group(1), match.group(2)
        return None, None

    def _get_rating_and_total_reviews(self, driver: Chrome) -> Dict[str, Any]:
        rating: Optional[float] = None
        total_reviews: Optional[int] = None

        def _inside_review_card(element: WebElement) -> bool:
            try:
                return bool(
                    driver.execute_script(
                        "return arguments[0].closest('[data-review-id]') !== null;",
                        element,
                    )
                )
            except Exception:  # noqa: BLE001
                return False

        header_selectors = (
            'motion.div[role="main"] div.F7nice',
            'div[role="main"] div.F7nice',
            'motion.div[role="main"] div.fontBodyMedium',
            'div[role="main"] h1 ~ div',
        )
        for selector in header_selectors:
            try:
                for block in driver.find_elements(By.CSS_SELECTOR, selector):
                    if _inside_review_card(block):
                        continue
                    block_rating, block_count = _parse_summary_rating_reviews_text(
                        block.text or ""
                    )
                    if block_rating is not None:
                        rating = block_rating
                    if block_count is not None:
                        total_reviews = block_count
                    if rating is not None and total_reviews is not None:
                        break
                if rating is not None and total_reviews is not None:
                    break
            except Exception:  # noqa: BLE001
                continue

        aria_selectors = (
            'motion.div[role="main"] [role="img"][aria-label]',
            'div[role="main"] [role="img"][aria-label]',
            'motion.div[role="main"] button[aria-label]',
            'div[role="main"] button[aria-label]',
            'motion.div[role="main"] a[aria-label]',
            'motion.div[role="main"] span[aria-label]',
        )
        for selector in aria_selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if _inside_review_card(el):
                        continue
                    label = el.get_attribute("aria-label") or ""
                    if rating is None:
                        parsed_rating = _parse_rating_from_aria(label)
                        if parsed_rating is not None:
                            rating = parsed_rating
                    if total_reviews is None:
                        parsed_count = _parse_review_count_from_aria(label)
                        if parsed_count is not None:
                            total_reviews = parsed_count
                    if rating is not None and total_reviews is not None:
                        break
                if rating is not None and total_reviews is not None:
                    break
            except Exception:  # noqa: BLE001
                continue

        if rating is None:
            rating_selectors = (
                'motion.div[role="main"] div.F7nice span[aria-hidden="true"]',
                'div[role="main"] div.F7nice span[aria-hidden="true"]',
                'motion.div[role="main"] span.ceNzKf',
            )
            for selector in rating_selectors:
                try:
                    for span in driver.find_elements(By.CSS_SELECTOR, selector):
                        if _inside_review_card(span):
                            continue
                        parsed = _parse_decimal(span.text or "")
                        if parsed is not None and 0 <= parsed <= 5:
                            rating = parsed
                            break
                    if rating is not None:
                        break
                except Exception:  # noqa: BLE001
                    continue

        if total_reviews is None:
            count_selectors = (
                'motion.div[role="main"] div.F7nice a',
                'motion.div[role="main"] div.F7nice button',
                'motion.div[role="main"] div.F7nice span',
                'motion.div[role="main"] button[jsaction*="review"]',
            )
            for selector in count_selectors:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, selector):
                        if _inside_review_card(el):
                            continue
                        text = (el.text or "").strip()
                        match = re.search(r"\(\s*([\d][\d,.\s]*)\s*\)", text)
                        if match:
                            parsed = _parse_int_count(match.group(1))
                            if parsed is not None:
                                total_reviews = parsed
                                break
                    if total_reviews is not None:
                        break
                except Exception:  # noqa: BLE001
                    continue

        result = {"rating": rating, "total_reviews": total_reviews}
        self.place_rating = rating
        self.total_reviews = total_reviews
        log.info(
            "Place summary — rating=%s, total_reviews=%s",
            rating,
            total_reviews,
        )
        return result

    def _place_page_loaded(self, driver: Chrome) -> bool:
        if driver.find_elements(
            By.CSS_SELECTOR,
            'div[role="main"] div.F7nice, div.F7nice',
        ):
            return True
        if driver.find_elements(By.CSS_SELECTOR, 'div[role="main"] h1'):
            return True
        return False

    def _is_limited_view(self, driver: Chrome) -> bool:
        try:
            body_text = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:  # noqa: BLE001
            return False

        for phrase in self._LIMITED_VIEW_STRINGS:
            if phrase in body_text:
                return True

        try:
            sign_in_visible = bool(
                driver.find_elements(
                    By.CSS_SELECTOR,
                    'a[data-action="sign in"], a[href*="ServiceLogin"]',
                )
            )
            tab_present = bool(driver.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
            if sign_in_visible and not tab_present:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _is_permanently_closed(self, driver: Chrome) -> bool:
        texts: list[str] = []
        try:
            texts.append(driver.find_element(By.CSS_SELECTOR, 'div[role="main"]').text or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            texts.append(driver.find_element(By.TAG_NAME, "body").text or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, "[aria-label]"):
                label = element.get_attribute("aria-label") or ""
                if label:
                    texts.append(label)
        except Exception:  # noqa: BLE001
            pass

        combined = "\n".join(texts).casefold()
        return any(phrase in combined for phrase in self._PERMANENTLY_CLOSED_PHRASES)

    def navigate_to_place(
        self,
        driver: Chrome,
        url: str,
        wait: WebDriverWait,
        *,
        place_name: str | None = None,
    ) -> bool:
        """Navigate to a place page; prefer one load + wait over fixed sleeps."""
        import urllib.parse

        log.info("Navigating to place: %s", url)

        if not self._maps_session_ready:
            try:
                driver.get("https://www.google.com/maps")
                self.dismiss_cookies(driver)
            except Exception as exc:  # noqa: BLE001
                log.debug("Maps session warm-up failed: %s", exc)
            self._maps_session_ready = True

        driver.get(url)
        if self._wait_for_place_summary(driver):
            self.dismiss_cookies(driver)
            if self._is_limited_view(driver):
                log.warning("Google Maps limited view detected")
            return True

        search_name = (place_name or "").strip() or self._place_name_from_url(url)
        if search_name:
            query = urllib.parse.quote(search_name)
            lat, lng = self._extract_place_coords(url)
            if lat and lng:
                search_url = (
                    f"https://www.google.com/maps/search/{query}/@{lat},{lng},17z"
                )
            else:
                search_url = f"https://www.google.com/maps/search/{query}"

            log.info("Direct URL slow; trying search navigation: %s", search_url)
            driver.get(search_url)
            if self._wait_for_place_summary(driver):
                self.dismiss_cookies(driver)
                return True

        self.dismiss_cookies(driver)
        if self._is_limited_view(driver):
            log.warning("Google Maps limited view detected")
        return self._place_page_loaded(driver)

    def scrape_place_summary(
        self,
        url: str,
        *,
        place_name: str | None = None,
    ) -> Dict[str, Any]:
        """Navigate to a Maps URL and return rating + total_reviews."""
        if self._driver is None or self._wait is None:
            raise ScraperError("WebDriver not started; call start_driver() first")

        driver = self._driver
        wait = self._wait

        if self.cancel_event.is_set():
            raise ScraperError("Scrape cancelled")

        try:
            driver.execute_script("return 1")
        except (
            InvalidSessionIdException,
            NoSuchWindowException,
            WebDriverException,
        ) as exc:
            raise _DriverSessionLost(str(exc)) from exc

        self.navigate_to_place(driver, url, wait, place_name=place_name)
        if not self._wait_for_place_summary(driver, timeout=3):
            time.sleep(0.5)

        try:
            current_url = (driver.current_url or "").lower()
            if (
                "/sorry/" in current_url
                or "recaptcha" in current_url
                or "captcha" in current_url
            ):
                raise _RateLimited(f"rate-limit redirect: {current_url}")
        except WebDriverException:
            pass

        if self._is_permanently_closed(driver):
            return {
                "rating": None,
                "total_reviews": None,
                "is_permanently_closed": True,
            }

        summary = self._get_rating_and_total_reviews(driver)
        if summary["rating"] is None and summary["total_reviews"] is None:
            if self._is_permanently_closed(driver):
                return {
                    "rating": None,
                    "total_reviews": None,
                    "is_permanently_closed": True,
                }
            raise ScraperError("Could not parse rating or review count from the page")
        return summary

    def scrape(self) -> bool:
        """Legacy single-URL scrape (uses ``url`` from constructor config)."""
        if not self.url:
            log.error("No URL configured for scrape()")
            return False
        try:
            self.start_driver()
            summary = self.scrape_place_summary(self.url)
            return summary["rating"] is not None or summary["total_reviews"] is not None
        except Exception as exc:  # noqa: BLE001
            log.error("Scrape failed: %s", exc)
            log.error(traceback.format_exc())
            return False
        finally:
            self.stop_driver()


# --- Batch pipeline (airgrab p3) ---


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape rating and total_reviews for restaurants in "
            "p2_batched_gapi_details/output batch files."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"GAPI output batch directory (default: {DEFAULT_INPUT_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Successful output directory (default: {DEFAULT_OUTPUT_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--error-dir",
        type=Path,
        default=DEFAULT_ERROR_DIR,
        help=f"Error output directory (default: {DEFAULT_ERROR_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Chrome headless (default: true)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds between restaurant scrapes (default: 0.2)",
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
        raise FileNotFoundError(f"Input directory not found: {batch_dir}")
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


def relative_to_airgrab(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(AIRGRAB_DIR))
    except ValueError:
        return str(path.resolve())


def _empty_manifest() -> dict[str, Any]:
    return {"runs": []}


def load_manifest(path: Path) -> dict[str, Any]:
    """Load manifest; empty, missing, or invalid files start as {"runs": []}."""
    if not path.is_file():
        return _empty_manifest()

    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return _empty_manifest()

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


def maps_url_from_record(record: dict) -> str | None:
    results = record.get("results")
    if not isinstance(results, dict):
        return None
    uri = results.get("formatted_googleMapsUri") or results.get(
        "formatted_google_maps_uri"
    )
    return str(uri).strip() if uri else None


def has_scrapable_maps_result(record: dict) -> bool:
    results = record.get("results")
    if not isinstance(results, dict):
        return False
    cid = str(results.get("cid") or "").strip()
    maps_uri = maps_url_from_record(record)
    return bool(cid and maps_uri)


def merge_summary_into_results(record: dict, summary: dict[str, Any]) -> None:
    if not isinstance(record.get("results"), dict):
        record["results"] = {}
    record["results"]["rating"] = summary.get("rating")
    record["results"]["total_reviews"] = summary.get("total_reviews")
    record["results"]["is_gmaps_checked"] = True


def is_manual_rating_record(record: dict) -> bool:
    results = record.get("results")
    return (
        isinstance(results, dict)
        and str(results.get("rating_type") or "").strip().upper() == RATING_TYPE_MANUAL
    )


def mark_record_permanently_closed(
    record: dict[str, Any],
    *,
    cache: GapiCache | None,
) -> None:
    record["is_permanently_closed"] = True
    if isinstance(record.get("results"), dict):
        record["results"]["is_gmaps_checked"] = True
    if cache is not None:
        cache.mark_permanently_closed(record, marked_at=utc_now_iso())


def process_provider(
    provider: dict,
    scraper: GoogleReviewsScraper,
    *,
    cache: GapiCache | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    record = deepcopy(provider)
    if is_manual_rating_record(record):
        return record, None

    provider_id = str(record.get("id") or "").strip()
    if cache is not None and provider_id:
        cached_entry = cache.get_entry(provider_id)
        if cached_entry and is_permanently_closed_entry(cached_entry):
            record["is_permanently_closed"] = True
            return record, None
        if cached_entry and is_manually_blocked_entry(cached_entry):
            record["is_manually_blocked"] = True
            return record, None

    maps_url = maps_url_from_record(record)
    if not maps_url:
        return record, None

    place_name = str(provider.get("name") or "").strip() or None

    try:
        summary = scraper.scrape_place_summary(maps_url, place_name=place_name)
        if summary.get("is_permanently_closed"):
            mark_record_permanently_closed(record, cache=cache)
            return record, None
        merge_summary_into_results(record, summary)
        return record, None
    except (_DriverSessionLost, _RateLimited):
        raise
    except ScraperError as exc:
        record["scraper_error"] = exc.to_dict()
        return None, record
    except Exception as exc:  # noqa: BLE001
        record["scraper_error"] = ScraperError(str(exc)).to_dict()
        return None, record


def _batch_time_taken(start_perf: float) -> float:
    """Elapsed time for a batch, in minutes (2 decimal places)."""
    elapsed_seconds = time.perf_counter() - start_perf
    return round(elapsed_seconds / 60, 2)


def process_batch_file(
    batch_path: Path,
    scraper: GoogleReviewsScraper,
    *,
    output_dir: Path,
    error_dir: Path,
    delay: float,
    cache: GapiCache | None = None,
) -> dict[str, Any]:
    batch_started = utc_now_iso()
    batch_start_perf = time.perf_counter()
    batch_id = batch_id_from_path(batch_path)
    batch_number = batch_number_from_path(batch_path)
    input_file = relative_to_airgrab(batch_path)

    providers = load_batch(batch_path)
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    missing_results_formatted_googleMapsUri_count = 0

    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            errors.append(
                {
                    "index": index,
                    "scraper_error": {
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
        if not has_scrapable_maps_result(provider):
            successes.append(deepcopy(provider))
            missing_results_formatted_googleMapsUri_count += 1
            continue

        try:
            success, error = process_provider(provider, scraper, cache=cache)
        except _DriverSessionLost:
            scraper.restart_driver()
            success, error = process_provider(provider, scraper, cache=cache)
        except _RateLimited as exc:
            raise ScraperError(f"Rate limited: {exc}") from exc

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
        "output_file": relative_to_airgrab(output_path) if successes else None,
        "error_file": relative_to_airgrab(error_path) if errors else None,
        "status": status,
        "started_at": batch_started,
        "finished_at": utc_now_iso(),
        "time_taken": _batch_time_taken(batch_start_perf),
        "restaurant_count": len(providers),
        "success_count": success_count,
        "error_count": error_count,
        "missing_results_formatted_googleMapsUri_count": (
            missing_results_formatted_googleMapsUri_count
        ),
    }


def run_batches(
    *,
    input_dir: Path,
    output_dir: Path,
    error_dir: Path,
    headless: bool,
    delay: float,
    limit_batches: int | None = None,
) -> int:
    batch_files = list_batch_files(input_dir)
    if limit_batches is not None:
        batch_files = batch_files[:limit_batches]

    run_started = utc_now_iso()
    run_start_perf = time.perf_counter()
    batch_results: list[dict[str, Any]] = []
    cache = GapiCache(GAPI_CACHE_DIR)
    scraper = GoogleReviewsScraper(headless=headless)

    try:
        scraper.start_driver()
        for batch_path in batch_files:
            print(f"Processing {batch_path.name} ...")
            batch_started = utc_now_iso()
            batch_start_perf = time.perf_counter()
            try:
                result = process_batch_file(
                    batch_path,
                    scraper,
                    output_dir=output_dir,
                    error_dir=error_dir,
                    delay=delay,
                    cache=cache,
                )
            except (OSError, json.JSONDecodeError, ValueError, ScraperError) as exc:
                result = {
                    "batch_id": batch_id_from_path(batch_path),
                    "input_file": relative_to_airgrab(batch_path),
                    "output_file": None,
                    "error_file": None,
                    "status": "error",
                    "started_at": batch_started,
                    "finished_at": utc_now_iso(),
                    "time_taken": _batch_time_taken(batch_start_perf),
                    "restaurant_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "batch_error": str(exc),
                }
                print(f"  batch failed: {exc}", file=sys.stderr)
            except _DriverSessionLost:
                scraper.restart_driver()
                try:
                    result = process_batch_file(
                        batch_path,
                        scraper,
                        output_dir=output_dir,
                        error_dir=error_dir,
                        delay=delay,
                        cache=cache,
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    result = {
                        "batch_id": batch_id_from_path(batch_path),
                        "input_file": relative_to_airgrab(batch_path),
                        "output_file": None,
                        "error_file": None,
                        "status": "error",
                        "started_at": batch_started,
                        "finished_at": utc_now_iso(),
                        "time_taken": _batch_time_taken(batch_start_perf),
                        "restaurant_count": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "batch_error": str(retry_exc),
                    }
                    print(
                        f"  batch failed after driver restart: {retry_exc}",
                        file=sys.stderr,
                    )

            batch_results.append(result)
            print(
                f"  {result['status']}: "
                f"{result['success_count']} ok, {result['error_count']} error(s), "
                f"time_taken={result.get('time_taken')} min"
            )
    finally:
        scraper.stop_driver()
        try:
            cache.flush()
        except OSError as exc:
            print(f"Warning: could not flush gapi cache: {exc}", file=sys.stderr)

    successful_batches = sum(1 for b in batch_results if b["status"] == "success")
    error_batches = len(batch_results) - successful_batches
    missing_results_formatted_googleMapsUri_count = sum(
        b.get("missing_results_formatted_googleMapsUri_count", 0) for b in batch_results
    )

    run_record = {
        "run_id": run_started,
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "time_taken": _batch_time_taken(run_start_perf),
        "total_batches_count": len(batch_results),
        "successful_batches_count": successful_batches,
        "error_batches_count": error_batches,
        "missing_results_formatted_googleMapsUri_count": (
            missing_results_formatted_googleMapsUri_count
        ),
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
        f"successful, time_taken={run_record['time_taken']} min. "
        f"Manifest: {MANIFEST_PATH.relative_to(REPO_ROOT)}"
    )
    return 0 if error_batches == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_batches(
        input_dir=args.input_dir.resolve(),
        output_dir=args.output_dir.resolve(),
        error_dir=args.error_dir.resolve(),
        headless=args.headless,
        delay=args.delay,
        limit_batches=args.limit_batches,
    )


if __name__ == "__main__":
    raise SystemExit(main())
