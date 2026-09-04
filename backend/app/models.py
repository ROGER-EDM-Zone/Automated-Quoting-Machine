"""Data model (spec section 2).

Conventions:
  * Money is ``Numeric(12, 2)``; minutes ``Numeric(10, 2)``; percentages
    ``Numeric(7, 3)``. Everything monetary is read back as ``Decimal`` — the
    pricing engine never sees a float.
  * Enum columns are stored as their string values so the DB stays legible to
    the admin who reads it and to the ERP export later.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    AdjustmentType,
    AttachmentKind,
    EnquiryStatus,
    FlagSeverity,
    JobType,
    MarketMethod,
    Process,
    QuoteStatus,
    TimeSource,
)

Money = Numeric(12, 2)
Minutes = Numeric(10, 2)
Pct = Numeric(7, 3)
Qty = Numeric(12, 3)


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
class Customer(Base, TimestampMixin):
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(200), index=True)

    default_margin_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False, default=Decimal("0"))
    default_lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Service-only vs full supply: does this customer normally send material?
    is_material_supplied_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    requires_cert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    enquiries: Mapped[list[Enquiry]] = relationship(back_populates="customer")


# --------------------------------------------------------------------------
# Enquiries and their attachments
# --------------------------------------------------------------------------
class Enquiry(Base, TimestampMixin):
    __tablename__ = "enquiry"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer.id", ondelete="SET NULL"), index=True
    )
    outlook_message_id: Mapped[str | None] = mapped_column(String(512), unique=True)
    subject: Mapped[str | None] = mapped_column(String(1000))
    body_text: Mapped[str | None] = mapped_column(Text)
    sender_email: Mapped[str | None] = mapped_column(String(320))

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnquiryStatus.RECEIVED.value, index=True
    )
    #: e.g. "previous quote 6123" — the anchor for repricing from history.
    customer_reference: Mapped[str | None] = mapped_column(String(200))
    #: Set when the RFQ reached us as an internal forward. Holds the colleague
    #: who forwarded it; sender_email then holds the actual customer, so
    #: customer matching and the reply both go to the right party.
    forwarded_by: Mapped[str | None] = mapped_column(String(320))
    #: What the forwarder wrote above the chain — "RFQ to process Wire EDM".
    #: Routing instruction from a colleague, not a customer request, and the
    #: classifier is told which is which.
    internal_note: Mapped[str | None] = mapped_column(Text)
    #: Resolved anchor, set when customer_reference maps to a real past quote.
    anchor_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quote.id", ondelete="SET NULL"))
    due_date: Mapped[date | None] = mapped_column(Date)
    #: Computed at send: received_at → sent_at.
    turnaround_seconds: Mapped[int | None] = mapped_column(Integer)
    #: Last pipeline error, when status == failed.
    error_detail: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer | None] = relationship(back_populates="enquiries")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="enquiry", cascade="all, delete-orphan"
    )
    parts: Mapped[list[Part]] = relationship(back_populates="enquiry", cascade="all, delete-orphan")
    quotes: Mapped[list[Quote]] = relationship(
        back_populates="enquiry",
        cascade="all, delete-orphan",
        foreign_keys="Quote.enquiry_id",
    )

    __table_args__ = (Index("ix_enquiry_status_received", "status", "received_at"),)


class Attachment(Base, TimestampMixin):
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiry.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_uri: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AttachmentKind.OTHER.value
    )
    #: SHA-256 of the bytes, for duplicate-RFQ detection.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)

    drawing_number: Mapped[str | None] = mapped_column(String(120), index=True)
    revision: Mapped[str | None] = mapped_column(String(40))
    page_count: Mapped[int | None] = mapped_column(Integer)

    enquiry: Mapped[Enquiry] = relationship(back_populates="attachments")
    parts: Mapped[list[Part]] = relationship(back_populates="attachment")


# --------------------------------------------------------------------------
# Parts, operations, material
# --------------------------------------------------------------------------
class Part(Base, TimestampMixin):
    """A quotable part, one per drawing / line item."""

    __tablename__ = "part"

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiry.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachment.id", ondelete="SET NULL")
    )

    drawing_number: Mapped[str | None] = mapped_column(String(120), index=True)
    revision: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    #: Nullable on purpose. Drawings frequently do not state a quantity, and
    #: defaulting an unread quantity to 1 would be exactly the confidently
    #: wrong value the rest of this system exists to prevent. None means
    #: "nobody has told us yet", and pricing refuses to proceed on it.
    quantity: Mapped[int | None] = mapped_column(Integer)
    #: Where the quantity came from: "drawing", "email" or "estimator". The
    #: workspace shows it, because a quantity read off an email is a different
    #: kind of fact from one printed in a title block.
    quantity_source: Mapped[str | None] = mapped_column(String(20))

    material: Mapped[str | None] = mapped_column(String(200))
    heat_treatment: Mapped[str | None] = mapped_column(String(200))
    surface_coat: Mapped[str | None] = mapped_column(String(200))
    finish_spec: Mapped[str | None] = mapped_column(String(200))

    envelope_x: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    envelope_y: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    envelope_z: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: True when the part is turned — round about one axis. It decides how
    #: much bar the job needs: a round part clears the bore on its own
    #: diameter, a block has to clear its corners too. None means nobody has
    #: said, and the nester infers it from the envelope and the routing.
    is_rotational: Mapped[bool | None] = mapped_column(Boolean)

    tightest_tolerance: Mapped[str | None] = mapped_column(String(120))
    features: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    job_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobType.AMBIGUOUS.value
    )
    #: {"material": 0.96, "tightest_tolerance": 0.71, ...} — one score per
    #: field the vision call returned. Fields scoring below threshold are held
    #: back as null on the part itself and surfaced as "unread" in the UI.
    extraction_confidence: Mapped[dict[str, float] | None] = mapped_column(JSON)
    #: Fields the extractor read but that were withheld for low confidence,
    #: kept so the UI can offer "AI thought X — confirm?" without pricing it.
    withheld_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: Processes the classifier settled on, in sequence.
    process_mix: Mapped[list[str] | None] = mapped_column(JSON)
    #: True when the customer named the processes explicitly, so the
    #: classifier must not infer extras.
    process_mix_constrained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    enquiry: Mapped[Enquiry] = relationship(back_populates="parts")
    attachment: Mapped[Attachment | None] = relationship(back_populates="parts")
    operations: Mapped[list[Operation]] = relationship(
        back_populates="part",
        cascade="all, delete-orphan",
        order_by="Operation.op_number",
    )
    material_requirements: Mapped[list[MaterialRequirement]] = relationship(
        back_populates="part", cascade="all, delete-orphan"
    )
    flags: Mapped[list[Flag]] = relationship(
        back_populates="part",
        cascade="all, delete-orphan",
        foreign_keys="Flag.part_id",
    )
    corrections: Mapped[list[CorrectionLog]] = relationship(
        back_populates="part", cascade="all, delete-orphan"
    )


class Operation(Base, TimestampMixin):
    """Sequenced operation.

    THIS IS THE ERP HANDOFF SURFACE (spec section 2/6). Real op numbers,
    controlled `process` enum, proper numeric fields — never free text in
    place of a field. Today an admin retypes these rows into the ERP by hand;
    the same rows feed the ERP link later, so the shape must not drift.
    """

    __tablename__ = "operation"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("part.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 10, 20, 30 ... gaps left deliberately so an op can be inserted.
    op_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    process: Mapped[str] = mapped_column(String(32), nullable=False)

    set_time_mins: Mapped[Decimal] = mapped_column(Minutes, nullable=False, default=Decimal("0"))
    run_time_mins_per_unit: Mapped[Decimal] = mapped_column(
        Minutes, nullable=False, default=Decimal("0")
    )
    hourly_rate: Mapped[Decimal | None] = mapped_column(Money)
    subcontract_unit_cost: Mapped[Decimal | None] = mapped_column(Money)

    #: Written by the pricing engine only. Never by the AI, never by hand.
    computed_cost: Mapped[Decimal | None] = mapped_column(Money)
    time_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TimeSource.MANUAL.value
    )
    #: When time_source == historical_estimate, the quote it was drawn from.
    source_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quote.id", ondelete="SET NULL"))
    #: The rate_table row used, for audit of a price after a rate change.
    rate_table_id: Mapped[int | None] = mapped_column(
        ForeignKey("rate_table.id", ondelete="SET NULL")
    )

    part: Mapped[Part] = relationship(back_populates="operations")

    __table_args__ = (UniqueConstraint("part_id", "op_number", name="uq_operation_part_opnum"),)

    @property
    def is_subcontract(self) -> bool:
        return self.process == Process.SUBCONTRACT.value


class MaterialRequirement(Base, TimestampMixin):
    """Material purchase line. Empty for service-only jobs."""

    __tablename__ = "material_requirement"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("part.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spec: Mapped[str | None] = mapped_column(String(200))
    stock_form: Mapped[str | None] = mapped_column(String(40))
    stock_size: Mapped[str | None] = mapped_column(String(200))

    qty_required: Mapped[Decimal] = mapped_column(Qty, nullable=False, default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: Nesting outputs — deterministic, not an AI judgement.
    blanks_per_unit_stock: Mapped[int | None] = mapped_column(Integer)
    utilisation_pct: Mapped[Decimal | None] = mapped_column(Pct)
    total_cost: Mapped[Decimal | None] = mapped_column(Money)
    #: What the part actually needed once the machining allowance was on,
    #: kept beside what was bought. "Needed 91.32, bought 100" is the sentence
    #: an estimator wants; "bought 100" on its own is not.
    required_section_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    required_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    section_oversize_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: Where the unit cost came from, when it came from a live source.
    price_source_name: Mapped[str | None] = mapped_column(String(120))
    price_source_url: Mapped[str | None] = mapped_column(String(500))
    price_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    part: Mapped[Part] = relationship(back_populates="material_requirements")


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------
class Quote(Base, TimestampMixin):
    __tablename__ = "quote"

    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(
        ForeignKey("enquiry.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=QuoteStatus.DRAFT.value, index=True
    )

    material_total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    labour_total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    margin_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False, default=Decimal("0"))
    margin_value: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    quote_value: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: Uplifts/contingencies applied from rules_table, itemised for audit.
    adjustments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    #: rules_table rows a human (or the note loop, citing a rule) has put in
    #: scope for this quote. Selecting a rule is a judgement; the percentage
    #: itself always comes from the table. min_quote_value is always in scope
    #: and is not listed here.
    applied_rule_ids: Mapped[list[int] | None] = mapped_column(JSON)
    #: Set when rules_table.min_quote_value lifted the price.
    min_value_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    approved_by: Mapped[str | None] = mapped_column(String(320))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Snapshot of the priced record, frozen at send so the sent numbers are
    #: recoverable even if rates or operations later change.
    frozen_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outlook_draft_id: Mapped[str | None] = mapped_column(String(512))

    enquiry: Mapped[Enquiry] = relationship(back_populates="quotes", foreign_keys=[enquiry_id])
    lines: Mapped[list[QuoteLine]] = relationship(
        back_populates="quote", cascade="all, delete-orphan"
    )
    flags: Mapped[list[Flag]] = relationship(
        back_populates="quote",
        cascade="all, delete-orphan",
        foreign_keys="Flag.quote_id",
    )
    notes: Mapped[list[QuoteNote]] = relationship(
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="QuoteNote.created_at",
    )
    outcome: Mapped[QuoteOutcome | None] = relationship(
        back_populates="quote", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (UniqueConstraint("enquiry_id", "version", name="uq_quote_enquiry_version"),)


class QuoteLine(Base, TimestampMixin):
    __tablename__ = "quote_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), nullable=False, index=True
    )
    part_id: Mapped[int | None] = mapped_column(ForeignKey("part.id", ondelete="SET NULL"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: Denormalised so a frozen/sent quote still reads correctly if the part
    #: is later edited for a new revision.
    drawing_number: Mapped[str | None] = mapped_column(String(120))
    revision: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)

    quote: Mapped[Quote] = relationship(back_populates="lines")
    part: Mapped[Part | None] = relationship()


# --------------------------------------------------------------------------
# Flags, notes, corrections
# --------------------------------------------------------------------------
class Flag(Base, TimestampMixin):
    """The AI's uncertainty and craft-knowledge warnings.

    A flag of severity `block` must be resolved before approval is allowed.
    """

    __tablename__ = "flag"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("part.id", ondelete="CASCADE"), index=True
    )
    quote_id: Mapped[int | None] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), index=True
    )
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("enquiry.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FlagSeverity.WARN.value, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: The extracted field this flag is about, when it is field-level.
    field_name: Mapped[str | None] = mapped_column(String(80))
    #: Stable key so re-running extraction updates rather than duplicates.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)
    #: For version_conflict: the earlier quote this drawing was quoted on.
    related_quote_id: Mapped[int | None] = mapped_column(
        ForeignKey("quote.id", ondelete="SET NULL")
    )
    related_enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("enquiry.id", ondelete="SET NULL")
    )

    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_by: Mapped[str | None] = mapped_column(String(320))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    part: Mapped[Part | None] = relationship(back_populates="flags", foreign_keys=[part_id])
    quote: Mapped[Quote | None] = relationship(back_populates="flags", foreign_keys=[quote_id])

    @property
    def is_blocking(self) -> bool:
        return self.severity == FlagSeverity.BLOCK.value and not self.resolved


class QuoteNote(Base):
    """Human context the AI couldn't know, plus what it changed as a result.

    Notes are training data (spec section 6) — each row is a labelled example
    of judgement the AI could not reach alone. Promoting a recurring note into
    a standing rules_table row is a human decision, never automatic.
    """

    __tablename__ = "quote_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author: Mapped[str] = mapped_column(String(320), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    note_kind: Mapped[str | None] = mapped_column(String(32))
    #: e.g. "removed 15m electrode setup"
    adjustment_summary: Mapped[str | None] = mapped_column(Text)
    price_before: Mapped[Decimal | None] = mapped_column(Money)
    price_after: Mapped[Decimal | None] = mapped_column(Money)
    #: Which rules_table row supplied the percentage, if any. Null here with a
    #: pct adjustment means the AI invented a number — which it must not.
    applied_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("rules_table.id", ondelete="SET NULL")
    )
    #: The concrete input change the AI proposed, as applied.
    proposed_change: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: True when the AI declined to act and asked the estimator instead.
    awaiting_answer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    question: Mapped[str | None] = mapped_column(Text)

    quote: Mapped[Quote] = relationship(back_populates="notes")


class CorrectionLog(Base):
    """Every field an estimator overrode. This is how accuracy gets measured."""

    __tablename__ = "correction_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("part.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    ai_value: Mapped[str | None] = mapped_column(Text)
    human_value: Mapped[str | None] = mapped_column(Text)
    #: The confidence the extractor claimed for this field, so reporting can
    #: separate "confidently wrong" from "flagged and duly corrected".
    ai_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    was_withheld: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    corrected_by: Mapped[str] = mapped_column(String(320), nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    part: Mapped[Part] = relationship(back_populates="corrections")


# --------------------------------------------------------------------------
# Business-editable tables
# --------------------------------------------------------------------------
class RateTable(Base, TimestampMixin):
    """Editable rates. NEVER hardcode these."""

    __tablename__ = "rate_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    process: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    machine_group: Mapped[str | None] = mapped_column(String(120))
    hourly_rate: Mapped[Decimal] = mapped_column(Money, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: Exclusive: the rate applies on effective_from up to but NOT including
    #: effective_to, so a replacement starting on the same day means exactly
    #: one rate is in force on the changeover date.
    effective_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (Index("ix_rate_lookup", "process", "machine_group", "effective_from"),)


class RulesTable(Base, TimestampMixin):
    """Standing adjustment rules, editable by the business.

    The AI may cite a row here; it may not invent a percentage. If no row
    matches, it asks (spec stage 5).
    """

    __tablename__ = "rules_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    trigger_description: Mapped[str | None] = mapped_column(Text)
    adjustment_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AdjustmentType.PCT.value
    )
    adjustment_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("0")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Set when this rule was promoted from a recurring note — a reviewed
    #: human decision, recorded so the review cycle has something to read.
    promoted_from_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("quote_note.id", ondelete="SET NULL")
    )
    promoted_by: Mapped[str | None] = mapped_column(String(320))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockSize(Base, TimestampMixin):
    """Standard stock you actually buy — the nesting calculator's inputs."""

    __tablename__ = "stock_size"

    id: Mapped[int] = mapped_column(primary_key=True)
    spec: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    stock_form: Mapped[str] = mapped_column(String(40), nullable=False)
    #: mm. For bar/tube, length_mm is the stock length and width/thickness the
    #: diameter or section.
    length_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    thickness_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    unit_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Saw/grip allowance added around each blank, mm.
    kerf_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("3"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- live sourcing -------------------------------------------------
    #: Where this row came from. ``manual`` is a row somebody typed and owns;
    #: anything else was written by a market refresh and will be rewritten by
    #: the next one. A refresh never touches a manual row.
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MarketMethod.MANUAL.value
    )
    #: The price series this size costs from, e.g. ``material:en16:round_bar``.
    #: With a series and a density, ``unit_cost`` is computed from the live
    #: price rather than typed; without them the typed figure stands.
    market_series_key: Mapped[str | None] = mapped_column(String(120), index=True)
    #: kg/m3. No default: guessing a density to reach a price is exactly the
    #: kind of invention this system exists to prevent.
    density_kg_m3: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: Whether the supplier currently lists this size. A size that has fallen
    #: out of the range is kept, not deleted, so old quotes still explain
    #: themselves — but it stops being offered.
    listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_name: Mapped[str | None] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(500))


# --------------------------------------------------------------------------
# Live market data
# --------------------------------------------------------------------------
class MarketSource(Base, TimestampMixin):
    """One place the app goes to find out what something costs today.

    Adding a supplier is a data edit, not a deployment — the same rule the
    rate table follows. The source carries its own health, so "the price is
    old" and "the source has been failing for a fortnight" are different
    statements and the UI can make both.
    """

    __tablename__ = "market_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable identifier used in code and in stock rows.
    series_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    basis: Mapped[str] = mapped_column(String(30), nullable=False)
    url: Mapped[str | None] = mapped_column(String(500))
    #: What to look for on the page, in words. Passed to the reader so it
    #: knows whether it wants a per-kg figure, a size range, or both.
    target: Mapped[str | None] = mapped_column(Text)
    #: Material specification this source prices, when it prices one.
    spec: Mapped[str | None] = mapped_column(String(200), index=True)
    stock_form: Mapped[str | None] = mapped_column(String(40))
    #: Hours before a value from this source counts as stale. Steel moves
    #: faster than an energy tariff, so this is per-source.
    max_age_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=168)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    observations: Mapped[list[MarketObservation]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_market_source_series_active", "series_key", "active"),)


class MarketObservation(Base):
    """One reading of one series at one moment. Append-only.

    Never updated and never deleted by a refresh: the record of what the
    system believed when it priced a job is the only way to explain that job
    a year later.
    """

    __tablename__ = "market_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("market_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    series_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GBP")
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    basis: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    #: The text the value was read from, quoted. An unevidenced number from a
    #: reader is treated as unread, exactly as on a drawing.
    evidence: Mapped[str | None] = mapped_column(Text)
    #: Sizes the page listed, when the source is a stock range.
    sizes_mm: Mapped[list[Any] | None] = mapped_column(JSON)
    source_url: Mapped[str | None] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    source: Mapped[MarketSource] = relationship(back_populates="observations")

    __table_args__ = (Index("ix_market_obs_series_time", "series_key", "observed_at"),)


class QuoteOutcome(Base):
    """Won/lost and estimate vs actual, for calibration."""

    __tablename__ = "quote_outcome"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    result: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    actual_production_mins: Mapped[Decimal | None] = mapped_column(Minutes)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    recorded_by: Mapped[str | None] = mapped_column(String(320))
    notes: Mapped[str | None] = mapped_column(Text)

    quote: Mapped[Quote] = relationship(back_populates="outcome")


__all__ = [
    "Attachment",
    "CorrectionLog",
    "Customer",
    "Enquiry",
    "Flag",
    "MaterialRequirement",
    "MarketObservation",
    "MarketSource",
    "Operation",
    "Part",
    "Quote",
    "QuoteLine",
    "QuoteNote",
    "QuoteOutcome",
    "RateTable",
    "RulesTable",
    "StockSize",
]
