"""Tests for the deterministic nesting calculator.

Stock sizes and costs here are invented fixtures (spec section 9).
"""

from decimal import Decimal as D

import pytest

from app.enums import StockForm
from app.nesting import (
    Allowance,
    NestingError,
    PartEnvelope,
    StockOption,
    blanks_per_stock,
    fit_in_stock,
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

    with open(nesting_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("anthropic", "httpx", "requests", "openai"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# Machining allowance, then the size the supplier actually holds
# --------------------------------------------------------------------------
def bar_range(diameters, length=3000, price_per_mm="1.0") -> list[StockOption]:
    """A supplier's round-bar range, priced so bigger costs more."""
    return [
        StockOption(
            stock_id=100 + i,
            spec="EN16",
            stock_form=StockForm.BAR_ROUND.value,
            length_mm=D(str(length)),
            width_mm=D(str(dia)),
            thickness_mm=None,
            unit_cost=D(str(dia)) * D(price_per_mm),
            kerf_mm=D("3"),
        )
        for i, dia in enumerate(diameters)
    ]


def disc(diameter="85", thickness="20") -> PartEnvelope:
    """A turned part: round about one axis."""
    return PartEnvelope(D(diameter), D(diameter), D(thickness), is_rotational=True)


def test_the_allowance_is_added_before_any_stock_size_is_looked_at():
    assert fit_in_stock(disc(), round_bar(diameter=100)).required_section_mm == D("85")
    grown = fit_in_stock(disc(), round_bar(diameter=100), Allowance(D("4"), D("4")))
    assert grown.required_section_mm == D("89")
    assert grown.required_length_mm == D("24")


def test_a_part_is_bought_at_the_next_size_up_that_is_actually_stocked():
    # Needs 89mm once the OD allowance is on. 90 exists, so 90 it is —
    # not 89, which nobody sells.
    result = nest(disc(), 10, bar_range([70, 80, 90, 100, 110]), Allowance(D("4"), D("4")))
    assert result.required_section_mm == D("89.00")
    assert "90" in result.stock_label
    assert result.section_oversize_mm == D("1.00")
    assert result.allowance_applied is True


def test_the_allowance_can_push_the_choice_to_the_next_bar_up():
    sizes = bar_range([70, 80, 90, 100, 110])
    # Without an allowance a 90mm bar clears the 85mm part...
    assert "90" in nest(disc(), 10, sizes).stock_label
    # ...but 8mm on the OD needs 93, and 100 is the next size held.
    bigger = nest(disc(), 10, sizes, Allowance(D("8"), D("4")))
    assert bigger.required_section_mm == D("93.00")
    assert "100" in bigger.stock_label


def test_a_turned_part_is_sized_on_its_diameter_not_on_a_square_diagonal():
    # The same envelope, described as a block, has to have its corners
    # cleared: 85 x 85 has a 120mm diagonal, so it needs a 124mm bar and buys
    # far more steel than the job uses.
    block = PartEnvelope(D("85"), D("85"), D("20"))
    allowance = Allowance(D("4"), D("4"))
    assert fit_in_stock(disc(), round_bar(diameter=200), allowance).required_section_mm == D("89")
    as_block = fit_in_stock(block, round_bar(diameter=200), allowance)
    assert as_block.required_section_mm > D("120")


def test_a_disc_lies_flat_in_the_bar_rather_than_standing_on_end():
    # 85 dia x 20 thick: the 20 runs along the bar, giving many blanks per
    # bar. Standing it on end would give a fraction as many.
    fit = fit_in_stock(disc(), bar_range([90])[0], Allowance(D("4"), D("4")))
    assert fit.required_length_mm == D("24")
    assert fit.blanks == 3000 // (24 + 3)


def test_no_allowance_leaves_the_old_answer_untouched():
    part = PartEnvelope(D("100"), D("40"), D("40"))
    assert blanks_per_stock(part, round_bar()) == 10
    assert blanks_per_stock(part, round_bar(), Allowance()) == 10


def test_the_required_size_is_reported_even_when_it_is_bigger_than_the_stock():
    fit = fit_in_stock(disc(), round_bar(diameter=60), Allowance(D("4"), D("4")))
    assert fit.blanks == 0
    # Still says what was needed, so the flag can name a size to go and buy.
    assert fit.required_section_mm == D("89")


def test_plate_thickness_carries_the_allowance_on_both_faces():
    part = PartEnvelope(D("100"), D("50"), D("20"))
    assert blanks_per_stock(part, plate(thickness=22), Allowance(D("4"), D("0"))) == 0
    assert blanks_per_stock(part, plate(thickness=25), Allowance(D("4"), D("0"))) > 0


# --------------------------------------------------------------------------
# Shape: a cube is not a bar
# --------------------------------------------------------------------------
def square_bar(stock_id, side, cost, length=3000) -> StockOption:
    return StockOption(
        stock_id=stock_id,
        spec="EN30B",
        stock_form=StockForm.BAR_SQUARE.value,
        length_mm=D(str(length)),
        width_mm=D(str(side)),
        thickness_mm=D(str(side)),
        unit_cost=D(str(cost)),
        kerf_mm=D("3"),
    )


def round_of(stock_id, dia, cost, length=3000) -> StockOption:
    return StockOption(
        stock_id=stock_id,
        spec="EN30B",
        stock_form=StockForm.BAR_ROUND.value,
        length_mm=D(str(length)),
        width_mm=D(str(dia)),
        thickness_mm=None,
        unit_cost=D(str(cost)),
        kerf_mm=D("3"),
    )


CUBE = PartEnvelope(D("50"), D("50"), D("50"))
ALLOW = Allowance(D("4"), D("4"))


def test_a_cube_needs_its_width_from_square_but_its_diagonal_from_round():
    # Square: 50 across the flats plus 4mm of clean-up.
    assert fit_in_stock(CUBE, square_bar(1, 200, "999"), ALLOW).required_section_mm == D("54")
    # Round: the bar has to clear the cube's 70.71mm diagonal, and the shop's
    # allowance goes on the diameter — not on each face and then diagonally,
    # which would inflate it by a factor of root two.
    needed = fit_in_stock(CUBE, round_of(2, 200, "999"), ALLOW).required_section_mm
    assert D("74.7") < needed < D("74.8")
    # 21mm more steel across the section, for the same finished part.
    assert needed - D("54") > D("20")


def test_square_stock_wins_for_a_square_part_when_both_are_offered():
    # Priced by cross-section, as steel is.
    options = [round_of(1, 80, "284.10"), square_bar(2, 55, "170.97")]
    result = nest(CUBE, 20, options, ALLOW)
    assert result.stock_form == StockForm.BAR_SQUARE.value
    assert "55" in result.stock_label


def test_the_forms_that_were_on_offer_are_reported():
    only_round = nest(CUBE, 20, [round_of(1, 80, "284.10")], ALLOW)
    assert only_round.forms_offered == (StockForm.BAR_ROUND.value,)

    both = nest(CUBE, 20, [round_of(1, 80, "284.10"), square_bar(2, 55, "170.97")], ALLOW)
    assert set(both.forms_offered) == {StockForm.BAR_ROUND.value, StockForm.BAR_SQUARE.value}
