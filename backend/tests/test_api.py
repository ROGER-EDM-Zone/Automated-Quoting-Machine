"""End-to-end API tests.

Walks a real enquiry from an inbound email through to a recorded outcome, with
a stubbed AI so the deterministic half is exercised for real. Every figure is
an invented fixture (spec section 9).
"""

from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db
from app.deps import get_ai, get_storage_dep
from app.enums import AttachmentKind, EnquiryStatus, JobType, Process, QuoteStatus, TimeSource
from app.main import app
from app.models import Attachment, CorrectionLog, Customer, Enquiry, Flag, utcnow
from app.services.ai import StubAIClient
from app.services.storage import LocalStorage, attachment_key, content_hash


def _one_page_pdf() -> bytes:
    """A real one-page PDF, so rasterising is genuinely exercised."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "DRG 4471 REV B  BRACKET  MATL 1.2312")
    data = doc.tobytes()
    doc.close()
    return data


def _extraction_payload(**overrides) -> dict:
    def field(value, confidence, evidence="title block"):
        return {"value": value, "confidence": confidence, "evidence": evidence}

    payload = {
        "drawing_number": field("4471", 0.98),
        "revision": field("B", 0.97),
        "description": field("Bracket", 0.95),
        "quantity": field(None, None, "not stated on the drawing"),
        "material": field("1.2312", 0.96),
        "heat_treatment": field(None, None, "none called for"),
        "surface_coat": field(None, None, "none called for"),
        "finish_spec": field("Ra 1.6", 0.91),
        "envelope_x": field(120.0, 0.94),
        "envelope_y": field(80.0, 0.94),
        "envelope_z": field(25.0, 0.93),
        "tightest_tolerance": field("+0.000/-0.013", 0.93),
        "units": "mm",
        "features": {
            "holes": 6,
            "tapped_holes": 2,
            "counterbores": 0,
            "pockets": 1,
            "slots": 0,
            "internal_corners_below_1mm_radius": 2,
            "through_wire_starts": 1,
            "notes": "Two internal corners below 1mm radius will need wire.",
        },
        "conflicts": [],
        "illegible": [],
    }
    payload.update(overrides)
    return payload


def _classification_payload(**overrides) -> dict:
    payload = {
        "job_type": JobType.SERVICE_ONLY.value,
        "job_type_confidence": 0.9,
        "job_type_reasoning": "The email says material is supplied by the customer.",
        "customer_named_processes": [],
        "process_mix": [Process.CNC_MILL.value, Process.WIRE_EDM.value],
        "proposed_operations": [
            {
                "op_number": 10,
                "process": Process.CNC_MILL.value,
                "description": "Mill profile and pocket",
                "set_time_mins": None,
                "run_time_mins_per_unit": None,
                "source_reference": None,
            },
            {
                "op_number": 20,
                "process": Process.WIRE_EDM.value,
                "description": "Wire the sharp internal corners",
                "set_time_mins": None,
                "run_time_mins_per_unit": None,
                "source_reference": None,
            },
            {
                "op_number": 30,
                "process": Process.QC.value,
                "description": "Final inspection",
                "set_time_mins": None,
                "run_time_mins_per_unit": None,
                "source_reference": None,
            },
        ],
        "email_facts": {
            "quantity": 4,
            "required_date": None,
            "mentions_material_supply": True,
            "customer_reference": None,
            "requests_certification": None,
            "urgency_wording": None,
        },
        "concerns": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A wired-up client: fresh database, local storage, stubbed AI."""
    get_settings.cache_clear()
    monkeypatch.setenv("AQM_STORAGE_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("AQM_AUTH_REQUIRED", "false")
    settings = get_settings()

    # StaticPool: the TestClient serves requests on another thread, and a
    # second connection to :memory: would be a second, empty database.
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    storage = LocalStorage(settings.storage_root)
    stub = StubAIClient()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_ai] = lambda: stub
    app.dependency_overrides[get_storage_dep] = lambda: storage

    client = TestClient(app)
    client.headers["X-User-Email"] = "estimator@shop.example"
    try:
        yield client, session, stub, storage
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def seeded(api):
    """Customer, rates, rules and one enquiry with a real PDF drawing."""
    client, session, stub, storage = api
    since = (date.today() - timedelta(days=200)).isoformat()

    customer = client.post(
        "/admin/customers",
        json={
            "name": "Bracken Engineering",
            "domain": "bracken-eng.example",
            "default_margin_pct": "30",
            "default_lead_days": 10,
            "is_material_supplied_default": True,
            "requires_cert": False,
        },
    ).json()

    for process, rate in (
        (Process.CNC_MILL.value, "55.00"),
        (Process.WIRE_EDM.value, "42.00"),
        (Process.QC.value, "30.00"),
        (Process.SPARK_ERODE.value, "38.00"),
    ):
        assert client.post(
            "/admin/rates",
            json={"process": process, "hourly_rate": rate, "effective_from": since},
        ).status_code == 201

    assert client.post(
        "/admin/rules",
        json={
            "rule_key": "min_quote_value",
            "trigger_description": "Minimum order value",
            "adjustment_type": "fixed",
            "adjustment_value": "150.00",
        },
    ).status_code == 201
    rush = client.post(
        "/admin/rules",
        json={
            "rule_key": "rush_uplift",
            "trigger_description": "Delivery inside 5 working days",
            "adjustment_type": "pct",
            "adjustment_value": "15.00",
        },
    ).json()

    enquiry = Enquiry(
        customer_id=customer["id"],
        outlook_message_id="AAMk-api-0001",
        subject="RFQ 4471 rev B",
        body_text="Please quote 4 off drawing 4471 rev B. We will supply the material.",
        sender_email="buyer@bracken-eng.example",
        received_at=utcnow(),
        tagged_at=utcnow(),
        status=EnquiryStatus.RECEIVED.value,
    )
    session.add(enquiry)
    session.flush()

    pdf = _one_page_pdf()
    digest = content_hash(pdf)
    uri = storage.put(attachment_key(enquiry.id, "4471.pdf", digest), pdf, "application/pdf")
    session.add(
        Attachment(
            enquiry_id=enquiry.id,
            filename="4471.pdf",
            blob_uri=uri,
            mime_type="application/pdf",
            kind=AttachmentKind.DRAWING.value,
            content_hash=digest,
            size_bytes=len(pdf),
        )
    )
    session.commit()
    return client, session, stub, enquiry, customer, rush


# --------------------------------------------------------------------------
# Health and admin
# --------------------------------------------------------------------------
def test_health_reports_configuration_honestly(api):
    client, *_ = api
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["auth_required"] is False
    assert body["ai_configured"] is False


def test_a_new_rate_end_dates_the_one_it_replaces(api):
    client, session, *_ = api
    old = (date.today() - timedelta(days=100)).isoformat()
    new = date.today().isoformat()
    first = client.post(
        "/admin/rates",
        json={"process": Process.GRIND.value, "hourly_rate": "40.00", "effective_from": old},
    ).json()
    client.post(
        "/admin/rates",
        json={"process": Process.GRIND.value, "hourly_rate": "44.00", "effective_from": new},
    )
    rates = {r["id"]: r for r in client.get("/admin/rates?process=grind").json()}
    assert rates[first["id"]]["effective_to"] == new
    current = client.get("/admin/rates?process=grind&current_only=true").json()
    assert len(current) == 1 and current[0]["hourly_rate"] == "44.00"


def test_rates_are_never_deleted_only_ended(api):
    client, *_ = api
    assert "DELETE" not in {
        method
        for path, methods in client.get("/openapi.json").json()["paths"].items()
        if path.startswith("/admin/rates")
        for method in map(str.upper, methods)
    }


# --------------------------------------------------------------------------
# The pipeline, end to end
# --------------------------------------------------------------------------
def test_full_pipeline_from_extraction_to_recorded_outcome(seeded):
    client, session, stub, enquiry, customer, rush = seeded

    # --- extract -------------------------------------------------------
    stub.responses.append(_extraction_payload())
    extracted = client.post(f"/enquiries/{enquiry.id}/extract")
    assert extracted.status_code == 200, extracted.text
    body = extracted.json()
    assert body["status"] == EnquiryStatus.EXTRACTED.value
    assert len(body["parts"]) == 1
    part = body["parts"][0]
    assert part["drawing_number"] == "4471"
    assert part["material"] == "1.2312"
    # The drawing did not state a quantity, and nothing invented one.
    assert stub.calls[0]["images"], "the drawing must actually be sent as an image"

    # --- classify ------------------------------------------------------
    stub.responses.append(_classification_payload())
    classified = client.post(f"/enquiries/{enquiry.id}/classify")
    assert classified.status_code == 200, classified.text
    part = classified.json()["parts"][0]
    assert part["job_type"] == JobType.SERVICE_ONLY.value
    # Quantity came from the email, which is the order.
    assert part["quantity"] == 4
    assert [op["op_number"] for op in part["operations"]] == [10, 20, 30]
    # No times yet, so every operation is manual and costs nothing.
    assert all(op["time_source"] == TimeSource.MANUAL.value for op in part["operations"])

    # --- pricing is refused while the times are missing? No: it prices at
    # --- zero, but a blocking flag says the quote is not complete.
    priced = client.post(f"/enquiries/{enquiry.id}/price", json={})
    assert priced.status_code == 200, priced.text
    assert priced.json()["quote_value"] == "150.00"  # min value floor
    assert priced.json()["min_value_applied"] is True

    # --- supply real times --------------------------------------------
    part_id = part["id"]
    ops = client.put(
        f"/parts/{part_id}/operations",
        json=[
            {
                "op_number": 10,
                "process": Process.CNC_MILL.value,
                "description": "Mill profile and pocket",
                "set_time_mins": "45",
                "run_time_mins_per_unit": "22",
                "time_source": TimeSource.CALCULATOR.value,
            },
            {
                "op_number": 20,
                "process": Process.WIRE_EDM.value,
                "description": "Wire the sharp internal corners",
                "set_time_mins": "30",
                "run_time_mins_per_unit": "18",
                "time_source": TimeSource.HISTORICAL_ESTIMATE.value,
            },
            {
                "op_number": 30,
                "process": Process.QC.value,
                "description": "Final inspection",
                "set_time_mins": "10",
                "run_time_mins_per_unit": "4",
                "time_source": TimeSource.MANUAL.value,
            },
        ],
    )
    assert ops.status_code == 200, ops.text

    quote = client.post(f"/enquiries/{enquiry.id}/price", json={}).json()
    # mill 45+22*4=133m @55 = 121.92 ; wire 30+18*4=102m @42 = 71.40
    # qc 10+4*4=26m @30 = 13.00 ; labour 206.32, margin 30% = 61.90
    assert quote["labour_total"] == "206.32"
    assert quote["material_total"] == "0.00"  # service only
    assert quote["margin_value"] == "61.90"
    # 206.32 + 61.90 = 268.22, but the unit price rounds to 67.06 and the line
    # total is unit_price x qty so the customer-facing arithmetic adds up.
    # The 2p residue is reported, not hidden — see the breakdown assertions.
    assert quote["quote_value"] == "268.24"
    assert quote["lines"][0]["unit_price"] == "67.06"
    assert quote["lines"][0]["line_total"] == "268.24"
    assert quote["min_value_applied"] is False

    # --- workspace -----------------------------------------------------
    workspace = client.get(f"/enquiries/{enquiry.id}").json()
    assert workspace["breakdown"]["reconciles"] is True
    assert workspace["breakdown"]["quote_value"] == quote["quote_value"]
    assert workspace["breakdown"]["rounding_adjustment"] == "0.02"
    assert workspace["breakdown"]["uses_untrusted_times"] is True
    sources = {
        oc["op_number"]: oc["time_source"]
        for oc in workspace["breakdown"]["parts"][0]["operation_costs"]
    }
    assert sources == {
        10: TimeSource.CALCULATOR.value,
        20: TimeSource.HISTORICAL_ESTIMATE.value,
        30: TimeSource.MANUAL.value,
    }

    # --- approval is blocked while flags are unresolved -----------------
    quote_id = quote["id"]
    blockers = client.get(f"/quotes/{quote_id}/blockers").json()
    assert blockers, "unread fields should be blocking approval"
    refused = client.post(f"/quotes/{quote_id}/approve", json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["blocking_flags"]

    for flag in blockers:
        assert (
            client.post(
                f"/flags/{flag['id']}/resolve", json={"note": "checked against the drawing"}
            ).status_code
            == 200
        )

    approved = client.post(f"/quotes/{quote_id}/approve", json={"lead_time_days": 10})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == QuoteStatus.APPROVED.value
    assert approved.json()["approved_by"] == "estimator@shop.example"
    assert approved.json()["approved_at"] is not None

    # --- draft reply ---------------------------------------------------
    draft = client.post(f"/quotes/{quote_id}/draft-reply")
    assert draft.status_code == 200, draft.text
    reply = draft.json()
    assert reply["draft_created"] is False  # Graph not configured in tests
    # The customer-facing numbers came from the record.
    assert "268.24" in reply["body_text"]
    assert "67.06" in reply["body_text"]
    assert "machining only" in reply["body_text"]
    assert reply["to"] == ["buyer@bracken-eng.example"]

    # --- send and outcome ----------------------------------------------
    sent = client.post(f"/quotes/{quote_id}/mark-sent").json()
    assert sent["status"] == QuoteStatus.SENT.value
    assert sent["sent_at"] is not None
    session.expire_all()
    assert session.get(Enquiry, enquiry.id).turnaround_seconds is not None

    outcome = client.post(
        f"/quotes/{quote_id}/outcome",
        json={"result": "won", "actual_production_mins": "240", "notes": "Ran long on the wire."},
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["outcome"]["result"] == "won"
    assert session.get(Enquiry, enquiry.id).status == EnquiryStatus.WON.value


def test_a_sent_quote_is_frozen_and_revising_starts_a_new_version(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.extend([_extraction_payload(), _classification_payload()])
    client.post(f"/enquiries/{enquiry.id}/extract")
    client.post(f"/enquiries/{enquiry.id}/classify")
    quote = client.post(f"/enquiries/{enquiry.id}/price", json={}).json()
    quote_id = quote["id"]

    for flag in client.get(f"/quotes/{quote_id}/blockers").json():
        client.post(f"/flags/{flag['id']}/resolve", json={})
    client.post(f"/quotes/{quote_id}/approve", json={})
    client.post(f"/quotes/{quote_id}/mark-sent")

    session.expire_all()
    sent = session.get(type(session.get(Enquiry, enquiry.id).quotes[0]), quote_id)
    assert sent.frozen_snapshot is not None
    assert sent.frozen_snapshot["quote_value"] == quote["quote_value"]

    revision = client.post(f"/quotes/{quote_id}/revise").json()
    assert revision["version"] == 2
    assert revision["status"] == QuoteStatus.DRAFT.value


def test_a_draft_reply_is_refused_before_approval(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.extend([_extraction_payload(), _classification_payload()])
    client.post(f"/enquiries/{enquiry.id}/extract")
    client.post(f"/enquiries/{enquiry.id}/classify")
    quote = client.post(f"/enquiries/{enquiry.id}/price", json={}).json()
    refused = client.post(f"/quotes/{quote['id']}/draft-reply")
    assert refused.status_code == 409
    assert "approved" in refused.json()["detail"]


# --------------------------------------------------------------------------
# Confidence, corrections and flags
# --------------------------------------------------------------------------
def test_a_low_confidence_field_is_withheld_not_priced(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(
        _extraction_payload(
            material={"value": "1.2344?", "confidence": 0.42, "evidence": "smudged"}
        )
    )
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]
    assert part["material"] is None, "a low-confidence value must not reach the record"
    assert part["withheld_fields"]["material"] == "1.2344?"
    assert part["extraction_confidence"]["material"] == 0.42

    flags = [f for f in part["flags"] if f["field_name"] == "material"]
    assert flags and flags[0]["severity"] == "block"
    assert "below the" in flags[0]["message"]


def test_a_null_field_is_reported_as_unread_rather_than_guessed(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(
        _extraction_payload(
            tightest_tolerance={"value": None, "confidence": None, "evidence": "cropped view"}
        )
    )
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]
    assert part["tightest_tolerance"] is None
    assert "tightest_tolerance" not in (part["withheld_fields"] or {})
    messages = [f["message"] for f in part["flags"] if f["field_name"] == "tightest_tolerance"]
    assert any("could not be read" in m for m in messages)


def test_a_reported_conflict_becomes_a_flag_and_is_not_resolved(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(
        _extraction_payload(
            conflicts=[
                {"field": "quantity", "detail": "title block says 4, note says 6"}
            ]
        )
    )
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]
    conflict = next(f for f in part["flags"] if "Conflicting" in f["message"])
    assert conflict["severity"] == "block"
    assert "4" in conflict["message"] and "6" in conflict["message"]


def test_an_override_writes_a_correction_log_row_and_clears_the_flag(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(
        _extraction_payload(
            material={"value": "1.2344?", "confidence": 0.42, "evidence": "smudged"}
        )
    )
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]

    patched = client.patch(f"/parts/{part['id']}", json={"material": "1.2312"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["material"] == "1.2312"
    assert (patched.json()["withheld_fields"] or {}) == {}

    corrections = client.get(f"/parts/{part['id']}/corrections").json()
    assert len(corrections) == 1
    row = corrections[0]
    assert row["field_name"] == "material"
    assert row["ai_value"] == "1.2344?"      # what the AI actually read
    assert row["human_value"] == "1.2312"
    assert row["ai_confidence"] == 0.42
    assert row["was_withheld"] is True
    assert row["corrected_by"] == "estimator@shop.example"

    remaining = [
        f
        for f in client.get(f"/enquiries/{enquiry.id}").json()["enquiry"]["parts"][0]["flags"]
        if f["field_name"] == "material" and not f["resolved"]
    ]
    assert not remaining


def test_an_unchanged_field_does_not_write_a_correction(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(_extraction_payload())
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]
    client.patch(f"/parts/{part['id']}", json={"material": "1.2312"})
    assert client.get(f"/parts/{part['id']}/corrections").json() == []


def test_a_confidently_wrong_correction_is_distinguishable_in_reporting(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.append(_extraction_payload())  # material at 0.96, accepted
    part = client.post(f"/enquiries/{enquiry.id}/extract").json()["parts"][0]
    client.patch(f"/parts/{part['id']}", json={"material": "1.2367"})

    report = client.get("/reports/extraction-accuracy").json()
    material = report["per_field"]["material"]
    assert material["corrections"] == 1
    assert material["confidently_wrong"] == 1
    assert material["corrected_after_withholding"] == 0


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------
def test_the_queue_surfaces_flags_value_and_confidence(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.extend([_extraction_payload(), _classification_payload()])
    client.post(f"/enquiries/{enquiry.id}/extract")
    client.post(f"/enquiries/{enquiry.id}/classify")
    client.post(f"/enquiries/{enquiry.id}/price", json={})

    queue = client.get("/queue").json()
    assert len(queue) == 1
    item = queue[0]
    assert item["enquiry_id"] == enquiry.id
    assert item["customer_name"] == "Bracken Engineering"
    assert item["part_count"] == 1
    assert item["total_quantity"] == 4
    assert item["blocking_flag_count"] >= 1
    assert item["lowest_confidence"] is not None
    assert item["quote_value"] is not None


def test_the_queue_can_sort_by_confidence_lowest_first(api):
    client, session, *_ = api
    for index, confidence in enumerate([0.99, 0.55, 0.80]):
        enquiry = Enquiry(
            outlook_message_id=f"m-{index}",
            subject=f"RFQ {index}",
            received_at=utcnow(),
            status=EnquiryStatus.EXTRACTED.value,
        )
        session.add(enquiry)
        session.flush()
        from app.models import Part

        session.add(
            Part(
                enquiry_id=enquiry.id,
                quantity=1,
                extraction_confidence={"material": confidence},
            )
        )
    session.commit()
    order = [i["lowest_confidence"] for i in client.get("/queue?sort=confidence").json()]
    assert order == [0.55, 0.80, 0.99]


def test_sent_enquiries_drop_out_of_the_queue_by_default(seeded):
    client, session, stub, enquiry, *_ = seeded
    enquiry_row = session.get(Enquiry, enquiry.id)
    enquiry_row.status = EnquiryStatus.SENT.value
    session.commit()
    assert client.get("/queue").json() == []
    assert len(client.get("/queue?include_closed=true").json()) == 1


# --------------------------------------------------------------------------
# Rate changes
# --------------------------------------------------------------------------
def test_a_rate_change_reprices_without_a_deployment(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.extend([_extraction_payload(), _classification_payload()])
    client.post(f"/enquiries/{enquiry.id}/extract")
    client.post(f"/enquiries/{enquiry.id}/classify")
    part = client.get(f"/enquiries/{enquiry.id}").json()["enquiry"]["parts"][0]
    client.put(
        f"/parts/{part['id']}/operations",
        json=[
            {
                "op_number": 10,
                "process": Process.CNC_MILL.value,
                "set_time_mins": "60",
                "run_time_mins_per_unit": "0",
                "time_source": TimeSource.CALCULATOR.value,
            }
        ],
    )
    before = client.post(f"/enquiries/{enquiry.id}/price", json={}).json()
    assert before["labour_total"] == "55.00"

    client.post(
        "/admin/rates",
        json={
            "process": Process.CNC_MILL.value,
            "hourly_rate": "66.00",
            "effective_from": date.today().isoformat(),
        },
    )
    after = client.post(f"/enquiries/{enquiry.id}/price", json={}).json()
    assert after["labour_total"] == "66.00"


def test_pricing_is_refused_when_a_rate_is_missing(seeded):
    client, session, stub, enquiry, *_ = seeded
    stub.responses.extend(
        [
            _extraction_payload(),
            _classification_payload(
                proposed_operations=[
                    {
                        "op_number": 10,
                        "process": Process.CNC_TURN.value,  # no rate seeded
                        "description": "Turn spigot",
                        "set_time_mins": None,
                        "run_time_mins_per_unit": None,
                        "source_reference": None,
                    }
                ]
            ),
        ]
    )
    client.post(f"/enquiries/{enquiry.id}/extract")
    client.post(f"/enquiries/{enquiry.id}/classify")
    refused = client.post(f"/enquiries/{enquiry.id}/price", json={})
    assert refused.status_code == 409
    assert "cnc_turn" in str(refused.json()["detail"])
    assert session.query(Flag).filter(Flag.dedupe_key == "missing_rate:cnc_turn").count() == 1
