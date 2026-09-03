"""Part overrides and operations.

Every field an estimator changes writes a correction_log row. Without that
there is no way to know whether extraction is getting better, and the system
repeats its mistakes indefinitely (spec section 6).
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_current_user, get_part
from app.models import CorrectionLog, Operation, Part
from app.schemas import OperationIn, OperationOut, PartOut, PartPatch
from app.services.flags import clear_field_flags

router = APIRouter(tags=["parts"])


def _as_text(value) -> str | None:
    if value is None:
        return None
    return str(value)


@router.patch("/parts/{part_id}", response_model=PartOut)
def patch_part(
    patch: PartPatch,
    part: Part = Depends(get_part),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Override extracted fields, logging every change."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to change")

    confidences = part.extraction_confidence or {}
    withheld = dict(part.withheld_fields or {})

    for field_name, new_value in changes.items():
        old_value = getattr(part, field_name)
        if isinstance(new_value, Decimal) or isinstance(old_value, Decimal):
            unchanged = _as_text(old_value) == _as_text(new_value)
        else:
            unchanged = old_value == new_value
        if unchanged:
            continue

        # The AI's value for the log is whatever it actually produced — the
        # value on the part if it was accepted, or the withheld reading if it
        # was held back. Those are different failures and reporting must be
        # able to tell them apart.
        was_withheld = field_name in withheld
        ai_value = withheld.get(field_name, old_value)

        db.add(
            CorrectionLog(
                part_id=part.id,
                field_name=field_name,
                ai_value=_as_text(ai_value),
                human_value=_as_text(new_value),
                ai_confidence=confidences.get(field_name),
                was_withheld=was_withheld,
                corrected_by=user.email,
            )
        )

        setattr(part, field_name, new_value.value if hasattr(new_value, "value") else new_value)
        if field_name == "quantity":
            part.quantity_source = "estimator"

        # A value an estimator has supplied answers the "could not read it"
        # flags for that field.
        clear_field_flags(db, part.id, field_name, resolved_by=user.email)
        withheld.pop(field_name, None)

    part.withheld_fields = withheld or None
    db.commit()
    db.refresh(part)
    return part


@router.get("/parts/{part_id}/corrections")
def get_corrections(
    part: Part = Depends(get_part),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    rows = db.scalars(
        select(CorrectionLog)
        .where(CorrectionLog.part_id == part.id)
        .order_by(CorrectionLog.corrected_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "field_name": row.field_name,
            "ai_value": row.ai_value,
            "human_value": row.human_value,
            "ai_confidence": float(row.ai_confidence) if row.ai_confidence is not None else None,
            "was_withheld": row.was_withheld,
            "corrected_by": row.corrected_by,
            "corrected_at": row.corrected_at,
        }
        for row in rows
    ]


@router.put("/parts/{part_id}/operations", response_model=list[OperationOut])
def replace_operations(
    operations: list[OperationIn],
    part: Part = Depends(get_part),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Replace a part's operation list.

    Op numbers must be unique and are kept in sequence — this list is the ERP
    handoff surface and an admin retypes it by hand today.
    """
    numbers = [op.op_number for op in operations]
    if len(numbers) != len(set(numbers)):
        raise HTTPException(status_code=400, detail="Operation numbers must be unique")

    for existing in list(part.operations):
        db.delete(existing)
    part.operations.clear()
    db.flush()

    for entry in sorted(operations, key=lambda o: o.op_number):
        db.add(
            Operation(
                part_id=part.id,
                op_number=entry.op_number,
                process=entry.process.value,
                description=entry.description,
                set_time_mins=entry.set_time_mins,
                run_time_mins_per_unit=entry.run_time_mins_per_unit,
                subcontract_unit_cost=entry.subcontract_unit_cost,
                time_source=entry.time_source.value,
            )
        )
    db.commit()
    db.refresh(part)
    return part.operations
