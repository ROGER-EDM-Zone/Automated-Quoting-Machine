"""Tests for the deterministic quoting engine.

The engine is the one part of this system that must be boringly predictable,
so these tests are deliberately arithmetic-heavy: they pin the formulae from
spec stage 4 rather than merely exercising the code.

All figures here are invented test fixtures (see spec section 9) — they are
not real rates.
"""

from decimal import Decimal as D

import pytest

from app.enums import AdjustmentType, JobType, Process, RuleKey, TimeSource
from app.pricing import (
    AdjustmentRule,
    MaterialInput,
    MissingRate,
    OperationInput,
    PartInput,
    PricingError,
    money,
    price_operation,
    price_part,
    price_quote,
)


def op(**kw) -> OperationInput:
    defaults = dict(
        op_number=10,
        process=Process.CNC_MILL.value,
        set_time_mins=D("0"),
        run_time_mins_per_unit=D("0"),
        hourly_rate=D("60.00"),
    )
    defaults.update(kw)
    return OperationInput(**defaults)


def part(**kw) -> PartInput:
    defaults = dict(quantity=1, job_type=JobType.SERVICE_ONLY.value, operations=(op(),))
    defaults.update(kw)
    return PartInput(**defaults)


# --------------------------------------------------------------------------
# Operation costing
# --------------------------------------------------------------------------
def test_operation_cost_is_set_time_plus_run_time_times_quantity():
    # 30 set + (12 run x 5 off) = 90 mins = 1.5h at 60/h = 90.00
    cost = price_operation(
        op(set_time_mins=D("30"), run_time_mins_per_unit=D("12"), hourly_rate=D("60")),
        quantity=5,
    )
    assert cost.total_mins == D("90.00")
    assert cost.computed_cost == D("90.00")
    assert cost.is_subcontract is False


def test_set_time_is_charged_once_not_per_unit():
    one = price_operation(op(set_time_mins=D("60"), hourly_rate=D("40")), quantity=1)
    ten = price_operation(op(set_time_mins=D("60"), hourly_rate=D("40")), quantity=10)
    assert one.computed_cost == ten.computed_cost == D("40.00")


def test_subcontract_cost_is_unit_cost_times_quantity_and_ignores_time():
    cost = price_operation(
        op(
            op_number=40,
            process=Process.SUBCONTRACT.value,
            subcontract_unit_cost=D("18.50"),
            set_time_mins=D("999"),
            hourly_rate=None,
        ),
        quantity=6,
    )
    assert cost.computed_cost == D("111.00")
    assert cost.total_mins == D("0.00")
    assert cost.hourly_rate is None


def test_subcontract_without_a_unit_cost_is_an_error_not_a_zero():
    with pytest.raises(PricingError, match="subcontract_unit_cost"):
        price_operation(
            op(process=Process.SUBCONTRACT.value, subcontract_unit_cost=None), 1
        )


def test_missing_rate_raises_rather_than_defaulting():
    """A made-up rate is the failure mode the whole spec exists to prevent."""
    with pytest.raises(MissingRate) as excinfo:
        price_operation(op(process=Process.WIRE_EDM.value, hourly_rate=None), 1)
    assert excinfo.value.process == Process.WIRE_EDM.value


def test_negative_and_zero_quantities_are_rejected():
    with pytest.raises(PricingError):
        price_operation(op(), quantity=0)
    with pytest.raises(PricingError):
        price_part(part(quantity=-1), margin_pct=D("0"))


# --------------------------------------------------------------------------
# Part and quote build-up
# --------------------------------------------------------------------------
def test_full_build_up_follows_the_spec_formulae():
    priced = price_part(
        part(
            quantity=2,
            job_type=JobType.FULL_SUPPLY.value,
            operations=(
                op(op_number=10, set_time_mins=D("60"), run_time_mins_per_unit=D("30"), hourly_rate=D("60")),
                op(op_number=20, process=Process.GRIND.value, set_time_mins=D("30"), hourly_rate=D("40")),
            ),
            materials=(MaterialInput(qty_required=D("1"), unit_cost=D("80.00")),),
        ),
        margin_pct=D("25"),
    )
    # op10: 60 + 30*2 = 120 mins @60 = 120.00 ; op20: 30 mins @40 = 20.00
    assert priced.labour_total == D("140.00")
    assert priced.material_total == D("80.00")
    assert priced.subtotal == D("220.00")
    assert priced.margin_value == D("55.00")  # 220 * 25%
    assert priced.value == D("275.00")
    assert priced.unit_price == D("137.50")  # 275 / 2
    assert priced.line_total == D("275.00")


def test_service_only_ignores_material_even_when_lines_exist():
    """material_total = 0 for service_only (spec stage 4)."""
    priced = price_part(
        part(
            job_type=JobType.SERVICE_ONLY.value,
            materials=(MaterialInput(qty_required=D("3"), unit_cost=D("500.00")),),
            operations=(op(set_time_mins=D("60"), hourly_rate=D("50")),),
        ),
        margin_pct=D("0"),
    )
    assert priced.material_total == D("0.00")
    assert priced.material_costs == ()
    assert priced.subtotal == D("50.00")


def test_full_supply_includes_material():
    priced = price_part(
        part(
            job_type=JobType.FULL_SUPPLY.value,
            materials=(MaterialInput(qty_required=D("3"), unit_cost=D("25.00")),),
            operations=(),
        ),
        margin_pct=D("0"),
    )
    assert priced.material_total == D("75.00")


def test_nesting_supplied_total_cost_wins_over_qty_times_unit_cost():
    """The nesting calculator's total is authoritative — it accounts for
    whole pieces of stock bought, not just the blank volume used."""
    priced = price_part(
        part(
            job_type=JobType.FULL_SUPPLY.value,
            operations=(),
            materials=(
                MaterialInput(qty_required=D("2"), unit_cost=D("30.00"), total_cost=D("60.00")),
            ),
        ),
        margin_pct=D("0"),
    )
    assert priced.material_total == D("60.00")


def test_quote_totals_are_the_sum_of_their_parts():
    a = part(part_id=1, quantity=1, operations=(op(set_time_mins=D("60"), hourly_rate=D("60")),))
    b = part(part_id=2, quantity=1, operations=(op(set_time_mins=D("30"), hourly_rate=D("40")),))
    quote = price_quote([a, b], margin_pct=D("10"))
    assert quote.labour_total == D("80.00")
    assert quote.subtotal == D("80.00")
    assert quote.margin_value == D("8.00")
    assert quote.quote_value == D("88.00")
    assert quote.reconciles()


def test_pricing_an_empty_quote_is_an_error():
    with pytest.raises(PricingError, match="no parts"):
        price_quote([], margin_pct=D("20"))


# --------------------------------------------------------------------------
# Determinism — the whole point of this module
# --------------------------------------------------------------------------
def test_repeated_pricing_of_the_same_inputs_is_byte_identical():
    parts = [
        part(
            part_id=1,
            quantity=7,
            job_type=JobType.FULL_SUPPLY.value,
            operations=(
                op(op_number=10, set_time_mins=D("47.5"), run_time_mins_per_unit=D("13.25"), hourly_rate=D("38.00")),
                op(op_number=20, process=Process.WIRE_EDM.value, run_time_mins_per_unit=D("9.75"), hourly_rate=D("41.50")),
                op(op_number=30, process=Process.SUBCONTRACT.value, subcontract_unit_cost=D("7.35"), hourly_rate=None),
            ),
            materials=(MaterialInput(qty_required=D("2"), unit_cost=D("63.33")),),
        )
    ]
    rules = [
        AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("12.5")),
        AdjustmentRule(2, RuleKey.MIN_QUOTE_VALUE.value, AdjustmentType.FIXED.value, D("150.00")),
    ]
    results = [price_quote(parts, D("32.5"), rules) for _ in range(25)]
    assert len({r.quote_value for r in results}) == 1
    assert all(r == results[0] for r in results)


def test_rule_order_does_not_change_the_price():
    parts = [part(quantity=3, operations=(op(set_time_mins=D("90"), hourly_rate=D("55")),))]
    rush = AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("15"))
    conting = AdjustmentRule(2, RuleKey.DIFFICULT_JOB_CONTINGENCY.value, AdjustmentType.PCT.value, D("7.5"))
    forwards = price_quote(parts, D("20"), [rush, conting])
    backwards = price_quote(parts, D("20"), [conting, rush])
    assert forwards.quote_value == backwards.quote_value


def test_engine_makes_no_network_or_ai_calls():
    """Guards the spec's central rule: code calculates, AI never prices."""
    import app.pricing as pricing_module

    source = open(pricing_module.__file__, encoding="utf-8").read()
    for forbidden in ("anthropic", "requests", "httpx", "urllib", "openai"):
        assert forbidden not in source, f"pricing engine must not reference {forbidden}"


# --------------------------------------------------------------------------
# rules_table adjustments
# --------------------------------------------------------------------------
def test_percentage_rules_are_summed_not_compounded():
    parts = [part(operations=(op(set_time_mins=D("60"), hourly_rate=D("100")),))]
    rules = [
        AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("10")),
        AdjustmentRule(2, RuleKey.DIFFICULT_JOB_CONTINGENCY.value, AdjustmentType.PCT.value, D("10")),
    ]
    quote = price_quote(parts, D("0"), rules)
    # 100 base + 20% = 120.00, not 121.00 (which compounding would give).
    assert quote.quote_value == D("120.00")


def test_percentage_effects_are_itemised_per_rule_and_sum_exactly():
    parts = [part(operations=(op(set_time_mins=D("60"), hourly_rate=D("100")),))]
    rules = [
        AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("10")),
        AdjustmentRule(2, RuleKey.DIFFICULT_JOB_CONTINGENCY.value, AdjustmentType.PCT.value, D("5")),
    ]
    quote = price_quote(parts, D("0"), rules)
    effects = {a.rule_key: a.effect for a in quote.adjustments}
    assert effects[RuleKey.RUSH_UPLIFT.value] == D("10.00")
    assert effects[RuleKey.DIFFICULT_JOB_CONTINGENCY.value] == D("5.00")
    assert sum(effects.values()) == D("15.00")
    assert quote.reconciles()


def test_min_quote_value_lifts_a_cheap_quote_and_is_recorded():
    parts = [part(operations=(op(set_time_mins=D("6"), hourly_rate=D("50")),))]  # 5.00
    rules = [AdjustmentRule(9, RuleKey.MIN_QUOTE_VALUE.value, AdjustmentType.FIXED.value, D("75.00"))]
    quote = price_quote(parts, D("0"), rules)
    assert quote.quote_value == D("75.00")
    assert quote.min_value_applied is True
    floor = next(a for a in quote.adjustments if a.rule_key == RuleKey.MIN_QUOTE_VALUE.value)
    assert floor.effect == D("70.00")
    assert quote.reconciles()


def test_min_quote_value_does_not_touch_a_quote_already_above_it():
    parts = [part(operations=(op(set_time_mins=D("600"), hourly_rate=D("50")),))]  # 500.00
    rules = [AdjustmentRule(9, RuleKey.MIN_QUOTE_VALUE.value, AdjustmentType.FIXED.value, D("75.00"))]
    quote = price_quote(parts, D("0"), rules)
    assert quote.quote_value == D("500.00")
    assert quote.min_value_applied is False
    assert quote.adjustments == ()


def test_min_quote_value_is_applied_after_uplifts():
    """A rush uplift that clears the floor must not also collect the floor."""
    parts = [part(operations=(op(set_time_mins=D("60"), hourly_rate=D("70")),))]  # 70.00
    rules = [
        AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("20")),  # -> 84.00
        AdjustmentRule(9, RuleKey.MIN_QUOTE_VALUE.value, AdjustmentType.FIXED.value, D("75.00")),
    ]
    quote = price_quote(parts, D("0"), rules)
    assert quote.quote_value == D("84.00")
    assert quote.min_value_applied is False


def test_fixed_adjustment_lands_on_the_quote_total():
    parts = [part(operations=(op(set_time_mins=D("60"), hourly_rate=D("100")),))]
    rules = [AdjustmentRule(3, "programming_charge", AdjustmentType.FIXED.value, D("45.00"))]
    quote = price_quote(parts, D("0"), rules)
    assert quote.quote_value == D("145.00")
    assert quote.reconciles()


def test_flag_only_rules_have_no_arithmetic_effect():
    parts = [part(operations=(op(set_time_mins=D("60"), hourly_rate=D("100")),))]
    rules = [AdjustmentRule(4, "thin_wall_risk", AdjustmentType.FLAG_ONLY.value, D("0"))]
    quote = price_quote(parts, D("0"), rules)
    assert quote.quote_value == D("100.00")
    assert quote.adjustments == ()


# --------------------------------------------------------------------------
# Rounding and reconciliation
# --------------------------------------------------------------------------
def test_money_rounds_half_up():
    assert money(D("1.005")) == D("1.01")
    assert money(D("2.674")) == D("2.67")


def test_unit_price_times_quantity_always_equals_the_line_total():
    """Customer-facing arithmetic must not visibly fail to add up."""
    for quantity in range(1, 40):
        priced = price_part(
            part(quantity=quantity, operations=(op(set_time_mins=D("37"), run_time_mins_per_unit=D("11.3"), hourly_rate=D("43.70")),)),
            margin_pct=D("27.5"),
        )
        assert priced.unit_price * quantity == priced.line_total


def test_build_up_reconciles_across_awkward_quantities_and_rules():
    rules = [
        AdjustmentRule(1, RuleKey.RUSH_UPLIFT.value, AdjustmentType.PCT.value, D("7.5")),
        AdjustmentRule(5, "carriage", AdjustmentType.FIXED.value, D("22.50")),
        AdjustmentRule(9, RuleKey.MIN_QUOTE_VALUE.value, AdjustmentType.FIXED.value, D("120.00")),
    ]
    for quantity in (1, 3, 7, 13, 29):
        quote = price_quote(
            [
                part(
                    part_id=1,
                    quantity=quantity,
                    job_type=JobType.FULL_SUPPLY.value,
                    operations=(
                        op(op_number=10, set_time_mins=D("22.5"), run_time_mins_per_unit=D("6.7"), hourly_rate=D("38.00")),
                        op(op_number=20, process=Process.QC.value, set_time_mins=D("5"), run_time_mins_per_unit=D("1.5"), hourly_rate=D("29.00")),
                    ),
                    materials=(MaterialInput(qty_required=D("1"), unit_cost=D("41.11")),),
                )
            ],
            margin_pct=D("31.25"),
            rules=rules,
        )
        assert quote.reconciles(), f"quantity {quantity} failed to reconcile"


def test_rounding_adjustment_is_reported_not_hidden():
    priced = price_part(
        part(quantity=3, operations=(op(set_time_mins=D("1"), hourly_rate=D("60.10")),)),
        margin_pct=D("0"),
    )
    assert priced.line_total - priced.value == priced.rounding_adjustment


# --------------------------------------------------------------------------
# time_source must survive pricing (spec sections 4 and 6)
# --------------------------------------------------------------------------
def test_time_source_is_carried_through_to_the_costed_operation():
    priced = price_part(
        part(
            operations=(
                op(op_number=10, time_source=TimeSource.CALCULATOR.value, set_time_mins=D("10")),
                op(op_number=20, time_source=TimeSource.HISTORICAL_ESTIMATE.value, set_time_mins=D("10")),
                op(op_number=30, time_source=TimeSource.MANUAL.value, set_time_mins=D("10")),
            )
        ),
        margin_pct=D("0"),
    )
    assert [oc.time_source for oc in priced.operation_costs] == [
        TimeSource.CALCULATOR.value,
        TimeSource.HISTORICAL_ESTIMATE.value,
        TimeSource.MANUAL.value,
    ]
    assert priced.uses_untrusted_times is True


def test_a_quote_from_calculator_times_only_is_not_marked_untrusted():
    quote = price_quote(
        [part(operations=(op(time_source=TimeSource.CALCULATOR.value, set_time_mins=D("10")),))],
        margin_pct=D("0"),
    )
    assert quote.uses_untrusted_times is False
