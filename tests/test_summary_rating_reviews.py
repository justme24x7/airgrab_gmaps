"""Tests for place-header rating / review-count parsing helpers."""

import pytest

from modules.scraper import (
    _parse_decimal,
    _parse_int_count,
    _parse_rating_from_aria,
    _parse_review_count_from_aria,
    _parse_summary_rating_reviews_text,
)


class TestParseDecimal:
    def test_dot(self):
        assert _parse_decimal("2.7") == 2.7

    def test_comma(self):
        assert _parse_decimal("4,5") == 4.5


class TestParseIntCount:
    def test_plain(self):
        assert _parse_int_count("35") == 35

    def test_thousands(self):
        assert _parse_int_count("1,234") == 1234


class TestSummaryText:
    def test_rating_and_count(self):
        rating, count = _parse_summary_rating_reviews_text("2.7 (35)")
        assert rating == 2.7
        assert count == 35

    def test_with_extra_context(self):
        rating, count = _parse_summary_rating_reviews_text(
            "Ice cream shop · 2.7 (35) · ₹₹"
        )
        assert rating == 2.7
        assert count == 35


class TestAriaLabels:
    def test_stars_english(self):
        assert _parse_rating_from_aria("2.7 stars") == 2.7

    def test_out_of_five(self):
        assert _parse_rating_from_aria("Rated 4.2 out of 5") == 4.2

    def test_review_count_english(self):
        assert _parse_review_count_from_aria("35 reviews") == 35

    def test_review_count_french(self):
        assert _parse_review_count_from_aria("35 avis") == 35
