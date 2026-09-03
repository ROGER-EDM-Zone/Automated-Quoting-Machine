"""Note → reprice loop (spec stage 5).

The sequence, and the order matters:

  1. record the price before anything changes;
  2. ask the model what the note means, as concrete input changes;
  3. reject any change it was not entitled to make;
  4. apply the survivors to the inputs;
  5. let the deterministic engine recompute the price;
  6. store the note with before/after and the rule it cited.

Step 5 is the only place a number is produced. The model proposes the input
change; it never writes the output price.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import (
    AdjustmentType,
    FlagCategory,
    FlagSeverity,
    JobType,
    NoteKind,
    Process,
    TimeSource,
)
from app.models import Enquiry, Operation, Part, Quote, QuoteNote
from app.prompts import note_intent
from app.services import flags as flag_service
from app.services.ai import AIError, StructuredCaller, get_ai_client
from app.services.quoting import price_enquiry
from app.services.rates import active_rules, rule_by_key

logger = logging.getLogger(__name__)

_PART_FIELDS = {
    "material",
    "heat_treatment",
    "surface_coat",
    "finish_spec",
    "tightest_tolerance",
    "quantity",
    "job_type",
    "description",
}


class NoteError(Exception):
    pass


def _quote_summary(db: Session, enquiry: Enquiry, quote: Quote) -> str:
    lines = [
        f"Quote {quote.id} v{quote.version}, margin {quote.margin_pct}%",
        f"  labour {quote.labour_total}, material {quote.material_total}, "
        f"total {quote.quote_value}",
        "",
    ]
    for part in enquiry.parts:
        lines.append(
            f"Part {part.id}: drawing {part.drawing_number or '?'} rev "
            f"{part.revision or '?'}, qty {part.quantity}, "
            f"job type {part.job_type}, material {part.material or 'not read'}"
        )
        for operation in part.operations:
            source = {
                TimeSource.CALCULATOR.value: "calculator",
                TimeSource.HISTORICAL_ESTIMATE.value: "estimated from history",
                TimeSource.MANUAL.value: "entered by hand",
            }.get(operation.time_source, operation.time_source)
            if operation.is_subcontract:
                detail = f"subcontract at {operation.subcontract_unit_cost}/unit"
            else:
                detail = (
                    f"set {operation.set_time_mins}m, run "
                    f"{operation.run_time_mins_per_unit}m/unit, rate "
                    f"{operation.hourly_rate}/h [{source}]"
                )
            lines.append(
                f"  op {operation.op_number} {operation.process}: "
                f"{operation.description or ''} — {detail} — "
                f"cost {operation.computed_cost}"
            )
        for requirement in part.material_requirements:
            lines.append(
                f"  material: {requirement.spec} {requirement.stock_size}, "
                f"{requirement.qty_required} off at {requirement.unit_cost} = "
                f"{requirement.total_cost}"
            )
    return "\n".join(lines)


def _rules_summary(db: Session) -> tuple[str, list[str]]:
    rows = active_rules(db)
    if not rows:
        return (
            "  none defined. You cannot apply any percentage adjustment — "
            "use `ask`.",
            [],
        )
    lines = [
        f"  {row.rule_key}: {row.trigger_description or 'no description'} "
        f"({row.adjustment_type} {row.adjustment_value})"
        for row in rows
    ]
    return "\n".join(lines), [row.rule_key for row in rows]


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def add_note(
    db: Session,
    enquiry: Enquiry,
    quote: Quote,
    *,
    note_text: str,
    author: str,
    ai: StructuredCaller | None = None,
) -> QuoteNote:
    """Interpret a note, apply what it implies, and reprice."""
    if not note_text.strip():
        raise NoteError("A note cannot be empty")

    ai = ai or get_ai_client()
    price_before = Decimal(quote.quote_value)
    rules_summary, rule_keys = _rules_summary(db)

    note = QuoteNote(
        quote_id=quote.id,
        author=author,
        note_text=note_text.strip(),
        price_before=price_before,
    )
    db.add(note)
    db.flush()

    try:
        payload = ai.structured(
            system=note_intent.SYSTEM,
            prompt=note_intent.build_prompt(
                note_text=note_text,
                quote_summary=_quote_summary(db, enquiry, quote),
                rules_summary=rules_summary,
            ),
            schema=note_intent.build_schema(rule_keys),
        )
    except AIError as exc:
        # The note is still recorded — it is the estimator's context and is
        # worth keeping even when the interpretation failed.
        logger.exception("Note interpretation failed for quote %s", quote.id)
        note.adjustment_summary = f"Could not interpret this note automatically: {exc}"
        note.awaiting_answer = True
        note.question = "Apply this change by hand — the interpreter was unavailable."
        note.price_after = price_before
        db.flush()
        return note

    kind = payload.get("note_kind")
    note.note_kind = kind if kind in {k.value for k in NoteKind} else None
    note.adjustment_summary = payload.get("summary")

    applied, questions, rejected, rule_id = _apply_actions(
        db, enquiry, quote, payload.get("actions") or []
    )
    note.proposed_change = {
        "actions": payload.get("actions") or [],
        "applied": applied,
        "rejected": rejected,
    }
    note.applied_rule_id = rule_id

    if questions:
        note.awaiting_answer = True
        note.question = " ".join(questions)
        flag_service.raise_flag(
            db,
            quote_id=quote.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.WARN.value,
            message=f"Note needs an answer before it can be applied: {note.question}",
            dedupe_key=f"note_question:{note.id}",
        )

    if applied:
        price_enquiry(db, enquiry, margin_pct=Decimal(quote.margin_pct))
        db.refresh(quote)
    note.price_after = Decimal(quote.quote_value)
    db.flush()
    return note


def _apply_actions(
    db: Session,
    enquiry: Enquiry,
    quote: Quote,
    actions: list[dict],
) -> tuple[list[dict], list[str], list[dict], int | None]:
    """Apply the actions the model is entitled to make.

    Returns (applied, questions, rejected, applied_rule_id). A rejected action
    is recorded rather than dropped — an estimator should be able to see that
    the AI wanted to do something and was not allowed to.
    """
    applied: list[dict] = []
    questions: list[str] = []
    rejected: list[dict] = []
    applied_rule_id: int | None = None

    parts_by_id = {part.id: part for part in enquiry.parts}

    def reject(action: dict, why: str) -> None:
        rejected.append({"action": action, "reason": why})
        logger.info("Rejected note action %s: %s", action.get("action"), why)

    for action in actions:
        kind = action.get("action")

        if kind == "ask":
            question = action.get("question") or action.get("reason")
            if question:
                questions.append(question)
            continue

        if kind == "apply_rule":
            rule_key = action.get("rule_key")
            if not rule_key:
                reject(action, "no rule named — a percentage cannot be invented")
                questions.append(
                    "No standing rule was cited for this adjustment. Which rule "
                    "should apply, or should one be created?"
                )
                continue
            rule = rule_by_key(db, rule_key)
            if rule is None:
                reject(action, f"rule '{rule_key}' is not active in rules_table")
                questions.append(
                    f"There is no active '{rule_key}' rule. Add one in "
                    "/admin/rules before this adjustment can be applied."
                )
                continue
            if rule.adjustment_type == AdjustmentType.FLAG_ONLY.value:
                reject(action, f"rule '{rule_key}' is flag-only and changes no price")
                flag_service.raise_flag(
                    db,
                    quote_id=quote.id,
                    category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
                    severity=FlagSeverity.WARN.value,
                    message=f"{rule.trigger_description or rule_key} — for judgement, no price change.",
                    dedupe_key=f"flag_only_rule:{rule.id}",
                )
                continue
            scope = list(quote.applied_rule_ids or [])
            if rule.id not in scope:
                scope.append(rule.id)
                quote.applied_rule_ids = scope
            applied_rule_id = rule.id
            applied.append({**action, "resolved_rule_id": rule.id})
            continue

        if kind == "set_margin_pct":
            margin = _decimal(action.get("margin_pct"))
            if margin is None or not (Decimal("0") <= margin <= Decimal("100")):
                reject(action, "margin must be a number the estimator stated, 0-100")
                questions.append("What margin percentage should this quote carry?")
                continue
            quote.margin_pct = margin
            applied.append(action)
            continue

        part = parts_by_id.get(action.get("part_id")) if action.get("part_id") else None
        if part is None and len(enquiry.parts) == 1:
            part = enquiry.parts[0]
        if part is None:
            reject(action, "could not tell which part this applies to")
            questions.append(
                f"Which part does this apply to? ({action.get('reason') or kind})"
            )
            continue

        if kind == "set_operation_time":
            _set_operation_time(db, part, action, applied, reject, questions)
        elif kind == "remove_operation":
            _remove_operation(db, part, action, applied, reject)
        elif kind == "add_operation":
            _add_operation(db, part, action, applied, reject, questions)
        elif kind == "set_part_field":
            _set_part_field(db, part, action, applied, reject)
        else:
            reject(action, f"unknown action '{kind}'")

    db.flush()
    return applied, questions, rejected, applied_rule_id


def _find_operation(part: Part, op_number) -> Operation | None:
    return next((op for op in part.operations if op.op_number == op_number), None)


def _set_operation_time(db, part, action, applied, reject, questions) -> None:
    operation = _find_operation(part, action.get("op_number"))
    if operation is None:
        reject(action, f"part {part.id} has no operation {action.get('op_number')}")
        return
    set_time = _decimal(action.get("set_time_mins"))
    run_time = _decimal(action.get("run_time_mins_per_unit"))
    if set_time is None and run_time is None:
        reject(action, "no time given — the estimator must state the new minutes")
        questions.append(
            f"What should op {operation.op_number} ({operation.process}) be set to?"
        )
        return
    if (set_time is not None and set_time < 0) or (run_time is not None and run_time < 0):
        reject(action, "negative times are not a thing")
        return
    if set_time is not None:
        operation.set_time_mins = set_time
    if run_time is not None:
        operation.run_time_mins_per_unit = run_time
    # The estimator gave this number, so it is a manual time — not a
    # calculator output and not an AI estimate.
    operation.time_source = TimeSource.MANUAL.value
    applied.append(action)


def _remove_operation(db, part, action, applied, reject) -> None:
    operation = _find_operation(part, action.get("op_number"))
    if operation is None:
        reject(action, f"part {part.id} has no operation {action.get('op_number')}")
        return
    db.delete(operation)
    part.operations.remove(operation)
    applied.append(action)


def _add_operation(db, part, action, applied, reject, questions) -> None:
    process = action.get("process")
    if process not in {p.value for p in Process}:
        reject(action, f"'{process}' is not a process this shop runs")
        return
    op_number = action.get("op_number")
    if op_number is None:
        op_number = (max((op.op_number for op in part.operations), default=0)) + 10
    if _find_operation(part, op_number) is not None:
        reject(action, f"operation {op_number} already exists")
        return

    operation = Operation(
        part_id=part.id,
        op_number=op_number,
        process=process,
        description=action.get("description"),
        set_time_mins=_decimal(action.get("set_time_mins")) or Decimal("0"),
        run_time_mins_per_unit=_decimal(action.get("run_time_mins_per_unit")) or Decimal("0"),
        time_source=TimeSource.MANUAL.value,
    )
    db.add(operation)
    part.operations.append(operation)
    applied.append(action)

    if not operation.set_time_mins and not operation.run_time_mins_per_unit:
        questions.append(
            f"Op {op_number} ({process}) was added with no times — it costs "
            "nothing until someone supplies minutes."
        )
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message=f"Operation {op_number} ({process}) has no times and is costed at zero.",
            dedupe_key=f"op_no_times:{op_number}",
        )


def _set_part_field(db, part, action, applied, reject) -> None:
    field_name = action.get("field_name")
    value = action.get("field_value")
    if field_name not in _PART_FIELDS:
        reject(action, f"'{field_name}' is not an editable part field")
        return
    if field_name == "quantity":
        try:
            quantity = int(str(value))
        except (TypeError, ValueError):
            reject(action, f"'{value}' is not a quantity")
            return
        if quantity < 1:
            reject(action, "quantity must be at least 1")
            return
        part.quantity = quantity
    elif field_name == "job_type":
        if value not in {j.value for j in JobType}:
            reject(action, f"'{value}' is not a job type")
            return
        part.job_type = value
    else:
        setattr(part, field_name, value)
    applied.append(action)


def promote_note_to_rule(
    db: Session,
    note: QuoteNote,
    *,
    rule_key: str,
    trigger_description: str,
    adjustment_type: str,
    adjustment_value: Decimal,
    promoted_by: str,
):
    """Turn a recurring note into a standing rule.

    Spec section 6: "promoting a recurring note into a standing rule is a
    human decision, reviewed periodically, not something the system does
    silently." Hence this is an explicit admin action with a named person on
    it, and nothing in the pipeline calls it.
    """
    from app.models import RulesTable, utcnow

    rule = RulesTable(
        rule_key=rule_key,
        trigger_description=trigger_description,
        adjustment_type=adjustment_type,
        adjustment_value=adjustment_value,
        active=True,
        promoted_from_note_id=note.id,
        promoted_by=promoted_by,
        last_reviewed_at=utcnow(),
    )
    db.add(rule)
    db.flush()
    return rule


def recurring_note_candidates(db: Session, *, minimum: int = 3) -> list[dict]:
    """Notes whose wording keeps recurring, as candidates for promotion.

    Suggestion only. Nothing here changes a price or creates a rule — a human
    reads this list and decides.
    """
    from collections import Counter

    notes = db.scalars(select(QuoteNote).where(QuoteNote.note_kind.is_not(None))).all()
    counter: Counter[str] = Counter()
    examples: dict[str, list[int]] = {}
    for note in notes:
        key = (note.adjustment_summary or note.note_text).strip().lower()[:120]
        if not key:
            continue
        counter[key] += 1
        examples.setdefault(key, []).append(note.id)

    return [
        {"summary": key, "occurrences": count, "note_ids": examples[key]}
        for key, count in counter.most_common()
        if count >= minimum
    ]
