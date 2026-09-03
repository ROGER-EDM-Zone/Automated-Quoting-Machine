"""Shared fixtures.

Every rate, margin and stock size in here is an invented test figure (spec
section 9). None of it is real business data, and nothing in the application
carries a default that would let these numbers leak into production.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

# Point every component at a throwaway database before the app imports it.
os.environ.setdefault("AQM_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("AQM_STORAGE_BACKEND", "local")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base  # noqa: E402
from app.enums import (  # noqa: E402
    AdjustmentType,
    AttachmentKind,
    JobType,
    Process,
    RuleKey,
    StockForm,
    TimeSource,
)
from app.models import (  # noqa: E402
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


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def rates(db):
    """Illustrative rate rows, effective from a year ago."""
    since = date.today() - timedelta(days=365)
    rows = [
        RateTable(process=Process.CNC_MILL.value, hourly_rate=Decimal("55.00"), effective_from=since),
        RateTable(process=Process.CNC_TURN.value, hourly_rate=Decimal("52.00"), effective_from=since),
        RateTable(process=Process.WIRE_EDM.value, hourly_rate=Decimal("42.00"), effective_from=since),
        RateTable(process=Process.SPARK_ERODE.value, hourly_rate=Decimal("38.00"), effective_from=since),
        RateTable(process=Process.GRIND.value, hourly_rate=Decimal("40.00"), effective_from=since),
        RateTable(process=Process.MANUAL.value, hourly_rate=Decimal("35.00"), effective_from=since),
        RateTable(process=Process.QC.value, hourly_rate=Decimal("30.00"), effective_from=since),
    ]
    db.add_all(rows)
    db.commit()
    return {row.process: row for row in rows}


@pytest.fixture
def rules(db):
    rows = [
        RulesTable(
            rule_key=RuleKey.MIN_QUOTE_VALUE.value,
            trigger_description="Minimum order value",
            adjustment_type=AdjustmentType.FIXED.value,
            adjustment_value=Decimal("150.00"),
        ),
        RulesTable(
            rule_key=RuleKey.RUSH_UPLIFT.value,
            trigger_description="Customer needs delivery inside 5 working days",
            adjustment_type=AdjustmentType.PCT.value,
            adjustment_value=Decimal("15.00"),
        ),
        RulesTable(
            rule_key=RuleKey.DIFFICULT_JOB_CONTINGENCY.value,
            trigger_description="Tolerance tighter than 0.01mm or unfamiliar material",
            adjustment_type=AdjustmentType.PCT.value,
            adjustment_value=Decimal("10.00"),
        ),
    ]
    db.add_all(rows)
    db.commit()
    return {row.rule_key: row for row in rows}


@pytest.fixture
def stock(db):
    rows = [
        StockSize(
            spec="1.2312",
            stock_form=StockForm.PLATE.value,
            length_mm=Decimal("500"),
            width_mm=Decimal("250"),
            thickness_mm=Decimal("30"),
            unit_cost=Decimal("96.00"),
            kerf_mm=Decimal("4"),
        ),
        StockSize(
            spec="1.2312",
            stock_form=StockForm.PLATE.value,
            length_mm=Decimal("1000"),
            width_mm=Decimal("500"),
            thickness_mm=Decimal("30"),
            unit_cost=Decimal("340.00"),
            kerf_mm=Decimal("4"),
        ),
    ]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.fixture
def customer(db):
    row = Customer(
        name="Bracken Engineering",
        domain="bracken-eng.example",
        default_margin_pct=Decimal("30.00"),
        default_lead_days=10,
        is_material_supplied_default=True,
        requires_cert=False,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def enquiry(db, customer):
    row = Enquiry(
        customer_id=customer.id,
        outlook_message_id="AAMkAD-test-0001",
        subject="RFQ - bracket 4471 rev B, 4 off",
        body_text="Please quote 4 off drawing 4471 rev B. Material supplied by us. Needed week commencing Monday.",
        sender_email="buyer@bracken-eng.example",
        received_at=utcnow(),
        tagged_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def drawing_attachment(db, enquiry):
    row = Attachment(
        enquiry_id=enquiry.id,
        filename="4471.pdf",
        mime_type="application/pdf",
        kind=AttachmentKind.DRAWING.value,
        blob_uri="file:///dev/null/4471.pdf",
        content_hash="a" * 64,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def priceable_part(db, enquiry, drawing_attachment, rates):
    """A service-only part with three operations, ready to price."""
    part = Part(
        enquiry_id=enquiry.id,
        attachment_id=drawing_attachment.id,
        drawing_number="4471",
        revision="B",
        description="Bracket",
        quantity=4,
        material="1.2312",
        tightest_tolerance="+0.000/-0.013",
        job_type=JobType.SERVICE_ONLY.value,
        envelope_x=Decimal("120"),
        envelope_y=Decimal("80"),
        envelope_z=Decimal("25"),
        extraction_confidence={"material": 0.96, "quantity": 0.98, "tightest_tolerance": 0.94},
    )
    db.add(part)
    db.flush()
    db.add_all(
        [
            Operation(
                part_id=part.id,
                op_number=10,
                process=Process.CNC_MILL.value,
                description="Mill profile and pockets",
                set_time_mins=Decimal("45"),
                run_time_mins_per_unit=Decimal("22"),
                time_source=TimeSource.CALCULATOR.value,
            ),
            Operation(
                part_id=part.id,
                op_number=20,
                process=Process.WIRE_EDM.value,
                description="Wire form to profile",
                set_time_mins=Decimal("30"),
                run_time_mins_per_unit=Decimal("18"),
                time_source=TimeSource.HISTORICAL_ESTIMATE.value,
            ),
            Operation(
                part_id=part.id,
                op_number=30,
                process=Process.QC.value,
                description="Final inspection",
                set_time_mins=Decimal("10"),
                run_time_mins_per_unit=Decimal("4"),
                time_source=TimeSource.MANUAL.value,
            ),
        ]
    )
    db.commit()
    db.refresh(part)
    return part
