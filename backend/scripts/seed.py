"""Seed a development database.

Creates the reference data nothing works without (rates, rules, stock), one
customer, and a worked example enquiry so the workspace has something in it.

EVERY FIGURE HERE IS INVENTED (spec section 9). The rates, the margin, the
stock prices and the cycle times are placeholders chosen to show the shape of
the output. Real rates, real cycle times and real margin policy must come from
the business before anything is quoted for a customer — seeding this into a
production database would be a serious mistake, so the script refuses to.

    python -m scripts.seed              # reference data only
    python -m scripts.seed --example    # also build a worked example enquiry
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.enums import (
    AdjustmentType,
    AttachmentKind,
    FlagCategory,
    FlagSeverity,
    JobType,
    MarketBasis,
    MarketKind,
    MarketMethod,
    MarketUnit,
    Process,
    RuleKey,
    StockForm,
    TimeSource,
)
from app.models import (
    Attachment,
    Customer,
    Enquiry,
    Flag,
    MarketObservation,
    MarketSource,
    Operation,
    Part,
    RateTable,
    RulesTable,
    StockSize,
    utcnow,
)
from app.services.storage import attachment_key, content_hash, get_storage

# --- illustrative placeholders, not real rates -----------------------------
RATES: dict[str, str] = {
    Process.CNC_MILL.value: "55.00",
    Process.CNC_TURN.value: "52.00",
    Process.WIRE_EDM.value: "42.00",
    Process.SPARK_ERODE.value: "38.00",
    Process.GRIND.value: "40.00",
    Process.MANUAL.value: "35.00",
    Process.QC.value: "30.00",
}

RULES = [
    (
        RuleKey.MIN_QUOTE_VALUE.value,
        "Minimum order value — below this we quote the minimum",
        AdjustmentType.FIXED.value,
        "150.00",
    ),
    (
        RuleKey.RUSH_UPLIFT.value,
        "Customer needs delivery inside 5 working days",
        AdjustmentType.PCT.value,
        "15.00",
    ),
    (
        RuleKey.DIFFICULT_JOB_CONTINGENCY.value,
        "Tolerance tighter than 0.01mm, unfamiliar material, or thin walls",
        AdjustmentType.PCT.value,
        "10.00",
    ),
    # Millimetres, not money. Without these the calculator sizes stock to the
    # finished part and buys bar with nothing left to clean up.
    (
        RuleKey.MATERIAL_ALLOWANCE_SECTION.value,
        "Material left on the diameter, or on each section face, for clean-up",
        AdjustmentType.MM.value,
        "4",
    ),
    (
        RuleKey.MATERIAL_ALLOWANCE_LENGTH.value,
        "Material left on the length of one part, before the parting kerf",
        AdjustmentType.MM.value,
        "4",
    ),
]

#: Densities, kg/m3. Real physical constants rather than invented figures —
#: without one, a per-kilo price cannot cost a bar and the job flags instead.
DENSITY = {
    "EN16": "7850",
    "EN30B": "7850",
    "1.2312": "7850",
}

#: A supplier's round-bar range for EN16, and both forms for EN30B so the
#: shape choice is visible: a square part should buy square bar.
LIVE_RANGES = [
    ("EN16", StockForm.BAR_ROUND.value, "material:en16:round_bar", [60, 70, 80, 90, 100, 110, 120]),
    ("EN30B", StockForm.BAR_ROUND.value, "material:en30b:round_bar", [60, 70, 80, 90]),
    ("EN30B", StockForm.BAR_SQUARE.value, "material:en30b:square_bar", [40, 50, 55, 60, 70]),
]

STOCK = [
    ("1.2312", StockForm.PLATE.value, "500", "250", "30", "96.00"),
    ("1.2312", StockForm.PLATE.value, "1000", "500", "30", "340.00"),
    ("EN24T", StockForm.BAR_ROUND.value, "1000", "60", None, "45.00"),
    ("EN24T", StockForm.BAR_ROUND.value, "1000", "100", None, "112.00"),
]


def example_drawing_pdf() -> bytes:
    """A stand-in drawing, so the extraction path has a real PDF to rasterise."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 60), "DRAWING No. 4471", fontsize=16)
    page.insert_text((60, 90), "REV: B")
    page.insert_text((60, 110), "TITLE: BRACKET")
    page.insert_text((60, 130), "MATERIAL: 1.2312")
    page.insert_text((60, 150), "OVERALL: 120 x 80 x 25 mm")
    page.insert_text((60, 170), "TOLERANCE UNLESS STATED: +/- 0.1")
    page.insert_text((60, 190), "CRITICAL BORE: +0.000 / -0.013")
    page.insert_text((60, 210), "FINISH: Ra 1.6")
    page.insert_text((60, 240), "NOTE: TWO INTERNAL CORNERS R0.5 - WIRE REQUIRED")
    page.draw_rect(pymupdf.Rect(60, 270, 380, 470))
    data = doc.tobytes()
    doc.close()
    return data


def seed_reference_data(db) -> None:
    since = date.today() - timedelta(days=365)

    if db.query(RateTable).count() == 0:
        db.add_all(
            RateTable(process=process, hourly_rate=Decimal(rate), effective_from=since)
            for process, rate in RATES.items()
        )
        print(f"  added {len(RATES)} rates")

    if db.query(RulesTable).count() == 0:
        db.add_all(
            RulesTable(
                rule_key=key,
                trigger_description=description,
                adjustment_type=kind,
                adjustment_value=Decimal(value),
                active=True,
                last_reviewed_at=utcnow(),
            )
            for key, description, kind, value in RULES
        )
        print(f"  added {len(RULES)} rules")

    if db.query(StockSize).count() == 0:
        db.add_all(
            StockSize(
                spec=spec,
                stock_form=form,
                length_mm=Decimal(length),
                width_mm=Decimal(width) if width else None,
                thickness_mm=Decimal(thickness) if thickness else None,
                unit_cost=Decimal(cost),
                kerf_mm=Decimal("4"),
            )
            for spec, form, length, width, thickness, cost in STOCK
        )
        print(f"  added {len(STOCK)} stock sizes")

    seed_market(db)
    db.commit()


def seed_market(db) -> None:
    """Market sources, plus one recorded reading so the app has data to show.

    The reading here is a PLACEHOLDER, marked as such in its own evidence
    text. A real one comes from `scripts/refresh_market.py` fetching a page
    the business has pointed it at — this exists only so the workspace is not
    empty before that happens.
    """
    from scripts.refresh_market import SEED_SOURCES

    if db.query(MarketSource).count() == 0:
        # Off until somebody supplies a URL: an active source with nowhere to
        # look just fails nightly and trains people to ignore the failures.
        db.add_all(MarketSource(**spec, active=False) for spec in SEED_SOURCES)
        db.flush()
        print(f"  added {len(SEED_SOURCES)} market sources (all off until given a URL)")

    for spec, form, series_key, sizes in LIVE_RANGES:
        source = db.query(MarketSource).filter_by(series_key=series_key).first()
        if source is None:
            source = MarketSource(
                series_key=series_key,
                name=f"{spec} {form.replace('_', ' ')} — example supplier",
                kind=MarketKind.MATERIAL_PRICE.value,
                unit=MarketUnit.GBP_PER_KG.value,
                basis=MarketBasis.RETAIL_ONLINE.value,
                spec=spec,
                stock_form=form,
                target="the price per kilogram, and every size listed, in mm",
                max_age_hours=168,
                active=False,
            )
            db.add(source)
            db.flush()

        if db.query(MarketObservation).filter_by(series_key=series_key).count() == 0:
            db.add(
                MarketObservation(
                    source_id=source.id,
                    series_key=series_key,
                    value=Decimal("2.40"),
                    unit=MarketUnit.GBP_PER_KG.value,
                    method=MarketMethod.MANUAL.value,
                    basis=source.basis,
                    confidence=0.95,
                    evidence=(
                        "PLACEHOLDER — seeded for development, not read from a "
                        "supplier. Replace by running scripts/refresh_market.py."
                    ),
                    observed_at=utcnow(),
                )
            )

        existing = {
            Decimal(row.width_mm)
            for row in db.query(StockSize).filter_by(spec=spec, stock_form=form)
            if row.width_mm is not None
        }
        for size in sizes:
            if Decimal(size) in existing:
                continue
            db.add(
                StockSize(
                    spec=spec,
                    stock_form=form,
                    length_mm=Decimal("3000"),
                    width_mm=Decimal(size),
                    thickness_mm=(Decimal(size) if form == StockForm.BAR_SQUARE.value else None),
                    # Computed from the live price and the bar's own weight;
                    # this is only the fallback if that ever goes missing.
                    unit_cost=Decimal("0"),
                    kerf_mm=Decimal("3"),
                    density_kg_m3=Decimal(DENSITY[spec]),
                    market_series_key=series_key,
                    origin=MarketMethod.MANUAL.value,
                )
            )
    db.flush()


def seed_example(db) -> None:
    """One worked enquiry, already extracted and routed, ready to price."""
    if db.query(Enquiry).filter(Enquiry.outlook_message_id == "seed-example-0001").first():
        print("  example enquiry already present")
        return

    customer = db.query(Customer).filter(Customer.domain == "bracken-eng.example").first()
    if customer is None:
        customer = Customer(
            name="Bracken Engineering",
            domain="bracken-eng.example",
            default_margin_pct=Decimal("30"),
            default_lead_days=10,
            is_material_supplied_default=True,
            requires_cert=False,
            notes="Usually free-issues material. Chases hard on delivery dates.",
        )
        db.add(customer)
        db.flush()

    enquiry = Enquiry(
        customer_id=customer.id,
        outlook_message_id="seed-example-0001",
        subject="RFQ — bracket 4471 rev B, 4 off",
        body_text=(
            "Morning,\n\nPlease could you quote 4 off of drawing 4471 rev B, "
            "attached. We'll supply the material as usual.\n\n"
            "Thanks,\nJo"
        ),
        sender_email="buyer@bracken-eng.example",
        received_at=utcnow(),
        tagged_at=utcnow(),
        status="classified",
    )
    db.add(enquiry)
    db.flush()

    pdf = example_drawing_pdf()
    digest = content_hash(pdf)
    blob_uri = get_storage().put(
        attachment_key(enquiry.id, "4471.pdf", digest), pdf, "application/pdf"
    )
    attachment = Attachment(
        enquiry_id=enquiry.id,
        filename="4471.pdf",
        blob_uri=blob_uri,
        mime_type="application/pdf",
        kind=AttachmentKind.DRAWING.value,
        content_hash=digest,
        size_bytes=len(pdf),
        drawing_number="4471",
        revision="B",
        page_count=1,
    )
    db.add(attachment)
    db.flush()

    part = Part(
        enquiry_id=enquiry.id,
        attachment_id=attachment.id,
        drawing_number="4471",
        revision="B",
        description="Bracket",
        quantity=4,
        quantity_source="email",
        material="1.2312",
        finish_spec="Ra 1.6",
        tightest_tolerance="+0.000/-0.013",
        envelope_x=Decimal("120"),
        envelope_y=Decimal("80"),
        envelope_z=Decimal("25"),
        job_type=JobType.SERVICE_ONLY.value,
        features={"holes": 6, "tapped_holes": 2, "internal_corners_below_1mm_radius": 2},
        # Note the deliberately mixed confidence: the tolerance is the field an
        # estimator should look at first.
        extraction_confidence={
            "drawing_number": 0.99,
            "revision": 0.97,
            "material": 0.96,
            "finish_spec": 0.92,
            "tightest_tolerance": 0.88,
            "envelope_x": 0.94,
        },
        process_mix=[Process.CNC_MILL.value, Process.WIRE_EDM.value],
    )
    db.add(part)
    db.flush()

    # A deliberate mix of time sources so the UI distinction is visible.
    db.add_all(
        [
            Operation(
                part_id=part.id,
                op_number=10,
                process=Process.CNC_MILL.value,
                description="Mill profile, face and pocket",
                set_time_mins=Decimal("45"),
                run_time_mins_per_unit=Decimal("22"),
                time_source=TimeSource.CALCULATOR.value,
            ),
            Operation(
                part_id=part.id,
                op_number=20,
                process=Process.WIRE_EDM.value,
                description="Wire the two sharp internal corners",
                set_time_mins=Decimal("30"),
                run_time_mins_per_unit=Decimal("18"),
                time_source=TimeSource.HISTORICAL_ESTIMATE.value,
            ),
            Operation(
                part_id=part.id,
                op_number=30,
                process=Process.QC.value,
                description="Final inspection, report on the critical bore",
                set_time_mins=Decimal("10"),
                run_time_mins_per_unit=Decimal("4"),
                time_source=TimeSource.MANUAL.value,
            ),
        ]
    )
    db.commit()
    print(f"  added example enquiry {enquiry.id} with part {part.id}")
    seed_full_supply_example(db)


def seed_full_supply_example(db) -> None:
    """A turned part we buy the material for, so the sizing story is visible.

    The first example is free-issue, which means it never exercises the part
    of the system most likely to be wrong: how much bar to buy. This one does.
    """
    if db.query(Enquiry).filter(Enquiry.outlook_message_id == "seed-example-0002").first():
        return

    customer = db.query(Customer).filter(Customer.domain == "halden-power.example").first()
    if customer is None:
        customer = Customer(
            name="Halden Power Systems",
            domain="halden-power.example",
            default_margin_pct=Decimal("35"),
            default_lead_days=15,
            is_material_supplied_default=False,
            requires_cert=True,
            notes="Full supply, always wants material certs with the delivery.",
        )
        db.add(customer)
        db.flush()

    enquiry = Enquiry(
        customer_id=customer.id,
        outlook_message_id="seed-example-0002",
        subject="RFQ — oil feed plate 67980 iss 1, 15 off",
        body_text=(
            "Hi,\n\nPlease quote 15 off drawing 67980 issue 1. Material and "
            "cert to be supplied by yourselves.\n\nRegards,\nPaul"
        ),
        sender_email="purchasing@halden-power.example",
        received_at=utcnow(),
        tagged_at=utcnow(),
        status="classified",
    )
    db.add(enquiry)
    db.flush()

    part = Part(
        enquiry_id=enquiry.id,
        drawing_number="67980",
        revision="1",
        description="Oil feed plate",
        quantity=15,
        quantity_source="email",
        material="EN16",
        tightest_tolerance="+0.000/-0.025",
        # Round about one axis: 85 across, 20 thick.
        envelope_x=Decimal("85"),
        envelope_y=Decimal("85"),
        envelope_z=Decimal("20"),
        is_rotational=True,
        job_type=JobType.FULL_SUPPLY.value,
        extraction_confidence={
            "drawing_number": 0.98,
            "material": 0.95,
            "quantity": 0.97,
            "tightest_tolerance": 0.91,
        },
        process_mix=[Process.CNC_TURN.value, Process.CNC_MILL.value],
    )
    db.add(part)
    db.flush()

    db.add_all(
        [
            Operation(
                part_id=part.id,
                op_number=10,
                process=Process.CNC_TURN.value,
                description="Turn OD, face both sides, bore centre",
                set_time_mins=Decimal("60"),
                run_time_mins_per_unit=Decimal("11"),
                time_source=TimeSource.CALCULATOR.value,
            ),
            Operation(
                part_id=part.id,
                op_number=20,
                process=Process.CNC_MILL.value,
                description="Mill feed slots and drill fixing holes",
                set_time_mins=Decimal("40"),
                run_time_mins_per_unit=Decimal("9"),
                time_source=TimeSource.HISTORICAL_ESTIMATE.value,
            ),
            Operation(
                part_id=part.id,
                op_number=30,
                process=Process.QC.value,
                description="Inspect, record bore size, issue cert",
                set_time_mins=Decimal("15"),
                run_time_mins_per_unit=Decimal("3"),
                time_source=TimeSource.MANUAL.value,
            ),
        ]
    )
    db.commit()
    print(f"  added full-supply example enquiry {enquiry.id} with part {part.id}")
    seed_worklists(db)


#: Enough enquiries, in enough states, that the working lists are not all
#: empty on a fresh install. Subjects only — these carry no parts and no
#: prices, because their whole job is to show that the tabs sort work by what
#: has to happen to it next.
WORKLIST_EXAMPLES = [
    ("received", "RFQ — 12 off spacer rings, drawing SP-220", None),
    ("classified", "Quote request — manifold block, 2 off", None),
    ("approved", "RFQ — shaft collar 8891, 40 off", None),
    (
        "approved",
        "RFQ — pump housing PH-14, 6 off",
        "Customer has not confirmed whether the bore is reamed or ground. "
        "Priced as ground; needs confirming before this goes out.",
    ),
    ("sent", "RFQ — guide rail set, 4 off", None),
    ("won", "RFQ — clamp plate 5512, 25 off", None),
    ("lost", "RFQ — bearing carrier BC-9, 3 off", None),
    (
        "needs_attention",
        "Fwd: drawing for quote (no drawing attached)",
        "The email says a drawing is attached and none was. Nothing can be "
        "read until somebody asks for it.",
    ),
]


def seed_worklists(db) -> None:
    """Spread some enquiries across the working lists.

    Without this every tab but one is empty on a fresh install, which makes
    the screen look broken rather than quiet.
    """
    if db.query(Enquiry).filter(Enquiry.outlook_message_id.like("seed-worklist-%")).first():
        return

    customer = db.query(Customer).filter(Customer.domain == "bracken-eng.example").first()
    added = 0
    for index, (status, subject, blocker) in enumerate(WORKLIST_EXAMPLES, start=1):
        enquiry = Enquiry(
            customer_id=customer.id if customer else None,
            outlook_message_id=f"seed-worklist-{index:04d}",
            subject=subject,
            body_text="Seeded example — no drawing, no price, no real customer.",
            sender_email="buyer@bracken-eng.example",
            received_at=utcnow() - timedelta(hours=index * 7),
            tagged_at=utcnow() - timedelta(hours=index * 7),
            status=status,
        )
        db.add(enquiry)
        db.flush()
        if blocker:
            db.add(
                Flag(
                    enquiry_id=enquiry.id,
                    category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
                    severity=FlagSeverity.BLOCK.value,
                    message=blocker,
                )
            )
        added += 1

    db.commit()
    print(f"  added {added} enquiries across the working lists")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", action="store_true", help="also add a worked example enquiry")
    args = parser.parse_args()

    settings = get_settings()
    if settings.environment not in ("development", "test"):
        print(
            f"Refusing to seed: AQM_ENVIRONMENT is '{settings.environment}'.\n"
            "Every figure in this script is an invented placeholder. Real rates "
            "must come from the business.",
            file=sys.stderr,
        )
        return 1

    init_db()
    db = SessionLocal()
    try:
        print("Seeding reference data (illustrative figures only):")
        seed_reference_data(db)
        if args.example:
            print("Seeding worked example:")
            seed_example(db)
    finally:
        db.close()

    print("\nDone. Remember: these rates are placeholders, not the business's rates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
