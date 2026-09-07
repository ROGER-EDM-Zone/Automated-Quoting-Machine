"""The wire calculator, against EDM Zone's own cutting speeds.

These are not invented fixtures (unlike most figures in this suite). The
speeds come from the shop's costing spreadsheet, so a test that changes one
of them is a test that changes what customers get quoted.
"""

from decimal import Decimal as D

import pytest

from app.calculators import (
    FINISH_CUT_SPEED,
    FIRST_CUT_SPEED,
    MINUTES_PER_THREAD,
    SUSPECT_FINISH_HEIGHTS,
    CalculatorError,
    WireCut,
    speed_at,
    wire_cut_time,
)


# --------------------------------------------------------------------------
# The speed tables themselves
# --------------------------------------------------------------------------
def test_the_roughing_table_only_ever_slows_as_the_job_gets_taller():
    """Physics, and a guard against a mistyped row: a taller job cannot cut
    faster than a shorter one."""
    speeds = [FIRST_CUT_SPEED[h] for h in sorted(FIRST_CUT_SPEED)]
    assert speeds == sorted(speeds, reverse=True)


def test_the_finish_table_has_exactly_one_row_that_disagrees():
    """The shop's sheet reads 2.7 at 150mm, between two 2.5s. It is kept as
    written rather than quietly corrected, and named so a quote can say so."""
    heights = sorted(FINISH_CUT_SPEED)
    wrong = {
        heights[i]
        for i in range(1, len(heights))
        if FINISH_CUT_SPEED[heights[i]] > FINISH_CUT_SPEED[heights[i - 1]]
    }
    assert wrong == SUSPECT_FINISH_HEIGHTS


def test_skims_are_always_faster_than_the_roughing_pass():
    for height in set(FIRST_CUT_SPEED) & set(FINISH_CUT_SPEED):
        assert FINISH_CUT_SPEED[height] >= FIRST_CUT_SPEED[height], height


# --------------------------------------------------------------------------
# Looking a speed up
# --------------------------------------------------------------------------
def test_a_height_on_the_table_is_read_straight_off_it():
    speed, interpolated = speed_at(D("50"), FIRST_CUT_SPEED)
    assert speed == D("2.7")
    assert interpolated is False


def test_a_height_between_rows_is_interpolated_and_says_so():
    # Halfway between 100mm (1.0) and 125mm (0.85).
    speed, interpolated = speed_at(D("112.5"), FIRST_CUT_SPEED)
    assert D("0.9") < speed < D("0.95")
    assert interpolated is True


def test_a_job_taller_than_anything_measured_is_refused_not_guessed():
    with pytest.raises(CalculatorError, match="outside the speeds"):
        speed_at(D("500"), FIRST_CUT_SPEED)


def test_a_very_thin_job_holds_the_fastest_measured_speed():
    # A 2mm job does not cut four times faster than a 5mm one just because
    # the arithmetic would allow it.
    speed, interpolated = speed_at(D("2"), FIRST_CUT_SPEED)
    assert speed == FIRST_CUT_SPEED[5]
    assert interpolated is True


def test_a_zero_or_negative_height_is_refused():
    for height in (D("0"), D("-10")):
        with pytest.raises(CalculatorError, match="greater than zero"):
            speed_at(height, FIRST_CUT_SPEED)


# --------------------------------------------------------------------------
# Timing a job
# --------------------------------------------------------------------------
def test_the_arithmetic_is_path_length_over_speed():
    # 50mm tall cuts at 2.7mm/min, so 270mm of path is 100 minutes.
    result = wire_cut_time(WireCut(D("50"), D("270"), finish_cuts=0, threads=1))
    assert result.first_cut_mins == D("100.00")
    assert result.finish_cut_mins == D("0.00")


def test_every_skim_travels_the_whole_path_again():
    one = wire_cut_time(WireCut(D("50"), D("270"), finish_cuts=1, threads=1))
    two = wire_cut_time(WireCut(D("50"), D("270"), finish_cuts=2, threads=1))
    assert two.finish_cut_mins == one.finish_cut_mins * 2


def test_each_internal_shape_costs_another_thread():
    one = wire_cut_time(WireCut(D("30"), D("200"), threads=1))
    five = wire_cut_time(WireCut(D("30"), D("200"), threads=5))
    assert five.threading_mins - one.threading_mins == MINUTES_PER_THREAD * 4


def test_the_total_is_the_three_parts_and_nothing_else():
    """The total is computed from the unrounded parts, so it can sit up to a
    couple of hundredths away from the sum of the rounded ones. That is the
    right way round — rounding each part and then adding would drift."""
    r = wire_cut_time(WireCut(D("40"), D("320"), finish_cuts=2, threads=3))
    parts = r.first_cut_mins + r.finish_cut_mins + r.threading_mins
    assert abs(r.total_mins - parts) <= D("0.02")


def test_height_drives_the_time_far_harder_than_length():
    """The reason height is asked for first: doubling it costs more than
    doubling the path."""
    taller = wire_cut_time(WireCut(D("100"), D("200"), finish_cuts=0, threads=1))
    longer = wire_cut_time(WireCut(D("50"), D("400"), finish_cuts=0, threads=1))
    assert taller.first_cut_mins > longer.first_cut_mins


def test_a_job_on_the_questionable_row_is_flagged_as_such():
    assert wire_cut_time(WireCut(D("150"), D("200"))).speed_is_suspect is True
    assert wire_cut_time(WireCut(D("50"), D("200"))).speed_is_suspect is False


def test_the_speeds_used_are_reported_so_the_working_can_be_shown():
    r = wire_cut_time(WireCut(D("60"), D("200")))
    assert r.first_cut_speed == D("2.20")
    assert r.finish_cut_speed == D("3.70")


@pytest.mark.parametrize(
    "cut,message",
    [
        (WireCut(D("50"), D("0")), "Cut length"),
        (WireCut(D("50"), D("-5")), "Cut length"),
        (WireCut(D("50"), D("100"), finish_cuts=-1), "negative"),
        (WireCut(D("50"), D("100"), threads=0), "at least one thread"),
    ],
)
def test_nonsense_inputs_are_refused_rather_than_priced(cut, message):
    with pytest.raises(CalculatorError, match=message):
        wire_cut_time(cut)


def test_the_calculator_makes_no_ai_calls_and_touches_nothing():
    """Same rule as the pricing engine: a calculated time must be reproducible
    from its inputs alone, forever."""
    source = (__import__("pathlib").Path(__file__).parents[1] / "app/calculators.py").read_text()
    for forbidden in ("anthropic", "requests", "httpx", "urllib", "openai", "session", "datetime"):
        assert forbidden not in source.lower(), forbidden
