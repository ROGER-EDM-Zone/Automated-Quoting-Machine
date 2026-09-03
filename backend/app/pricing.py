"""The deterministic quoting engine (spec stage 4).

This module is plain arithmetic and nothing else. Given the same operations,
times, rates and rules it returns the same numbers every time. It imports no
AI client, makes no network calls, reads no clock and no database — the caller
resolves rates and rules and hands them in.

The AI's job is producing and correcting the *inputs* to this function. It
never produces the output number (spec section 6).

Rounding policy, fixed here so it is reviewable in one place:
  * every component cost (operation, material line) is rounded to 2dp with
    ROUND_HALF_UP, so the cost build-up an estimator reads on screen adds up
    to the totals exactly;
  * a line's unit price is rounded to 2dp and the line total is
    ``unit_price * quantity``, so the customer-facing arithmetic is exact;
  * the residue between that and the raw part value is reported as
    ``rounding_adjustment`` rather than silently absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from app.enums import AdjustmentType, JobType, Process, RuleKey, TimeSource

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")
MINUTES_PER_HOUR = Decimal("60")


def money(value: Decimal | int | str) -> Decimal:
    """Round to 2dp, half-up. The only rounding entry point in this module."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


class PricingError(Exception):
    """The inputs cannot be priced. Never guessed around."""


class MissingRate(PricingError):
    """No rate_table row covers this process on this date.

    Deliberately fatal rather than defaulted: a made-up rate is exactly the
    kind of confidently-wrong number the spec exists to prevent.
    """

    def __init__(self, process: str, machine_group: str | None = None) -> None:
        self.process = process
        self.machine_group = machine_group
        group = f" / {machine_group}" if machine_group else ""
        super().__init__(f"No effective rate for process '{process}'{group}")


# --------------------------------------------------------------------------
# Inputs — plain data, no ORM, no I/O
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class OperationInput:
    op_number: int
    process: str
    set_time_mins: Decimal = ZERO
    run_time_mins_per_unit: Decimal = ZERO
    #: Resolved from rate_table by the caller. Required unless subcontract.
    hourly_rate: Decimal | None = None
    #: Per-unit bought-out cost. Required when process == subcontract.
    subcontract_unit_cost: Decimal | None = None
    time_source: str = TimeSource.MANUAL.value
    description: str | None = None
    operation_id: int | None = None
    rate_table_id: int | None = None

    @property
    def is_subcontract(self) -> bool:
        return self.process == Process.SUBCONTRACT.value


@dataclass(frozen=True)
class MaterialInput:
    qty_required: Decimal = ZERO
    unit_cost: Decimal = ZERO
    #: Pre-computed by the nesting calculator. When None it is derived here as
    #: qty_required * unit_cost.
    total_cost: Decimal | None = None
    spec: str | None = None
    material_requirement_id: int | None = None

    def resolved_total(self) -> Decimal:
        if self.total_cost is not None:
            return money(self.total_cost)
        return money(Decimal(self.qty_required) * Decimal(self.unit_cost))


@dataclass(frozen=True)
class PartInput:
    quantity: int
    job_type: str
    operations: Sequence[OperationInput] = field(default_factory=tuple)
    materials: Sequence[MaterialInput] = field(default_factory=tuple)
    part_id: int | None = None
    drawing_number: str | None = None
    revision: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class AdjustmentRule:
    """One active rules_table row, as handed to the engine."""

    rule_id: int
    rule_key: str
    adjustment_type: str
    adjustment_value: Decimal
    trigger_description: str | None = None


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class OperationCost:
    op_number: int
    process: str
    operation_id: int | None
    time_source: str
    total_mins: Decimal
    hourly_rate: Decimal | None
    computed_cost: Decimal
    is_subcontract: bool


@dataclass(frozen=True)
class MaterialCost:
    material_requirement_id: int | None
    spec: str | None
    total_cost: Decimal


@dataclass(frozen=True)
class PartPrice:
    part_id: int | None
    quantity: int
    job_type: str
    drawing_number: str | None
    revision: str | None
    description: str | None
    operation_costs: tuple[OperationCost, ...]
    material_costs: tuple[MaterialCost, ...]
    labour_total: Decimal
    material_total: Decimal
    subtotal: Decimal
    margin_pct: Decimal
    margin_value: Decimal
    #: Part value before proportional (pct) rules were applied.
    value_before_adjustments: Decimal
    #: Part value after pct rules, before rounding to a unit price.
    value: Decimal
    unit_price: Decimal
    line_total: Decimal
    #: line_total - value. Reported, never hidden.
    rounding_adjustment: Decimal

    @property
    def uses_untrusted_times(self) -> bool:
        """True when any operation's minutes are an AI historical estimate."""
        return any(
            oc.time_source == TimeSource.HISTORICAL_ESTIMATE.value
            for oc in self.operation_costs
        )


@dataclass(frozen=True)
class AppliedAdjustment:
    rule_id: int | None
    rule_key: str
    adjustment_type: str
    adjustment_value: Decimal
    #: Cash effect on the quote total.
    effect: Decimal
    description: str | None = None

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_key": self.rule_key,
            "adjustment_type": self.adjustment_type,
            "adjustment_value": str(self.adjustment_value),
            "effect": str(self.effect),
            "description": self.description,
        }


@dataclass(frozen=True)
class QuotePrice:
    parts: tuple[PartPrice, ...]
    labour_total: Decimal
    material_total: Decimal
    subtotal: Decimal
    margin_pct: Decimal
    margin_value: Decimal
    adjustments: tuple[AppliedAdjustment, ...]
    quote_value: Decimal
    min_value_applied: bool
    rounding_adjustment: Decimal

    @property
    def uses_untrusted_times(self) -> bool:
        return any(p.uses_untrusted_times for p in self.parts)

    def reconciles(self) -> bool:
        """Does the build-up add up to the quoted figure?

        subtotal + margin + adjustments + rounding == quote_value. Asserted in
        the tests; also worth calling before persisting a quote.
        """
        total = (
            self.subtotal
            + self.margin_value
            + sum((a.effect for a in self.adjustments), ZERO)
            + self.rounding_adjustment
        )
        return money(total) == self.quote_value


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------
def price_operation(op: OperationInput, quantity: int) -> OperationCost:
    """Cost one operation.

    Subcontract:  cost = subcontract_unit_cost * quantity
    Otherwise:    total_mins = set_time + (run_time * quantity)
                  cost       = (total_mins / 60) * hourly_rate
    """
    if quantity < 1:
        raise PricingError(f"Quantity must be at least 1, got {quantity}")

    qty = Decimal(quantity)

    if op.is_subcontract:
        if op.subcontract_unit_cost is None:
            raise PricingError(
                f"Op {op.op_number} is subcontract but has no subcontract_unit_cost"
            )
        cost = money(Decimal(op.subcontract_unit_cost) * qty)
        return OperationCost(
            op_number=op.op_number,
            process=op.process,
            operation_id=op.operation_id,
            time_source=op.time_source,
            total_mins=ZERO,
            hourly_rate=None,
            computed_cost=cost,
            is_subcontract=True,
        )

    if op.hourly_rate is None:
        raise MissingRate(op.process)

    total_mins = Decimal(op.set_time_mins) + (Decimal(op.run_time_mins_per_unit) * qty)
    if total_mins < 0:
        raise PricingError(f"Op {op.op_number} has negative total minutes")
    cost = money((total_mins / MINUTES_PER_HOUR) * Decimal(op.hourly_rate))
    return OperationCost(
        op_number=op.op_number,
        process=op.process,
        operation_id=op.operation_id,
        time_source=op.time_source,
        total_mins=total_mins.quantize(CENTS, rounding=ROUND_HALF_UP),
        hourly_rate=money(op.hourly_rate),
        computed_cost=cost,
        is_subcontract=False,
    )


def _pct_multiplier(rules: Sequence[AdjustmentRule]) -> Decimal:
    """Combined proportional uplift, as a multiplier.

    Percentages are summed, not compounded — a 10% rush plus a 5% contingency
    is 15%, which is how the business states them. Summing is also
    order-independent, so the result cannot depend on rule iteration order.
    """
    total_pct = sum(
        (
            Decimal(r.adjustment_value)
            for r in rules
            if r.adjustment_type == AdjustmentType.PCT.value
        ),
        Decimal("0"),
    )
    return Decimal("1") + (total_pct / Decimal("100"))


def price_part(
    part: PartInput,
    margin_pct: Decimal,
    pct_rules: Sequence[AdjustmentRule] = (),
) -> PartPrice:
    """Cost and price one part."""
    if part.quantity < 1:
        raise PricingError(f"Part {part.part_id} has quantity {part.quantity}")

    op_costs = tuple(price_operation(op, part.quantity) for op in part.operations)
    labour_total = money(sum((oc.computed_cost for oc in op_costs), ZERO))

    # Material is zero for service-only work: the customer supplies it.
    if part.job_type == JobType.SERVICE_ONLY.value:
        mat_costs: tuple[MaterialCost, ...] = ()
        material_total = ZERO
    else:
        mat_costs = tuple(
            MaterialCost(
                material_requirement_id=m.material_requirement_id,
                spec=m.spec,
                total_cost=m.resolved_total(),
            )
            for m in part.materials
        )
        material_total = money(sum((mc.total_cost for mc in mat_costs), ZERO))

    subtotal = money(labour_total + material_total)
    margin_pct = Decimal(margin_pct)
    margin_value = money(subtotal * (margin_pct / Decimal("100")))
    value_before = money(subtotal + margin_value)

    value = money(value_before * _pct_multiplier(pct_rules))
    unit_price = money(value / Decimal(part.quantity))
    line_total = money(unit_price * Decimal(part.quantity))

    return PartPrice(
        part_id=part.part_id,
        quantity=part.quantity,
        job_type=part.job_type,
        drawing_number=part.drawing_number,
        revision=part.revision,
        description=part.description,
        operation_costs=op_costs,
        material_costs=mat_costs,
        labour_total=labour_total,
        material_total=material_total,
        subtotal=subtotal,
        margin_pct=margin_pct,
        margin_value=margin_value,
        value_before_adjustments=value_before,
        value=value,
        unit_price=unit_price,
        line_total=line_total,
        rounding_adjustment=money(line_total - value),
    )


def price_quote(
    parts: Sequence[PartInput],
    margin_pct: Decimal,
    rules: Sequence[AdjustmentRule] = (),
) -> QuotePrice:
    """Price a whole quote.

    ``rules`` are the *active, matched* rules_table rows. Deciding which rules
    match is the caller's job (and ultimately a human's, via the admin UI);
    applying them is this function's. Percentage rules scale each line so the
    customer-facing unit prices carry them; fixed rules and the minimum-value
    floor land on the quote total as their own visible entries, which is how a
    quote reads on paper.
    """
    if not parts:
        raise PricingError("Cannot price a quote with no parts")

    pct_rules = [r for r in rules if r.adjustment_type == AdjustmentType.PCT.value]
    fixed_rules = [
        r
        for r in rules
        if r.adjustment_type == AdjustmentType.FIXED.value
        and r.rule_key != RuleKey.MIN_QUOTE_VALUE.value
    ]
    # `flag_only` rules deliberately have no arithmetic effect here; the
    # classification stage raises them as flags for a human to judge.

    priced_parts = tuple(price_part(p, margin_pct, pct_rules) for p in parts)

    labour_total = money(sum((p.labour_total for p in priced_parts), ZERO))
    material_total = money(sum((p.material_total for p in priced_parts), ZERO))
    subtotal = money(sum((p.subtotal for p in priced_parts), ZERO))
    margin_value = money(sum((p.margin_value for p in priced_parts), ZERO))
    rounding_adjustment = money(sum((p.rounding_adjustment for p in priced_parts), ZERO))
    lines_total = money(sum((p.line_total for p in priced_parts), ZERO))

    applied: list[AppliedAdjustment] = []

    # Proportional rules: itemise the cash effect they had on the lines.
    if pct_rules:
        base = money(sum((p.value_before_adjustments for p in priced_parts), ZERO))
        uplifted = money(sum((p.value for p in priced_parts), ZERO))
        remaining = money(uplifted - base)
        total_pct = sum((Decimal(r.adjustment_value) for r in pct_rules), Decimal("0"))
        ordered = sorted(pct_rules, key=lambda r: r.rule_id)
        for index, rule in enumerate(ordered):
            # Split by each rule's share of the summed percentage; the last
            # rule absorbs the residue so the entries sum exactly.
            if index == len(ordered) - 1 or total_pct == 0:
                effect = remaining
            else:
                effect = money(
                    (uplifted - base) * (Decimal(rule.adjustment_value) / total_pct)
                )
            remaining = money(remaining - effect)
            applied.append(
                AppliedAdjustment(
                    rule_id=rule.rule_id,
                    rule_key=rule.rule_key,
                    adjustment_type=rule.adjustment_type,
                    adjustment_value=Decimal(rule.adjustment_value),
                    effect=effect,
                    description=rule.trigger_description,
                )
            )

    running = lines_total
    for rule in sorted(fixed_rules, key=lambda r: r.rule_id):
        effect = money(rule.adjustment_value)
        running = money(running + effect)
        applied.append(
            AppliedAdjustment(
                rule_id=rule.rule_id,
                rule_key=rule.rule_key,
                adjustment_type=rule.adjustment_type,
                adjustment_value=Decimal(rule.adjustment_value),
                effect=effect,
                description=rule.trigger_description,
            )
        )

    # Minimum quote value acts as a floor, applied last (spec stage 4).
    min_value_applied = False
    floors = [r for r in rules if r.rule_key == RuleKey.MIN_QUOTE_VALUE.value]
    if floors:
        floor = max(money(r.adjustment_value) for r in floors)
        if running < floor:
            floor_rule = max(
                (r for r in floors if money(r.adjustment_value) == floor),
                key=lambda r: r.rule_id,
            )
            applied.append(
                AppliedAdjustment(
                    rule_id=floor_rule.rule_id,
                    rule_key=RuleKey.MIN_QUOTE_VALUE.value,
                    adjustment_type=floor_rule.adjustment_type,
                    adjustment_value=floor,
                    effect=money(floor - running),
                    description=floor_rule.trigger_description
                    or "Minimum quote value applied",
                )
            )
            running = floor
            min_value_applied = True

    return QuotePrice(
        parts=priced_parts,
        labour_total=labour_total,
        material_total=material_total,
        subtotal=subtotal,
        margin_pct=Decimal(margin_pct),
        margin_value=margin_value,
        adjustments=tuple(applied),
        quote_value=money(running),
        min_value_applied=min_value_applied,
        rounding_adjustment=rounding_adjustment,
    )
