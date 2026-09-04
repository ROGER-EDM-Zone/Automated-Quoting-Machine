"""Controlled vocabularies.

These are deliberately narrow. `Process` in particular is the ERP handoff
surface (spec section 6: "Keep operation ERP-clean ... controlled process
enum ... never free text") so adding a member here is a schema decision, not
a convenience.
"""

from enum import StrEnum


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
    #: Part marking. A distinct machine and a real operation — easy to leave
    #: off a quote by accident, which is exactly why it gets its own process
    #: rather than being buried in `manual`.
    LASER_ETCH = "laser_etch"
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
        Process.LASER_ETCH,
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
    #: A length in millimetres, not money. Kept in the rules table because it
    #: is a shop rule the business changes, but it must never reach the
    #: pricing engine as an adjustment.
    MM = "mm"


#: Adjustment types the pricing engine will act on. Anything else in the
#: rules table is a setting the business edits, not money.
MONETARY_ADJUSTMENTS = frozenset({AdjustmentType.PCT, AdjustmentType.FIXED})


class RuleKey(StrEnum):
    RUSH_UPLIFT = "rush_uplift"
    DIFFICULT_JOB_CONTINGENCY = "difficult_job_contingency"
    MIN_QUOTE_VALUE = "min_quote_value"
    #: Millimetres left on the diameter (or each section face) for clean-up,
    #: before any stock size is looked at. The shop's "3-5mm on the OD".
    MATERIAL_ALLOWANCE_SECTION = "material_allowance_section"
    #: Millimetres left on the length of one part, before the parting kerf.
    MATERIAL_ALLOWANCE_LENGTH = "material_allowance_length"


class StockForm(StrEnum):
    PLATE = "plate"
    BAR_ROUND = "bar_round"
    BAR_SQUARE = "bar_square"
    TUBE = "tube"
    BILLET = "billet"


class MarketKind(StrEnum):
    """What a market series measures.

    Deliberately broad — the point of this layer is that *every* number that
    drifts with the outside world is refreshed the same way, not just steel.
    """

    MATERIAL_PRICE = "material_price"
    LABOUR_RATE = "labour_rate"
    CONSUMABLE = "consumable"
    ENERGY = "energy"
    SUBCONTRACT = "subcontract"
    INDEX = "index"


class MarketUnit(StrEnum):
    """The unit an observation is in. Never inferred — a source states it."""

    GBP_PER_KG = "gbp_per_kg"
    GBP_PER_METRE = "gbp_per_metre"
    GBP_PER_HOUR = "gbp_per_hour"
    GBP_PER_KWH = "gbp_per_kwh"
    GBP_EACH = "gbp_each"
    INDEX_POINTS = "index_points"


class MarketMethod(StrEnum):
    """How a value was obtained, because it changes how much to trust it.

    ``AI_READ`` means a model read a page this app fetched and reported what
    the page said, with a confidence score and a quoted excerpt. It never
    means the model recalled a price — nothing in this system prices off a
    remembered number.
    """

    SCRAPED = "scraped"
    AI_READ = "ai_read"
    MANUAL = "manual"


class MarketBasis(StrEnum):
    """Whose price this is, which matters more than the figure itself.

    A published web price is small-quantity retail. A shop buying on account
    pays less, and quoting off the retail figure quietly pads every job.
    """

    RETAIL_ONLINE = "retail_online"
    TRADE_PUBLISHED = "trade_published"
    SUPPLIER_QUOTE = "supplier_quote"
    SURVEY = "survey"
    INDEX = "index"
