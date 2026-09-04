"""Live market data: the receipts, the staleness, and the refusal to guess.

The behaviour under test is mostly *negative*. It is easy to build something
that returns a plausible steel price; the whole value here is that it returns
nothing at all unless a page was fetched and a line on that page said so.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.enums import MarketBasis, MarketKind, MarketMethod, MarketUnit, StockForm
from app.models import MarketObservation, MarketSource, StockSize
from app.services.ai import AIError, StubAIClient
from app.services.market import (
    latest,
    price_stock_row,
    refresh,
    series_summary,
    sync_stock_sizes,
)
from app.services.market_fetch import FetchError, to_text


def source(db, **overrides):
    row = MarketSource(
        series_key=overrides.pop("series_key", "material:en16:round_bar"),
        name=overrides.pop("name", "Test Stockholder"),
        kind=overrides.pop("kind", MarketKind.MATERIAL_PRICE.value),
        unit=overrides.pop("unit", MarketUnit.GBP_PER_KG.value),
        basis=overrides.pop("basis", MarketBasis.RETAIL_ONLINE.value),
        url=overrides.pop("url", "https://example.test/en16"),
        spec=overrides.pop("spec", "EN16"),
        stock_form=overrides.pop("stock_form", StockForm.BAR_ROUND.value),
        max_age_hours=overrides.pop("max_age_hours", 168),
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


def reader(**payload):
    """An AI client that returns one reading."""
    base = {
        "value": 2.40,
        "unit": MarketUnit.GBP_PER_KG.value,
        "confidence": 0.94,
        "evidence": "EN16T round bar — £2.40 per kg (ex VAT)",
        "sizes_mm": None,
        "notes": None,
    }
    base.update(payload)
    return StubAIClient([base])


def always(text):
    return lambda url, **kwargs: text


def never(message="blocked by the network"):
    def _fetch(url, **kwargs):
        raise FetchError(message)

    return _fetch


# --------------------------------------------------------------------------
# The refusal to guess
# --------------------------------------------------------------------------
def test_a_price_with_no_quoted_evidence_is_discarded(db):
    row = source(db)
    report = refresh(db, ai=reader(evidence=""), fetcher=always("some page text"))

    assert report.failed
    assert "quoted no text" in report.failed[0].detail
    assert db.query(MarketObservation).count() == 0
    assert latest(db, row.series_key) is None


def test_a_page_that_states_no_price_records_nothing(db):
    source(db)
    report = refresh(
        db,
        ai=reader(value=None, evidence=None, notes="Price on application"),
        fetcher=always("Call us for a price"),
    )

    assert report.failed
    assert db.query(MarketObservation).count() == 0


def test_an_unreachable_source_says_so_and_does_not_invent_a_figure(db):
    row = source(db)
    report = refresh(db, ai=reader(), fetcher=never("Could not reach example.test"))

    assert report.failed
    assert "Could not reach" in report.failed[0].detail
    assert latest(db, row.series_key) is None
    db.refresh(row)
    assert row.consecutive_failures == 1
    assert "Could not reach" in row.last_error


def test_one_failing_source_does_not_stop_the_others(db):
    source(db, series_key="a", url="https://a.test")
    source(db, series_key="b", url="https://b.test")

    calls = {"n": 0}

    def flaky(url, **kwargs):
        calls["n"] += 1
        if "a.test" in url:
            raise FetchError("down")
        return "EN16 £2.40/kg"

    report = refresh(
        db,
        ai=StubAIClient(
            [
                {
                    "value": 2.40,
                    "unit": MarketUnit.GBP_PER_KG.value,
                    "confidence": 0.9,
                    "evidence": "£2.40/kg",
                    "sizes_mm": None,
                    "notes": None,
                }
            ]
        ),
        fetcher=flaky,
    )

    assert len(report.failed) == 1
    assert len(report.succeeded) == 1
    assert latest(db, "b") is not None


def test_a_low_confidence_reading_is_recorded_but_never_returned(db):
    row = source(db)
    refresh(db, ai=reader(confidence=0.30), fetcher=always("page"))

    assert db.query(MarketObservation).count() == 1  # kept, for the audit trail
    assert latest(db, row.series_key) is None  # but not used


def test_with_no_api_key_every_source_fails_rather_than_defaulting(db):
    source(db)

    class NoKey:
        def structured(self, **kwargs):
            raise AIError("no key")

    report = refresh(db, ai=NoKey(), fetcher=always("page"))
    assert report.failed
    assert latest(db, "material:en16:round_bar") is None


# --------------------------------------------------------------------------
# Age
# --------------------------------------------------------------------------
def observation(db, row, *, value="2.40", age_hours=0, confidence=0.95):
    obs = MarketObservation(
        source_id=row.id,
        series_key=row.series_key,
        value=Decimal(value),
        unit=row.unit,
        method=MarketMethod.AI_READ.value,
        basis=row.basis,
        confidence=confidence,
        evidence="£2.40 per kg",
        source_url=row.url,
        observed_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    db.add(obs)
    db.commit()
    return obs


def test_a_fresh_reading_is_not_stale(db):
    row = source(db, max_age_hours=168)
    observation(db, row, age_hours=2)
    assert latest(db, row.series_key).is_stale is False


def test_a_reading_past_its_sources_limit_is_stale(db):
    row = source(db, max_age_hours=24)
    observation(db, row, age_hours=48)

    reading = latest(db, row.series_key)
    assert reading.is_stale is True
    # Still returned — hiding it would just mean nobody knows there is a price.
    assert reading.value == Decimal("2.40")


def test_the_limit_is_per_source_because_steel_moves_faster_than_energy(db):
    steel = source(db, series_key="steel", max_age_hours=24)
    energy = source(db, series_key="energy", url="https://e.test", max_age_hours=2160)
    observation(db, steel, age_hours=100)
    observation(db, energy, age_hours=100)

    assert latest(db, "steel").is_stale is True
    assert latest(db, "energy").is_stale is False


def test_the_newest_reading_wins(db):
    row = source(db)
    observation(db, row, value="2.00", age_hours=48)
    observation(db, row, value="2.75", age_hours=1)
    assert latest(db, row.series_key).value == Decimal("2.75")


def test_observations_are_never_overwritten(db):
    source(db)
    refresh(db, ai=reader(), fetcher=always("page one"))
    refresh(db, ai=reader(value=2.90), fetcher=always("page two"))

    values = sorted(Decimal(o.value) for o in db.query(MarketObservation).all())
    assert values == [Decimal("2.4000"), Decimal("2.9000")]


def test_a_series_nobody_has_read_reports_never_read_not_zero(db):
    source(db)
    row = series_summary(db)[0]
    assert row["value"] is None
    assert row["status"] == "never_read"
    assert row["is_stale"] is True


# --------------------------------------------------------------------------
# Live stock sizes
# --------------------------------------------------------------------------
def test_the_supplier_range_becomes_the_calculators_stock_list(db):
    row = source(db)
    written = sync_stock_sizes(db, row, [70, 80, 90, 100])

    assert written == 4
    sizes = {Decimal(s.width_mm) for s in db.query(StockSize).all()}
    assert sizes == {Decimal("70"), Decimal("80"), Decimal("90"), Decimal("100")}


def test_a_size_that_drops_out_of_the_range_is_unlisted_not_deleted(db):
    row = source(db)
    sync_stock_sizes(db, row, [70, 80, 90])
    sync_stock_sizes(db, row, [70, 90])

    by_size = {Decimal(s.width_mm): s for s in db.query(StockSize).all()}
    assert len(by_size) == 3  # nothing lost
    assert by_size[Decimal("80")].listed is False
    assert by_size[Decimal("70")].listed is True


def test_a_size_that_comes_back_is_listed_again(db):
    row = source(db)
    sync_stock_sizes(db, row, [70, 80])
    sync_stock_sizes(db, row, [70])
    sync_stock_sizes(db, row, [70, 80])

    by_size = {Decimal(s.width_mm): s for s in db.query(StockSize).all()}
    assert by_size[Decimal("80")].listed is True


def test_a_hand_entered_stock_row_is_never_touched_by_a_refresh(db):
    row = source(db)
    mine = StockSize(
        spec="EN16",
        stock_form=StockForm.BAR_ROUND.value,
        length_mm=Decimal("3000"),
        width_mm=Decimal("85"),
        thickness_mm=None,
        unit_cost=Decimal("120.00"),
        origin=MarketMethod.MANUAL.value,
    )
    db.add(mine)
    db.commit()

    sync_stock_sizes(db, row, [70, 90])
    db.refresh(mine)

    assert mine.listed is True
    assert mine.unit_cost == Decimal("120.00")


# --------------------------------------------------------------------------
# Turning a per-kilo price into the cost of a bar
# --------------------------------------------------------------------------
def steel_bar(db, diameter="100", length="3000", density="7850", series="material:en16:round_bar"):
    row = StockSize(
        spec="EN16",
        stock_form=StockForm.BAR_ROUND.value,
        length_mm=Decimal(length),
        width_mm=Decimal(diameter),
        thickness_mm=None,
        unit_cost=Decimal("0"),
        density_kg_m3=Decimal(density) if density else None,
        market_series_key=series,
        origin=MarketMethod.AI_READ.value,
    )
    db.add(row)
    db.commit()
    return row


def test_a_bar_is_costed_from_the_live_price_and_its_own_weight(db):
    src = source(db)
    observation(db, src, value="2.40")
    bar = steel_bar(db)

    priced = price_stock_row(db, bar)

    assert priced is not None
    cost, reading = priced
    # 100mm dia x 3m = 0.02356 m3 x 7850 kg/m3 = 184.9 kg x £2.40
    assert Decimal("440") < cost < Decimal("450")
    assert reading.source_name == "Test Stockholder"


def test_no_density_means_no_price_rather_than_an_assumed_one(db):
    src = source(db)
    observation(db, src)
    assert price_stock_row(db, steel_bar(db, density=None)) is None


def test_no_live_reading_means_no_price(db):
    source(db)
    assert price_stock_row(db, steel_bar(db)) is None


def test_a_stock_row_pointing_at_nothing_gets_no_price(db):
    src = source(db)
    observation(db, src)
    assert price_stock_row(db, steel_bar(db, series=None)) is None


def test_an_hourly_rate_is_not_silently_applied_to_a_weight(db):
    src = source(db, series_key="labour:uk_machining", unit=MarketUnit.GBP_PER_HOUR.value)
    observation(db, src, value="68.00")
    bar = steel_bar(db, series="labour:uk_machining")

    # It is a real, current, confident reading — in a unit that cannot cost a
    # bar. No conversion is better than an invented one.
    assert price_stock_row(db, bar) is None


def test_a_per_metre_price_costs_the_bar_by_its_length(db):
    src = source(db, series_key="m", unit=MarketUnit.GBP_PER_METRE.value)
    observation(db, src, value="30.00")
    cost, _ = price_stock_row(db, steel_bar(db, series="m"))
    assert cost == Decimal("90.00")


# --------------------------------------------------------------------------
# The fetcher's HTML handling
# --------------------------------------------------------------------------
def test_table_rows_survive_as_lines_so_a_price_list_stays_readable():
    html = """
    <table><tr><td>Diameter</td><td>Price</td></tr>
    <tr><td>90mm</td><td>£270.00</td></tr>
    <tr><td>100mm</td><td>£300.00</td></tr></table>
    """
    text = to_text(html)
    assert "90mm | £270.00" in text
    assert "100mm | £300.00" in text


def test_scripts_and_styles_are_stripped():
    text = to_text("<style>.a{color:red}</style><script>var p=1;</script><p>£2.40/kg</p>")
    assert "color:red" not in text
    assert "var p" not in text
    assert "£2.40/kg" in text


@pytest.mark.parametrize("entity,expected", [("&pound;2.40", "£2.40"), ("&amp;", "&")])
def test_html_entities_are_decoded(entity, expected):
    assert expected in to_text(f"<p>{entity}</p>")
