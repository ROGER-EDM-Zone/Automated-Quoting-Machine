"""Raising and resolving flags.

Flags are the system's way of saying "a human needs to look at this". They are
idempotent by `dedupe_key` so that re-running extraction or classification
updates a flag rather than burying the estimator in duplicates — an enquiry
whose flag list grows every time a pipeline stage re-runs is an enquiry nobody
reads.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import FlagSeverity
from app.models import Flag, utcnow
from app.services.confidence import PendingFlag


def raise_flag(
    db: Session,
    *,
    category: str,
    severity: str,
    message: str,
    enquiry_id: int | None = None,
    part_id: int | None = None,
    quote_id: int | None = None,
    field_name: str | None = None,
    dedupe_key: str | None = None,
    related_quote_id: int | None = None,
    related_enquiry_id: int | None = None,
) -> Flag:
    """Create or update one flag.

    A flag that is re-raised after a human resolved it is deliberately
    re-opened: if extraction still cannot read the material, the fact that
    somebody ticked it off last time does not make it readable.
    """
    existing: Flag | None = None
    if dedupe_key:
        stmt = select(Flag).where(Flag.dedupe_key == dedupe_key)
        if part_id is not None:
            stmt = stmt.where(Flag.part_id == part_id)
        elif quote_id is not None:
            stmt = stmt.where(Flag.quote_id == quote_id)
        elif enquiry_id is not None:
            stmt = stmt.where(Flag.enquiry_id == enquiry_id)
        existing = db.scalars(stmt).first()

    if existing is not None:
        existing.category = category
        existing.severity = severity
        existing.message = message
        existing.field_name = field_name
        existing.related_quote_id = related_quote_id
        existing.related_enquiry_id = related_enquiry_id
        if existing.resolved:
            existing.resolved = False
            existing.resolved_by = None
            existing.resolved_at = None
            existing.resolution_note = None
        return existing

    flag = Flag(
        enquiry_id=enquiry_id,
        part_id=part_id,
        quote_id=quote_id,
        category=category,
        severity=severity,
        message=message,
        field_name=field_name,
        dedupe_key=dedupe_key,
        related_quote_id=related_quote_id,
        related_enquiry_id=related_enquiry_id,
    )
    db.add(flag)
    return flag


def raise_pending(
    db: Session,
    pending: PendingFlag,
    *,
    enquiry_id: int | None = None,
    part_id: int | None = None,
    quote_id: int | None = None,
) -> Flag:
    return raise_flag(
        db,
        category=pending.category,
        severity=pending.severity,
        message=pending.message,
        field_name=pending.field_name,
        dedupe_key=pending.dedupe_key,
        enquiry_id=enquiry_id,
        part_id=part_id,
        quote_id=quote_id,
    )


def resolve_flag(db: Session, flag: Flag, *, resolved_by: str, note: str | None = None) -> Flag:
    flag.resolved = True
    flag.resolved_by = resolved_by
    flag.resolved_at = utcnow()
    flag.resolution_note = note
    return flag


def clear_field_flags(db: Session, part_id: int, field_name: str, *, resolved_by: str) -> int:
    """Resolve the extraction flags for a field an estimator has just supplied.

    Called from the part-override path: once a human has typed the material in,
    "material could not be read" is answered and should stop blocking.
    """
    flags = db.scalars(
        select(Flag).where(
            Flag.part_id == part_id,
            Flag.field_name == field_name,
            Flag.resolved.is_(False),
        )
    ).all()
    for flag in flags:
        resolve_flag(db, flag, resolved_by=resolved_by, note="Value supplied by estimator")
    return len(flags)


def blocking_flags(db: Session, quote_id: int, part_ids: list[int]) -> list[Flag]:
    """Every unresolved `block` flag standing between this quote and approval."""
    stmt = select(Flag).where(
        Flag.severity == FlagSeverity.BLOCK.value,
        Flag.resolved.is_(False),
    )
    conditions = [Flag.quote_id == quote_id]
    if part_ids:
        conditions.append(Flag.part_id.in_(part_ids))
    from sqlalchemy import or_

    return list(db.scalars(stmt.where(or_(*conditions))).all())
