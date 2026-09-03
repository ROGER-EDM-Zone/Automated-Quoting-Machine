"""Historical match lanes (spec stage 3).

Two separate lanes, deliberately not blended into one "similarity" number:

* **Geometry match** — envelope, feature type, material. Answers "have we cut
  a shape like this before".
* **Problem match** — tolerance band, material/hardness, and the flag history
  of that part number. Answers "have we been hurt by a job like this before".

They are separate because they fail differently. A part can be geometrically
routine and commercially painful, and an estimator needs to see which of the
two a suggestion is coming from before deciding what to do with it.

Scoring is plain arithmetic over stored fields — no AI. The archive is the
learning mechanism (spec section 6), so what improves over time is the volume
and quality of these rows, not a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import QuoteStatus
from app.models import Enquiry, Flag, Part, Quote, QuoteLine

#: Tolerance bands in mm, tightest first. A fit callout (H7, h6) is mapped
#: onto a band rather than parsed dimensionally.
_BANDS: tuple[tuple[str, Decimal], ...] = (
    ("ultra", Decimal("0.005")),
    ("tight", Decimal("0.02")),
    ("fine", Decimal("0.05")),
    ("general", Decimal("999")),
)

_NUMBER = re.compile(r"(\d+\.\d+|\.\d+|\d+)")
#: ISO fit callouts: a letter plus an IT grade, e.g. H7, h6, g6, k5.
_FIT = re.compile(r"\b([A-Za-z])\s?([4-9]|1[0-4])\b")


def tolerance_band(tolerance: str | None) -> str | None:
    """Classify a tolerance string into a band.

    Returns None when there is nothing to classify — which is itself
    information: an unread tolerance must not silently match "general".
    """
    if not tolerance:
        return None
    text = tolerance.strip()

    # A fit callout only counts when the grade digit is the *only* number in
    # the string. "H7" is a fit; "H7 within 0.008" is a dimensional tolerance
    # and the number governs.
    fit = _FIT.search(text)
    if fit and not _NUMBER.search(_FIT.sub("", text, count=1)):
        grade = int(fit.group(2))
        # IT4-IT6 behave like a tight fit, IT7+ like a fine one.
        return "tight" if grade <= 6 else "fine"

    numbers = [Decimal(m) for m in _NUMBER.findall(text) if Decimal(m) > 0]
    if not numbers:
        return None
    smallest = min(numbers)
    for name, ceiling in _BANDS:
        if smallest <= ceiling:
            return name
    return "general"


@dataclass
class Match:
    """One historical part, and why it was suggested."""

    part_id: int
    quote_id: int | None
    enquiry_id: int
    drawing_number: str | None
    revision: str | None
    description: str | None
    quantity: int | None
    quote_value: Decimal | None
    unit_price: Decimal | None
    #: 0-1. Comparable within a lane, not across lanes.
    score: float
    reasons: list[str] = field(default_factory=list)
    #: From quote_outcome, when it was recorded.
    result: str | None = None
    actual_production_mins: Decimal | None = None

    def as_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "quote_id": self.quote_id,
            "enquiry_id": self.enquiry_id,
            "drawing_number": self.drawing_number,
            "revision": self.revision,
            "description": self.description,
            "quantity": self.quantity,
            "quote_value": str(self.quote_value) if self.quote_value is not None else None,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "result": self.result,
            "actual_production_mins": (
                str(self.actual_production_mins)
                if self.actual_production_mins is not None
                else None
            ),
        }


def _archive_parts(db: Session, exclude_part_id: int | None) -> list[Part]:
    """Parts belonging to quotes that actually went out.

    An unsent draft is not evidence of anything — it was never tested against
    a customer, and half of them are abandoned mid-edit.
    """
    stmt = (
        select(Part)
        .join(Enquiry, Part.enquiry_id == Enquiry.id)
        .join(Quote, Quote.enquiry_id == Enquiry.id)
        .where(Quote.status == QuoteStatus.SENT.value)
    )
    if exclude_part_id is not None:
        stmt = stmt.where(Part.id != exclude_part_id)
    # De-duplicate: the join multiplies a part by its quote versions.
    seen: dict[int, Part] = {}
    for part in db.scalars(stmt).all():
        seen.setdefault(part.id, part)
    return list(seen.values())


def _line_for(db: Session, part: Part) -> QuoteLine | None:
    return db.scalars(
        select(QuoteLine)
        .join(Quote, QuoteLine.quote_id == Quote.id)
        .where(QuoteLine.part_id == part.id, Quote.status == QuoteStatus.SENT.value)
        .order_by(Quote.version.desc())
    ).first()


def _volume(part: Part) -> Decimal | None:
    if None in (part.envelope_x, part.envelope_y, part.envelope_z):
        return None
    return Decimal(part.envelope_x) * Decimal(part.envelope_y) * Decimal(part.envelope_z)


def _feature_counts(part: Part) -> dict[str, int]:
    features = part.features or {}
    return {
        key: int(value)
        for key, value in features.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _to_match(db: Session, part: Part, score: float, reasons: list[str]) -> Match:
    line = _line_for(db, part)
    quote = line.quote if line is not None else None
    outcome = quote.outcome if quote is not None else None
    return Match(
        part_id=part.id,
        quote_id=quote.id if quote else None,
        enquiry_id=part.enquiry_id,
        drawing_number=part.drawing_number,
        revision=part.revision,
        description=part.description,
        quantity=part.quantity,
        quote_value=quote.quote_value if quote else None,
        unit_price=line.unit_price if line else None,
        score=score,
        reasons=reasons,
        result=outcome.result if outcome else None,
        actual_production_mins=outcome.actual_production_mins if outcome else None,
    )


# --------------------------------------------------------------------------
# Lane 1: geometry
# --------------------------------------------------------------------------
def geometry_matches(db: Session, part: Part, *, limit: int = 5) -> list[Match]:
    """Parts of a similar shape, material and feature mix."""
    subject_volume = _volume(part)
    subject_features = _feature_counts(part)
    results: list[Match] = []

    for candidate in _archive_parts(db, part.id):
        score = 0.0
        reasons: list[str] = []

        if part.material and candidate.material:
            if candidate.material.strip().lower() == part.material.strip().lower():
                score += 0.35
                reasons.append(f"same material ({candidate.material})")

        candidate_volume = _volume(candidate)
        if subject_volume and candidate_volume and candidate_volume > 0:
            ratio = float(candidate_volume / subject_volume)
            if 0.5 <= ratio <= 2.0:
                # Closest to 1.0 scores highest.
                closeness = 1 - abs(1 - min(ratio, 1 / ratio if ratio else 1))
                score += 0.35 * max(closeness, 0.0)
                reasons.append(f"envelope within {ratio:.2f}x")

        if subject_features and (candidate_features := _feature_counts(candidate)):
            shared = set(subject_features) & set(candidate_features)
            if shared:
                overlap = len(shared) / len(set(subject_features) | set(candidate_features))
                score += 0.30 * overlap
                reasons.append(f"shares {len(shared)} feature type(s)")

        if score > 0:
            results.append(_to_match(db, candidate, min(score, 1.0), reasons))

    results.sort(key=lambda m: (-m.score, m.part_id))
    return results[:limit]


# --------------------------------------------------------------------------
# Lane 2: problems
# --------------------------------------------------------------------------
def problem_matches(db: Session, part: Part, *, limit: int = 5) -> list[Match]:
    """Parts that were hard for the reasons this one might be hard."""
    subject_band = tolerance_band(part.tightest_tolerance)
    results: list[Match] = []

    for candidate in _archive_parts(db, part.id):
        score = 0.0
        reasons: list[str] = []

        candidate_band = tolerance_band(candidate.tightest_tolerance)
        if subject_band and candidate_band == subject_band:
            score += 0.4
            reasons.append(f"same tolerance band ({subject_band})")

        if part.material and candidate.material:
            if candidate.material.strip().lower() == part.material.strip().lower():
                score += 0.2
                reasons.append("same material")
        if part.heat_treatment and candidate.heat_treatment:
            if candidate.heat_treatment.strip().lower() == part.heat_treatment.strip().lower():
                score += 0.15
                reasons.append(f"same heat treatment ({candidate.heat_treatment})")

        # Flag history on the same drawing number is the strongest signal
        # there is: this exact part has caused trouble before.
        if part.drawing_number and candidate.drawing_number == part.drawing_number:
            prior_flags = db.scalars(
                select(Flag).where(Flag.part_id == candidate.id)
            ).all()
            if prior_flags:
                score += 0.25
                categories = sorted({f.category for f in prior_flags})
                reasons.append(f"same drawing previously flagged: {', '.join(categories)}")

        if score > 0:
            results.append(_to_match(db, candidate, min(score, 1.0), reasons))

    results.sort(key=lambda m: (-m.score, m.part_id))
    return results[:limit]


# --------------------------------------------------------------------------
# The anchor
# --------------------------------------------------------------------------
_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:previous\s+)?quote\s*(?:no\.?|number|#)?\s*(\d{3,7})\b", re.I),
    re.compile(r"\bjob\s*(?:no\.?|number|#)?\s*(\d{3,7})\b", re.I),
    re.compile(r"\bq[-/]?(\d{3,7})\b", re.I),
    re.compile(r"\byour\s+ref(?:erence)?\s*:?\s*(\d{3,7})\b", re.I),
)


def parse_customer_reference(text: str | None) -> str | None:
    """Pull a quote/job number out of an email body.

    Regex first, as the spec prescribes; the classification call then confirms
    it against the actual email wording, so a stray order number in a
    signature block does not become an anchor on its own.
    """
    if not text:
        return None
    for pattern in _REFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def resolve_anchor(db: Session, enquiry: Enquiry) -> Quote | None:
    """Turn a customer reference into the past quote it names.

    "If customer_reference resolves to a real past quote, that quote is the
    anchor: pull its actual times and prices, and adjust from there rather
    than pricing from scratch." (spec stage 3)

    Only a sent quote counts, and only one belonging to the same customer —
    a bare number from one customer must never resolve into another's history.
    """
    if not enquiry.customer_reference:
        return None
    if not enquiry.customer_reference.isdigit():
        return None

    quote = db.get(Quote, int(enquiry.customer_reference))
    if quote is None or quote.status != QuoteStatus.SENT.value:
        return None
    if enquiry.customer_id is not None:
        anchor_enquiry = db.get(Enquiry, quote.enquiry_id)
        if anchor_enquiry is None or anchor_enquiry.customer_id != enquiry.customer_id:
            return None
    return quote
