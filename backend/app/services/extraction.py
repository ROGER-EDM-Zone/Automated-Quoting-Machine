"""Extraction service (spec stage 2).

One AI vision call per drawing, forced to a JSON schema, with a confidence per
field. What comes back is filtered through `services.confidence` before it
touches the part record: a field the model was unsure of never becomes a value
the pricing engine can see.

Idempotent and re-runnable, as the `/enquiries/:id/extract` endpoint promises —
re-extracting a drawing updates its part in place rather than adding a second
one, and re-raises its flags by dedupe key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import AttachmentKind, EnquiryStatus, FlagCategory, FlagSeverity
from app.models import Attachment, Enquiry, Part
from app.prompts import extraction as extraction_prompt
from app.services import flags as flag_service
from app.services.ai import AIError, AIRefused, ImageBlock, StructuredCaller, get_ai_client
from app.services.confidence import (
    ConfidenceOutcome,
    apply_policy,
    readings_from_payload,
)
from app.services.rasterise import RasteriseError, rasterise_image, rasterise_pdf, to_image_blocks
from app.services.storage import LocalStorage, Storage, get_storage

logger = logging.getLogger(__name__)

#: Fields written straight onto the part when accepted.
_SCALAR_FIELDS = (
    "drawing_number",
    "revision",
    "description",
    "quantity",
    "material",
    "heat_treatment",
    "surface_coat",
    "finish_spec",
    "tightest_tolerance",
)
_ENVELOPE_FIELDS = ("envelope_x", "envelope_y", "envelope_z")


@dataclass
class ExtractionResult:
    part: Part
    outcome: ConfidenceOutcome
    #: Raw payload, kept so a re-score against a changed threshold does not
    #: need another AI call.
    payload: dict


def _load_bytes(attachment: Attachment, storage: Storage) -> bytes:
    if not attachment.blob_uri:
        raise RasteriseError(f"Attachment {attachment.id} has no stored blob")
    if isinstance(storage, LocalStorage) and attachment.blob_uri.startswith("file:"):
        return storage.get_by_uri(attachment.blob_uri)
    # Azure blob URIs end in the key we stored under.
    return storage.get(attachment.blob_uri.rsplit("/", 1)[-1])


def _images_for(attachment: Attachment, storage: Storage) -> list[ImageBlock]:
    data = _load_bytes(attachment, storage)
    mime = (attachment.mime_type or "").lower()
    if mime == "application/pdf" or attachment.filename.lower().endswith(".pdf"):
        pages = rasterise_pdf(data)
    else:
        pages = rasterise_image(data, mime or "image/png")
    attachment.page_count = len(pages)
    return to_image_blocks(pages)


def extract_attachment(
    db: Session,
    attachment: Attachment,
    *,
    ai: StructuredCaller | None = None,
    storage: Storage | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Extract one drawing into its part."""
    settings = settings or get_settings()
    ai = ai or get_ai_client(settings)
    storage = storage or get_storage(settings)
    enquiry = attachment.enquiry

    images = _images_for(attachment, storage)
    payload = ai.structured(
        system=extraction_prompt.SYSTEM,
        prompt=extraction_prompt.build_prompt(
            page_count=len(images),
            filename=attachment.filename,
            email_subject=enquiry.subject,
            email_body=enquiry.body_text,
        ),
        schema=extraction_prompt.build_schema(),
        images=images,
    )

    field_payload = {
        name: payload[name] for name in extraction_prompt.FIELDS if name in payload
    }
    outcome = apply_policy(readings_from_payload(field_payload), settings)

    part = db.scalars(
        select(Part).where(
            Part.enquiry_id == attachment.enquiry_id,
            Part.attachment_id == attachment.id,
        )
    ).first()
    if part is None:
        part = Part(enquiry_id=attachment.enquiry_id, attachment_id=attachment.id)
        db.add(part)

    _apply_to_part(part, outcome, payload)
    db.flush()

    # Carry the drawing identity onto the attachment so duplicate and
    # version-conflict detection has something to match on next time.
    if part.drawing_number:
        attachment.drawing_number = part.drawing_number
    if part.revision:
        attachment.revision = part.revision

    for pending in outcome.flags:
        flag_service.raise_pending(db, pending, part_id=part.id)

    _raise_model_reported_flags(db, part, payload)
    return ExtractionResult(part=part, outcome=outcome, payload=payload)


def _apply_to_part(part: Part, outcome: ConfidenceOutcome, payload: dict) -> None:
    """Write accepted fields onto the part; blank the rest.

    Withheld fields are explicitly set back to None. That matters on a
    re-extraction: a value that used to be confident and now is not must stop
    being priced, not linger from the previous run.
    """
    for name in _SCALAR_FIELDS:
        if name == "quantity":
            continue
        if name in outcome.accepted:
            setattr(part, name, outcome.accepted[name])
        elif name in outcome.withheld or name in outcome.unread:
            setattr(part, name, None)

    # An unread quantity stays None. It is not "one off" — it is unknown, and
    # pricing refuses to run until a person or the email supplies it.
    if "quantity" in outcome.accepted and outcome.accepted["quantity"]:
        part.quantity = int(outcome.accepted["quantity"])
        part.quantity_source = "drawing"
    elif "quantity" in outcome.withheld or "quantity" in outcome.unread:
        part.quantity = None
        part.quantity_source = None

    for name in _ENVELOPE_FIELDS:
        if name in outcome.accepted and outcome.accepted[name] is not None:
            from decimal import Decimal

            setattr(part, name, Decimal(str(outcome.accepted[name])))
        elif name in outcome.withheld or name in outcome.unread:
            setattr(part, name, None)

    features = payload.get("features") or {}
    if payload.get("units"):
        features = {**features, "units": payload["units"]}
    part.features = features or None
    part.extraction_confidence = outcome.confidences or None
    part.withheld_fields = outcome.withheld or None


def _raise_model_reported_flags(db: Session, part: Part, payload: dict) -> None:
    """Turn the model's own `conflicts` and `illegible` entries into flags.

    The prompt tells the model to report a disagreement rather than resolve
    it. This is where that instruction is honoured — the conflict becomes a
    human decision instead of a silently chosen value.
    """
    for index, conflict in enumerate(payload.get("conflicts") or []):
        field = conflict.get("field") or "unspecified"
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value
            if field in ("quantity", "material", "tightest_tolerance")
            else FlagSeverity.WARN.value,
            message=f"Conflicting information on {field}: {conflict.get('detail', '')}",
            field_name=field if field != "unspecified" else None,
            dedupe_key=f"conflict:{field}:{index}",
        )

    for entry in payload.get("illegible") or []:
        field = entry.get("field") or "unspecified"
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.WARN.value,
            message=f"Could not read {field}: {entry.get('reason', 'not stated')}",
            field_name=field if field != "unspecified" else None,
            dedupe_key=f"illegible:{field}",
        )

    notes = (payload.get("features") or {}).get("notes")
    if notes:
        # The model's craft observations are advisory: an estimator reads them
        # and decides. They never change a number on their own.
        flag_service.raise_flag(
            db,
            part_id=part.id,
            category=FlagCategory.INDUSTRY_EXPERIENCE.value,
            severity=FlagSeverity.INFO.value,
            message=f"Drawing observations: {notes}",
            dedupe_key="drawing_notes",
        )


def extract_enquiry(
    db: Session,
    enquiry: Enquiry,
    *,
    ai: StructuredCaller | None = None,
    storage: Storage | None = None,
    settings: Settings | None = None,
) -> list[ExtractionResult]:
    """Extract every drawing on an enquiry. Safe to call repeatedly."""
    drawings = [
        a for a in enquiry.attachments if a.kind == AttachmentKind.DRAWING.value
    ]
    if not drawings:
        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        flag_service.raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message="No drawing attachments found on this enquiry — nothing to extract.",
            dedupe_key="no_drawings",
        )
        db.flush()
        return []

    enquiry.status = EnquiryStatus.EXTRACTING.value
    db.flush()

    results: list[ExtractionResult] = []
    failures: list[str] = []
    for attachment in drawings:
        try:
            results.append(
                extract_attachment(
                    db, attachment, ai=ai, storage=storage, settings=settings
                )
            )
        except AIRefused as exc:
            failures.append(f"{attachment.filename}: model declined ({exc.category})")
            flag_service.raise_flag(
                db,
                enquiry_id=enquiry.id,
                category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
                severity=FlagSeverity.BLOCK.value,
                message=(
                    f"Extraction declined for {attachment.filename}. "
                    "This drawing needs reading by hand."
                ),
                dedupe_key=f"ai_refused:{attachment.id}",
            )
        except (AIError, RasteriseError) as exc:
            logger.exception("Extraction failed for attachment %s", attachment.id)
            failures.append(f"{attachment.filename}: {exc}")
            flag_service.raise_flag(
                db,
                enquiry_id=enquiry.id,
                category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
                severity=FlagSeverity.BLOCK.value,
                message=f"Extraction failed for {attachment.filename}: {exc}",
                dedupe_key=f"extract_failed:{attachment.id}",
            )

    if failures and not results:
        enquiry.status = EnquiryStatus.FAILED.value
        enquiry.error_detail = "; ".join(failures)
    elif failures:
        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        enquiry.error_detail = "; ".join(failures)
    else:
        enquiry.status = EnquiryStatus.EXTRACTED.value
        enquiry.error_detail = None

    db.flush()
    return results
