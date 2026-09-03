"""The bridge between the database and the deterministic engine.

This module reads operations, materials and rates out of the ORM, hands them to
`app.pricing` as plain data, and writes the results back. It contains no
pricing arithmetic of its own — every number it stores came out of the engine.

That separation is what makes the engine testable in isolation and keeps the
"code calculates" rule enforceable: if a price is ever wrong, there is exactly
one function that produced it.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import (
    EnquiryStatus,
    FlagCategory,
    FlagSeverity,
    JobType,
    QuoteStatus,
    TimeSource,
)
from app.models import Enquiry, MaterialRequirement, Operation, Part, Quote, QuoteLine
from app.nesting import NestingError, PartEnvelope, StockOption, nest
from app.pricing import (
    MaterialInput,
    MissingRate,
    OperationInput,
    PartInput,
    PartPrice,
    PricingError,
    QuotePrice,
    price_quote,
)
from app.services import flags as flag_service
from app.services.rates import resolve_rate, rules_in_scope, stock_options

logger = logging.getLogger(__name__)


class NotPriceable(Exception):
    """The enquiry cannot be priced yet, and why."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# --------------------------------------------------------------------------
# ORM -> engine inputs
# --------------------------------------------------------------------------
def _operation_input(db: Session, operation: Operation, *, on_date=None) -> OperationInput:
    """Read one operation, resolving its rate from the table.

    The resolved rate is written back onto the row along with the rate_table id
    it came from, so a quote stays auditable after the business changes a rate.
    """
    if operation.is_subcontract:
        return OperationInput(
            op_number=operation.op_number,
            process=operation.process,
            subcontract_unit_cost=operation.subcontract_unit_cost,
            time_source=operation.time_source,
            description=operation.description,
            operation_id=operation.id,
        )

    rate_row = resolve_rate(db, operation.process, on_date=on_date)
    operation.hourly_rate = Decimal(rate_row.hourly_rate)
    operation.rate_table_id = rate_row.id
    return OperationInput(
        op_number=operation.op_number,
        process=operation.process,
        set_time_mins=operation.set_time_mins,
        run_time_mins_per_unit=operation.run_time_mins_per_unit,
        hourly_rate=Decimal(rate_row.hourly_rate),
        time_source=operation.time_source,
        description=operation.description,
        operation_id=operation.id,
        rate_table_id=rate_row.id,
    )


def _material_input(requirement: MaterialRequirement) -> MaterialInput:
    return MaterialInput(
        qty_required=requirement.qty_required,
        unit_cost=requirement.unit_cost,
        total_cost=requirement.total_cost,
        spec=requirement.spec,
        material_requirement_id=requirement.id,
    )


def _part_input(db: Session, part: Part, *, on_date=None) -> PartInput:
    return PartInput(
        quantity=part.quantity,
        job_type=part.job_type,
        operations=tuple(_operation_input(db, op, on_date=on_date) for op in part.operations),
        materials=tuple(_material_input(m) for m in part.material_requirements),
        part_id=part.id,
        drawing_number=part.drawing_number,
        revision=part.revision,
        description=part.description,
    )


# --------------------------------------------------------------------------
# Nesting
# --------------------------------------------------------------------------
def compute_material(db: Session, part: Part) -> MaterialRequirement | None:
    """Run the nesting calculator for a full-supply part.

    Service-only parts get nothing: the customer supplies the material, so
    there is no purchase line to compute.
    """
    if part.job_type == JobType.SERVICE_ONLY.value:
        return None
    if not part.material:
        return None
    if None in (part.envelope_x, part.envelope_y, part.envelope_z):
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                "Cannot compute material: the part envelope is incomplete. "
                "Material for a full-supply job needs all three dimensions."
            ),
            dedupe_key="nesting_no_envelope",
        )
        db.flush()
        return None

    options = [
        StockOption(
            stock_id=row.id,
            spec=row.spec,
            stock_form=row.stock_form,
            length_mm=row.length_mm,
            width_mm=row.width_mm,
            thickness_mm=row.thickness_mm,
            unit_cost=row.unit_cost,
            kerf_mm=row.kerf_mm,
        )
        for row in stock_options(db, part.material)
    ]
    if not options:
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                f"No standard stock listed for '{part.material}'. A buyer needs "
                "to price the material before this can be quoted."
            ),
            dedupe_key="nesting_no_stock",
        )
        db.flush()
        return None

    try:
        result = nest(
            PartEnvelope(part.envelope_x, part.envelope_y, part.envelope_z),
            part.quantity,
            options,
        )
    except NestingError as exc:
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.BLOCK.value,
            message=f"Material nesting failed: {exc}",
            dedupe_key="nesting_failed",
        )
        db.flush()
        return None

    requirement = db.scalars(
        select(MaterialRequirement).where(MaterialRequirement.part_id == part.id)
    ).first()
    if requirement is None:
        requirement = MaterialRequirement(part_id=part.id)
        db.add(requirement)

    requirement.spec = part.material
    requirement.stock_form = result.stock_form
    requirement.stock_size = result.stock_size
    requirement.qty_required = result.qty_required
    requirement.unit_cost = result.unit_cost
    requirement.blanks_per_unit_stock = result.blanks_per_unit_stock
    requirement.utilisation_pct = result.utilisation_pct
    requirement.total_cost = result.total_cost
    db.flush()
    return requirement


# --------------------------------------------------------------------------
# Pricing an enquiry
# --------------------------------------------------------------------------
def _readiness_problems(db: Session, enquiry: Enquiry) -> list[str]:
    problems: list[str] = []
    if not enquiry.parts:
        problems.append("No parts on this enquiry — run extraction first.")
    for part in enquiry.parts:
        label = part.drawing_number or f"part {part.id}"
        if not part.operations:
            problems.append(f"{label} has no operations to cost.")
        if not part.quantity or part.quantity < 1:
            problems.append(
                f"{label} has no quantity. Nothing on the drawing or in the "
                "email stated one, and it is not assumed."
            )
        if part.job_type == JobType.AMBIGUOUS.value:
            # Not fatal: the workspace shows both cost paths. Recorded so the
            # estimator knows the figure is provisional.
            problems.append(
                f"{label} job type is still ambiguous — both cost paths shown, "
                "neither is the quote."
            )
    return problems


def current_quote(db: Session, enquiry: Enquiry) -> Quote | None:
    """The quote currently being worked on, if any.

    A sent quote is frozen; work after that starts a new version.
    """
    return db.scalars(
        select(Quote)
        .where(
            Quote.enquiry_id == enquiry.id,
            Quote.status.notin_([QuoteStatus.SENT.value, QuoteStatus.SUPERSEDED.value]),
        )
        .order_by(Quote.version.desc())
    ).first()


def _next_version(db: Session, enquiry_id: int) -> int:
    highest = db.scalars(
        select(Quote.version).where(Quote.enquiry_id == enquiry_id).order_by(Quote.version.desc())
    ).first()
    return (highest or 0) + 1


def price_enquiry(
    db: Session,
    enquiry: Enquiry,
    *,
    margin_pct: Decimal | None = None,
    recompute_material: bool = True,
    on_date=None,
) -> Quote:
    """Recompute the enquiry's working quote from its current inputs.

    Idempotent, as `/enquiries/:id/price` promises: calling it twice with
    unchanged inputs produces the same quote row with the same numbers.
    """
    problems = _readiness_problems(db, enquiry)
    fatal = [p for p in problems if "ambiguous" not in p]
    if fatal:
        raise NotPriceable(fatal)

    if recompute_material:
        for part in enquiry.parts:
            compute_material(db, part)

    if margin_pct is None:
        margin_pct = (
            Decimal(enquiry.customer.default_margin_pct)
            if enquiry.customer is not None
            else Decimal("0")
        )

    quote = current_quote(db, enquiry)
    if quote is None:
        quote = Quote(
            enquiry_id=enquiry.id,
            version=_next_version(db, enquiry.id),
            status=QuoteStatus.DRAFT.value,
        )
        db.add(quote)
        db.flush()

    try:
        part_inputs = [_part_input(db, part, on_date=on_date) for part in enquiry.parts]
        priced = price_quote(
            part_inputs,
            margin_pct,
            rules_in_scope(db, quote.applied_rule_ids),
        )
    except MissingRate as exc:
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            quote_id=quote.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                f"{exc} — add a rate in /admin/rates before this can be quoted. "
                "No default rate is assumed."
            ),
            dedupe_key=f"missing_rate:{exc.process}",
        )
        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        db.flush()
        raise NotPriceable([str(exc)]) from exc
    except PricingError as exc:
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            quote_id=quote.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.BLOCK.value,
            message=f"Pricing failed: {exc}",
            dedupe_key="pricing_failed",
        )
        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        db.flush()
        raise NotPriceable([str(exc)]) from exc

    _persist(db, enquiry, quote, priced)

    if enquiry.status in (
        EnquiryStatus.RECEIVED.value,
        EnquiryStatus.EXTRACTING.value,
        EnquiryStatus.EXTRACTED.value,
        EnquiryStatus.CLASSIFIED.value,
        EnquiryStatus.PRICED.value,
    ):
        enquiry.status = EnquiryStatus.PRICED.value

    db.flush()
    return quote


def _persist(db: Session, enquiry: Enquiry, quote: Quote, priced: QuotePrice) -> None:
    """Write the engine's output onto the quote, its lines and its operations."""
    parts_by_id = {p.id: p for p in enquiry.parts}

    for part_price in priced.parts:
        part = parts_by_id.get(part_price.part_id)
        if part is None:
            continue
        costs_by_op_id = {
            oc.operation_id: oc for oc in part_price.operation_costs if oc.operation_id
        }
        for operation in part.operations:
            cost = costs_by_op_id.get(operation.id)
            if cost is not None:
                operation.computed_cost = cost.computed_cost

    quote.material_total = priced.material_total
    quote.labour_total = priced.labour_total
    quote.subtotal = priced.subtotal
    quote.margin_pct = priced.margin_pct
    quote.margin_value = priced.margin_value
    quote.quote_value = priced.quote_value
    quote.min_value_applied = priced.min_value_applied
    quote.adjustments = [a.as_dict() for a in priced.adjustments] or None
    if quote.lead_time_days is None and enquiry.customer is not None:
        quote.lead_time_days = enquiry.customer.default_lead_days

    _sync_lines(db, quote, priced)
    db.flush()


def _sync_lines(db: Session, quote: Quote, priced: QuotePrice) -> None:
    """Replace the quote's lines with the freshly priced ones."""
    existing = {line.part_id: line for line in quote.lines}
    seen: set[int | None] = set()

    for part_price in priced.parts:
        line = existing.get(part_price.part_id)
        if line is None:
            line = QuoteLine(quote_id=quote.id, part_id=part_price.part_id)
            db.add(line)
            quote.lines.append(line)
        line.quantity = part_price.quantity
        line.unit_price = part_price.unit_price
        line.line_total = part_price.line_total
        line.drawing_number = part_price.drawing_number
        line.revision = part_price.revision
        line.description = part_price.description
        seen.add(part_price.part_id)

    for part_id, line in existing.items():
        if part_id not in seen:
            db.delete(line)


# --------------------------------------------------------------------------
# Presentation helpers
# --------------------------------------------------------------------------
def price_breakdown(db: Session, enquiry: Enquiry, quote: Quote) -> QuotePrice:
    """Re-derive the full build-up for display, without persisting anything.

    Used by the workspace so the cost build-up an estimator reads is produced
    by the same engine that produced the quoted figure, not by a second
    implementation that could drift from it.
    """
    return price_quote(
        [_part_input(db, part) for part in enquiry.parts],
        Decimal(quote.margin_pct),
        rules_in_scope(db, quote.applied_rule_ids),
    )


def ambiguous_cost_paths(db: Session, part: Part) -> dict[str, PartPrice]:
    """Both cost paths for an ambiguous part (spec stage 3).

    "On ambiguous, compute and display both cost paths — do not guess."
    """
    from app.pricing import price_part

    base = _part_input(db, part)
    margin = (
        Decimal(part.enquiry.customer.default_margin_pct)
        if part.enquiry.customer is not None
        else Decimal("0")
    )
    service_only = PartInput(
        quantity=base.quantity,
        job_type=JobType.SERVICE_ONLY.value,
        operations=base.operations,
        materials=(),
        part_id=base.part_id,
        drawing_number=base.drawing_number,
        revision=base.revision,
        description=base.description,
    )
    full_supply = PartInput(
        quantity=base.quantity,
        job_type=JobType.FULL_SUPPLY.value,
        operations=base.operations,
        materials=base.materials,
        part_id=base.part_id,
        drawing_number=base.drawing_number,
        revision=base.revision,
        description=base.description,
    )
    return {
        JobType.SERVICE_ONLY.value: price_part(service_only, margin),
        JobType.FULL_SUPPLY.value: price_part(full_supply, margin),
    }
