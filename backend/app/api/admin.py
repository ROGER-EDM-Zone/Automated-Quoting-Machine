"""Business-editable tables (spec section 4 and 6).

"Rates and rules live in the database. A rate change is a data edit, not a
deployment." These endpoints are what makes that true.

Rates are never deleted, only end-dated: a quote sent last month must stay
explicable, and that needs the row it was priced from to still exist.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_ai, get_current_user
from app.models import (
    Customer,
    MarketSource,
    QuoteNote,
    RateTable,
    RulesTable,
    StockSize,
    utcnow,
)
from app.schemas import (
    CustomerIn,
    CustomerOut,
    MarketRefreshOut,
    MarketSeriesOut,
    MarketSourceIn,
    MarketSourceOut,
    RateIn,
    RateOut,
    RuleIn,
    RuleOut,
    StockIn,
    StockOut,
)
from app.services import market
from app.services.notes import promote_note_to_rule, recurring_note_candidates

router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------
@router.get("/rates", response_model=list[RateOut])
def list_rates(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    process: str | None = None,
    current_only: bool = False,
):
    stmt = select(RateTable)
    if process:
        stmt = stmt.where(RateTable.process == process)
    if current_only:
        today = date.today()
        stmt = stmt.where(
            RateTable.effective_from <= today,
            (RateTable.effective_to.is_(None)) | (RateTable.effective_to > today),
        )
    return list(db.scalars(stmt.order_by(RateTable.process, RateTable.effective_from.desc())).all())


@router.post("/rates", response_model=RateOut, status_code=201)
def create_rate(
    body: RateIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Add a rate.

    A new open-ended rate end-dates the previous one for the same process and
    machine group, so the two never both apply on the same day.
    """
    if body.effective_to and body.effective_to < body.effective_from:
        raise HTTPException(status_code=400, detail="effective_to precedes effective_from")

    superseded = db.scalars(
        select(RateTable).where(
            RateTable.process == body.process.value,
            RateTable.machine_group == body.machine_group,
            RateTable.effective_to.is_(None),
            RateTable.effective_from < body.effective_from,
        )
    ).all()
    for row in superseded:
        row.effective_to = body.effective_from

    rate = RateTable(
        process=body.process.value,
        machine_group=body.machine_group,
        hourly_rate=body.hourly_rate,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    db.add(rate)
    db.commit()
    db.refresh(rate)
    return rate


@router.post("/rates/{rate_id}/end", response_model=RateOut)
def end_rate(
    rate_id: int,
    effective_to: date = Query(...),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """End-date a rate. Deliberately no delete — history must stay readable."""
    rate = db.get(RateTable, rate_id)
    if rate is None:
        raise HTTPException(status_code=404, detail="Rate not found")
    if effective_to < rate.effective_from:
        raise HTTPException(status_code=400, detail="effective_to precedes effective_from")
    rate.effective_to = effective_to
    db.commit()
    db.refresh(rate)
    return rate


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
@router.get("/rules", response_model=list[RuleOut])
def list_rules(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    active_only: bool = False,
):
    stmt = select(RulesTable)
    if active_only:
        stmt = stmt.where(RulesTable.active.is_(True))
    return list(db.scalars(stmt.order_by(RulesTable.rule_key, RulesTable.id)).all())


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(
    body: RuleIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    rule = RulesTable(
        rule_key=body.rule_key,
        trigger_description=body.trigger_description,
        adjustment_type=body.adjustment_type.value,
        adjustment_value=body.adjustment_value,
        active=body.active,
        last_reviewed_at=utcnow(),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/rules/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: int,
    body: RuleIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    rule = db.get(RulesTable, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.rule_key = body.rule_key
    rule.trigger_description = body.trigger_description
    rule.adjustment_type = body.adjustment_type.value
    rule.adjustment_value = body.adjustment_value
    rule.active = body.active
    rule.last_reviewed_at = utcnow()
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/rules/promotion-candidates")
def promotion_candidates(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    minimum: int = Query(default=3, ge=2),
):
    """Notes that keep recurring, as candidates for becoming standing rules.

    A suggestion list only. Promotion is a reviewed human decision (spec
    section 6) and nothing here creates a rule.
    """
    return recurring_note_candidates(db, minimum=minimum)


@router.post("/rules/promote/{note_id}", response_model=RuleOut, status_code=201)
def promote_note(
    note_id: int,
    body: RuleIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Promote a recurring note into a standing rule, on the record."""
    note = db.get(QuoteNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    rule = promote_note_to_rule(
        db,
        note,
        rule_key=body.rule_key,
        trigger_description=body.trigger_description or note.note_text[:200],
        adjustment_type=body.adjustment_type.value,
        adjustment_value=body.adjustment_value,
        promoted_by=user.email,
    )
    db.commit()
    db.refresh(rule)
    return rule


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------
@router.get("/stock", response_model=list[StockOut])
def list_stock(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    spec: str | None = None,
):
    stmt = select(StockSize)
    if spec:
        stmt = stmt.where(StockSize.spec == spec)
    return list(db.scalars(stmt.order_by(StockSize.spec, StockSize.id)).all())


@router.post("/stock", response_model=StockOut, status_code=201)
def create_stock(
    body: StockIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    row = StockSize(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/stock/{stock_id}", response_model=StockOut)
def update_stock(
    stock_id: int,
    body: StockIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    row = db.get(StockSize, stock_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Stock size not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
@router.get("/market", response_model=list[MarketSeriesOut])
def list_market_series(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Every outside number the quote depends on, with its age.

    Deliberately one list rather than one per kind: the question an estimator
    asks before sending a quote is "is anything in here out of date", and the
    answer should not require visiting six screens.
    """
    return market.series_summary(db)


@router.get("/market/sources", response_model=list[MarketSourceOut])
def list_market_sources(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    return list(db.scalars(select(MarketSource).order_by(MarketSource.kind, MarketSource.id)).all())


@router.post("/market/sources", response_model=MarketSourceOut, status_code=201)
def create_market_source(
    body: MarketSourceIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Adding a supplier is a data edit, exactly as adding a rate is."""
    row = MarketSource(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/market/sources/{source_id}", response_model=MarketSourceOut)
def update_market_source(
    source_id: int,
    body: MarketSourceIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    row = db.get(MarketSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Market source not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.post("/market/refresh", response_model=MarketRefreshOut)
def refresh_market(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
    ai=Depends(get_ai),
    series_key: str | None = None,
):
    """Go and look again, now.

    Returns what each source said, including the ones that failed and why —
    a refresh that silently half-worked is worse than one that did not run.
    """
    report = market.refresh(db, series_key=series_key, ai=ai)
    db.commit()
    return {
        "results": [
            {
                "series_key": r.series_key,
                "source_name": r.source_name,
                "ok": r.ok,
                "detail": r.detail,
                "value": r.value,
                "unit": r.unit,
                "sizes_found": r.sizes_found,
                "stock_rows_written": r.stock_rows_written,
            }
            for r in report.results
        ],
        "succeeded": len(report.succeeded),
        "failed": len(report.failed),
    }


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Session = Depends(get_db), _user: CurrentUser = Depends(get_current_user)):
    return list(db.scalars(select(Customer).order_by(Customer.name)).all())


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(
    body: CustomerIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    if body.domain and db.scalars(select(Customer).where(Customer.domain == body.domain)).first():
        raise HTTPException(status_code=409, detail=f"Domain {body.domain} already mapped")
    customer = Customer(**body.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: int,
    body: CustomerIn,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    for key, value in body.model_dump().items():
        setattr(customer, key, value)
    db.commit()
    db.refresh(customer)
    return customer
