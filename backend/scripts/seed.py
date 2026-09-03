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
    JobType,
    Process,
    RuleKey,
    StockForm,
    TimeSource,
)
from app.models import (
    Attachment,
    Customer,
    Enquiry,
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

    db.commit()


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
