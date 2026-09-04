"""Material sizing: allowance first, then the size the supplier actually holds.

These cover the two ways a materially wrong number reaches a quote without
anything looking broken: buying stock the size of the finished part, and
buying the wrong *shape* of stock because the right one was never listed.
"""

from decimal import Decimal

import pytest

from app.enums import (
    AdjustmentType,
    FlagSeverity,
    JobType,
    Process,
    RuleKey,
    StockForm,
)
from app.models import Flag, Operation, Part, RulesTable, StockSize
from app.services.quoting import compute_material, is_rotational


@pytest.fixture
def allowance_rules(db):
    """The shop's rule of thumb: 4mm on the OD, 4mm on the length."""
    rows = [
        RulesTable(
            rule_key=RuleKey.MATERIAL_ALLOWANCE_SECTION.value,
            trigger_description="Material left on the diameter or section for clean-up",
            adjustment_type=AdjustmentType.MM.value,
            adjustment_value=Decimal("4"),
        ),
        RulesTable(
            rule_key=RuleKey.MATERIAL_ALLOWANCE_LENGTH.value,
            trigger_description="Material left on the length of one part",
            adjustment_type=AdjustmentType.MM.value,
            adjustment_value=Decimal("4"),
        ),
    ]
    db.add_all(rows)
    db.commit()
    return rows


def bar_stock(db, spec, form, sizes, price_per_mm=Decimal("3")):
    rows = [
        StockSize(
            spec=spec,
            stock_form=form,
            length_mm=Decimal("3000"),
            width_mm=Decimal(str(size)),
            thickness_mm=(
                Decimal(str(size))
                if form in (StockForm.BAR_SQUARE.value, StockForm.BILLET.value)
                else None
            ),
            unit_cost=Decimal(str(size)) * price_per_mm,
            kerf_mm=Decimal("3"),
        )
        for size in sizes
    ]
    db.add_all(rows)
    db.commit()
    return rows


def turned_part(db, enquiry, diameter="85", thickness="20", quantity=15):
    part = Part(
        enquiry_id=enquiry.id,
        drawing_number="67980",
        quantity=quantity,
        material="EN16",
        job_type=JobType.FULL_SUPPLY.value,
        envelope_x=Decimal(diameter),
        envelope_y=Decimal(diameter),
        envelope_z=Decimal(thickness),
        process_mix=[Process.CNC_TURN.value],
    )
    db.add(part)
    db.flush()
    db.add(
        Operation(
            part_id=part.id,
            op_number=10,
            process=Process.CNC_TURN.value,
            description="Turn",
            set_time_mins=Decimal("60"),
            run_time_mins_per_unit=Decimal("10"),
        )
    )
    db.commit()
    return part


def cube_part(db, enquiry, side="50", quantity=20):
    part = Part(
        enquiry_id=enquiry.id,
        drawing_number="CUBE-1",
        quantity=quantity,
        material="EN30B",
        job_type=JobType.FULL_SUPPLY.value,
        envelope_x=Decimal(side),
        envelope_y=Decimal(side),
        envelope_z=Decimal(side),
        process_mix=[Process.CNC_MILL.value],
    )
    db.add(part)
    db.flush()
    db.add(
        Operation(
            part_id=part.id,
            op_number=10,
            process=Process.CNC_MILL.value,
            description="Mill all over",
            set_time_mins=Decimal("60"),
            run_time_mins_per_unit=Decimal("15"),
        )
    )
    db.commit()
    return part


def flags_for(db, part, severity=None):
    rows = db.query(Flag).filter(Flag.part_id == part.id).all()
    if severity:
        rows = [f for f in rows if f.severity == severity]
    return rows


# --------------------------------------------------------------------------
# Round parts
# --------------------------------------------------------------------------
def test_the_next_stocked_size_up_is_bought_not_the_size_needed(db, enquiry, allowance_rules):
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [70, 80, 90, 100, 110])
    part = turned_part(db, enquiry)

    requirement = compute_material(db, part)

    assert requirement is not None
    # 85 finished + 4mm on the diameter = 89, and 90 is the smallest held.
    assert requirement.required_section_mm == Decimal("89.00")
    assert "90" in requirement.stock_size
    assert requirement.section_oversize_mm == Decimal("1.00")


def test_a_bigger_allowance_moves_the_purchase_to_the_next_bar(db, enquiry, allowance_rules):
    for rule in allowance_rules:
        if rule.rule_key == RuleKey.MATERIAL_ALLOWANCE_SECTION.value:
            rule.adjustment_value = Decimal("8")
    db.commit()
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [70, 80, 90, 100, 110])
    part = turned_part(db, enquiry)

    requirement = compute_material(db, part)

    assert requirement.required_section_mm == Decimal("93.00")
    assert "100" in requirement.stock_size


def test_without_an_allowance_the_part_is_sized_to_itself_and_says_so(db, enquiry):
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [70, 80, 90, 100])
    part = turned_part(db, enquiry)

    compute_material(db, part)

    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert any("machining allowance" in m for m in messages)


def test_a_turned_part_is_recognised_from_its_routing_and_envelope(db, enquiry):
    part = turned_part(db, enquiry)
    assert is_rotational(part) is True


def test_an_estimator_can_overrule_the_inference(db, enquiry):
    part = turned_part(db, enquiry)
    part.is_rotational = False
    assert is_rotational(part) is False


def test_a_milled_block_is_never_treated_as_turned(db, enquiry):
    part = cube_part(db, enquiry)
    assert is_rotational(part) is False


# --------------------------------------------------------------------------
# Square parts
# --------------------------------------------------------------------------
def test_a_square_part_buys_square_stock_when_it_is_listed(db, enquiry, allowance_rules):
    # Priced by cross-sectional area, as steel is.
    bar_stock(db, "EN30B", StockForm.BAR_ROUND.value, [70, 80, 90], Decimal("3.6"))
    bar_stock(db, "EN30B", StockForm.BAR_SQUARE.value, [50, 55, 60], Decimal("3.1"))
    part = cube_part(db, enquiry)

    requirement = compute_material(db, part)

    assert requirement.stock_form == StockForm.BAR_SQUARE.value
    assert requirement.required_section_mm == Decimal("54.00")
    assert "55" in requirement.stock_size


def test_a_square_part_with_only_round_stock_listed_is_flagged(db, enquiry, allowance_rules):
    bar_stock(db, "EN30B", StockForm.BAR_ROUND.value, [70, 80, 90, 100])
    part = cube_part(db, enquiry)

    requirement = compute_material(db, part)

    # It still prices — but it says the shape it had to buy, and why.
    assert requirement.stock_form == StockForm.BAR_ROUND.value
    assert requirement.required_section_mm == Decimal("74.71")
    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert any("not round" in m and "square or flat" in m for m in messages)


def test_no_such_flag_when_square_was_available_and_round_still_won(db, enquiry, allowance_rules):
    # Square bar priced absurdly high, so round genuinely is the cheaper buy.
    bar_stock(db, "EN30B", StockForm.BAR_ROUND.value, [80], Decimal("1"))
    bar_stock(db, "EN30B", StockForm.BAR_SQUARE.value, [55], Decimal("99"))
    part = cube_part(db, enquiry)

    compute_material(db, part)

    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert not any("not round" in m for m in messages)


def test_an_unlisted_size_is_not_offered_to_the_calculator(db, enquiry, allowance_rules):
    rows = bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [90, 100])
    part = turned_part(db, enquiry)
    assert "90" in compute_material(db, part).stock_size

    # The supplier drops 90 from the range.
    rows[0].listed = False
    db.commit()
    assert "100" in compute_material(db, part).stock_size


# --------------------------------------------------------------------------
# Where the price came from
# --------------------------------------------------------------------------
def test_a_typed_material_price_is_flagged_as_not_live(db, enquiry, allowance_rules):
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [90, 100])
    part = turned_part(db, enquiry)

    requirement = compute_material(db, part)

    assert requirement.price_is_stale is True
    assert requirement.price_source_name is None
    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert any("entered by hand" in m for m in messages)


def test_a_live_price_is_used_and_its_source_recorded(db, enquiry, allowance_rules):
    from datetime import UTC, datetime

    from app.enums import MarketBasis, MarketKind, MarketMethod, MarketUnit
    from app.models import MarketObservation, MarketSource

    src = MarketSource(
        series_key="material:en16:round_bar",
        name="Test Stockholder",
        kind=MarketKind.MATERIAL_PRICE.value,
        unit=MarketUnit.GBP_PER_KG.value,
        basis=MarketBasis.RETAIL_ONLINE.value,
        url="https://example.test/en16",
        spec="EN16",
        stock_form=StockForm.BAR_ROUND.value,
        max_age_hours=168,
    )
    db.add(src)
    db.flush()
    db.add(
        MarketObservation(
            source_id=src.id,
            series_key=src.series_key,
            value=Decimal("2.40"),
            unit=MarketUnit.GBP_PER_KG.value,
            method=MarketMethod.AI_READ.value,
            basis=src.basis,
            confidence=0.95,
            evidence="EN16T round bar £2.40/kg",
            source_url=src.url,
            observed_at=datetime.now(UTC),
        )
    )
    rows = bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [90, 100])
    for row in rows:
        row.market_series_key = src.series_key
        row.density_kg_m3 = Decimal("7850")
    db.commit()

    part = turned_part(db, enquiry)
    requirement = compute_material(db, part)

    assert requirement.price_source_name == "Test Stockholder"
    assert requirement.price_is_stale is False
    # A 90mm x 3m bar weighs 149.8kg; at £2.40/kg that is around £360.
    assert Decimal("350") < requirement.unit_cost < Decimal("370")
    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert not any("entered by hand" in m for m in messages)


def test_buying_a_whole_bar_for_a_small_job_is_flagged(db, enquiry, allowance_rules):
    # 15 discs needing 24mm each = 360mm, out of a 3m bar.
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [90, 100])
    part = turned_part(db, enquiry, quantity=15)

    requirement = compute_material(db, part)

    assert requirement.utilisation_pct < Decimal("25")
    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert any("cut to length" in m for m in messages)


def test_a_job_that_uses_most_of_the_bar_is_not_flagged(db, enquiry, allowance_rules):
    bar_stock(db, "EN16", StockForm.BAR_ROUND.value, [90])
    # 100 discs at 24mm + 3mm kerf fills the bar and the volume follows.
    part = turned_part(db, enquiry, diameter="88", thickness="80", quantity=100)

    compute_material(db, part)

    messages = [f.message for f in flags_for(db, part, FlagSeverity.WARN.value)]
    assert not any("cut to length" in m for m in messages)
