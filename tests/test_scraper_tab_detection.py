"""
Regression tests for summary rating / review-count parsing helpers in scraper.py.
"""

from modules.scraper import (
    GoogleReviewsScraper,
    _parse_rating_from_aria,
    _parse_review_count_from_aria,
    _parse_summary_rating_reviews_text,
)


class TestSummaryParsing:
    def test_rating_and_count_text(self):
        rating, count = _parse_summary_rating_reviews_text("2.7 (35)")
        assert rating == 2.7
        assert count == 35

    def test_stars_aria(self):
        assert _parse_rating_from_aria("2.7 stars") == 2.7

    def test_reviews_aria(self):
        assert _parse_review_count_from_aria("35 reviews") == 35


class TestScraperInit:
    def test_minimal_config(self):
        scraper = GoogleReviewsScraper({"url": "https://maps.google.com/", "db_path": ":memory:"})
        assert scraper.place_rating is None
        assert scraper.total_reviews is None
