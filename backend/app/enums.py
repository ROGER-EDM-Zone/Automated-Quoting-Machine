"""Controlled vocabularies.

These are deliberately narrow. `Process` in particular is the ERP handoff
surface (spec section 6: "Keep operation ERP-clean ... controlled process
enum ... never free text") so adding a member here is a schema decision, not
a convenience.
"""

from enum import Enum


class StrEnum(str, Enum):
    """str-valued enum: serialises as its value, compares to plain strings."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class EnquiryStatus(StrEnum):
    RECEIVED = "received"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    CLASSIFIED = "classified"
    PRICED = "priced"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SENT = "sent"
    WON = "won"
    LOST = "lost"
    # Off to the side of the happy path, reachable from any stage.
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


#: The linear progression. `needs_attention` and `failed` sit outside it.
ENQUIRY_PIPELINE: tuple[EnquiryStatus, ...] = (
    EnquiryStatus.RECEIVED,
    EnquiryStatus.EXTRACTING,
    EnquiryStatus.EXTRACTED,
    EnquiryStatus.CLASSIFIED,
    EnquiryStatus.PRICED,
    EnquiryStatus.IN_REVIEW,
    EnquiryStatus.APPROVED,
    EnquiryStatus.SENT,
)

TERMINAL_ENQUIRY_STATUSES = frozenset({EnquiryStatus.WON, EnquiryStatus.LOST})


class AttachmentKind(StrEnum):
    DRAWING = "drawing"
    STEP = "step"
    SPREADSHEET = "spreadsheet"
    OTHER = "other"


class JobType(StrEnum):
    SERVICE_ONLY = "service_only"
    FULL_SUPPLY = "full_supply"
    AMBIGUOUS = "ambiguous"


class Process(StrEnum):
    CNC_MILL = "cnc_mill"
    CNC_TURN = "cnc_turn"
    WIRE_EDM = "wire_edm"
    SPARK_ERODE = "spark_erode"
    GRIND = "grind"
    MANUAL = "manual"
    QC = "qc"
    SUBCONTRACT = "subcontract"


#: Processes the classifier may assign as the shop's own production mix.
#: `qc` and `manual` are support operations; `subcontract` is bought out.
PRODUCTION_PROCESSES = frozenset(
    {
        Process.CNC_MILL,
        Process.CNC_TURN,
        Process.WIRE_EDM,
        Process.SPARK_ERODE,
        Process.GRIND,
    }
)


class TimeSource(StrEnum):
    """Where an operation's minutes came from.

    The UI must render these differently (spec section 4/6): an estimator has
    to be able to tell at a glance which numbers to trust blind.
    """

    CALCULATOR = "calculator"
    HISTORICAL_ESTIMATE = "historical_estimate"
    MANUAL = "manual"


#: Times an estimator should check rather than trust blind.
UNTRUSTED_TIME_SOURCES = frozenset({TimeSource.HISTORICAL_ESTIMATE})


class FlagCategory(StrEnum):
    EXTRACTION_UNCERTAINTY = "extraction_uncertainty"
    TOLERANCE_RISK = "tolerance_risk"
    INDUSTRY_EXPERIENCE = "industry_experience"
    COMMERCIAL_JUDGEMENT = "commercial_judgement"
    VERSION_CONFLICT = "version_conflict"
    DUPLICATE_RFQ = "duplicate_rfq"


class FlagSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SENT = "sent"
    SUPERSEDED = "superseded"


class NoteKind(StrEnum):
    FACT_CORRECTION = "fact_correction"
    COMMERCIAL_INSTRUCTION = "commercial_instruction"


class OutcomeResult(StrEnum):
    WON = "won"
    LOST = "lost"
    NO_RESPONSE = "no_response"
    WITHDRAWN = "withdrawn"


class AdjustmentType(StrEnum):
    PCT = "pct"
    FIXED = "fixed"
    FLAG_ONLY = "flag_only"


class RuleKey(StrEnum):
    RUSH_UPLIFT = "rush_uplift"
    DIFFICULT_JOB_CONTINGENCY = "difficult_job_contingency"
    MIN_QUOTE_VALUE = "min_quote_value"


class StockForm(StrEnum):
    PLATE = "plate"
    BAR_ROUND = "bar_round"
    BAR_SQUARE = "bar_square"
    TUBE = "tube"
    BILLET = "billet"
