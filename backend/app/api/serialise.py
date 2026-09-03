"""Turning engine output into response models."""

from __future__ import annotations

from app.pricing import PartPrice, QuotePrice
from app.schemas import BreakdownOut, OperationCostOut, PartPriceOut


def part_price_out(priced: PartPrice) -> PartPriceOut:
    return PartPriceOut(
        part_id=priced.part_id,
        quantity=priced.quantity,
        labour_total=priced.labour_total,
        material_total=priced.material_total,
        subtotal=priced.subtotal,
        margin_value=priced.margin_value,
        value=priced.value,
        unit_price=priced.unit_price,
        line_total=priced.line_total,
        uses_untrusted_times=priced.uses_untrusted_times,
        operation_costs=[
            OperationCostOut(
                op_number=oc.op_number,
                process=oc.process,
                time_source=oc.time_source,
                total_mins=oc.total_mins,
                hourly_rate=oc.hourly_rate,
                computed_cost=oc.computed_cost,
                is_subcontract=oc.is_subcontract,
            )
            for oc in priced.operation_costs
        ],
    )


def breakdown_out(priced: QuotePrice) -> BreakdownOut:
    return BreakdownOut(
        labour_total=priced.labour_total,
        material_total=priced.material_total,
        subtotal=priced.subtotal,
        margin_pct=priced.margin_pct,
        margin_value=priced.margin_value,
        quote_value=priced.quote_value,
        rounding_adjustment=priced.rounding_adjustment,
        min_value_applied=priced.min_value_applied,
        uses_untrusted_times=priced.uses_untrusted_times,
        reconciles=priced.reconciles(),
        adjustments=[a.as_dict() for a in priced.adjustments],
        parts=[part_price_out(p) for p in priced.parts],
    )
