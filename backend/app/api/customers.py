"""Customer view: standing preferences, quote history, win rate."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_current_user
from app.enums import OutcomeResult, QuoteStatus
from app.models import Customer, Enquiry, Quote
from app.schemas import CustomerOut

router = APIRouter(tags=["customers"])


@router.get("/customers/{customer_id}")
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    enquiries = list(
        db.scalars(
            select(Enquiry)
            .where(Enquiry.customer_id == customer_id)
            .order_by(Enquiry.received_at.desc())
        ).all()
    )
    enquiry_ids = [e.id for e in enquiries]
    quotes = (
        list(
            db.scalars(
                select(Quote)
                .where(Quote.enquiry_id.in_(enquiry_ids))
                .order_by(Quote.id.desc())
            ).all()
        )
        if enquiry_ids
        else []
    )

    sent = [q for q in quotes if q.status in (QuoteStatus.SENT.value, QuoteStatus.SUPERSEDED.value)]
    won = [q for q in sent if q.outcome and q.outcome.result == OutcomeResult.WON.value]
    lost = [q for q in sent if q.outcome and q.outcome.result == OutcomeResult.LOST.value]
    decided = len(won) + len(lost)

    turnarounds = [e.turnaround_seconds for e in enquiries if e.turnaround_seconds]

    return {
        "customer": CustomerOut.model_validate(customer),
        "enquiry_count": len(enquiries),
        "quotes_sent": len(sent),
        "win_rate_pct": round(100 * len(won) / decided, 1) if decided else None,
        "value_won": f"{sum((Decimal(q.quote_value) for q in won), Decimal('0')):.2f}",
        "mean_turnaround_hours": (
            round(sum(turnarounds) / len(turnarounds) / 3600, 2) if turnarounds else None
        ),
        "history": [
            {
                "enquiry_id": enquiry.id,
                "subject": enquiry.subject,
                "received_at": enquiry.received_at,
                "status": enquiry.status,
                "drawing_numbers": sorted(
                    {p.drawing_number for p in enquiry.parts if p.drawing_number}
                ),
                "quote_id": next((q.id for q in quotes if q.enquiry_id == enquiry.id), None),
                "quote_value": next(
                    (f"{Decimal(q.quote_value):.2f}" for q in quotes if q.enquiry_id == enquiry.id),
                    None,
                ),
                "outcome": next(
                    (
                        q.outcome.result
                        for q in quotes
                        if q.enquiry_id == enquiry.id and q.outcome
                    ),
                    None,
                ),
            }
            for enquiry in enquiries
        ],
    }
