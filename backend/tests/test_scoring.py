"""Tests for the extraction scoring harness.

The harness exists to produce one number — how often the extractor was sure
and wrong — so these tests are mostly about that classification being right.
Getting the scoring wrong would be worse than not scoring at all: it would
give false confidence in the thing the whole design is defending against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from scripts.score_extraction import (
    Outcome,
    Report,
    normalise,
    score_one,
    values_match,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        confidence_threshold_default=0.80,
        confidence_threshold_overrides={"material": 0.85, "quantity": 0.90},
    )


def payload(**fields) -> dict:
    """Build an extractor payload: name=(value, confidence)."""
    return {
        name: {"value": value, "confidence": confidence, "evidence": "test"}
        for name, (value, confidence) in fields.items()
    }


def score(truth: dict, extracted: dict, settings) -> dict[str, str]:
    results = score_one(Path("4471.pdf"), truth, extracted, settings)
    return {r.field_name: r.outcome for r in results}


# --------------------------------------------------------------------------
# The number that matters
# --------------------------------------------------------------------------
def test_a_wrong_value_read_confidently_is_the_headline_failure(settings):
    outcomes = score(
        {"material": "1.2312"},
        payload(material=("1.2344", 0.96)),
        settings,
    )
    assert outcomes["material"] == Outcome.CONFIDENTLY_WRONG


def test_a_wrong_value_below_threshold_is_caught_not_counted_as_wrong(settings):
    """The safety net working is not the same failure as the net not existing."""
    outcomes = score(
        {"material": "1.2312"},
        payload(material=("1.2344", 0.55)),
        settings,
    )
    assert outcomes["material"] == Outcome.WITHHELD_AND_WOULD_HAVE_BEEN_WRONG


def test_a_right_value_withheld_is_recorded_as_a_cost_not_a_win(settings):
    outcomes = score(
        {"material": "1.2312"},
        payload(material=("1.2312", 0.55)),
        settings,
    )
    assert outcomes["material"] == Outcome.WITHHELD_BUT_WAS_RIGHT


def test_a_correct_confident_read_is_correct(settings):
    outcomes = score({"material": "1.2312"}, payload(material=("1.2312", 0.96)), settings)
    assert outcomes["material"] == Outcome.CORRECT


def test_an_honest_null_is_unread_not_wrong(settings):
    outcomes = score({"material": "1.2312"}, payload(material=(None, None)), settings)
    assert outcomes["material"] == Outcome.UNREAD


def test_fields_absent_from_the_truth_are_not_scored(settings):
    """Scoring against a value the drawing never carried would punish honesty."""
    results = score_one(
        Path("x.pdf"), {"material": "1.2312"}, payload(revision=("B", 0.99)), settings
    )
    assert [r.field_name for r in results] == ["material"]


# --------------------------------------------------------------------------
# Comparing like an estimator, not like a string
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("truth", "extracted"),
    [
        ("1.2312", " 1.2312 "),
        ("H7", "h7"),
        ("±0.05", "+/-0.05"),
        ("52-54 HRC", "52-54 hrc"),
        ("+0.000/-0.013", "+0.000/-0.013"),
    ],
)
def test_cosmetic_differences_are_not_errors(truth, extracted):
    assert values_match("tightest_tolerance", truth, extracted)


def test_a_genuinely_different_tolerance_is_an_error():
    assert not values_match("tightest_tolerance", "+0.000/-0.013", "+0.000/-0.13")


def test_envelope_dimensions_allow_a_small_reading_difference():
    """119.98 read as 120 is not a mistake an estimator would care about."""
    assert values_match("envelope_x", 120, 119.98)
    assert values_match("envelope_x", 120, 120)
    assert not values_match("envelope_x", 120, 150)


def test_quantity_is_not_fuzzy_in_any_way_that_matters():
    assert values_match("quantity", 4, 4)
    assert not values_match("quantity", 4, 6)


def test_normalise_handles_the_dash_and_plusminus_variants():
    assert normalise("±0.05") == normalise("+/-0.05")
    assert normalise("52–54") == normalise("52-54")


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------
def test_the_report_counts_only_scored_fields(settings):
    report = Report()
    report.results.extend(
        score_one(
            Path("a.pdf"),
            {"material": "1.2312", "revision": "B"},
            payload(material=("1.2344", 0.96), revision=("B", 0.99)),
            settings,
        )
    )
    assert len(report.scored()) == 2
    assert report.count(Outcome.CONFIDENTLY_WRONG) == 1
    assert report.count(Outcome.CORRECT) == 1
