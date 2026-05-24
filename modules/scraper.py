"""
Selenium scraping logic for Google Maps place summary (rating + review count).
Uses SeleniumBase UC Mode for enhanced anti-detection and Chrome version management.
"""

import logging
import os
import platform
import re
import threading
import time
import traceback
from typing import Dict, Any, Optional, Tuple

from seleniumbase import Driver
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

from modules.review_db import ReviewDB

log = logging.getLogger("scraper")

COOKIE_BTN = (
    'button[aria-label*="Accept" i],'
    'button[jsname="hZCF7e"],'
    'button[data-mdc-dialog-action="accept"]'
)

_SUMMARY_RATING_REVIEWS_RE = re.compile(
    r"(\d+[.,]\d+)\s*\(\s*([\d][\d,.\s]*)\s*\)"
)
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


def _parse_summary_rating_reviews_text(text: str) -> Tuple[Optional[float], Optional[int]]:
    if not text:
        return None, None
    match = _SUMMARY_RATING_REVIEWS_RE.search(text.replace("\n", " "))
    if not match:
        return None, None
    return _parse_decimal(match.group(1)), _parse_int_count(match.group(2))


class _DriverSessionLost(Exception):
    """Chrome/WebDriver session died mid-scrape."""
    pass


class _RateLimited(Exception):
    """Google served CAPTCHA / 429 / limited-view page."""
    pass


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
        "受限视图", "受限檢視",
        "عرض محدود",
        "sınırlı görünüm",
        "ograniczony widok",
        "beperkte weergave",
    )

    def __init__(self, config: Dict[str, Any],
                 cancel_event: threading.Event | None = None):
        self.config = config
        self.cancel_event = cancel_event or threading.Event()
        db_path = config.get("db_path", "reviews.db")
        self.review_db = ReviewDB(db_path)
        self.place_rating: Optional[float] = None
        self.total_reviews: Optional[int] = None

    def setup_driver(self, headless: bool) -> Chrome:
        """Set up SeleniumBase UC Mode Chrome driver."""
        log.info(f"Platform: {platform.platform()}")
        log.info(f"Python version: {platform.python_version()}")
        log.info("Using SeleniumBase UC Mode for enhanced anti-detection")

        in_container = os.environ.get("CHROME_BIN") is not None
        if in_container:
            chrome_binary = os.environ.get("CHROME_BIN")
            kwargs = {"uc": True, "headless": headless, "page_load_strategy": "normal"}
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

        driver.set_page_load_timeout(30)
        driver.set_window_size(1400, 900)

        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                """,
            })
        except Exception as e:  # noqa: BLE001
            log.debug(f"Could not apply stealth settings: {e}")

        return driver

    def dismiss_cookies(self, driver: Chrome) -> bool:
        """Dismiss cookie consent dialogs if present."""
        try:
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COOKIE_BTN))
            )
            for elem in driver.find_elements(By.CSS_SELECTOR, COOKIE_BTN):
                try:
                    if elem.is_displayed():
                        elem.click()
                        log.info("Cookie dialog dismissed")
                        return True
                except Exception as e:  # noqa: BLE001
                    log.debug(f"Error clicking cookie button: {e}")
        except TimeoutException:
            log.debug("No cookie consent dialog detected")
        except Exception as e:  # noqa: BLE001
            log.debug(f"Error handling cookie dialog: {e}")
        return False

    def _extract_place_name(self, driver: Chrome, url: str) -> str:
        """Extract place name from URL or page title."""
        import urllib.parse

        match = re.search(r"/maps/place/([^/@]+)", url)
        if match:
            name = urllib.parse.unquote(match.group(1))
            name = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", name)
            if len(name) > 2:
                return name

        try:
            driver.get(url)
            time.sleep(4)
            title = driver.title or ""
            name = title.replace(" - Google Maps", "").strip()
            name = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", name)
            if name:
                return name
        except Exception as e:  # noqa: BLE001
            log.debug(f"Could not extract place name from page: {e}")
        return ""

    def _extract_place_coords(self, url: str) -> tuple:
        """Extract lat/lng from a Google Maps URL."""
        match = re.search(r"@(-?[\d.]+),(-?[\d.]+)", url)
        if match:
            return match.group(1), match.group(2)
        match = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", url)
        if match:
            return match.group(1), match.group(2)
        return None, None

    def _get_rating_and_total_reviews(self, driver: Chrome) -> Dict[str, Any]:
        """
        Read place-level rating and total review count from the Maps header DOM
        (e.g. ``2.7 (35)``). Does not iterate individual review cards.
        """
        rating: Optional[float] = None
        total_reviews: Optional[int] = None

        def _inside_review_card(element: WebElement) -> bool:
            try:
                return bool(driver.execute_script(
                    "return arguments[0].closest('[data-review-id]') !== null;",
                    element,
                ))
            except Exception:  # noqa: BLE001
                return False

        header_selectors = (
            "motion.div[role=\"main\"] div.F7nice",
            "div[role=\"main\"] div.F7nice",
            "motion.div[role=\"main\"] div.fontBodyMedium",
            "div[role=\"main\"] h1 ~ div",
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
            "motion.div[role=\"main\"] [role=\"img\"][aria-label]",
            "div[role=\"main\"] [role=\"img\"][aria-label]",
            "motion.div[role=\"main\"] button[aria-label]",
            "div[role=\"main\"] button[aria-label]",
            "motion.div[role=\"main\"] a[aria-label]",
            "motion.div[role=\"main\"] span[aria-label]",
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
                "motion.div[role=\"main\"] div.F7nice span[aria-hidden=\"true\"]",
                "div[role=\"main\"] div.F7nice span[aria-hidden=\"true\"]",
                "motion.div[role=\"main\"] span.ceNzKf",
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
                "motion.div[role=\"main\"] div.F7nice a",
                "motion.div[role=\"main\"] div.F7nice button",
                "motion.div[role=\"main\"] div.F7nice span",
                "motion.div[role=\"main\"] button[jsaction*=\"review\"]",
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
        print(
            f"summary_rating_reviews: rating={rating}, total_reviews={total_reviews}"
        )
        log.info(
            "Place summary from DOM — rating=%s, total_reviews=%s",
            rating, total_reviews,
        )
        self.place_rating = rating
        self.total_reviews = total_reviews
        return result

    def _place_page_loaded(self, driver: Chrome) -> bool:
        """True when the place header (rating row or title) is visible."""
        if driver.find_elements(
            By.CSS_SELECTOR,
            'div[role="main"] div.F7nice, div.F7nice',
        ):
            return True
        if driver.find_elements(By.CSS_SELECTOR, 'div[role="main"] h1'):
            return True
        return False

    def _is_limited_view(self, driver: Chrome) -> bool:
        """Detect limited-view restriction across languages + structure."""
        try:
            body_text = (
                driver.find_element(By.TAG_NAME, "body").text or ""
            ).lower()
        except Exception:  # noqa: BLE001
            return False

        for phrase in self._LIMITED_VIEW_STRINGS:
            if phrase in body_text:
                return True

        try:
            sign_in_visible = bool(driver.find_elements(
                By.CSS_SELECTOR,
                'a[data-action="sign in"], a[href*="ServiceLogin"]',
            ))
            tab_present = bool(driver.find_elements(
                By.CSS_SELECTOR, '[role="tab"]'
            ))
            if sign_in_visible and not tab_present:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def navigate_to_place(self, driver: Chrome, url: str, wait: WebDriverWait) -> bool:
        """Navigate to a Google Maps place page."""
        log.info("Navigating to place with limited-view bypass...")

        try:
            driver.get("https://www.google.com")
            time.sleep(2)
            self.dismiss_cookies(driver)
        except Exception as e:  # noqa: BLE001
            log.debug(f"Warm-up navigation failed: {e}")

        place_name = self._extract_place_name(driver, url)
        current_url = driver.current_url

        if place_name:
            lat, lng = self._extract_place_coords(current_url)
            if lat and lng:
                search_url = (
                    f"https://www.google.com/maps/search/{place_name}/@{lat},{lng},17z"
                )
            else:
                search_url = f"https://www.google.com/maps/search/{place_name}/"

            log.info(f"Trying search-based navigation: {search_url}")
            driver.get(search_url)
            time.sleep(5)

            if self._place_page_loaded(driver):
                log.info("Search-based navigation successful — place page loaded")
                self.dismiss_cookies(driver)
                return True

            log.info("Search-based navigation did not show place header, trying direct URL...")

        log.info(f"Navigating directly to: {url}")
        driver.get(url)
        try:
            wait.until(lambda d: "google.com/maps" in d.current_url)
        except TimeoutException:
            log.warning("Timed out waiting for Google Maps to load")
        time.sleep(3)
        self.dismiss_cookies(driver)

        if self._is_limited_view(driver):
            log.warning(
                "Google Maps is showing a limited view — summary may be unavailable"
            )

        return True

    def scrape(self) -> bool:
        """Open Maps, navigate to the place, print summary_rating_reviews."""
        resilience = self.config.get("resilience", {}) or {}
        max_retries = int(resilience.get("retry_on_session_death", 1))
        backoff_base = int(resilience.get("retry_backoff_base_seconds", 3))

        for attempt in range(max_retries + 1):
            try:
                return self._scrape_once()
            except _DriverSessionLost as e:
                if attempt >= max_retries:
                    log.error("Driver session lost, retries exhausted: %s", e)
                    return False
                delay = backoff_base * (3 ** attempt)
                log.warning(
                    "Driver session lost (attempt %d/%d) — retrying in %ds",
                    attempt + 1, max_retries + 1, delay,
                )
                time.sleep(delay)
            except _RateLimited as e:
                cooldown = int(resilience.get("rate_limit_cooldown_seconds", 60))
                log.warning("Rate-limit signal: %s. Sleeping %ds", e, cooldown)
                time.sleep(cooldown)
                return False
            except InterruptedError:
                log.info("Scrape cancelled — not retrying")
                return False
        return False

    def _scrape_once(self) -> bool:
        """Single attempt: navigate and read rating / review count from header."""
        start_time = time.time()
        url = self.config.get("url")
        headless = self.config.get("headless", True)

        log.info(f"Starting summary scrape: headless={headless}")
        log.info(f"URL: {url}")

        driver = None
        try:
            driver = self.setup_driver(headless)
            wait = WebDriverWait(driver, 20)

            self.navigate_to_place(driver, url, wait)
            self.dismiss_cookies(driver)

            try:
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

            try:
                driver.execute_script("return 1")
            except (InvalidSessionIdException, NoSuchWindowException,
                    WebDriverException) as probe_err:
                raise _DriverSessionLost(str(probe_err)) from probe_err

            try:
                current_url = (driver.current_url or "").lower()
                if "/sorry/" in current_url or "recaptcha" in current_url or "captcha" in current_url:
                    raise _RateLimited(f"rate-limit redirect: {current_url}")
            except WebDriverException:
                pass

            if self.cancel_event.is_set():
                raise InterruptedError("Scrape cancelled")

            result = self._get_rating_and_total_reviews(driver)
            ok = result["rating"] is not None or result["total_reviews"] is not None
            if not ok:
                log.warning("Could not parse rating or review count from the page")
                return False

            log.info("Summary scrape completed in %.2f seconds", time.time() - start_time)
            return True

        except (_DriverSessionLost, _RateLimited, InterruptedError):
            raise
        except Exception as e:
            log.error(f"Error during summary scrape: {e}")
            log.error(traceback.format_exc())
            return False
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
