"""Reporting (spec section 4).

Turnaround, extraction accuracy from correction_log, win rate, and estimate vs
actual. These are the measurements that say whether the system is working —
particularly extraction accuracy, which the spec singles out as the number
that predicts whether the project saves time or creates a new checking burden.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_current_user
from app.enums import OutcomeResult, QuoteStatus, TimeSource
from app.models import CorrectionLog, Enquiry, Operation, Part, Quote, QuoteOutcome

router = APIRouter(prefix="/reports", tags=["reports"])


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@router.get("/turnaround")
def turnaround(
    days: int = Query(default=90, ge=1, le=1095),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Received to sent, for enquiries that went out."""
    rows = db.scalars(
        select(Enquiry).where(
            Enquiry.turnaround_seconds.is_not(None),
            Enquiry.received_at >= _since(days),
        )
    ).all()
    if not rows:
        return {"enquiries": 0, "median_hours": None, "mean_hours": None, "within_24h_pct": None}

    hours = sorted(r.turnaround_seconds / 3600 for r in rows)
    middle = len(hours) // 2
    median = hours[middle] if len(hours) % 2 else (hours[middle - 1] + hours[middle]) / 2
    return {
        "enquiries": len(hours),
        "median_hours": round(median, 2),
        "mean_hours": round(sum(hours) / len(hours), 2),
        "fastest_hours": round(hours[0], 2),
        "slowest_hours": round(hours[-1], 2),
        "within_24h_pct": round(100 * sum(1 for h in hours if h <= 24) / len(hours), 1),
    }


@router.get("/extraction-accuracy")
def extraction_accuracy(
    days: int = Query(default=90, ge=1, le=1095),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """How often the AI's reading needed correcting, per field.

    The important split is `confidently_wrong` — corrections to fields the
    extractor was *sure* about. A field that was flagged and then corrected is
    the system working; a field that was confidently wrong and only caught
    because someone happened to look is the failure mode that matters.
    """
    corrections = db.scalars(
        select(CorrectionLog).where(CorrectionLog.corrected_at >= _since(days))
    ).all()

    extracted: dict[str, int] = defaultdict(int)
    for part in db.scalars(select(Part)).all():
        for field_name in part.extraction_confidence or {}:
            extracted[field_name] += 1

    per_field: dict[str, dict] = {}
    for row in corrections:
        entry = per_field.setdefault(
            row.field_name,
            {
                "corrections": 0,
                "confidently_wrong": 0,
                "corrected_after_withholding": 0,
                "mean_ai_confidence": None,
                "_confidences": [],
            },
        )
        entry["corrections"] += 1
        if row.was_withheld:
            entry["corrected_after_withholding"] += 1
        elif row.ai_confidence is not None and float(row.ai_confidence) >= 0.8:
            entry["confidently_wrong"] += 1
        if row.ai_confidence is not None:
            entry["_confidences"].append(float(row.ai_confidence))

    for field_name, entry in per_field.items():
        confidences = entry.pop("_confidences")
        entry["mean_ai_confidence"] = (
            round(sum(confidences) / len(confidences), 3) if confidences else None
        )
        seen = extracted.get(field_name, 0)
        entry["fields_extracted"] = seen
        entry["correction_rate_pct"] = round(100 * entry["corrections"] / seen, 1) if seen else None

    return {
        "window_days": days,
        "total_corrections": len(corrections),
        "per_field": dict(sorted(per_field.items(), key=lambda kv: -kv[1]["corrections"])),
    }


@router.get("/win-rate")
def win_rate(
    days: int = Query(default=365, ge=1, le=1095),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    outcomes = db.scalars(
        select(QuoteOutcome).where(QuoteOutcome.recorded_at >= _since(days))
    ).all()
    counts: dict[str, int] = defaultdict(int)
    value_won = Decimal("0")
    value_total = Decimal("0")
    for outcome in outcomes:
        counts[outcome.result] += 1
        quote = outcome.quote
        if quote is not None:
            value_total += Decimal(quote.quote_value)
            if outcome.result == OutcomeResult.WON.value:
                value_won += Decimal(quote.quote_value)

    decided = counts[OutcomeResult.WON.value] + counts[OutcomeResult.LOST.value]
    sent_count = db.scalar(
        select(func.count(Quote.id)).where(Quote.status == QuoteStatus.SENT.value)
    )
    return {
        "window_days": days,
        "outcomes_recorded": len(outcomes),
        "quotes_sent": sent_count or 0,
        "by_result": dict(counts),
        "win_rate_pct": (
            round(100 * counts[OutcomeResult.WON.value] / decided, 1) if decided else None
        ),
        "value_quoted": f"{value_total:.2f}",
        "value_won": f"{value_won:.2f}",
    }


@router.get("/estimate-vs-actual")
def estimate_vs_actual(
    days: int = Query(default=365, ge=1, le=1095),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Quoted minutes against what the job actually took.

    Broken down by time_source, because that is the question worth answering:
    are the AI's historical estimates worse than the calculator's numbers, and
    by how much?
    """
    outcomes = db.scalars(
        select(QuoteOutcome).where(
            QuoteOutcome.actual_production_mins.is_not(None),
            QuoteOutcome.recorded_at >= _since(days),
        )
    ).all()

    rows: list[dict] = []
    by_source: dict[str, list[float]] = defaultdict(list)

    for outcome in outcomes:
        quote = outcome.quote
        if quote is None:
            continue
        enquiry = quote.enquiry
        quoted_mins = Decimal("0")
        sources: set[str] = set()
        for part in enquiry.parts:
            for operation in part.operations:
                if operation.process == "subcontract":
                    continue
                quoted_mins += Decimal(operation.set_time_mins) + (
                    Decimal(operation.run_time_mins_per_unit) * part.quantity
                )
                sources.add(operation.time_source)
        if quoted_mins <= 0:
            continue

        actual = Decimal(outcome.actual_production_mins)
        ratio = float(actual / quoted_mins)
        rows.append(
            {
                "quote_id": quote.id,
                "quoted_mins": f"{quoted_mins:.2f}",
                "actual_mins": f"{actual:.2f}",
                "ratio": round(ratio, 3),
                "time_sources": sorted(sources),
            }
        )
        # A job mixing sources counts towards each one present, since we
        # cannot attribute the overrun to a single operation.
        for source in sources:
            by_source[source].append(ratio)

    summary = {
        source: {
            "jobs": len(ratios),
            "mean_ratio": round(sum(ratios) / len(ratios), 3),
            "over_estimate_pct": round(100 * sum(1 for r in ratios if r > 1.1) / len(ratios), 1),
        }
        for source, ratios in by_source.items()
    }
    return {"window_days": days, "jobs": rows, "by_time_source": summary}


@router.get("/time-source-mix")
def time_source_mix(db: Session = Depends(get_db), _user: CurrentUser = Depends(get_current_user)):
    """How much of the quoted work rests on numbers that need checking."""
    rows = db.execute(
        select(Operation.time_source, func.count(Operation.id)).group_by(Operation.time_source)
    ).all()
    counts = {source: count for source, count in rows}
    total = sum(counts.values())
    return {
        "counts": counts,
        "total": total,
        "estimated_pct": (
            round(100 * counts.get(TimeSource.HISTORICAL_ESTIMATE.value, 0) / total, 1)
            if total
            else None
        ),
    }
