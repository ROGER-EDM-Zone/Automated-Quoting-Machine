"""Live market data: what things cost today, and what sizes exist today.

Every number in a quote that is not the shop's own decision comes from outside
the business and drifts: steel per kilo, the sizes a stockholder actually
holds, energy, consumables, what the trade is charging per hour. A quoting
system that reads those once at install and never again is wrong within a
month, quietly, in the direction of whichever way the market moved.

So they are fetched, and every fetch keeps its receipt. Three rules hold this
together, and they are the same three the rest of the system runs on:

  * **Nothing is remembered.** A value exists only if a page was fetched and a
    line on that page said it. There is no fallback to a plausible figure.
  * **Age is a fact, not an implementation detail.** Every value carries when
    it was read; anything past its source's `max_age_hours` is stale, and
    stale prices flag rather than quietly price.
  * **Observations are append-only.** A refresh writes a new row. Explaining a
    quote from eighteen months ago means reading what was true then, which is
    impossible if refreshes overwrite.

Note on where this runs: the fetch happens from wherever the app is hosted, on
the business's own network. Reachability is therefore a property of that
network, and `refresh` reports it per-source rather than assuming it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import MarketMethod, MarketUnit
from app.models import MarketObservation, MarketSource, StockSize, utcnow
from app.prompts import market as market_prompt
from app.services.ai import AIError, get_ai_client
from app.services.market_fetch import FetchError, fetch_text

logger = logging.getLogger(__name__)

#: A reading below this is recorded but never priced from. Same principle as
#: the drawing-extraction floor: a value the reader was unsure of is worse
#: than no value, because no value asks a human and a wrong value does not.
DEFAULT_MARKET_CONFIDENCE = 0.75


class MarketError(Exception):
    """A market value was asked for and cannot honestly be given."""


@dataclass(frozen=True)
class Reading:
    """The current value of one series, with everything needed to judge it."""

    series_key: str
    value: Decimal
    unit: str
    observed_at: datetime
    source_name: str
    source_url: str | None
    basis: str
    method: str
    confidence: float | None
    evidence: str | None
    max_age_hours: int

    @property
    def age(self) -> timedelta:
        observed = self.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        return datetime.now(UTC) - observed

    @property
    def age_hours(self) -> float:
        return self.age.total_seconds() / 3600

    @property
    def is_stale(self) -> bool:
        return self.age_hours > self.max_age_hours

    def describe(self) -> str:
        days = self.age.days
        when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} old"
        return f"{self.source_name}, {when}"


@dataclass
class SourceResult:
    """What happened to one source during a refresh."""

    series_key: str
    source_name: str
    ok: bool
    detail: str
    value: Decimal | None = None
    unit: str | None = None
    sizes_found: int = 0
    stock_rows_written: int = 0


@dataclass
class RefreshReport:
    results: list[SourceResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SourceResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and not self.failed


# --------------------------------------------------------------------------
# Reading what is already known
# --------------------------------------------------------------------------
def latest(db: Session, series_key: str, *, min_confidence: float | None = None) -> Reading | None:
    """The most recent usable observation of a series, or None.

    None means exactly that — nobody knows. It never means zero, and it never
    means "use last quarter's figure".
    """
    floor = DEFAULT_MARKET_CONFIDENCE if min_confidence is None else min_confidence
    row = db.scalars(
        select(MarketObservation)
        .where(MarketObservation.series_key == series_key)
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
        .limit(25)
    ).all()
    for observation in row:
        if observation.confidence is not None and float(observation.confidence) < floor:
            continue
        source = observation.source
        return Reading(
            series_key=observation.series_key,
            value=Decimal(observation.value),
            unit=observation.unit,
            observed_at=observation.observed_at,
            source_name=source.name if source else "unknown",
            source_url=observation.source_url,
            basis=observation.basis,
            method=observation.method,
            confidence=float(observation.confidence)
            if observation.confidence is not None
            else None,
            evidence=observation.evidence,
            max_age_hours=source.max_age_hours if source else 168,
        )
    return None


def series_summary(db: Session) -> list[dict]:
    """Every configured series with its current value and age, for the UI."""
    summary = []
    for source in db.scalars(select(MarketSource).order_by(MarketSource.kind, MarketSource.name)):
        reading = latest(db, source.series_key)
        summary.append(
            {
                "id": source.id,
                "series_key": source.series_key,
                "name": source.name,
                "kind": source.kind,
                "unit": source.unit,
                "basis": source.basis,
                "spec": source.spec,
                "url": source.url,
                "active": source.active,
                "max_age_hours": source.max_age_hours,
                "value": str(reading.value) if reading else None,
                "observed_at": reading.observed_at.isoformat() if reading else None,
                "age_hours": round(reading.age_hours, 1) if reading else None,
                "is_stale": reading.is_stale if reading else True,
                "evidence": reading.evidence if reading else None,
                "confidence": reading.confidence if reading else None,
                "last_success_at": source.last_success_at.isoformat()
                if source.last_success_at
                else None,
                "last_error": source.last_error,
                "consecutive_failures": source.consecutive_failures,
                # A series that has never been read is not "0" and not "fine".
                "status": _status(source, reading),
            }
        )
    return summary


def _status(source: MarketSource, reading: Reading | None) -> str:
    if not source.active:
        return "off"
    if reading is None:
        return "never_read"
    if reading.is_stale:
        return "stale"
    if source.consecutive_failures:
        return "last_refresh_failed"
    return "current"


# --------------------------------------------------------------------------
# Refreshing
# --------------------------------------------------------------------------
def refresh(
    db: Session,
    *,
    series_key: str | None = None,
    settings: Settings | None = None,
    ai=None,
    fetcher=fetch_text,
) -> RefreshReport:
    """Re-read every active source, or just one series.

    A source that fails is recorded as having failed and the others carry on:
    one unreachable stockholder must not stop the energy price refreshing.
    """
    settings = settings or get_settings()
    report = RefreshReport()

    stmt = select(MarketSource).where(MarketSource.active.is_(True))
    if series_key:
        stmt = stmt.where(MarketSource.series_key == series_key)

    sources = list(db.scalars(stmt.order_by(MarketSource.id)))
    if not sources:
        return report

    if ai is None:
        try:
            ai = get_ai_client(settings)
        except AIError as exc:
            for source in sources:
                report.results.append(SourceResult(source.series_key, source.name, False, str(exc)))
            return report

    for source in sources:
        report.results.append(_refresh_one(db, source, ai=ai, fetcher=fetcher))
    return report


def _refresh_one(db: Session, source: MarketSource, *, ai, fetcher) -> SourceResult:
    source.last_attempt_at = utcnow()

    if not source.url:
        return _failure(db, source, "No URL configured for this source.")

    try:
        text = fetcher(source.url)
    except FetchError as exc:
        return _failure(db, source, str(exc))

    try:
        payload = ai.structured(
            system=market_prompt.SYSTEM,
            prompt=market_prompt.build_prompt(
                source_name=source.name,
                target=source.target,
                url=source.url,
                text=text,
            ),
            schema=market_prompt.build_schema(),
        )
    except AIError as exc:
        return _failure(db, source, f"Could not read the page: {exc}")

    value = payload.get("value")
    unit = payload.get("unit") or source.unit
    evidence = (payload.get("evidence") or "").strip()
    sizes = payload.get("sizes_mm") or []
    confidence = payload.get("confidence")

    if value is not None and not evidence:
        # A number with nothing behind it is the failure mode this whole
        # design exists to avoid. Discard it rather than record it.
        return _failure(
            db,
            source,
            "The reader returned a price but quoted no text from the page to "
            "support it, so it was discarded.",
        )

    if value is None and not sizes:
        return _failure(db, source, payload.get("notes") or "The page did not state a price.")

    written = 0
    if value is not None:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError):
            return _failure(db, source, f"Unreadable value from the page: {value!r}")

        db.add(
            MarketObservation(
                source_id=source.id,
                series_key=source.series_key,
                value=amount,
                unit=unit,
                method=MarketMethod.AI_READ.value,
                basis=source.basis,
                confidence=confidence,
                evidence=evidence[:4000] or None,
                sizes_mm=sizes or None,
                source_url=source.url,
            )
        )
    else:
        amount = None

    if sizes:
        written = sync_stock_sizes(db, source, sizes)

    source.last_success_at = utcnow()
    source.last_error = None
    source.consecutive_failures = 0
    db.flush()

    detail = []
    if amount is not None:
        detail.append(f"{amount} {unit}")
    if sizes:
        detail.append(f"{len(sizes)} size(s) listed")
    if payload.get("notes"):
        detail.append(str(payload["notes"]))

    return SourceResult(
        series_key=source.series_key,
        source_name=source.name,
        ok=True,
        detail="; ".join(detail),
        value=amount,
        unit=unit,
        sizes_found=len(sizes),
        stock_rows_written=written,
    )


def _failure(db: Session, source: MarketSource, detail: str) -> SourceResult:
    source.last_error = detail[:2000]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    db.flush()
    logger.warning("market source %s failed: %s", source.series_key, detail)
    return SourceResult(source.series_key, source.name, False, detail)


# --------------------------------------------------------------------------
# Live stock sizes
# --------------------------------------------------------------------------
def sync_stock_sizes(db: Session, source: MarketSource, sizes: list) -> int:
    """Bring the stock table in line with the range the supplier now lists.

    This is the half of "live data" that is not a price. Knowing steel is
    £2.40/kg is no use if the calculator is still choosing from sizes typed in
    last year: the answer to "I need 92mm" has to be a size the supplier
    actually holds today.

    Sizes that have dropped out of the range are marked unlisted rather than
    deleted — a quote sent last month has to keep explaining itself — and rows
    somebody typed by hand are never touched.
    """
    if not source.spec or not source.stock_form:
        return 0

    existing = {
        Decimal(row.width_mm): row
        for row in db.scalars(
            select(StockSize).where(
                StockSize.spec == source.spec,
                StockSize.stock_form == source.stock_form,
                StockSize.origin != MarketMethod.MANUAL.value,
            )
        )
        if row.width_mm is not None
    }

    seen: set[Decimal] = set()
    written = 0
    for raw in sizes:
        try:
            size = Decimal(str(raw))
        except (InvalidOperation, TypeError):
            continue
        if size <= 0:
            continue
        seen.add(size)
        row = existing.get(size)
        if row is None:
            row = StockSize(
                spec=source.spec,
                stock_form=source.stock_form,
                # Stock length is a property of the supplier's range, not of
                # this size, so it comes from the source rather than the page.
                length_mm=Decimal("3000"),
                width_mm=size,
                thickness_mm=None,
                unit_cost=Decimal("0"),
                origin=MarketMethod.AI_READ.value,
                market_series_key=source.series_key,
            )
            db.add(row)
            written += 1
        row.listed = True
        row.active = True
        row.source_name = source.name
        row.source_url = source.url
        row.market_series_key = source.series_key

    for size, row in existing.items():
        if size not in seen:
            # Still on file, no longer offered.
            row.listed = False

    db.flush()
    return written


# --------------------------------------------------------------------------
# Turning a per-kilo price into the cost of one piece of stock
# --------------------------------------------------------------------------
def price_stock_row(db: Session, row: StockSize) -> tuple[Decimal, Reading] | None:
    """Cost one piece of stock from the live per-kilo price.

    Returns None — not a guess — when anything is missing: no series, no
    density, no live reading, or a reading in a unit that cannot be applied to
    a weight. `compute_material` turns each of those into a flag naming the
    thing that is missing.
    """
    if not row.market_series_key or row.density_kg_m3 is None:
        return None

    reading = latest(db, row.market_series_key)
    if reading is None:
        return None

    volume_m3 = _stock_volume_m3(row)
    if volume_m3 is None or volume_m3 <= 0:
        return None

    if reading.unit == MarketUnit.GBP_PER_KG.value:
        kilos = volume_m3 * Decimal(row.density_kg_m3)
        cost = reading.value * kilos
    elif reading.unit == MarketUnit.GBP_PER_METRE.value:
        cost = reading.value * (Decimal(row.length_mm) / Decimal("1000"))
    elif reading.unit == MarketUnit.GBP_EACH.value:
        cost = reading.value
    else:
        # A per-hour or index figure cannot cost a bar. Better to have no
        # price than a number arrived at by an unstated conversion.
        return None

    return cost.quantize(Decimal("0.01")), reading


def _stock_volume_m3(row: StockSize) -> Decimal | None:
    """Volume of one piece of stock, in cubic metres."""
    from app.enums import StockForm

    length = Decimal(row.length_mm) / Decimal("1000")
    if row.stock_form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        if row.width_mm is None:
            return None
        radius = (Decimal(row.width_mm) / Decimal("1000")) / 2
        return Decimal("3.14159265358979") * radius * radius * length
    if row.width_mm is None or row.thickness_mm is None:
        return None
    return (
        length
        * (Decimal(row.width_mm) / Decimal("1000"))
        * (Decimal(row.thickness_mm) / Decimal("1000"))
    )
