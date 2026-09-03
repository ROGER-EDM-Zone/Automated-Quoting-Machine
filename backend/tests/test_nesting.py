"""Tests for the deterministic nesting calculator.

Stock sizes and costs here are invented fixtures (spec section 9).
"""

from decimal import Decimal as D

import pytest

from app.enums import StockForm
from app.nesting import (
    NestingError,
    PartEnvelope,
    StockOption,
    blanks_per_stock,
    nest,
)


def plate(stock_id=1, length=500, width=250, thickness=25, cost="60.00", kerf="0") -> StockOption:
    return StockOption(
        stock_id=stock_id,
        spec="1.2312",
        stock_form=StockForm.PLATE.value,
        length_mm=D(str(length)),
        width_mm=D(str(width)),
        thickness_mm=D(str(thickness)),
        unit_cost=D(cost),
        kerf_mm=D(kerf),
    )


def round_bar(stock_id=2, length=1000, diameter=60, cost="45.00", kerf="0") -> StockOption:
    return StockOption(
        stock_id=stock_id,
        spec="EN24T",
        stock_form=StockForm.BAR_ROUND.value,
        length_mm=D(str(length)),
        width_mm=D(str(diameter)),
        thickness_mm=None,
        unit_cost=D(cost),
        kerf_mm=D(kerf),
    )


# --------------------------------------------------------------------------
# Blanks per piece of stock
# --------------------------------------------------------------------------
def test_plate_blanks_are_a_simple_grid_when_there_is_no_kerf():
    # 500x250 plate, 100x50 blanks -> 5 along x 5 across = 25
    got = blanks_per_stock(PartEnvelope(D("100"), D("50"), D("20")), plate())
    assert got == 25


def test_kerf_is_added_around_every_blank():
    # With a 5mm allowance each blank occupies 105 x 55 on a 500 x 250 plate.
    # Laid one way that is floor(500/105) x floor(250/55) = 4 x 4 = 16; turned
    # 90 degrees it is floor(500/55) x floor(250/105) = 9 x 2 = 18. The better
    # orientation must win.
    stock = plate(kerf="5")
    got = blanks_per_stock(PartEnvelope(D("100"), D("50"), D("20")), stock)
    assert got == 18


def test_the_better_plate_orientation_wins():
    # 100 x 50 blanks on a 500 x 250 plate: 5 x 5 = 25 laid lengthways,
    # 10 x 2 = 20 turned. The calculator must take the 25.
    stock = plate(length=500, width=250)
    assert blanks_per_stock(PartEnvelope(D("100"), D("50"), D("10")), stock) == 25


def test_a_part_thicker_than_the_plate_does_not_fit():
    assert blanks_per_stock(PartEnvelope(D("50"), D("50"), D("40")), plate(thickness=25)) == 0


def test_a_part_wider_than_the_plate_does_not_fit():
    assert blanks_per_stock(PartEnvelope(D("900"), D("50"), D("5")), plate()) == 0


def test_round_bar_cuts_along_its_length():
    # 1000mm bar, 100mm long blanks -> 10 slugs.
    assert blanks_per_stock(PartEnvelope(D("100"), D("40"), D("40")), round_bar()) == 10


def test_round_bar_rejects_a_blank_whose_diagonal_exceeds_the_diameter():
    # 45 x 45 section has a 63.6mm diagonal; a 60mm bar cannot hold it.
    assert blanks_per_stock(PartEnvelope(D("100"), D("45"), D("45")), round_bar(diameter=60)) == 0
    assert blanks_per_stock(PartEnvelope(D("100"), D("45"), D("45")), round_bar(diameter=70)) > 0


def test_square_bar_needs_both_section_dimensions_to_clear():
    bar = StockOption(
        stock_id=3,
        spec="EN8",
        stock_form=StockForm.BAR_SQUARE.value,
        length_mm=D("1000"),
        width_mm=D("50"),
        thickness_mm=D("50"),
        unit_cost=D("30.00"),
        kerf_mm=D("0"),
    )
    assert blanks_per_stock(PartEnvelope(D("80"), D("50"), D("50")), bar) == 12
    assert blanks_per_stock(PartEnvelope(D("80"), D("60"), D("50")), bar) == 0


def test_a_zero_dimension_envelope_is_rejected():
    with pytest.raises(NestingError):
        blanks_per_stock(PartEnvelope(D("0"), D("50"), D("20")), plate())


# --------------------------------------------------------------------------
# Choosing stock
# --------------------------------------------------------------------------
def test_pieces_of_stock_are_rounded_up_to_whole_pieces():
    # 25 blanks per plate, 30 wanted -> 2 plates.
    result = nest(PartEnvelope(D("100"), D("50"), D("20")), 30, [plate(cost="60.00")])
    assert result.blanks_per_unit_stock == 25
    assert result.qty_required == D("2")
    assert result.total_cost == D("120.00")


def test_one_plate_covers_a_quantity_that_fits_in_it():
    result = nest(PartEnvelope(D("100"), D("50"), D("20")), 25, [plate(cost="60.00")])
    assert result.qty_required == D("1")
    assert result.total_cost == D("60.00")


def test_the_cheapest_total_cost_wins():
    big = plate(stock_id=1, length=1000, width=500, cost="200.00")
    small = plate(stock_id=2, length=500, width=250, cost="60.00")
    # One small plate yields 25 blanks for 60; the big one yields 100 for 200.
    result = nest(PartEnvelope(D("100"), D("50"), D("20")), 20, [big, small])
    assert result.stock_id == 2
    assert result.total_cost == D("60.00")


def test_utilisation_is_reported_as_a_percentage_of_stock_bought():
    # 25 blanks of 100x50x20 = 2,500,000mm3 out of a 500x250x25 = 3,125,000mm3
    result = nest(PartEnvelope(D("100"), D("50"), D("20")), 25, [plate()])
    assert result.utilisation_pct == D("80.00")


def test_a_part_that_fits_nothing_raises_for_a_buyer_decision():
    with pytest.raises(NestingError, match="buyer decision"):
        nest(PartEnvelope(D("2000"), D("2000"), D("500")), 1, [plate(), round_bar()])


def test_no_stock_offered_is_an_error_not_a_free_part():
    with pytest.raises(NestingError, match="No stock sizes"):
        nest(PartEnvelope(D("10"), D("10"), D("10")), 1, [])


def test_nesting_is_deterministic_including_ties():
    # Two identical options differing only by id: the lower id must always win.
    a = plate(stock_id=7, cost="60.00")
    b = plate(stock_id=8, cost="60.00")
    envelope = PartEnvelope(D("100"), D("50"), D("20"))
    results = [nest(envelope, 30, [a, b]) for _ in range(10)]
    assert {r.stock_id for r in results} == {7}
    assert all(r == results[0] for r in results)
    # Order of the candidate list must not matter either.
    assert nest(envelope, 30, [b, a]).stock_id == 7


def test_nesting_makes_no_ai_calls():
    import app.nesting as nesting_module

    source = open(nesting_module.__file__, encoding="utf-8").read()
    for forbidden in ("anthropic", "httpx", "requests", "openai"):
        assert forbidden not in source
