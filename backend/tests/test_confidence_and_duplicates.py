"""Confidence policy, duplicate detection and reply rendering."""

from decimal import Decimal as D

import pytest

from app.config import Settings
from app.enums import AttachmentKind, FlagCategory, FlagSeverity, QuoteStatus
from app.models import Attachment, Enquiry, Flag, Quote, QuoteLine, utcnow
from app.services.classification import _is_later_revision, duplicate_check
from app.services.confidence import (
    FieldReading,
    apply_policy,
    cross_check,
    readings_from_payload,
)
from app.services.reply import ReplyError, build_reply


@pytest.fixture
def settings() -> Settings:
    return Settings(
        confidence_threshold_default=0.80,
        confidence_threshold_overrides={"material": 0.85, "quantity": 0.90},
    )


# --------------------------------------------------------------------------
# Confidence policy
# --------------------------------------------------------------------------
def test_a_confident_reading_is_accepted(settings):
    outcome = apply_policy([FieldReading("material", "1.2312", 0.96)], settings)
    assert outcome.accepted == {"material": "1.2312"}
    assert outcome.withheld == {}
    assert outcome.flags == []


def test_a_reading_below_its_per_field_threshold_is_withheld(settings):
    # 0.82 clears the 0.80 default but not the 0.85 override for material.
    outcome = apply_policy([FieldReading("material", "1.2312", 0.82)], settings)
    assert outcome.accepted == {}
    assert outcome.withheld == {"material": "1.2312"}
    assert outcome.confidences == {"material": 0.82}
    assert outcome.flags[0].severity == FlagSeverity.BLOCK.value


def test_a_null_reading_is_unread_not_withheld(settings):
    outcome = apply_policy([FieldReading("finish_spec", None, None)], settings)
    assert outcome.unread == ["finish_spec"]
    assert outcome.withheld == {}
    assert "could not be read" in outcome.flags[0].message


def test_a_value_with_no_confidence_is_not_trusted(settings):
    """An unscored value is not a trusted one."""
    outcome = apply_policy([FieldReading("material", "1.2312", None)], settings)
    assert outcome.accepted == {}
    assert outcome.withheld == {"material": "1.2312"}


def test_a_bare_scalar_payload_is_treated_as_unscored(settings):
    readings = readings_from_payload({"material": "1.2312"})
    outcome = apply_policy(readings, settings)
    assert outcome.withheld == {"material": "1.2312"}


def test_pricing_critical_fields_block_and_others_only_warn(settings):
    outcome = apply_policy(
        [FieldReading("quantity", None, None), FieldReading("surface_coat", None, None)],
        settings,
    )
    severities = {f.field_name: f.severity for f in outcome.flags}
    assert severities["quantity"] == FlagSeverity.BLOCK.value
    assert severities["surface_coat"] == FlagSeverity.WARN.value


def test_a_part_missing_its_quantity_is_not_priceable(settings):
    assert apply_policy([FieldReading("quantity", None, None)], settings).is_priceable() is False
    assert apply_policy([FieldReading("quantity", 4, 0.95)], settings).is_priceable() is True


def test_cross_check_flags_a_disagreement_rather_than_picking(settings):
    flag = cross_check("quantity", 4, 6)
    assert flag is not None
    assert flag.severity == FlagSeverity.BLOCK.value
    assert "4" in flag.message and "6" in flag.message
    assert "must choose" in flag.message


def test_cross_check_is_silent_when_the_sources_agree():
    assert cross_check("material", "1.2312", "1.2312") is None
    assert cross_check("material", " 1.2312 ", "1.2312") is None
    assert cross_check("quantity", None, 6) is None


# --------------------------------------------------------------------------
# Duplicate and version conflict
# --------------------------------------------------------------------------
def _enquiry_with_drawing(db, drawing: str, revision: str) -> Enquiry:
    enquiry = Enquiry(
        outlook_message_id=f"m-{drawing}-{revision}-{utcnow().timestamp()}",
        subject=f"RFQ {drawing} rev {revision}",
        received_at=utcnow(),
    )
    db.add(enquiry)
    db.flush()
    db.add(
        Attachment(
            enquiry_id=enquiry.id,
            filename=f"{drawing}.pdf",
            kind=AttachmentKind.DRAWING.value,
            drawing_number=drawing,
            revision=revision,
        )
    )
    db.commit()
    return enquiry


def test_the_same_drawing_at_the_same_revision_raises_a_duplicate(db):
    _enquiry_with_drawing(db, "4471", "B")
    second = _enquiry_with_drawing(db, "4471", "B")
    duplicate_check(db, second)
    db.commit()
    flag = db.query(Flag).filter(Flag.enquiry_id == second.id).one()
    assert flag.category == FlagCategory.DUPLICATE_RFQ.value
    assert flag.severity == FlagSeverity.WARN.value


def test_a_higher_revision_raises_a_blocking_version_conflict(db):
    first = _enquiry_with_drawing(db, "4471", "B")
    second = _enquiry_with_drawing(db, "4471", "C")
    duplicate_check(db, second)
    db.commit()
    flag = db.query(Flag).filter(Flag.enquiry_id == second.id).one()
    assert flag.category == FlagCategory.VERSION_CONFLICT.value
    assert flag.severity == FlagSeverity.BLOCK.value
    assert flag.related_enquiry_id == first.id


def test_a_different_drawing_raises_nothing(db):
    _enquiry_with_drawing(db, "4471", "B")
    other = _enquiry_with_drawing(db, "9000", "A")
    duplicate_check(db, other)
    db.commit()
    assert db.query(Flag).filter(Flag.enquiry_id == other.id).count() == 0


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [
        ("C", "B", True),
        ("B", "C", False),
        ("2", "1", True),
        ("1", "2", False),
        ("B", "B", False),
        ("A", "1", False),   # different schemes: refuse to compare
        ("AA", "B", False),  # different lengths: refuse to compare
        (None, "B", False),
    ],
)
def test_revision_comparison_refuses_to_guess_across_schemes(current, prior, expected):
    assert _is_later_revision(current, prior) is expected


# --------------------------------------------------------------------------
# Reply rendering
# --------------------------------------------------------------------------
def _approved_quote(db, enquiry, status=QuoteStatus.APPROVED.value) -> Quote:
    quote = Quote(
        enquiry_id=enquiry.id,
        version=1,
        status=status,
        quote_value=D("957.00"),
        subtotal=D("736.15"),
        margin_pct=D("30"),
        margin_value=D("220.85"),
        lead_time_days=10,
    )
    db.add(quote)
    db.flush()
    db.add(
        QuoteLine(
            quote_id=quote.id,
            quantity=4,
            unit_price=D("239.25"),
            line_total=D("957.00"),
            drawing_number="4471",
            revision="B",
            description="Bracket",
        )
    )
    db.commit()
    return quote


def test_the_reply_uses_the_stored_numbers(db, enquiry):
    quote = _approved_quote(db, enquiry)
    reply = build_reply(db, enquiry, quote)
    assert "957.00" in reply.body_text
    assert "239.25" in reply.body_text
    assert "4471" in reply.body_text
    assert "Q1-1" in reply.subject or "Q1-1" in reply.body_text
    assert reply.to == ["buyer@bracken-eng.example"]


def test_an_unapproved_quote_cannot_be_drafted(db, enquiry):
    quote = _approved_quote(db, enquiry, status=QuoteStatus.DRAFT.value)
    with pytest.raises(ReplyError, match="approved"):
        build_reply(db, enquiry, quote)


def test_a_quote_with_no_lines_cannot_be_drafted(db, enquiry):
    quote = Quote(enquiry_id=enquiry.id, version=1, status=QuoteStatus.APPROVED.value)
    db.add(quote)
    db.commit()
    with pytest.raises(ReplyError, match="no lines"):
        build_reply(db, enquiry, quote)


def test_the_reply_module_contains_no_ai_call():
    """Customer-facing numbers are formatted, never generated."""
    import app.services.reply as reply_module

    source = open(reply_module.__file__, encoding="utf-8").read()
    for forbidden in ("anthropic", "ai.structured", "get_ai_client", "StructuredCaller"):
        assert forbidden not in source
