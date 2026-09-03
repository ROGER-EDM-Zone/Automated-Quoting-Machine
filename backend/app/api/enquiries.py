"""Enquiry endpoints: the pipeline stages and the workspace read."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.serialise import breakdown_out, part_price_out
from app.db import get_db
from app.deps import CurrentUser, get_ai, get_current_user, get_enquiry, get_storage_dep
from app.enums import EnquiryStatus, FlagSeverity, JobType
from app.models import Customer, Enquiry, Flag, Part, Quote
from app.pricing import PricingError
from app.schemas import (
    EnquiryOut,
    FlagOut,
    PriceRequest,
    QueueItemOut,
    QuoteOut,
    WorkspaceOut,
)
from app.services.approval import unresolved_blockers
from app.services.classification import classify_enquiry, duplicate_check
from app.services.extraction import extract_enquiry
from app.services.quoting import (
    NotPriceable,
    ambiguous_cost_paths,
    current_quote,
    price_breakdown,
    price_enquiry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enquiries"])


@router.post("/enquiries/{enquiry_id}/extract", response_model=EnquiryOut)
def run_extraction(
    enquiry: Enquiry = Depends(get_enquiry),
    db: Session = Depends(get_db),
    ai=Depends(get_ai),
    storage=Depends(get_storage_dep),
    _user: CurrentUser = Depends(get_current_user),
):
    """Run extraction over every drawing. Idempotent and re-runnable."""
    extract_enquiry(db, enquiry, ai=ai, storage=storage)
    duplicate_check(db, enquiry)
    db.commit()
    db.refresh(enquiry)
    return enquiry


@router.post("/enquiries/{enquiry_id}/classify", response_model=EnquiryOut)
def run_classification(
    enquiry: Enquiry = Depends(get_enquiry),
    db: Session = Depends(get_db),
    ai=Depends(get_ai),
    _user: CurrentUser = Depends(get_current_user),
):
    """Job type, process mix, operation skeleton and historical match."""
    classify_enquiry(db, enquiry, ai=ai)
    db.commit()
    db.refresh(enquiry)
    return enquiry


@router.post("/enquiries/{enquiry_id}/price", response_model=QuoteOut)
def run_pricing(
    request: PriceRequest | None = None,
    enquiry: Enquiry = Depends(get_enquiry),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Deterministic recompute. Same inputs, same number, every time."""
    request = request or PriceRequest()
    if request.applied_rule_ids is not None:
        quote = current_quote(db, enquiry)
        if quote is None:
            quote = Quote(enquiry_id=enquiry.id, version=1)
            db.add(quote)
            db.flush()
        quote.applied_rule_ids = request.applied_rule_ids or None

    try:
        quote = price_enquiry(
            db,
            enquiry,
            margin_pct=request.margin_pct,
            recompute_material=request.recompute_material,
        )
    except NotPriceable as exc:
        db.commit()  # keep the blocking flags the attempt raised
        raise HTTPException(status_code=409, detail={"detail": str(exc), "reasons": exc.reasons}) from exc
    except PricingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.commit()
    db.refresh(quote)
    return quote


@router.get("/enquiries/{enquiry_id}", response_model=WorkspaceOut)
def get_workspace(
    enquiry: Enquiry = Depends(get_enquiry),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Everything the estimator workspace needs, in one response."""
    quote = current_quote(db, enquiry) or next(
        (q for q in sorted(enquiry.quotes, key=lambda q: -q.version)), None
    )

    breakdown = None
    if quote is not None and enquiry.parts:
        try:
            breakdown = breakdown_out(price_breakdown(db, enquiry, quote))
        except (PricingError, Exception) as exc:  # noqa: BLE001
            # The workspace must still open when a quote cannot currently be
            # priced — that is exactly when an estimator needs to look at it.
            logger.info("Breakdown unavailable for enquiry %s: %s", enquiry.id, exc)

    enquiry_flags = list(
        db.scalars(select(Flag).where(Flag.enquiry_id == enquiry.id)).all()
    )
    blockers = unresolved_blockers(db, enquiry, quote) if quote is not None else []

    ambiguous: dict[int, dict] = {}
    for part in enquiry.parts:
        if part.job_type != JobType.AMBIGUOUS.value:
            continue
        try:
            ambiguous[part.id] = {
                name: part_price_out(price)
                for name, price in ambiguous_cost_paths(db, part).items()
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("Cost paths unavailable for part %s: %s", part.id, exc)

    return WorkspaceOut(
        enquiry=EnquiryOut.model_validate(enquiry),
        current_quote=QuoteOut.model_validate(quote) if quote else None,
        breakdown=breakdown,
        enquiry_flags=[FlagOut.model_validate(f) for f in enquiry_flags],
        blocking_flag_count=len(blockers),
        can_approve=quote is not None and not blockers and bool(quote.lines),
        ambiguous_paths=ambiguous,
    )


@router.get("/queue", response_model=list[QueueItemOut])
def get_queue(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    status_filter: list[str] | None = Query(default=None, alias="status"),
    sort: str = Query(default="age", pattern="^(age|value|flags|confidence)$"),
    include_closed: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Triage list. Must cope with many enquiries landing at once."""
    stmt = select(Enquiry)
    if status_filter:
        stmt = stmt.where(Enquiry.status.in_(status_filter))
    elif not include_closed:
        stmt = stmt.where(
            Enquiry.status.notin_(
                [EnquiryStatus.WON.value, EnquiryStatus.LOST.value, EnquiryStatus.SENT.value]
            )
        )
    enquiries = list(db.scalars(stmt.order_by(Enquiry.received_at.desc())).all())

    customer_names = dict(db.execute(select(Customer.id, Customer.name)).all())
    now = datetime.now(timezone.utc)
    items: list[QueueItemOut] = []

    for enquiry in enquiries:
        quote = current_quote(db, enquiry) or next(
            (q for q in sorted(enquiry.quotes, key=lambda q: -q.version)), None
        )
        part_ids = [p.id for p in enquiry.parts]
        flag_rows = list(
            db.scalars(
                select(Flag).where(
                    Flag.resolved.is_(False),
                    (Flag.enquiry_id == enquiry.id)
                    | (Flag.part_id.in_(part_ids) if part_ids else False)
                    | (Flag.quote_id == (quote.id if quote else -1)),
                )
            ).all()
        )
        confidences = [
            score
            for part in enquiry.parts
            for score in (part.extraction_confidence or {}).values()
            if isinstance(score, (int, float))
        ]
        received = enquiry.received_at
        if received is not None and received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)

        items.append(
            QueueItemOut(
                enquiry_id=enquiry.id,
                customer_name=customer_names.get(enquiry.customer_id),
                subject=enquiry.subject,
                status=enquiry.status,
                received_at=enquiry.received_at,
                age_hours=round((now - received).total_seconds() / 3600, 1) if received else 0.0,
                part_count=len(enquiry.parts),
                job_types=sorted({p.job_type for p in enquiry.parts}),
                process_mix=sorted(
                    {proc for p in enquiry.parts for proc in (p.process_mix or [])}
                ),
                total_quantity=sum(p.quantity or 0 for p in enquiry.parts),
                quote_id=quote.id if quote else None,
                quote_value=quote.quote_value if quote else None,
                flag_count=len(flag_rows),
                blocking_flag_count=sum(
                    1 for f in flag_rows if f.severity == FlagSeverity.BLOCK.value
                ),
                lowest_confidence=min(confidences) if confidences else None,
                due_date=enquiry.due_date,
            )
        )

    sorters = {
        "age": lambda i: -i.age_hours,
        "value": lambda i: -(i.quote_value or 0),
        "flags": lambda i: (-i.blocking_flag_count, -i.flag_count),
        # Lowest confidence first: the ones most likely to be wrong.
        "confidence": lambda i: (i.lowest_confidence if i.lowest_confidence is not None else 2),
    }
    items.sort(key=sorters[sort])
    return items[:limit]


@router.get("/enquiries/{enquiry_id}/parts/{part_id}/paths")
def get_ambiguous_paths(
    part_id: int,
    enquiry: Enquiry = Depends(get_enquiry),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Both cost paths for an ambiguous part — the system never picks one."""
    part = db.get(Part, part_id)
    if part is None or part.enquiry_id != enquiry.id:
        raise HTTPException(status_code=404, detail="Part not found on this enquiry")
    return {
        name: part_price_out(price)
        for name, price in ambiguous_cost_paths(db, part).items()
    }


@router.get("/stats/pipeline")
def pipeline_stats(
    db: Session = Depends(get_db), _user: CurrentUser = Depends(get_current_user)
):
    rows = db.execute(
        select(Enquiry.status, func.count(Enquiry.id)).group_by(Enquiry.status)
    ).all()
    return {status: count for status, count in rows}
