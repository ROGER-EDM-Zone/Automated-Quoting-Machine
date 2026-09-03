"""Request and response models.

Money crosses the wire as a decimal-formatted string. JSON numbers are
binary floats in most clients, and a quote value that arrives as
957.0000000001 is not a quote value.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.enums import (
    AdjustmentType,
    FlagSeverity,
    JobType,
    OutcomeResult,
    Process,
    TimeSource,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _serialise_decimals(self, value: Any, _info):
        if isinstance(value, Decimal):
            return f"{value:.2f}"
        return value


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
class FlagOut(ORMModel):
    id: int
    category: str
    severity: str
    message: str
    field_name: str | None = None
    part_id: int | None = None
    quote_id: int | None = None
    enquiry_id: int | None = None
    related_quote_id: int | None = None
    related_enquiry_id: int | None = None
    resolved: bool
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    created_at: datetime


class OperationOut(ORMModel):
    id: int
    op_number: int
    process: str
    description: str | None = None
    set_time_mins: Decimal
    run_time_mins_per_unit: Decimal
    hourly_rate: Decimal | None = None
    subcontract_unit_cost: Decimal | None = None
    computed_cost: Decimal | None = None
    #: The UI must render calculator and historical_estimate differently.
    time_source: str
    rate_table_id: int | None = None
    source_quote_id: int | None = None


class MaterialRequirementOut(ORMModel):
    id: int
    spec: str | None = None
    stock_form: str | None = None
    stock_size: str | None = None
    qty_required: Decimal
    unit_cost: Decimal
    blanks_per_unit_stock: int | None = None
    utilisation_pct: Decimal | None = None
    total_cost: Decimal | None = None


class PartOut(ORMModel):
    id: int
    attachment_id: int | None = None
    drawing_number: str | None = None
    revision: str | None = None
    description: str | None = None
    #: None when nobody has stated a quantity yet — never defaulted to 1.
    quantity: int | None = None
    #: "drawing", "email" or "estimator".
    quantity_source: str | None = None
    material: str | None = None
    heat_treatment: str | None = None
    surface_coat: str | None = None
    finish_spec: str | None = None
    envelope_x: Decimal | None = None
    envelope_y: Decimal | None = None
    envelope_z: Decimal | None = None
    tightest_tolerance: str | None = None
    features: dict[str, Any] | None = None
    job_type: str
    #: {field: score} for everything the extractor scored.
    extraction_confidence: dict[str, float] | None = None
    #: Fields read but withheld as too uncertain to price. The workspace shows
    #: these as "unread", never as values.
    withheld_fields: dict[str, Any] | None = None
    process_mix: list[str] | None = None
    process_mix_constrained: bool
    operations: list[OperationOut] = Field(default_factory=list)
    material_requirements: list[MaterialRequirementOut] = Field(default_factory=list)
    flags: list[FlagOut] = Field(default_factory=list)


class AttachmentOut(ORMModel):
    id: int
    filename: str
    mime_type: str | None = None
    kind: str
    drawing_number: str | None = None
    revision: str | None = None
    page_count: int | None = None
    size_bytes: int | None = None


class QuoteLineOut(ORMModel):
    id: int
    part_id: int | None = None
    drawing_number: str | None = None
    revision: str | None = None
    description: str | None = None
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class QuoteNoteOut(ORMModel):
    id: int
    author: str
    note_text: str
    created_at: datetime
    note_kind: str | None = None
    adjustment_summary: str | None = None
    price_before: Decimal | None = None
    price_after: Decimal | None = None
    applied_rule_id: int | None = None
    proposed_change: dict[str, Any] | None = None
    awaiting_answer: bool
    question: str | None = None


class QuoteOutcomeOut(ORMModel):
    id: int
    result: str
    actual_production_mins: Decimal | None = None
    recorded_at: datetime
    recorded_by: str | None = None
    notes: str | None = None


class QuoteOut(ORMModel):
    id: int
    version: int
    status: str
    material_total: Decimal
    labour_total: Decimal
    subtotal: Decimal
    margin_pct: Decimal
    margin_value: Decimal
    quote_value: Decimal
    adjustments: list[dict[str, Any]] | None = None
    applied_rule_ids: list[int] | None = None
    min_value_applied: bool
    lead_time_days: int | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    outlook_draft_id: str | None = None
    lines: list[QuoteLineOut] = Field(default_factory=list)
    notes: list[QuoteNoteOut] = Field(default_factory=list)
    flags: list[FlagOut] = Field(default_factory=list)
    outcome: QuoteOutcomeOut | None = None


class CustomerOut(ORMModel):
    id: int
    name: str
    domain: str | None = None
    default_margin_pct: Decimal
    default_lead_days: int
    is_material_supplied_default: bool
    requires_cert: bool
    notes: str | None = None


class OperationCostOut(BaseModel):
    """One line of the cost build-up, straight from the engine."""

    op_number: int
    process: str
    time_source: str
    total_mins: Decimal
    hourly_rate: Decimal | None = None
    computed_cost: Decimal
    is_subcontract: bool


class PartPriceOut(BaseModel):
    part_id: int | None = None
    quantity: int
    labour_total: Decimal
    material_total: Decimal
    subtotal: Decimal
    margin_value: Decimal
    value: Decimal
    unit_price: Decimal
    line_total: Decimal
    uses_untrusted_times: bool
    operation_costs: list[OperationCostOut] = Field(default_factory=list)


class BreakdownOut(BaseModel):
    """The full cost build-up, produced by the same engine that priced it."""

    labour_total: Decimal
    material_total: Decimal
    subtotal: Decimal
    margin_pct: Decimal
    margin_value: Decimal
    quote_value: Decimal
    rounding_adjustment: Decimal
    min_value_applied: bool
    uses_untrusted_times: bool
    reconciles: bool
    adjustments: list[dict[str, Any]] = Field(default_factory=list)
    parts: list[PartPriceOut] = Field(default_factory=list)


class EnquiryOut(ORMModel):
    id: int
    customer_id: int | None = None
    customer: CustomerOut | None = None
    outlook_message_id: str | None = None
    subject: str | None = None
    body_text: str | None = None
    sender_email: str | None = None
    received_at: datetime | None = None
    status: str
    customer_reference: str | None = None
    anchor_quote_id: int | None = None
    due_date: date | None = None
    turnaround_seconds: int | None = None
    error_detail: str | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)
    parts: list[PartOut] = Field(default_factory=list)
    quotes: list[QuoteOut] = Field(default_factory=list)


class WorkspaceOut(BaseModel):
    """Everything /enquiry/:id needs in one response."""

    enquiry: EnquiryOut
    current_quote: QuoteOut | None = None
    breakdown: BreakdownOut | None = None
    enquiry_flags: list[FlagOut] = Field(default_factory=list)
    blocking_flag_count: int = 0
    can_approve: bool = False
    #: Both cost paths, keyed by part id, when a part's job type is ambiguous.
    ambiguous_paths: dict[int, dict[str, PartPriceOut]] = Field(default_factory=dict)


class QueueItemOut(BaseModel):
    enquiry_id: int
    customer_name: str | None = None
    subject: str | None = None
    status: str
    received_at: datetime | None = None
    age_hours: float
    part_count: int
    job_types: list[str] = Field(default_factory=list)
    process_mix: list[str] = Field(default_factory=list)
    total_quantity: int = 0
    quote_id: int | None = None
    quote_value: Decimal | None = None
    flag_count: int = 0
    blocking_flag_count: int = 0
    #: Lowest field confidence anywhere on the enquiry, for sorting.
    lowest_confidence: float | None = None
    due_date: date | None = None


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
class PartPatch(BaseModel):
    """An estimator's override. Every changed field writes a correction_log row."""

    drawing_number: str | None = None
    revision: str | None = None
    description: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    material: str | None = None
    heat_treatment: str | None = None
    surface_coat: str | None = None
    finish_spec: str | None = None
    envelope_x: Decimal | None = None
    envelope_y: Decimal | None = None
    envelope_z: Decimal | None = None
    tightest_tolerance: str | None = None
    job_type: JobType | None = None

    model_config = ConfigDict(extra="forbid")


class OperationIn(BaseModel):
    op_number: int = Field(ge=1)
    process: Process
    description: str | None = None
    set_time_mins: Decimal = Decimal("0")
    run_time_mins_per_unit: Decimal = Decimal("0")
    subcontract_unit_cost: Decimal | None = None
    #: Defaults to manual: a time typed by a person is not a calculator output.
    time_source: TimeSource = TimeSource.MANUAL

    model_config = ConfigDict(extra="forbid")


class PriceRequest(BaseModel):
    margin_pct: Decimal | None = Field(default=None, ge=0, le=100)
    recompute_material: bool = True
    applied_rule_ids: list[int] | None = None


class NoteIn(BaseModel):
    note_text: str = Field(min_length=1, max_length=5000)


class ApproveRequest(BaseModel):
    #: Recorded on the quote. Approval is a named human act.
    lead_time_days: int | None = Field(default=None, ge=0)


class OutcomeIn(BaseModel):
    result: OutcomeResult
    actual_production_mins: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None


class FlagResolveIn(BaseModel):
    note: str | None = None


class RateIn(BaseModel):
    process: Process
    machine_group: str | None = None
    hourly_rate: Decimal = Field(gt=0)
    effective_from: date
    effective_to: date | None = None

    model_config = ConfigDict(extra="forbid")


class RateOut(ORMModel):
    id: int
    process: str
    machine_group: str | None = None
    hourly_rate: Decimal
    effective_from: date
    effective_to: date | None = None


class RuleIn(BaseModel):
    rule_key: str = Field(min_length=1, max_length=60)
    trigger_description: str | None = None
    adjustment_type: AdjustmentType = AdjustmentType.PCT
    adjustment_value: Decimal = Decimal("0")
    active: bool = True

    model_config = ConfigDict(extra="forbid")


class RuleOut(ORMModel):
    id: int
    rule_key: str
    trigger_description: str | None = None
    adjustment_type: str
    adjustment_value: Decimal
    active: bool
    promoted_from_note_id: int | None = None
    promoted_by: str | None = None
    last_reviewed_at: datetime | None = None


class StockIn(BaseModel):
    spec: str
    stock_form: str
    length_mm: Decimal = Field(gt=0)
    width_mm: Decimal | None = None
    thickness_mm: Decimal | None = None
    unit_cost: Decimal = Field(gt=0)
    kerf_mm: Decimal = Decimal("3")
    active: bool = True

    model_config = ConfigDict(extra="forbid")


class StockOut(ORMModel):
    id: int
    spec: str
    stock_form: str
    length_mm: Decimal
    width_mm: Decimal | None = None
    thickness_mm: Decimal | None = None
    unit_cost: Decimal
    kerf_mm: Decimal
    active: bool


class CustomerIn(BaseModel):
    name: str
    domain: str | None = None
    default_margin_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    default_lead_days: int = Field(default=0, ge=0)
    is_material_supplied_default: bool = False
    requires_cert: bool = False
    notes: str | None = None

    model_config = ConfigDict(extra="forbid")


class MatchOut(BaseModel):
    part_id: int
    quote_id: int | None = None
    enquiry_id: int
    drawing_number: str | None = None
    revision: str | None = None
    description: str | None = None
    quantity: int | None = None
    quote_value: str | None = None
    unit_price: str | None = None
    score: float
    reasons: list[str] = Field(default_factory=list)
    result: str | None = None
    actual_production_mins: str | None = None


class SimilarOut(BaseModel):
    """Two lanes, kept separate on purpose."""

    geometry: list[MatchOut] = Field(default_factory=list)
    problem: list[MatchOut] = Field(default_factory=list)


class SeverityCount(BaseModel):
    severity: FlagSeverity
    count: int
