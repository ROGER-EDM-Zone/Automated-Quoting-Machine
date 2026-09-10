"""The estimator's judgement, checked against what he actually said."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.judgement import (
    BENCHMARKS,
    CHECKLIST,
    LADDER_STEP,
    LONG_RUNNER_GATES,
    LONG_RUNNER_THRESHOLD_HOURS,
    MANDATORY_KEYS,
    JudgementError,
    is_long_runner,
    material_warning,
    rate_ladder,
    unanswered_mandatory,
)


# --------------------------------------------------------------------------
# The long-runner ladder
# --------------------------------------------------------------------------
def test_ladder_reproduces_the_table_he_quoted():
    """His words: at 20 hours, £50 / £52.50 / £55 / £57.50 / £60 gives
    £1,000 / £1,050 / £1,100 / £1,150 / £1,200."""
    rungs = rate_ladder(D("60"), D("20"))
    assert [r.hourly_rate for r in rungs] == [
        D("50.00"), D("52.50"), D("55.00"), D("57.50"), D("60.00")
    ]
    assert [r.value for r in rungs] == [
        D("1000.00"), D("1050.00"), D("1100.00"), D("1150.00"), D("1200.00")
    ]


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(D("10"), [D("500.00"), D("525.00"), D("550.00"), D("575.00"), D("600.00")]),
     (D("40"), [D("2000.00"), D("2100.00"), D("2200.00"), D("2300.00"), D("2400.00")])],
)
def test_ladder_matches_his_other_two_columns(hours, expected):
    assert [r.value for r in rate_ladder(D("60"), hours)] == expected


def test_ladder_rebases_onto_whatever_rate_the_shop_charges():
    """The £2.50 steps are his; £60 was only where he started from. At a
    different standard rate the ladder must move with it, not stay put."""
    rungs = rate_ladder(D("65"), D("10"))
    assert [r.hourly_rate for r in rungs] == [
        D("55.00"), D("57.50"), D("60.00"), D("62.50"), D("65.00")
    ]


def test_exactly_one_rung_is_the_standard_rate_and_it_is_the_dearest():
    rungs = rate_ladder(D("65"), D("12"))
    assert sum(1 for r in rungs if r.is_standard) == 1
    assert rungs[-1].is_standard
    assert rungs[-1].hourly_rate == D("65.00")


def test_ladder_never_returns_a_rate_at_or_below_zero():
    """A very low standard rate must not produce a free or negative rung."""
    rungs = rate_ladder(D("5"), D("10"))
    assert all(r.hourly_rate > 0 for r in rungs)
    assert rungs[-1].hourly_rate == D("5.00")


def test_ladder_refuses_nonsense():
    with pytest.raises(JudgementError):
        rate_ladder(D("0"), D("10"))
    with pytest.raises(JudgementError):
        rate_ladder(D("65"), D("-1"))


def test_the_ladder_does_not_choose_for_him():
    """He asked to see every rate and then pick. Returning one would throw
    away the judgement this module exists to keep."""
    assert len(rate_ladder(D("60"), D("20"))) == 5


def test_ten_hours_is_the_threshold_and_it_is_inclusive():
    assert not is_long_runner(D("9.99"))
    assert is_long_runner(LONG_RUNNER_THRESHOLD_HOURS)
    assert is_long_runner(D("40"))


def test_a_reduced_rate_has_gates_attached():
    """The discount is never automatic — 'over 10 hours = £55' is precisely
    the rule he did not want taught."""
    assert len(LONG_RUNNER_GATES) == 6
    keys = {q.key for q in LONG_RUNNER_GATES}
    assert {"runs_unattended", "low_scrap_risk", "simple_setup"} <= keys


def test_ladder_step_is_two_pounds_fifty():
    assert LADDER_STEP == D("2.50")


# --------------------------------------------------------------------------
# The checklist
# --------------------------------------------------------------------------
def test_all_eight_sections_in_his_order():
    assert [s.number for s in CHECKLIST] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [s.title for s in CHECKLIST][:2] == ["Basic job information", "Material"]
    assert CHECKLIST[-1].title == "Commercial sense-check"


def test_workholding_and_distortion_come_before_the_time_calculation():
    """Both change the time. Asked afterwards, a checklist only records
    regret."""
    order = {s.title: s.number for s in CHECKLIST}
    assert order["Workholding"] < order["Time calculation"]
    assert order["Movement and distortion"] < order["Time calculation"]


def test_how_are_we_going_to_hold_it_is_compulsory():
    """He singled this one out: the question an inexperienced estimator skips
    while concentrating on the drawing."""
    workholding = next(s for s in CHECKLIST if s.title == "Workholding")
    hold = next(q for q in workholding.questions if q.key == "how_held")
    assert hold.mandatory
    assert "hold this" in hold.text


def test_the_commercial_gut_check_is_compulsory():
    assert "feels_right" in MANDATORY_KEYS
    assert "historical_price" in MANDATORY_KEYS


def test_question_keys_are_unique_across_the_whole_checklist():
    """Answers are recorded against the key, so a duplicate silently
    overwrites another section's answer."""
    keys = [q.key for s in CHECKLIST for q in s.questions]
    assert len(keys) == len(set(keys))


def test_most_questions_are_not_mandatory():
    """Make everything compulsory and people tick blindly."""
    total = sum(len(s.questions) for s in CHECKLIST)
    assert 0 < len(MANDATORY_KEYS) < total / 2


def test_a_blank_answer_is_not_an_answer():
    assert {q.key for q in unanswered_mandatory({})} == MANDATORY_KEYS
    answers = dict.fromkeys(MANDATORY_KEYS, "yes")
    assert unanswered_mandatory(answers) == ()
    answers["how_held"] = "   "
    assert [q.key for q in unanswered_mandatory(answers)] == ["how_held"]
    answers["how_held"] = None
    assert [q.key for q in unanswered_mandatory(answers)] == ["how_held"]


def test_not_applicable_counts_as_answered():
    """The mechanism is making somebody say N/A out loud, not skip past."""
    answers = dict.fromkeys(MANDATORY_KEYS, "N/A")
    assert unanswered_mandatory(answers) == ()


# --------------------------------------------------------------------------
# Materials that do not cut like steel
# --------------------------------------------------------------------------
def test_phosphor_bronze_warns_in_his_own_words():
    result = material_warning("Phosphor Bronze PB1")
    assert result is not None
    multiplier, words = result
    assert multiplier > 1
    assert "PHOSPHOR BRONZE" in words
    assert "slower" in words


@pytest.mark.parametrize("spec", ["Titanium Grade 5", "TUNGSTEN CARBIDE", "C101 Copper"])
def test_the_difficult_materials_he_named_are_all_caught(spec):
    assert material_warning(spec) is not None


@pytest.mark.parametrize("spec", ["EN16", "1.2312", "304 stainless", None, ""])
def test_ordinary_steels_get_no_warning(spec):
    assert material_warning(spec) is None


def test_every_multiplier_makes_the_job_slower_not_faster():
    for spec in ("phosphor bronze", "titanium", "tungsten", "copper", "carbide"):
        multiplier, _ = material_warning(spec)
        assert multiplier > 1, spec


# --------------------------------------------------------------------------
# Benchmarks
# --------------------------------------------------------------------------
def test_the_comet_keyways_are_recorded():
    """His example of a price the calculator would not have arrived at."""
    comet = next(b for b in BENCHMARKS if "COMET" in b.description)
    assert comet.quantity == 50
    assert comet.price_each == D("16.00")


def test_a_benchmark_without_its_reasoning_says_so():
    """A remembered number with no reason attached is the thing this system
    is built to distrust."""
    comet = next(b for b in BENCHMARKS if "COMET" in b.description)
    assert "has not been recorded" in comet.note


def test_judgement_makes_no_network_or_ai_calls():
    import app.judgement as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("anthropic", "requests", "httpx", "urllib", "openai"):
        assert forbidden not in source, f"judgement must not reference {forbidden}"
