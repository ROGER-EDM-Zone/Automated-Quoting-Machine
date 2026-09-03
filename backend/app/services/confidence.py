"""Confidence policy (spec section 6: "Confidence is a first-class field").

Every field the vision call returns carries a score. A field scoring below its
threshold is *withheld*: it does not reach the part record, it cannot flow into
pricing, and the UI renders it as unread rather than as a value. The value the
extractor thought it saw is kept to one side so an estimator can be asked to
confirm it — but nothing prices off it until they do.

The thresholds themselves are an open decision in the spec (section 8), so they
live in settings and are per-field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.enums import FlagCategory, FlagSeverity

#: Fields that must be present and trusted before a part can be priced at all.
#: Quantity drives every operation cost; without it there is no number to give.
PRICING_CRITICAL_FIELDS = frozenset({"quantity"})

#: Fields whose absence should stop approval rather than merely warn, because
#: quoting without them means quoting a different part from the one drawn.
BLOCKING_FIELDS = frozenset({"quantity", "material"})


@dataclass
class FieldReading:
    """One field as the extractor reported it."""

    name: str
    value: Any
    confidence: float | None
    #: Where on the drawing the extractor believes it read this, if it said.
    evidence: str | None = None

    @property
    def is_null(self) -> bool:
        return self.value is None or self.value == ""


@dataclass
class PendingFlag:
    """A flag to raise, described independently of the ORM."""

    category: str
    severity: str
    message: str
    field_name: str | None = None
    dedupe_key: str | None = None


@dataclass
class ConfidenceOutcome:
    """The result of applying the policy to one part's readings."""

    #: Fields safe to write onto the part and to price from.
    accepted: dict[str, Any] = field(default_factory=dict)
    #: {field: value} the extractor read but that scored too low to use.
    withheld: dict[str, Any] = field(default_factory=dict)
    #: {field: score} for every field the extractor scored, accepted or not.
    confidences: dict[str, float] = field(default_factory=dict)
    #: Fields the extractor honestly returned as null.
    unread: list[str] = field(default_factory=list)
    flags: list[PendingFlag] = field(default_factory=list)

    @property
    def has_blocking_gap(self) -> bool:
        return any(f.severity == FlagSeverity.BLOCK.value for f in self.flags)

    def is_priceable(self) -> bool:
        """Can the deterministic engine be run on this part at all?"""
        return all(
            name in self.accepted and self.accepted[name] is not None
            for name in PRICING_CRITICAL_FIELDS
        )


def _severity_for(field_name: str) -> str:
    return FlagSeverity.BLOCK.value if field_name in BLOCKING_FIELDS else FlagSeverity.WARN.value


def apply_policy(
    readings: list[FieldReading],
    settings: Settings | None = None,
) -> ConfidenceOutcome:
    """Split readings into accepted and withheld, and raise the flags.

    Three distinct cases, all of which the UI must show differently:

    * **null** — the extractor could not read the field and said so. This is
      the honest answer the prompt asks for and is treated as unread.
    * **below threshold** — it read something but is not sure enough. The
      value is withheld and offered back as a question, never priced.
    * **accepted** — it read the field with enough confidence to use.
    """
    settings = settings or get_settings()
    outcome = ConfidenceOutcome()

    for reading in readings:
        threshold = settings.threshold_for(reading.name)

        if reading.confidence is not None:
            outcome.confidences[reading.name] = round(float(reading.confidence), 4)

        if reading.is_null:
            outcome.unread.append(reading.name)
            outcome.flags.append(
                PendingFlag(
                    category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
                    severity=_severity_for(reading.name),
                    message=(
                        f"{reading.name.replace('_', ' ')} could not be read from the "
                        "drawing. Needs an estimator to supply it."
                    ),
                    field_name=reading.name,
                    dedupe_key=f"unread:{reading.name}",
                )
            )
            continue

        if reading.confidence is None or float(reading.confidence) < threshold:
            score = (
                f"{float(reading.confidence):.2f}"
                if reading.confidence is not None
                else "none given"
            )
            outcome.withheld[reading.name] = reading.value
            outcome.flags.append(
                PendingFlag(
                    category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
                    severity=_severity_for(reading.name),
                    message=(
                        f"{reading.name.replace('_', ' ')} read as "
                        f"'{reading.value}' with confidence {score}, below the "
                        f"{threshold:.2f} threshold. Withheld — confirm before quoting."
                    ),
                    field_name=reading.name,
                    dedupe_key=f"low_confidence:{reading.name}",
                )
            )
            continue

        outcome.accepted[reading.name] = reading.value

    return outcome


def readings_from_payload(payload: dict[str, Any]) -> list[FieldReading]:
    """Turn the extractor's tool-call payload into readings.

    Accepts the per-field object shape the extraction tool schema asks for::

        {"material": {"value": "1.2312", "confidence": 0.96, "evidence": "..."}}

    A bare scalar is treated as a value with no confidence stated, which the
    policy then withholds — an unscored value is not a trusted one.
    """
    readings: list[FieldReading] = []
    for name, raw in payload.items():
        if isinstance(raw, dict) and "value" in raw:
            confidence = raw.get("confidence")
            readings.append(
                FieldReading(
                    name=name,
                    value=raw.get("value"),
                    confidence=None if confidence is None else float(confidence),
                    evidence=raw.get("evidence"),
                )
            )
        else:
            readings.append(FieldReading(name=name, value=raw, confidence=None))
    return readings


def cross_check(
    field_name: str,
    drawing_value: Any,
    email_value: Any,
) -> PendingFlag | None:
    """Flag a disagreement between two sources rather than picking one.

    Spec stage 2: "if two views or drawing vs email disagree (e.g. quantity),
    flag rather than pick."
    """
    if drawing_value is None or email_value is None:
        return None
    if str(drawing_value).strip().lower() == str(email_value).strip().lower():
        return None
    return PendingFlag(
        category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
        severity=_severity_for(field_name),
        message=(
            f"{field_name.replace('_', ' ')} disagrees between sources: drawing "
            f"says '{drawing_value}', email says '{email_value}'. Not resolved "
            "automatically — an estimator must choose."
        ),
        field_name=field_name,
        dedupe_key=f"cross_check:{field_name}",
    )
