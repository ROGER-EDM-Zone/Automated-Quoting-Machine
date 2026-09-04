"""Tests written against the shapes of real EDM Zone mail.

Every fixture here is modelled on an actual message in the sales inbox — a
colleague forwarding a customer's RFQ, a customer on a consumer ISP address,
EDM Zone's own Op##### quote numbers. The bugs these cover were invisible
against invented test data and obvious within ten minutes of real mail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models import Customer, Enquiry
from app.services.graph import GraphAttachment, GraphMessage
from app.services.history import parse_customer_reference
from app.services.intake import (
    GENERIC_EMAIL_DOMAINS,
    ingest_message,
    parse_forwarded_origin,
    resolve_customer,
)
from app.services.storage import LocalStorage

INTERNAL = {"edmzone.co.uk"}

# Modelled on "Fw: Spline Drive Hub Housing keyway wiring", 12 Aug.
ROGER_FORWARD = """Hi Kelly, Wire RFQ

Cheers,

Roger

Sent from Outlook for Android
________________________________
From: Kirsty Bruce <KBruce@act-group.co.uk>
Sent: Wednesday, 12 August 2026 16:01:00
To: Roger Wilson - EDM Zone <roger.wilson@edmzone.co.uk>
Subject: Spline Drive Hub Housing keyway wiring

Please could you quote for wiring the keyway.
"""

# Modelled on "FW: RFQ" from Bloochip — a customer on a consumer ISP domain.
BLOOCHIP_FORWARD = """RFQ to process.

From: bloochip@btinternet.com <bloochip@btinternet.com>
Sent: 03 September 2026 11:28
To: Cameron Fletcher <cameron.fletcher@edmzone.co.uk>
Subject: RFQ

Could you give me a quotation for this job?
Drg. 92454 qty 5 off
"""


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "blobs")


@pytest.fixture(autouse=True)
def edmzone_settings(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AQM_GRAPH_QUOTING_MAILBOX", "sales@edmzone.co.uk")
    yield
    get_settings.cache_clear()


def forwarded_message(body: str, sender: str = "roger.wilson@edmzone.co.uk") -> GraphMessage:
    return GraphMessage(
        message_id=f"msg-{hash(body) & 0xFFFF}",
        subject="FW: RFQ",
        body_text=body,
        sender_email=sender,
        sender_name="Roger Wilson",
        received_at=datetime.now(UTC),
        categories=["RFQ"],
        attachments=[
            GraphAttachment(
                filename="92454.pdf", content_type="application/pdf", content_bytes=b"%PDF-1.4"
            )
        ],
    )


# --------------------------------------------------------------------------
# Reading the customer out of a forwarded chain
# --------------------------------------------------------------------------
def test_the_original_sender_is_found_in_a_forward():
    origin = parse_forwarded_origin(ROGER_FORWARD, internal_domains=INTERNAL)
    assert origin is not None
    assert origin.email == "KBruce@act-group.co.uk"
    assert origin.name == "Kirsty Bruce"


def test_a_forward_of_a_forward_keeps_looking_for_the_outside_party():
    body = """Passing on.

From: Cameron Fletcher <cameron.fletcher@edmzone.co.uk>
Sent: Monday
Subject: FW: RFQ

From: buyer@nexusprecision.com
Sent: Monday
"""
    origin = parse_forwarded_origin(body, internal_domains=INTERNAL)
    assert origin is not None
    assert origin.email == "buyer@nexusprecision.com"


def test_a_direct_email_has_no_forwarded_origin():
    assert (
        parse_forwarded_origin("Please quote 4 off drawing 4471.", internal_domains=INTERNAL)
        is None
    )


def test_an_address_only_from_line_yields_no_bogus_name():
    origin = parse_forwarded_origin(BLOOCHIP_FORWARD, internal_domains=INTERNAL)
    assert origin is not None
    assert origin.email == "bloochip@btinternet.com"
    assert origin.name is None, "the address repeated as a name is not a name"


# --------------------------------------------------------------------------
# Ingest: the customer must be the customer, not us
# --------------------------------------------------------------------------
def test_a_forwarded_rfq_is_attributed_to_the_customer_not_to_us(db, storage):
    act = Customer(name="ACT Group", domain="act-group.co.uk", default_margin_pct=Decimal("30"))
    edm = Customer(name="EDM Zone", domain="edmzone.co.uk", default_margin_pct=Decimal("0"))
    db.add_all([act, edm])
    db.commit()

    result = ingest_message(db, forwarded_message(ROGER_FORWARD), storage=storage)
    db.commit()

    enquiry = result.enquiry
    assert enquiry.customer_id == act.id, "must be the customer, never ourselves"
    assert enquiry.sender_email == "KBruce@act-group.co.uk"
    assert enquiry.forwarded_by == "roger.wilson@edmzone.co.uk"


def test_the_forwarders_instruction_is_kept_separately(db, storage):
    result = ingest_message(db, forwarded_message(ROGER_FORWARD), storage=storage)
    db.commit()
    note = result.enquiry.internal_note
    assert note is not None
    assert "Wire RFQ" in note
    # It must not swallow the customer's own words from further down the chain.
    assert "quote for wiring the keyway" not in note


def test_a_direct_customer_email_is_untouched(db, storage):
    nexus = Customer(name="Nexus", domain="nexusprecision.com", default_margin_pct=Decimal("30"))
    db.add(nexus)
    db.commit()

    message = forwarded_message(
        "Please quote best price for wiring.", sender="michael.carlin@nexusprecision.com"
    )
    result = ingest_message(db, message, storage=storage)
    db.commit()

    assert result.enquiry.customer_id == nexus.id
    assert result.enquiry.forwarded_by is None
    assert result.enquiry.internal_note is None


# --------------------------------------------------------------------------
# Consumer domains must not become customers
# --------------------------------------------------------------------------
def test_a_consumer_isp_domain_never_matches_a_customer(db):
    """btinternet.com identifies BT, not Bloochip — and every other BT customer."""
    db.add(Customer(name="Bloochip", domain="btinternet.com", default_margin_pct=Decimal("30")))
    db.commit()
    assert resolve_customer(db, "bloochip@btinternet.com") is None


def test_a_company_domain_still_matches(db):
    act = Customer(name="ACT Group", domain="act-group.co.uk", default_margin_pct=Decimal("30"))
    db.add(act)
    db.commit()
    assert resolve_customer(db, "KBruce@act-group.co.uk") is act


def test_a_forwarded_rfq_from_a_consumer_domain_is_left_unmatched(db, storage):
    result = ingest_message(db, forwarded_message(BLOOCHIP_FORWARD), storage=storage)
    db.commit()
    enquiry = db.get(Enquiry, result.enquiry.id)
    assert enquiry.sender_email == "bloochip@btinternet.com"
    assert enquiry.forwarded_by == "roger.wilson@edmzone.co.uk"
    # No customer, rather than the wrong customer. A human attaches it.
    assert enquiry.customer_id is None


def test_the_generic_list_covers_the_common_uk_providers():
    for domain in ("btinternet.com", "btconnect.com", "gmail.com", "hotmail.co.uk", "sky.com"):
        assert domain in GENERIC_EMAIL_DOMAINS


# --------------------------------------------------------------------------
# EDM Zone's own quote number formats
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Apsley Purchase Order - Quote ref: Op04034 - PO11111", "Op04034"),
        ("Pricing as per quote: Op07100", "Op07100"),
        ("RE: RFQ COM02258", "COM02258"),
        ("Q14658 - Wire", "Q14658"),
        ("Following on from previous quote 6123", "6123"),
        ("Please see attached our new PO", None),
        ("PO158672", None),
    ],
)
def test_real_quote_reference_formats_are_recognised(text, expected):
    assert parse_customer_reference(text) == expected


def test_a_purchase_order_number_is_not_mistaken_for_a_quote(db):
    """POs vastly outnumber quotes in this inbox; a false anchor is worse than none."""
    assert parse_customer_reference("Purchase Order from AIR - PO158672") is None
    assert parse_customer_reference("PURCHASE ORDER : 034607/1") is None
