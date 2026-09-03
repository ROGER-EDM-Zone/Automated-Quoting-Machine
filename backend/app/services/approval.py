"""Approval and send (spec stage 6).

"Nothing sends itself. Approval is a human action, recorded with who and when."

Approval is gated on unresolved `block` flags, and the gate is here rather
than in the API layer so that no future caller can route around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.enums import EnquiryStatus, OutcomeResult, QuoteStatus
from app.models import Enquiry, Flag, Quote, QuoteOutcome, utcnow
from app.services.flags import blocking_flags


class ApprovalBlocked(Exception):
    """Approval refused because blocking flags are unresolved."""

    def __init__(self, flags: list[Flag]) -> None:
        self.flags = flags
        super().__init__(
            f"{len(flags)} blocking flag(s) must be resolved before approval"
        )

    def as_dict(self) -> dict:
        return {
            "detail": str(self),
            "blocking_flags": [
                {
                    "id": f.id,
                    "category": f.category,
                    "message": f.message,
                    "field_name": f.field_name,
                    "part_id": f.part_id,
                }
                for f in self.flags
            ],
        }


@dataclass
class SendResult:
    quote: Quote
    turnaround_seconds: int | None


def unresolved_blockers(db: Session, enquiry: Enquiry, quote: Quote) -> list[Flag]:
    return blocking_flags(db, quote.id, [part.id for part in enquiry.parts])


def approve(db: Session, enquiry: Enquiry, quote: Quote, *, approved_by: str) -> Quote:
    """Approve a quote. Refuses while any `block` flag is unresolved."""
    if quote.status == QuoteStatus.SENT.value:
        raise ApprovalBlocked([])
    blockers = unresolved_blockers(db, enquiry, quote)
    if blockers:
        raise ApprovalBlocked(blockers)

    quote.status = QuoteStatus.APPROVED.value
    quote.approved_by = approved_by
    quote.approved_at = utcnow()
    enquiry.status = EnquiryStatus.APPROVED.value
    db.flush()
    return quote


def freeze_snapshot(db: Session, enquiry: Enquiry, quote: Quote) -> dict:
    """Capture what was sent, so it stays recoverable after rates change."""
    return {
        "quote_id": quote.id,
        "version": quote.version,
        "quote_value": str(quote.quote_value),
        "subtotal": str(quote.subtotal),
        "labour_total": str(quote.labour_total),
        "material_total": str(quote.material_total),
        "margin_pct": str(quote.margin_pct),
        "margin_value": str(quote.margin_value),
        "adjustments": quote.adjustments,
        "lead_time_days": quote.lead_time_days,
        "approved_by": quote.approved_by,
        "approved_at": quote.approved_at.isoformat() if quote.approved_at else None,
        "lines": [
            {
                "part_id": line.part_id,
                "drawing_number": line.drawing_number,
                "revision": line.revision,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
            }
            for line in quote.lines
        ],
        "operations": [
            {
                "part_id": part.id,
                "op_number": operation.op_number,
                "process": operation.process,
                "description": operation.description,
                "set_time_mins": str(operation.set_time_mins),
                "run_time_mins_per_unit": str(operation.run_time_mins_per_unit),
                "hourly_rate": str(operation.hourly_rate or 0),
                "subcontract_unit_cost": (
                    str(operation.subcontract_unit_cost)
                    if operation.subcontract_unit_cost is not None
                    else None
                ),
                "computed_cost": str(operation.computed_cost or 0),
                "time_source": operation.time_source,
                "rate_table_id": operation.rate_table_id,
            }
            for part in enquiry.parts
            for operation in part.operations
        ],
    }


def mark_sent(db: Session, enquiry: Enquiry, quote: Quote) -> SendResult:
    """Record that the estimator pressed send.

    Sets sent_at, computes the turnaround, and freezes the version — after
    this the quote is a historical record and further work starts a new one.
    """
    if quote.status != QuoteStatus.APPROVED.value:
        raise ApprovalBlocked([])

    sent_at = utcnow()
    quote.sent_at = sent_at
    quote.status = QuoteStatus.SENT.value
    quote.frozen_snapshot = freeze_snapshot(db, enquiry, quote)

    turnaround: int | None = None
    if enquiry.received_at is not None:
        received = enquiry.received_at
        if received.tzinfo is None:
            from datetime import timezone

            received = received.replace(tzinfo=timezone.utc)
        turnaround = int((sent_at - received).total_seconds())
        enquiry.turnaround_seconds = turnaround

    enquiry.status = EnquiryStatus.SENT.value
    db.flush()
    return SendResult(quote=quote, turnaround_seconds=turnaround)


def record_outcome(
    db: Session,
    enquiry: Enquiry,
    quote: Quote,
    *,
    result: str,
    actual_production_mins: Decimal | None = None,
    recorded_by: str | None = None,
    notes: str | None = None,
) -> QuoteOutcome:
    """Record won/lost and, when known, the actual production time.

    This is what calibrates future estimates — without it the archive records
    what was quoted but never whether it was right.
    """
    if result not in {r.value for r in OutcomeResult}:
        raise ValueError(f"'{result}' is not a valid outcome")

    outcome = quote.outcome or QuoteOutcome(quote_id=quote.id)
    outcome.result = result
    outcome.actual_production_mins = actual_production_mins
    outcome.recorded_by = recorded_by
    outcome.notes = notes
    outcome.recorded_at = utcnow()
    if outcome not in db:
        db.add(outcome)

    if result == OutcomeResult.WON.value:
        enquiry.status = EnquiryStatus.WON.value
    elif result == OutcomeResult.LOST.value:
        enquiry.status = EnquiryStatus.LOST.value

    db.flush()
    return outcome


def revise(db: Session, enquiry: Enquiry, sent_quote: Quote) -> Quote:
    """Start a new version after a quote has gone out.

    A sent quote is frozen. Revising copies its rule scope and margin onto a
    fresh version rather than editing history.
    """
    if sent_quote.status != QuoteStatus.SENT.value:
        raise ValueError("Only a sent quote is revised; edit the working quote instead")

    sent_quote.status = QuoteStatus.SUPERSEDED.value
    revision = Quote(
        enquiry_id=enquiry.id,
        version=sent_quote.version + 1,
        status=QuoteStatus.DRAFT.value,
        margin_pct=sent_quote.margin_pct,
        lead_time_days=sent_quote.lead_time_days,
        applied_rule_ids=list(sent_quote.applied_rule_ids or []) or None,
    )
    db.add(revision)
    enquiry.status = EnquiryStatus.IN_REVIEW.value
    db.flush()
    return revision
