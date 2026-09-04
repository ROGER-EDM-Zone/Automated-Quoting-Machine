"""Classification service (spec stage 3).

Decides job type, settles the process mix, builds the operation skeleton and
reads the commercial facts out of the email. Everything it produces is an
*input* to the deterministic engine.

Two rules are enforced here in code rather than trusted to the prompt, because
they matter too much to leave to instruction-following:

* if the customer named processes, the routing is filtered down to those —
  anything the model added is dropped and reported;
* an operation time only survives if the model named the past job it came
  from, in which case it is stored as `historical_estimate` so the UI can
  render it as a number to check rather than trust.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import (
    PRODUCTION_PROCESSES,
    EnquiryStatus,
    FlagCategory,
    FlagSeverity,
    JobType,
    Process,
    TimeSource,
)
from app.models import Enquiry, Operation, Part
from app.prompts import classification as classification_prompt
from app.services import flags as flag_service
from app.services.ai import AIError, StructuredCaller, get_ai_client
from app.services.confidence import cross_check
from app.services.history import (
    Match,
    geometry_matches,
    parse_customer_reference,
    problem_matches,
    resolve_anchor,
)

logger = logging.getLogger(__name__)

_VALID_PROCESSES = {p.value for p in Process}


def _part_summary(part: Part) -> str:
    def show(label: str, value) -> str:
        return f"  {label}: {value if value not in (None, '') else 'not read'}"

    lines = [
        f"Drawing {part.drawing_number or 'not read'} rev {part.revision or 'not read'}",
        show("description", part.description),
        show("quantity", part.quantity if part.quantity else None),
        show("material", part.material),
        show("heat treatment", part.heat_treatment),
        show("surface coat", part.surface_coat),
        show("finish", part.finish_spec),
        show(
            "envelope",
            f"{part.envelope_x} x {part.envelope_y} x {part.envelope_z}"
            if part.envelope_x is not None
            else None,
        ),
        show("tightest tolerance", part.tightest_tolerance),
        show("features", part.features),
    ]
    if part.withheld_fields:
        lines.append(
            "  fields read but withheld for low confidence (treat as unknown): "
            + ", ".join(sorted(part.withheld_fields))
        )
    return "\n".join(lines)


def _customer_summary(enquiry: Enquiry) -> str:
    customer = enquiry.customer
    if customer is None:
        return "Unknown customer — no standing preferences on file."
    return "\n".join(
        [
            f"  name: {customer.name}",
            f"  normally supplies their own material: {customer.is_material_supplied_default}",
            f"  requires certification: {customer.requires_cert}",
            f"  standard lead time: {customer.default_lead_days} days",
            f"  notes: {customer.notes or 'none'}",
        ]
    )


def _history_summary(matches: dict[str, list[Match]]) -> str:
    if not any(matches.values()):
        return "  none found — the archive has nothing comparable yet."
    lines: list[str] = []
    for lane, entries in matches.items():
        lines.append(f"  {lane} lane:")
        if not entries:
            lines.append("    (nothing)")
        for match in entries:
            detail = (
                f"    quote {match.quote_id} / drawing {match.drawing_number} "
                f"rev {match.revision}: qty {match.quantity}, "
                f"unit {match.unit_price}, score {match.score:.2f} "
                f"({'; '.join(match.reasons)})"
            )
            if match.result:
                detail += f" [outcome: {match.result}"
                if match.actual_production_mins is not None:
                    detail += f", actual {match.actual_production_mins} mins"
                detail += "]"
            lines.append(detail)
    return "\n".join(lines)


def gather_history(db: Session, part: Part, settings: Settings) -> dict[str, list[Match]]:
    limit = settings.history_candidates_per_lane
    return {
        "geometry": geometry_matches(db, part, limit=limit),
        "problem": problem_matches(db, part, limit=limit),
    }


def classify_part(
    db: Session,
    part: Part,
    *,
    ai: StructuredCaller | None = None,
    settings: Settings | None = None,
) -> dict:
    """Classify one part and build its operation skeleton."""
    settings = settings or get_settings()
    ai = ai or get_ai_client(settings)
    enquiry = part.enquiry
    matches = gather_history(db, part, settings)

    payload = ai.structured(
        system=classification_prompt.SYSTEM,
        prompt=classification_prompt.build_prompt(
            part_summary=_part_summary(part),
            email_subject=enquiry.subject,
            email_body=enquiry.body_text,
            customer_summary=_customer_summary(enquiry),
            history_summary=_history_summary(matches),
            internal_note=enquiry.internal_note,
            forwarded_by=enquiry.forwarded_by,
        ),
        schema=classification_prompt.build_schema(),
    )

    _apply_job_type(db, part, payload)
    named, mix = _apply_process_mix(db, part, payload, settings)
    _apply_operations(db, part, payload, constrained_to=named if named else None)
    _apply_email_facts(db, part, payload)
    _apply_concerns(db, part, payload)
    db.flush()
    return {
        **payload,
        "resolved_process_mix": mix,
        "history": {lane: [m.as_dict() for m in entries] for lane, entries in matches.items()},
    }


def _apply_job_type(db: Session, part: Part, payload: dict) -> None:
    job_type = payload.get("job_type")
    if job_type not in {j.value for j in JobType}:
        job_type = JobType.AMBIGUOUS.value
    part.job_type = job_type

    if job_type == JobType.AMBIGUOUS.value:
        # Not a failure — the correct answer when the signals disagree. Both
        # cost paths get computed and the estimator picks.
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                "Service-only or full supply is unresolved: "
                f"{payload.get('job_type_reasoning') or 'no reason given'}. "
                "Both cost paths are shown; an estimator must choose before "
                "this can be approved."
            ),
            field_name="job_type",
            dedupe_key="job_type_ambiguous",
        )


def _apply_process_mix(
    db: Session, part: Part, payload: dict, settings: Settings
) -> tuple[list[str], list[str]]:
    """Settle the process mix, enforcing the customer's constraint in code."""
    named = [p for p in (payload.get("customer_named_processes") or []) if p in _VALID_PROCESSES]
    proposed = [p for p in (payload.get("process_mix") or []) if p in _VALID_PROCESSES]

    if named:
        allowed = set(named)
        dropped = [p for p in proposed if p not in allowed]
        mix = [p for p in proposed if p in allowed] or named
        part.process_mix_constrained = True
        if dropped:
            # The model went beyond what was asked. Honour the customer and
            # tell the estimator what was removed, in case it matters.
            flag_service.raise_flag(
                db,
                part_id=part.id,
                category=FlagCategory.INDUSTRY_EXPERIENCE.value,
                severity=FlagSeverity.WARN.value,
                message=(
                    "The customer asked for "
                    f"{', '.join(named)}. Additional processes were suggested "
                    f"({', '.join(dropped)}) and have been left out of the "
                    "quote. Add them deliberately if the job really needs them."
                ),
                dedupe_key="processes_constrained",
            )
    else:
        mix = proposed
        part.process_mix_constrained = False
        if mix and not settings.propose_unnamed_processes:
            flag_service.raise_flag(
                db,
                part_id=part.id,
                category=FlagCategory.INDUSTRY_EXPERIENCE.value,
                severity=FlagSeverity.BLOCK.value,
                message=(
                    "The customer named no process and this system is "
                    "configured to quote only what was asked. Proposed "
                    f"routing ({', '.join(mix)}) needs an estimator's approval."
                ),
                dedupe_key="processes_unnamed",
            )
        elif mix:
            flag_service.raise_flag(
                db,
                part_id=part.id,
                category=FlagCategory.INDUSTRY_EXPERIENCE.value,
                severity=FlagSeverity.INFO.value,
                message=(
                    "The customer named no process. Proposed routing: "
                    f"{', '.join(mix)}. This is a proposal, not a request."
                ),
                dedupe_key="processes_proposed",
            )

    part.process_mix = mix or None
    return named, mix


def _apply_operations(
    db: Session,
    part: Part,
    payload: dict,
    *,
    constrained_to: list[str] | None,
) -> None:
    """Build the operation skeleton.

    Times survive only when the model named the past job it copied them from.
    Anything else is stored as zero with `time_source = manual`, and a flag
    says the times are still needed — a zero-cost operation must never look
    like a costed one.
    """
    proposed = payload.get("proposed_operations") or []
    if not proposed:
        return

    allowed = set(constrained_to) if constrained_to else None
    # `qc` and `subcontract` are support/bought-out steps, not production
    # processes, so a customer naming "sparking" does not exclude inspection.
    if allowed:
        allowed |= {Process.QC.value, Process.SUBCONTRACT.value, Process.MANUAL.value}

    existing = {op.op_number: op for op in part.operations}
    needs_times: list[int] = []
    kept_numbers: set[int] = set()

    for index, entry in enumerate(proposed):
        process = entry.get("process")
        if process not in _VALID_PROCESSES:
            continue
        if allowed and process in PRODUCTION_PROCESSES and process not in allowed:
            continue

        op_number = entry.get("op_number") or (index + 1) * 10
        operation = existing.get(op_number)
        if operation is None:
            operation = Operation(part_id=part.id, op_number=op_number, process=process)
            db.add(operation)
        elif operation.time_source == TimeSource.MANUAL.value and (
            operation.set_time_mins or operation.run_time_mins_per_unit
        ):
            # An estimator has already put real times on this op. Leave it.
            kept_numbers.add(op_number)
            continue

        operation.process = process
        operation.description = entry.get("description")

        source = entry.get("source_reference")
        set_time = entry.get("set_time_mins")
        run_time = entry.get("run_time_mins_per_unit")
        if source and (set_time is not None or run_time is not None):
            operation.set_time_mins = Decimal(str(set_time or 0))
            operation.run_time_mins_per_unit = Decimal(str(run_time or 0))
            operation.time_source = TimeSource.HISTORICAL_ESTIMATE.value
        else:
            operation.set_time_mins = Decimal("0")
            operation.run_time_mins_per_unit = Decimal("0")
            operation.time_source = TimeSource.MANUAL.value
            needs_times.append(op_number)
        kept_numbers.add(op_number)

    db.flush()

    if needs_times:
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                "Operations "
                + ", ".join(str(n) for n in sorted(needs_times))
                + " have no times yet. They are costed at zero until a "
                "calculator or an estimator supplies real minutes — this "
                "quote is not complete."
            ),
            dedupe_key="operations_need_times",
        )


def _apply_email_facts(db: Session, part: Part, payload: dict) -> None:
    """Record what the email said, and cross-check it against the drawing."""
    facts = payload.get("email_facts") or {}
    enquiry = part.enquiry

    if facts.get("required_date"):
        try:
            enquiry.due_date = date.fromisoformat(facts["required_date"])
        except (ValueError, TypeError):
            logger.warning("Classifier returned an unparseable date: %r", facts["required_date"])

    reference = facts.get("customer_reference") or parse_customer_reference(enquiry.body_text)
    if reference and not enquiry.customer_reference:
        enquiry.customer_reference = str(reference)

    anchor = resolve_anchor(db, enquiry)
    if anchor is not None:
        enquiry.anchor_quote_id = anchor.id
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.INDUSTRY_EXPERIENCE.value,
            severity=FlagSeverity.INFO.value,
            message=(
                f"This enquiry references quote {anchor.id} "
                f"(£{anchor.quote_value}). Adjust from that quote rather than "
                "pricing from scratch."
            ),
            dedupe_key=f"anchor:{anchor.id}",
        )
    elif enquiry.customer_reference:
        # Historic quote numbers (Op#####, COM#####) live in the old system
        # until the archive is imported, so "not found" here usually means
        # "before this system's time" rather than "wrong reference".
        looks_like_ours = enquiry.customer_reference[:3].lower() in ("op0", "com", "op1")
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.INDUSTRY_EXPERIENCE.value
            if looks_like_ours
            else FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.INFO.value if looks_like_ours else FlagSeverity.WARN.value,
            message=(
                f"This refers back to quote {enquiry.customer_reference}, which "
                "predates this system. Look it up in the old records and price "
                "from there rather than from scratch."
                if looks_like_ours
                else f"The email references '{enquiry.customer_reference}' but that "
                "does not resolve to a sent quote for this customer. Check "
                "before treating it as a repeat."
            ),
            dedupe_key="anchor_unresolved",
        )

    # Quantity: drawing vs email. Flag, never pick (spec stage 2).
    email_quantity = facts.get("quantity")
    if email_quantity:
        drawing_quantity = part.quantity if part.quantity else None
        pending = cross_check("quantity", drawing_quantity, email_quantity)
        if pending is not None:
            flag_service.raise_pending(db, pending, part_id=part.id)
        elif drawing_quantity is None:
            # The drawing did not state one and the email did. The email is
            # the order, so this is the quantity — but it is recorded as
            # coming from the email, not the drawing.
            part.quantity = int(email_quantity)
            part.quantity_source = "email"
            flag_service.clear_field_flags(
                db, part.id, "quantity", resolved_by="classification:email"
            )

    if (
        facts.get("requests_certification")
        and enquiry.customer is not None
        and not enquiry.customer.requires_cert
    ):
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.WARN.value,
            message=(
                "This enquiry asks for certification but the customer record "
                "does not normally require it. Certification costs time — "
                "confirm before quoting."
            ),
            dedupe_key="cert_requested",
        )

    if facts.get("urgency_wording"):
        # Deliberately not turned into a rush uplift. The percentage lives in
        # rules_table and putting it in scope is an estimator's decision.
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
            severity=FlagSeverity.INFO.value,
            message=(
                "The customer said: "
                f'"{facts["urgency_wording"]}". If this warrants a rush '
                "uplift, apply the rush_uplift rule — no uplift has been "
                "applied automatically."
            ),
            dedupe_key="urgency_noted",
        )


def _apply_concerns(db: Session, part: Part, payload: dict) -> None:
    valid = {s.value for s in FlagSeverity}
    for index, concern in enumerate(payload.get("concerns") or []):
        severity = concern.get("severity")
        message = concern.get("message")
        if not message:
            continue
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.INDUSTRY_EXPERIENCE.value,
            severity=severity if severity in valid else FlagSeverity.INFO.value,
            message=message,
            dedupe_key=f"concern:{index}",
        )


def classify_enquiry(
    db: Session,
    enquiry: Enquiry,
    *,
    ai: StructuredCaller | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    """Classify every part on an enquiry."""
    if not enquiry.parts:
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message="Nothing to classify — this enquiry has no parts yet.",
            dedupe_key="no_parts_to_classify",
        )
        db.flush()
        return []

    results: list[dict] = []
    failures: list[str] = []
    for part in enquiry.parts:
        try:
            results.append(classify_part(db, part, ai=ai, settings=settings))
        except AIError as exc:
            logger.exception("Classification failed for part %s", part.id)
            failures.append(str(exc))
            flag_service.raise_flag(
                db,
                part_id=part.id,
                category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
                severity=FlagSeverity.BLOCK.value,
                message=f"Classification failed: {exc}. Route this part by hand.",
                dedupe_key=f"classify_failed:{part.id}",
            )

    if failures and not results:
        enquiry.status = EnquiryStatus.FAILED.value
        enquiry.error_detail = "; ".join(failures)
    elif failures:
        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        enquiry.error_detail = "; ".join(failures)
    else:
        enquiry.status = EnquiryStatus.CLASSIFIED.value
        enquiry.error_detail = None

    db.flush()
    return results


def duplicate_check(db: Session, enquiry: Enquiry) -> None:
    """Duplicate and version-conflict detection (spec stage 1).

    Same drawing at the same revision is a duplicate RFQ; the same drawing at a
    higher revision is a version conflict, and the earlier quote is linked so
    the estimator can see what changed.
    """
    from app.models import Attachment

    for attachment in enquiry.attachments:
        if not attachment.drawing_number:
            continue

        others = db.scalars(
            select(Attachment)
            .where(
                Attachment.drawing_number == attachment.drawing_number,
                Attachment.enquiry_id != enquiry.id,
            )
            .order_by(Attachment.id.desc())
        ).all()

        for other in others:
            prior = db.get(Enquiry, other.enquiry_id)
            if prior is None:
                continue
            prior_quote = next((q for q in sorted(prior.quotes, key=lambda q: -q.version)), None)

            if (other.revision or "") == (attachment.revision or ""):
                flag_service.raise_flag(
                    db,
                    enquiry_id=enquiry.id,
                    category=FlagCategory.DUPLICATE_RFQ.value,
                    severity=FlagSeverity.WARN.value,
                    message=(
                        f"Drawing {attachment.drawing_number} rev "
                        f"{attachment.revision or '-'} was already quoted on "
                        f"enquiry {prior.id}"
                        + (
                            f" (quote {prior_quote.id}, £{prior_quote.quote_value})"
                            if prior_quote
                            else ""
                        )
                        + ". Check whether this is a repeat before requoting."
                    ),
                    dedupe_key=f"duplicate:{other.id}",
                    related_enquiry_id=prior.id,
                    related_quote_id=prior_quote.id if prior_quote else None,
                )
            elif _is_later_revision(attachment.revision, other.revision):
                flag_service.raise_flag(
                    db,
                    enquiry_id=enquiry.id,
                    category=FlagCategory.VERSION_CONFLICT.value,
                    severity=FlagSeverity.BLOCK.value,
                    message=(
                        f"Drawing {attachment.drawing_number} is at rev "
                        f"{attachment.revision}; we previously quoted rev "
                        f"{other.revision} on enquiry {prior.id}"
                        + (f" (quote {prior_quote.id})" if prior_quote else "")
                        + ". The change between revisions may alter the price — "
                        "compare before quoting."
                    ),
                    dedupe_key=f"version_conflict:{other.id}",
                    related_enquiry_id=prior.id,
                    related_quote_id=prior_quote.id if prior_quote else None,
                )
    db.flush()


def _is_later_revision(current: str | None, prior: str | None) -> bool:
    """Is `current` a later revision than `prior`?

    Handles the two schemes drawings actually use — letters and numbers —
    and refuses to compare across them rather than guessing.
    """
    if not current or not prior:
        return False
    current, prior = current.strip().upper(), prior.strip().upper()
    if current == prior:
        return False
    if current.isdigit() and prior.isdigit():
        return int(current) > int(prior)
    if current.isalpha() and prior.isalpha() and len(current) == len(prior):
        return current > prior
    return False
