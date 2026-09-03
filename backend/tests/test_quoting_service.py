"""Tests for the ORM <-> engine bridge."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.enums import EnquiryStatus, FlagSeverity, JobType, Process, QuoteStatus, TimeSource
from app.models import Flag, Operation, Part, RateTable
from app.services.quoting import (
    NotPriceable,
    ambiguous_cost_paths,
    compute_material,
    current_quote,
    price_breakdown,
    price_enquiry,
)


def test_pricing_an_enquiry_creates_a_draft_quote_with_lines(db, enquiry, priceable_part, rules):
    quote = price_enquiry(db, enquiry)
    assert quote.status == QuoteStatus.DRAFT.value
    assert quote.version == 1
    assert len(quote.lines) == 1
    line = quote.lines[0]
    assert line.part_id == priceable_part.id
    assert line.quantity == 4
    assert line.drawing_number == "4471"
    assert quote.quote_value > 0
    assert enquiry.status == EnquiryStatus.PRICED.value


def test_the_stored_numbers_match_the_engine_exactly(db, enquiry, priceable_part, rules):
    quote = price_enquiry(db, enquiry)
    breakdown = price_breakdown(db, enquiry, quote)
    assert quote.labour_total == breakdown.labour_total
    assert quote.material_total == breakdown.material_total
    assert quote.subtotal == breakdown.subtotal
    assert quote.margin_value == breakdown.margin_value
    assert quote.quote_value == breakdown.quote_value
    assert breakdown.reconciles()


def test_rates_are_resolved_from_the_table_and_recorded_on_the_operation(
    db, enquiry, priceable_part, rates
):
    price_enquiry(db, enquiry)
    mill = next(op for op in priceable_part.operations if op.process == Process.CNC_MILL.value)
    assert mill.hourly_rate == D("55.00")
    assert mill.rate_table_id == rates[Process.CNC_MILL.value].id
    # 45 set + 22 x 4 = 133 mins @ 55/h = 121.92
    assert mill.computed_cost == D("121.92")


def test_customer_default_margin_is_used_when_none_is_given(db, enquiry, priceable_part, rates, customer):
    quote = price_enquiry(db, enquiry)
    assert quote.margin_pct == D("30.000")


def test_an_explicit_margin_overrides_the_customer_default(db, enquiry, priceable_part, rates):
    quote = price_enquiry(db, enquiry, margin_pct=D("45"))
    assert quote.margin_pct == D("45")


def test_repricing_is_idempotent(db, enquiry, priceable_part, rules):
    first = price_enquiry(db, enquiry)
    first_value, first_id = first.quote_value, first.id
    second = price_enquiry(db, enquiry)
    assert second.id == first_id, "repricing must update the working quote, not fork it"
    assert second.version == 1
    assert second.quote_value == first_value


def test_a_missing_rate_blocks_with_a_flag_instead_of_inventing_one(db, enquiry, priceable_part, rates):
    db.add(
        Operation(
            part_id=priceable_part.id,
            op_number=40,
            process=Process.SPARK_ERODE.value,
            set_time_mins=D("60"),
            time_source=TimeSource.MANUAL.value,
        )
    )
    # Withdraw the spark erode rate: no row in force means no price.
    db.query(RateTable).filter(RateTable.process == Process.SPARK_ERODE.value).delete()
    db.commit()

    with pytest.raises(NotPriceable):
        price_enquiry(db, enquiry)

    flag = db.query(Flag).filter(Flag.dedupe_key == "missing_rate:spark_erode").one()
    assert flag.severity == FlagSeverity.BLOCK.value
    assert "No default rate is assumed" in flag.message
    assert enquiry.status == EnquiryStatus.NEEDS_ATTENTION.value


def test_a_rate_that_has_expired_is_not_used(db, enquiry, priceable_part, rates):
    yesterday = date.today() - timedelta(days=1)
    for row in rates.values():
        row.effective_to = yesterday
    db.commit()
    with pytest.raises(NotPriceable):
        price_enquiry(db, enquiry)


def test_a_part_with_no_operations_cannot_be_priced(db, enquiry, drawing_attachment, rates):
    db.add(Part(enquiry_id=enquiry.id, attachment_id=drawing_attachment.id, quantity=2, drawing_number="9000"))
    db.commit()
    with pytest.raises(NotPriceable, match="no operations"):
        price_enquiry(db, enquiry)


def test_an_enquiry_with_no_parts_cannot_be_priced(db, enquiry, rates):
    with pytest.raises(NotPriceable, match="No parts"):
        price_enquiry(db, enquiry)


# --------------------------------------------------------------------------
# Material / nesting integration
# --------------------------------------------------------------------------
def test_service_only_parts_get_no_material_line(db, enquiry, priceable_part, rates, stock):
    assert compute_material(db, priceable_part) is None
    quote = price_enquiry(db, enquiry)
    assert quote.material_total == D("0.00")


def test_full_supply_parts_get_a_nested_material_line(db, enquiry, priceable_part, rates, stock):
    priceable_part.job_type = JobType.FULL_SUPPLY.value
    db.commit()
    requirement = compute_material(db, priceable_part)
    assert requirement is not None
    assert requirement.spec == "1.2312"
    assert requirement.blanks_per_unit_stock >= 1
    assert requirement.total_cost > 0
    quote = price_enquiry(db, enquiry)
    assert quote.material_total == requirement.total_cost


def test_material_with_no_listed_stock_blocks_for_a_buyer(db, enquiry, priceable_part, rates):
    priceable_part.job_type = JobType.FULL_SUPPLY.value
    priceable_part.material = "Unobtainium"
    db.commit()
    assert compute_material(db, priceable_part) is None
    flag = db.query(Flag).filter(Flag.dedupe_key == "nesting_no_stock").one()
    assert flag.severity == FlagSeverity.BLOCK.value
    assert "buyer" in flag.message


def test_full_supply_without_an_envelope_blocks(db, enquiry, priceable_part, rates, stock):
    priceable_part.job_type = JobType.FULL_SUPPLY.value
    priceable_part.envelope_z = None
    db.commit()
    assert compute_material(db, priceable_part) is None
    flag = db.query(Flag).filter(Flag.dedupe_key == "nesting_no_envelope").one()
    assert flag.severity == FlagSeverity.BLOCK.value


# --------------------------------------------------------------------------
# Ambiguity: show both paths, pick neither
# --------------------------------------------------------------------------
def test_an_ambiguous_part_yields_both_cost_paths(db, enquiry, priceable_part, rates, stock):
    priceable_part.job_type = JobType.AMBIGUOUS.value
    db.commit()
    compute_material(db, priceable_part)
    paths = ambiguous_cost_paths(db, priceable_part)
    assert set(paths) == {JobType.SERVICE_ONLY.value, JobType.FULL_SUPPLY.value}
    assert paths[JobType.SERVICE_ONLY.value].material_total == D("0.00")
    assert paths[JobType.FULL_SUPPLY.value].material_total > 0
    assert paths[JobType.FULL_SUPPLY.value].value > paths[JobType.SERVICE_ONLY.value].value


# --------------------------------------------------------------------------
# time_source survives to the stored record
# --------------------------------------------------------------------------
def test_the_breakdown_still_distinguishes_estimated_times(db, enquiry, priceable_part, rates):
    quote = price_enquiry(db, enquiry)
    breakdown = price_breakdown(db, enquiry, quote)
    sources = {oc.op_number: oc.time_source for oc in breakdown.parts[0].operation_costs}
    assert sources[10] == TimeSource.CALCULATOR.value
    assert sources[20] == TimeSource.HISTORICAL_ESTIMATE.value
    assert sources[30] == TimeSource.MANUAL.value
    assert breakdown.uses_untrusted_times is True


# --------------------------------------------------------------------------
# rules_table application
# --------------------------------------------------------------------------
def test_min_quote_value_applies_without_anyone_selecting_it(db, enquiry, drawing_attachment, rates, rules):
    part = Part(
        enquiry_id=enquiry.id,
        attachment_id=drawing_attachment.id,
        drawing_number="TINY",
        quantity=1,
        job_type=JobType.SERVICE_ONLY.value,
    )
    db.add(part)
    db.flush()
    db.add(Operation(part_id=part.id, op_number=10, process=Process.MANUAL.value, set_time_mins=D("6")))
    db.commit()
    quote = price_enquiry(db, enquiry)
    assert quote.min_value_applied is True
    assert quote.quote_value == D("150.00")


def test_a_rush_uplift_only_applies_once_it_is_put_in_scope(db, enquiry, priceable_part, rates, rules):
    before = price_enquiry(db, enquiry).quote_value
    quote = current_quote(db, enquiry)
    quote.applied_rule_ids = [rules["rush_uplift"].id]
    db.commit()
    after = price_enquiry(db, enquiry).quote_value
    assert after > before
    keys = {a["rule_key"] for a in quote.adjustments}
    assert "rush_uplift" in keys


def test_an_inactive_rule_in_scope_is_ignored(db, enquiry, priceable_part, rates, rules):
    baseline = price_enquiry(db, enquiry).quote_value
    quote = current_quote(db, enquiry)
    quote.applied_rule_ids = [rules["rush_uplift"].id]
    rules["rush_uplift"].active = False
    db.commit()
    assert price_enquiry(db, enquiry).quote_value == baseline
