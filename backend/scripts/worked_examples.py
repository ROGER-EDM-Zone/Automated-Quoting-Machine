"""Two real jobs, priced end to end.

    python -m scripts.worked_examples

Both come from drawings EDM Zone actually sent, and both run through the same
three modules the app uses — `calculators`, `nesting`, `pricing` — so the
figures on the status document are reproducible rather than typed by hand.

It exists for the reason Paul gave for building the whole thing: real jobs
first, software second. When a set time or a material price is corrected,
re-run this and the difference is visible immediately.

Nothing here is a fixture and nothing is a test. The set times and the
material price are marked in the output as the estimates they are.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from app.calculators import FIRST_CUT_SPEED, WireCut, wire_cut_time
from app.enums import JobType, Process, StockForm, TimeSource
from app.judgement import is_long_runner, rate_ladder
from app.nesting import Allowance, PartEnvelope, StockOption, nest
from app.pricing import MaterialInput, OperationInput, PartInput, price_quote

#: The shop's charge-out rate, confirmed 10 September 2026 over Paul's older £60.
RATE = Decimal("65")

#: EN16 density. A physical constant, not an estimate.
DENSITY_KG_M3 = Decimal("7850")

#: ESTIMATE. Ballpark UK trade price for EN16, not a supplier's figure and not
#: EDM Zone's account price. It is 41% of example B, so it is the number most
#: worth replacing with a real one.
EN16_GBP_PER_KG = Decimal("2.50")


def q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rule(text: str) -> None:
    print(f"\n{text}\n{'=' * len(text)}")


def bar_weight_kg(diameter_mm: int, length_mm: int) -> Decimal:
    radius_m = Decimal(diameter_mm) / 2000
    length_m = Decimal(length_mm) / 1000
    return Decimal(str(math.pi)) * radius_m**2 * length_m * DENSITY_KG_M3


def example_a() -> None:
    """Wire EDM only, free issue — Auto Elect cycloidal housing GEN4-PRT-0001 A."""
    rule("A — WIRE EDM ONLY — Cycloidal housing GEN4-PRT-0001 rev A — 10 off")
    quantity = 10

    # The cycloidal bore. An 8-lobe form runs longer than the circle it sits
    # on; 1.15 is an ESTIMATE and the weakest figure in this example. The
    # drawing offers a STEP file, which would replace it with the exact path.
    circle_mm = Decimal(str(round(math.pi * 68, 1)))
    lobe_factor = Decimal("1.15")
    path_mm = q2(circle_mm * lobe_factor)
    print(f"  bore path   pi x 68 = {circle_mm} x {lobe_factor} = {path_mm} mm  [ESTIMATE]")

    cut = WireCut(
        job_height_mm=Decimal("13.6"),
        cut_length_mm=path_mm,
        finish_cuts=3,  # Ra 0.4 on the bore
        threads=1,
    )
    result = wire_cut_time(cut)
    print(
        f"  13.6mm sits between table rows 10mm ({FIRST_CUT_SPEED[10]}) "
        f"and 15mm ({FIRST_CUT_SPEED[15]})"
    )
    print(
        f"  speeds      {result.first_cut_speed} roughing / "
        f"{result.finish_cut_speed} skimming mm/min "
        f"(interpolated={result.height_interpolated})"
    )
    print(
        f"  run time    {result.first_cut_mins} + {result.finish_cut_mins} "
        f"+ {result.threading_mins} = {result.total_mins} min per part"
    )

    operations = [
        # The only calculator-sourced time in either example.
        OperationInput(
            10,
            Process.WIRE_EDM.value,
            Decimal("45"),
            result.total_mins,
            RATE,
            time_source=TimeSource.CALCULATOR.value,
            description="Wire cycloidal bore — 1 cut + 3 skims to Ra 0.4",
        ),
        OperationInput(
            20,
            Process.QC.value,
            Decimal("15"),
            Decimal("4"),
            RATE,
            time_source=TimeSource.MANUAL.value,
            description="Inspect bore form, 0.01 runout to A|B",
        ),
    ]
    quote = price_quote(
        [
            PartInput(
                quantity,
                JobType.SERVICE_ONLY.value,
                operations,
                [],
                drawing_number="GEN4-PRT-0001",
                revision="A",
                description="Cycloidal housing STD",
            )
        ],
        Decimal("0"),
        [],
    )
    report(quote, quantity)

    hours = sum(op.total_mins for op in quote.parts[0].operation_costs) / 60
    print(f"\n  {q2(hours)} hours — long runner? {is_long_runner(hours)}")
    if is_long_runner(hours):
        print("  Paul's ladder (a person picks the rung, and says why):")
        for rung in rate_ladder(RATE, hours):
            mark = "  <- standard" if rung.is_standard else ""
            print(f"    £{rung.hourly_rate}/h -> £{rung.value}{mark}")


def example_b() -> None:
    """Full supply — Ricardo MTC oil feed plate 67980 iss 1, EN16T."""
    rule("B — FULL SUPPLY — Oil feed plate 67980 iss 1, EN16T — 15 off")
    quantity = 15
    finished_dia = Decimal("79.7")
    finished_len = Decimal("12")

    # The shop's own rule, from the rules table: 4mm on the section, 10mm on
    # the length. Without it the nester buys bar the size of the finished part.
    allowance = Allowance(section_mm=Decimal("4"), length_mm=Decimal("10"))

    stock = []
    for index, diameter in enumerate([70, 75, 80, 85, 90, 100], start=1):
        kilos = bar_weight_kg(diameter, 3000)
        stock.append(
            StockOption(
                stock_id=index,
                spec="EN16T",
                stock_form=StockForm.BAR_ROUND.value,
                length_mm=Decimal("3000"),
                width_mm=Decimal(diameter),
                thickness_mm=None,
                unit_cost=q2(kilos * EN16_GBP_PER_KG),
                kerf_mm=Decimal("3"),
            )
        )

    nested = nest(
        PartEnvelope(finished_dia, finished_dia, finished_len, is_rotational=True),
        quantity,
        stock,
        allowance=allowance,
    )
    print(
        f"  needs       dia {nested.required_section_mm} "
        f"({finished_dia} + {allowance.section_mm}) and "
        f"{nested.required_length_mm}mm of bar ({finished_len} + {allowance.length_mm})"
    )
    print(f"  chosen      {nested.stock_label}, {nested.section_oversize_mm}mm oversize")
    print(
        f"  buying      {nested.qty_required} bar(s), {nested.blanks_per_unit_stock} "
        f"blanks each, utilisation {nested.utilisation_pct}%"
    )
    print(f"  material    £{nested.total_cost}  [rate per kg is an ESTIMATE]")

    # Every time below is MANUAL: there is no turning or milling calculator.
    # That is the point of the example — the tags differ from example A.
    operations = [
        OperationInput(10, Process.CNC_TURN.value, Decimal("60"), Decimal("14"), RATE,
                       time_source=TimeSource.MANUAL.value,
                       description="Turn 79.7 dia, 60 deg cone, 1.2 web, R2 radii, part off"),
        OperationInput(20, Process.CNC_MILL.value, Decimal("30"), Decimal("3"), RATE,
                       time_source=TimeSource.MANUAL.value,
                       description="4mm and 6mm bores, 1.0 x 45 deg chamfer"),
        OperationInput(30, Process.LASER_ETCH.value, Decimal("15"), Decimal("1"), RATE,
                       time_source=TimeSource.MANUAL.value,
                       description="Etch part number and issue at position P"),
        OperationInput(40, Process.QC.value, Decimal("15"), Decimal("3"), RATE,
                       time_source=TimeSource.MANUAL.value,
                       description="Inspect 79.7 +/-0.2 and the 1.2 -0/-0.2 web"),
    ]
    quote = price_quote(
        [
            PartInput(
                quantity,
                JobType.FULL_SUPPLY.value,
                operations,
                [
                    MaterialInput(
                        nested.qty_required, nested.unit_cost, nested.total_cost, "EN16T"
                    )
                ],
                drawing_number="67980",
                revision="1",
                description="Oil feed plate",
            )
        ],
        Decimal("0"),
        [],
    )
    report(quote, quantity)

    per_part_now = q2(nested.total_cost / quantity)
    per_part_full = q2(nested.total_cost / nested.blanks_per_unit_stock)
    print(
        f"\n  Utilisation flag: material is £{per_part_now} a part at {quantity} off, "
        f"and £{per_part_full} if the whole bar were used."
    )


def report(quote, quantity: int) -> None:
    part = quote.parts[0]
    print()
    for op in part.operation_costs:
        print(
            f"  op{op.op_number:<3} {op.process:<12} {op.total_mins:>9} min  "
            f"[{op.time_source:<20}] £{op.computed_cost}"
        )
    print(f"  {'labour':<17}{part.labour_total:>18}")
    print(f"  {'material':<17}{part.material_total:>18}")
    print(f"  {'QUOTE':<17}{quote.quote_value:>18}   £{part.unit_price} each x {quantity}")
    # The build-up must add up to the quoted figure, or something is wrong.
    assert quote.reconciles(), "quote does not reconcile"


def main() -> int:
    example_a()
    example_b()
    print(
        "\nEvery 'manual' time above is an estimate awaiting the shop's own figure.\n"
        "Only the wire cutting time is calculated, because only wire has a\n"
        "calculator. Replace a number, re-run, and the difference is visible.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
